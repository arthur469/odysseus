"""Journal worker partial results must reach the existing task partial status."""
import asyncio
import json
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


@pytest.mark.parametrize('status,success,expected',[
    ('partial',False,'partial'),('error',False,'error'),('success',True,'success')])
def test_journal_status_persisted_by_scheduler(tmp_path,monkeypatch,status,success,expected):
    import core.database as cd
    import src.task_scheduler as module
    engine=create_engine(f"sqlite:///{tmp_path/'journal-tasks.db'}")
    cd.Base.metadata.create_all(engine)
    factory=sessionmaker(bind=engine)
    monkeypatch.setattr(cd,'SessionLocal',factory)
    monkeypatch.setattr(module,'owner_has_admin_task_privileges',lambda owner:True)
    result=json.dumps({'status':status,'results':[{'date':'2026-10-23','status':status}]})
    with factory() as db:
        db.add(cd.ScheduledTask(id='journal',owner='test-owner',name='Journal recovery',
               task_type='action',action='journal_ia',status='active',trigger_type='event'))
        db.add(cd.TaskRun(id='run',task_id='journal',status='queued'))
        db.commit()
    async def drive():
        scheduler=module.TaskScheduler(None)
        scheduler._execute_action=AsyncMock(return_value=(result,success))
        scheduler._deliver_task_result=AsyncMock()
        scheduler._log_to_assistant=Mock()
        await scheduler._execute_task_locked('journal','run',gate_foreground=False,release_executing=False)
        assert scheduler._deliver_task_result.call_count==(0 if expected=='error' else 1)
    asyncio.run(drive())
    with factory() as db:
        run=db.get(cd.TaskRun,'run')
        assert run.status==expected
        assert run.result==result
        assert (run.error is not None)==(expected=='error')
        assert run.finished_at is not None
    engine.dispose()
