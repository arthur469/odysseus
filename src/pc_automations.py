"""Actions natives limitées à la bibliothèque personnelle Automatisations-PC.

Pas de modèle, de commande fournie par le chat, ni d'exécution pendant la synchro.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import re

ROOT = Path.home() / 'Documents' / 'Automatisations-PC'
RUN = 'pc_executer_routine'
SYNC = 'pc_synchroniser_routines'


def _admin(owner):
    from src.task_action_policy import owner_has_admin_task_privileges
    if not owner or not owner_has_admin_task_privileges(owner):
        raise PermissionError('PC automations require an Odysseus administrator account.')


def _routine_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', value):
        raise ValueError('Invalid routine identifier.')
    return value


async def _cli(*arguments):
    # Exécutable, script et répertoire fixes ; aucune commande arbitraire.
    process = await asyncio.create_subprocess_exec(
        str(ROOT / '.venv/Scripts/python.exe'), str(ROOT / 'cli.py'), *arguments,
        cwd=str(ROOT), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        creationflags=0x08000000,
        env=__import__('os').environ | {'PYTHONUTF8':'1', 'PYTHONIOENCODING':'utf-8'},
    )
    try:
        # Une routine a au plus 32 étapes, chacune contrôlée puis appliquée dans
        # un worker borné à 25 s. Une cible lente ne doit pas couper les suivantes.
        timeout = 1800 if arguments and arguments[0] == 'executer' else 180
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    if len(stdout) > 4_000_000:
        raise ValueError('Library output is too large.')
    try:
        result = json.loads(stdout.decode('utf-8'))
    except (ValueError, UnicodeError):
        raise RuntimeError('The library did not return a valid JSON result: '
                           + stderr.decode('utf-8', errors='replace')[-1000:])
    if not isinstance(result, dict):
        raise ValueError('Invalid library result.')
    return result, process.returncode


def _payload(prompt):
    data = json.loads(prompt or '{}')
    if not isinstance(data, dict) or set(data) != {'routine'}:
        raise ValueError('This action accepts only {"routine":"identifier"}.')
    return _routine_id(data['routine'])


async def execute_routine(owner, prompt='', **kwargs):
    _admin(owner)
    routine = _payload(prompt)
    result, code = await _cli('executer', routine)
    if result.get('routine') != routine:
        raise ValueError('The result does not match the requested routine.')
    # Import local évite de charger le modèle ou le moteur de chat.
    # Le JSON complet est conservé dans le journal de tâche et celui de la bibliothèque.
    return json.dumps(result, ensure_ascii=False, indent=2), code == 0 and result.get('status') in {
        'completed', 'completed_with_unverified_steps'}


def task_run_outcome(raw_result, success):
    """Traduire le bilan JSON en statut de tâche et journal anglais, sans modèle."""
    try:
        payload = json.loads(raw_result)
    except (ValueError, TypeError):
        return ('success' if success else 'error'), raw_result
    if not isinstance(payload, dict) or not isinstance(payload.get('steps'), list):
        return ('success' if success else 'error'), raw_result
    rows = payload['steps']
    if any(not isinstance(row, dict) for row in rows):
        return 'error', raw_result
    if not rows and payload.get('status') == 'blocked':
        rows = [dict(action=row.get('action', 'unknown'), ok=False, skipped=True,
                     error=row.get('error') or 'Not executed: the routine was blocked before application.')
                for row in payload.get('preflight', [])]
    processed = sum(row.get('ok') is True for row in rows)
    skipped = sum(row.get('ok') is not True and bool(row.get('skipped')) for row in rows)
    failed = len(rows) - processed - skipped
    unverified = sum(row.get('ok') is True and (row.get('result') or {}).get('verified') is False for row in rows)
    if payload.get('status') == 'partial_failure' and processed:
        status = 'partial'
    else:
        status = 'success' if success else 'error'
    label = {'partial':'Partially completed', 'success':'Completed', 'error':'Failed'}[status]
    names = {'volume':'Windows volume', 'luminosite':'Screen brightness', 'rgb':'RGB lighting',
             'fancontrol':'FanControl profile', 'odysseus':'Stop Odysseus'}
    lines = [f'Routine "{payload.get("routine", "unknown")}" — {label}',
             f'Processed: {processed} | Failed: {failed} | Skipped: {skipped} | Unverified: {unverified}', '']
    for index, row in enumerate(rows, 1):
        action = row.get('action', 'unknown')
        name = names.get(action, action)
        if row.get('ok') is not True:
            state = 'SKIPPED' if row.get('skipped') else 'FAILED'
            lines.append(f'{index}. [{state}] {name} — {row.get("error", "Reason unavailable")}')
            continue
        state = row.get('result') or {}
        details = []
        if action in ('volume', 'luminosite'):
            if 'after' in state:
                details.append(f'readback: {state["after"]}%')
            elif 'target' in state:
                details.append(f'requested: {state["target"]}%')
        if state.get('color'):
            details.append('requested colour: ' + str(state['color']))
        if state.get('profile'):
            details.append('requested profile: ' + str(state['profile']))
        if state.get('sent'):
            details.append('commands sent to: ' + ', '.join(map(str, state['sent'])))
        if state.get('verified') is True:
            details.append('verified')
        elif state.get('verified') is False:
            details.append('not independently verified')
        else:
            details.append('action returned successfully; verification unavailable')
        lines.append(f'{index}. [PROCESSED] {name} — ' + '; '.join(details))
    if not rows:
        for row in payload.get('preflight', []):
            name = names.get(row.get('action'), row.get('action', 'unknown'))
            reason = row.get('error') or 'Not executed: the routine was blocked before application.'
            lines.append(f'[NOT PROCESSED] {name} — {reason}')
    if payload.get('id'):
        lines.extend(['', 'Routine run: ' + str(payload['id'])])
    return status, '\n'.join(lines)


def _manual_task_authority(owner, prompt):
    """Délégation native de la synchro : une seule routine manuelle exacte.

    Ce producteur privilégié n'accorde ni shell, ni accès fichiers, ni MCP.
    Il matérialise exclusivement la création de tâches demandée par l'administrateur.
    """
    from src.agent_runtime.authority import ExactOperation, OperationGrant, RequestAuthority
    operation = ExactOperation.normalize('scheduled__' + RUN, prompt)
    authority = replace(RequestAuthority.empty(owner=owner),
                        grants=(OperationGrant(operation.tool, inputs=frozenset({operation.input})),))
    return json.dumps({'task_input':[prompt, 'action', RUN], 'authority':authority.to_dict()})


def _sync(db, owner, routines):
    from core.database import ScheduledTask
    if not isinstance(routines, list) or len(routines) > 256:
        raise ValueError('Invalid routine list.')
    # Valider toute la liste avant la première écriture.
    rows=[]
    for routine in routines:
        rid=_routine_id(routine.get('id'))
        name=routine.get('nom')
        if not isinstance(name,str) or not name.strip():
            raise ValueError('Invalid routine name.')
        rows.append((rid,name.strip()[:120]))
    if len({rid for rid,_ in rows}) != len(rows):
        raise ValueError('Duplicate routine identifiers.')
    existing=set()
    for task in db.query(ScheduledTask).filter_by(owner=owner, task_type='action', action=RUN).all():
        try:
            existing.add(_payload(task.prompt))
        except (ValueError, TypeError):
            pass
    created=[]
    for rid,name in rows:
        if rid in existing:
            continue
        prompt=json.dumps({'routine':rid}, ensure_ascii=False, separators=(',',':'))
        digest=hashlib.sha256((owner+'\0'+rid).encode('utf-8')).hexdigest()[:32]
        task_id='pc-routine-'+digest
        collision=db.get(ScheduledTask,task_id)
        if collision is not None:
            raise ValueError('A modified task already uses this identifier; no automatic changes were made.')
        db.add(ScheduledTask(id=task_id, owner=owner, name='PC · '+name,
            task_type='action', action=RUN, prompt=prompt,
            request_authority_json=_manual_task_authority(owner,prompt),
            trigger_type='manual', status='active', next_run=None,
            output_target='session', notifications_enabled=False, model=None, endpoint_url=None))
        created.append({'routine':rid,'task_id':task_id,'name':'PC · '+name})
    db.commit()
    return {'routines_detectees':len(rows),'taches_ajoutees':created,
            'taches_existantes_conservees':len(rows)-len(created),'routine_executee':False}


async def synchronise_routines(owner, prompt='', **kwargs):
    _admin(owner)
    if prompt not in ('', None, '{}'):
        raise ValueError('Synchronization does not accept paths or commands.')
    result, code=await _cli('catalogue')
    if code != 0:
        raise RuntimeError('Unable to read the catalogue.')
    from core.database import SessionLocal
    with SessionLocal() as db:
        report=_sync(db,owner,result.get('routines'))
    return json.dumps(report, ensure_ascii=False, indent=2), True
