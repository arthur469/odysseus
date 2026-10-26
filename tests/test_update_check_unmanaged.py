"""Imported source installs must not claim an update result without provenance."""
from unittest.mock import Mock
import pytest
from src import update_check as checks


def test_imported_source_has_no_invented_commit(tmp_path):
    with pytest.raises(checks.UnmanagedInstallation):
        checks.installed_commit(tmp_path)


@pytest.mark.asyncio
async def test_unmanaged_source_skips_network_and_notifications(tmp_path, monkeypatch):
    from src.builtin_actions import TaskNoop
    from src import runtime_paths, task_action_policy
    monkeypatch.setattr(runtime_paths, "get_app_root", lambda: str(tmp_path))
    monkeypatch.setattr(task_action_policy, "owner_has_admin_task_privileges", lambda owner: owner == "test-owner")
    network = Mock(side_effect=AssertionError("Network must not be requested"))
    monkeypatch.setattr(checks.httpx, "AsyncClient", network)
    notify = Mock()
    with pytest.raises(TaskNoop, match="not managed by Git"):
        await checks.check_updates("test-owner", notify=notify)
    network.assert_not_called()
    notify.assert_not_called()
    assert not list(tmp_path.glob("update-check-*.json"))
