"""Trace real Run route, scheduler, file SQLite and Activity route, stubbing effects."""
import asyncio
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


@pytest.mark.parametrize('outcome', ['success', 'partial', 'error', 'skipped'])
def test_run_persists_and_activity_exposes_owner_scoped_lifecycle(tmp_path, monkeypatch, outcome):
    import core.database as cd
    import routes.task.task_routes as routes
    import src.task_scheduler as module
    from src.builtin_actions import TaskNoop

    engine = create_engine(f"sqlite:///{tmp_path/'tasks.db'}")
    cd.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(cd, 'SessionLocal', factory)
    monkeypatch.setattr(routes, 'SessionLocal', factory)
    monkeypatch.setattr(routes, 'get_current_user', lambda request: request.headers.get('x-test-owner', 'alice'))
    monkeypatch.setattr(routes, 'owner_has_admin_task_privileges', lambda owner: True)
    monkeypatch.setattr(module, 'owner_has_admin_task_privileges', lambda owner: True)
    with factory() as db:
        db.add(cd.ScheduledTask(id='journal', owner='alice', name='Journal-IA daily',
            task_type='action', action='journal_ia', status='active', trigger_type='event', output_target='session'))
        db.add(cd.ScheduledTask(id='private', owner='bob', name='Private task',
            task_type='action', status='active', trigger_type='event'))
        db.add(cd.TaskRun(id='private-run', task_id='private', status='success', result='Private content'))
        db.commit()
    trace = []

    async def drive():
        scheduler = module.TaskScheduler(None)
        scheduler._deliver_task_result = AsyncMock()
        scheduler._log_to_assistant = Mock()
        started, release = asyncio.Event(), asyncio.Event()
        original = scheduler._execute_task_locked

        async def traced(task_id, run_id, **kwargs):
            with factory() as db:
                row = db.get(cd.TaskRun, run_id)
                trace.append({'boundary': 'persisted-before-execution', 'run_id': row.id, 'status': row.status})
                assert row.status == 'queued'
            return await original(task_id, run_id, **kwargs)

        async def action(task, run_id=None):
            with factory() as db:
                row = db.get(cd.TaskRun, run_id)
                row.result = 'Collecting sources: 1/2'
                db.commit()
            trace.append({'boundary': 'action-entered', 'run_id': run_id, 'status': 'running'})
            started.set()
            await release.wait()
            if outcome == 'skipped':
                raise TaskNoop('No pending day; no publication attempted')
            return json.dumps({'status': outcome, 'results': [{'status': outcome}]}), outcome == 'success'

        scheduler._execute_task_locked = traced
        scheduler._execute_action = action
        app = FastAPI()
        app.include_router(routes.setup_task_routes(scheduler))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://isolated') as client:
            accepted = await client.post('/api/tasks/journal/run')
            assert accepted.status_code == 200
            trace.append({'boundary': 'run-route', 'http': accepted.status_code})
            await asyncio.wait_for(started.wait(), 5)
            worker = scheduler._task_handles['journal']
            duplicate = await client.post('/api/tasks/journal/run')
            assert duplicate.status_code == 409
            denied = await client.post('/api/tasks/journal/run', headers={'x-test-owner': 'bob'})
            assert denied.status_code == 403
            live = await client.get('/api/tasks/runs/recent?limit=40')
            assert live.status_code == 200
            row = live.json()['runs'][0]
            assert row['task_id'] == 'journal' and row['status'] == 'running'
            assert row['result'] == 'Collecting sources: 1/2'
            assert row['output_target'] == 'session'
            assert all(r['task_id'] != 'private' for r in live.json()['runs'])
            trace.append({'boundary': 'activity-api-running', 'run_id': row['id'], 'status': row['status']})
            # Activity reads and browser foreground cancellation cannot terminate this manual run.
            assert await scheduler.stop_background_tasks_for_foreground(reason='test heartbeat') == 0
            release.set()
            await asyncio.wait_for(worker, 5)
            final = (await client.get('/api/tasks/runs/recent?limit=40')).json()['runs'][0]
            assert final['status'] == outcome and final['finished_at']
            assert final['id'] == row['id']
            detail = (await client.get('/api/tasks/journal/runs?limit=1')).json()
            assert detail['total'] == 1 and detail['runs'][0]['id'] == row['id']
            assert (await client.get('/api/tasks/journal/runs', headers={'x-test-owner': 'bob'})).status_code == 403
            trace.append({'boundary': 'activity-api-final', 'run_id': final['id'], 'status': final['status']})
            with factory() as db:
                saved = db.get(cd.TaskRun, row['id'])
                assert saved.status == outcome and saved.finished_at
                assert db.query(cd.TaskRun).filter(cd.TaskRun.task_id == 'journal').count() == 1
            # A new connection observes persisted status, independently of the running scheduler.
            trace.append({'boundary': 'fresh-database-connection', 'status': outcome})
    try:
        asyncio.run(drive())
        if os.environ.get('ACTIVITY_TRACE_DIR'):
            Path(os.environ['ACTIVITY_TRACE_DIR'], f'pipeline-{outcome}.json').write_text(json.dumps(trace, indent=2))
    finally:
        engine.dispose()


def test_activity_limit_preview_and_history_offset(tmp_path, monkeypatch):
    import core.database as cd
    import routes.task.task_routes as routes
    from datetime import datetime, timedelta
    engine = create_engine(f"sqlite:///{tmp_path/'pagination.db'}")
    cd.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(routes, 'SessionLocal', factory)
    monkeypatch.setattr(routes, 'get_current_user', lambda request: 'alice')
    with factory() as db:
        db.add(cd.ScheduledTask(id='journal', owner='alice', name='Journal', task_type='action', action='journal_ia'))
        for i in range(3):
            db.add(cd.TaskRun(id=f'run-{i}', task_id='journal', status='success', result='x' * 1200,
                started_at=datetime(2026, 10, 24) + timedelta(minutes=i)))
        db.commit()
    async def drive():
        app = FastAPI()
        app.include_router(routes.setup_task_routes(Mock()))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://isolated') as client:
            first = (await client.get('/api/tasks/runs/recent?limit=1&max_result_chars=500')).json()
            assert first['has_more'] and [r['id'] for r in first['runs']] == ['run-2']
            assert first['runs'][0]['result'] == 'x' * 500 + '\n\n[Activity preview truncated]'
            all_runs = (await client.get('/api/tasks/runs/recent?limit=200')).json()
            assert not all_runs['has_more'] and len(all_runs['runs']) == 3
            offset = (await client.get('/api/tasks/journal/runs?limit=1&offset=1')).json()
            assert offset['total'] == 3 and offset['runs'][0]['id'] == 'run-1'
            assert offset['runs'][0]['result'] == 'x' * 1200
    try:
        asyncio.run(drive())
    finally:
        engine.dispose()
