"""Revocable, owner/thread/workspace-bound host consent using existing signing/IO."""
from __future__ import annotations
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from pathlib import Path

from core.atomic_io import atomic_write_json, store_transaction
from src.constants import DATA_DIR
from src.agent_runtime.resources import FilesystemRoot

logger = logging.getLogger(__name__)
HOST_CHAT_DECISION = 'approve_host_chat'


def _path():
    return Path(DATA_DIR) / 'host_chat_grants.json'


def _read():
    try:
        value = json.loads(_path().read_text(encoding='utf8'))
    except FileNotFoundError:
        return {'version': 1, 'grants': {}}
    if not isinstance(value, dict) or value.get('version') != 1 or not isinstance(value.get('grants'), dict):
        raise ValueError('Invalid host chat grant store')
    return value


def _signature(payload):
    from src.tool_approval_scopes import _grant_key
    key = _grant_key()
    if key is None:
        raise ValueError('Host chat grant signing is unavailable')
    body = json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    return hmac.new(key, ('odysseus.host-chat.v1\0' + body).encode('utf8'), hashlib.sha256).hexdigest()


@store_transaction(_path)
def revoke(owner, session_id, *, expected_id=None):
    data = _read()
    record = data['grants'].get(str(session_id))
    if (record is None or record.get('owner') != str(owner or '').strip().casefold()
            or (expected_id is not None and record.get('id') != expected_id)):
        return
    del data['grants'][str(session_id)]
    atomic_write_json(str(_path()), data)
    logger.info('[host-chat] revoked owner=%s session=%s', owner, session_id)


def get_grant(owner, session_id, workspace=None):
    if not session_id:
        return None
    try:
        record = _read()['grants'].get(str(session_id))
        if not isinstance(record, dict):
            return None
        payload = {k: v for k, v in record.items() if k != 'signature'}
        signature = record.get('signature')
        if (record.get('version') != 1
                or record.get('owner') != str(owner or '').strip().casefold() or record.get('session_id') != str(session_id)
                or not isinstance(signature, str) or not signature.isascii()
                or not hmac.compare_digest(signature, _signature(payload))):
            return None
        root = FilesystemRoot.from_dict(record['root'])
        if root.owner != record['owner'] or (workspace is not None and (not workspace or root.path != os.path.realpath(workspace))):
            revoke(owner, session_id, expected_id=record['id'])
            return None
        try:
            root.validate()
        except (ValueError, OSError, RuntimeError):
            revoke(owner, session_id, expected_id=record['id'])
            return None
        return record
    except (ValueError, OSError, TypeError, KeyError):
        return None


@store_transaction(_path)
def enable_from_exact_approval(approval, spec, *, owner):
    from src.host_execution_approval import approved_host_action
    from src.tool_approvals import ExactToolApproval
    if (not isinstance(approval, ExactToolApproval) or not approval.grants_host_chat
            or approved_host_action(spec, owner=owner) is not approval):
        raise ValueError('Host chat consent requires a claimed exact human approval')
    pending = approval.pending
    root = pending.process_operation.launch.scope.root
    root.validate()
    payload = {'version': 1, 'id': secrets.token_hex(16), 'owner': pending.owner,
               'session_id': pending.session_id, 'root': root.to_dict(), 'created_at': time.time(),
               'approval_digest': pending.digest}
    record = {**payload, 'signature': _signature(payload)}
    data = _read()
    data['grants'][pending.session_id] = record
    atomic_write_json(str(_path()), data)
    logger.warning('[host-chat] enabled owner=%s session=%s workspace=%s approval=%s',
                   pending.owner, pending.session_id, pending.workspace, pending.digest[:16])
    return record
