"""Super admin deletes / restores / purges users; deleted people are told why."""

import io

import pytest
from fastapi.testclient import TestClient

from app.db.session import init_db
from app.main import create_app


@pytest.fixture(autouse=True)
def _skip_document_semantic_match(monkeypatch):
    async def _always_ok(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        "app.services.parser.match.assert_statement_matches_documents",
        _always_ok,
    )


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "t.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("JWT_SECRET", "test-secret-at-least-thirty-two-chars!!")
    monkeypatch.setenv("SUPER_ADMIN_EMAIL", "admin@ac.com")
    monkeypatch.setenv("SUPER_ADMIN_PASSWORD", "1681149@sPk")
    import app.core.config as cfg
    import app.db.session as sess

    cfg.get_settings.cache_clear()
    sess._engine = None
    sess._SessionLocal = None
    init_db()
    app = create_app()
    with TestClient(app) as c:
        yield c


def _admin(client: TestClient) -> dict[str, str]:
    r = client.post(
        "/api/v1/auth/login", json={"email": "admin@ac.com", "password": "1681149@sPk"}
    )
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _approved_user(client: TestClient, email: str = "deletable@example.com") -> tuple[str, dict]:
    """Signup → admin approve → login. Returns (user_id, auth headers)."""
    r = client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": "secret12", "name": "Deletable"},
    )
    assert r.status_code == 200, r.text
    admin = _admin(client)
    users = client.get("/api/v1/admin/users", headers=admin).json()
    uid = next(u["id"] for u in users if u["email"] == email)
    assert client.post(f"/api/v1/admin/users/{uid}/approve", headers=admin).status_code == 200
    r = client.post("/api/v1/auth/login", json={"email": email, "password": "secret12"})
    assert r.status_code == 200, r.text
    return uid, {"Authorization": f"Bearer {r.json()['access_token']}"}


def _with_one_project(client: TestClient, headers: dict) -> str:
    """Give a user a project with an uploaded document. Returns the project id."""
    pid = client.post("/api/v1/projects", json={"name": "Workspace"}, headers=headers).json()["id"]
    client.post(f"/api/v1/projects/{pid}/path", json={"path": "docs"}, headers=headers)
    statement = "Build a fleet telematics dashboard with alerts and maps."
    client.post(
        f"/api/v1/projects/{pid}/documents",
        data={"problem_statement": statement},
        files={"files": ("spec.txt", io.BytesIO(statement.encode()), "text/plain")},
        headers=headers,
    )
    return pid


def _find(rows: list[dict], uid: str) -> dict | None:
    return next((u for u in rows if u["id"] == uid), None)


def test_admin_deletes_an_approved_user(client: TestClient):
    """The screenshot's gap: approved accounts had no delete action at all."""
    uid, _ = _approved_user(client)
    admin = _admin(client)

    r = client.delete(f"/api/v1/admin/users/{uid}", headers=admin)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["email"] == "deletable@example.com"
    assert body["deleted_sessions"] == 0
    assert "deleted" in body["message"].lower()


def test_deleted_user_stays_visible_to_the_admin(client: TestClient):
    """Otherwise the admin cannot see who was removed, let alone restore them."""
    uid, _ = _approved_user(client, "visible@example.com")
    admin = _admin(client)
    client.delete(f"/api/v1/admin/users/{uid}", headers=admin)

    row = _find(client.get("/api/v1/admin/users", headers=admin).json(), uid)
    assert row is not None, "deleted user vanished from the admin list"
    assert row["status"] == "deleted"
    assert row["previous_status"] == "approved"
    assert row["restorable"] is True
    assert row["deleted_by"] == "admin@ac.com"
    assert row["deleted_at"]


def test_delete_keeps_the_workspaces_for_restore(client: TestClient):
    """Destroying them would make restore worthless — they are held, not deleted."""
    uid, headers = _approved_user(client, "owner@example.com")
    pid = _with_one_project(client, headers)

    admin = _admin(client)
    r = client.delete(f"/api/v1/admin/users/{uid}", headers=admin)
    assert r.status_code == 200, r.text
    assert r.json()["deleted_sessions"] == 1

    row = _find(client.get("/api/v1/admin/users", headers=admin).json(), uid)
    assert row["session_count"] == 1
    assert row["sessions"][0]["id"] == pid


def test_restore_brings_back_the_account_and_its_workspaces(client: TestClient):
    uid, headers = _approved_user(client, "comeback@example.com")
    pid = _with_one_project(client, headers)
    admin = _admin(client)
    client.delete(f"/api/v1/admin/users/{uid}", headers=admin)

    r = client.post(f"/api/v1/admin/users/{uid}/restore", headers=admin)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["restored_sessions"] == 1
    # Restored to the status held at deletion, not back to pending.
    assert body["user"]["status"] == "approved"
    assert body["user"]["id"] == uid

    # Same password as before — the hash was archived, never reset.
    r = client.post(
        "/api/v1/auth/login", json={"email": "comeback@example.com", "password": "secret12"}
    )
    assert r.status_code == 200, r.text
    restored = {"Authorization": f"Bearer {r.json()['access_token']}"}
    # And their workspace opens again, with its documents.
    r = client.get(f"/api/v1/projects/{pid}", headers=restored)
    assert r.status_code == 200, r.text
    assert [d["filename"] for d in r.json()["brief"]["documents"]] == ["spec.txt"]


def test_restore_puts_a_pending_user_back_as_pending(client: TestClient):
    """Restore must not silently grant access the account never had."""
    client.post(
        "/api/v1/auth/signup",
        json={"email": "stillwaiting@example.com", "password": "secret12", "name": "Waiting"},
    )
    admin = _admin(client)
    uid = next(
        u["id"]
        for u in client.get("/api/v1/admin/users", headers=admin).json()
        if u["email"] == "stillwaiting@example.com"
    )
    client.delete(f"/api/v1/admin/users/{uid}", headers=admin)

    r = client.post(f"/api/v1/admin/users/{uid}/restore", headers=admin)
    assert r.status_code == 200, r.text
    assert r.json()["user"]["status"] == "pending"

    r = client.post(
        "/api/v1/auth/login", json={"email": "stillwaiting@example.com", "password": "secret12"}
    )
    assert r.status_code == 403
    assert "waiting for admin approval" in r.json()["message"].lower()


def test_restore_refuses_when_the_email_was_taken_again(client: TestClient):
    """A new signup owns the address now — restoring would collide on it."""
    uid, _ = _approved_user(client, "reused@example.com")
    admin = _admin(client)
    client.delete(f"/api/v1/admin/users/{uid}", headers=admin)
    client.post(
        "/api/v1/auth/signup",
        json={"email": "reused@example.com", "password": "other123", "name": "New Owner"},
    )

    r = client.post(f"/api/v1/admin/users/{uid}/restore", headers=admin)
    # The signup cleared the archive, so there is nothing left to restore.
    assert r.status_code == 404, r.text


def test_restore_unknown_id_is_404(client: TestClient):
    r = client.post("/api/v1/admin/users/nope/restore", headers=_admin(client))
    assert r.status_code == 404


def test_restore_requires_admin(client: TestClient):
    uid, headers = _approved_user(client, "notadmin@example.com")
    r = client.post(f"/api/v1/admin/users/{uid}/restore", headers=headers)
    assert r.status_code == 403


def test_purge_erases_the_account_and_its_workspaces(client: TestClient):
    uid, headers = _approved_user(client, "erased@example.com")
    pid = _with_one_project(client, headers)
    admin = _admin(client)
    client.delete(f"/api/v1/admin/users/{uid}", headers=admin)

    r = client.delete(f"/api/v1/admin/users/{uid}/purge", headers=admin)
    assert r.status_code == 200, r.text
    assert r.json()["purged_sessions"] == 1

    # Gone from the list, unrestorable, and the project row is gone too.
    assert _find(client.get("/api/v1/admin/users", headers=admin).json(), uid) is None
    assert client.post(f"/api/v1/admin/users/{uid}/restore", headers=admin).status_code == 404
    assert client.get(f"/api/v1/projects/{pid}", headers=admin).status_code == 404

    # No deletion notice any more — the address is simply unknown.
    r = client.post(
        "/api/v1/auth/login", json={"email": "erased@example.com", "password": "secret12"}
    )
    assert r.status_code == 401
    assert r.json()["message"] == "Invalid email or password"


def test_purge_refuses_a_live_account(client: TestClient):
    """Purge only applies to the archive — a live user must be deleted first."""
    uid, _ = _approved_user(client, "alive@example.com")
    r = client.delete(f"/api/v1/admin/users/{uid}/purge", headers=_admin(client))
    assert r.status_code == 404


def test_purge_requires_admin(client: TestClient):
    uid, headers = _approved_user(client, "purger@example.com")
    admin = _admin(client)
    client.delete(f"/api/v1/admin/users/{uid}", headers=admin)
    # The account is gone, so use a second user's token to prove the guard holds.
    _, other = _approved_user(client, "other@example.com")
    r = client.delete(f"/api/v1/admin/users/{uid}/purge", headers=other)
    assert r.status_code == 403


def test_deleted_user_login_says_the_admin_deleted_the_account(client: TestClient):
    uid, _ = _approved_user(client, "gone@example.com")
    client.delete(f"/api/v1/admin/users/{uid}", headers=_admin(client))

    r = client.post("/api/v1/auth/login", json={"email": "gone@example.com", "password": "secret12"})
    assert r.status_code == 403, r.text
    message = r.json()["message"]
    assert "deleted by the admin" in message.lower()
    # Not the misleading generic error a missing row used to produce.
    assert "invalid email or password" not in message.lower()


def test_existing_token_of_a_deleted_user_is_told_why(client: TestClient):
    """A JWT issued before the delete still decodes — the UI needs a reason to log out."""
    uid, headers = _approved_user(client, "stale@example.com")
    client.delete(f"/api/v1/admin/users/{uid}", headers=_admin(client))

    r = client.get("/api/v1/projects/sessions", headers=headers)
    assert r.status_code == 403, r.text
    assert "deleted by the admin" in r.json()["message"].lower()


def test_unknown_email_still_gets_the_generic_error(client: TestClient):
    """Only deleted accounts get the explanation — no probing for who existed."""
    r = client.post("/api/v1/auth/login", json={"email": "nobody@example.com", "password": "x"})
    assert r.status_code == 401
    assert r.json()["message"] == "Invalid email or password"


def test_signup_again_clears_the_deleted_notice(client: TestClient):
    """Re-signing up supersedes the archive, else the new account looks deleted."""
    uid, headers = _approved_user(client, "returning@example.com")
    pid = _with_one_project(client, headers)
    admin = _admin(client)
    client.delete(f"/api/v1/admin/users/{uid}", headers=admin)

    r = client.post(
        "/api/v1/auth/signup",
        json={"email": "returning@example.com", "password": "secret12", "name": "Back"},
    )
    assert r.status_code == 200, r.text

    # Pending again — the approval gate, not the deletion message.
    r = client.post(
        "/api/v1/auth/login", json={"email": "returning@example.com", "password": "secret12"}
    )
    assert r.status_code == 403
    assert "waiting for admin approval" in r.json()["message"].lower()

    # The old archive and its held workspace go with it: that id can never come back,
    # so keeping the project would leave a row nothing can ever reach.
    rows = client.get("/api/v1/admin/users", headers=admin).json()
    assert _find(rows, uid) is None
    assert client.get(f"/api/v1/projects/{pid}", headers=admin).status_code == 404


def test_super_admin_cannot_delete_itself(client: TestClient):
    admin = _admin(client)
    me = client.get("/api/v1/auth/me", headers=admin).json()

    r = client.delete(f"/api/v1/admin/users/{me['id']}", headers=admin)
    assert r.status_code == 400
    assert "cannot be deleted" in r.json()["message"].lower()
    # Still able to authenticate afterwards.
    assert client.get("/api/v1/admin/users", headers=admin).status_code == 200


def test_delete_requires_admin(client: TestClient):
    uid, headers = _approved_user(client, "regular@example.com")

    r = client.delete(f"/api/v1/admin/users/{uid}", headers=headers)
    assert r.status_code == 403
    assert "admin access required" in r.json()["message"].lower()


def test_delete_unknown_user_is_404(client: TestClient):
    r = client.delete("/api/v1/admin/users/does-not-exist", headers=_admin(client))
    assert r.status_code == 404


def test_delete_a_pending_user(client: TestClient):
    """Delete is not limited to approved accounts."""
    client.post(
        "/api/v1/auth/signup",
        json={"email": "pending@example.com", "password": "secret12", "name": "Waiting"},
    )
    admin = _admin(client)
    uid = next(
        u["id"]
        for u in client.get("/api/v1/admin/users", headers=admin).json()
        if u["email"] == "pending@example.com"
    )

    assert client.delete(f"/api/v1/admin/users/{uid}", headers=admin).status_code == 200
    r = client.post(
        "/api/v1/auth/login", json={"email": "pending@example.com", "password": "secret12"}
    )
    assert r.status_code == 403
    assert "deleted by the admin" in r.json()["message"].lower()
