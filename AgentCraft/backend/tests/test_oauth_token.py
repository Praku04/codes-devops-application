"""The OAuth2 password flow behind Swagger's Authorize button.

`POST /auth/token` exists so the docs page can sign a person in from a form instead of
making them paste a token. It is a second door onto the same credentials, so what matters
here is that it is not a *weaker* door: the approval gate, the rejection message and the
deleted-account message must all behave exactly as they do on `/auth/login`.
"""

import pytest
from fastapi.testclient import TestClient

from app.db.session import init_db
from app.main import create_app

ADMIN_EMAIL, ADMIN_PW = "admin@ac.com", "1681149@sPk"
USER_EMAIL, USER_PW = "oauth-user@example.com", "secret12"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "t.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("JWT_SECRET", "test-secret-at-least-thirty-two-chars!!")
    monkeypatch.setenv("SUPER_ADMIN_EMAIL", ADMIN_EMAIL)
    monkeypatch.setenv("SUPER_ADMIN_PASSWORD", ADMIN_PW)
    import app.core.config as cfg
    import app.db.session as sess

    cfg.get_settings.cache_clear()
    sess._engine = None
    sess._SessionLocal = None
    init_db()
    with TestClient(create_app()) as c:
        yield c


def _token(client: TestClient, email: str, password: str):
    """Sign in the way Swagger does: form-encoded, email in the username field."""
    return client.post(
        "/api/v1/auth/token",
        data={"username": email, "password": password, "grant_type": "password"},
    )


def _admin_headers(client: TestClient) -> dict[str, str]:
    r = _token(client, ADMIN_EMAIL, ADMIN_PW)
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _signup(client: TestClient) -> str:
    r = client.post(
        "/api/v1/auth/signup",
        json={"email": USER_EMAIL, "password": USER_PW, "name": "OAuth User"},
    )
    assert r.status_code == 200, r.text
    admin = _admin_headers(client)
    users = client.get("/api/v1/admin/users", headers=admin).json()
    return next(u["id"] for u in users if u["email"] == USER_EMAIL)


def test_token_returns_only_the_two_oauth_fields(client):
    """Swagger reads access_token/token_type; anything else is noise it ignores."""
    body = _token(client, ADMIN_EMAIL, ADMIN_PW).json()
    assert set(body) == {"access_token", "token_type"}
    assert body["token_type"] == "bearer"


def test_token_is_form_encoded_not_json(client):
    """The OAuth2 dialog posts a form. JSON must not be silently accepted."""
    r = client.post("/api/v1/auth/token", json={"username": ADMIN_EMAIL, "password": ADMIN_PW})
    assert r.status_code == 422


def test_token_accepts_the_swagger_client_credential_fields(client):
    """The dialog always sends client_id/client_secret. Blank or filled, they are ignored."""
    r = client.post(
        "/api/v1/auth/token",
        data={
            "username": ADMIN_EMAIL,
            "password": ADMIN_PW,
            "grant_type": "password",
            "client_id": "",
            "client_secret": "",
        },
    )
    assert r.status_code == 200, r.text


def test_token_works_on_padlocked_endpoints(client):
    tok = _token(client, ADMIN_EMAIL, ADMIN_PW).json()["access_token"]
    r = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200
    assert r.json()["email"] == ADMIN_EMAIL


def test_token_is_declared_as_the_oauth2_password_flow(client):
    """This is what draws the username/password form instead of a paste-a-token box."""
    spec = client.get("/openapi.json").json()
    schemes = spec["components"]["securitySchemes"]
    assert "OAuth2PasswordBearer" in schemes
    scheme = schemes["OAuth2PasswordBearer"]
    assert scheme["type"] == "oauth2"
    assert scheme["flows"]["password"]["tokenUrl"] == "/api/v1/auth/token"
    # Padlocks: protected endpoints reference the scheme, the three public ones do not.
    assert spec["paths"]["/api/v1/auth/me"]["get"]["security"] == [{"OAuth2PasswordBearer": []}]
    for path, method in [
        ("/api/v1/auth/login", "post"),
        ("/api/v1/auth/signup", "post"),
        ("/api/v1/auth/token", "post"),
    ]:
        assert not spec["paths"][path][method].get("security"), path


def test_token_refuses_a_pending_account_like_login_does(client):
    _signup(client)
    r = _token(client, USER_EMAIL, USER_PW)
    assert r.status_code == 403
    assert "approval" in r.json()["message"].lower()


def test_token_refuses_a_rejected_account(client):
    uid = _signup(client)
    admin = _admin_headers(client)
    assert client.post(f"/api/v1/admin/users/{uid}/reject", headers=admin).status_code == 200
    r = _token(client, USER_EMAIL, USER_PW)
    assert r.status_code == 403
    assert "rejected" in r.json()["message"].lower()


def test_token_works_once_approved(client):
    uid = _signup(client)
    admin = _admin_headers(client)
    assert client.post(f"/api/v1/admin/users/{uid}/approve", headers=admin).status_code == 200
    r = _token(client, USER_EMAIL, USER_PW)
    assert r.status_code == 200, r.text
    tok = r.json()["access_token"]
    # Authenticated, but still only a user: the role check sits on top of the padlock.
    h = {"Authorization": f"Bearer {tok}"}
    assert client.get("/api/v1/projects", headers=h).status_code == 200
    assert client.get("/api/v1/admin/users", headers=h).status_code == 403


def test_token_rejects_a_wrong_password(client):
    r = _token(client, ADMIN_EMAIL, "not-the-password")
    assert r.status_code == 401
    assert r.json()["message"] == "Invalid email or password"


def test_token_tells_a_deleted_user_what_happened(client):
    uid = _signup(client)
    admin = _admin_headers(client)
    client.post(f"/api/v1/admin/users/{uid}/approve", headers=admin)
    assert client.request("DELETE", f"/api/v1/admin/users/{uid}", headers=admin).status_code == 200
    r = _token(client, USER_EMAIL, USER_PW)
    assert r.status_code == 403
    assert "delete" in r.json()["message"].lower()


def test_missing_and_malformed_authorization_keep_our_error_shape(client):
    """auto_error=False is deliberate: the UI and CLI branch on 401 + `message`."""
    for headers in ({}, {"Authorization": "Basic abc"}, {"Authorization": "Bearer "}):
        r = client.get("/api/v1/auth/me", headers=headers)
        assert r.status_code == 401, headers
        assert r.json()["message"] == "Authentication required", headers
    r = client.get("/api/v1/auth/me", headers={"Authorization": "Bearer garbage"})
    assert r.status_code == 401
    assert r.json()["message"] == "Invalid or expired token"


def test_lowercase_bearer_prefix_is_accepted(client):
    """Some clients send `bearer`. The scheme compares case-insensitively."""
    tok = _token(client, ADMIN_EMAIL, ADMIN_PW).json()["access_token"]
    assert client.get("/api/v1/auth/me", headers={"Authorization": f"bearer {tok}"}).status_code == 200


def test_json_login_still_returns_the_user_for_the_ui_and_cli(client):
    """The UI and CLI depend on this; /token must not have replaced it."""
    r = client.post("/api/v1/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PW})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["user"]["role"] == "admin"
    tok = body["access_token"]
    assert client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {tok}"}).status_code == 200


def test_settings_llm_is_login_only_and_admin_only_to_change(client):
    uid = _signup(client)
    admin = _admin_headers(client)
    client.post(f"/api/v1/admin/users/{uid}/approve", headers=admin)
    user = {"Authorization": f"Bearer {_token(client, USER_EMAIL, USER_PW).json()['access_token']}"}

    assert client.get("/api/v1/settings/llm").status_code == 401
    assert client.patch("/api/v1/settings/llm", json={"model": "x"}).status_code == 401
    assert client.get("/api/v1/settings/llm", headers=user).status_code == 200
    r = client.patch("/api/v1/settings/llm", headers=user, json={"model": "x"})
    assert r.status_code == 403
    assert r.json()["message"] == "Admin access required"
    current = client.get("/api/v1/settings/llm", headers=admin).json()["model"]
    assert client.patch(
        "/api/v1/settings/llm", headers=admin, json={"model": current}
    ).status_code == 200
