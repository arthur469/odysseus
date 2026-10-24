import pytest

from src.clean_agent_preview import (
    PREVIEW_TOOLS, compact_schemas, evaluate_preview_call, scope_preview_contract,
)
from src.tool_policy import ToolPolicy
from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
from src.turn_contract import FAMILY_TOOLS, resolve_full_inventory_contract


def webui_contract(policy=ToolPolicy()):
    # Match the WebUI compact route's preview filter and routed intersection.
    schemas = [s for s in FUNCTION_TOOL_SCHEMAS if s['function']['name'] in FAMILY_TOOLS['shell_files']]
    routed = resolve_full_inventory_contract(schemas=schemas, policy=policy)
    preview = resolve_full_inventory_contract(
        schemas=[s for s in schemas if s['function']['name'] in PREVIEW_TOOLS], policy=policy,
    )
    return scope_preview_contract(preview, routed, {'shell_files'})


@pytest.mark.parametrize('tool,args', [('get_workspace', {}), ('ls', {'path': '.'})])
def test_compact_webui_offers_callable_discovery_tools(tool, args):
    contract = webui_contract()
    wire = compact_schemas(contract.schemas(), model='qwen3-gpu:latest')
    assert tool in {s['function']['name'] for s in wire}
    assert evaluate_preview_call(tool, args, 'Show the active workspace and list its entries').allowed


@pytest.mark.parametrize('tool', ['get_workspace', 'ls'])
def test_explicit_tool_denial_still_removes_discovery(tool):
    contract = webui_contract(ToolPolicy(disabled_tools=frozenset({tool})))
    assert tool not in contract.offered
    assert tool not in {s['function']['name'] for s in compact_schemas(contract.schemas())}


def test_block_all_still_removes_all_workspace_tools():
    assert not webui_contract(ToolPolicy(block_all_tool_calls=True)).offered


@pytest.mark.parametrize('tool,args', [
    ('write_file', {'path': 'new.txt', 'content': 'unexpected'}),
    ('edit_file', {'path': 'file.txt', 'old_string': 'a', 'new_string': 'b'}),
])
def test_discovery_does_not_enable_webui_workspace_writes(tool, args):
    assert tool not in webui_contract().offered
    assert not evaluate_preview_call(tool, args, 'List the workspace').allowed
