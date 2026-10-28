"""
Process blueprint: SIPOC / flow / swimlane, the follow-up loop, and the freeze gate.

Two properties matter most here and each has its own test: a project *with* diagrams cannot
generate until they are frozen, and a project *without* them generates exactly as it did
before the feature existed. The second is the one that would quietly regress.
"""

import io
import time
import zipfile

import pytest
from fastapi.testclient import TestClient

from app.db.session import init_db
from app.main import create_app

API = "/api/v1"


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
    # Render into the temp dir so the suite never writes to the repo's diagrams/ folder.
    monkeypatch.chdir(tmp_path)
    import app.core.config as cfg
    import app.db.session as sess

    cfg.get_settings.cache_clear()
    sess._engine = None
    sess._SessionLocal = None
    init_db()
    app = create_app()
    with TestClient(app) as c:
        yield c


def _headers(client: TestClient, email: str = "blueprint@example.com") -> dict[str, str]:
    client.post(API + "/auth/signup", json={"email": email, "password": "secret12", "name": "T"})
    admin = client.post(
        API + "/auth/login", json={"email": "admin@ac.com", "password": "1681149@sPk"}
    ).json()
    ah = {"Authorization": f"Bearer {admin['access_token']}"}
    users = client.get(API + "/admin/users", headers=ah).json()
    uid = next(u["id"] for u in users if u["email"] == email)
    client.post(f"{API}/admin/users/{uid}/approve", headers=ah)
    token = client.post(
        API + "/auth/login", json={"email": email, "password": "secret12"}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _project_with_context(client: TestClient, headers: dict[str, str], statement: str) -> str:
    pid = client.post(API + "/projects", json={"name": "Blueprint"}, headers=headers).json()["id"]
    client.post(f"{API}/projects/{pid}/path", json={"path": "docs"}, headers=headers)
    body = f"Product specification:\n{statement}\n"
    r = client.post(
        f"{API}/projects/{pid}/documents",
        data={"problem_statement": statement},
        files={"files": ("spec.txt", io.BytesIO(body.encode()), "text/plain")},
        headers=headers,
    )
    assert r.json()["state"] == "CONTEXT_READY", r.text
    return pid


def _fake_generate(monkeypatch):
    from app.core.state_machine import Platform
    from app.models.schemas import ProjectBrief
    from app.services import projects as projects_mod
    from app.services.deduction.engine import demo_plan

    async def _fake_deduce(brief: ProjectBrief, platform: Platform, **kwargs):
        _fake_generate.last_kwargs = kwargs
        return demo_plan(brief, platform)

    monkeypatch.setattr(projects_mod, "deduce_plan", _fake_deduce)


def test_demo_blueprint_renders_three_pngs(client: TestClient):
    headers = _headers(client)
    pid = _project_with_context(client, headers, "Loan application intake and approval")

    r = client.post(
        f"{API}/projects/{pid}/diagrams",
        params={"demo": True, "wait": True},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    view = r.json()
    assert view["version"] == 1
    assert view["frozen"] is False
    assert view["llm"] is False  # demo path never calls Bedrock
    assert {i["kind"] for i in view["images"]} == {"sipoc", "flow", "swimlane"}
    assert all(i["width"] > 0 and i["bytes"] > 0 for i in view["images"])
    # The three views agree because they are projections of one model, not three answers.
    assert len(view["flow"]["nodes"]) == len(view["model"]["steps"])
    assert len(view["swimlane"]["steps"]) == len(view["model"]["steps"])
    assert 5 <= len(view["sipoc"]["process"]) <= 7

    for kind in ("sipoc", "flow", "swimlane"):
        img = client.get(f"{API}/projects/{pid}/diagrams/{kind}.png", headers=headers)
        assert img.status_code == 200, kind
        assert img.headers["content-type"] == "image/png"
        assert img.content[:8] == b"\x89PNG\r\n\x1a\n"

    # And it is readable back without regenerating.
    again = client.get(f"{API}/projects/{pid}/diagrams", headers=headers)
    assert again.json()["version"] == 1


def test_background_draw_is_pollable_like_the_ui_does_it(client: TestClient):
    """The wait=false path the wizard drives: queue, poll the project, read the set."""
    headers = _headers(client, email="poll@example.com")
    pid = _project_with_context(client, headers, "Field service dispatch and closure")

    queued = client.post(
        f"{API}/projects/{pid}/diagrams", params={"demo": True, "wait": False}, headers=headers
    )
    assert queued.status_code == 200, queued.text
    # A ProjectStatus comes back, not a blueprint — the UI polls for the rest.
    assert queued.json()["id"] == pid

    # The wizard watches `diagram_version` rather than `progress`: progress is cleared at the
    # very end of the job, so a poll landing in the gap before the first update would read an
    # empty string and stop before anything had been drawn.
    for _ in range(80):
        status = client.get(f"{API}/projects/{pid}", headers=headers).json()
        assert not status.get("error"), status["error"]
        if (status.get("diagram_version") or 0) > 0:
            break
        time.sleep(0.05)
    else:
        pytest.fail("background blueprint never bumped diagram_version")

    assert status["has_diagrams"] is True
    assert status["diagrams_frozen"] is False
    assert not status.get("progress")

    view = client.get(f"{API}/projects/{pid}/diagrams", headers=headers).json()
    assert view["version"] == status["diagram_version"]
    png = client.get(f"{API}/projects/{pid}/diagrams/flow.png", headers=headers)
    assert png.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_pngs_carry_the_scale_the_viewer_needs(client: TestClient):
    """The PNG is drawn at 2×, so the API has to say so or the UI shows it double size."""
    headers = _headers(client, email="scale@example.com")
    pid = _project_with_context(client, headers, "Grant application review")
    view = client.post(
        f"{API}/projects/{pid}/diagrams", params={"demo": True, "wait": True}, headers=headers
    ).json()

    from app.services.diagrams.render import RENDER_SCALE

    for image in view["images"]:
        assert image["scale"] == RENDER_SCALE, image["kind"]
        # width is the true pixel width; width/scale is the size it is laid out to be read at.
        assert image["width"] % RENDER_SCALE == 0
        assert image["width"] / image["scale"] >= 360

    # Reading the metadata back off the cache (no re-render) must report the same scale, which
    # is why the scale is part of the cached filename rather than assumed.
    again = client.get(f"{API}/projects/{pid}/diagrams", headers=headers).json()
    assert [i["scale"] for i in again["images"]] == [i["scale"] for i in view["images"]]
    assert [i["width"] for i in again["images"]] == [i["width"] for i in view["images"]]


def test_edge_labels_are_kept_off_shapes_and_off_each_other():
    """A label's plate hides what is under it, so placement has to dodge, not just centre.

    Straight geometry, no API: an arrow whose midpoint lands inside a box must put its label
    somewhere else on the same arrow, and the next label must not land on the first.
    """
    from app.services.diagrams.render import _Canvas

    c = _Canvas(1200, 400)
    box = (500.0, 150.0, 200.0, 100.0)  # sits right on the midpoint of the arrow below
    c.obstacles.append(box)
    start, end = (100.0, 200.0), (1100.0, 200.0)

    def inside(pt, rect):
        x, y, w, h = rect
        return x <= pt[0] <= x + w and y <= pt[1] <= y + h

    assert inside((600.0, 200.0), box), "the test is pointless if the midpoint misses the box"
    first = c._label_point(start, end, "valid source", rad=0.0, label_t=0.5)
    assert not inside(first, box)
    assert start[0] < first[0] < end[0], "the label has to stay on its own arrow"

    # Placing it registers it, so a second arrow along the same line has to give way.
    c.arrow(start, end, label="valid source")
    second = c._label_point(start, end, "no source", rad=0.0, label_t=0.5)
    assert abs(second[0] - first[0]) > 20, "two labels must not stack in one spot"
    assert not inside(second, box)


def test_swimlane_handoffs_route_around_the_boxes():
    """Straight geometry: a handoff must travel in the reserved channels, never over a box.

    This is the fix for the arrow tangle — every non-adjacent handoff leaves its box sideways,
    runs vertically in a column gutter and horizontally along a lane boundary. `swimlane_route`
    is the corner list on its own so the rule can be asserted instead of eyeballed.
    """
    from app.services.diagrams.render import swimlane_route

    # Two boxes two columns apart, in different lanes, with a third box in between.
    src = (100.0, 100.0)  # exit point on the right edge of the source box
    dst = (700.0, 400.0)  # entry point on the left edge of the target box
    blocker = (300.0, 60.0, 254.0, 74.0)  # the middle column's box, on the source's own row
    route = swimlane_route(src, dst, channel_x1=280.0, channel_x2=660.0, channel_y=330.0)

    assert route[0] == src and route[-1] == dst
    # Manhattan: every leg is horizontal or vertical, so a route can be followed by eye.
    for a, b in zip(route, route[1:]):
        assert a[0] == b[0] or a[1] == b[1], f"diagonal leg {a}->{b}"

    def crosses(rect, a, b) -> bool:
        x, y, w, h = rect
        lo_x, hi_x = min(a[0], b[0]), max(a[0], b[0])
        lo_y, hi_y = min(a[1], b[1]), max(a[1], b[1])
        return lo_x <= x + w and hi_x >= x and lo_y <= y + h and hi_y >= y

    interior = list(zip(route, route[1:]))[1:-1]  # the stubs at each end touch their own box
    assert interior, "a cross-lane handoff needs channel legs, not a straight line"
    for a, b in interior:
        assert not crosses(blocker, a, b), f"leg {a}->{b} runs through the box in between"

    # Same column, or a lane boundary with no room: one vertical leg in the gutter, still
    # orthogonal, and still not a straight diagonal across the picture.
    tight = swimlane_route(src, dst, channel_x1=280.0, channel_x2=280.0, channel_y=None)
    for a, b in zip(tight, tight[1:]):
        assert a[0] == b[0] or a[1] == b[1]


def test_an_earlier_version_can_be_redrawn_from_the_change_history(
    client: TestClient, monkeypatch
):
    """Clicking a change-history entry in the UI asks for that version's PNGs.

    Old PNGs are not kept — the views behind them are — so this checks the endpoint renders
    v1 again after v2 replaced it, that it is a different picture, and that a version that
    never existed is a 404 rather than a 500.
    """
    headers = _headers(client, email="history@example.com")
    pid = _project_with_context(client, headers, "Insurance claim settlement")
    first = client.post(
        f"{API}/projects/{pid}/diagrams", params={"demo": True, "wait": True}, headers=headers
    ).json()
    assert first["version"] == 1
    v1_png = client.get(f"{API}/projects/{pid}/diagrams/flow.png", headers=headers).content

    from app.models.diagrams import ProcessStep
    from app.services.diagrams import build as build_mod

    async def _fake_revise(model, instruction, **kwargs):
        steps = [*model.steps, ProcessStep(id="s98", name="Confirm the payout", actor="Finance")]
        return model.model_copy(update={"steps": steps}), "Added a payout confirmation."

    monkeypatch.setattr(build_mod, "revise_process_model", _fake_revise)
    view = client.post(
        f"{API}/projects/{pid}/diagrams/followup",
        json={"instruction": "confirm the payout at the end"},
        params={"wait": True},
        headers=headers,
    ).json()
    assert view["version"] == 2
    # The UI only offers the entries it can draw, so the flag has to come back with them.
    assert all(r["viewable"] is True for r in view["revisions"])

    archived = client.get(
        f"{API}/projects/{pid}/diagrams/flow.png", params={"version": 1}, headers=headers
    )
    assert archived.status_code == 200, archived.text
    assert archived.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert "-v1" in archived.headers.get("content-disposition", "")
    current = client.get(f"{API}/projects/{pid}/diagrams/flow.png", headers=headers).content
    assert archived.content != current, "v1 and v2 must not render to the same picture"
    # Same version, same bytes — the render is deterministic, and v1 is what it was.
    assert archived.content == v1_png

    for kind in ("sipoc", "swimlane"):
        old = client.get(
            f"{API}/projects/{pid}/diagrams/{kind}.png", params={"version": 1}, headers=headers
        )
        assert old.status_code == 200, kind

    missing = client.get(
        f"{API}/projects/{pid}/diagrams/flow.png", params={"version": 99}, headers=headers
    )
    assert missing.status_code == 404


def test_followup_in_background_clears_a_stale_error(client: TestClient, monkeypatch):
    """The wizard's follow-up path, on a project whose previous attempt failed.

    The client polls the project and reads `error` as "this attempt failed", so a leftover
    message from the last try has to be cleared when the next one is queued — otherwise the
    retry is reported as broken on its first tick without ever having run.
    """
    headers = _headers(client, email="retry@example.com")
    pid = _project_with_context(client, headers, "Insurance renewal quoting")
    client.post(
        f"{API}/projects/{pid}/diagrams", params={"demo": True, "wait": True}, headers=headers
    )

    from app.db.session import repo

    repo.update(pid, error="Process blueprint failed (Bedrock/LiteLLM): expired token")
    assert client.get(f"{API}/projects/{pid}", headers=headers).json()["error"]

    from app.models.diagrams import ProcessStep
    from app.services.diagrams import build as build_mod

    async def _fake_revise(model, instruction, **kwargs):
        steps = [*model.steps, ProcessStep(id="s98", name="Second-line review", actor="Reviewer")]
        return model.model_copy(update={"steps": steps}), "Added a second-line review."

    monkeypatch.setattr(build_mod, "revise_process_model", _fake_revise)

    queued = client.post(
        f"{API}/projects/{pid}/diagrams/followup",
        json={"instruction": "add a second-line review"},
        params={"wait": False},
        headers=headers,
    )
    assert queued.status_code == 200, queued.text
    assert not queued.json()["error"], "a queued follow-up must not report the previous failure"

    # Longer budget than the first-draw poll: only the model revision is stubbed here, so the
    # three view layouts still go out to Bedrock and fall back to local derivation, which costs
    # a connection timeout each when there are no credentials.
    for _ in range(300):
        status = client.get(f"{API}/projects/{pid}", headers=headers).json()
        assert not status.get("error"), status["error"]
        if (status.get("diagram_version") or 0) >= 2:
            break
        time.sleep(0.1)
    else:
        pytest.fail("background follow-up never bumped diagram_version")

    view = client.get(f"{API}/projects/{pid}/diagrams", headers=headers).json()
    assert view["version"] == 2
    assert any(s["id"] == "s98" for s in view["model"]["steps"])
    assert any(s["id"] == "s98" for s in view["swimlane"]["steps"])


def test_generate_blocked_until_blueprint_frozen(client: TestClient, monkeypatch):
    _fake_generate(monkeypatch)
    headers = _headers(client, email="gate@example.com")
    pid = _project_with_context(client, headers, "Claims triage and settlement")
    client.post(
        f"{API}/projects/{pid}/diagrams", params={"demo": True, "wait": True}, headers=headers
    )
    client.post(f"{API}/projects/{pid}/platform", json={"platform": "cursor"}, headers=headers)

    blocked = client.post(f"{API}/projects/{pid}/generate", params={"wait": True}, headers=headers)
    assert blocked.status_code == 409
    assert "freeze" in blocked.text.lower()

    frozen = client.post(f"{API}/projects/{pid}/diagrams/freeze", headers=headers)
    assert frozen.status_code == 200
    assert frozen.json()["frozen"] is True
    assert frozen.json()["frozen_at"]

    ok = client.post(f"{API}/projects/{pid}/generate", params={"wait": True}, headers=headers)
    assert ok.status_code == 200, ok.text
    assert ok.json()["state"] == "READY_FOR_REVIEW"
    # The frozen process reached the blueprint prompt.
    context = _fake_generate.last_kwargs["process_context"]
    assert "Approved process blueprint" in context
    assert "Submit request" in context


def test_project_without_blueprint_generates_unchanged(client: TestClient, monkeypatch):
    """The pre-existing flow. No diagrams means no gate and no extra prompt context."""
    _fake_generate(monkeypatch)
    headers = _headers(client, email="legacy@example.com")
    pid = _project_with_context(client, headers, "Vendor onboarding checks")
    client.post(f"{API}/projects/{pid}/platform", json={"platform": "claude_code"}, headers=headers)

    status = client.get(f"{API}/projects/{pid}", headers=headers).json()
    assert status["has_diagrams"] is False
    assert status["diagrams_frozen"] is False

    r = client.post(f"{API}/projects/{pid}/generate", params={"wait": True}, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "READY_FOR_REVIEW"
    assert _fake_generate.last_kwargs["process_context"] == ""

    assert client.get(f"{API}/projects/{pid}/diagrams", headers=headers).json() is None


def test_followup_bumps_version_and_unfreezes(client: TestClient, monkeypatch):
    headers = _headers(client, email="followup@example.com")
    pid = _project_with_context(client, headers, "Purchase order approval")
    client.post(
        f"{API}/projects/{pid}/diagrams", params={"demo": True, "wait": True}, headers=headers
    )
    client.post(f"{API}/projects/{pid}/diagrams/freeze", headers=headers)

    # Frozen means settled: a change has to be an explicit reopening, because the generated
    # agents follow whatever is frozen.
    denied = client.post(
        f"{API}/projects/{pid}/diagrams/followup",
        json={"instruction": "add a fraud check"},
        params={"wait": True},
        headers=headers,
    )
    assert denied.status_code == 409

    client.post(f"{API}/projects/{pid}/diagrams/unfreeze", headers=headers)

    from app.models.diagrams import ProcessStep
    from app.services.diagrams import build as build_mod

    async def _fake_revise(model, instruction, **kwargs):
        steps = [*model.steps, ProcessStep(id="s99", name="Run fraud check", actor="Reviewer")]
        return model.model_copy(update={"steps": steps}), "Added a fraud check as s99."

    monkeypatch.setattr(build_mod, "revise_process_model", _fake_revise)

    r = client.post(
        f"{API}/projects/{pid}/diagrams/followup",
        json={"instruction": "add a fraud check before approval"},
        params={"wait": True},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    view = r.json()
    assert view["version"] == 2
    assert view["frozen"] is False  # a revision reopens by construction
    assert any(s["id"] == "s99" for s in view["model"]["steps"])
    assert view["revisions"][-1]["summary"] == "Added a fraud check as s99."
    assert view["revisions"][-1]["instruction"].startswith("add a fraud check")
    # All three views were redrawn against the new model, not left on the old one.
    assert any(n["id"] == "s99" for n in view["flow"]["nodes"])
    assert any(s["id"] == "s99" for s in view["swimlane"]["steps"])


def test_export_ships_the_approved_pngs_and_documents_them(client: TestClient, monkeypatch):
    _fake_generate(monkeypatch)
    headers = _headers(client, email="export@example.com")
    pid = _project_with_context(client, headers, "Invoice matching and dispute handling")
    client.post(
        f"{API}/projects/{pid}/diagrams", params={"demo": True, "wait": True}, headers=headers
    )
    client.post(f"{API}/projects/{pid}/diagrams/freeze", headers=headers)
    client.post(f"{API}/projects/{pid}/platform", json={"platform": "cursor"}, headers=headers)
    client.post(f"{API}/projects/{pid}/generate", params={"wait": True}, headers=headers)

    exported = client.post(f"{API}/projects/{pid}/export", json={"mode": "zip"}, headers=headers)
    assert exported.status_code == 200, exported.text
    listed = exported.json()["files"]
    # The version is in every filename, and the folder holds nothing but the pictures.
    assert "docs/diagrams/sipoc-v1.png" in listed
    assert "docs/diagrams/process-flow-v1.png" in listed
    assert "docs/diagrams/swimlane-v1.png" in listed
    assert not [p for p in listed if p.startswith("docs/") and not p.endswith(".png")]

    # The file tree the UI browses is the same set, so a PNG is selectable there.
    preview = client.get(f"{API}/projects/{pid}/export/preview", headers=headers)
    assert "docs/diagrams/swimlane-v1.png" in preview.json()["files"]
    assert "docs/diagrams/swimlane-v1.png" in preview.json()["images"]
    # Listed, but never carried as text — the client fetches the bytes when it opens one.
    assert "docs/diagrams/swimlane-v1.png" not in preview.json()["contents"]

    blob = client.get(f"{API}/projects/{pid}/export/download", headers=headers)
    with zipfile.ZipFile(io.BytesIO(blob.content)) as zf:
        names = zf.namelist()
        png = next(n for n in names if n.endswith("docs/diagrams/swimlane-v1.png"))
        assert zf.read(png)[:8] == b"\x89PNG\r\n\x1a\n"
        # The workspace README is where the folder and each image's purpose are explained.
        readme = zf.read(next(n for n in names if n.endswith("/README.md"))).decode()
        assert "docs/diagrams/" in readme
        assert "docs/diagrams/sipoc-v1.png" in readme
        assert "scope on one page" in readme
        assert "the order of work" in readme
        assert "who owns what" in readme


def test_deleting_the_blueprint_restores_the_plain_flow(client: TestClient, monkeypatch):
    _fake_generate(monkeypatch)
    headers = _headers(client, email="discard@example.com")
    pid = _project_with_context(client, headers, "Warranty claim intake")
    client.post(
        f"{API}/projects/{pid}/diagrams", params={"demo": True, "wait": True}, headers=headers
    )
    client.post(f"{API}/projects/{pid}/platform", json={"platform": "windsurf"}, headers=headers)
    assert (
        client.post(f"{API}/projects/{pid}/generate", params={"wait": True}, headers=headers).status_code
        == 409
    )

    dropped = client.delete(f"{API}/projects/{pid}/diagrams", headers=headers)
    assert dropped.status_code == 200
    assert dropped.json()["has_diagrams"] is False

    ok = client.post(f"{API}/projects/{pid}/generate", params={"wait": True}, headers=headers)
    assert ok.status_code == 200, ok.text
    assert _fake_generate.last_kwargs["process_context"] == ""


# ── Text that does not fit ──────────────────────────────────────────────────────
#
# Every string in a diagram is either wrapped or cut to a *measured* width, and a cut is
# always marked. The bug these guard against shipped: the scope was sliced at 150 characters
# and drawn as one line the canvas edge cut again, so the sentence ended mid-word with nothing
# to say it had been shortened.


def test_the_scope_wraps_instead_of_being_cut_off():
    from app.services.diagrams.render import _header_height, _header_lines

    scope = (
        "Starts when an administrator uploads a document or a user submits a query. Ends when "
        "the answer is delivered with source references, or when a query is rejected because no "
        "indexed document can support an answer at the required confidence threshold."
    )
    narrow = _header_lines(scope, 900.0)
    wide = _header_lines(scope, 2600.0)
    # Whatever the width, the whole sentence survives — only the line breaks move.
    assert " ".join(narrow) == scope
    assert " ".join(wide) == scope
    assert len(narrow) > len(wide)
    # And a scope that needs more lines pushes the body down instead of being drawn over it.
    assert _header_height(scope, 900.0) > _header_height(scope, 2600.0)
    assert _header_height("", 900.0) < _header_height(scope, 900.0)


def test_a_cut_label_is_marked_once_not_twice():
    from app.services.diagrams.build import _cap
    from app.services.diagrams.render import _clip, _wrap

    long = "Validate the submitted request against the current retention schedule and policy"
    capped = _cap(long, 52)
    # Cut between words, marked, and no longer than asked for.
    assert capped.endswith("…") and " " in capped
    assert len(capped) <= 53
    assert long.startswith(capped[:-1].rstrip())

    # An already-shortened string wrapped again keeps exactly one ellipsis.
    for line in _wrap(capped, 160.0, 9.4, "bold", max_lines=2):
        assert "……" not in line
    assert "……" not in _clip(capped, 90.0, 7.6)
    assert _wrap(capped, 160.0, 9.4, "bold", max_lines=2)[-1].endswith("…")

    # A short label is left completely alone.
    assert _cap("Record decision", 52) == "Record decision"
    assert _clip("Record decision", 400.0, 7.6) == "Record decision"
