"""Update detection, quiet behavior, deduplication, and Paris DST scheduling."""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from src import update_check as checks
from src.builtin_actions import TaskNoop
from src.task_scheduler import compute_next_run, _resolve_task_timezone

LOCAL = "a" * 40
REMOTE = "b" * 40


@pytest.mark.parametrize("status,expected", [("ahead", "ahead"), ("behind", "behind"),
                                             ("diverged", "diverged")])
async def test_compare_ancestry(status, expected):
    def response(request):
        body = ({"commit": {"sha": REMOTE}} if "/branches/" in request.url.path
                else {"status": status, "ahead_by": 2})
        return httpx.Response(200, json=body)
    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        assert (await checks.probe(LOCAL, client))["status"] == expected


async def test_identical_needs_only_one_request():
    requests = []
    def response(request):
        requests.append(request)
        return httpx.Response(200, json={"commit": {"sha": LOCAL}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        assert (await checks.probe(LOCAL, client))["status"] == "identical"
    assert len(requests) == 1


@pytest.fixture
def setup_check(tmp_path, monkeypatch):
    import src.constants
    import src.runtime_paths
    import src.task_action_policy
    monkeypatch.setattr(src.constants, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(src.runtime_paths, "get_app_root", lambda: str(tmp_path))
    monkeypatch.setattr(src.task_action_policy, "owner_has_admin_task_privileges", lambda owner: owner == "test-owner")
    monkeypatch.setattr(checks, "installed_commit", lambda root: LOCAL)
    result = {"status": "ahead", "local": LOCAL, "remote": REMOTE, "ahead_by": 2}
    probe = AsyncMock(return_value=result)
    monkeypatch.setattr(checks, "probe", probe)
    return tmp_path, probe, result


async def test_update_notifies_once_and_retries_when_disabled(setup_check):
    path, probe, result = setup_check
    alerts = []
    with pytest.raises(TaskNoop, match="disabled"):
        await checks.check_updates("test-owner", notify=lambda body: False)
    assert not list(path.glob("update-check-*.json"))
    def notify(body):
        alerts.append(body)
        return True
    text, success = await checks.check_updates("test-owner", notify=notify)
    assert success and "No update has been installed" in text
    with pytest.raises(TaskNoop, match="already reported"):
        await checks.check_updates("test-owner", notify=notify)
    assert len(alerts) == 1
    result["remote"] = "c" * 40
    await checks.check_updates("test-owner", notify=notify)
    assert len(alerts) == 2


@pytest.mark.parametrize("status", ["identical", "behind"])
async def test_no_update_is_quiet(setup_check, status):
    path, probe, result = setup_check
    result["status"] = status
    alerts = []
    with pytest.raises(TaskNoop, match="no upstream update"):
        await checks.check_updates("test-owner", notify=lambda body: alerts.append(body))
    assert not alerts and not list(path.glob("update-check-*.json"))


async def test_network_failure_and_divergence_do_not_claim_success(setup_check):
    path, probe, result = setup_check
    result["status"] = "diverged"
    alerts = []
    with pytest.raises(RuntimeError, match="inconclusive"):
        await checks.check_updates("test-owner", notify=lambda body: alerts.append(body))
    probe.side_effect = httpx.ConnectError("offline")
    with pytest.raises(httpx.ConnectError):
        await checks.check_updates("test-owner", notify=lambda body: alerts.append(body))
    assert not alerts and not list(path.glob("update-check-*.json"))


async def test_rejects_non_admin_and_repository_override(setup_check):
    with pytest.raises(PermissionError):
        await checks.check_updates("someone_else")
    with pytest.raises(ValueError):
        await checks.check_updates("test-owner", prompt="another repository")


@pytest.mark.parametrize("after,expected", [
    (datetime(2026, 10, 23, 12), datetime(2026, 10, 24, 2)),
    (datetime(2026, 10, 24, 12), datetime(2026, 10, 25, 3)),
    (datetime(2027, 3, 27, 12), datetime(2027, 3, 28, 2)),
    (datetime(2027, 7, 1, 12), datetime(2027, 7, 2, 2)),
])
def test_four_am_paris_across_dst(after, expected):
    task = SimpleNamespace(action=checks.ACTION, crew_member_id=None)
    tz = _resolve_task_timezone(None, task)
    assert tz == "Europe/Paris"
    assert compute_next_run("daily", "04:00", after=after, tz_name=tz) == expected


def test_other_tasks_keep_existing_timezone_behavior():
    assert _resolve_task_timezone(None, SimpleNamespace(action="tidy_sessions", crew_member_id=None)) is None


@pytest.mark.parametrize("scenario,expected,notifications", [
    ("identical", "skipped", 0), ("ahead", "success", 1), ("offline", "error", 0),
])
async def test_native_scheduler_delivery(setup_check, monkeypatch, scenario, expected, notifications):
    """Exercise authority, run persistence and the real native notification log."""
    import core.database as cdb
    import src.task_scheduler as scheduling
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from src.agent_runtime.authority import seal_task_authority
    path, probe, result = setup_check
    result["status"] = scenario
    if scenario == "offline":
        probe.side_effect = httpx.ConnectError("offline")
    engine = create_engine("sqlite:///:memory:")
    cdb.Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr(cdb, "SessionLocal", sessions)
    monkeypatch.setattr(scheduling, "owner_has_admin_task_privileges", lambda owner: True)
    with sessions() as db:
        db.add(cdb.ScheduledTask(id="test-update", owner="test-owner", name="Odysseus update check",
            task_type="action", action=checks.ACTION, prompt="", schedule="daily",
            scheduled_time="04:00", status="active", output_target="notification",
            notifications_enabled=True, request_authority_json=seal_task_authority(
                "", "action", checks.ACTION, owner="test-owner")))
        db.commit()
    scheduler = scheduling.TaskScheduler(None)
    await scheduler._execute_task_locked("test-update", "test-run", gate_foreground=False)
    with sessions() as db:
        run = db.query(cdb.TaskRun).filter_by(id="test-run").one()
        assert run.status == expected
        assert db.query(cdb.NotificationLog).count() == notifications
        assert len(scheduler._pending_notifications) == notifications
        assert db.query(cdb.ScheduledTask).first().next_run is not None
