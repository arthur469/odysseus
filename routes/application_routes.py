"""Local desktop lifecycle controls for a single native Windows server."""

import asyncio
import ipaddress
import signal
import sys
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from pydantic import BaseModel
from uvicorn import Server

from core.middleware import require_admin
from src.owner_identity import INTERNAL_TOOL_USER

router = APIRouter(prefix="/api/application", tags=["application"])


class StopApplication(BaseModel):
    confirmation: Literal["stop-odysseus"]


def _is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host.lower() == "localhost"


def _require_local_admin(request: Request) -> None:
    require_admin(request)
    if (getattr(request.state, "api_token", False)
            or getattr(request.state, "current_user", None) == INTERNAL_TOOL_USER):
        raise HTTPException(403, "Use the local application settings")
    if not request.client or not _is_loopback(request.client.host):
        raise HTTPException(403, "Available only on this PC")
    if not _is_loopback(request.url.hostname or ""):
        raise HTTPException(403, "Open Odysseus through localhost")
    if any(key in request.headers for key in ("forwarded", "x-forwarded-for", "x-real-ip")):
        raise HTTPException(403, "Unavailable through a proxy")


def _native_server() -> Server | None:
    if sys.platform != "win32":
        return None
    # Uvicorn installs its bound handler on the main thread. Requesting normal
    # server exit preserves its connection drain and application shutdown hooks;
    # no process kill, console signal or child-process scan is needed.
    server = getattr(signal.getsignal(signal.SIGINT), "__self__", None)
    if (not isinstance(server, Server) or server.config.reload
            or server.config.workers != 1):
        return None
    return server


async def _stop_after_response(server: Server) -> None:
    await asyncio.sleep(0.3)
    server.should_exit = True


@router.get("/status")
async def application_status(request: Request):
    _require_local_admin(request)
    return {"can_stop": _native_server() is not None,
            "stopping": bool(getattr(request.app.state, "application_stopping", False))}


@router.post("/stop", status_code=202)
async def stop_application(request: Request, body: StopApplication, background: BackgroundTasks):
    _require_local_admin(request)
    try:
        origin = urlsplit(request.headers.get("origin", ""))
    except ValueError:
        raise HTTPException(403, "Invalid request origin") from None
    if (origin.scheme != request.url.scheme or origin.netloc != request.url.netloc
            or origin.path or origin.query or origin.fragment):
        raise HTTPException(403, "Open the local application settings to stop Odysseus")
    server = _native_server()
    if server is None:
        raise HTTPException(409, "Use the launcher to stop this installation")
    if not getattr(request.app.state, "application_stopping", False):
        request.app.state.application_stopping = True
        background.add_task(_stop_after_response, server)
    return {"status": "stopping"}
