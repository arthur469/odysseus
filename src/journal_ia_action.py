"""Adaptateur Odysseus limité au journal personnel ; aucun paramètre de commande libre."""
import asyncio
import json
import os
from pathlib import Path

ROOT = Path.home() / 'Documents' / 'Journal-IA'

def task_run_outcome(result, success):
    """Use Odysseus' existing partial status for durable, unfinished work."""
    try:
        state = json.loads(result).get('status')
    except (ValueError,TypeError,AttributeError):
        state = None
    if state == 'partial':
        return 'partial',result
    return ('success' if success else 'error'),result

def flush_journal_notifications(notify, owner):
    """File persistante : livrer les alertes même après un arrêt du serveur."""
    import sys
    if str(ROOT) not in sys.path:
        sys.path.insert(0,str(ROOT))
    from journal_ia.incidents import Incidents
    return Incidents(ROOT).deliver_odysseus(notify,owner)

async def run_journal(owner, prompt='', progress_cb=None, **kwargs):
    from src.task_action_policy import owner_has_admin_task_privileges
    from src.builtin_actions import TaskNoop, TaskDeferred
    if not owner_has_admin_task_privileges(owner):
        raise PermissionError('Journal-IA requires the owning administrator account.')
    cfg = json.loads((ROOT / 'config.json').read_text(encoding='utf-8'))
    if owner != cfg['owner']:
        raise PermissionError('This journal is restricted to its owner.')
    if prompt not in ('', None, '{}'):
        raise ValueError('Journal-IA does not accept task commands or paths.')
    if progress_cb:
        progress_cb('Journal-IA: checking pending days and source checkpoints')
    process = await asyncio.create_subprocess_exec(
        str(ROOT / '.venv/Scripts/python.exe'), '-X', 'utf8', str(ROOT / 'cli.py'), 'tick',
        cwd=str(ROOT), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        creationflags=0x08000000,
        env=os.environ | {'PYTHONUTF8':'1', 'PYTHONIOENCODING':'utf-8'})
    waiter = asyncio.create_task(process.communicate())
    try:
        deadline = asyncio.get_running_loop().time() + 7200
        while not waiter.done():
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError('Journal-IA time limit reached; saved checkpoints will resume on the next run.')
            await asyncio.wait({waiter}, timeout=min(10, remaining))
            if not waiter.done() and progress_cb:
                try:
                    state = json.loads((ROOT / 'data/status.json').read_text(encoding='utf-8'))
                    position = (' · ' + str(state['current']) + '/' + str(state['total'])) if state.get('total') else ''
                    progress_cb('Journal-IA: ' + state.get('stage','Processing') + position)
                except (OSError, ValueError):
                    pass
        stdout, stderr = await waiter
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.wait()
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)
        raise
    try:
        result = json.loads(stdout.decode('utf-8'))
    except (ValueError, UnicodeError):
        raise RuntimeError('Journal-IA returned an invalid result; check its local status.')
    if result.get('status')=='deferred' and result.get('reason')=='chatgpt_rate_limit':
        raise TaskDeferred(result.get('message','ChatGPT read cooldown'),
                           delay_seconds=max(1,int(result['retry_after_seconds'])))
    if result.get('status') in ('noop', 'deferred'):
        raise TaskNoop(result.get('message', result['status']))
    return json.dumps(result, ensure_ascii=False, indent=2), process.returncode == 0 and result.get('status') == 'success'
