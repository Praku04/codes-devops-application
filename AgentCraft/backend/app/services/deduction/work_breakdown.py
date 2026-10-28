"""Generate and backfill WORKBREAKDOWN.md for exported workspaces."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from app.core.state_machine import Platform
from app.models.schemas import AgentSpec, ProjectBrief, ProjectPlan, SkillSpec

logger = logging.getLogger("agentcraft.work_breakdown")

WORK_BREAKDOWN_FILENAME = "WORKBREAKDOWN.md"
_PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"
_PHASE_MAX_TOKENS = 8192
_OUTLINE_MAX_TOKENS = 3500
_PREAMBLE_MAX_TOKENS = 8192
_MAX_PHASE_RETRIES = 2
_PHASE_CONTINUATIONS = 3
_PARALLEL_PHASE_LIMIT = 4  # legacy per-phase path (regen script only)
_CONCISE_MAX_TOKENS = 12000
_CONCISE_BATCH_MAX_TOKENS = 6000
_CONCISE_MIN_PHASES = 5
_CONCISE_MAX_PHASES = 7


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


def _agent_skill_summary(agents: list[AgentSpec], skills: list[SkillSpec]) -> str:
    lines: list[str] = []
    for a in agents[:12]:
        skill_refs = ", ".join(a.skills[:4]) if a.skills else "none"
        lines.append(f"- `{a.name}` — {a.description[:120]} (skills: {skill_refs})")
    if skills:
        lines.append("")
        lines.append("Skills:")
        for s in skills[:12]:
            lines.append(f"- `{s.name}` — {s.description[:100]}")
    return "\n".join(lines) if lines else "- (see generated agents in this workspace)"


RULES_SECTION_HEADING = "## Rules in this workspace"


def _rules_section_markdown(plan: ProjectPlan, platform: Platform | None) -> str:
    """
    The `## Rules in this workspace` section, including the platform footer that closes it.

    Split out from `_ide_artifacts_markdown` so a WORKBREAKDOWN.md written before Claude
    had rules can have just this section refreshed in place — see
    `refresh_rules_section`.
    """
    from app.services.deduction.project_rules import ensure_project_rules, rules_dir

    lines: list[str] = [RULES_SECTION_HEADING, ""]
    # Rules are derived from the plan at export time, so `plan.rules` can be empty — for a
    # Claude project generated before they existed, or any plan the LLM returned none for.
    # Resolve them the same way the exporter does, or this section would claim "none"
    # beside a populated rules folder.
    rules = ensure_project_rules(plan, platform)
    if rules:
        folder = rules_dir(platform)
        if platform == Platform.CLAUDE_CODE:
            lines.append(
                f"Exported as plain markdown under `{folder}`. Always-on rules are "
                "imported by `CLAUDE.md`, so they are in context every session; scoped rules "
                "state their globs in their own header."
            )
        elif platform == Platform.WINDSURF:
            lines.append(
                f"Exported under `{folder}` as `.md` with a `trigger` field, so Cascade "
                "applies each one at the right time."
            )
        elif platform == Platform.GITHUB_COPILOT:
            lines.append(
                f"Exported under `{folder}` as `.instructions.md` with `applyTo` "
                "frontmatter, so GitHub Copilot applies each instruction to the matching files."
            )
        else:
            lines.append(
                f"Exported under `{folder}` as `.mdc` with `alwaysApply` or `globs` "
                "frontmatter, so Cursor applies each one at the right time."
            )
        lines.append("")
        for r in rules:
            mode = "always apply" if r.always_apply else (
                f"globs: {', '.join(r.globs)}" if r.globs else "model decides"
            )
            lines.append(f"- **`{r.name}`** — {r.description.strip()} ({mode})")
    else:
        lines.append("- (none generated for this plan)")
    lines.extend(
        [
            "",
            f"Platform: **{_platform_label(platform)}**. IDE folders (`.cursor/`, "
            "`.windsurf/`, `.claude/`, or `.github/`) are listed in **README.md** — not duplicated here.",
            "",
        ]
    )
    return "\n".join(lines)


def refresh_rules_section(body: str, plan: ProjectPlan, platform: Platform | None) -> str:
    """
    Rewrite the rules section of an already-written WORKBREAKDOWN.md.

    Existing Claude projects carry a stored body that says rules are not exported — that
    text is now wrong, and it is what the user reads in the file. Regenerating only this
    section keeps every hand-edited phase intact while correcting the one stale claim. A
    body that never had the section gets it appended.
    """
    section = _rules_section_markdown(plan, platform)
    start = body.find(RULES_SECTION_HEADING)
    if start < 0:
        return body.rstrip() + "\n\n" + section

    # The section runs to the next `##` heading — its platform footer belongs to it.
    after = body.find("\n## ", start + len(RULES_SECTION_HEADING))
    tail = body[after + 1 :] if after >= 0 else ""
    return body[:start] + section + ("\n" + tail if tail else "")


def _ide_artifacts_markdown(plan: ProjectPlan, platform: Platform | None) -> str:
    """Deterministic agents/skills/rules block for WORKBREAKDOWN.md preamble."""
    lines: list[str] = [
        "## Agents in this workspace",
        "",
        "Invoke these from your IDE when implementing matching phases:",
        "",
    ]
    if plan.agents:
        for a in plan.agents:
            skill_refs = ", ".join(a.skills[:6]) if a.skills else "none"
            tools = ", ".join(a.tools[:4]) if a.tools else "default"
            lines.append(f"- **`{a.name}`** — {a.description.strip()}")
            lines.append(f"  - Skills: {skill_refs} | Tools: {tools}")
    else:
        lines.append("- (none)")
    lines.extend(["", "## Skills in this workspace", ""])
    if plan.skills:
        for s in plan.skills:
            lines.append(f"- **`{s.name}`** — {s.description.strip()}")
    else:
        lines.append("- (none)")
    lines.extend(["", _rules_section_markdown(plan, platform)])
    return "\n".join(lines)


def _brief_context(brief: ProjectBrief, limit: int = 28000) -> str:
    context = brief.as_prompt_context()
    if len(context) > limit:
        return context[:limit] + "\n…[truncated]"
    return context


def _scaffold_hint(plan: ProjectPlan) -> str:
    """Internal LLM context only — never written into WORKBREAKDOWN.md."""
    paths = sorted(
        {s.path.replace("\\", "/").lstrip("/") for s in (plan.source_tree or []) if s.path}
    )[:50]
    if not paths:
        return "main.py, backend/api/routes/, backend/services/, frontend/app/"
    return ", ".join(paths)


def _phase_blocks(md: str) -> list[str]:
    """Split markdown into ## Phase N: sections."""
    text = (md or "").strip()
    if not text:
        return []
    matches = list(re.finditer(r"^## Phase \d+:", text, re.MULTILINE))
    if not matches:
        return []
    blocks: list[str] = []
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        blocks.append(text[start:end].strip())
    return blocks


def _phase_block_complete(block: str) -> bool:
    if not block.strip():
        return False
    if "### Objectives" not in block:
        return False
    if "### Tasks" not in block and "### Tasks (actionable)" not in block:
        return False
    if "### Test & acceptance" not in block:
        return False
    if "### Testing strategy" not in block:
        return False
    if "### Phase completion checklist" not in block:
        return False
    if "### Agent prompt" not in block:
        return False
    tail = block.split("### Agent prompt", 1)[-1]
    if "```" not in tail:
        return False
    # Must have opening and closing fence after agent prompt
    fences = tail.count("```")
    if fences < 2:
        return False
    last_line = block.splitlines()[-1].strip() if block.splitlines() else ""
    if last_line == "###":
        return False
    if re.match(r"^#{1,3}\s*$", last_line):
        return False
    return True


def patch_work_breakdown_artifacts(
    plan: ProjectPlan,
    platform: Platform | None,
) -> ProjectPlan:
    """Ensure WORKBREAKDOWN.md lists agents, skills, and rules (existing plans)."""
    body = plan_work_breakdown_body(plan) or (plan.work_breakdown or "").strip()
    if not body:
        return plan
    if RULES_SECTION_HEADING in body:
        # The section is there but a Claude project written before rules existed says they
        # are not exported. Rewrite just that section and persist, so the WORKBREAKDOWN
        # tab in the UI reads correctly and not only the exported copy.
        refreshed = refresh_rules_section(body, plan, platform)
        if refreshed.strip() == body.strip():
            return plan
        return apply_work_breakdown_to_plan(
            plan,
            refreshed.strip() + "\n",
            used_llm=bool(getattr(plan, "work_breakdown_llm", False)),
            complete=bool(getattr(plan, "work_breakdown_complete", False)),
        )
    artifacts = _ide_artifacts_markdown(plan, platform)
    body = re.sub(
        r"(?ms)^## Agents(?: & skills)? in this workspace.*?(?=^## |\Z)",
        "",
        body,
    )
    if "## Phase overview" in body:
        body = body.replace("## Phase overview", artifacts + "## Phase overview", 1)
    else:
        body = body.rstrip() + "\n\n" + artifacts
    return apply_work_breakdown_to_plan(
        plan,
        body.strip() + "\n",
        used_llm=bool(getattr(plan, "work_breakdown_llm", False)),
        complete=bool(getattr(plan, "work_breakdown_complete", False)),
    )


def apply_work_breakdown_to_plan(
    plan: ProjectPlan,
    wb: str,
    *,
    used_llm: bool,
    complete: bool,
) -> ProjectPlan:
    """Persist WBS on plan AND file_overrides so preview/export stay in sync."""
    overrides = dict(plan.file_overrides or {})
    overrides[WORK_BREAKDOWN_FILENAME] = wb
    return plan.model_copy(
        update={
            "work_breakdown": wb,
            "work_breakdown_llm": used_llm and complete,
            "work_breakdown_complete": complete,
            "file_overrides": overrides,
        }
    )


def is_work_breakdown_complete(md: str) -> bool:
    """True when every phase section is fully written (not truncated mid-output)."""
    text = (md or "").strip()
    if not re.search(r"^## How to use", text, re.MULTILINE | re.IGNORECASE):
        return False
    if not re.search(r"^## Tech stack", text, re.MULTILINE | re.IGNORECASE):
        return False
    if re.search(r"^## Project scaffold", text, re.MULTILINE | re.IGNORECASE):
        # Old incomplete exports may include this — still validate phases
        pass
    blocks = _phase_blocks(text)
    if len(blocks) < 4:
        return False
    min_len = max(600, len(blocks) * 250)
    if len(text) < min_len:
        return False
    if not all(_phase_block_complete(b) for b in blocks):
        return False
    last_line = text.splitlines()[-1].strip()
    if re.match(r"^#{1,3}\s*$", last_line):
        return False
    return True


def _strip_scaffold_section(md: str) -> str:
    """Remove project-structure sections (belongs in README only)."""
    text = md or ""
    for heading in (
        r"## Project scaffold[^\n]*",
        r"## Project structure[^\n]*",
        r"## Scaffold[^\n]*",
    ):
        text = re.sub(
            rf"(?ms)^{heading}\n.*?(?=^## |\Z)",
            "",
            text,
        )
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def fallback_work_breakdown(
    brief: ProjectBrief,
    plan: ProjectPlan,
    platform: Platform | None,
) -> str:
    """Deterministic complete WBS when LLM is unavailable."""
    plat = _platform_label(platform)
    ctx = brief.as_prompt_context()[:3000]
    stack = brief.tech_stack.strip() or (
        "FastAPI + Pydantic + SQLAlchemy/SQLModel, PostgreSQL, Redis (optional), "
        "React/Next.js or Angular frontend, JWT/session auth, pytest."
    )
    name = plan.project_name or "Project"

    phases = [
        (
            "Phase 1: Foundation, settings & health",
            [
                "Wire `main.py` entrypoint and `backend/core/settings.py` from `.env.example`.",
                "Add health/readiness routes and structured logging.",
                "Add smoke test proving the API boots.",
            ],
        ),
        (
            "Phase 2: Auth, roles & tenancy",
            [
                "Implement auth routes and RBAC for roles named in the brief.",
                "Enforce authorization on every mutating repository method.",
                "Add tests for forbidden cross-tenant access (403).",
            ],
        ),
        (
            "Phase 3: Core domain models & repositories",
            [
                "Define ORM models and Pydantic schemas for primary entities from documents.",
                "Implement repositories with parameterized queries only.",
                "Seed minimal fixtures for integration tests.",
            ],
        ),
        (
            "Phase 4: Primary user flows & frontend",
            [
                "Build primary UI routes under `frontend/` for each actor in the brief.",
                "Add typed API client with loading/error states.",
                "Cover happy path + validation errors in component tests.",
            ],
        ),
        (
            "Phase 5: Integrations, AI services & async jobs",
            [
                "Implement external integrations listed in the brief (webhooks, Bedrock, storage, voice).",
                "Add retries, idempotency keys, and dead-letter handling for jobs.",
                "Mock third parties in tests; never log secrets.",
            ],
        ),
        (
            "Phase 6: Security review, VAPT & release",
            [
                "Run security-vapt-reviewer; fix High/Critical findings.",
                "Execute regression + acceptance checklist from documents.",
                "Update README deployment notes only (structure already documented there).",
            ],
        ),
    ]

    body: list[str] = [
        f"# Work Breakdown Plan — {name}",
        "",
        f"Generated for **{plat}**. See **README.md** for project file layout.",
        "",
        "## How to use this plan with IDE agents",
        "",
        "1. Open this workspace and skim **README.md** for agents/skills and file layout.",
        "2. Pick **one phase** below (start at Phase 1).",
        "3. Ask your agent: `Implement Phase N from WORKBREAKDOWN.md`.",
        "4. Run every **Test & acceptance** bullet for that phase.",
        "5. Mark tasks `- [x]` before moving on.",
        "",
        "## Product scope (from user documents)",
        "",
        plan.summary.strip() or brief.problem_statement[:800] or "See brief below.",
        "",
        "### Document requirements covered",
        "",
        "- Problem statement and uploaded documents drive every phase below.",
        "- Each phase maps back to specific brief constraints and integrations.",
        "",
        "## Tech stack",
        "",
        stack,
        "",
        "### Brief excerpt",
        "",
        ctx[:2000] + ("…" if len(ctx) > 2000 else ""),
        "",
        "## Assumptions & constraints",
        "",
        brief.constraints.strip() or "- Follow VAPT guidance from generated security agents.",
        "",
        _ide_artifacts_markdown(plan, platform),
        "## Phase overview",
        "",
    ]
    for title, _ in phases:
        body.append(f"- **{title}**")
    body.append("")

    for title, tasks in phases:
        body.extend(
            [
                f"## {title}",
                "",
                "### Objectives",
                "",
                f"- Deliver incremental, testable value for `{name}`.",
                "- Satisfy document requirements listed for this phase.",
                "",
                "### Tasks (actionable)",
                "",
            ]
        )
        body.extend(f"- [ ] {t}" for t in tasks)
        body.extend(
            [
                "",
                "### Testing strategy",
                "",
                "- Unit: pytest for services/repositories touched in this phase.",
                "- Integration: API tests for new routes with auth fixtures.",
                "- Manual: walk through primary user flow for this phase in the UI.",
                "",
                "### Test & acceptance (phase gate — must pass before next phase)",
                "",
                "- [ ] All Tasks marked `[x]`.",
                "- [ ] `pytest` passes for new/changed modules.",
                "- [ ] OpenAPI or curl checks pass for new endpoints.",
                "- [ ] Wrong-role access returns 403 where required.",
                "- [ ] No secrets/PII in logs for this phase's code paths.",
                "- [ ] Ready to start the next phase.",
                "",
                "### Phase completion checklist",
                "",
                "- [ ] All Tasks (actionable) complete",
                "- [ ] All Test & acceptance checks verified",
                "- [ ] No open High/Critical VAPT findings",
                "- [ ] Ask agent: `Implement next phase from WORKBREAKDOWN.md`",
                "",
                "### Agent prompt (copy-paste)",
                "",
                "```text",
                f"Implement {title} from WORKBREAKDOWN.md. Follow the tech stack,",
                "use agents/skills in this repo, and satisfy all Test & acceptance bullets.",
                "```",
                "",
            ]
        )

    return _strip_scaffold_section("\n".join(body)).rstrip() + "\n"


def _normalize_markdown(md: str, project_name: str) -> str:
    text = _strip_scaffold_section((md or "").strip())
    if not text:
        return ""
    if not text.startswith("#"):
        text = f"# Work Breakdown Plan — {project_name}\n\n{text}"
    return text.rstrip() + "\n"


def _fallback_outline_from_brief(brief: ProjectBrief, plan: ProjectPlan) -> dict[str, Any]:
    """Deterministic 8–10 phase outline when Bedrock JSON outline fails."""
    ctx = brief.as_prompt_context()[:4000]
    stack = brief.tech_stack.strip() or (
        "FastAPI, Pydantic, SQLAlchemy/SQLModel, PostgreSQL, Redis, "
        "React/Next.js or Angular, AWS Bedrock, S3, pytest."
    )
    reqs = [brief.problem_statement.strip()[:300]] if brief.problem_statement else []
    reqs.extend(brief.domains[:8])
    if brief.integrations:
        reqs.extend(brief.integrations[:6])
    if plan.summary:
        reqs.append(plan.summary[:280])
    reqs = [r for r in reqs if r][:12]

    phase_templates: list[tuple[str, str, str]] = [
        ("Foundation, settings & API scaffold", "Boot app, env, health checks, modular packages", "Problem statement — infrastructure"),
        ("Authentication, RBAC & tenant isolation", "Roles, JWT/session, 403 on cross-tenant access", "Auth requirements in documents"),
        ("Core domain models & primary API", "Entities, repositories, versioned REST routes from documents", "Core domain requirements"),
        ("Primary user flows & frontend", "UI routes, API client, main workflows per role", "UX/workflow requirements"),
        ("Integrations, AI & async jobs", "External services, Bedrock, storage, webhooks, background jobs", "Integration requirements"),
        ("Testing, security review & release", "VAPT, audit logs, regression, release checklist", "Security & compliance constraints"),
    ]
    phases = [
        {
            "number": i + 1,
            "title": title,
            "goal": goal,
            "document_refs": refs,
        }
        for i, (title, goal, refs) in enumerate(phase_templates)
    ]
    return {
        "tech_stack_detail": stack + "\n\n" + (plan.summary or ""),
        "document_requirements": reqs or ["See problem statement and uploaded documents."],
        "assumptions": [
            brief.constraints.strip() or "Follow VAPT guidance from security agents in this workspace.",
            "Use README.md for project file layout; reference paths inline in tasks only.",
        ],
        "phases": phases,
    }


def _parse_outline(data: Any, plan: ProjectPlan) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("outline not a dict")
    phases = data.get("phases") or []
    if not isinstance(phases, list) or len(phases) < 4:
        raise ValueError("outline needs at least 4 phases")
    cleaned: list[dict[str, Any]] = []
    for i, p in enumerate(phases[:12]):
        if not isinstance(p, dict):
            continue
        num = int(p.get("number") or i + 1)
        title = str(p.get("title") or f"Phase {num}").strip()
        cleaned.append(
            {
                "number": num,
                "title": title,
                "goal": str(p.get("goal") or "").strip(),
                "document_refs": str(p.get("document_refs") or "").strip(),
            }
        )
    if len(cleaned) < 4:
        raise ValueError("too few valid phases")
    return {
        "tech_stack_detail": str(data.get("tech_stack_detail") or plan.summary).strip(),
        "document_requirements": [
            str(x).strip() for x in (data.get("document_requirements") or []) if str(x).strip()
        ],
        "assumptions": [str(x).strip() for x in (data.get("assumptions") or []) if str(x).strip()],
        "phases": cleaned,
    }


def _parse_concise_json(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("concise WBS not a dict")
    phases = data.get("phases") or []
    if not isinstance(phases, list):
        raise ValueError("phases missing")
    cleaned: list[dict[str, Any]] = []
    for i, p in enumerate(phases[: _CONCISE_MAX_PHASES]):
        if not isinstance(p, dict):
            continue
        num = int(p.get("number") or i + 1)
        title = str(p.get("title") or f"Phase {num}").strip()
        objectives = [str(x).strip() for x in (p.get("objectives") or []) if str(x).strip()]
        tasks = [str(x).strip() for x in (p.get("tasks") or []) if str(x).strip()]
        testing = [
            str(x).strip() for x in (p.get("testing_strategy") or p.get("testing") or []) if str(x).strip()
        ]
        acceptance = [str(x).strip() for x in (p.get("acceptance") or []) if str(x).strip()]
        agent_prompt = str(p.get("agent_prompt") or "").strip()
        if len(objectives) < 2 or len(tasks) < 4 or len(acceptance) < 3 or not agent_prompt:
            continue
        cleaned.append(
            {
                "number": num,
                "title": title,
                "objectives": objectives,
                "tasks": tasks[:10],
                "testing_strategy": testing[:6] or [
                    "Unit: pytest for services touched in this phase.",
                    "Integration: API tests with auth fixtures.",
                    "Manual: walk primary user flow.",
                ],
                "acceptance": acceptance[:8],
                "agent_prompt": agent_prompt,
            }
        )
    if len(cleaned) < _CONCISE_MIN_PHASES:
        raise ValueError(f"need at least {_CONCISE_MIN_PHASES} valid phases")
    return {
        "product_scope": str(data.get("product_scope") or "").strip(),
        "document_requirements": [
            str(x).strip() for x in (data.get("document_requirements") or []) if str(x).strip()
        ],
        "tech_stack": str(data.get("tech_stack") or "").strip(),
        "assumptions": [str(x).strip() for x in (data.get("assumptions") or []) if str(x).strip()],
        "phases": cleaned,
    }


def _render_concise_wbs(
    data: dict[str, Any],
    plan: ProjectPlan,
    platform: Platform | None,
) -> str:
    """Render JSON concise WBS to markdown with required phase subsections."""
    plat = _platform_label(platform)
    name = plan.project_name or "Project"
    lines: list[str] = [
        f"# Work Breakdown Plan — {name}",
        "",
        f"Generated for **{plat}**. See **README.md** for project file layout.",
        "",
        "## How to use this plan with IDE agents",
        "",
        "1. Open this workspace and skim **README.md** for agents/skills and file layout.",
        "2. Pick **one phase** below (start at Phase 1).",
        "3. Ask your agent: `Implement Phase N from WORKBREAKDOWN.md`.",
        "4. Run every **Test & acceptance** bullet for that phase.",
        "5. Mark tasks `- [x]` before moving on.",
        "",
        "## Product scope (from user documents)",
        "",
        data["product_scope"] or plan.summary.strip() or "See brief and uploaded documents.",
        "",
        "### Document requirements covered",
        "",
    ]
    reqs = data["document_requirements"] or ["See problem statement and uploaded documents."]
    lines.extend(f"- {r}" for r in reqs[:14])
    lines.extend(["", "## Tech stack", "", data["tech_stack"] or plan.summary or "See brief."])
    lines.extend(["", "## Assumptions & constraints", ""])
    assumptions = data["assumptions"] or ["Follow VAPT guidance from security agents in this workspace."]
    lines.extend(f"- {a}" for a in assumptions[:8])
    lines.append("")
    lines.append(_ide_artifacts_markdown(plan, platform))
    lines.extend(["## Phase overview", ""])
    for p in data["phases"]:
        lines.append(f"- **Phase {p['number']}: {p['title']}**")
    lines.append("")

    for p in data["phases"]:
        n, title = p["number"], p["title"]
        lines.extend([f"## Phase {n}: {title}", "", "### Objectives", ""])
        lines.extend(f"- {o}" for o in p["objectives"])
        lines.extend(["", "### Tasks (actionable)", ""])
        lines.extend(f"- [ ] {t}" for t in p["tasks"])
        lines.extend(["", "### Testing strategy", ""])
        lines.extend(f"- {t}" for t in p["testing_strategy"])
        lines.extend(["", "### Test & acceptance (phase gate — must pass before next phase)", ""])
        lines.extend(f"- [ ] {a}" for a in p["acceptance"])
        lines.extend(
            [
                "",
                "### Phase completion checklist",
                "",
                "- [ ] All Tasks (actionable) complete",
                "- [ ] All Test & acceptance checks verified",
                "- [ ] No open High/Critical VAPT findings",
                f"- [ ] Ready to ask agent: `Implement Phase {n + 1} from WORKBREAKDOWN.md`",
                "",
                "### Agent prompt (copy-paste)",
                "",
                "```text",
                p["agent_prompt"],
                "```",
                "",
            ]
        )
    return _normalize_markdown("\n".join(lines), name)


async def _fetch_wbs_overview(
    brief: ProjectBrief,
    plan: ProjectPlan,
    platform: Platform | None,
    outline: dict[str, Any],
) -> dict[str, Any]:
    from app.services.llm.client import llm_client

    system = _read_prompt("work_breakdown_concise_overview.txt", "Return WBS overview JSON.")
    phase_lines = "\n".join(
        f"- Phase {p['number']}: {p['title']} — {p.get('goal') or ''}" for p in outline["phases"]
    )
    user = (
        f"Platform: {_platform_label(platform)}\n"
        f"Project: {plan.project_name}\n"
        f"Summary: {plan.summary}\n\n"
        f"Planned phases (bodies generated separately):\n{phase_lines}\n\n"
        f"Agents/skills:\n{_agent_skill_summary(plan.agents, plan.skills)}\n\n"
        f"User documents:\n{_brief_context(brief, 20000)}\n"
    )
    data = await llm_client.complete_json(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        max_tokens=4000,
        temperature=0.25,
        timeout=180.0,
    )
    if not isinstance(data, dict):
        raise ValueError("overview not a dict")
    return {
        "product_scope": str(data.get("product_scope") or "").strip(),
        "document_requirements": [
            str(x).strip() for x in (data.get("document_requirements") or []) if str(x).strip()
        ],
        "tech_stack": str(data.get("tech_stack") or "").strip(),
        "assumptions": [str(x).strip() for x in (data.get("assumptions") or []) if str(x).strip()],
    }


def _parse_phase_batch(data: Any, *, min_count: int = 2) -> list[dict[str, Any]]:
    if not isinstance(data, dict):
        raise ValueError("phase batch not a dict")
    phases = data.get("phases") or []
    if not isinstance(phases, list):
        raise ValueError("phases missing")
    cleaned: list[dict[str, Any]] = []
    for i, p in enumerate(phases):
        if not isinstance(p, dict):
            continue
        num = int(p.get("number") or i + 1)
        title = str(p.get("title") or f"Phase {num}").strip()
        objectives = [str(x).strip() for x in (p.get("objectives") or []) if str(x).strip()]
        tasks = [str(x).strip() for x in (p.get("tasks") or []) if str(x).strip()]
        testing = [
            str(x).strip() for x in (p.get("testing_strategy") or p.get("testing") or []) if str(x).strip()
        ]
        acceptance = [str(x).strip() for x in (p.get("acceptance") or []) if str(x).strip()]
        agent_prompt = str(p.get("agent_prompt") or "").strip()
        if len(objectives) < 2 or len(tasks) < 4 or len(acceptance) < 3 or not agent_prompt:
            continue
        cleaned.append(
            {
                "number": num,
                "title": title,
                "objectives": objectives,
                "tasks": tasks[:10],
                "testing_strategy": testing[:6] or [
                    "Unit: pytest for services touched in this phase.",
                    "Integration: API tests with auth fixtures.",
                    "Manual: walk primary user flow.",
                ],
                "acceptance": acceptance[:8],
                "agent_prompt": agent_prompt,
            }
        )
    if len(cleaned) < min_count:
        raise ValueError(f"need at least {min_count} valid phases in batch")
    return cleaned


async def _fetch_wbs_phase_batch(
    brief: ProjectBrief,
    plan: ProjectPlan,
    platform: Platform | None,
    batch: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    from app.services.llm.client import llm_client

    system = _read_prompt("work_breakdown_concise_batch.txt", "Return phase batch JSON.")
    spec_lines = "\n".join(
        f"Phase {p['number']}: {p['title']}\n  Goal: {p.get('goal') or ''}\n  Docs: {p.get('document_refs') or ''}"
        for p in batch
    )
    user = (
        f"Platform: {_platform_label(platform)}\n"
        f"Project: {plan.project_name}\n"
        f"Summary: {plan.summary}\n\n"
        f"Write these phases ONLY:\n{spec_lines}\n\n"
        f"Agents/skills:\n{_agent_skill_summary(plan.agents, plan.skills)}\n\n"
        f"User documents:\n{_brief_context(brief, 18000)}\n"
    )
    data = await llm_client.complete_json(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        max_tokens=_CONCISE_BATCH_MAX_TOKENS,
        temperature=0.25,
        timeout=180.0,
    )
    min_count = 1 if len(batch) == 1 else 2
    parsed = _parse_phase_batch(data, min_count=min_count)
    ordered: list[dict[str, Any]] = []
    for i, spec in enumerate(batch):
        if i >= len(parsed):
            break
        ordered.append({**parsed[i], "number": spec["number"], "title": spec["title"]})
    if len(ordered) < min_count:
        raise ValueError("batch missing expected phases")
    return ordered


async def _generate_concise_wbs_parallel(
    brief: ProjectBrief,
    plan: ProjectPlan,
    platform: Platform | None,
    *,
    on_progress: Callable[[str], Awaitable[None]] | None = None,
) -> str:
    """3 parallel Bedrock calls: overview + 2 phase batches → complete concise WBS."""
    outline = _fallback_outline_from_brief(brief, plan)
    phases = outline["phases"][:_CONCISE_MAX_PHASES]
    mid = max(1, (len(phases) + 1) // 2)
    batch_a = phases[:mid]
    batch_b = phases[mid:]
    parallel = 2 + (1 if batch_b else 0)

    if on_progress:
        await on_progress(
            f"WORKBREAKDOWN.md: parallel generation (overview + {len(batch_a)}+{len(batch_b)} phases, ×{parallel})…"
        )

    tasks: list[Awaitable[Any]] = [
        _fetch_wbs_overview(brief, plan, platform, {**outline, "phases": phases}),
        _fetch_wbs_phase_batch(brief, plan, platform, batch_a),
    ]
    if batch_b:
        tasks.append(_fetch_wbs_phase_batch(brief, plan, platform, batch_b))

    results = await asyncio.gather(*tasks)
    overview = results[0]
    phase_chunks: list[dict[str, Any]] = list(results[1])
    if batch_b:
        phase_chunks.extend(results[2])
    phase_chunks.sort(key=lambda p: p["number"])

    merged = {**overview, "phases": phase_chunks}
    parsed = _parse_concise_json(merged)
    return _render_concise_wbs(parsed, plan, platform)


async def _generate_concise_wbs_single(
    brief: ProjectBrief,
    plan: ProjectPlan,
    platform: Platform | None,
    *,
    on_progress: Callable[[str], Awaitable[None]] | None = None,
) -> str:
    """Single Bedrock call fallback for concise WBS."""
    from app.services.llm.client import llm_client

    if on_progress:
        await on_progress("WORKBREAKDOWN.md: writing plan from your documents…")
    system = _read_prompt("work_breakdown_concise.txt", "Return concise WBS JSON.")
    user = (
        f"Platform: {_platform_label(platform)}\n"
        f"Project: {plan.project_name}\n"
        f"Summary: {plan.summary}\n\n"
        f"Agents/skills in workspace:\n{_agent_skill_summary(plan.agents, plan.skills)}\n\n"
        f"User documents and brief:\n{_brief_context(brief, 28000)}\n\n"
        f"Return JSON with {_CONCISE_MIN_PHASES}-{_CONCISE_MAX_PHASES} phases covering ALL document requirements."
    )
    data = await llm_client.complete_json(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        max_tokens=_CONCISE_MAX_TOKENS,
        temperature=0.25,
        timeout=300.0,
    )
    parsed = _parse_concise_json(data)
    return _render_concise_wbs(parsed, plan, platform)


async def _generate_concise_wbs(
    brief: ProjectBrief,
    plan: ProjectPlan,
    platform: Platform | None,
    *,
    on_progress: Callable[[str], Awaitable[None]] | None = None,
) -> str:
    """Concise document-grounded WBS — parallel first, single-call fallback."""
    try:
        return await _generate_concise_wbs_parallel(brief, plan, platform, on_progress=on_progress)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Parallel concise WBS failed (%s); trying single call", exc)
    return await _generate_concise_wbs_single(brief, plan, platform, on_progress=on_progress)


async def _generate_outline(
    brief: ProjectBrief,
    plan: ProjectPlan,
    platform: Platform | None,
) -> dict[str, Any]:
    from app.services.llm.client import llm_client

    system = _read_prompt("work_breakdown_outline.txt", "Return JSON phase outline.")
    user = (
        f"Platform: {_platform_label(platform)}\n"
        f"Project: {plan.project_name}\n"
        f"Summary: {plan.summary}\n\n"
        f"Context excerpt:\n{_brief_context(brief, 12000)}\n\n"
        "Return JSON with phases array of 8-10 items."
    )
    try:
        data = await llm_client.complete_json(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            max_tokens=_OUTLINE_MAX_TOKENS,
            temperature=0.2,
            timeout=180.0,
        )
        return _parse_outline(data, plan)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Outline LLM unavailable (%s); using product template", exc)
    return _fallback_outline_from_brief(brief, plan)


async def _generate_preamble(
    brief: ProjectBrief,
    plan: ProjectPlan,
    platform: Platform | None,
    outline: dict[str, Any],
) -> str:
    from app.services.llm.client import llm_client

    system = _read_prompt("work_breakdown_system.txt", "Write WORKBREAKDOWN preamble markdown.")
    overview_lines = [
        f"- **Phase {p['number']}: {p['title']}** — {p.get('goal') or ''}" for p in outline["phases"]
    ]
    user = (
        f"Platform: {_platform_label(platform)}\n"
        f"Project: {plan.project_name}\n\n"
        f"Outline JSON:\n{json.dumps(outline, indent=2)[:12000]}\n\n"
        f"Phase overview bullets to include verbatim:\n" + "\n".join(overview_lines) + "\n\n"
        f"Agents/skills:\n{_agent_skill_summary(plan.agents, plan.skills)}\n\n"
        f"User documents:\n{_brief_context(brief, 22000)}\n\n"
        "Write the preamble markdown now (through Phase overview). Do NOT write phase bodies."
    )
    md = await llm_client.complete(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        max_tokens=_PREAMBLE_MAX_TOKENS,
        temperature=0.25,
        timeout=360.0,
    )
    md = _normalize_markdown(md, plan.project_name or "Project")
    artifacts = _ide_artifacts_markdown(plan, platform)
    # Drop any truncated LLM agents/skills block — use deterministic list instead.
    md = re.sub(
        r"(?ms)^## Agents(?: & skills)? in this workspace.*?(?=^## Phase overview|\Z)",
        "",
        md,
    )
    if "## Phase overview" in md:
        md = md.replace("## Phase overview", artifacts + "## Phase overview", 1)
    else:
        md = md.rstrip() + "\n\n" + artifacts
    return md.rstrip() + "\n"


def _merge_continuation(existing: str, continuation: str) -> str:
    """Append continuation, dropping duplicated overlap with the tail of existing."""
    cont = (continuation or "").strip()
    if not cont:
        return ""
    tail = existing[-600:]
    for overlap in range(min(300, len(tail), len(cont)), 20, -1):
        if tail.endswith(cont[:overlap]):
            return cont[overlap:].lstrip()
    return cont


async def _generate_phase_section(
    brief: ProjectBrief,
    plan: ProjectPlan,
    platform: Platform | None,
    outline: dict[str, Any],
    phase: dict[str, Any],
    *,
    prior_titles: list[str],
) -> str:
    from app.services.llm.client import llm_client

    system = _read_prompt("work_breakdown_phase.txt", "Write one complete phase section.")
    n = phase["number"]
    title = phase["title"]
    base_user = (
        f"Platform: {_platform_label(platform)}\n"
        f"Project: {plan.project_name}\n"
        f"Phase {n} of {len(outline['phases'])}: {title}\n"
        f"Goal: {phase.get('goal') or ''}\n"
        f"Document refs: {phase.get('document_refs') or ''}\n\n"
        f"Tech stack (use in tasks):\n{outline.get('tech_stack_detail') or brief.tech_stack}\n\n"
        f"Prior phases completed: {', '.join(prior_titles) or 'none'}\n\n"
        f"Scaffold paths (inline in tasks only — do NOT list file tree):\n"
        f"{_scaffold_hint(plan)}\n\n"
        f"Agents/skills:\n{_agent_skill_summary(plan.agents, plan.skills)}\n\n"
        f"User documents:\n{_brief_context(brief, 18000)}\n\n"
        f"Write ## Phase {n}: {title} with ALL subsections including Testing strategy "
        f"and Phase completion checklist. Include 18–25 detailed checkbox tasks."
    )
    messages: list[dict[str, str]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": base_user},
    ]
    section = ""
    for cont in range(_PHASE_CONTINUATIONS + 1):
        chunk = await llm_client.complete(
            messages,
            max_tokens=_PHASE_MAX_TOKENS,
            temperature=0.25,
            timeout=420.0,
        )
        if cont == 0:
            section = chunk.strip()
        else:
            section = section.rstrip() + "\n" + _merge_continuation(section, chunk.strip())
        if not section.startswith(f"## Phase {n}"):
            section = f"## Phase {n}: {title}\n\n{section}"
        if _phase_block_complete(section):
            return section.rstrip() + "\n"
        messages = list(messages) + [
            {"role": "assistant", "content": section[-12000:]},
            {
                "role": "user",
                "content": (
                    "Continue this phase section from exactly where you stopped. "
                    "Complete ALL remaining subsections through ### Agent prompt "
                    "with a closing ``` fence. Markdown only."
                ),
            },
        ]
    return section.rstrip() + "\n"


async def _generate_phase_with_retry(
    brief: ProjectBrief,
    plan: ProjectPlan,
    platform: Platform | None,
    outline: dict[str, Any],
    phase: dict[str, Any],
    *,
    prior_titles: list[str],
) -> tuple[int, str]:
    """Generate one phase section; retry on incomplete output."""
    n = phase["number"]
    section = ""
    for attempt in range(_MAX_PHASE_RETRIES + 1):
        try:
            section = await _generate_phase_section(
                brief, plan, platform, outline, phase, prior_titles=prior_titles
            )
            if _phase_block_complete(section):
                return n, section
            logger.warning("Phase %s incomplete (attempt %s), retrying", n, attempt + 1)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Phase %s generation failed: %s", n, exc)
            if attempt >= _MAX_PHASE_RETRIES:
                raise
    if not _phase_block_complete(section):
        raise ValueError(f"Phase {n} still incomplete after retries")
    return n, section


async def _generate_phases_parallel(
    brief: ProjectBrief,
    plan: ProjectPlan,
    platform: Platform | None,
    outline: dict[str, Any],
    *,
    preamble_task: asyncio.Task[str] | None = None,
    on_progress: Callable[[str], Awaitable[None]] | None = None,
    on_partial: Callable[[str], Awaitable[None]] | None = None,
) -> list[str]:
    """Generate all phase sections concurrently (bounded parallelism)."""
    phases = outline["phases"]
    total = len(phases)
    sem = asyncio.Semaphore(_PARALLEL_PHASE_LIMIT)
    completed: dict[int, str] = {}
    name = plan.project_name or "Project"

    async def _preamble_text() -> str:
        if preamble_task is None:
            return ""
        if preamble_task.done():
            return preamble_task.result()
        return "## WORKBREAKDOWN.md\n\n_(Generating preamble…)_"

    async def _emit_partial() -> None:
        if not on_partial or not completed:
            return
        pre = (await _preamble_text()).rstrip()
        ordered = [completed[n] for n in sorted(completed)]
        body = (pre + "\n\n" + "\n\n".join(ordered)).strip() if pre else "\n\n".join(ordered)
        await on_partial(_normalize_markdown(body, name))

    async def _run(phase: dict[str, Any], prior_titles: list[str]) -> tuple[int, str]:
        n = phase["number"]
        async with sem:
            if on_progress:
                await on_progress(
                    f"WORKBREAKDOWN.md: Phase {n} of {total} — {phase['title'][:50]}… "
                    f"(parallel ×{_PARALLEL_PHASE_LIMIT})"
                )
            return await _generate_phase_with_retry(
                brief, plan, platform, outline, phase, prior_titles=prior_titles
            )

    tasks = [
        asyncio.create_task(
            _run(phase, [f"Phase {p['number']}: {p['title']}" for p in phases[:i]])
        )
        for i, phase in enumerate(phases)
    ]
    for finished in asyncio.as_completed(tasks):
        n, section = await finished
        completed[n] = section
        await _emit_partial()

    return [completed[n] for n in sorted(completed)]


async def _generate_by_phases(
    brief: ProjectBrief,
    plan: ProjectPlan,
    platform: Platform | None,
    *,
    on_progress: Callable[[str], Awaitable[None]] | None = None,
    on_partial: Callable[[str], Awaitable[None]] | None = None,
) -> str:
    outline = await _generate_outline(brief, plan, platform)
    total = len(outline["phases"])
    if on_progress:
        await on_progress(
            f"WORKBREAKDOWN.md: {total} phases — preamble + parallel generation (×{_PARALLEL_PHASE_LIMIT})…"
        )
    preamble_task = asyncio.create_task(_generate_preamble(brief, plan, platform, outline))
    phase_sections = await _generate_phases_parallel(
        brief,
        plan,
        platform,
        outline,
        preamble_task=preamble_task,
        on_progress=on_progress,
        on_partial=on_partial,
    )
    preamble = await preamble_task
    full = _normalize_markdown(
        preamble.rstrip() + "\n\n" + "\n\n".join(phase_sections),
        plan.project_name or "Project",
    )
    if on_partial:
        await on_partial(full)
    return full


async def generate_work_breakdown(
    brief: ProjectBrief,
    plan: ProjectPlan,
    platform: Platform | None,
    *,
    on_progress: Callable[[str], Awaitable[None]] | None = None,
    on_partial: Callable[[str], Awaitable[None]] | None = None,
    detailed: bool = False,
) -> tuple[str, bool]:
    """Generate WORKBREAKDOWN.md. Default: one concise Bedrock call during Generate."""
    _ = on_partial  # concise path completes in one shot — no incremental partials
    try:
        md = await _generate_concise_wbs(brief, plan, platform, on_progress=on_progress)
        if is_work_breakdown_complete(md):
            logger.info(
                "WORKBREAKDOWN complete (concise), chars=%s phases=%s",
                len(md),
                len(_phase_blocks(md)),
            )
            return md, True
        logger.warning("Concise WBS failed completeness check")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Concise work breakdown failed: %s", exc)

    if detailed:
        try:
            md = await _generate_by_phases(
                brief,
                plan,
                platform,
                on_progress=on_progress,
                on_partial=on_partial,
            )
            if is_work_breakdown_complete(md):
                logger.info(
                    "WORKBREAKDOWN complete (per-phase), chars=%s phases=%s",
                    len(md),
                    len(_phase_blocks(md)),
                )
                return md, True
        except Exception as exc:  # noqa: BLE001
            logger.warning("Per-phase work breakdown failed: %s", exc)

    fb = fallback_work_breakdown(brief, plan, platform)
    return fb, False


def outline_from_brief(brief: ProjectBrief, plan: ProjectPlan) -> dict[str, Any]:
    """Public helper for tests — deterministic phase outline."""
    return _fallback_outline_from_brief(brief, plan)


def plan_work_breakdown_body(plan: ProjectPlan) -> str:
    overrides = plan.file_overrides or {}
    if WORK_BREAKDOWN_FILENAME in overrides and overrides[WORK_BREAKDOWN_FILENAME]:
        return str(overrides[WORK_BREAKDOWN_FILENAME])
    return (plan.work_breakdown or "").strip()


def plan_needs_work_breakdown(plan: ProjectPlan | None) -> bool:
    if not plan:
        return True
    if plan_work_breakdown_body(plan):
        return False
    return not (plan.work_breakdown or "").strip()


def plan_needs_detailed_work_breakdown(plan: ProjectPlan | None) -> bool:
    """True when WORKBREAKDOWN.md is missing or structurally incomplete."""
    if not plan:
        return True
    body = plan_work_breakdown_body(plan) or (plan.work_breakdown or "").strip()
    if not body:
        return True
    return not is_work_breakdown_complete(body)
