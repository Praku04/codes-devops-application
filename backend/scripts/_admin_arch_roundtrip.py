"""Ad-hoc round trip: the architecture and SDD-download endpoints, against the real app.

Not a test — no pytest in this project, and no server or port either: `TestClient` drives the
real ASGI app, so the routes, the admin dependency, `project_service.architecture_png` and
`project_service.solution_design_document` all run exactly as they do behind uvicorn.

What it proves:
  * a signed-in super admin gets three PNGs for a session they do NOT own
  * a signed-in ordinary user is refused (403), so the route is admin-gated
  * an anonymous caller is refused (401)
  * the owner's own `/projects/{id}/plan/architecture/{kind}.png` still serves the same bytes
  * SDD.md downloads as a PDF and as a DOCX, both with §3.2's figures inside, and the admin's
    copy is byte for byte the owner's — the two routes share one service method

Credentials are read from settings and never printed.
"""

from __future__ import annotations

import os
import pathlib
import sys
import uuid

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

os.environ.setdefault("DATABASE_URL", "sqlite:///./_roundtrip.db")

from fastapi.testclient import TestClient  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.main import app  # noqa: E402

settings = get_settings()

API = "/api/v1"
KINDS = ("logical", "development", "deployment")

#: Answered as `goal`, this finishes the interview in one POST. Shaped like what the wizard's
#: "expand brief" step produces — the detector only needs 120+ characters and three `##`
#: headings, but the sections are real ones so the drawn views have something to say.
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


#: `%PDF` and the ZIP local-file header a .docx is. Enough to catch a JSON error body served
#: with a 200, which is the failure mode a length check alone reads as success.
_MAGIC = {"pdf": b"%PDF", "docx": b"PK\x03\x04"}
_MEDIA = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


def _pdf_shape(data: bytes) -> tuple[int, int]:
    """`(page count, embedded image count)` — the two numbers a missing figure changes.

    Counted off the raw object stream rather than through a parser: every XObject the writer
    emitted for a figure carries `/Subtype /Image`, and reportlab writes one per picture.
    """
    return data.count(b"/Type /Page\n") + data.count(b"/Type /Page "), data.count(b"/Subtype /Image")


def _docx_shape(data: bytes) -> tuple[int, int, int]:
    """`(paragraphs, tables, embedded images)`.

    A DOCX cannot be rendered on this machine, so its structure is what there is to check. The
    images are counted in `word/media/`, which is where python-docx puts an `add_picture`.
    """
    import io
    import zipfile

    import docx

    doc = docx.Document(io.BytesIO(data))
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        media = [n for n in z.namelist() if n.startswith("word/media/")]
    return len(doc.paragraphs), len(doc.tables), len(media)


# Why the two copies are compared by content and not by `a.content == b.content`: a PDF carries
# `/CreationDate` and a DOCX carries both `dcterms:created` and a modification time per zip
# entry, so two renders of one document differ in a handful of bytes and agree in every byte
# that means anything. Equal *content* is the claim worth making anyway — an admin must be
# reading what the user has, not a document produced at the same instant.


def _pdf_content(data: bytes) -> str:
    """Every page's text, joined. What a reader would compare if they read both copies."""
    import io

    import pdfplumber

    with pdfplumber.open(io.BytesIO(data)) as pdf:
        return "\f".join((p.extract_text() or "") for p in pdf.pages)


def _docx_content(data: bytes) -> tuple:
    """Paragraph text, table cell text, and a digest of each embedded picture."""
    import hashlib
    import io
    import zipfile

    import docx

    doc = docx.Document(io.BytesIO(data))
    paras = tuple(p.text for p in doc.paragraphs)
    cells = tuple(c.text for t in doc.tables for r in t.rows for c in r.cells)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        media = tuple(
            sorted(
                hashlib.sha256(z.read(n)).hexdigest()
                for n in z.namelist()
                if n.startswith("word/media/")
            )
        )
    return paras, cells, media


def _cli_helpers():
    """`(_save_solution_design, _document_formats)` from the CLI, or None if it is not present.

    Imported rather than reimplemented: the point of checking it here is that the terminal and
    the browser download the same document, which a copy of the helper could not show.
    """
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "cli"))
    try:
        from agentcraft.main import _document_formats, _save_solution_design
    except Exception:  # noqa: BLE001 — the CLI is optional to this check
        return None
    return _save_solution_design, _document_formats


def _png_size(data: bytes) -> str:
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
        return "NOT A PNG"
    w = int.from_bytes(data[16:20], "big")
    h = int.from_bytes(data[20:24], "big")
    return f"{w}x{h}"


def main() -> int:
    failures: list[str] = []

    def check(label: str, ok: bool, extra: str = "") -> None:
        print(f"  {'ok  ' if ok else 'FAIL'}  {label}{'  — ' + extra if extra else ''}")
        if not ok:
            failures.append(label)

    with TestClient(app) as c:
        # -- the admin, from settings; never echoed --
        r = c.post(
            f"{API}/auth/login",
            json={
                "email": settings.super_admin_email,
                "password": settings.super_admin_password,
            },
        )
        if r.status_code != 200:
            print(f"  FAIL  super admin login ({r.status_code}) {r.text[:200]}")
            return 1
        admin_token = r.json()["access_token"]
        print("  ok    super admin signed in")

        # -- an ordinary user who will own the project --
        email = f"owner-{uuid.uuid4().hex[:8]}@example.com"
        password = uuid.uuid4().hex + "Aa1!"
        r = c.post(
            f"{API}/auth/signup",
            json={"email": email, "password": password, "full_name": "Round Trip"},
        )
        check("owner signed up", r.status_code in (200, 201), f"HTTP {r.status_code}")
        # A fresh signup is pending until approved, so approve it as the admin.
        users = c.get(
            f"{API}/admin/users", headers={"Authorization": f"Bearer {admin_token}"}
        ).json()
        rows = users if isinstance(users, list) else users.get("users") or []
        uid = next((u["id"] for u in rows if u.get("email") == email), None)
        if uid:
            c.post(
                f"{API}/admin/users/{uid}/approve",
                headers={"Authorization": f"Bearer {admin_token}"},
            )
        r = c.post(f"{API}/auth/login", json={"email": email, "password": password})
        check("owner signed in", r.status_code == 200, f"HTTP {r.status_code}")
        if r.status_code != 200:
            return 1
        owner = {"Authorization": f"Bearer {r.json()['access_token']}"}
        admin = {"Authorization": f"Bearer {admin_token}"}

        # -- a project with a plan, so there is a design to draw --
        #
        # The full FSM, not a shortcut: generate is only reachable from PLATFORM_SELECTED
        # (create → path → interview → platform → generate). The interview is finished in a
        # single call by answering `goal` with a structured expanded brief — three or more `##`
        # sections is what `is_structured_expanded_brief` looks for, and that path prefills the
        # optional answers without calling Bedrock, so this script stays offline.
        r = c.post(f"{API}/projects", json={"name": "Round Trip"}, headers=owner)
        check("project created", r.status_code in (200, 201), f"HTTP {r.status_code}")
        if r.status_code not in (200, 201):
            print(r.text[:400])
            return 1
        pid = r.json()["id"]

        r = c.post(f"{API}/projects/{pid}/path", json={"path": "interview"}, headers=owner)
        check("interview path chosen", r.status_code == 200, f"HTTP {r.status_code}")

        r = c.post(
            f"{API}/projects/{pid}/interview",
            json={"answers": {"goal": EXPANDED_BRIEF}, "name": "Round Trip"},
            headers=owner,
        )
        state = r.json().get("state") if r.status_code == 200 else None
        check(
            "interview completed in one answer",
            state == "CONTEXT_READY",
            f"HTTP {r.status_code}, state {state}",
        )

        r = c.post(
            f"{API}/projects/{pid}/platform", json={"platform": "claude_code"}, headers=owner
        )
        check("platform selected", r.status_code == 200, f"HTTP {r.status_code}")

        r = c.post(f"{API}/projects/{pid}/generate?demo=true&wait=true", headers=owner)
        check("plan generated (demo)", r.status_code in (200, 202), f"HTTP {r.status_code}")
        if r.status_code not in (200, 202):
            print("   ", r.text[:300])

        print("\n1. The admin sees a session they do not own")
        for kind in KINDS:
            r = c.get(f"{API}/admin/projects/{pid}/architecture/{kind}.png", headers=admin)
            ok = r.status_code == 200 and r.content[:8] == b"\x89PNG\r\n\x1a\n"
            check(
                f"admin GET {kind}.png",
                ok,
                f"HTTP {r.status_code}, {len(r.content)} bytes, {_png_size(r.content)}",
            )
            check(
                f"{kind}.png is inline, not a download",
                "inline" in r.headers.get("content-disposition", ""),
                r.headers.get("content-disposition", "(none)"),
            )

        print("\n2. The owner's own route serves the same picture")
        for kind in KINDS:
            a = c.get(f"{API}/admin/projects/{pid}/architecture/{kind}.png", headers=admin)
            o = c.get(f"{API}/projects/{pid}/plan/architecture/{kind}.png", headers=owner)
            check(
                f"{kind}: admin bytes == owner bytes",
                a.status_code == o.status_code == 200 and a.content == o.content,
                f"admin {a.status_code}/{len(a.content)}B, owner {o.status_code}/{len(o.content)}B",
            )

        print("\n3. The route is admin-gated")
        r = c.get(f"{API}/admin/projects/{pid}/architecture/logical.png", headers=owner)
        check("the owner is refused on the admin route", r.status_code == 403, f"HTTP {r.status_code}")
        r = c.get(f"{API}/admin/projects/{pid}/architecture/logical.png")
        check("an anonymous caller is refused", r.status_code in (401, 403), f"HTTP {r.status_code}")
        r = c.get(f"{API}/admin/projects/{pid}/architecture/nonsense.png", headers=admin)
        check("an unknown view is rejected", r.status_code >= 400, f"HTTP {r.status_code}")

        print("\n4. /admin/users reports has_plan for the panel")
        users = c.get(f"{API}/admin/users", headers=admin).json()
        rows = users if isinstance(users, list) else users.get("users") or []
        sessions = [s for u in rows for s in (u.get("sessions") or [])]
        mine = next((s for s in sessions if s.get("id") == pid), None)
        check("the session is listed for the admin", mine is not None)
        if mine is not None:
            check("has_plan is true", bool(mine.get("has_plan")), repr(mine.get("has_plan")))

        # The other half of the same request: the exported workspace has to explain the folder
        # those PNGs land in. Checked off the live preview rather than a unit call, so the
        # splice is verified where the user meets it.
        print("\n5. The exported workspace documents docs/architecture/")
        r = c.get(f"{API}/projects/{pid}/export/preview", headers=owner)
        check("export preview served", r.status_code == 200, f"HTTP {r.status_code}")
        if r.status_code == 200:
            body = r.json()
            tree = body.get("files") or []
            readme = (body.get("contents") or {}).get("README.md", "")
            arch = [p for p in tree if p.startswith("docs/architecture/")]
            check("three architecture PNGs in the tree", len(arch) == 3, ", ".join(arch) or "none")
            check(
                "README has the Architecture section",
                "## Architecture (`docs/architecture/`)" in readme,
            )
            check(
                "README still documents the blueprint folder",
                "docs/diagrams/" in readme,
            )
            check(
                "every drawn view is named in the README",
                all(pathlib.PurePosixPath(p).name in readme for p in arch),
            )
            check(
                "the Generated-for footer is still last",
                readme.rstrip().splitlines()[-1].startswith("Generated for ")
                or "Generated for " in readme.rstrip().splitlines()[-1],
                readme.rstrip().splitlines()[-1][:70] if readme.strip() else "(empty)",
            )
            sdd = next((k for k in (body.get("contents") or {}) if k.endswith("SDD.md")), None)
            check("SDD.md is in the preview", sdd is not None, sdd or "missing")
            if sdd:
                text = body["contents"][sdd]
                check(
                    "SDD embeds all three views",
                    all(pathlib.PurePosixPath(p).name in text for p in arch),
                )
                check("SDD shows no mermaid source", "```mermaid" not in text)

        # The download the user asked for: the same document, on paper, from either side of the
        # app. Checked structurally rather than by eye — `scripts/_document_export_sample.py`
        # plus a raster pass is where the layout itself is inspected.
        print("\n6. SDD.md downloads as a PDF and a DOCX")
        docs: dict[str, bytes] = {}
        for fmt in ("pdf", "docx"):
            o = c.get(f"{API}/projects/{pid}/plan/solution-design.{fmt}", headers=owner)
            check(f"owner GET solution-design.{fmt}", o.status_code == 200, f"HTTP {o.status_code}")
            if o.status_code != 200:
                print("   ", o.text[:300])
                continue
            docs[fmt] = o.content
            check(
                f"{fmt} is an attachment named after the project",
                "attachment" in o.headers.get("content-disposition", "")
                and f"SDD.{fmt}" in o.headers.get("content-disposition", ""),
                o.headers.get("content-disposition", "(none)"),
            )
            check(
                f"{fmt} carries its own media type",
                o.headers.get("content-type", "").startswith(_MEDIA[fmt]),
                o.headers.get("content-type", "(none)"),
            )
            check(f"{fmt} starts with the right magic bytes", o.content[:4] == _MAGIC[fmt],
                  repr(o.content[:4]))
            a = c.get(f"{API}/admin/projects/{pid}/solution-design.{fmt}", headers=admin)
            reader = _pdf_content if fmt == "pdf" else _docx_content
            check(
                f"the admin's {fmt} is the same document as the owner's",
                a.status_code == 200 and reader(a.content) == reader(o.content),
                f"admin {a.status_code}/{len(a.content)}B, owner {len(o.content)}B",
            )

        check(
            "the two formats are the same document, not the same bytes",
            len(docs) == 2 and docs["pdf"] != docs["docx"],
        )
        if "pdf" in docs:
            pages, figures = _pdf_shape(docs["pdf"])
            check("the PDF has the pages a 20k-character document needs", pages >= 8, f"{pages} pages")
            check("all three figures are embedded in the PDF", figures >= 3, f"{figures} images")
        if "docx" in docs:
            paras, tables, figures = _docx_shape(docs["docx"])
            check("the DOCX has the document's tables", tables >= 8, f"{tables} tables")
            check("the DOCX has its prose", paras >= 100, f"{paras} paragraphs")
            check("all three figures are embedded in the DOCX", figures >= 3, f"{figures} images")

        print("\n7. The download routes are gated the same way the PNGs are")
        r = c.get(f"{API}/admin/projects/{pid}/solution-design.pdf", headers=owner)
        check("the owner is refused on the admin route", r.status_code == 403, f"HTTP {r.status_code}")
        r = c.get(f"{API}/admin/projects/{pid}/solution-design.pdf")
        check("an anonymous caller is refused", r.status_code in (401, 403), f"HTTP {r.status_code}")
        r = c.get(f"{API}/projects/{pid}/plan/solution-design.rtf", headers=owner)
        check("an unknown format is rejected", r.status_code == 404, f"HTTP {r.status_code}")

        # The CLI's own code path, not a re-implementation of it: `TestClient` *is* an
        # `httpx.Client`, so the helper behind `agentcraft plan document` runs here unmodified,
        # including the `Content-Disposition` parse that decides what the file is called.
        print("\n8. The CLI writes the same two files")
        cli = _cli_helpers()
        if cli is None:
            print("  skip  cli/agentcraft is not importable from here")
        else:
            save, formats = cli
            out = pathlib.Path(__file__).resolve().parents[1] / ".scratch" / "cli-docs"
            for label, headers, as_admin in (("owner", owner, False), ("admin", admin, True)):
                c.headers.update(headers)
                try:
                    written = save(c, pid, out / label, formats("both"), as_admin=as_admin)
                finally:
                    c.headers.pop("Authorization", None)
                check(f"{label}: two files written", len(written) == 2, str(len(written)))
                for path, size in written:
                    check(
                        f"{label}: {path.name} is named after the project and has content",
                        path.name.startswith("round-trip-SDD.") and path.stat().st_size > 20_000,
                        size,
                    )

    print()
    if failures:
        print(f"{len(failures)} check(s) failed:")
        for f in failures:
            print("  -", f)
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
