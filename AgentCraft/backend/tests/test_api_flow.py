"""API integration tests (authenticated + admin approval)."""

import io

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.db.session import init_db


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
    import app.db.session as sess
    import app.core.config as cfg

    cfg.get_settings.cache_clear()
    sess._engine = None
    sess._SessionLocal = None
    init_db()
    app = create_app()
    with TestClient(app) as c:
        yield c


def _admin_headers(client: TestClient) -> dict[str, str]:
    r = client.post(
        "/api/v1/auth/login",
        json={"email": "admin@ac.com", "password": "1681149@sPk"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["user"]["role"] == "admin"
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _auth_headers(client: TestClient, email: str = "tester@example.com") -> dict[str, str]:
    """Signup (pending) → admin approve → login."""
    r = client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": "secret12", "name": "Tester"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "pending"
    assert "access_token" not in r.json()

    # Pending user cannot log in yet
    blocked = client.post(
        "/api/v1/auth/login", json={"email": email, "password": "secret12"}
    )
    assert blocked.status_code == 403

    admin = _admin_headers(client)
    users = client.get("/api/v1/admin/users", headers=admin).json()
    pending = next(u for u in users if u["email"] == email)
    assert pending["status"] == "pending"
    apr = client.post(f"/api/v1/admin/users/{pending['id']}/approve", headers=admin)
    assert apr.status_code == 200
    assert apr.json()["status"] == "approved"

    r = client.post("/api/v1/auth/login", json={"email": email, "password": "secret12"})
    assert r.status_code == 200, r.text
    token = r.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _submit_docs(
    client: TestClient,
    pid: str,
    statement: str,
    headers: dict[str, str],
):
    """Upload aligned spec file + problem statement (required by documents endpoint)."""
    spec_body = f"Product specification:\n{statement}\n"
    return client.post(
        f"/api/v1/projects/{pid}/documents",
        data={"problem_statement": statement},
        files={"files": ("spec.txt", io.BytesIO(spec_body.encode("utf-8")), "text/plain")},
        headers=headers,
    )


def test_docs_path_to_export(client: TestClient, monkeypatch):
    from app.services import projects as projects_mod
    from app.services.deduction.engine import demo_plan
    from app.core.state_machine import Platform
    from app.models.schemas import ProjectBrief

    async def _fake_deduce(brief: ProjectBrief, platform: Platform, **kwargs):
        return demo_plan(brief, platform)

    monkeypatch.setattr(projects_mod, "deduce_plan", _fake_deduce)
    headers = _auth_headers(client)

    r = client.post("/api/v1/projects", json={"name": "PayAPI"}, headers=headers)
    assert r.status_code == 200
    pid = r.json()["id"]
    assert r.json()["state"] == "CREATED"

    r = client.post(f"/api/v1/projects/{pid}/path", json={"path": "docs"}, headers=headers)
    assert r.json()["state"] == "AWAITING_DOCS"

    r = _submit_docs(
        client,
        pid,
        "Build a payments API with ledger and webhooks",
        headers,
    )
    assert r.status_code == 200
    assert r.json()["state"] == "CONTEXT_READY"

    r = client.post(
        f"/api/v1/projects/{pid}/platform", json={"platform": "cursor"}, headers=headers
    )
    assert r.json()["state"] == "PLATFORM_SELECTED"

    r = client.post(
        f"/api/v1/projects/{pid}/generate", params={"wait": True}, headers=headers
    )
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "READY_FOR_REVIEW"
    assert len(body["plan"]["agents"]) >= 4

    r = client.post(f"/api/v1/projects/{pid}/export", json={"mode": "zip"}, headers=headers)
    assert r.status_code == 200
    assert r.json()["files"]

    r = client.get(f"/api/v1/projects/{pid}/export/download", headers=headers)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/zip")

    r = client.get("/api/v1/projects/sessions", headers=headers)
    assert r.status_code == 200
    sess = next(s for s in r.json() if s["id"] == pid)
    assert sess["rule_count"] >= 1  # Cursor exports rules

    r = client.delete(f"/api/v1/projects/{pid}", headers=headers)
    assert r.status_code == 204
    r = client.get("/api/v1/projects/sessions", headers=headers)
    assert all(s["id"] != pid for s in r.json())


def test_claude_session_exports_rules(client: TestClient, monkeypatch):
    from app.services import projects as projects_mod
    from app.services.deduction.engine import demo_plan
    from app.core.state_machine import Platform
    from app.models.schemas import ProjectBrief

    async def _fake_deduce(brief: ProjectBrief, platform: Platform, **kwargs):
        return demo_plan(brief, platform)

    monkeypatch.setattr(projects_mod, "deduce_plan", _fake_deduce)
    headers = _auth_headers(client, email="claude-user@example.com")
    pid = client.post("/api/v1/projects", json={"name": "MoM"}, headers=headers).json()["id"]
    client.post(f"/api/v1/projects/{pid}/path", json={"path": "docs"}, headers=headers)
    _submit_docs(
        client,
        pid,
        "AI minutes of meeting with action items",
        headers,
    )
    client.post(f"/api/v1/projects/{pid}/platform", json={"platform": "claude_code"}, headers=headers)
    r = client.post(f"/api/v1/projects/{pid}/generate", params={"wait": True}, headers=headers)
    assert r.status_code == 200
    assert r.json()["plan"]["rules"]
    files = client.get(f"/api/v1/projects/{pid}/export/preview", headers=headers).json()["files"]
    assert any(f.startswith(".claude/agents/") for f in files)
    # Rules land under .claude/rules as plain `.md` — same folder shape as agents/skills.
    rule_files = [f for f in files if f.startswith(".claude/rules/")]
    assert rule_files
    assert all(f.endswith(".md") for f in rule_files)
    sess = client.get("/api/v1/projects/sessions", headers=headers).json()[0]
    assert sess["rule_count"] >= 1
    assert sess["agent_count"] >= 1
    assert sess["skill_count"] >= 1


@pytest.mark.parametrize("ide", ["claude_code", "cursor", "windsurf", "github_copilot"])
def test_existing_project_with_no_rules_gains_them_on_read(
    client: TestClient, monkeypatch, ide: str
):
    """
    A plan can carry `rules: []` — always for a Claude project generated before rules
    existed, and on any IDE when the LLM returned none. The Review step's Rules tab and
    the sessions rule count read the plan, so the backfill has to be persisted, not only
    rendered into the export, or the UI shows nothing beside a populated rules folder.
    """
    from app.db.session import repo
    from app.services import projects as projects_mod
    from app.services.deduction.engine import demo_plan
    from app.core.state_machine import Platform
    from app.models.schemas import ProjectBrief

    async def _fake_deduce(brief: ProjectBrief, platform: Platform, **kwargs):
        return demo_plan(brief, platform)

    monkeypatch.setattr(projects_mod, "deduce_plan", _fake_deduce)
    headers = _auth_headers(client, email=f"legacy-{ide}@example.com")
    pid = client.post("/api/v1/projects", json={"name": "Legacy"}, headers=headers).json()["id"]
    client.post(f"/api/v1/projects/{pid}/path", json={"path": "docs"}, headers=headers)
    _submit_docs(client, pid, "Build a payments API with a ledger", headers)
    client.post(f"/api/v1/projects/{pid}/platform", json={"platform": ide}, headers=headers)
    client.post(f"/api/v1/projects/{pid}/generate", params={"wait": True}, headers=headers)

    # Simulate a plan that reached us with no rules.
    stored = repo.get(pid)
    repo.update(pid, plan=stored.plan.model_copy(update={"rules": []}))
    assert repo.get(pid).plan.rules == []

    body = client.get(f"/api/v1/projects/{pid}", headers=headers).json()
    assert body["plan"]["rules"], f"reading a {ide} project must backfill its rules"
    names = [r["name"] for r in body["plan"]["rules"]]
    assert "architecture" in names and "secure-coding-vapt" in names
    # Persisted, so the next read is consistent rather than recomputed each time.
    assert repo.get(pid).plan.rules
    sess = client.get("/api/v1/projects/sessions", headers=headers).json()[0]
    assert sess["rule_count"] == len(names), "the count must match the plan the UI reads"

    # …and the count must also match what the export actually writes.
    files = client.get(f"/api/v1/projects/{pid}/export/preview", headers=headers).json()["files"]
    folder = {"claude_code": ".claude/rules/", "cursor": ".cursor/rules/", "windsurf": ".windsurf/rules/", "github_copilot": ".github/instructions/"}[ide]
    assert len([f for f in files if f.startswith(folder)]) == len(names)


def test_illegal_generate_before_platform(client: TestClient):
    headers = _auth_headers(client, email="early@example.com")
    pid = client.post("/api/v1/projects", json={"name": "X"}, headers=headers).json()["id"]
    client.post(f"/api/v1/projects/{pid}/path", json={"path": "docs"}, headers=headers)
    _submit_docs(client, pid, "Something meaningful enough", headers)
    r = client.post(f"/api/v1/projects/{pid}/generate", headers=headers)
    assert r.status_code == 409


def test_signup_requires_admin_approval(client: TestClient):
    r = client.post(
        "/api/v1/auth/signup",
        json={"email": "a@b.com", "password": "secret12", "name": "Ada"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "pending"

    r = client.post("/api/v1/auth/login", json={"email": "a@b.com", "password": "secret12"})
    assert r.status_code == 403

    admin = _admin_headers(client)
    users = client.get("/api/v1/admin/users", headers=admin).json()
    ada = next(u for u in users if u["email"] == "a@b.com")
    assert ada["session_count"] == 0

    r = client.post(f"/api/v1/admin/users/{ada['id']}/approve", headers=admin)
    assert r.status_code == 200
    assert r.json()["status"] == "approved"

    r = client.post("/api/v1/auth/login", json={"email": "a@b.com", "password": "secret12"})
    assert r.status_code == 200
    token = r.json()["access_token"]
    r = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json()["email"] == "a@b.com"
    assert r.json()["status"] == "approved"


def test_admin_sees_user_sessions(client: TestClient):
    headers = _auth_headers(client, email="sess@example.com")
    client.post("/api/v1/projects", json={"name": "Alpha Workspace"}, headers=headers)
    client.post("/api/v1/projects", json={"name": "Beta Workspace"}, headers=headers)

    admin = _admin_headers(client)
    users = client.get("/api/v1/admin/users", headers=admin).json()
    user = next(u for u in users if u["email"] == "sess@example.com")
    assert user["session_count"] == 2
    titles = {s["name"] for s in user["sessions"]}
    assert "Alpha Workspace" in titles
    assert "Beta Workspace" in titles
    # Artifact name fields present (may be empty before generate)
    assert "agent_names" in user["sessions"][0]
    assert "skill_names" in user["sessions"][0]
    assert "rule_names" in user["sessions"][0]

    # Non-admin cannot call admin API
    r = client.get("/api/v1/admin/users", headers=headers)
    assert r.status_code == 403
