"""
Generate and backfill `SDD.md` — the Solution Design Document — for exported workspaces.

Sibling of `work_breakdown.py`, and deliberately built the same way: a filename
constant, prompt files under `app/prompts/`, JSON from Bedrock rendered to markdown by
code, a structural completeness validator, an `apply_*_to_plan` that writes both the plan
field **and** `file_overrides`, and a deterministic renderer for the no-LLM path.

Why the LLM returns JSON rather than markdown
---------------------------------------------
The section numbering (`1`, `1.1`, `3.2.1`, …) is a contract: the validator checks for
those exact headings, and the UI/CLI both report "complete" from it. A model asked for
markdown drifts — it renumbers, merges 3.4.1 into 3.4, or stops mid-table. Asking for
JSON and rendering here means the outline is produced by code and cannot drift, and the
model only supplies content.

Architectural views
-------------------
§3.2 is rendered as a **drawn PNG** plus a table per view, because that is what a solution
design is read for. The picture is a real image file under `docs/architecture/`, produced by
`services/diagrams/architecture.py` with the same renderer that draws the SIPOC, process flow
and swimlane — not a fenced ```mermaid block. A Mermaid fence is a diagram in GitHub and in
an IDE preview and a wall of code everywhere else (this app's own file pane, a pasted email,
a Word document, a PDF), and the wall of code is what the reader who most needs the diagram
sees. An image is an image in all of them.

The structure behind those pictures is normalised by `architecture_views_payload` and stored
on `plan.architecture_views`, so the PNGs can be re-drawn — after an edit, for a later export,
by a newer renderer — without another model call. The Development View is derived from
`plan.source_tree` rather than from the model: those are the paths the workspace actually
exports, so the picture cannot disagree with the files beside it.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from app.core.state_machine import Platform
from app.models.schemas import ProjectBrief, ProjectPlan
from app.services.diagrams.architecture import (
    ARCH_DOC_DIR,
    ARCH_VIEW_BY_KIND,
    arch_workspace_path,
)

logger = logging.getLogger("agentcraft.solution_design")

SDD_FILENAME = "SDD.md"
_PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"

# Three section groups, generated in parallel, so the whole document costs roughly one
# call's latency instead of three. Budgets are per group: §3 carries the architecture and
# is the largest, §1–2 the requirement tables, §4–7 the delivery sections.
_SCOPE_MAX_TOKENS = 6000
_SOLUTION_MAX_TOKENS = 8000
_DELIVERY_MAX_TOKENS = 6000
_SINGLE_MAX_TOKENS = 14000

_MIN_FUNCTIONAL_REQUIREMENTS = 6
_MIN_NON_FUNCTIONAL_REQUIREMENTS = 5
_MIN_ACCEPTANCE_CRITERIA = 5
_MIN_DOCUMENT_LENGTH = 5000


def _read_prompt(name: str, fallback: str) -> str:
    path = _PROMPTS_DIR / name
    if path.exists():
        return path.read_text(encoding="utf-8")
    return fallback


def _platform_label(platform: Platform | None) -> str:
    if platform == Platform.CLAUDE_CODE:
        return "Claude Code"
    if platform == Platform.WINDSURF:
        return "Windsurf"
    if platform == Platform.GITHUB_COPILOT:
        return "GitHub Copilot"
    return "Cursor"


def _brief_context(brief: ProjectBrief, limit: int = 28000) -> str:
    context = brief.as_prompt_context()
    if len(context) > limit:
        return context[:limit] + "\n…[truncated]"
    return context


def _scaffold_paths(plan: ProjectPlan, limit: int = 200) -> list[str]:
    return sorted(
        {s.path.replace("\\", "/").lstrip("/") for s in (plan.source_tree or []) if s.path}
    )[:limit]


def _scaffold_hint(plan: ProjectPlan) -> str:
    """Internal LLM context only — the real tree lives in README.md."""
    paths = _scaffold_paths(plan, 60)
    if not paths:
        return "main.py, backend/api/, backend/services/, backend/repositories/, frontend/"
    return ", ".join(paths)


# ---------------------------------------------------------------------------
# Architecture figures
#
# The image path is workspace-relative because SDD.md sits at the root of the exported folder,
# next to `docs/`. That makes the reference resolve in GitHub, in an IDE preview, in this app's
# file pane, and after somebody unzips the folder somewhere else — which a Mermaid fence does
# in the first two of those only.
# ---------------------------------------------------------------------------


def _figure(kind: str) -> list[str]:
    """The image reference and caption for one architectural view."""
    meta = ARCH_VIEW_BY_KIND[kind]
    return [
        f"![{meta.label} — {meta.purpose}]({arch_workspace_path(kind)})",
        "",
        f"*Figure {meta.section} — {meta.label}: {meta.purpose}*",
        "",
    ]


#: Every image reference §3.2 must carry, checked by `is_solution_design_complete`. A document
#: that lost its figures still reads as prose, which is exactly why the validator has to catch
#: it rather than trusting the renderer.
ARCH_FIGURE_REFS: tuple[str, ...] = tuple(
    f"]({arch_workspace_path(k)})" for k in ARCH_VIEW_BY_KIND
)


def _code(value: str) -> str:
    """A value as an inline code span, or "—" when empty.

    Strips backticks first: several of these cells carry model-supplied prose that may already
    contain a code span, and wrapping ``a `.env` file`` in backticks again closes the span at
    the first inner tick and renders the rest as plain text with stray ticks in it. One place
    to fix, because every wrapped cell in §3.2 goes through here.
    """
    text = " ".join(str(value or "").split()).replace("`", "")
    return f"`{text}`" if text else "—"


def _md_table(headers: list[str], rows: list[list[str]]) -> list[str]:
    """Pipe table with cells sanitised so a `|` in content cannot split a column."""
    if not rows:
        return []
    out = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        cells = []
        for i in range(len(headers)):
            raw = row[i] if i < len(row) else ""
            cells.append(" ".join(str(raw or "—").split()).replace("|", "\\|"))
        out.append("| " + " | ".join(cells) + " |")
    return out


# ---------------------------------------------------------------------------
# JSON parsing / normalisation
# ---------------------------------------------------------------------------


def _strs(value: Any, limit: int = 20) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [" ".join(str(v).split()) for v in value if str(v or "").strip()][:limit]


def _dicts(value: Any, limit: int = 24) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [v for v in value if isinstance(v, dict)][:limit]


def _text(value: Any, limit: int = 4000) -> str:
    """A trimmed string, bounded — never cut mid-word, and a cut is always visible.

    This was `str(value or "").strip()[:limit]`, and it is the function that put
    "Environment variables are the only coupling between th" in the header of a drawn
    Development View: a hard slice at 400 characters, mid-word, with nothing to mark it. From
    a picture or a paragraph, an unmarked cut is indistinguishable from a sentence whose author
    stopped typing — which is worse than a shorter sentence that says it was shortened.

    A whole sentence is preferred to a marked fragment when one ends late enough in the budget,
    because these strings are read as prose in §3.2 as well as drawn in a header. Otherwise the
    bound falls back to the last word boundary plus an ellipsis, the same contract as
    `diagrams/build.py::_cap` and `diagrams/architecture.py::_prose`.
    """
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    end = max(cut.rfind(f"{p} ") for p in (".", "!", "?"))
    if end >= limit * 0.6:
        return cut[: end + 1]
    head = cut[: cut.rfind(" ")] if " " in cut else cut
    return f"{head.rstrip(' ,;:.')}…"


def _components(value: Any, limit: int = 12) -> list[dict[str, str]]:
    """A layer's components or a node's processes as `{name, tech, detail, interface}`.

    Both the picture and §3.2's tables read this one shape, so a component carries the same
    four facts everywhere it appears. A bare string is still accepted — it is what the model
    returns when it ignores the schema, and what older stored payloads contain — and simply
    arrives with the detail fields empty.

    `interface` is the checkable fact: the route, table, topic or state the component owns.
    Without it the tables could say a component is "responsible for persistence" and a reader
    still had nothing to grep for, which is what made §3.2 read as narration.
    """
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    out: list[dict[str, str]] = []
    for item in value:
        if isinstance(item, dict):
            name = _get(item, "name", "component", "label", "process", "module")
            tech = _get(item, "tech", "technology", "runtime", "framework")
            detail = _get(item, "detail", "responsibility", "purpose", "description")
            interface = _get(item, "interface", "contract", "exposes", "state", "api")
        else:
            name, tech, detail, interface = " ".join(str(item or "").split()), "", "", ""
        if not name:
            continue
        # `_text`, not a bare slice: `detail` and `interface` are drawn as the lines inside a
        # box on the logical and deployment views, and a box note ending "opens no databa" is
        # the same defect as a cut header — the reader cannot tell it from a note nobody
        # finished. The bounds themselves are unchanged; only how they are reached is.
        out.append(
            {
                "name": _text(name, 80),
                "tech": _text(tech, 44),
                # The same budgets `architecture.py::_components` uses, so a cell is bounded once
                # rather than at two different lengths on the way to the same box.
                "detail": _text(detail, 280),
                "interface": _text(interface, 160),
            }
        )
        if len(out) >= limit:
            break
    return out


def _anchor(components: list[dict[str, str]], fallback: str) -> str:
    """The name an edge should attach to when a band has to stand in for its contents."""
    return components[0]["name"] if components else fallback


def _get(d: dict[str, Any], *keys: str, default: str = "") -> str:
    """First non-empty value among `keys` — models rename fields between calls."""
    for key in keys:
        val = d.get(key)
        if isinstance(val, list):
            val = ", ".join(str(x) for x in val if str(x or "").strip())
        if str(val or "").strip():
            return " ".join(str(val).split())
    return default


def _parse_scope(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("scope section not a dict")
    functional = _dicts(data.get("functional_requirements"), 30)
    non_functional = _dicts(data.get("non_functional_requirements"), 20)
    if len(functional) < _MIN_FUNCTIONAL_REQUIREMENTS:
        raise ValueError(f"need at least {_MIN_FUNCTIONAL_REQUIREMENTS} functional requirements")
    if len(non_functional) < _MIN_NON_FUNCTIONAL_REQUIREMENTS:
        raise ValueError(
            f"need at least {_MIN_NON_FUNCTIONAL_REQUIREMENTS} non-functional requirements"
        )
    if not _text(data.get("problem_statement")):
        raise ValueError("problem_statement missing")
    return {
        "problem_statement": _text(data.get("problem_statement")),
        "objective": _text(data.get("objective")),
        "in_scope": _strs(data.get("in_scope"), 16),
        "functional_requirements": functional,
        "non_functional_requirements": non_functional,
        "out_of_scope": _strs(data.get("out_of_scope"), 12),
    }


def _parse_solution(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("solution section not a dict")
    logical = data.get("logical_view") if isinstance(data.get("logical_view"), dict) else {}
    development = (
        data.get("development_view") if isinstance(data.get("development_view"), dict) else {}
    )
    deployment = (
        data.get("deployment_view") if isinstance(data.get("deployment_view"), dict) else {}
    )
    # The caps here and below are deliberately above what the prompts ask for: a model that
    # returns one extra layer or a couple of extra hops should have them drawn, not silently
    # dropped. `architecture.py` applies its own MAX_BOXES_PER_BAND / MAX_EDGES when laying out.
    layers = _dicts(logical.get("layers"), 9)
    if not _text(data.get("overview")):
        raise ValueError("overview missing")
    if len(layers) < 2:
        raise ValueError("logical view needs at least 2 layers")
    framework = (
        data.get("development_framework")
        if isinstance(data.get("development_framework"), dict)
        else {}
    )
    setup = (
        data.get("hardware_software_access")
        if isinstance(data.get("hardware_software_access"), dict)
        else {}
    )
    docker = (
        data.get("docker_deployment") if isinstance(data.get("docker_deployment"), dict) else {}
    )
    return {
        "overview": _text(data.get("overview")),
        "logical_view": {
            "description": _text(logical.get("description")),
            "layers": layers,
            "flows": _dicts(logical.get("flows"), 32),
        },
        "development_view": {
            "description": _text(development.get("description")),
            "modules": _dicts(development.get("modules"), 28),
        },
        "deployment_view": {
            "description": _text(deployment.get("description")),
            "nodes": _dicts(deployment.get("nodes"), 14),
            "connections": _dicts(deployment.get("connections"), 32),
        },
        "development_framework": {
            "description": _text(framework.get("description")),
            "components": _dicts(framework.get("components"), 20),
        },
        "hardware_software_access": {
            "hardware": _dicts(setup.get("hardware"), 10),
            "software": _dicts(setup.get("software"), 16),
            "access": _dicts(setup.get("access"), 14),
        },
        "docker_deployment": {
            "description": _text(docker.get("description")),
            "services": _dicts(docker.get("services"), 10),
            "steps": _strs(docker.get("steps"), 12),
        },
        "coding_best_practices": _dicts(data.get("coding_best_practices"), 16),
        "ai_guardrails": _dicts(data.get("ai_guardrails"), 16),
    }


def _parse_delivery(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("delivery section not a dict")
    acceptance = _dicts(data.get("acceptance_criteria"), 20)
    if len(acceptance) < _MIN_ACCEPTANCE_CRITERIA:
        raise ValueError(f"need at least {_MIN_ACCEPTANCE_CRITERIA} acceptance criteria")
    risks = _dicts(data.get("risks"), 14)
    if len(risks) < 3:
        raise ValueError("need at least 3 risks")
    return {
        "dependencies": _dicts(data.get("dependencies"), 16),
        "assumptions": _strs(data.get("assumptions"), 14),
        "challenges": _dicts(data.get("challenges"), 12),
        "risks": risks,
        "acceptance_criteria": acceptance,
    }


def _parse_solution_design_json(data: Any) -> dict[str, Any]:
    """Validate one merged document payload (single-call path and parallel merge)."""
    if not isinstance(data, dict):
        raise ValueError("solution design not a dict")
    return {**_parse_scope(data), **_parse_solution(data), **_parse_delivery(data)}


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

_TOC = [
    "| § | Section |",
    "| --- | --- |",
    "| 1 | [Project Summary](#1-project-summary) |",
    "| 1.1 | [Problem Statement](#11-problem-statement) |",
    "| 1.2 | [Objective](#12-objective) |",
    "| 2 | [Scope](#2-scope) |",
    "| 2.1 | [In-Scope](#21-in-scope) |",
    "| 2.2 | [Functional Requirements](#22-functional-requirements) |",
    "| 2.3 | [Non-Functional Requirements](#23-non-functional-requirements) |",
    "| 2.4 | [Out of Scope](#24-out-of-scope) |",
    "| 3 | [Solution Definition](#3-solution-definition) |",
    "| 3.1 | [Overview](#31-overview) |",
    "| 3.2 | [Architectural Views](#32-architectural-views) |",
    "| 3.2.1 | [Logical View](#321-logical-view) |",
    "| 3.2.2 | [Development View](#322-development-view) |",
    "| 3.2.3 | [Deployment View](#323-deployment-view) |",
    "| 3.3 | [Development Framework](#33-development-framework) |",
    "| 3.4 | [Setup and Configuration/Migration Requirements]"
    "(#34-setup-and-configurationmigration-requirements) |",
    "| 3.4.1 | [Hardware, Software, and Access Requirements]"
    "(#341-hardware-software-and-access-requirements) |",
    "| 3.4.2 | [Deployment with Docker](#342-deployment-with-docker) |",
    "| 3.5 | [Coding Best Practices](#35-coding-best-practices) |",
    "| 3.6 | [AI Guardrails & Data Security](#36-ai-guardrails--data-security) |",
    "| 4 | [Dependencies](#4-dependencies) |",
    "| 5 | [Assumptions](#5-assumptions) |",
    "| 6 | [Challenges and Risks](#6-challenges-and-risks) |",
    "| 6.1 | [Challenges](#61-challenges) |",
    "| 6.2 | [Risk](#62-risk) |",
    "| 7 | [Acceptance Criteria](#7-acceptance-criteria) |",
]


#: The §3.2 heading each drawn view is written under, keyed by the name it has in the stored
#: `architecture_views` payload. One definition because it is read in two directions: the
#: renderers below write these headings, and `_restore_descriptions` reads them back out of a
#: stored SDD to recover a description an older build cut. A drifted copy would silently turn
#: that recovery into a no-op, which is the failure mode hardest to notice.
_VIEW_HEADINGS = {
    "logical": "#### 3.2.1 Logical View",
    "development": "#### 3.2.2 Development View",
    "deployment": "#### 3.2.3 Deployment View",
}


def _render_logical_view(view: dict[str, Any]) -> list[str]:
    """§3.2.1 — the layered component figure, a responsibility table, and the permitted calls.

    The tables are not a duplicate of the picture. The figure answers "what is the shape of
    this", the tables answer "what exactly is in it" — and a table is searchable, diffable in a
    pull request, and readable by a screen reader, none of which a PNG is.
    """
    lines: list[str] = [_VIEW_HEADINGS["logical"], ""]
    lines.append(
        view["description"]
        or (
            "The logical view groups the solution into layers of responsibility. Each layer "
            "only calls the layer beneath it, which is what keeps a change to one layer from "
            "rippling through the rest."
        )
    )
    lines.append("")

    layers = view["layers"]
    lines.extend(_figure("logical"))

    rows: list[list[str]] = []
    part_rows: list[list[str]] = []
    for li, layer in enumerate(layers, start=1):
        name = _get(layer, "name", "layer", "title", default=f"Layer {li}")
        components = _components(layer.get("components") or layer.get("elements"), 12)
        rows.append(
            [
                name,
                ", ".join(_code(c["name"]) for c in components) or "—",
                _get(layer, "responsibility", "description", "purpose", default="—"),
            ]
        )
        part_rows.extend(
            [
                _code(c["name"]),
                name,
                _code(c["tech"]),
                c["detail"] or "—",
                _code(c["interface"]),
            ]
            for c in components
        )
    lines.extend(_md_table(["Layer", "Components", "Responsibility"], rows))
    lines.append("")

    # The per-component table, not just the per-layer one. A layer row says the solution has an
    # application layer; this says which four things are in it, what each is built with, what
    # each is responsible for, and what each exposes — the level at which a design review has
    # something to disagree with. It is also exactly what the boxes in Figure 3.2.1 carry.
    #
    # "Exposes" is the column that makes the rest checkable: a responsibility is a sentence
    # nobody can be held to, while a route, table or topic name is something a reviewer can
    # look for in the scaffold and find missing.
    if any(r[2] != "—" or r[3] != "—" or r[4] != "—" for r in part_rows):
        lines.append(
            "**Components in detail.** Every box in Figure 3.2.1, with what it is built with "
            "and what it exposes to the rest of the system:"
        )
        lines.append("")
        lines.extend(
            _md_table(
                ["Component", "Layer", "Built with", "Responsibility", "Exposes / contract"],
                part_rows[:48],
            )
        )
        lines.append("")

    flow_rows = [
        [
            _code(_get(flow, "from", "source", "src", default="—")),
            _code(_get(flow, "to", "target", "dst", default="—")),
            # "—" would read as "no answer"; an unlabelled arrow in a layered view is a
            # synchronous in-process call, which is worth saying rather than eliding.
            _get(flow, "label", "description", "protocol", default="in-process call"),
        ]
        for flow in view["flows"]
        if _get(flow, "from", "source", "src") and _get(flow, "to", "target", "dst")
    ]
    if flow_rows:
        lines.append("**Permitted calls.** Every arrow in Figure 3.2.1, in writing:")
        lines.append("")
        lines.extend(_md_table(["From", "To", "Carries"], flow_rows[:24]))
        lines.append("")
        lines.append(
            "A call not in this table is a call the design does not permit. That is the point of "
            "listing them: the review question becomes *is this edge here* rather than *does "
            "this feel layered*."
        )
        lines.append("")
    return lines


#: The layering every scaffold this exporter writes follows. Used when the model supplied no
#: usable `depends_on`, so the view still shows a direction instead of a row of loose boxes.
_IMPORT_CHAIN = (
    "main.py",
    "backend/api/",
    "backend/services/",
    "backend/repositories/",
    "backend/models/",
)


def _dev_packages(view: dict[str, Any], plan: ProjectPlan) -> list[dict[str, Any]]:
    """The Development View's packages, grouped from `plan.source_tree`.

    Deliberately not the model's idea of a folder layout: these are the paths this export
    actually writes, so the picture and the workspace cannot disagree. The model's contribution
    is the per-package responsibility text and the import edges, matched onto those paths.

    One function serves the table in §3.2.2, the stored payload, and the PNG drawn from it —
    so a reader comparing the figure against the table can never find them describing different
    trees.
    """
    paths = _scaffold_paths(plan)
    # Group by the first two path segments, so `backend/services/...` is one package and not
    # one box per file.
    groups: dict[str, list[str]] = {}
    for p in paths:
        parts = p.split("/")
        if len(parts) > 2:
            key = "/".join(parts[:2]) + "/"
        elif len(parts) == 2:
            key = parts[0] + "/"
        else:
            key = p
        groups.setdefault(key, []).append(p)

    # Keyed with and without the trailing slash because a model writes `backend/services` as
    # often as `backend/services/`, and the group keys always carry it.
    notes: dict[str, str] = {}
    deps: dict[str, list[str]] = {}
    for mod in view.get("modules") or []:
        raw = _get(mod, "path", "module", "name").replace("\\", "/").lstrip("/")
        if not raw:
            continue
        note = _get(mod, "responsibility", "description", "purpose")
        depends = _strs(mod.get("depends_on") or mod.get("dependencies"), 6)
        for variant in {raw, raw.rstrip("/"), raw.rstrip("/") + "/"}:
            if note:
                notes[variant.lower()] = note
            if depends:
                deps[variant.lower()] = depends

    def _as_group(raw: str) -> str:
        """A model-supplied path resolved to one of the real group keys, or ''."""
        cand = str(raw or "").replace("\\", "/").lstrip("/")
        for variant in (cand, cand.rstrip("/") + "/", cand.rstrip("/")):
            if variant in groups:
                return variant
        return ""

    def _lookup(key: str, table: dict[str, Any]) -> Any:
        return table.get(key.lower()) or table.get(key.rstrip("/").lower())

    packages: list[dict[str, Any]] = []
    for key in sorted(groups):
        resolved: list[str] = []
        for raw in _lookup(key, deps) or []:
            match = _as_group(raw)
            if match and match != key and match not in resolved:
                resolved.append(match)
        packages.append(
            {
                "path": key,
                "files": len(groups[key]),
                "top": key.split("/")[0] or key,
                "responsibility": _lookup(key, notes) or "",
                # The filenames, so the box in the figure and the row in the table both say
                # what is inside the package rather than only how much of it there is. Shortest
                # first: `routes.py` identifies a package, `document_ingest_pipeline.py` fills
                # the line.
                #
                # Five, not three: the figure now packs them across two lines, and three names
                # out of a nine-file package left the reader unable to tell whether the ones
                # they were looking for were absent or merely unlisted.
                "key_files": sorted(
                    (p.rsplit("/", 1)[-1] for p in groups[key]), key=lambda n: (len(n), n)
                )[:5],
                "depends_on": resolved,
            }
        )

    if not packages:
        # No scaffold yet (a plan generated before the source tree, or an empty tree): fall
        # back to the model's own module list so §3.2.2 still says something.
        for mod in (view.get("modules") or [])[:20]:
            path = _get(mod, "path", "module", "name").replace("\\", "/").lstrip("/")
            if not path:
                continue
            packages.append(
                {
                    "path": path,
                    "files": 0,
                    "top": path.split("/")[0] or path,
                    "responsibility": _get(mod, "responsibility", "description", "purpose"),
                    "key_files": _strs(mod.get("key_files") or mod.get("files"), 5),
                    "depends_on": _strs(mod.get("depends_on") or mod.get("dependencies"), 6),
                }
            )

    if not any(p["depends_on"] for p in packages):
        by_path = {p["path"]: p for p in packages}
        chain = [k for k in _IMPORT_CHAIN if k in by_path]
        for a, b in zip(chain, chain[1:]):
            by_path[a]["depends_on"] = [b]

    # The reverse edges, inverted here rather than in either consumer. "Imports" answers what
    # this package needs; "imported by" answers what breaks when it changes — which is the
    # question a developer actually arrives with, and the one neither the figure nor the table
    # could answer before. Computed after the `_IMPORT_CHAIN` fallback so the two agree.
    for p in packages:
        p["imported_by"] = sorted(
            other["path"] for other in packages if p["path"] in other["depends_on"]
        )

    return packages


def _render_development_view(view: dict[str, Any], plan: ProjectPlan) -> list[str]:
    """§3.2.2 — the code organisation figure plus the package table behind it."""
    lines: list[str] = [_VIEW_HEADINGS["development"], ""]
    lines.append(
        view["description"]
        or (
            "The development view is the code organisation a developer opens: which package "
            "owns which concern, and which direction the imports are allowed to run."
        )
    )
    lines.append("")
    lines.extend(_figure("development"))

    packages = _dev_packages(view, plan)
    rows = [
        [
            _code(p["path"]),
            str(p["files"]) if p["files"] else "—",
            # The names, so a developer can go straight to the file. The same five the figure's
            # box shows, so the picture and the table cannot be read as different trees.
            ", ".join(_code(f) for f in p.get("key_files") or []) or "—",
            ", ".join(_code(d) for d in p["depends_on"]) or "—",
            # The blast radius. A package with an empty cell here is a leaf or the entrypoint,
            # and saying "none" rather than "—" distinguishes that from missing data.
            ", ".join(_code(d) for d in p.get("imported_by") or []) or "none",
            p["responsibility"] or "See **File purposes** in README.md.",
        ]
        for p in packages[:24]
    ]
    lines.extend(
        _md_table(
            [
                "Package / path",
                "Files",
                "Key files",
                "Imports",
                "Imported by",
                "Responsibility",
            ],
            rows,
        )
    )
    lines.append("")
    lines.append(
        "Import direction is one-way down that chain. A route that reaches straight into a "
        "repository, or a service that opens its own database session, is the review finding "
        "this view exists to make obvious."
    )
    lines.append("")
    return lines


def _render_deployment_view(view: dict[str, Any]) -> list[str]:
    """§3.2.3 — runtime topology plus a node table."""
    lines: list[str] = [_VIEW_HEADINGS["deployment"], ""]
    lines.append(
        view["description"]
        or (
            "The deployment view is what runs where: one box per process, with the protocol, "
            "port and payload on every hop. It is the view an on-call engineer reads first."
        )
    )
    lines.append("")

    nodes = view["nodes"]
    lines.extend(_figure("deployment"))

    rows: list[list[str]] = []
    host_rows: list[list[str]] = []
    for node in nodes:
        node_name = _get(node, "name", "node", "component", default="—")
        hosts = _components(
            node.get("hosts") or node.get("contains") or node.get("components"), 12
        )
        rows.append(
            [
                node_name,
                _get(node, "runtime", "platform", "hosting", "environment", default="—"),
                ", ".join(_code(h["name"]) for h in hosts) or "—",
                _get(node, "scaling", "sizing", "notes", "availability", default="—"),
            ]
        )
        host_rows.extend(
            [
                _code(h["name"]),
                node_name,
                _code(h["tech"]),
                h["detail"] or "—",
                # Stateless or not is the first thing a runbook needs: it decides whether the
                # process can be restarted, replaced or scaled out without a plan.
                h["interface"] or "confirm — state not stated",
            ]
            for h in hosts
        )
    lines.extend(_md_table(["Node", "Runtime", "Hosts", "Scaling / notes"], rows))
    lines.append("")

    if any(r[2] != "—" or r[3] != "—" for r in host_rows):
        lines.append(
            "**Processes in detail.** Every box inside a node in Figure 3.2.3 — this is the "
            "list a runbook is written from:"
        )
        lines.append("")
        lines.extend(
            _md_table(
                ["Process", "Runs on", "Built with", "What it does", "State"],
                host_rows[:48],
            )
        )
        lines.append("")

    hop_rows = [
        [
            _code(_get(conn, "from", "source", "src", default="—")),
            _code(_get(conn, "to", "target", "dst", default="—")),
            # Not "—": this table is read as the security-group list, so an unknown protocol
            # has to read as an open question rather than as an empty cell.
            _get(conn, "protocol", "label", "transport", "description", default="TCP — confirm port"),
            _get(conn, "port", "ports", default="confirm"),
            _get(conn, "data", "carries", "payload", "purpose", default="—"),
        ]
        for conn in view["connections"]
        if _get(conn, "from", "source", "src") and _get(conn, "to", "target", "dst")
    ]
    if hop_rows:
        lines.append(
            "**Network hops.** Every arrow in Figure 3.2.3, with its protocol, port and payload:"
        )
        lines.append("")
        lines.extend(
            _md_table(["From", "To", "Protocol", "Port", "Carries"], hop_rows[:32])
        )
        lines.append("")
        lines.append(
            "This table is the security group and network-policy list. A hop that is not here "
            "should not be reachable."
        )
        lines.append("")
    return lines


# ---------------------------------------------------------------------------
# The structure behind the pictures
#
# `services/diagrams/architecture.py` draws from a normalised payload rather than from this
# module's raw model output, so the renderer never has to know which of `from`/`source`/`src` a
# particular model chose. Building it here — and storing it on the plan — is what lets the PNGs
# be re-drawn later, by a newer renderer or for a second export, without another model call.
# ---------------------------------------------------------------------------


def architecture_views_payload(data: dict[str, Any], plan: ProjectPlan) -> dict[str, Any]:
    """Normalise the parsed §3.2 content into the shape `architecture.py` draws."""
    logical = data.get("logical_view") if isinstance(data.get("logical_view"), dict) else {}
    development = (
        data.get("development_view") if isinstance(data.get("development_view"), dict) else {}
    )
    deployment = (
        data.get("deployment_view") if isinstance(data.get("deployment_view"), dict) else {}
    )

    layers: list[dict[str, Any]] = []
    for li, layer in enumerate(_dicts(logical.get("layers"), 9), start=1):
        name = _get(layer, "name", "layer", "title", default=f"Layer {li}")
        layers.append(
            {
                "name": name,
                "components": _components(layer.get("components") or layer.get("elements"), 12),
                "responsibility": _get(layer, "responsibility", "description", "purpose"),
            }
        )

    # An edge is only drawable if both ends name something the picture contains — a component
    # box or the layer band itself. Anything else is silently dropped rather than left to
    # resolve to nothing at draw time.
    drawable = {c["name"].lower() for layer in layers for c in layer["components"]}
    drawable |= {layer["name"].lower() for layer in layers}
    flows: list[dict[str, str]] = []
    for flow in _dicts(logical.get("flows"), 32):
        src = _get(flow, "from", "source", "src")
        dst = _get(flow, "to", "target", "dst")
        if src.lower() in drawable and dst.lower() in drawable and src.lower() != dst.lower():
            flows.append(
                {
                    "from": src,
                    "to": dst,
                    "label": _get(flow, "label", "description", "protocol"),
                }
            )
    if not flows:
        # No usable flows: chain the layers top-down, the one relationship a layered view
        # always has. A picture of disconnected boxes is worse than a coarse one.
        anchors = [_anchor(layer["components"], layer["name"]) for layer in layers]
        flows = [{"from": a, "to": b, "label": ""} for a, b in zip(anchors, anchors[1:])]

    nodes: list[dict[str, Any]] = []
    for ni, node in enumerate(_dicts(deployment.get("nodes"), 9), start=1):
        nodes.append(
            {
                "name": _get(node, "name", "node", "component", default=f"Node {ni}"),
                "runtime": _get(node, "runtime", "platform", "hosting", "environment"),
                "hosts": _components(
                    node.get("hosts") or node.get("contains") or node.get("components"), 12
                ),
                "scaling": _get(node, "scaling", "sizing", "notes", "availability"),
            }
        )
    reachable = {h["name"].lower() for node in nodes for h in node["hosts"]}
    reachable |= {node["name"].lower() for node in nodes}
    connections: list[dict[str, str]] = []
    for conn in _dicts(deployment.get("connections"), 32):
        src = _get(conn, "from", "source", "src")
        dst = _get(conn, "to", "target", "dst")
        if src.lower() in reachable and dst.lower() in reachable and src.lower() != dst.lower():
            connections.append(
                {
                    "from": src,
                    "to": dst,
                    "protocol": _get(conn, "protocol", "label", "transport", "description"),
                    # Kept as their own fields rather than folded into `protocol`: the renderer
                    # joins them onto the arrow, and §3.2.3's table gives each its own column.
                    "port": _get(conn, "port", "ports"),
                    "data": _get(conn, "data", "carries", "payload", "purpose"),
                }
            )
    if not connections and len(nodes) > 1:
        anchors = [_anchor(node["hosts"], node["name"]) for node in nodes]
        connections = [
            {"from": a, "to": b, "protocol": "", "port": "", "data": ""}
            for a, b in zip(anchors, anchors[1:])
        ]

    # The descriptions are stored whole. They used to be cut to 400 characters here, which is
    # both shorter than the header can draw and shorter than these paragraphs run: every one of
    # the three views in a real project lost its last clause, and the reader saw the loss as an
    # unfinished sentence rather than as a shortening. The renderer already bounds a description
    # twice — `architecture.py::_prose` at 1100 characters and `_wrap` at `HEADER_SUB_MAX_LINES`
    # lines — and both mark what they take, so bounding it a third time here bought nothing but
    # the defect. §3.2's prose reads the same field, and it wants the whole paragraph anyway.
    return {
        "logical": {
            "description": _text(logical.get("description")),
            "layers": layers,
            "flows": flows,
        },
        "development": {
            "description": _text(development.get("description")),
            "groups": _dev_packages(development, plan),
        },
        "deployment": {
            "description": _text(deployment.get("description")),
            "nodes": nodes,
            "connections": connections,
        },
    }


def plan_architecture_views(plan: ProjectPlan | None) -> dict[str, Any]:
    """The views to draw for a plan, falling back to a structure derived from the plan itself.

    A project generated before this feature existed, or one whose SDD was hand-edited, has no
    stored payload — and an export with a broken image link would be worse than a coarse
    picture. So the fallback re-derives the views from the brief-free deterministic payload,
    whose Development View is the real source tree either way.
    """
    if plan is None:
        return {}
    stored = getattr(plan, "architecture_views", None)
    if isinstance(stored, dict) and stored.get("development"):
        return _restore_descriptions(stored, getattr(plan, "solution_design", "") or "")
    brief = ProjectBrief(problem_statement=plan.summary or plan.project_name)
    return architecture_views_payload(_fallback_payload(brief, plan), plan)


def _looks_cut(text: str) -> bool:
    """Whether a stored paragraph reads as something a slice stopped rather than a full stop.

    Deliberately narrow. A long paragraph ending without terminal punctuation and without an
    ellipsis is the signature of the old 400-character slice; a short description, or one that
    ends in `.`/`!`/`?`/`…`/`:`, is left alone. Getting this wrong in the permissive direction
    would rewrite prose that was never cut, so the bar is high and the fallback is to do nothing.
    """
    text = text.strip()
    return len(text) > 200 and text[-1] not in ".!?…:;)\"'"


def _restore_descriptions(views: dict[str, Any], sdd: str) -> dict[str, Any]:
    """Put back a view description that an older build stored already cut.

    Projects generated before the cap came off carry a mid-word 400-character prefix on
    `architecture_views[*]["description"]` — and that string is what the drawn header shows, so
    re-rendering with a fixed renderer cannot help: the missing words are not in the payload.

    They are, however, in the payload's own sibling. §3.2 renders each description as the
    paragraph directly under its heading, uncut, and that markdown is stored on the same plan.
    So the whole sentence is recoverable, and this reads it back — accepting a paragraph only
    when the stored description is a prefix of it, which is what makes the match a repair rather
    than a guess. Anything else (no SDD, a hand-edited section, a heading that moved) leaves the
    payload exactly as it was found.

    Non-destructive: it returns a copy and never writes to the database. A plan re-generated or
    re-saved by current code stores the full description in the first place and this is a no-op.
    """
    if not sdd:
        return views
    out: dict[str, Any] = dict(views)
    for key, heading in _VIEW_HEADINGS.items():
        view = out.get(key)
        if not isinstance(view, dict):
            continue
        stored = str(view.get("description") or "")
        if not _looks_cut(stored):
            continue
        at = sdd.find(f"{heading}\n")
        if at < 0:
            continue
        paragraph = " ".join(
            sdd[at + len(heading) :].lstrip("\n").split("\n\n")[0].split()
        )
        if len(paragraph) > len(stored) and paragraph.startswith(stored):
            out[key] = {**view, "description": paragraph}
    return out


def _render_solution_design(
    data: dict[str, Any],
    plan: ProjectPlan,
    platform: Platform | None,
) -> str:
    """Render the validated payload into the exact 7-section outline."""
    plat = _platform_label(platform)
    name = plan.project_name or "Project"

    lines: list[str] = [
        f"# Solution Design Document — {name}",
        "",
        f"Target IDE: **{plat}**. Companion documents in this workspace: "
        "**README.md** (workspace and file layout) and **WORKBREAKDOWN.md** (phased "
        "implementation plan). This document says *what* is being built and *why it is "
        "shaped this way*; WORKBREAKDOWN.md says *in what order*.",
        "",
        *_TOC,
        "",
        "---",
        "",
        "## 1. Project Summary",
        "",
        "### 1.1 Problem Statement",
        "",
        data["problem_statement"] or "See the problem statement supplied with this project.",
        "",
        "### 1.2 Objective",
        "",
        data["objective"] or plan.summary.strip() or "Deliver the solution described above.",
        "",
        "## 2. Scope",
        "",
        "### 2.1 In-Scope",
        "",
    ]
    lines.extend(f"- {item}" for item in (data["in_scope"] or ["See functional requirements below."]))
    lines.extend(["", "### 2.2 Functional Requirements", ""])
    lines.append(
        "Each requirement is traceable to the problem statement and uploaded documents, and "
        "is delivered by one of the phases in `WORKBREAKDOWN.md`."
    )
    lines.append("")
    fr_rows = [
        [
            _get(fr, "id", "ref", default=f"FR-{i:02d}"),
            _get(fr, "requirement", "title", "name", default="—"),
            _get(fr, "description", "detail", "notes", default="—"),
            _get(fr, "priority", "moscow", default="Must"),
        ]
        for i, fr in enumerate(data["functional_requirements"], start=1)
    ]
    lines.extend(_md_table(["ID", "Requirement", "Description", "Priority"], fr_rows))
    lines.extend(["", "### 2.3 Non-Functional Requirements", ""])
    nfr_rows = [
        [
            _get(nfr, "id", "ref", default=f"NFR-{i:02d}"),
            _get(nfr, "category", "attribute", "quality", default="—"),
            _get(nfr, "requirement", "description", "title", default="—"),
            _get(nfr, "target", "measure", "metric", "acceptance", default="—"),
        ]
        for i, nfr in enumerate(data["non_functional_requirements"], start=1)
    ]
    lines.extend(_md_table(["ID", "Category", "Requirement", "Target / measure"], nfr_rows))
    lines.extend(["", "### 2.4 Out of Scope", ""])
    lines.append(
        "Named explicitly, because an unstated exclusion is read as a commitment:"
    )
    lines.append("")
    lines.extend(
        f"- {item}"
        for item in (
            data["out_of_scope"]
            or ["Anything not listed under **2.1 In-Scope** or **2.2 Functional Requirements**."]
        )
    )

    lines.extend(["", "## 3. Solution Definition", "", "### 3.1 Overview", ""])
    lines.append(data["overview"] or plan.summary.strip() or "See the architectural views below.")
    lines.extend(["", "### 3.2 Architectural Views", ""])
    lines.append(
        "Three views of one solution, in the order they are usually needed: what the parts "
        "are (**logical**), where the code for them lives (**development**), and what runs "
        "where at runtime (**deployment**)."
    )
    lines.append("")
    lines.append(
        f"Each view is a drawn diagram in `{ARCH_DOC_DIR}/`, followed by the same content as a "
        "table. The image is how the shape is read; the table is what is searched, diffed in a "
        "pull request, and read aloud by a screen reader. Re-exporting the workspace redraws "
        "the images from the current design, so they cannot drift from the text beside them."
    )
    lines.append("")
    lines.extend(_render_logical_view(data["logical_view"]))
    lines.extend(_render_development_view(data["development_view"], plan))
    lines.extend(_render_deployment_view(data["deployment_view"]))

    lines.extend(["", "### 3.3 Development Framework", ""])
    framework = data["development_framework"]
    lines.append(
        framework["description"]
        or "The stack this solution is built on, with the reason each choice was made."
    )
    lines.append("")
    fw_rows = [
        [
            _get(c, "layer", "area", "concern", default="—"),
            _get(c, "technology", "framework", "tool", "name", default="—"),
            _get(c, "version", "release", default="—"),
            _get(c, "rationale", "why", "reason", "notes", default="—"),
        ]
        for c in framework["components"]
    ]
    lines.extend(
        _md_table(["Layer", "Technology", "Version", "Rationale"], fw_rows)
        or ["See **Tech stack** in `WORKBREAKDOWN.md`."]
    )

    lines.extend(["", "### 3.4 Setup and Configuration/Migration Requirements", ""])
    lines.append(
        "What a new environment needs before the first deployment, and what has to be "
        "migrated into it."
    )
    lines.extend(["", "#### 3.4.1 Hardware, Software, and Access Requirements", ""])
    setup = data["hardware_software_access"]
    for heading, key, cols in (
        ("**Hardware / compute**", "hardware", ["Item", "Specification", "Purpose"]),
        ("**Software**", "software", ["Item", "Version", "Purpose"]),
        ("**Access / credentials**", "access", ["Item", "Granted to", "Purpose"]),
    ):
        entries = setup[key]
        if not entries:
            continue
        lines.append(heading)
        lines.append("")
        rows = [
            [
                _get(e, "item", "name", "component", "resource", default="—"),
                _get(e, "specification", "spec", "version", "sizing", "granted_to", "role", default="—"),
                _get(e, "purpose", "notes", "why", "description", default="—"),
            ]
            for e in entries
        ]
        lines.extend(_md_table(cols, rows))
        lines.append("")
    if not any(setup[k] for k in ("hardware", "software", "access")):
        lines.append("- See `.env.example` in this workspace for every configuration key.")
        lines.append("")
    lines.append(
        "Configuration is read from environment variables only — never a committed constant. "
        "Every key the application reads appears in `.env.example` with an empty value; a "
        "real value belongs in a secrets manager, not in the repository."
    )

    lines.extend(["", "#### 3.4.2 Deployment with Docker", ""])
    docker = data["docker_deployment"]
    lines.append(
        docker["description"]
        or (
            "The solution ships as container images so the environment a developer runs and "
            "the one that serves traffic are the same artifact."
        )
    )
    lines.append("")
    svc_rows = [
        [
            _get(s, "name", "service", default="—"),
            _get(s, "image", "base_image", "build", default="—"),
            _get(s, "ports", "port", default="—"),
            _get(s, "notes", "purpose", "description", "volumes", default="—"),
        ]
        for s in docker["services"]
    ]
    lines.extend(_md_table(["Service", "Image / build", "Ports", "Notes"], svc_rows))
    if docker["services"]:
        lines.append("")
    steps = docker["steps"] or [
        "Build each image from its own Dockerfile with the environment's build arguments.",
        "Run the stack locally with `docker compose up --build` and confirm the health endpoint.",
        "Push the tagged images to the registry the environment pulls from.",
        "Deploy, then verify the health check and one real request end to end.",
    ]
    lines.append("Deployment steps:")
    lines.append("")
    lines.extend(f"{i}. {step}" for i, step in enumerate(steps, start=1))
    lines.append("")
    lines.append(
        "Two container facts worth stating, because both are asked again later: a health "
        "check runs **inside** the container, so it targets loopback — the only address "
        "guaranteed to reach the server it is testing; and the server **binds** to "
        "`0.0.0.0`, which is the separate, opposite setting that makes the port reachable "
        "from outside. State that is expected to survive a redeploy lives on a mounted "
        "volume or a managed service, never on the container filesystem."
    )

    lines.extend(["", "### 3.5 Coding Best Practices", ""])
    practices = data["coding_best_practices"]
    if practices:
        rows = [
            [
                _get(p, "area", "topic", "category", default="—"),
                _get(p, "practice", "rule", "guideline", "description", default="—"),
                _get(p, "why", "rationale", "reason", "notes", default="—"),
            ]
            for p in practices
        ]
        lines.extend(_md_table(["Area", "Practice", "Why it matters"], rows))
    else:
        lines.extend(
            [
                "- Keep routes thin: validate with a schema, call a service, return a DTO.",
                "- Only repositories run queries; services never open a database session.",
                "- Read configuration through one settings module; never hardcode a secret.",
                "- Add or update tests in the same change as the code they cover.",
            ]
        )
    lines.append("")
    lines.append(
        "These are enforced rather than suggested: the rules exported under this "
        "workspace's IDE folder apply them while you edit, so a violation is caught in the "
        "editor instead of in review."
    )

    lines.extend(["", "### 3.6 AI Guardrails & Data Security", ""])
    guardrails = data["ai_guardrails"]
    if guardrails:
        rows = [
            [
                _get(g, "control", "guardrail", "name", "area", default="—"),
                _get(g, "implementation", "how", "description", "practice", default="—"),
                _get(g, "risk_addressed", "risk", "why", "threat", default="—"),
            ]
            for g in guardrails
        ]
        lines.extend(_md_table(["Control", "Implementation", "Risk addressed"], rows))
    else:
        lines.extend(
            [
                "- Never send secrets, credentials or personal data into a model prompt.",
                "- Treat every model output as untrusted input: validate it before it reaches "
                "a query, a shell, or a rendered page.",
                "- Log prompt and response metadata, not prompt and response bodies.",
                "- Keep a human approval step in front of any irreversible action.",
            ]
        )
    lines.append("")
    lines.append(
        "Data security is not a separate phase: parameterised queries, output encoding, "
        "authorisation on every mutating call, and no secrets or PII in logs are acceptance "
        "criteria on each phase in `WORKBREAKDOWN.md`, not a review at the end."
    )

    lines.extend(["", "## 4. Dependencies", ""])
    dep_rows = [
        [
            _get(d, "name", "dependency", "item", default="—"),
            _get(d, "type", "kind", "category", default="—"),
            _get(d, "owner", "provider", "team", default="—"),
            _get(d, "impact_if_delayed", "impact", "notes", "description", default="—"),
        ]
        for d in data["dependencies"]
    ]
    lines.extend(
        _md_table(["Dependency", "Type", "Owner", "Impact if unavailable"], dep_rows)
        or ["- No external dependency blocks the first phase."]
    )

    lines.extend(["", "## 5. Assumptions", ""])
    lines.append(
        "Each of these is a decision taken in the absence of a confirmed answer. If one turns "
        "out to be wrong, the design changes — so they are listed rather than buried."
    )
    lines.append("")
    lines.extend(
        f"- {a}"
        for a in (
            data["assumptions"]
            or ["Requirements are as stated in the problem statement and uploaded documents."]
        )
    )

    lines.extend(["", "## 6. Challenges and Risks", "", "### 6.1 Challenges", ""])
    challenges = data["challenges"]
    if challenges:
        rows = [
            [
                _get(c, "challenge", "title", "name", "description", default="—"),
                _get(c, "mitigation", "approach", "plan", "notes", default="—"),
            ]
            for c in challenges
        ]
        lines.extend(_md_table(["Challenge", "How it is handled"], rows))
    else:
        lines.append("- None identified beyond the risks in **6.2**.")
    lines.extend(["", "### 6.2 Risk", ""])
    risk_rows = [
        [
            _get(r, "id", "ref", default=f"R-{i:02d}"),
            _get(r, "risk", "description", "title", default="—"),
            _get(r, "likelihood", "probability", default="Medium"),
            _get(r, "impact", "severity", "consequence", default="Medium"),
            _get(r, "mitigation", "response", "plan", default="—"),
            _get(r, "owner", "responsible", default="Delivery lead"),
        ]
        for i, r in enumerate(data["risks"], start=1)
    ]
    lines.extend(
        _md_table(["ID", "Risk", "Likelihood", "Impact", "Mitigation", "Owner"], risk_rows)
    )

    lines.extend(["", "## 7. Acceptance Criteria", ""])
    lines.append(
        "The solution is accepted when every criterion below passes by the stated method. "
        "Each maps to a phase gate in `WORKBREAKDOWN.md`, so nothing here is verified for the "
        "first time at the end."
    )
    lines.append("")
    ac_rows = [
        [
            _get(a, "id", "ref", default=f"AC-{i:02d}"),
            _get(a, "criterion", "criteria", "requirement", "description", default="—"),
            _get(a, "verification", "how", "method", "evidence", default="—"),
        ]
        for i, a in enumerate(data["acceptance_criteria"], start=1)
    ]
    lines.extend(_md_table(["ID", "Criterion", "Verification method"], ac_rows))
    lines.append("")
    lines.append(f"_Generated for {plat} by AgentCraft from this project's brief and documents._")
    lines.append("")

    return _normalize_markdown("\n".join(lines), name)


def _normalize_markdown(md: str, project_name: str) -> str:
    text = (md or "").strip()
    if not text:
        return ""
    if not text.startswith("#"):
        text = f"# Solution Design Document — {project_name}\n\n{text}"
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    return text.rstrip() + "\n"


# ---------------------------------------------------------------------------
# Completeness validation
# ---------------------------------------------------------------------------

#: Every heading the outline promises. Order matters: the document is checked for the
#: headings *in sequence*, so a model that renumbers or drops 3.4.1 fails rather than
#: passing on a coincidental substring match.
REQUIRED_HEADINGS: tuple[str, ...] = (
    "## 1. Project Summary",
    "### 1.1 Problem Statement",
    "### 1.2 Objective",
    "## 2. Scope",
    "### 2.1 In-Scope",
    "### 2.2 Functional Requirements",
    "### 2.3 Non-Functional Requirements",
    "### 2.4 Out of Scope",
    "## 3. Solution Definition",
    "### 3.1 Overview",
    "### 3.2 Architectural Views",
    "#### 3.2.1 Logical View",
    "#### 3.2.2 Development View",
    "#### 3.2.3 Deployment View",
    "### 3.3 Development Framework",
    "### 3.4 Setup and Configuration/Migration Requirements",
    "#### 3.4.1 Hardware, Software, and Access Requirements",
    "#### 3.4.2 Deployment with Docker",
    "### 3.5 Coding Best Practices",
    "### 3.6 AI Guardrails & Data Security",
    "## 4. Dependencies",
    "## 5. Assumptions",
    "## 6. Challenges and Risks",
    "### 6.1 Challenges",
    "### 6.2 Risk",
    "## 7. Acceptance Criteria",
)

#: A §3.2 that lost a figure still reads as prose, so nothing else in the document would flag
#: it. Checked by path rather than by counting `![` so a stray image elsewhere cannot stand in
#: for a missing architectural view.
_REQUIRED_FIGURES = ARCH_FIGURE_REFS


def missing_solution_design_sections(md: str) -> list[str]:
    """Headings absent or out of order — used by the validator and by the verify script."""
    text = md or ""
    missing: list[str] = []
    cursor = 0
    for heading in REQUIRED_HEADINGS:
        idx = text.find("\n" + heading, cursor)
        if idx < 0 and text.startswith(heading):
            idx = 0
        if idx < 0:
            missing.append(heading)
            continue
        cursor = idx + len(heading)
    return missing


def is_solution_design_complete(md: str) -> bool:
    """True when every numbered section is present, in order, and the tables closed."""
    text = (md or "").strip()
    if len(text) < _MIN_DOCUMENT_LENGTH:
        return False
    if missing_solution_design_sections(text):
        return False
    if any(ref not in text for ref in _REQUIRED_FIGURES):
        return False
    # An odd fence count means a code block or table was cut off mid-output.
    if text.count("```") % 2 != 0:
        return False
    if not re.search(r"^\|\s*ID\s*\|", text, re.MULTILINE):
        return False
    last_line = text.splitlines()[-1].strip()
    if re.match(r"^#{1,4}\s*$", last_line):
        return False
    return True


# ---------------------------------------------------------------------------
# Plan persistence
# ---------------------------------------------------------------------------


def apply_solution_design_to_plan(
    plan: ProjectPlan,
    sdd: str,
    *,
    used_llm: bool,
    complete: bool,
    views: dict[str, Any] | None = None,
) -> ProjectPlan:
    """Persist the SDD on the plan AND in `file_overrides`, so preview/export agree.

    `views` is the payload §3.2's three PNGs are drawn from. Stored alongside the markdown
    because the document references the images by path: without it, a re-export would have to
    call the model again to know what to draw.
    """
    overrides = dict(plan.file_overrides or {})
    overrides[SDD_FILENAME] = sdd
    update: dict[str, Any] = {
        "solution_design": sdd,
        "solution_design_llm": used_llm and complete,
        "solution_design_complete": complete,
        "file_overrides": overrides,
    }
    if views:
        update["architecture_views"] = views
    return plan.model_copy(update=update)


def plan_solution_design_body(plan: ProjectPlan) -> str:
    overrides = plan.file_overrides or {}
    if overrides.get(SDD_FILENAME):
        return str(overrides[SDD_FILENAME])
    return (getattr(plan, "solution_design", "") or "").strip()


def plan_needs_solution_design(plan: ProjectPlan | None) -> bool:
    if not plan:
        return True
    return not plan_solution_design_body(plan)


def plan_needs_detailed_solution_design(plan: ProjectPlan | None) -> bool:
    """True when `SDD.md` is missing or structurally incomplete."""
    if not plan:
        return True
    body = plan_solution_design_body(plan)
    if not body:
        return True
    return not is_solution_design_complete(body)


# ---------------------------------------------------------------------------
# Deterministic fallback — no Bedrock
# ---------------------------------------------------------------------------


def _fallback_payload(brief: ProjectBrief, plan: ProjectPlan) -> dict[str, Any]:
    """
    A complete, honest document from the brief and the scaffold alone.

    Every field is derived from something the project actually has — the problem
    statement, the declared stack, the domains, the integrations, the exported paths — so
    the no-LLM document is thinner than the generated one but never invented.
    """
    problem = brief.problem_statement.strip() or plan.summary.strip()
    stack = brief.tech_stack.strip()
    domains = [d for d in (brief.domains or []) if str(d).strip()]
    integrations = [i for i in (brief.integrations or []) if str(i).strip()]
    paths = _scaffold_paths(plan)
    has_frontend = any(p.startswith("frontend/") for p in paths)

    functional = [
        {
            "id": f"FR-{i:02d}",
            "requirement": d,
            "description": f"Deliver the `{d}` capability described in the project documents.",
            "priority": "Must",
        }
        for i, d in enumerate(domains[:10], start=1)
    ]
    next_id = len(functional) + 1
    for integ in integrations[:6]:
        functional.append(
            {
                "id": f"FR-{next_id:02d}",
                "requirement": f"Integrate with {integ}",
                "description": f"Implement and error-handle the {integ} integration named in the brief.",
                "priority": "Must",
            }
        )
        next_id += 1
    baseline = [
        ("Authentication and authorisation", "Sign-in plus role checks on every mutating call."),
        ("Core domain APIs", "Create, read, update and list endpoints for the primary entities."),
        ("Persistence", "Schema, migrations and repositories for those entities."),
        ("Audit and observability", "Structured logs and a health endpoint the platform can poll."),
        ("Automated tests", "Unit and API tests covering each requirement above."),
    ]
    for title, desc in baseline:
        if len(functional) >= _MIN_FUNCTIONAL_REQUIREMENTS + 4:
            break
        functional.append(
            {
                "id": f"FR-{next_id:02d}",
                "requirement": title,
                "description": desc,
                "priority": "Must",
            }
        )
        next_id += 1

    non_functional = [
        {"id": "NFR-01", "category": "Security", "requirement": "No injection, XSS, IDOR or secret in a log line.", "target": "Zero High/Critical findings before release."},
        {"id": "NFR-02", "category": "Performance", "requirement": "Interactive endpoints respond quickly under expected load.", "target": "p95 under 500 ms for read endpoints."},
        {"id": "NFR-03", "category": "Availability", "requirement": "A single instance failure does not take the service down.", "target": "Two or more instances behind a load balancer."},
        {"id": "NFR-04", "category": "Maintainability", "requirement": "One concern per layer; imports run one way only.", "target": "No route touching a repository directly."},
        {"id": "NFR-05", "category": "Observability", "requirement": "Every request and failure is traceable without reading a body.", "target": "Structured logs with a correlation id; no PII."},
        {"id": "NFR-06", "category": "Data protection", "requirement": "Secrets and personal data are never committed or logged.", "target": "All secrets from the environment; `.env` gitignored."},
    ]

    # The `tech` line under each box in Figure 3.2.1 quotes the brief rather than guessing: a
    # term only appears if the project declared it. An empty string draws no second line, which
    # is the honest outcome for a brief that named no stack at all.
    declared = [t for t in re.split(r"[,/+|]| and ", stack) if t.strip()]

    def _declared(*keywords: str) -> str:
        for term in declared:
            low = term.lower()
            if any(k in low for k in keywords):
                return term.strip()
        return ""

    ui_tech = _declared("angular", "react", "vue", "svelte", "next", "html")
    api_tech = _declared("fastapi", "django", "flask", "express", "spring", "node", ".net")
    db_tech = _declared("postgres", "mysql", "sqlite", "mongo", "dynamo", "oracle", "sql")

    layers: list[dict[str, Any]] = []
    if has_frontend:
        layers.append(
            {
                "name": "Presentation",
                "components": [
                    {"name": "Web UI", "tech": ui_tech, "detail": "Screens and forms per role", "interface": "one route per screen"},
                    {"name": "API client", "tech": ui_tech, "detail": "One typed call site per endpoint", "interface": "one method per endpoint"},
                    {"name": "Route guards", "tech": ui_tech, "detail": "Blocks a screen the role cannot open", "interface": "role check before navigation"},
                ],
                "responsibility": "Renders the workflows for each role and calls the API; holds no business rules.",
            }
        )
    layers.extend(
        [
            {
                "name": "API",
                "components": [
                    {"name": "Routes", "tech": api_tech, "detail": "One handler per endpoint", "interface": "the HTTP surface — every path and method"},
                    {"name": "Request/response schemas", "tech": _declared("pydantic", "zod") or api_tech, "detail": "Rejects bad input at the edge", "interface": "one schema per request and response body"},
                    {"name": "Auth dependencies", "tech": _declared("jwt", "oauth", "oidc") or api_tech, "detail": "Identity and role on every call", "interface": "caller identity + role, injected per request"},
                    {"name": "Error handlers", "tech": api_tech, "detail": "One shape for every failure", "interface": "a single error body for every status"},
                ],
                "responsibility": "Validates input, authorises the caller, delegates to a service, returns a DTO.",
            },
            {
                "name": "Domain services",
                "components": [
                    {"name": "Service modules", "tech": _declared("python", "typescript", "java", "c#", "go"), "detail": "Every business rule lives here", "interface": "one function per use case, no HTTP types"},
                    {"name": "Validation rules", "tech": "", "detail": "What the data is allowed to be", "interface": "raises a domain error, never an HTTP one"},
                    {"name": "Orchestration", "tech": "", "detail": "Multi-step work, one transaction", "interface": "owns the transaction boundary"},
                ],
                "responsibility": "All business rules. The only layer that decides anything.",
            },
            {
                "name": "Persistence",
                "components": [
                    {"name": "Repositories", "tech": _declared("sqlalchemy", "prisma", "hibernate", "orm"), "detail": "The only code that queries", "interface": "one repository per aggregate"},
                    {"name": "ORM models", "tech": _declared("sqlalchemy", "prisma", "hibernate", "orm"), "detail": "Tables as classes", "interface": "the table definitions — the schema of record"},
                    {"name": "Migrations", "tech": _declared("alembic", "flyway", "liquibase"), "detail": "Schema change, versioned", "interface": "one ordered, reversible script per change"},
                ],
                "responsibility": "The only layer that issues queries. Parameterised access only.",
            },
            {
                "name": "Platform",
                "components": [
                    {"name": "Database", "tech": db_tech, "detail": "The system of record", "interface": "stateful — the only durable store"},
                    {"name": "Configuration", "tech": "", "detail": "Environment only, never committed", "interface": "read once at startup; `.env` gitignored"},
                    {"name": "Logging", "tech": "", "detail": "Structured, correlation id, no PII", "interface": "one JSON line per request, to stdout"},
                ]
                + (
                    [{"name": integrations[0], "tech": "", "detail": "External integration from the brief", "interface": "outbound only — no inbound callback"}]
                    if integrations
                    else []
                ),
                "responsibility": "Managed services and cross-cutting concerns the layers above depend on.",
            },
        ]
    )
    # Labelled, because an unlabelled arrow is exactly the "high level" the reader complains
    # about: the label says what crosses the boundary, not merely that something does.
    labels = [
        "HTTPS request",
        "Validated command",
        "Query / write",
        "SQL over a pooled connection",
    ]
    flows: list[dict[str, str]] = []
    chain = [str(layer["components"][0]["name"]) for layer in layers]
    for i, (a, b) in enumerate(zip(chain, chain[1:])):
        flows.append({"from": a, "to": b, "label": labels[i] if i < len(labels) else ""})

    # Used only when `plan.source_tree` is empty — but "Business rules." is precisely the
    # one-phrase answer that makes a Development View read as high-level, so the fallback says
    # what each package owns *and* what it is not allowed to do. The prohibition is the useful
    # half: it is what a reviewer can find violated.
    modules = [
        {"path": "main.py", "responsibility": "Process entrypoint: loads configuration, builds the app, starts the server. Contains no business rule.", "depends_on": ["backend/api/"], "key_files": ["main.py", "config.py"]},
        {"path": "backend/api/", "responsibility": "The HTTP surface: one handler per endpoint, request validation, the auth dependency and the error shape. Delegates every decision downward.", "depends_on": ["backend/services/"], "key_files": ["routes.py", "deps.py", "errors.py"]},
        {"path": "backend/services/", "responsibility": "Every business rule and the transaction boundary around multi-step work. Sees no HTTP type and opens no database session of its own.", "depends_on": ["backend/repositories/"], "key_files": ["service.py"]},
        {"path": "backend/repositories/", "responsibility": "The only code that issues a query. Parameterised access, one repository per aggregate, no rule about what the data means.", "depends_on": ["backend/models/"], "key_files": ["repository.py", "session.py"]},
        {"path": "backend/models/", "responsibility": "The schema of record: ORM entities, the request/response contracts and the migrations that version them. Imports nothing above it.", "depends_on": [], "key_files": ["models.py", "schemas.py", "migrations/"]},
    ]

    nodes = [
        {
            "name": "Client",
            "runtime": "Browser",
            # `interface` on a deployment process is its state: whether it can be restarted,
            # replaced or scaled out without a plan. It is the fact the figure draws on the
            # fourth line of the box and §3.2.3's process table carries as its last column.
            "hosts": (
                [{"name": "Web UI", "tech": ui_tech, "detail": "Static bundle, no secrets", "interface": "stateless — cacheable, no server session"}]
                if has_frontend
                else [{"name": "API consumer", "tech": "", "detail": "Calls the API with a bearer token", "interface": "holds only its own token"}]
            ),
            "scaling": "n/a",
        },
        {
            "name": "Edge",
            "runtime": "Load balancer",
            "hosts": [
                {"name": "TLS termination", "tech": "", "detail": "Certificate ends here", "interface": "stateless — certificate from the managed store"},
                {"name": "Routing", "tech": "", "detail": "Path to target group", "interface": "stateless — rules are configuration"},
                {"name": "Health checks", "tech": "", "detail": "Pulls an unhealthy replica out", "interface": "stateless — probes `/health`"},
            ],
            "scaling": "Managed.",
        },
        {
            "name": "Application",
            "runtime": "Container",
            "hosts": [{"name": "API service", "tech": api_tech, "detail": "Stateless, two or more replicas", "interface": "stateless — nothing on the container disk"}]
            + ([{"name": "UI service", "tech": ui_tech, "detail": "Serves the built frontend", "interface": "stateless — immutable build artefact"}] if has_frontend else []),
            "scaling": "Horizontal — two or more replicas.",
        },
        {
            "name": "Data",
            "runtime": "Managed database",
            "hosts": [
                {"name": "Primary datastore", "tech": db_tech, "detail": "System of record; encrypted at rest", "interface": "stateful — the only durable copy"},
                {"name": "Automated backups", "tech": "", "detail": "Point-in-time restore", "interface": "stateful — retained per the backup policy"},
            ],
            "scaling": "Vertical plus backups; state never on the container filesystem.",
        },
    ]
    connections = [
        {"from": "Web UI" if has_frontend else "API consumer", "to": "TLS termination", "protocol": "HTTPS", "port": "443", "data": "Signed-in user requests"},
        {"from": "Routing", "to": "API service", "protocol": "HTTP", "port": "8000", "data": "Forwarded request, private subnet only"},
        {"from": "API service", "to": "Primary datastore", "protocol": "TCP / driver", "port": "5432", "data": "Parameterised queries and writes"},
        {"from": "Health checks", "to": "API service", "protocol": "HTTP", "port": "8000", "data": "`/health` probe"},
        {"from": "Primary datastore", "to": "Automated backups", "protocol": "Managed", "port": "n/a", "data": "Snapshots and WAL"},
    ]

    framework_components = [
        {"layer": "Backend", "technology": stack or "FastAPI + Pydantic", "version": "—", "rationale": "As declared in the project brief."},
    ]
    if has_frontend:
        framework_components.append(
            {"layer": "Frontend", "technology": "SPA framework as scaffolded under `frontend/`", "version": "—", "rationale": "Matches the exported scaffold."}
        )
    framework_components.extend(
        [
            {"layer": "Persistence", "technology": "Relational database with migrations", "version": "—", "rationale": "The entities in this brief are relational."},
            {"layer": "Testing", "technology": "pytest plus API-level tests", "version": "—", "rationale": "Each phase gate in WORKBREAKDOWN.md is verified by tests."},
        ]
    )

    return {
        "problem_statement": problem or "See the problem statement supplied with this project.",
        "objective": plan.summary.strip()
        or "Deliver a working, tested implementation of the requirements below.",
        "in_scope": (domains[:10] or ["Every functional requirement listed in 2.2."])
        + ([f"Integration with {i}" for i in integrations[:5]]),
        "functional_requirements": functional,
        "non_functional_requirements": non_functional,
        "out_of_scope": [
            "Anything not listed under 2.1 or 2.2.",
            "Data migration from systems not named in the brief.",
            "Non-functional targets beyond those stated in 2.3.",
        ],
        "overview": (
            plan.summary.strip()
            or "A layered application: a thin API over domain services over a repository layer."
        )
        + " Each layer is replaceable because it depends only on the layer beneath it.",
        "logical_view": {
            "description": "Layers of responsibility, with calls running one way only — downward.",
            "layers": layers,
            "flows": flows,
        },
        "development_view": {
            "description": "The package layout this workspace exports, and the import direction between packages.",
            "modules": modules,
        },
        "deployment_view": {
            "description": "Containerised application behind a load balancer, with state on a managed datastore.",
            "nodes": nodes,
            "connections": connections,
        },
        "development_framework": {
            "description": stack or "The stack declared in the project brief.",
            "components": framework_components,
        },
        "hardware_software_access": {
            "hardware": [
                {"item": "Application host", "specification": "2 vCPU / 4 GB per replica", "purpose": "Runs the API container."},
                {"item": "Database host", "specification": "Managed instance with backups", "purpose": "Persistent state."},
            ],
            "software": [
                {"item": "Container runtime", "version": "Docker 24+", "purpose": "Build and run the images."},
                {"item": "Language runtime", "version": "As declared in the brief", "purpose": "Executes the application."},
                {"item": "Database engine", "version": "As declared in the brief", "purpose": "Persistence."},
            ],
            "access": [
                {"item": "Source repository", "granted_to": "Every engineer on the team", "purpose": "Clone, branch, review."},
                {"item": "Container registry", "granted_to": "CI service account", "purpose": "Push and pull images."},
                {"item": "Secrets manager", "granted_to": "Deployment role only", "purpose": "Inject configuration at start-up."},
            ]
            + [
                {"item": f"{i} credentials", "granted_to": "Application runtime", "purpose": "Named integration in the brief."}
                for i in integrations[:4]
            ],
        },
        "docker_deployment": {
            "description": "One image per deployable, so the local and deployed environments are the same artifact.",
            "services": [
                {"name": "api", "image": "Built from the backend Dockerfile", "ports": "8000", "notes": "Health endpoint polled by the platform."},
            ]
            + (
                [{"name": "web", "image": "Built from the frontend Dockerfile", "ports": "80", "notes": "Static bundle served by nginx."}]
                if has_frontend
                else []
            )
            + [
                {"name": "db", "image": "Official database image", "ports": "5432", "notes": "Named volume — never the container filesystem."},
            ],
            "steps": [],
        },
        "coding_best_practices": [
            {"area": "Routes", "practice": "Validate with a schema, call a service, return a DTO.", "why": "Business rules in a route cannot be reused or tested in isolation."},
            {"area": "Persistence", "practice": "Only repositories issue queries, always parameterised.", "why": "One place to audit for injection and for N+1 queries."},
            {"area": "Configuration", "practice": "Read every value through one settings module.", "why": "A hardcoded secret ships to production and cannot be rotated."},
            {"area": "Errors", "practice": "Fail with a typed error the API layer maps to a status code.", "why": "A leaked stack trace is both a bad UX and an information disclosure."},
            {"area": "Tests", "practice": "Change code and its tests in the same commit.", "why": "A phase gate that can be passed without tests is not a gate."},
            {"area": "Logging", "practice": "Log identifiers and outcomes, never bodies or credentials.", "why": "Logs are the most commonly over-shared data store in any system."},
        ],
        "ai_guardrails": [
            {"control": "Prompt data minimisation", "implementation": "Send only the fields a task needs; never credentials or full personal records.", "risk_addressed": "Data leaving the trust boundary in a prompt."},
            {"control": "Output is untrusted input", "implementation": "Validate and encode model output before it reaches a query, a shell, or a page.", "risk_addressed": "Injection through generated content."},
            {"control": "Human approval gate", "implementation": "Any irreversible or outward-facing action requires an explicit confirmation.", "risk_addressed": "Autonomous action with real-world consequences."},
            {"control": "Prompt-injection resistance", "implementation": "Treat retrieved documents as data, not instructions; never let content change the system prompt.", "risk_addressed": "Instructions smuggled in through uploaded content."},
            {"control": "Audit trail", "implementation": "Record model, version, latency and a request id — not the prompt body.", "risk_addressed": "Untraceable AI decisions, and logs becoming a second copy of the data."},
            {"control": "Least-privilege credentials", "implementation": "The model-invoking role can call the model and nothing else.", "risk_addressed": "Lateral movement from a compromised AI path."},
        ],
        "dependencies": [
            {"name": i, "type": "External integration", "owner": "Integration owner", "impact_if_delayed": "The dependent requirement in 2.2 cannot be completed."}
            for i in integrations[:8]
        ]
        + [
            {"name": "Environment provisioning", "type": "Internal", "owner": "Platform team", "impact_if_delayed": "No target to deploy to; verification slips."},
            {"name": "Credentials and secrets", "type": "Internal", "owner": "Security team", "impact_if_delayed": "The application cannot start in a real environment."},
        ],
        "assumptions": [
            brief.constraints.strip() or "Constraints are as stated in the project documents.",
            "The stack in 3.3 is agreed and will not change mid-delivery.",
            "Requirements not present in the uploaded documents are out of scope.",
            "One environment is available for verification before release.",
        ],
        "challenges": [
            {"challenge": "Requirements arrive as prose documents rather than a specification.", "mitigation": "Each requirement in 2.2 carries an ID and is traceable to a phase in WORKBREAKDOWN.md."},
            {"challenge": "Integration behaviour is only knowable against the real system.", "mitigation": "Mock the contract for tests, then verify against the real endpoint in its own phase."},
            {"challenge": "Security work tends to be deferred to the end.", "mitigation": "Security checks are acceptance criteria on every phase gate, not a final review."},
        ],
        "risks": [
            {"id": "R-01", "risk": "A requirement is discovered late because it was implicit in a document.", "likelihood": "Medium", "impact": "High", "mitigation": "Re-read documents at each phase boundary; 2.2 is amended rather than worked around.", "owner": "Delivery lead"},
            {"id": "R-02", "risk": "An external integration is unavailable or its contract changes.", "likelihood": "Medium", "impact": "High", "mitigation": "Isolate behind an adapter; test against a recorded contract.", "owner": "Tech lead"},
            {"id": "R-03", "risk": "State is lost on redeployment because it was written to the container filesystem.", "likelihood": "Low", "impact": "Critical", "mitigation": "All persistent state on a volume or managed service; verified before the first release.", "owner": "Platform team"},
            {"id": "R-04", "risk": "A secret is committed or logged.", "likelihood": "Low", "impact": "Critical", "mitigation": "Secrets from the environment only; `.env` gitignored; secret scanning in CI.", "owner": "Security team"},
            {"id": "R-05", "risk": "Performance targets in 2.3 are missed under real data volumes.", "likelihood": "Medium", "impact": "Medium", "mitigation": "Load-test the read paths in the phase that delivers them, not at the end.", "owner": "Tech lead"},
        ],
        "acceptance_criteria": [
            {"id": "AC-01", "criterion": "Every requirement in 2.2 is implemented and demonstrable.", "verification": "Walk-through against the requirement table."},
            {"id": "AC-02", "criterion": "Every phase gate in WORKBREAKDOWN.md is closed.", "verification": "Checklists complete in the file."},
            {"id": "AC-03", "criterion": "Automated tests pass for every changed module.", "verification": "Test suite in CI."},
            {"id": "AC-04", "criterion": "No High or Critical security finding is open.", "verification": "Security review report."},
            {"id": "AC-05", "criterion": "The non-functional targets in 2.3 are measured, not assumed.", "verification": "Load-test and log evidence."},
            {"id": "AC-06", "criterion": "The service survives a redeployment with its data intact.", "verification": "Redeploy the environment and re-read the data."},
            {"id": "AC-07", "criterion": "No secret or personal data appears in a log line or the repository.", "verification": "Log sample plus a secret scan."},
        ],
    }


def fallback_solution_design(
    brief: ProjectBrief,
    plan: ProjectPlan,
    platform: Platform | None,
) -> str:
    """Deterministic, structurally complete SDD.md when Bedrock is unavailable."""
    return _render_solution_design(_fallback_payload(brief, plan), plan, platform)


def fallback_solution_design_views(brief: ProjectBrief, plan: ProjectPlan) -> dict[str, Any]:
    """The architecture payload behind the deterministic document."""
    return architecture_views_payload(_fallback_payload(brief, plan), plan)


# ---------------------------------------------------------------------------
# Bedrock generation — three parallel section groups, then a single-call fallback
#
# Every tier returns `(markdown, architecture views)`. The views are a pure function of the
# same payload the markdown is rendered from, so producing them here — rather than re-deriving
# them from the finished document — is what guarantees the PNGs match the tables beside them.
# ---------------------------------------------------------------------------


def _render_pair(
    data: dict[str, Any], plan: ProjectPlan, platform: Platform | None
) -> tuple[str, dict[str, Any]]:
    return (
        _render_solution_design(data, plan, platform),
        architecture_views_payload(data, plan),
    )


def _user_context(brief: ProjectBrief, plan: ProjectPlan, platform: Platform | None, limit: int) -> str:
    return (
        f"Platform: {_platform_label(platform)}\n"
        f"Project: {plan.project_name}\n"
        f"Summary: {plan.summary}\n"
        f"Declared tech stack: {brief.tech_stack or '(not stated — infer and say so)'}\n"
        f"Exported scaffold paths (use these verbatim in the development view):\n"
        f"{_scaffold_hint(plan)}\n\n"
        f"User documents and brief:\n{_brief_context(brief, limit)}\n"
    )


async def _fetch_scope(
    brief: ProjectBrief, plan: ProjectPlan, platform: Platform | None
) -> dict[str, Any]:
    from app.services.llm.client import llm_client

    system = _read_prompt("solution_design_scope.txt", "Return SDD sections 1-2 as JSON.")
    data = await llm_client.complete_json(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": _user_context(brief, plan, platform, 22000)},
        ],
        max_tokens=_SCOPE_MAX_TOKENS,
        temperature=0.2,
        timeout=240.0,
    )
    return _parse_scope(data)


async def _fetch_solution(
    brief: ProjectBrief, plan: ProjectPlan, platform: Platform | None
) -> dict[str, Any]:
    from app.services.llm.client import llm_client

    system = _read_prompt("solution_design_solution.txt", "Return SDD section 3 as JSON.")
    data = await llm_client.complete_json(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": _user_context(brief, plan, platform, 22000)},
        ],
        max_tokens=_SOLUTION_MAX_TOKENS,
        temperature=0.2,
        timeout=300.0,
    )
    return _parse_solution(data)


async def _fetch_delivery(
    brief: ProjectBrief, plan: ProjectPlan, platform: Platform | None
) -> dict[str, Any]:
    from app.services.llm.client import llm_client

    system = _read_prompt("solution_design_delivery.txt", "Return SDD sections 4-7 as JSON.")
    data = await llm_client.complete_json(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": _user_context(brief, plan, platform, 20000)},
        ],
        max_tokens=_DELIVERY_MAX_TOKENS,
        temperature=0.2,
        timeout=240.0,
    )
    return _parse_delivery(data)


async def _generate_parallel(
    brief: ProjectBrief,
    plan: ProjectPlan,
    platform: Platform | None,
    *,
    on_progress: Callable[[str], Awaitable[None]] | None = None,
) -> tuple[str, dict[str, Any]]:
    """
    Three Bedrock calls at once — scope, solution, delivery.

    Split this way because the groups do not need each other: §1–2 are requirements, §3 is
    architecture, §4–7 are delivery. Sequential generation would triple the wall clock for
    no gain in coherence, since the numbering and cross-references are added by the
    renderer, not by the model.
    """
    if on_progress:
        await on_progress(
            f"{SDD_FILENAME}: parallel generation (scope + solution + delivery, ×3)…"
        )
    scope, solution, delivery = await asyncio.gather(
        _fetch_scope(brief, plan, platform),
        _fetch_solution(brief, plan, platform),
        _fetch_delivery(brief, plan, platform),
    )
    return _render_pair({**scope, **solution, **delivery}, plan, platform)


async def _generate_single(
    brief: ProjectBrief,
    plan: ProjectPlan,
    platform: Platform | None,
    *,
    on_progress: Callable[[str], Awaitable[None]] | None = None,
) -> tuple[str, dict[str, Any]]:
    """One-call fallback when a section group fails validation."""
    from app.services.llm.client import llm_client

    if on_progress:
        await on_progress(f"{SDD_FILENAME}: writing the design from your documents…")
    system = _read_prompt("solution_design_single.txt", "Return the full SDD as JSON.")
    data = await llm_client.complete_json(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": _user_context(brief, plan, platform, 26000)},
        ],
        max_tokens=_SINGLE_MAX_TOKENS,
        temperature=0.2,
        timeout=420.0,
    )
    return _render_pair(_parse_solution_design_json(data), plan, platform)


def _merge_partial(
    brief: ProjectBrief,
    plan: ProjectPlan,
    platform: Platform | None,
    parts: list[Any],
) -> tuple[str, dict[str, Any]]:
    """Fill only the groups that failed from the deterministic payload."""
    payload = _fallback_payload(brief, plan)
    for part in parts:
        if isinstance(part, dict):
            payload.update(part)
    return _render_pair(payload, plan, platform)


async def generate_solution_design(
    brief: ProjectBrief,
    plan: ProjectPlan,
    platform: Platform | None,
    *,
    on_progress: Callable[[str], Awaitable[None]] | None = None,
    on_partial: Callable[[str], Awaitable[None]] | None = None,
) -> tuple[str, bool, dict[str, Any]]:
    """
    Generate `SDD.md`. Returns `(markdown, produced_by_llm, architecture views)`.

    Three tiers, same shape as the work breakdown: parallel section groups, then one
    combined call, then a per-group salvage that keeps whatever did come back and fills the
    rest deterministically. The last tier matters more here than for the work breakdown —
    a document with 26 required headings should not be thrown away because one of three
    calls returned five acceptance criteria instead of the six it was asked for.
    """
    _ = on_partial  # every tier completes in one shot; nothing useful to stream

    try:
        md, views = await _generate_parallel(brief, plan, platform, on_progress=on_progress)
        if is_solution_design_complete(md):
            logger.info("%s complete (parallel), chars=%s", SDD_FILENAME, len(md))
            return md, True, views
        logger.warning("%s failed completeness check after parallel generation", SDD_FILENAME)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Parallel solution design failed: %s", exc)

    try:
        md, views = await _generate_single(brief, plan, platform, on_progress=on_progress)
        if is_solution_design_complete(md):
            logger.info("%s complete (single call), chars=%s", SDD_FILENAME, len(md))
            return md, True, views
        logger.warning("%s failed completeness check after single call", SDD_FILENAME)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Single-call solution design failed: %s", exc)

    # Salvage: keep whichever groups did validate, fill the rest from the deterministic
    # payload. `return_exceptions` so one bad group cannot discard the other two.
    try:
        if on_progress:
            await on_progress(f"{SDD_FILENAME}: completing the remaining sections…")
        parts = await asyncio.gather(
            _fetch_scope(brief, plan, platform),
            _fetch_solution(brief, plan, platform),
            _fetch_delivery(brief, plan, platform),
            return_exceptions=True,
        )
        good = [p for p in parts if isinstance(p, dict)]
        if good:
            md, views = _merge_partial(brief, plan, platform, good)
            if is_solution_design_complete(md):
                logger.info(
                    "%s complete (salvaged %s/3 groups), chars=%s", SDD_FILENAME, len(good), len(md)
                )
                return md, True, views
    except Exception as exc:  # noqa: BLE001
        logger.warning("Salvage solution design failed: %s", exc)

    payload = _fallback_payload(brief, plan)
    return (
        _render_solution_design(payload, plan, platform),
        False,
        architecture_views_payload(payload, plan),
    )


def solution_design_payload_from_brief(brief: ProjectBrief, plan: ProjectPlan) -> dict[str, Any]:
    """Public helper for the verify script — the deterministic payload."""
    return _fallback_payload(brief, plan)
