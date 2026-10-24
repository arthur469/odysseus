import asyncio
import json

import pytest

from src import agent_loop, containment, llm_core, tool_execution
from src.agent_runtime.authority import ExactOperation, OperationGrant, RequestAuthority, create_request_authority
from src.tool_capabilities import ToolRunSecurityContext
from src.tool_parsing import parse_tool_blocks
from src.tool_policy import ToolPolicy
from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
from src.turn_contract import CONTRACT_CORE_TOOLS, requested_capabilities, resolve_full_inventory_contract, resolve_turn_contract, selected_tools_for_request


@pytest.mark.parametrize('model', ['gpt-5.6-luna', 'gpt-6.1-sol', 'gpt-6-astra'])
def test_subscription_text_protocol_advertises_executable_workspace_reads(model):
    prompt = agent_loop._assemble_prompt(set(CONTRACT_CORE_TOOLS))
    payload = llm_core._build_chatgpt_responses_payload(
        model, [{'role': 'system', 'content': prompt}, {'role': 'user', 'content': 'List this workspace'}],
        0, 512, stream=True,
    )
    assert 'tools' not in payload
    for tool in ('get_workspace', 'ls'):
        assert f'```{tool}\n' in payload['instructions']
    assert 'instead of running shell commands' in payload['instructions']
    blocks = parse_tool_blocks('```get_workspace\n```\n\n```ls\n{"path":"."}\n```')
    assert [b.tool_type for b in blocks] == ['get_workspace', 'ls']


@pytest.mark.parametrize('tool', ['get_workspace', 'ls'])
def test_shared_native_surface_respects_explicit_denial(tool):
    assert tool in CONTRACT_CORE_TOOLS
    allowed = resolve_full_inventory_contract(schemas=FUNCTION_TOOL_SCHEMAS, policy=ToolPolicy())
    denied = resolve_full_inventory_contract(
        schemas=FUNCTION_TOOL_SCHEMAS, policy=ToolPolicy(disabled_tools=frozenset({tool})),
    )
    assert tool in allowed.offered
    assert tool not in denied.offered
    assert f'```{tool}\n' not in agent_loop._assemble_prompt(set(CONTRACT_CORE_TOOLS), {tool})


@pytest.mark.asyncio
async def test_textual_workspace_reads_need_no_shell_or_containment(tmp_path, monkeypatch):
    monkeypatch.setattr(tool_execution, '_owner_is_admin', lambda owner: True)
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', lambda *a, **kw: pytest.fail('Workspace read spawned a process'))
    monkeypatch.setattr(containment, 'acquire', lambda *a, **kw: pytest.fail('Workspace read requested shell containment'))
    (tmp_path / 'shortcut.lnk').write_text('fixture')
    authority = RequestAuthority('request', 'test-owner', 'test', str(tmp_path),
                                 (OperationGrant('get_workspace'), OperationGrant('ls')))
    blocks = parse_tool_blocks('```get_workspace\n```\n\n```ls\n{"path":"."}\n```')
    assert [block.tool_type for block in blocks] == ['get_workspace', 'ls']
    outputs = []
    for block in blocks:
        _, result = await tool_execution.execute_tool_block(
            block, owner='test-owner', session_id='test', workspace=str(tmp_path),
            security_context=ToolRunSecurityContext(external_untrusted_context_seen=True),
            request_authority=authority,
        )
        assert result.get('exit_code', 0) == 0 and not result.get('error'), result
        outputs.append(result)
    assert 'shortcut.lnk' in json.dumps(outputs)


@pytest.mark.asyncio
async def test_textual_listing_cannot_escape_selected_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(tool_execution, '_owner_is_admin', lambda owner: True)
    workspace = tmp_path / 'selected'
    workspace.mkdir()
    outside = tmp_path / 'private'
    outside.mkdir()
    (outside / 'do-not-expose.txt').write_text('private')
    authority = RequestAuthority('request', 'test-owner', 'test', str(workspace), (OperationGrant('ls'),))
    block = parse_tool_blocks('```ls\n' + json.dumps({'path': str(outside)}) + '\n```')[0]
    _, result = await tool_execution.execute_tool_block(
        block, owner='test-owner', session_id='test', workspace=str(workspace),
        security_context=ToolRunSecurityContext(), request_authority=authority,
    )
    assert result.get('error') or result.get('blocked') or result.get('exit_code') == 1
    assert 'do-not-expose.txt' not in json.dumps(result)


def test_empty_read_fences_do_not_enable_empty_commands_or_native_examples():
    text = '```get_workspace\n```\n```ls\n```\n```bash\n```\n```python\n```\n```write_file\n```'
    assert [block.tool_type for block in parse_tool_blocks(text)] == ['get_workspace', 'ls']
    assert not parse_tool_blocks(text, skip_fenced=True)


@pytest.mark.parametrize('message', [
    "J'aimerai réduire le nombre d'icones et de raccourcis sur mon bureau.\n\nPeux tu scanner et proposer une architecture ?",
    'Peux-tu inspecter mon bureau et proposer une organisation ?',
    'Liste le contenu de mon bureau',
    'List the files in my Desktop folder and propose an organization',
    'Inspect this workspace and propose a structure',
])
def test_workspace_inventory_authorizes_only_discovery(message, tmp_path):
    assert selected_tools_for_request(message) == {'get_workspace', 'ls'}
    assert requested_capabilities(message, workspace=True) == {'shell_files'}
    authority = create_request_authority(message, owner='test-owner', session_id='test', workspace=str(tmp_path))
    for tool, args in [('get_workspace', ''), ('ls', '{"path":"."}')]:
        assert authority.permits(ExactOperation.normalize(tool, args))
    for tool in ('bash', 'python', 'write_file', 'edit_file', 'manage_memory', 'web_fetch'):
        assert not authority.permits(ExactOperation.normalize(tool, '{}'))


@pytest.mark.parametrize('message', [
    'How do I scan my desktop?',
    'Comment scanner mon bureau ?',
    'Scan this website directory https://example.com',
    'Scan my Desktop and delete its files',
    'Inspect workspace and run bash',
    'Scan /workspace/fixtures/paper.pdf, verify its citations and save /workspace/report.csv',
    'Use get_workspace to inspect the current workspace.',
    'Now use ls to list that same workspace directory.',
    'Create a note titled Desktop inventory with text: scan my desktop',
])
def test_inventory_detection_does_not_replace_other_operations(message):
    assert selected_tools_for_request(message) != {'get_workspace', 'ls'}


def test_compact_inventory_executes_required_reads_before_clarification():
    from src.clean_agent_preview import required_read_tool_choice
    contract = resolve_turn_contract(capabilities={'shell_files'}, policy=ToolPolicy(),
        schemas=FUNCTION_TOOL_SCHEMAS, selected_tools={'get_workspace', 'ls'}, required_tools={'get_workspace', 'ls'})
    offered = contract.schemas()
    assert required_read_tool_choice(contract, offered) == {
        'type': 'function', 'function': {'name': 'get_workspace'}}
    assert required_read_tool_choice(contract, offered, calls=1, attempted_required_tools={'get_workspace'}) == {
        'type': 'function', 'function': {'name': 'ls'}}
    assert required_read_tool_choice(contract, offered, calls=2, attempted_required_tools={'get_workspace', 'ls'}) is None


@pytest.mark.asyncio
async def test_local_inventory_wire_has_only_pending_read(monkeypatch, tmp_path):
    import copy
    from src import clean_agent_preview as module
    requests = []
    responses = iter([
        {'choices': [{'delta': {'tool_calls': [{'index': 0, 'id': 'workspace', 'type': 'function',
            'function': {'name': 'get_workspace', 'arguments': '{}'}}]}}]},
        {'choices': [{'delta': {'tool_calls': [{'index': 0, 'id': 'listing', 'type': 'function',
            'function': {'name': 'ls', 'arguments': '{"path":"."}'}}]}}]},
        {'choices': [{'delta': {'content': 'The observed shortcut can be grouped under Applications.'}}]},
    ])
    class Response:
        def __init__(self, payload): self.payload = payload
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def raise_for_status(self): pass
        async def aiter_lines(self):
            yield 'data: ' + json.dumps(self.payload)
            yield 'data: [DONE]'
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def stream(self, *args, **kwargs):
            requests.append(copy.deepcopy(kwargs['json']))
            return Response(next(responses))
    executed = []
    async def execute(block, **kwargs):
        executed.append(block.tool_type)
        return block.tool_type, {'exit_code': 0, 'output': '/workspace' if block.tool_type == 'get_workspace' else 'Browser.lnk'}
    monkeypatch.setattr(module.httpx, 'AsyncClient', Client)
    monkeypatch.setattr(module, 'execute_tool_block', execute)
    contract = resolve_turn_contract(capabilities={'shell_files'}, policy=ToolPolicy(),
        schemas=FUNCTION_TOOL_SCHEMAS, selected_tools={'get_workspace', 'ls'}, required_tools={'get_workspace', 'ls'})
    message = 'Inspect this workspace and propose a structure'
    authority = create_request_authority(message, owner='test-owner', session_id='test', workspace=str(tmp_path))
    _events = [event async for event in module.stream_preview(
        endpoint_url='http://test', model='qwen3-gpu:latest', messages=[{'role': 'user', 'content': message}],
        headers={}, turn_contract=contract, session_id='test', owner='test-owner',
        disabled_tools=set(), tool_policy=ToolPolicy(), workspace=str(tmp_path), request_authority=authority,
    )]
    assert [s['function']['name'] for s in requests[0]['tools']] == ['get_workspace']
    assert [s['function']['name'] for s in requests[1]['tools']] == ['ls']
    assert 'Call get_workspace now' in requests[0]['messages'][-1]['content']
    assert 'Call ls now' in requests[1]['messages'][-1]['content']
    assert executed == ['get_workspace', 'ls']
