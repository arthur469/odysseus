"""Behavioral regressions for the Windows email-task audit (no live mail)."""
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


@pytest.fixture
def task_db(tmp_path, monkeypatch):
    import core.database as cd
    import routes.task.task_routes as routes
    engine = create_engine(f"sqlite:///{tmp_path / 'audit.db'}")
    cd.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(cd, "SessionLocal", factory)
    monkeypatch.setattr(routes, "SessionLocal", factory)
    yield factory
    engine.dispose()


@pytest.mark.parametrize("action", ["summarize_emails", "check_email_urgency"])
@pytest.mark.parametrize("schedule,cron,time", [
    ("cron", "*/15 * * * *", None),
    ("cron", "0 */6 * * *", None),
    ("daily", None, "17:35"),
    ("weekly", None, "08:20"),
])
def test_saved_email_schedule_survives_repeated_default_loading(
        task_db, monkeypatch, action, schedule, cron, time):
    import core.database as cd
    import routes.task.task_routes as routes
    import routes.prefs_routes as prefs
    from src.task_scheduler import TaskScheduler, HOUSEKEEPING_DEFAULTS
    monkeypatch.setattr(prefs, "_load_for_user", lambda owner: {"tasks_opened": True})
    monkeypatch.setattr(routes, "get_current_user", lambda request: "audit-user")
    scheduler = TaskScheduler(None)
    monkeypatch.setattr(scheduler, "ensure_assistant_defaults", AsyncMock())
    with task_db() as db:
        db.add(cd.ScheduledTask(id="email", owner="audit-user", task_type="action",
            action=action, name=HOUSEKEEPING_DEFAULTS[action]["name"], status="active",
            trigger_type="schedule", schedule="cron", cron_expression="0 */2 * * *"))
        db.commit()
    router = routes.setup_task_routes(scheduler)
    update = next(r.endpoint for r in router.routes
                  if r.path == "/api/tasks/{task_id}" and "PUT" in r.methods)

    async def drive():
        payload = routes.TaskUpdate(schedule=schedule, cron_expression=cron or "",
                                    scheduled_time=time, scheduled_day=2)
        saved = await update(None, "email", payload)
        expected_next = saved["next_run"]
        for _ in range(3):
            await scheduler.ensure_defaults("audit-user")
        return expected_next

    expected_next = asyncio.run(drive())
    with task_db() as db:
        row = db.get(cd.ScheduledTask, "email")
        assert (row.schedule, row.cron_expression, row.scheduled_time) == (schedule, cron, time)
        assert row.next_run.isoformat() + "Z" == expected_next
        assert row.status == "active"


@pytest.mark.parametrize("queued", [False, True])
@pytest.mark.parametrize("foreground", [False, True])
def test_cancellation_reports_reason_and_preserves_retry(task_db, monkeypatch, queued, foreground):
    import core.database as cd
    from src.task_scheduler import TaskScheduler
    from src.agent_runtime.authority import seal_task_authority
    monkeypatch.setenv("BACKGROUND_TASK_FOREGROUND_GATE", "false")
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with task_db() as db:
        db.add(cd.ScheduledTask(id="email", owner="audit-user", name="Email Tags",
            task_type="action", action="check_email_urgency", status="active",
            schedule="cron", cron_expression="0 * * * *", next_run=now - timedelta(seconds=1),
            request_authority_json=seal_task_authority(None, "action", "check_email_urgency", owner="audit-user")))
        db.commit()

    async def drive():
        scheduler = TaskScheduler(None)
        started = asyncio.Event()
        async def blocked(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()
        monkeypatch.setattr(scheduler, "_execute_action", blocked)
        if queued:
            await scheduler._run_semaphore.acquire()
        scheduler._executing.add("email")
        pending = asyncio.create_task(scheduler._execute_task("email"))
        if queued:
            for _ in range(100):
                with task_db() as db:
                    if db.query(cd.TaskRun).count():
                        break
                await asyncio.sleep(.01)
            else:
                pytest.fail("task never queued")
        else:
            await asyncio.wait_for(started.wait(), 3)
        if foreground:
            await scheduler.stop_background_tasks_for_foreground(reason="audit interaction")
        else:
            assert await scheduler.stop_task("email")
        try:
            await pending
        except asyncio.CancelledError:
            pass
        if queued:
            scheduler._run_semaphore.release()
        assert not scheduler._foreground_stopped()

    asyncio.run(drive())
    message = "Paused because Odysseus became active" if foreground else "Stopped by user"
    with task_db() as db:
        run = db.query(cd.TaskRun).one()
        assert run.status == "aborted"
        assert run.error == message
        assert run.result == message
        assert run.finished_at is not None
        row = db.get(cd.ScheduledTask, "email")
        assert row.next_run > now
        if foreground:
            assert now + timedelta(minutes=14) < row.next_run < now + timedelta(minutes=16)


def test_login_probes_are_passive_but_login_submissions_are_interactive():
    from src.interactive_gate import should_track_interactive_request
    assert not should_track_interactive_request("/login", "GET")
    assert not should_track_interactive_request("/login", "HEAD")
    assert should_track_interactive_request("/api/auth/login", "POST")
    assert should_track_interactive_request("/api/chat_stream", "POST")


def test_foreground_preemption_preserves_local_pc_routines(task_db):
    import core.database as cd
    from src.task_scheduler import TaskScheduler
    with task_db() as db:
        db.add(cd.ScheduledTask(id="pc", owner="audit-user", name="PC routine",
            task_type="action", action="pc_executer_routine", status="active"))
        db.commit()
    async def drive():
        scheduler = TaskScheduler(None)
        pending = asyncio.create_task(asyncio.Event().wait())
        scheduler._executing.add("pc")
        scheduler._task_handles["pc"] = pending
        try:
            assert await scheduler.stop_background_tasks_for_foreground() == 0
            assert not pending.cancelling()
        finally:
            pending.cancel()
            try:
                await pending
            except asyncio.CancelledError:
                pass
    asyncio.run(drive())


def test_cancellation_keeps_substantive_progress(task_db):
    import core.database as cd
    from src.task_scheduler import TaskScheduler
    with task_db() as db:
        db.add(cd.ScheduledTask(id="email", owner="audit-user", name="Email Tags"))
        db.add(cd.TaskRun(id="run", task_id="email", status="running", result="Scanned 3 messages"))
        db.commit()
    assert TaskScheduler(None)._mark_run_aborted("email", message="Paused because Odysseus became active")
    with task_db() as db:
        run = db.get(cd.TaskRun, "run")
        assert run.result == "Scanned 3 messages"
        assert run.error == "Paused because Odysseus became active"


def test_idle_tab_heartbeat_only_fires_on_interaction():
    import subprocess
    source = (Path(__file__).resolve().parents[1] / "static/app.js").read_text(encoding="utf-8")
    function = source[source.index("function initForegroundActivityHeartbeat() {"):
                      source.index("initForegroundActivityHeartbeat();")]
    script = r'''
const assert = require('node:assert/strict');
let calls = 0, now = 20000;
const listeners = {};
const window = {addEventListener: (k, v) => listeners[k] = v};
const document = {visibilityState: 'visible', addEventListener: (k, v) => listeners[k] = v};
const navigator = {sendBeacon: () => {calls++; return true;}};
const Date = {now: () => now};
const Blob = function() {};
const setInterval = () => {throw new Error('idle tab scheduled a periodic heartbeat');};
''' + function + r'''
initForegroundActivityHeartbeat();
assert.equal(calls, 1);
now += 60000;
assert.equal(calls, 1);
listeners.pointerdown();
assert.equal(calls, 2);
listeners.keydown();
assert.equal(calls, 2);
now += 15000;
document.visibilityState = 'hidden';
listeners.scroll();
assert.equal(calls, 2);
document.visibilityState = 'visible';
listeners.visibilitychange();
assert.equal(calls, 3);
'''
    subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
