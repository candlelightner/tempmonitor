import time

from fastapi.testclient import TestClient
import pytest

from tempmonitor.auth import Auth
from tempmonitor.models import Credentials, PasswordChange, Settings
from tempmonitor.store import Store
from tempmonitor.web import create_app

PASSWORD = "test-only-password-123"


def test_setup_password_hash_sessions_persist_and_revoke(tmp_path):
    store = Store(tmp_path / "db.sqlite")
    auth = Auth(store)
    assert auth.setup_file.stat().st_mode & 0o777 == 0o600
    credentials = Credentials(username="admin", password=PASSWORD)
    with pytest.raises(PermissionError):
        auth.setup(credentials, "incorrect")
    auth.setup(credentials, auth.setup_file.read_text())
    assert not auth.setup_file.exists()
    with store.transaction() as db:
        assert db.execute("SELECT password FROM admin").fetchone()[0].startswith("$argon2id$")
    token = auth.login(credentials, "local")
    assert Auth(Store(tmp_path / "db.sqlite")).session(token)["username"] == "admin"
    with store.transaction() as db:
        assert db.execute("SELECT token FROM sessions").fetchone()[0] != token
    auth.change_password(token, PasswordChange(current_password=PASSWORD, new_password="different-password-456"))
    assert auth.session(token) is None
    with pytest.raises(PermissionError):
        auth.login(credentials, "local")
    token = auth.login(Credentials(password="different-password-456"), "local")
    auth.logout(token)
    assert auth.session(token) is None


def test_login_rate_limit_and_expiry(tmp_path):
    auth = Auth(Store(tmp_path / "db.sqlite"))
    auth.setup(Credentials(password=PASSWORD), auth.setup_file.read_text())
    for _ in range(5):
        with pytest.raises(PermissionError):
            auth.login(Credentials(password="wrong-password-123"), "peer")
    with pytest.raises(TimeoutError):
        auth.login(Credentials(password=PASSWORD), "peer")
    token = auth.login(Credentials(password=PASSWORD), "another-peer")
    with auth.store.transaction() as db:
        db.execute("UPDATE sessions SET expires=?", (time.time() - 1,))
    assert auth.session(token) is None


@pytest.fixture
def client(tmp_path):
    app = create_app(Settings(data_dir=tmp_path, public_origin="https://testserver"), with_dashboard=False)
    with TestClient(app, base_url="https://testserver") as client:
        client.headers["origin"] = "https://testserver"
        yield client


def sign_in(client):
    token = client.app.state.auth.setup_file.read_text()
    assert client.post("/setup", data={"setup_token": token, "username": "admin", "password": PASSWORD},
                       follow_redirects=False).status_code == 303
    response = client.post("/login", data={"username": "admin", "password": PASSWORD}, follow_redirects=False)
    assert response.status_code == 303
    return response


def test_authenticated_endpoints_origin_csrf_and_cookie(client):
    assert client.get("/api/status").status_code == 401
    assert client.get("/setup").status_code == 200
    response = sign_in(client)
    cookie = response.headers["set-cookie"]
    assert all(flag in cookie.lower() for flag in ("httponly", "secure", "samesite=strict", "max-age="))
    assert client.get("/api/status").status_code == 200
    csrf = client.get("/api/session").json()["csrf"]
    curve = {"points": [{"temperature": t, "duty": d} for t, d in ((35, 0), (45, 35), (60, 70), (75, 100))]}
    path = "/api/channels/1/curve"
    assert client.put(path, json=curve).status_code == 403
    assert client.put(path, json=curve, headers={"x-csrf-token": csrf, "origin": "https://attacker.example"}).status_code == 403
    assert client.put(path, json=curve, headers={"x-csrf-token": csrf}).status_code == 200
    assert client.app.state.store.curves()[0].points[-1].duty == 100
    assert client.put("/api/channels/5/override", json={"duty": 70}, headers={"x-csrf-token": csrf}).status_code == 422
    assert client.get("/api/history?hours=900").status_code == 422
    assert client.get("/api/history").status_code == 200
    assert client.post("/logout", data={"csrf": "wrong"}).status_code == 403
    assert client.post("/logout", data={"csrf": csrf}, follow_redirects=False).status_code == 303
    assert client.get("/api/status").status_code == 401


def test_password_change_revokes_other_sessions_and_hides_input(client):
    sign_in(client)
    csrf = client.get("/api/session").json()["csrf"]
    response = client.post("/password", data={"csrf": csrf, "current_password": PASSWORD,
                                             "new_password": "new-local-password-789"}, follow_redirects=False)
    assert response.status_code == 303
    assert client.get("/api/status").status_code == 401
    assert PASSWORD not in response.text
    assert client.post("/login", data={"username": "admin", "password": PASSWORD}).status_code == 401


def test_no_cross_origin_or_anonymous_socketio(client):
    path = "/_nicegui_ws/socket.io/?EIO=4&transport=polling"
    assert client.get(path).status_code == 403
    sign_in(client)
    assert client.get(path, headers={"origin": "https://evil.example"}).status_code == 403
    assert client.post("/setup", data={}, headers={"origin": "https://evil.example"}).status_code == 403


def test_setup_cannot_replace_existing_admin(client):
    sign_in(client)
    response = client.post("/setup", data={"setup_token": "anything", "username": "other", "password": PASSWORD})
    assert response.status_code == 400
    assert client.get("/api/session").json()["username"] == "admin"
