"""Real process execution plus adversarial exact-grant tests; no model dependency."""
import asyncio
from dataclasses import replace
import os
import time

import pytest

from src import containment, tool_execution
from src.agent_runtime.authority import OperationGrant, RequestAuthority
from src.host_execution_approval import HOST_ONCE, host_execution_reason
from src.tool_approvals import ToolApprovalStore
from src.tool_capabilities import ToolRunSecurityContext, capabilities_for_action
from src.tool_types import ToolBlock


@pytest.fixture(autouse=True)
def isolated_host(tmp_path, monkeypatch):
    from src.agent_runtime import process_resources
    monkeypatch.setattr(tool_execution, "_owner_is_admin", lambda owner: True)
    monkeypatch.setattr(containment, "CONTAINMENT_MODE", containment.MODE_ENFORCING)
    monkeypatch.setattr(containment, "MECHANISMS", tuple(
        m for m in containment.MECHANISMS if m.name == ("windows_tree" if os.name == "nt" else "process_group")))
    monkeypatch.setattr(containment, "_store_path", lambda: tmp_path / "control" / "containment.json")
    monkeypatch.setattr(process_resources, "_LAUNCH_DIR", tmp_path / "control" / "launches")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return workspace


def authority(workspace, *tools):
    return RequestAuthority("host-approval-test", "alice", "host-test", str(workspace),
                            tuple(OperationGrant(t) for t in tools))


def propose(auth, *, tool="bash", content="printf host-approved", profile=HOST_ONCE,
            store=None, untrusted=False):
    store = store or ToolApprovalStore()
    pending = store.create(owner=auth.owner, session_id=auth.session_id, origin_run_id="origin-run",
        tool_name=tool, content=content, workspace=auth.workspace, request_authority=auth,
        external_untrusted_context_seen=untrusted, capabilities=capabilities_for_action(tool, content),
        execution_profile=profile)
    return store, pending


def approve(store, pending, decision="approve_task"):
    return store.consume(pending.approval_id, decision=decision, owner=pending.owner,
                         session_id=pending.session_id)


async def dispatch(auth, *, tool="bash", content="printf host-approved", exact=None, **kwargs):
    return (await tool_execution.execute_tool_block(ToolBlock(tool, content), owner=auth.owner,
        session_id=auth.session_id, workspace=auth.workspace, request_authority=auth,
        exact_approval=exact, security_context=ToolRunSecurityContext(), **kwargs))[1]


@pytest.mark.parametrize("tool,content,marker", [
    ("bash", "printf host-approved", "host-approved"),
    ("python", "print('python-host-approved')", "python-host-approved"),
])
async def test_real_native_action_runs_only_after_exact_approval(isolated_host, tool, content, marker):
    auth = authority(isolated_host, tool)
    refused = await dispatch(auth, tool=tool, content=content)
    assert refused["containment"]["executed"] is False
    store, pending = propose(auth, tool=tool, content=content)
    assert pending.process_operation is not None
    exact = approve(store, pending)
    assert exact.allow_remaining_actions is False and not exact.grants_chat_session
    result = await dispatch(auth, tool=tool, content=content, exact=exact)
    assert result["exit_code"] == 0, result
    assert marker in result["output"]
    boundary = result["containment"]
    assert boundary["mode"] == containment.MODE_HOST_APPROVED
    assert boundary["contained"] is False and boundary["executed"] is True
    assert boundary["unenforced_required"] == ["filesystem", "process_tree"]
    assert result["teardown"]["process_tree_verified"] is False
    replay = await dispatch(auth, tool=tool, content=content, exact=exact)
    assert replay.get("blocked") is True
    subsequent = await dispatch(auth, tool=tool, content=content)
    assert subsequent["containment"]["executed"] is False
    assert containment.CONTAINMENT_MODE == containment.MODE_ENFORCING


async def test_real_move_requires_approval_and_can_be_rolled_back(isolated_host):
    auth = authority(isolated_host, "python")
    source = isolated_host / "sample-shortcut.txt"
    source.write_text("fixture")
    content = "from pathlib import Path\np=Path('Organized')\np.mkdir()\nPath('sample-shortcut.txt').rename(p/'sample-shortcut.txt')\nprint('moved')"
    store, pending = propose(auth, tool="python", content=content)
    assert source.exists() and not (isolated_host / "Organized").exists()
    result = await dispatch(auth, tool="python", content=content, exact=approve(store, pending))
    assert result["exit_code"] == 0, result
    target = isolated_host / "Organized" / source.name
    assert not source.exists() and target.read_text() == "fixture"
    target.rename(source)
    target.parent.rmdir()
    assert source.read_text() == "fixture"


@pytest.mark.parametrize("field,value", [
    ("content", "printf changed"), ("tool", "python"),
    ("owner", "bob"), ("session_id", "other-chat"), ("workspace", "other"),
])
async def test_changed_action_cannot_reuse_host_approval(isolated_host, field, value):
    auth = authority(isolated_host, "bash", "python")
    store, pending = propose(auth)
    exact = approve(store, pending)
    tool, content = "bash", "printf host-approved"
    if field in {"owner", "session_id", "workspace"}:
        if field == "workspace":
            other = isolated_host.parent / value
            other.mkdir()
            value = str(other)
        fields = {"owner": auth.owner, "session_id": auth.session_id, "workspace": auth.workspace}
        fields[field] = value
        auth = RequestAuthority(auth.request_id, fields["owner"], fields["session_id"],
                                fields["workspace"], auth.grants)
    elif field == "tool":
        tool = value
    else:
        content = value
    result = await dispatch(auth, tool=tool, content=content, exact=exact)
    assert result.get("blocked"), result
    assert not exact._claimed


@pytest.mark.parametrize("decision", ["deny", "approve", "invented"])
def test_host_grant_cannot_become_chat_or_task_authority(isolated_host, decision):
    store, pending = propose(authority(isolated_host, "bash"))
    assert approve(store, pending, decision) is None


async def test_normal_chat_approval_does_not_relax_containment(isolated_host):
    auth = authority(isolated_host, "bash")
    store, pending = propose(auth, profile="workspace_sandbox", untrusted=True)
    exact = approve(store, pending, "approve")
    assert exact.grants_chat_session
    result = (await tool_execution.execute_tool_block(ToolBlock("bash", pending.content),
        owner=auth.owner, session_id=auth.session_id, workspace=auth.workspace,
        request_authority=auth, exact_approval=exact,
        security_context=ToolRunSecurityContext(external_untrusted_context_seen=True)))[1]
    assert result["containment"]["executed"] is False


def test_host_profile_is_digest_bound_and_displayed(isolated_host):
    auth = authority(isolated_host, "bash")
    store, pending = propose(auth)
    payload = pending.public_payload()
    assert payload["action"]["execution_profile"] == HOST_ONCE
    assert [x["value"] for x in payload["options"]] == ["approve_task", "approve_host_chat", "deny"]
    assert "outside the workspace" in payload["description"]
    exact = approve(store, pending)
    exact.pending = replace(pending, execution_profile="workspace_sandbox")
    assert not exact.matches(owner=auth.owner, session_id=auth.session_id, workspace=auth.workspace,
                             tool_name="bash", content=pending.content)


def test_expired_approval_cannot_execute(isolated_host):
    store = ToolApprovalStore(ttl_seconds=1)
    store, pending = propose(authority(isolated_host, "bash"), store=store)
    store._pending[pending.approval_id] = replace(pending, expires_at=time.time() - 1)
    assert approve(store, pending) is None


def test_wrong_owner_cannot_consume_or_invalidate(isolated_host):
    store, pending = propose(authority(isolated_host, "bash"))
    assert store.consume(pending.approval_id, decision="approve_task", owner="bob",
                         session_id=pending.session_id) is None
    assert approve(store, pending) is not None


@pytest.mark.parametrize("restriction", ["inherited", "non_admin", "background", "no_workspace"])
def test_host_proposal_cannot_exceed_supported_scope(isolated_host, monkeypatch, restriction):
    auth = authority(isolated_host, "bash")
    content = "printf host-approved"
    if restriction == "inherited":
        auth = replace(auth, inherited=True)
    elif restriction == "non_admin":
        monkeypatch.setattr(tool_execution, "_owner_is_admin", lambda owner: False)
    elif restriction == "background":
        content = "#!bg\nprintf host-approved"
    else:
        auth = replace(auth, workspace="")
    with pytest.raises(ValueError):
        propose(auth, content=content)


async def test_current_disable_wins_over_approval(isolated_host):
    auth = authority(isolated_host, "bash")
    store, pending = propose(auth)
    exact = approve(store, pending)
    result = await dispatch(auth, exact=exact, disabled_tools={"bash"})
    assert result.get("blocked") and not exact._claimed


async def test_context_does_not_leak_between_concurrent_calls(isolated_host):
    auth = authority(isolated_host, "bash")
    store, pending = propose(auth)
    allowed, refused = await asyncio.gather(dispatch(auth, exact=approve(store, pending)), dispatch(auth))
    assert allowed["exit_code"] == 0, allowed
    assert refused["containment"]["executed"] is False


def test_fake_host_containment_receipt_is_rejected(isolated_host):
    spec = containment.agent_spec(str(isolated_host), dict(os.environ), 5)
    with pytest.raises(ValueError, match="exact human approval"):
        containment.acquire(spec, owner="host-test", host_approval=object())


def test_supported_sandbox_does_not_request_host_access(isolated_host, monkeypatch):
    monkeypatch.setattr(containment, "MECHANISMS", (containment.Mechanism(
        "bubblewrap", 30, lambda: True, lambda spec: containment.DEFAULT_REQUIRED),))
    assert host_execution_reason("bash", "pwd", workspace=str(isolated_host), owner="alice") is None


@pytest.mark.parametrize("marker", ["#!BG", "# bg", "#background", "@background", "# @background"])
def test_all_detached_markers_keep_their_original_boundary(isolated_host, marker):
    with pytest.raises(ValueError):
        propose(authority(isolated_host, "bash"), content=marker + "\nprintf detached")


async def test_consumed_but_expired_host_approval_cannot_launch(isolated_host):
    auth = authority(isolated_host, "bash")
    store, pending = propose(auth)
    exact = approve(store, pending)
    exact.pending = replace(pending, expires_at=time.time() - 1)
    result = await dispatch(auth, exact=exact)
    assert result["containment"]["executed"] is False


async def test_admin_revocation_after_proposal_prevents_execution(isolated_host, monkeypatch):
    auth = authority(isolated_host, "bash")
    store, pending = propose(auth)
    exact = approve(store, pending)
    monkeypatch.setattr(tool_execution, "_owner_is_admin", lambda owner: False)
    result = await dispatch(auth, exact=exact)
    assert result.get("blocked") and not exact._claimed


async def test_same_exact_grant_cannot_launch_twice_concurrently(isolated_host):
    auth = authority(isolated_host, "bash")
    store, pending = propose(auth)
    exact = approve(store, pending)
    results = await asyncio.gather(dispatch(auth, exact=exact), dispatch(auth, exact=exact))
    assert sum(r.get("exit_code") == 0 for r in results) == 1, results
    assert sum(bool(r.get("blocked")) for r in results) == 1, results


async def test_compact_runtime_proposes_host_approval_and_stops_the_batch(isolated_host, monkeypatch):
    import json
    from src import clean_agent_preview as compact
    from src.agent_runtime.authority import bind_request_authority
    from src.tool_policy import ToolPolicy
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
    from src.turn_contract import resolve_full_inventory_contract
    requests = []
    class Response:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def raise_for_status(self): pass
        async def aiter_lines(self):
            calls = [{'index': i, 'id': 'call-' + str(i), 'type': 'function',
                      'function': {'name': 'bash', 'arguments': json.dumps({'command': command})}}
                     for i, command in enumerate(('pwd', 'printf never-approved'))]
            yield 'data: ' + json.dumps({'choices': [{'delta': {'tool_calls': calls}}]})
            yield 'data: [DONE]'
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def stream(self, *args, **kwargs):
            requests.append(kwargs['json'])
            return Response()
    async def forbidden(*args, **kwargs):
        pytest.fail('Compact runtime executed a command before human host approval')
    monkeypatch.setattr(compact.httpx, 'AsyncClient', Client)
    monkeypatch.setattr(compact, 'execute_tool_block', forbidden)
    schemas = [s for s in FUNCTION_TOOL_SCHEMAS if s['function']['name'] == 'bash']
    contract = resolve_full_inventory_contract(schemas=schemas, policy=ToolPolicy())
    auth = authority(isolated_host, 'bash')
    with bind_request_authority(auth):
        raw = [chunk async for chunk in compact.stream_preview(
            endpoint_url='http://test', model='qwen-test',
            messages=[{'role': 'user', 'content': 'Run bash with exactly pwd.'}], headers={},
            turn_contract=contract, session_id=auth.session_id, owner=auth.owner,
            disabled_tools=set(), tool_policy=ToolPolicy(), workspace=str(isolated_host),
            request_authority=auth,
            client_runtime_context={'surface': 'odysseus-native', 'terminal_agent': True}, max_rounds=3)]
    events = [json.loads(chunk[6:]) for chunk in raw if chunk.startswith('data: ') and '[DONE]' not in chunk]
    cards = [e['ask_user'] for e in events if e.get('ask_user')]
    assert len(cards) == 1, events
    assert cards[0]['action']['content'] == 'pwd'
    assert cards[0]['action']['execution_profile'] == HOST_ONCE
    assert len(requests) == 1
    assert not any(e.get('type') == 'tool_start' for e in events)
    metrics = next(e['data'] for e in events if e.get('type') == 'metrics')
    assert metrics['tool_events'][0]['ask_user']['approval_id'] == cards[0]['approval_id']
