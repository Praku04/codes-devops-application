"""
Offline checks for SDD.md — no Bedrock, no database, no pytest.

Run it as:

    PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/verify_solution_design.py

What it proves, in order:

1. `fallback_solution_design` produces a structurally complete document with no LLM at all,
   so a project exported while Bedrock is down still ships a usable design document.
2. The validator actually rejects things — a truncated document, a missing sub-section, a
   document with a missing architecture figure, an unbalanced code fence. A validator that
   only ever says yes is worse than none, because the exporters trust it.
3. §3.2 references the three PNGs as images and contains no Mermaid at all, since a fenced
   Mermaid block is a wall of code everywhere except GitHub and an IDE preview.
3b. Each figure carries detail rather than a four-box overview: every component and every
   hosted process has a technology and a one-line purpose, edges are labelled, hops carry a
   port and a payload, and all of it also reaches the reader as a table — a PNG is not
   searchable, so the document must stand on its own.
3c. A payload stored before that shape existed — `components` as plain strings — still draws
   all three views, with one line per box instead of three.
4. The Development View is drawn from `plan.source_tree`, so the picture cannot disagree
   with the workspace the exporter writes.
5. `apply_solution_design_to_plan` writes both the plan field and `file_overrides` —
   `file_overrides` is applied last in `render_files`, so missing it would mean a
   regenerated document never reached the exported file — and stores the architecture views
   the PNGs are re-drawn from.
6. Those views actually render: three PNGs, drawn by the same renderer as the SIPOC.

Exits non-zero on the first failure, and prints one line per check either way.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.state_machine import Platform  # noqa: E402
from app.models.schemas import ProjectBrief, ProjectPlan, SourceFileSpec  # noqa: E402
from app.services.deduction.solution_design import (  # noqa: E402
    ARCH_FIGURE_REFS,
    REQUIRED_HEADINGS,
    SDD_FILENAME,
    apply_solution_design_to_plan,
    fallback_solution_design,
    fallback_solution_design_views,
    is_solution_design_complete,
    missing_solution_design_sections,
    plan_architecture_views,
    plan_needs_detailed_solution_design,
    plan_solution_design_body,
)
from app.services.diagrams.architecture import (  # noqa: E402
    ARCH_KINDS,
    ARCH_WORKSPACE_PATHS,
    render_architecture,
)

_FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok    {label}")
        return
    _FAILURES.append(label)
    print(f"  FAIL  {label}{f' — {detail}' if detail else ''}")


def sample_brief() -> ProjectBrief:
    return ProjectBrief(
        problem_statement=(
            "Trainers cannot see which modules a batch has completed, so they rebuild the "
            "same progress spreadsheet every week and trainees are re-assigned work they "
            "have already finished."
        ),
        tech_stack="FastAPI, PostgreSQL, Angular, AWS Bedrock",
        domains=["batch management", "trainee progress", "module catalogue", "reporting"],
        integrations=["AWS Bedrock", "S3", "corporate SSO"],
        constraints="Trainee PII must not leave ap-south-1. Reports are read-only for trainers.",
    )


def sample_plan() -> ProjectPlan:
    paths = [
        "main.py",
        "backend/api/routes/batches.py",
        "backend/api/routes/progress.py",
        "backend/services/batch_service.py",
        "backend/services/progress_service.py",
        "backend/repositories/batch_repo.py",
        "backend/models/batch.py",
        "backend/core/config.py",
        "backend/tests/test_batches.py",
        "frontend/app/page.tsx",
        "frontend/services/api.ts",
    ]
    return ProjectPlan(
        project_name="TrainTrack",
        summary="Batch training progress tracker for internal enablement teams.",
        source_tree=[SourceFileSpec(path=p, purpose=f"Role: {p}") for p in paths],
    )


def main() -> int:
    brief, plan = sample_brief(), sample_plan()

    print("1. Deterministic renderer (no Bedrock call)")
    md = fallback_solution_design(brief, plan, Platform.CLAUDE_CODE)
    missing = missing_solution_design_sections(md)
    check("every required heading present and in order", not missing, f"missing {missing}")
    check("is_solution_design_complete", is_solution_design_complete(md))
    check("substantial document", len(md) >= 5000, f"{len(md)} chars")
    check("code fences balanced", md.count("```") % 2 == 0, f"{md.count('```')} fences")
    check("at least one ID table", "| ID |" in md)
    print(f"  info  {len(md)} chars, {len(REQUIRED_HEADINGS)} required headings")

    print("2. Validator rejects what it should")
    truncated = md[: md.index("## 4. Dependencies")]
    check("truncated document is incomplete", not is_solution_design_complete(truncated))
    check(
        "truncation is reported by name",
        "## 7. Acceptance Criteria" in missing_solution_design_sections(truncated),
    )
    dropped = md.replace("#### 3.2.2 Development View", "#### Development View")
    check("a renumbered sub-section is caught", not is_solution_design_complete(dropped))
    for ref in ARCH_FIGURE_REFS:
        # A document that lost a figure still reads as prose, so nothing but the validator
        # would notice. Drop each one in turn and confirm it does.
        check(
            f"a document missing {ref[2:-1]} is incomplete",
            not is_solution_design_complete(md.replace(ref, ")")),
        )
    check("an unbalanced fence is incomplete", not is_solution_design_complete(md + "\n```\n"))
    # Order matters, not just presence: a model that answers the sections in its own order
    # still fails, because the numbered outline is the contract.
    section_one = md[md.index("## 1. Project Summary") : md.index("## 2. Scope")]
    reordered = md.replace(section_one, "") + "\n" + section_one
    check("a document with §1 moved to the end is incomplete", not is_solution_design_complete(reordered))

    print("3. Architecture views are images, not Mermaid")
    check("no Mermaid fence anywhere", "```mermaid" not in md)
    section = md[md.index("### 3.2 Architectural Views") : md.index("### 3.3")]
    for path in ARCH_WORKSPACE_PATHS:
        check(f"§3.2 references {path}", f"]({path})" in section)
    check("each figure has a caption", section.count("*Figure 3.2.") == 3, section.count("*Figure 3.2."))
    check("the TOC still lists all three views", "#323-deployment-view" in md)

    print("3b. The figures carry detail, not a four-box overview")
    # The reported problem was that the diagrams read as high level. Detail is not a matter of
    # taste here: each box is drawn from three fields and each arrow from a label, so the check
    # is whether those fields survive from the payload into the document.
    detailed = fallback_solution_design_views(brief, plan)
    parts = [c for layer in detailed["logical"]["layers"] for c in layer["components"]]
    check("logical view has boxes to draw", len(parts) >= 12, f"{len(parts)} components")
    check("every component is a dict with all three lines", all(
        isinstance(c, dict) and {"name", "tech", "detail"} <= set(c) for c in parts
    ))
    check("most components name their technology", sum(1 for c in parts if c["tech"]) >= 8,
          f"{sum(1 for c in parts if c['tech'])} with tech")
    check("every component says what it does", all(c["detail"] for c in parts))
    lflows = detailed["logical"]["flows"]
    check("flows are labelled", sum(1 for f in lflows if f["label"]) >= 3, f"{lflows}")
    procs = [h for node in detailed["deployment"]["nodes"] for h in node["hosts"]]
    check("deployment nodes hold real processes", len(procs) >= 8, f"{len(procs)} hosts")
    check("every process says what it does", all(h["detail"] for h in procs))
    hops = detailed["deployment"]["connections"]
    check("hops carry a port", sum(1 for c in hops if c["port"]) >= 3, f"{hops}")
    check("hops say what they carry", all(c["data"] for c in hops))
    check("packages name their key files", any(g.get("key_files") for g in detailed["development"]["groups"]))
    # And the same detail reaches the reader as a table, because a PNG is not searchable and a
    # reviewer working from the document alone must be able to read every box.
    check("§3.2.1 has a component detail table", "**Components in detail.**" in md)
    check("§3.2.3 has a process detail table", "**Processes in detail.**" in md)
    check("the hop table has a Port column", "| Port |" in md)
    check("the hop table has a Carries column", "| Carries |" in md)
    check("§3.2.2 lists key files", "| Key files |" in md)

    print("3c. A payload stored before this change still draws")
    # `components`/`hosts` used to be plain lists of strings. Those payloads are on real plans,
    # so they must keep drawing — with one line per box instead of three, not with an exception.
    legacy = {
        "logical": {
            "description": "",
            "layers": [
                {"name": "API", "components": ["Routes", "Schemas"], "responsibility": ""},
                {"name": "Data", "components": ["Repositories"], "responsibility": ""},
            ],
            "flows": [{"from": "Routes", "to": "Repositories", "label": "SQL"}],
        },
        "development": {"description": "", "groups": [{"path": "backend/", "files": 3, "top": "backend", "responsibility": "", "depends_on": []}]},
        "deployment": {
            "description": "",
            "nodes": [
                {"name": "App", "runtime": "Container", "hosts": ["API service"], "scaling": ""},
                {"name": "Data", "runtime": "RDS", "hosts": ["Postgres"], "scaling": ""},
            ],
            "connections": [{"from": "API service", "to": "Postgres", "protocol": "TCP"}],
        },
    }
    legacy_drawn = render_architecture(legacy, project_name="Legacy")
    check("a string-shaped payload draws all three views", sorted(legacy_drawn) == sorted(ARCH_KINDS))
    for kind in ARCH_KINDS:
        data, _w, _h, _s = legacy_drawn.get(kind, (b"", 0, 0, 0))
        check(f"legacy {kind} is a real PNG", data[:8] == b"\x89PNG\r\n\x1a\n", f"{len(data)} bytes")

    print("4. Development View follows plan.source_tree")
    view = md[md.index("#### 3.2.2 Development View") : md.index("#### 3.2.3 Deployment View")]
    for path in ("backend/api/", "backend/services/", "backend/repositories/", "frontend/app/"):
        check(f"{path} appears in the view", path in view)
    check("main.py appears in the view", "main.py" in view)
    check(
        "no package the plan does not have",
        "backend/workers/" not in view and "backend/graphql/" not in view,
    )
    empty_tree = fallback_solution_design(
        brief, plan.model_copy(update={"source_tree": []}), Platform.CLAUDE_CODE
    )
    check("an empty source tree still renders a complete document", is_solution_design_complete(empty_tree))

    print("5. Persistence writes both the plan field and file_overrides")
    views = fallback_solution_design_views(brief, plan)
    saved = apply_solution_design_to_plan(plan, md, used_llm=True, complete=True, views=views)
    check("plan.solution_design set", saved.solution_design == md)
    check("plan.solution_design_llm set", saved.solution_design_llm is True)
    check("plan.solution_design_complete set", saved.solution_design_complete is True)
    check(f"file_overrides['{SDD_FILENAME}'] set", (saved.file_overrides or {}).get(SDD_FILENAME) == md)
    check("plan_solution_design_body reads it back", plan_solution_design_body(saved) == md)
    check("a complete document needs no regeneration", not plan_needs_detailed_solution_design(saved))
    # used_llm=False must not be recorded as an LLM document, or the UI would stop offering
    # to regenerate a fallback into a real one.
    fb = apply_solution_design_to_plan(plan, md, used_llm=False, complete=True)
    check("fallback is not marked as LLM", fb.solution_design_llm is False)
    check("an empty plan needs generation", plan_needs_detailed_solution_design(plan))
    check("plan.architecture_views stored", bool(saved.architecture_views.get("development")))
    check("stored views are read back", plan_architecture_views(saved) == views)
    # A project generated before this feature has no stored payload, and its SDD.md still
    # links to three images — so the fallback has to produce a drawable structure.
    derived = plan_architecture_views(plan)
    check("a plan with no stored views still derives them", bool(derived.get("development")))
    check(
        "derived development view is the real source tree",
        {g["path"] for g in derived["development"]["groups"]} >= {"backend/api/", "frontend/app/"},
    )

    print("6. The views render to PNG")
    drawn = render_architecture(views, project_name=plan.project_name)
    check("all three views drew", sorted(drawn) == sorted(ARCH_KINDS), sorted(drawn))
    for kind in ARCH_KINDS:
        data, w, h, _scale = drawn.get(kind, (b"", 0, 0, 0))
        check(f"{kind} is a real PNG", data[:8] == b"\x89PNG\r\n\x1a\n" and w > 400 and h > 300,
              f"{len(data)} bytes {w}x{h}")
    thin = render_architecture({}, project_name="Empty")
    check("an empty payload still draws something", len(thin) == 3, sorted(thin))

    print("7. Every platform renders")
    for platform in (Platform.CLAUDE_CODE, Platform.CURSOR, Platform.WINDSURF, Platform.GITHUB_COPILOT,):
        body = fallback_solution_design(brief, plan, platform)
        check(f"{platform.value} complete", is_solution_design_complete(body))

    if _FAILURES:
        print(f"\n{len(_FAILURES)} check(s) failed:")
        for label in _FAILURES:
            print(f"  - {label}")
        return 1
    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
