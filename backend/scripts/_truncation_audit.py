"""Ad-hoc: list every way the six views can lose a sentence, with the call site that lost it.

Not a test — an instrument. It watches three separate ways the same defect happens, because each
of the first two was once found by luck instead:

1. **The renderer shortens.** Both renderers mark every cut with a "…" (see `_wrap` and `_clip`
   in `render.py`), so a truncation is always *visible* to a reader; what is not visible is
   whether the bound was a sensible one, and eyeballing six PNGs finds a clipped line only if the
   eye happens to land on it. Reading the list is how you tell the two cases apart —

     * a multi-sentence *description* bounded on a word boundary is the design working;
     * a **label**, a box note, or a `·`-joined **list** losing items is a defect — raise the
       bound or give the box more room, because those heights are all measured from the wrapped
       text.

2. **Something upstream shortened it first.** `_wrap` cannot see one of those: the text handed to
   it already ends in somebody else's ellipsis, so the wrap looks clean. `_s`, `_prose` and
   `_text` are watched directly for that reason — a `_s(scaling, 90)` once turned "fails over
   automatically across AZs" into "fails over automatically", dropping the answer.

3. **Something was drawn over it.** An edge label sits on an opaque plate. Two labels in one
   place is not one of them slightly obscured but *both* lost, and a plate through the middle of a
   box's sentence reads exactly like a truncated one. So every placed label is checked against
   every string on the page, and against how far it ended up from the arrow it annotates.

Exits non-zero if 2 or 3 found anything, or if a stored view description ends mid-sentence. A
shortening from 1 is reported but never fails the run — only a human can judge those.

Writes nothing but `backend/_truncation.db` (gitignored — delete it afterwards).
"""

from __future__ import annotations

import math
import os
import pathlib
import sys
import uuid

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

os.environ.setdefault("DATABASE_URL", "sqlite:///./_truncation.db")

from fastapi.testclient import TestClient  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.main import app  # noqa: E402
from app.models.schemas import ProjectPlan  # noqa: E402
from app.services.deduction import solution_design as sdd_mod  # noqa: E402
from app.services.diagrams import architecture as arch_mod  # noqa: E402
from app.services.diagrams import render as render_mod  # noqa: E402

from _arch_sample import VIEWS as SAMPLE_VIEWS  # noqa: E402

settings = get_settings()
API = "/api/v1"

EXPANDED_BRIEF = """## Product
A claims intake portal for a mid-size general insurer. A policyholder files a claim, uploads
supporting documents, and follows the claim to settlement without phoning anyone.

## Users
Policyholders filing and tracking claims. Claims adjusters triaging and settling them.
Compliance officers reading the audit trail.

## Tech stack
Angular front end, FastAPI back end, PostgreSQL, S3 for uploaded documents, Redis for the
job queue, ECS Fargate behind an internal ALB.

## Constraints
Every state change is auditable. Personally identifiable data is encrypted at rest.
"""

#: (call site, kind, the text as it arrived, what was drawn)
CUTS: list[tuple[str, str, str, str]] = []
#: Cuts made before the renderer ever saw the text. Always a defect: unlike a `_wrap` cut, the
#: bound had no idea how much room the box actually had.
EARLY: list[tuple[str, str, str, str]] = []
#: (view, label, what it covers) — a label drawn over text, or adrift from its own arrow.
COVERED: list[tuple[str, str, str]] = []
ADRIFT: list[tuple[str, str, float]] = []

CURRENT = ["?"]


def _site(depth: int = 2) -> str:
    frame = sys._getframe(depth)
    return f"{pathlib.Path(frame.f_code.co_filename).name}:{frame.f_lineno} {frame.f_code.co_name}"


def _install() -> None:
    real_wrap = render_mod._wrap
    real_clip = render_mod._clip

    def wrap(text, max_px, size, weight="normal", max_lines=4):
        rows = real_wrap(text, max_px, size, weight, max_lines)
        if rows and rows[-1].endswith("…") and not (text or "").strip().endswith("…"):
            CUTS.append((_site(), f"wrap max_lines={max_lines}", " ".join(str(text).split()), " ".join(rows)))
        return rows

    def clip(text, max_px, size, weight="normal"):
        out = real_clip(text, max_px, size, weight)
        if out.endswith("…") and not (text or "").strip().endswith("…"):
            CUTS.append((_site(), "clip", " ".join(str(text).split()), out))
        return out

    for mod in (render_mod, arch_mod):
        if getattr(mod, "_wrap", None) is not None:
            mod._wrap = wrap
        if getattr(mod, "_clip", None) is not None:
            mod._clip = clip

    _watch(arch_mod, "_s")
    _watch(arch_mod, "_prose")
    _watch(sdd_mod, "_text")
    _watch_labels()


def _bare(text: str) -> str:
    """The words alone, so a rewrite is not mistaken for a cut.

    These helpers do two jobs at once: they shorten, and they strip the markdown a plan is
    written in, because a diagram has no code spans to render `` `.env` `` as. Only the first job
    loses anything, so the comparison is made on text with the markup already gone.
    """
    return " ".join(text.replace("`", "").replace("*", "").replace("_", " ").split())


def _watch(module, name: str) -> None:
    """Catch a cut made before the renderer, where `_wrap` is blind to it."""
    real = getattr(module, name)

    def limited(value, *args, **kwargs):
        out = real(value, *args, **kwargs)
        if isinstance(value, str) and isinstance(out, str) and _bare(out) != _bare(value):
            EARLY.append((_site(), f"{module.__name__.rsplit('.', 1)[-1]}.{name}", value, out))
        return out

    setattr(module, name, limited)


def _watch_labels() -> None:
    """Check where each edge label ended up: over nothing, over words, or off on its own.

    `reserve` is wrapped only to learn the *text* of each claimed rect, so a collision can be
    reported as "this label, over that one" rather than as a pair of coordinates.
    """
    canvas = render_mod._Canvas
    real_reserve, real_point = canvas.reserve, canvas._label_point
    named: dict[int, dict[tuple[float, float, float, float], str]] = {}

    def reserve(self, x, y, label, *, rotated=False):
        real_reserve(self, x, y, label, rotated=rotated)
        named.setdefault(id(self), {})[self.obstacles[-1]] = " | ".join(label.splitlines())

    def label_point(self, start, end, label, *, rad, label_t, rotated=False):
        pt = real_point(self, start, end, label, rad=rad, label_t=label_t, rotated=rotated)
        flat = " | ".join(label.splitlines())
        half_w, half_h = self._label_half(label, rotated=rotated)

        def hits(among):
            return [
                r
                for r in among
                if pt[0] + half_w > r[0]
                and pt[0] - half_w < r[0] + r[2]
                and pt[1] + half_h > r[1]
                and pt[1] - half_h < r[1] + r[3]
            ]

        over = hits(self.text_obstacles)
        if over:
            names = {named.get(id(self), {}).get(r, "a box's own text") for r in over}
            COVERED.append((CURRENT[0], flat, ", ".join(sorted(names))))

        # How far the plate sits from the arrow it belongs to. Past roughly its own size the
        # renderer draws a leader, so these are never *unattached* — but a dotted line crossing a
        # third of the page is its own kind of ugly, and a run of them means some corridor is too
        # tight. Reported for a human to look at, like a shortening, not failed on.
        dx, dy = end[0] - start[0], end[1] - start[1]
        length2 = dx * dx + dy * dy or 1.0
        t = max(0.0, min(1.0, ((pt[0] - start[0]) * dx + (pt[1] - start[1]) * dy) / length2))
        gap = math.hypot(pt[0] - (start[0] + dx * t), pt[1] - (start[1] + dy * t))
        if gap > max(half_w, half_h) * 3 + 4:
            ADRIFT.append((CURRENT[0], flat, round(gap, 1)))
        return pt

    canvas.reserve, canvas._label_point = reserve, label_point


def _report(header: str) -> None:
    print(f"\n{header}")
    if not CUTS:
        print("  nothing was shortened")
        return
    for site, kind, before, after in CUTS:
        print(f"\n  {site}  [{kind}]")
        print(f"    in : {before}")
        print(f"    out: {after}")


def _unterminated(views: object) -> list[tuple[str, str]]:
    """Stored prose that stops mid-sentence, with no full stop and no ellipsis to admit it.

    The renderer is not the only place a sentence can be lost — a bound applied while the payload
    was being *built* leaves nothing for `_wrap` to notice, and the ellipsis-free ones are the
    ones a reader reports as "the text is half broken".
    """
    out: list[tuple[str, str]] = []

    def walk(node, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                walk(value, f"{path}.{key}" if path else str(key))
        elif isinstance(node, list):
            for i, value in enumerate(node):
                walk(value, f"{path}[{i}]")
        elif isinstance(node, str) and len(node.split()) >= 12:
            # Only prose is judged: a signature list or a `·`-joined enumeration legitimately
            # ends on a bracket or a word, and a short label is not a sentence at all.
            if node.rstrip()[-1:] not in ".!?…:)]\"'":
                out.append((path, node))

    walk(views, "")
    return out


def main() -> int:
    _install()

    # 1. The architecture views, from the eyeball sample's payload.
    CURRENT[0] = "architecture (sample)"
    arch_mod.render_architecture(SAMPLE_VIEWS, project_name="AgentCraft Studio")
    _report(f"Architecture views (ARCH_REVISION={arch_mod.ARCH_REVISION}) — {len(CUTS)} cut(s)")

    # 2. The blueprint views, from a project generated through the real app.
    CUTS.clear()
    CURRENT[0] = "blueprint"
    with TestClient(app) as c:
        r = c.post(
            f"{API}/auth/login",
            json={"email": settings.super_admin_email, "password": settings.super_admin_password},
        )
        if r.status_code != 200:
            print(f"  FAIL super admin login ({r.status_code})")
            return 1
        admin = {"Authorization": f"Bearer {r.json()['access_token']}"}

        email = f"trunc-{uuid.uuid4().hex[:8]}@example.com"
        password = uuid.uuid4().hex + "Aa1!"
        c.post(f"{API}/auth/signup", json={"email": email, "password": password, "full_name": "Trunc"})
        users = c.get(f"{API}/admin/users", headers=admin).json()
        rows = users if isinstance(users, list) else users.get("users") or []
        uid = next((u["id"] for u in rows if u.get("email") == email), None)
        if uid:
            c.post(f"{API}/admin/users/{uid}/approve", headers=admin)
        r = c.post(f"{API}/auth/login", json={"email": email, "password": password})
        owner = {"Authorization": f"Bearer {r.json()['access_token']}"}

        pid = c.post(f"{API}/projects", json={"name": "Truncation"}, headers=owner).json()["id"]
        c.post(f"{API}/projects/{pid}/path", json={"path": "interview"}, headers=owner)
        c.post(
            f"{API}/projects/{pid}/interview",
            json={"answers": {"goal": EXPANDED_BRIEF}, "name": "Truncation"},
            headers=owner,
        )
        c.post(f"{API}/projects/{pid}/platform", json={"platform": "claude_code"}, headers=owner)
        r = c.post(f"{API}/projects/{pid}/generate?demo=true&wait=true", headers=owner)
        print(f"\n  generate -> HTTP {r.status_code}")
        # The blueprint is its own step — `generate` writes the plan and the architectural
        # views, but SIPOC / flow / swimlane are only drawn once the process model exists.
        r = c.post(f"{API}/projects/{pid}/diagrams?demo=true&wait=true", headers=owner)
        print(f"  diagrams -> HTTP {r.status_code}  {r.text[:160] if r.status_code >= 400 else ''}")

        out = pathlib.Path(__file__).resolve().parents[1] / ".scratch"
        out.mkdir(parents=True, exist_ok=True)
        for kind in ("sipoc", "flow", "swimlane"):
            g = c.get(f"{API}/projects/{pid}/diagrams/{kind}.png", headers=owner)
            if g.status_code == 200:
                (out / f"{kind}.png").write_bytes(g.content)
            print(f"  {kind:9} HTTP {g.status_code}  {len(g.content)} bytes")

        _report(
            f"Blueprint views (RENDER_REVISION={render_mod.RENDER_REVISION}) — {len(CUTS)} cut(s)"
        )

        # 3. The same project's architecture views, through the endpoint the Diagrams tab calls,
        #    so they are drawn from this project's own plan rather than the sample payload.
        CUTS.clear()
        for kind in ("logical", "development", "deployment"):
            CURRENT[0] = f"{kind} view"
            g = c.get(f"{API}/projects/{pid}/plan/architecture/{kind}.png", headers=owner)
            if g.status_code == 200:
                (out / f"{kind}.png").write_bytes(g.content)
            print(f"  {kind:12} HTTP {g.status_code}  {len(g.content)} bytes")
        _report(f"Architecture views, live plan — {len(CUTS)} cut(s)")

        status = c.get(f"{API}/projects/{pid}", headers=owner).json()
        views = sdd_mod.plan_architecture_views(ProjectPlan.model_validate(status["plan"]))

    print("\nCuts applied before the renderer saw the text")
    for site, kind, before, after in EARLY:
        print(f"\n  {site}  [{kind}]\n    in : {before}\n    out: {after}")
    if not EARLY:
        print("  none")

    print("\nLabels drawn over text")
    for view, label, covers in COVERED:
        print(f"  {view:22} {label!r} over {covers}")
    if not COVERED:
        print("  none")

    print("\nLabels far from their own arrow (leadered, but worth a look)")
    for view, label, gap in sorted(ADRIFT, key=lambda r: -r[2]):
        print(f"  {view:22} {gap:7}px  {label!r}")
    if not ADRIFT:
        print("  none")

    print("\nStored prose ending mid-sentence")
    loose = _unterminated(views.model_dump() if hasattr(views, "model_dump") else views)
    for path, text in loose:
        print(f"  {path}\n    {text}")
    if not loose:
        print("  none")

    defects = len(EARLY) + len(COVERED) + len(loose)
    print(f"\n{defects} defect(s); {len(ADRIFT)} label(s) leadered back to their arrow")
    return 1 if defects else 0


if __name__ == "__main__":
    raise SystemExit(main())
