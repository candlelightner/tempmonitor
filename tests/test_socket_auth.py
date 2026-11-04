from types import SimpleNamespace

import pytest
from nicegui import Client, core

from tempmonitor.auth import Auth, COOKIE, VIEWER_COOKIE
from tempmonitor.models import Credentials
from tempmonitor.socket_auth import bind_socket_sessions
from tempmonitor.store import Store


@pytest.mark.asyncio
async def test_socket_messages_require_rendering_session(tmp_path, monkeypatch):
    auth = Auth(Store(tmp_path / "auth.db"))
    credentials = Credentials(password="test-only-password-123")
    auth.setup(credentials, auth.setup_file.read_text())
    admin = auth.login(credentials, "admin")
    viewer = auth.viewer_login("fanclub", "viewer")
    other_viewer = auth.viewer_login("fanclub", "other-viewer")
    admin_page = SimpleNamespace(request=SimpleNamespace(cookies={COOKIE: admin}))
    viewer_page = SimpleNamespace(request=SimpleNamespace(cookies={VIEWER_COOKIE: viewer}))
    monkeypatch.setattr(Client, "instances", {"admin-page": admin_page, "viewer-page": viewer_page})

    def environ(cookie, query=""):
        return {"QUERY_STRING": query, "asgi.scope": {"type": "http", "headers": [(b"cookie", cookie.encode())]}}

    environments = {"admin": environ(f"{COOKIE}={admin}"),
                    "viewer": environ(f"{VIEWER_COOKIE}={viewer}"),
                    "other-viewer": environ(f"{VIEWER_COOKIE}={other_viewer}")}
    calls = []

    def record(sid, message, *_):
        calls.append((sid, message))
        return "accepted"

    async def async_record(sid, message, *_):
        return record(sid, message)

    sio = SimpleNamespace(handlers={"/": {event: record for event in
                          ("connect", "event", "javascript_response", "ack", "log")}},
                          get_environ=environments.get)
    sio.handlers["/"]["handshake"] = async_record
    sio.on = lambda event, handler: sio.handlers["/"].__setitem__(event, handler)
    monkeypatch.setattr(core, "sio", sio)
    bind_socket_sessions(auth)

    for event in ("handshake", "event", "javascript_response", "ack", "log"):
        handler = sio.handlers["/"][event]
        assert await handler("viewer", {"client_id": "admin-page"}) is False
        assert await handler("admin", {"client_id": "viewer-page"}) is False
        assert await handler("other-viewer", {"client_id": "viewer-page"}) is False
        assert await handler("viewer", {"client_id": "missing"}) is False
        assert await handler("viewer", {"client_id": []}) is False
        assert await handler("viewer", None) is False
        assert not calls
        assert await handler("admin", {"client_id": "admin-page"}) == "accepted"
        assert await handler("viewer", {"client_id": "viewer-page"}) == "accepted"
        calls.clear()

    connect = sio.handlers["/"]["connect"]
    assert await connect("viewer", environ(f"{VIEWER_COOKIE}={viewer}", "client_id=admin-page")) is False
    assert await connect("admin", environ(f"{COOKIE}={admin}", "client_id=admin-page")) == "accepted"
    # A reconnect with a fresh socket ID keeps the original page's cookie identity.
    assert await connect("admin-reconnect", environ(f"{COOKIE}={admin}", "client_id=admin-page")) == "accepted"
    calls.clear()
    auth.logout(admin)
    assert await sio.handlers["/"]["event"]("admin", {"client_id": "admin-page"}) is False
    with auth.store.transaction() as db:
        db.execute("UPDATE viewers SET expires=0")
    assert await sio.handlers["/"]["event"]("viewer", {"client_id": "viewer-page"}) is False
    assert not calls
