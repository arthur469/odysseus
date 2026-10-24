"""Read-only upstream check using the existing task and notification systems."""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import re
import subprocess

import httpx

ACTION = "check_odysseus_updates"
TIMEZONE = "Europe/Paris"
REPOSITORY = "odysseus-dev/odysseus"
BRANCH = "main"


def action_timezone(action):
    return TIMEZONE if action == ACTION else None


def installed_commit(root):
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True,
        text=True, check=True, timeout=10,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    sha = result.stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("Could not verify the installed Git commit.")
    return sha


async def probe(local_sha, client):
    """Compare ancestry, rather than treating any different SHA as an update."""
    base = f"https://api.github.com/repos/{REPOSITORY}"
    response = await client.get(f"{base}/branches/{BRANCH}")
    response.raise_for_status()
    remote = response.json()["commit"]["sha"]
    if not re.fullmatch(r"[0-9a-f]{40}", remote):
        raise ValueError("GitHub returned an invalid commit identifier.")
    if remote == local_sha:
        return {"status": "identical", "local": local_sha, "remote": remote, "ahead_by": 0}
    response = await client.get(f"{base}/compare/{local_sha}...{remote}")
    response.raise_for_status()
    comparison = response.json()
    status = comparison["status"]
    if status not in {"ahead", "behind", "identical", "diverged"}:
        raise ValueError("GitHub returned an unrecognized comparison status.")
    return {"status": status, "local": local_sha, "remote": remote,
            "ahead_by": int(comparison["ahead_by"])}


async def check_updates(owner, prompt="", notify=None, task_name="Odysseus update check", **kwargs):
    from src.builtin_actions import TaskNoop
    from src.constants import DATA_DIR
    from src.runtime_paths import get_app_root
    from src.task_action_policy import owner_has_admin_task_privileges

    if not owner or not owner_has_admin_task_privileges(owner):
        raise PermissionError("The update check requires the owning administrator.")
    if prompt not in (None, "", "{}"):
        raise ValueError("The update check does not accept commands or repository overrides.")
    root = Path(get_app_root())
    local = await asyncio.to_thread(installed_commit, root)
    async with httpx.AsyncClient(timeout=20, headers={
        "Accept": "application/vnd.github+json", "User-Agent": "Odysseus-local-update-check",
        "X-GitHub-Api-Version": "2022-11-28",
    }) as client:
        result = await probe(local, client)
    summary = (f"[PROCESSED] GitHub {REPOSITORY}/{BRANCH} checked against installed commit "
               f"{local[:12]}; verification: GitHub commit ancestry API.")
    if result["status"] in {"identical", "behind"}:
        raise TaskNoop(summary + "\n[SKIPPED] Notification: no upstream update available.")
    if result["status"] == "diverged":
        raise RuntimeError(summary + "\n[FAILED] Installed and upstream histories have diverged; "
                           "update availability is inconclusive. No notification sent.")
    state_path = Path(DATA_DIR) / ("update-check-" + hashlib.sha256(owner.encode()).hexdigest()[:16] + ".json")
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    key = f"{REPOSITORY}:{BRANCH}:{result['remote']}"
    if state.get("last_notified") == key:
        raise TaskNoop(summary + "\n[SKIPPED] Notification: this upstream update was already reported.")
    body = (f"Odysseus update available on {BRANCH}: {result['ahead_by']} new commit(s). "
            f"Installed: {local[:12]}; available: {result['remote'][:12]}. "
            f"https://github.com/{REPOSITORY}/compare/{local}...{result['remote']} "
            "No update has been installed. Local customizations require review before updating.")
    if notify is None or not notify(body):
        raise TaskNoop(summary + "\n[SKIPPED] Notification: disabled for this task; update remains available.")
    # Use the application's existing atomic writer after queuing the alert.
    from core.atomic_io import atomic_write_json
    atomic_write_json(state_path, {"last_notified": key, "comparison": result})
    return summary + "\n[PROCESSED] Update notification queued in Odysseus.\n" + body, True
