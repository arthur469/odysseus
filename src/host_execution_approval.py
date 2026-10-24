"""Exact and revocable chat-scoped native host execution (#6092 / #5819).

Reuse the installed exact-approval and process-identity pipeline. No persistent
Full Access mode or global permission change. Chat consent remains bound to
its authenticated owner and observed workspace, independently of the model.
"""
from __future__ import annotations

import contextvars
import time
import hashlib
from contextlib import contextmanager
from dataclasses import dataclass

WORKSPACE_SANDBOX = "workspace_sandbox"
HOST_ONCE = "host_once"
HOST_CHAT = "host_chat"
_active = contextvars.ContextVar("exact_host_execution_approval", default=None)


def host_chat_authorized(tool, content, *, workspace, owner, session_id=None,
                         authority=None, delegated=False, client_runtime_context=None):
    if delegated or tool not in {'bash', 'python'} or not workspace:
        return None
    from src.agent_runtime.authority import active_request_authority, ExactOperation
    from src.agent_runtime.remote_resources import resolve_backend
    from src.agent_runtime.resources import NativeBackendResource
    from src.tool_execution import _owner_is_admin, _split_bg_marker
    from src.host_chat_grants import get_grant
    authority = authority or active_request_authority()
    if (authority is None or authority.inherited or not _owner_is_admin(owner)
            or authority.owner != str(owner or '').strip().casefold()
            or (session_id is not None and authority.session_id != str(session_id))
            or authority.workspace != workspace):
        return None
    if tool == 'bash' and _split_bg_marker(str(content or ''))[0]:
        return None
    try:
        if (not authority.permits(ExactOperation.normalize(tool, content))
                or not isinstance(resolve_backend(tool, context=client_runtime_context,
                                                content=content, owner=owner), NativeBackendResource)):
            return None
        return get_grant(authority.owner, authority.session_id, workspace)
    except (ValueError, TypeError, OSError):
        return None


@dataclass(frozen=True)
class _ChatHostPermit:
    owner: str
    session_id: str
    workspace: str
    tool_name: str
    content: str
    request_id: str
    process_operation: object
    grant_id: str
    digest: str
    execution_profile: str = HOST_CHAT

    @property
    def pending(self):
        return self


def approval_scope(approval):
    return 'chat_session' if isinstance(approval, _ChatHostPermit) or getattr(approval, 'grants_host_chat', False) else 'single_action'


def host_execution_reason(tool, content, *, workspace, owner, client_runtime_context=None):
    """Read-only capability probe; remote backends keep their own execution policy."""
    if tool not in {"bash", "python"} or not workspace:
        return None
    from src.tool_execution import _owner_is_admin, vet_workspace, _split_bg_marker
    from src.agent_runtime.authority import active_request_authority
    from src.agent_runtime.remote_resources import resolve_backend
    from src.agent_runtime.resources import NativeBackendResource
    from src.agent_tools.subprocess_tools import _owned_spec
    from src import containment
    authority = active_request_authority()
    if (not _owner_is_admin(owner) or (authority is not None and authority.inherited)
            or vet_workspace(workspace) != workspace):
        return None
    # Detached execution needs a separate lifecycle integration. Never turn a
    # foreground one-use approval into permission for a detached job.
    if tool == "bash" and _split_bg_marker(str(content or ""))[0]:
        return None
    try:
        backend = resolve_backend(tool, context=client_runtime_context, content=content, owner=owner)
        if not isinstance(backend, NativeBackendResource):
            return None
        probe = containment.probe(_owned_spec(workspace, None, 120))
    except (ValueError, TypeError, OSError):
        return None
    missing = frozenset(probe.unenforced_required)
    if probe.refuses and missing and missing <= {containment.FILESYSTEM, containment.PROCESS_TREE}:
        return (
            "Isolation is unavailable for: " + ", ".join(sorted(missing)) + ". "
            "Commands run with the account running Odysseus. "
            "It can read or modify any files accessible to that account, including files outside "
            "the selected workspace, and may start child processes that cannot be reliably "
            "contained or stopped. Network access follows the host account. "
            "Choose permission for the displayed command or for commands in this chat. "
            "Chat permission remains until revoked and requires new approval after changing workspace."
        )
    return None


@contextmanager
def bind_host_execution_approval(approval, *, delegated=False, client_runtime_context=None):
    if approval is None and not delegated:
        from src.agent_runtime.authority import active_request_authority
        from src.agent_runtime.process_resources import active_process_operation
        bound, authority = active_process_operation(), active_request_authority()
        if bound is not None and bound.launch is not None and authority is not None:
            record = host_chat_authorized(bound.operation.tool, bound.operation.input,
                workspace=authority.workspace, owner=authority.owner, authority=authority,
                client_runtime_context=client_runtime_context)
            if record is not None:
                fingerprint = hashlib.sha256((record['id'] + authority.request_id + bound.operation.input).encode()).hexdigest()
                approval = _ChatHostPermit(authority.owner, authority.session_id, authority.workspace,
                    bound.operation.tool, bound.operation.input, authority.request_id, bound, record['id'], fingerprint)
    token = _active.set(approval if approval is not None
                        and approval.pending.execution_profile in {HOST_ONCE, HOST_CHAT} else None)
    try:
        yield
    finally:
        _active.reset(token)


def propose_host_action(block, *, workspace, owner, session_id, security,
                        continuation_query, selected_tools, client_runtime_context=None):
    """Shared approval protocol for native and textual model runtimes."""
    if security.delegated_credential:
        return None
    if host_chat_authorized(block.tool_type, block.content, workspace=workspace,
                            owner=owner, session_id=session_id, client_runtime_context=client_runtime_context):
        return None
    reason = host_execution_reason(block.tool_type, block.content, workspace=workspace,
                                   owner=owner, client_runtime_context=client_runtime_context)
    if not reason:
        return None
    from src.agent_runtime.authority import active_request_authority
    from src.tool_approvals import tool_approval_store
    from src.tool_capabilities import capabilities_for_action
    pending = tool_approval_store.create(
        owner=owner, session_id=session_id, origin_run_id=security.run_id,
        tool_name=block.tool_type, content=block.content, workspace=workspace,
        external_untrusted_context_seen=security.external_untrusted_context_seen,
        capabilities=capabilities_for_action(block.tool_type, block.content),
        selected_tools=selected_tools, continuation_query=continuation_query,
        request_text=continuation_query, request_authority=active_request_authority(),
        client_runtime_context=client_runtime_context, execution_profile=HOST_ONCE,
    )
    return f"{block.tool_type}: APPROVAL REQUIRED", {
        "output": "Waiting for an exact user approval.", "exit_code": None,
        "approval_required": True, "ask_user": pending.public_payload(reason=reason),
    }


def approved_host_action(spec, *, owner):
    """Return only the exact grant already claimed by the central dispatcher."""
    approval = _active.get()
    if approval is None:
        return None
    from src.agent_runtime.authority import active_request_authority
    from src.agent_runtime.process_resources import active_process_operation
    from src.tool_execution import _owner_is_admin
    authority = active_request_authority()
    bound = active_process_operation()
    pending = approval.pending
    if isinstance(approval, _ChatHostPermit):
        record = host_chat_authorized(approval.tool_name, approval.content, workspace=spec.workspace,
                                      owner=approval.owner, session_id=approval.session_id, authority=authority)
        if (record is None or record['id'] != approval.grant_id or authority.request_id != approval.request_id
                or str(owner) != str(approval.session_id or approval.owner)
                or bound is None or bound.to_dict() != approval.process_operation.to_dict()):
            return None
        return approval
    if (not approval._claimed or pending.execution_profile != HOST_ONCE or pending.expires_at <= time.time()
            or authority is None or authority.inherited or not _owner_is_admin(authority.owner)
            or (authority.owner, authority.session_id) != (pending.owner, pending.session_id)
            or str(owner) != str(pending.session_id or pending.owner or pending.tool_name)
            or pending.workspace != spec.workspace
            or bound is None or bound.exact_approval is not approval
            or pending.process_operation is None
            or bound.to_dict() != pending.process_operation.to_dict()):
        return None
    return approval


def activate_chat_approval(approval, spec, *, owner):
    if getattr(approval, 'grants_host_chat', False):
        from src.host_chat_grants import enable_from_exact_approval
        enable_from_exact_approval(approval, spec, owner=owner)


def validate_host_grant(grant):
    from src import containment
    approval = approved_host_action(grant.spec, owner=grant.owner)
    record = containment._load_records().get(grant.id, {})
    if (approval is None or record.get("host_approval_digest") != approval.pending.digest
            or record.get("host_launch_claimed")):
        raise ValueError("Host execution requires an unused exact human approval")
    # The normal exact approval claim prevents another dispatcher invocation;
    # this receipt also prevents reuse within a producer invocation.
    containment._update_record(grant.id, host_launch_claimed=True)
