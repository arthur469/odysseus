"""Local desktop shutdown must never grant a remote or tool-driven stop."""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from uvicorn import Config, Server

from routes import application_routes as controls


@pytest.fixture
def application(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    app = FastAPI()
    app.include_router(controls.router)
    app.state.auth_manager = SimpleNamespace(
        is_configured=True, is_admin=lambda user: user == "admin",
    )

    @app.middleware("http")
    async def identity(request, call_next):
        request.state.current_user = request.headers.get("test-user", "admin")
        request.state.api_token = request.headers.get("test-token") == "yes"
        return await call_next(request)

    server = SimpleNamespace(should_exit=False)
    monkeypatch.setattr(controls, "_native_server", lambda: server)
    stopped = []

    async def stop_after_response(target):
        stopped.append(target)
        target.should_exit = True

    monkeypatch.setattr(controls, "_stop_after_response", stop_after_response)
    return app, server, stopped


def client(app, host="127.0.0.1", base="http://localhost:7000"):
    return TestClient(app, base_url=base, client=(host, 43210))


def test_confirmed_local_admin_stop_and_duplicate(application):
    app, server, stopped = application
    with client(app) as browser:
        assert browser.get("/api/application/status").json()["can_stop"] is True
        assert server.should_exit is False
        for _ in range(2):
            response = browser.post("/api/application/stop",
                                    headers={"origin": "http://localhost:7000"},
                                    json={"confirmation": "stop-odysseus"})
            assert response.status_code == 202
    assert stopped == [server]
    assert server.should_exit is True


@pytest.mark.parametrize("headers,host,base", [
    ({"test-user": "user"}, "127.0.0.1", "http://localhost:7000"),
    ({"test-user": ""}, "127.0.0.1", "http://localhost:7000"),
    ({"test-user": "internal-tool"}, "127.0.0.1", "http://localhost:7000"),
    ({"test-token": "yes"}, "127.0.0.1", "http://localhost:7000"),
    ({}, "192.168.1.10", "http://localhost:7000"),
    ({}, "127.0.0.1", "http://untrusted.example:7000"),
    ({"x-forwarded-for": "127.0.0.1"}, "127.0.0.1", "http://localhost:7000"),
    ({"forwarded": "for=127.0.0.1"}, "127.0.0.1", "http://localhost:7000"),
])
def test_untrusted_request_cannot_stop(application, headers, host, base):
    app, server, stopped = application
    with client(app, host, base) as browser:
        assert browser.get("/api/application/status", headers=headers).status_code == 403
        response = browser.post("/api/application/stop", headers={"origin": base, **headers},
                                json={"confirmation": "stop-odysseus"})
        assert response.status_code == 403
    assert not stopped and not server.should_exit


@pytest.mark.parametrize("origin", [None, "null", "https://localhost:7000",
                                     "http://localhost:7001", "http://evil.example",
                                     "http://localhost:7000/path", "http://["])
def test_origin_required(application, origin):
    app, server, stopped = application
    with client(app) as browser:
        response = browser.post("/api/application/stop",
                                headers={"origin": origin} if origin else {},
                                json={"confirmation": "stop-odysseus"})
        assert response.status_code == 403
    assert not stopped and not server.should_exit


def test_confirmation_and_launcher_required(application, monkeypatch):
    app, server, stopped = application
    with client(app) as browser:
        assert browser.post("/api/application/stop", json={}).status_code == 422
        assert browser.get("/api/application/stop").status_code == 405
        monkeypatch.setattr(controls, "_native_server", lambda: None)
        assert browser.get("/api/application/status").json()["can_stop"] is False
        response = browser.post("/api/application/stop",
                                headers={"origin": "http://localhost:7000"},
                                json={"confirmation": "stop-odysseus"})
        assert response.status_code == 409
    assert not stopped and not server.should_exit


def test_native_server_detection(monkeypatch):
    monkeypatch.setattr(controls.sys, "platform", "win32")
    server = Server(Config(FastAPI(), workers=1))
    monkeypatch.setattr(controls.signal, "getsignal", lambda _: server.handle_exit)
    assert controls._native_server() is server
    server.config.reload = True
    assert controls._native_server() is None
    server.config.reload = False
    server.config.workers = 2
    assert controls._native_server() is None
    monkeypatch.setattr(controls.signal, "getsignal", lambda _: lambda *_: None)
    assert controls._native_server() is None


@pytest.mark.asyncio
async def test_normal_exit_is_requested_without_force(monkeypatch):
    server = SimpleNamespace(should_exit=False, force_exit=False)
    waits = []

    async def sleep(delay):
        waits.append(delay)

    monkeypatch.setattr(controls.asyncio, "sleep", sleep)
    await controls._stop_after_response(server)
    assert server.should_exit is True and server.force_exit is False
    assert waits
