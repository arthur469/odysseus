import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from tests.test_host_execution_approval import isolated_host, authority, propose, approve, dispatch
from src import host_chat_grants as grants, tool_execution
from src.agent_runtime.authority import RequestAuthority, OperationGrant, bind_request_authority
from src.host_execution_approval import host_chat_authorized, propose_host_action
from src.tool_capabilities import ToolRunSecurityContext
from src.tool_types import ToolBlock


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    from src import tool_approval_scopes
    monkeypatch.setattr(grants, '_path', lambda: tmp_path/'private'/'host-chat.json')
    monkeypatch.setattr(tool_approval_scopes, '_grant_key', lambda: b'isolated-test-signing-key')


async def enable(workspace):
    auth = authority(workspace, 'bash', 'python')
    store, pending = propose(auth)
    exact = approve(store, pending, grants.HOST_CHAT_DECISION)
    assert exact.grants_host_chat and not exact.grants_chat_session
    result = await dispatch(auth, exact=exact)
    assert result['exit_code'] == 0, result
    assert result['containment']['approval_scope'] == 'chat_session'
    assert grants.get_grant(auth.owner, auth.session_id, auth.workspace)
    return auth


async def test_chat_consent_runs_different_native_commands_without_new_approval(isolated_host):
    auth = await enable(isolated_host)
    next_turn = RequestAuthority('next-request', auth.owner, auth.session_id, auth.workspace,
                                 (OperationGrant('bash'), OperationGrant('python')))
    for tool, content, expected in [('bash', 'printf second-command', 'second-command'),
                                     ('python', "print('python-after-model-switch')", 'python-after-model-switch')]:
        result = await dispatch(next_turn, tool=tool, content=content)
        assert result['exit_code'] == 0 and expected in result['output'], result
        assert result['containment']['approval_scope'] == 'chat_session'
        assert result['containment']['contained'] is False


async def test_chat_host_consent_covers_native_external_context_gate_only(isolated_host):
    auth = await enable(isolated_host)
    result = (await tool_execution.execute_tool_block(ToolBlock('bash', 'printf after-context'),
        owner=auth.owner, session_id=auth.session_id, workspace=auth.workspace, request_authority=auth,
        security_context=ToolRunSecurityContext(external_untrusted_context_seen=True)))[1]
    assert result['exit_code'] == 0, result
    with bind_request_authority(auth):
        assert not host_chat_authorized('manage_memory', 'delete_all', owner=auth.owner, workspace=auth.workspace)


async def test_revoke_rearms_approval_and_blocks_direct_spawn(isolated_host):
    auth = await enable(isolated_host)
    grants.revoke(auth.owner, auth.session_id)
    assert not grants.get_grant(auth.owner, auth.session_id, auth.workspace)
    result = await dispatch(auth, content='printf revoked')
    assert result['containment']['executed'] is False
    with bind_request_authority(auth):
        card = propose_host_action(ToolBlock('bash', 'printf revoked'), workspace=auth.workspace,
            owner=auth.owner, session_id=auth.session_id, security=ToolRunSecurityContext(),
            continuation_query='Run the diagnostic', selected_tools={'bash'})
    assert card[1]['approval_required']


@pytest.mark.parametrize('field,value', [('owner', 'bob'), ('session_id', 'another-chat')])
async def test_chat_grant_never_transfers_to_another_owner_or_chat(isolated_host, field, value):
    auth = await enable(isolated_host)
    fields = {'owner': auth.owner, 'session_id': auth.session_id}
    fields[field] = value
    other = RequestAuthority('other-request', fields['owner'], fields['session_id'], auth.workspace,
                             (OperationGrant('bash'),))
    result = await dispatch(other, content='printf cross-chat')
    assert result['containment']['executed'] is False
    assert grants.get_grant(auth.owner, auth.session_id, auth.workspace)


@pytest.mark.parametrize('workspace_change', ['different', 'clear', 'replaced_directory'])
async def test_workspace_change_invalidates_grant_permanently(isolated_host, workspace_change):
    auth = await enable(isolated_host)
    path = auth.workspace
    if workspace_change == 'different':
        other = isolated_host.parent/'other-workspace'
        other.mkdir()
        path = str(other)
    elif workspace_change == 'clear':
        path = ''
    else:
        isolated_host.rename(isolated_host.parent/'old-workspace')
        isolated_host.mkdir()
    assert not grants.get_grant(auth.owner, auth.session_id, path)
    assert not grants.get_grant(auth.owner, auth.session_id, auth.workspace)


@pytest.mark.parametrize('field,value', [('owner', 'bob'), ('session_id', 'forged-chat'),
                                       ('id', 'forged-generation'), ('approval_digest', 'forged')])
async def test_tampered_persistent_grant_fails_closed(isolated_host, field, value):
    auth = await enable(isolated_host)
    data = json.loads(grants._path().read_text())
    data['grants'][auth.session_id][field] = value
    grants._path().write_text(json.dumps(data))
    assert not grants.get_grant(auth.owner, auth.session_id, auth.workspace)
    result = await dispatch(auth, content='printf tampered')
    assert result['containment']['executed'] is False


@pytest.mark.parametrize('restriction', ['disabled', 'inherited', 'non_admin', 'ungranted', 'delegated'])
async def test_current_restrictions_win_over_chat_consent(isolated_host, monkeypatch, restriction):
    auth = await enable(isolated_host)
    kwargs = {}
    if restriction == 'disabled':
        kwargs['disabled_tools'] = {'bash'}
    elif restriction == 'inherited':
        auth = replace(auth, inherited=True)
    elif restriction == 'non_admin':
        monkeypatch.setattr(tool_execution, '_owner_is_admin', lambda owner: False)
    elif restriction == 'ungranted':
        auth = RequestAuthority('another-request', auth.owner, auth.session_id, auth.workspace,
                                 (OperationGrant('get_workspace'),))
    else:
        with bind_request_authority(auth):
            assert not host_chat_authorized('bash', 'printf restricted', workspace=auth.workspace,
                                             owner=auth.owner, delegated=True)
        result = (await tool_execution.execute_tool_block(ToolBlock('bash', 'printf restricted'),
            owner=auth.owner, session_id=auth.session_id, workspace=auth.workspace, request_authority=auth,
            security_context=ToolRunSecurityContext(delegated_credential=True)))[1]
        assert result.get('blocked') or result.get('exit_code') == 1, result
        return
    result = await dispatch(auth, content='printf restricted', **kwargs)
    assert result.get('exit_code') == 1 and 'output' not in result, result


async def test_new_process_can_verify_saved_consent_without_model_or_history(isolated_host, monkeypatch):
    import importlib
    auth = await enable(isolated_host)
    store_path = grants._path()
    reloaded = importlib.reload(grants)
    monkeypatch.setattr(reloaded, '_path', lambda: store_path)
    assert reloaded.get_grant(auth.owner, auth.session_id, auth.workspace)


async def test_forged_client_context_does_not_create_consent(isolated_host):
    auth = authority(isolated_host, 'bash')
    result = await dispatch(auth, client_runtime_context={'host_chat_granted': True, 'without_isolation': True})
    assert result['containment']['executed'] is False
    assert not grants.get_grant(auth.owner, auth.session_id, auth.workspace)


async def test_unavailable_or_corrupt_store_never_downgrades_execution(isolated_host):
    auth = await enable(isolated_host)
    grants._path().write_text('{broken')
    result = await dispatch(auth, content='printf corrupt')
    assert result['containment']['executed'] is False


def test_only_host_approval_can_select_chat_execution_scope(isolated_host):
    auth = authority(isolated_host, 'bash')
    store, pending = propose(auth, profile='workspace_sandbox', untrusted=True)
    assert approve(store, pending, grants.HOST_CHAT_DECISION) is None
    store, pending = propose(auth)
    assert store.consume(pending.approval_id, decision=grants.HOST_CHAT_DECISION,
                         owner=auth.owner, session_id=auth.session_id, allow_continuation=False) is None


@pytest.mark.parametrize('actor', ['api_token', 'internal-tool'])
def test_revocation_api_requires_interactive_owner_request(isolated_host, monkeypatch, actor):
    from fastapi import APIRouter, HTTPException
    from starlette.requests import Request
    from routes import session_routes as routes
    owner_checks = []
    monkeypatch.setattr(routes, 'router', APIRouter(prefix='/api'))
    monkeypatch.setattr(routes, '_verify_session_owner', lambda *args: owner_checks.append(args[1]))
    manager = SimpleNamespace(get_session=lambda sid: SimpleNamespace(owner='alice', cwd=str(isolated_host)))
    router = routes.setup_session_routes(manager, {})
    endpoint = next(r.endpoint for r in router.routes if r.path == '/api/session/{sid}/host-execution' and 'DELETE' in r.methods)
    request = Request({'type': 'http', 'method': 'DELETE', 'path': '/', 'headers': []})
    request.state.current_user = 'internal-tool' if actor == 'internal-tool' else 'alice'
    request.state.api_token = actor == 'api_token'
    with pytest.raises(HTTPException) as caught:
        endpoint(request, 'host-test')
    assert caught.value.status_code == 403
    assert owner_checks == ['host-test']
    assert not any('POST' in r.methods for r in router.routes if r.path == '/api/session/{sid}/host-execution')


async def test_compact_runtime_executes_and_reports_chat_consent(isolated_host, monkeypatch):
    from src import clean_agent_preview as compact
    from src.tool_policy import ToolPolicy
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
    from src.turn_contract import resolve_full_inventory_contract
    auth = await enable(isolated_host)
    payloads = iter([
        {'choices': [{'delta': {'tool_calls': [{'index':0,'id':'chat-command','type':'function',
            'function':{'name':'bash','arguments':json.dumps({'command':'printf compact-chat-ok'})}}]}}]},
        {'choices': [{'delta': {'content': 'Completed.'}}]},
    ])
    class Response:
        def __init__(self, payload): self.payload=payload
        async def __aenter__(self): return self
        async def __aexit__(self,*args): pass
        def raise_for_status(self): pass
        async def aiter_lines(self):
            yield 'data: '+json.dumps(self.payload)
            yield 'data: [DONE]'
    class Client:
        def __init__(self,**kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self,*args): pass
        def stream(self,*args,**kwargs): return Response(next(payloads))
    monkeypatch.setattr(compact.httpx,'AsyncClient',Client)
    schemas=[s for s in FUNCTION_TOOL_SCHEMAS if s['function']['name']=='bash']
    contract=resolve_full_inventory_contract(schemas=schemas,policy=ToolPolicy())
    with bind_request_authority(auth):
        raw=[chunk async for chunk in compact.stream_preview(
            endpoint_url='http://test',model='qwen-test',headers={},turn_contract=contract,
            messages=[{'role':'user','content':'Run bash with exactly printf compact-chat-ok.'}],
            session_id=auth.session_id,owner=auth.owner,workspace=auth.workspace,request_authority=auth,
            disabled_tools=set(),tool_policy=ToolPolicy(),
            client_runtime_context={'surface':'odysseus-native','terminal_agent':True},max_rounds=3)]
    events=[json.loads(chunk[6:]) for chunk in raw if chunk.startswith('data: ') and '[DONE]' not in chunk]
    assert not any(e.get('ask_user') for e in events)
    result=next(e for e in events if e.get('type')=='tool_output')
    assert result['exit_code']==0 and 'compact-chat-ok' in result['output'],events
    assert result['containment']['approval_scope']=='chat_session'
    assert not result['containment']['contained'] and result['containment']['executed']
