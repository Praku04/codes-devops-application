"""Parallel LLM deduction of agents/skills/rules (faster wall-clock)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from app.core.state_machine import Platform
from app.models.schemas import (
    AgentSpec,
    McpServerSpec,
    ProjectBrief,
    ProjectPlan,
    RuleSpec,
    SkillSpec,
    SourceFileSpec,
)
from app.services.deduction.goals_format import ensure_goals_bullets
from app.services.deduction.solution_design import (
    fallback_solution_design,
    fallback_solution_design_views,
    generate_solution_design,
    is_solution_design_complete,
)
from app.services.deduction.work_breakdown import (
    fallback_work_breakdown,
    generate_work_breakdown,
    is_work_breakdown_complete,
)
from app.services.deduction.source_tree import (
    build_source_tree_from_brief,
    merge_llm_source_files,
    normalize_source_meta,
)
from app.services.llm.client import llm_client

logger = logging.getLogger("agentcraft.deduction")

ProgressCb = Callable[[str], Awaitable[None] | None]

_PROMPT_PATH = Path(__file__).resolve().parents[2] / "prompts" / "deduction_system.txt"
_BLUEPRINT_PROMPT = Path(__file__).resolve().parents[2] / "prompts" / "blueprint_system.txt"


def _system_prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


def _blueprint_prompt() -> str:
    if _BLUEPRINT_PROMPT.exists():
        return _BLUEPRINT_PROMPT.read_text(encoding="utf-8")
    return (
        "Return ONLY JSON blueprint: project_name, summary, agents[{name,description,tools,skills}], "
        "skills[{name,description}], rules[{name,description,always_apply,globs}], mcp_servers[], "
        "source_tree[{path,purpose}] starting at main.py with package __init__.py files. "
        "No long bodies yet. Always include security-vapt-reviewer, "
        "observability-agent, and guardrails-agent. Add requirement-specific agents as needed."
    )


async def _emit(cb: ProgressCb | None, message: str) -> None:
    if not cb:
        return
    result = cb(message)
    if asyncio.iscoroutine(result):
        await result


_BODY_MAX = 49  # must stay under 50; AI chooses length — never pad to max
_BODY_PREF = "12–22"


def _target_lines(name: str) -> int:
    """Stable per-name soft target so bodies vary (12–22), not a single ceiling."""
    h = sum(ord(c) for c in (name or "x"))
    return 12 + (h % 11)


def _ensure_skill_script(skill: SkillSpec) -> SkillSpec:
    """
    Docs-aligned optional scripts/ for skills that need deterministic checks
    (Cursor/Claude/Windsurf: skill-name/scripts/*.py).
    """
    refs = dict(skill.references or {})
    has_script = any(p.startswith("scripts/") and p.endswith((".py", ".sh")) for p in refs)
    keys = f"{skill.name} {skill.description}".lower()
    needs = any(
        k in keys
        for k in (
            "vapt",
            "secure",
            "tenant",
            "hipaa",
            "idor",
            "hmac",
            "audit",
            "encrypt",
            "fhir",
            "webhook",
        )
    )
    if has_script or not needs:
        return skill

    script = f'''#!/usr/bin/env python3
"""Deterministic helper for skill `{skill.name}`.

Official Agent Skills layout: keep this under scripts/ and invoke from SKILL.md.
Example (from repo root):
  python .cursor/skills/{skill.name}/scripts/validate_checks.py
  # or .claude/skills/... / .windsurf/skills/...
"""
from __future__ import annotations

import argparse
import sys


CHECKS = [
    "AuthZ / tenant isolation verified on mutating routes",
    "No user-controlled regex (prefer schema validators)",
    "Secrets not hardcoded; env/vault only",
    "PII/PHI not logged in plaintext",
    "High/Critical findings waived or fixed before VAPT handoff",
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print checklist for {skill.name}")
    parser.add_argument("--json", action="store_true", help="Emit JSON checklist")
    args = parser.parse_args(argv)
    if args.json:
        import json

        print(json.dumps({{"skill": "{skill.name}", "checks": CHECKS}}, indent=2))
    else:
        print(f"Skill: {skill.name}")
        for i, item in enumerate(CHECKS, 1):
            print(f"  [ ] {{i}}. {{item}}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''
    refs["scripts/validate_checks.py"] = script
    instructions = skill.instructions.rstrip()
    if "scripts/validate_checks.py" not in instructions:
        instructions += (
            "\n\n## Script\n"
            f"Run `scripts/validate_checks.py` for a deterministic checklist "
            f"(Agent Skills `scripts/` convention).\n"
        )
        instructions = _clip_lines(instructions, max_lines=_BODY_MAX)
    return skill.model_copy(update={"references": refs, "instructions": instructions})


def _clip_lines(text: str, max_lines: int = _BODY_MAX) -> str:
    """Hard cap under 50; collapse blank runs; never pad upward."""
    raw = [ln.rstrip() for ln in (text or "").strip().splitlines()]
    lines: list[str] = []
    blank_run = 0
    for ln in raw:
        if not ln.strip():
            blank_run += 1
            if blank_run > 1:
                continue
            lines.append("")
        else:
            blank_run = 0
            lines.append(ln)
    while lines and not lines[-1]:
        lines.pop()
    if len(lines) <= max_lines:
        return "\n".join(lines)
    return "\n".join(lines[:max_lines])


def _domain_hint(brief: ProjectBrief) -> str:
    parts = [
        brief.problem_statement.strip(),
        brief.tech_stack.strip(),
        ", ".join(brief.domains),
        brief.constraints.strip(),
    ]
    return " | ".join(p for p in parts if p)[:500] or "general software delivery"


async def deduce_plan(
    brief: ProjectBrief,
    platform: Platform,
    *,
    on_progress: ProgressCb | None = None,
    process_context: str = "",
) -> ProjectPlan:
    """Two-phase generation: blueprint, then parallel body expansion.

    `process_context` is the frozen process blueprint (see `DiagramSet.as_generation_context`)
    when the user approved diagrams first. Empty for a project without them, which is what
    keeps the pre-existing flow byte-for-byte unchanged.
    """
    from app.services.brief_expand import expand_problem_statement, is_short_brief

    # Cap context so blueprint stays small and is not truncated mid-JSON
    working = brief
    if is_short_brief(brief.problem_statement) and not (brief.document_text or "").strip():
        await _emit(on_progress, "Expanding short problem statement into a detailed brief…")
        try:
            expanded = await expand_problem_statement(brief.problem_statement)
            working = brief.model_copy(update={"problem_statement": expanded})
        except Exception as exc:  # noqa: BLE001 — fall back to seed
            logger.warning("Brief expand failed, using seed: %s", exc)

    full_context = working.as_prompt_context()
    docs = (working.document_text or "").strip()
    # Prefer raw full docs (fast). Only run parallel digest when too large for prompts.
    if docs:
        from app.services.parser.digest import PASS_THROUGH_CHARS, digest_documents_for_generation

        if len(docs) > PASS_THROUGH_CHARS:
            await _emit(
                on_progress,
                f"Digesting document context in parallel ({len(docs):,} chars)…",
            )
            digested = await digest_documents_for_generation(
                working.problem_statement,
                working.document_text,
            )
            working = working.model_copy(update={"document_text": digested})
            full_context = working.as_prompt_context()
        else:
            await _emit(
                on_progress,
                f"Using complete document context ({len(docs):,} chars)…",
            )

    # The approved process goes first on purpose: it is the part the user explicitly signed
    # off on, so if anything has to be dropped by the length caps below it should be the tail
    # of the raw brief the process was extracted from — not the process itself.
    if process_context.strip():
        full_context = f"{process_context.strip()}\n\n{full_context}"

    blueprint_context = full_context if len(full_context) <= 18000 else full_context[:18000] + "\n…[truncated]"
    expand_context = full_context if len(full_context) <= 28000 else full_context[:28000] + "\n…[truncated]"
    await _emit(
        on_progress,
        f"Designing {platform.value} agent & skill blueprint…",
    )

    blueprint = await llm_client.complete_json(
        [
            {"role": "system", "content": _blueprint_prompt()},
            {
                "role": "user",
                "content": (
                    f"Target IDE/platform: {platform.value}\n"
                    "Tailor agent tools, skill layouts, and rules to THIS IDE only "
                    "(claude_code → .claude/agents + .claude/skills + .claude/rules/*.md, "
                    "plain markdown rules with no frontmatter; "
                    "cursor → .cursor/rules + agents/skills; "
                    "windsurf → .windsurf/agents + .windsurf/skills + .windsurf/rules;"
                    "github_copilot → .github/copilot-instructions.md + "
                    ".github/instructions/*.instructions.md + "
                    ".github/agents/*.agent.md + .github/prompts/*.prompt.md).\n\n"
                    f"{blueprint_context}\n\n"
                    "IMPORTANT: Ground every agent, skill, and rule in the Documents and "
                    "Problem statement above. Use the complete document requirements — "
                    "do not invent an unrelated product.\n\n"
                    "Return ONE complete compact blueprint JSON now."
                ),
            },
        ],
        max_tokens=5000,
        temperature=0.15,
    )

    agents_meta = blueprint.get("agents") or []
    skills_meta = blueprint.get("skills") or []
    rules_meta = blueprint.get("rules") or []
    mcp_meta = blueprint.get("mcp_servers") or []
    source_meta = normalize_source_meta(blueprint.get("source_tree"))

    if not any(a.get("name") == "security-vapt-reviewer" for a in agents_meta):
        agents_meta.append(
            {
                "name": "security-vapt-reviewer",
                "description": (
                    "Pre-VAPT AppSec gate for SQL/XSS/log/command injection, IDOR, SSRF, "
                    "and other common findings. Use before pen-test handoff."
                ),
                "tools": ["Read", "Grep", "Glob"],
                "skills": ["pre-vapt-secure-coding"],
            }
        )
    if not any(s.get("name") == "pre-vapt-secure-coding" for s in skills_meta):
        skills_meta.append(
            {
                "name": "pre-vapt-secure-coding",
                "description": (
                    "Common VAPT checklist: SQLi, XSS, log injection, command injection, "
                    "IDOR, SSRF, CSRF, path traversal, ReDoS, secrets hygiene."
                ),
            }
        )
    if not any(r.get("name") == "secure-coding-vapt" for r in rules_meta):
        rules_meta.append(
            {
                "name": "secure-coding-vapt",
                "description": (
                    "Always-on VAPT gate: no SQLi, XSS, log injection, or other common "
                    "injection/authZ flaws"
                ),
                "always_apply": True,
                "globs": [],
            }
        )

    # Mandatory observability artifacts.
    # Keep the fallback metadata concise: the body expansion below grounds the
    # final agent, skill, and rule in the actual project brief and technology stack.
    if not any(a.get("name") == "observability-agent" for a in agents_meta):
        agents_meta.append(
            {
                "name": "observability-agent",
                "description": (
                    "Design and review project-specific logging, tracing, metrics, "
                    "error monitoring, and a visual observability dashboard."
                ),
                "tools": ["Read", "Grep", "Glob"],
                "skills": ["observability-engineering"],
            }
        )

    if not any(s.get("name") == "observability-engineering" for s in skills_meta):
        skills_meta.append(
            {
                "name": "observability-engineering",
                "description": (
                    "Project-specific observability playbook for telemetry, logs, traces, "
                    "metrics, errors, correlation, and visual dashboard monitoring."
                ),
            }
        )

    if not any(r.get("name") == "observability-standards" for r in rules_meta):
        rules_meta.append(
            {
                "name": "observability-standards",
                "description": (
                    "Always-on observability standards for project-appropriate telemetry, "
                    "monitoring, dashboard visibility, and safe diagnostic logging."
                ),
                "always_apply": True,
                "globs": [],
            }
        )

    # Mandatory guardrail artifacts.
    # Detailed controls are generated from the actual project context during expansion.
    if not any(a.get("name") == "guardrails-agent" for a in agents_meta):
        agents_meta.append(
            {
                "name": "guardrails-agent",
                "description": (
                    "Design and review project-specific input, output, access, data, "
                    "integration, and AI guardrails where applicable."
                ),
                "tools": ["Read", "Grep", "Glob"],
                "skills": ["guardrails-engineering"],
            }
        )

    if not any(s.get("name") == "guardrails-engineering" for s in skills_meta):
        skills_meta.append(
            {
                "name": "guardrails-engineering",
                "description": (
                    "Project-specific guardrail implementation and validation playbook "
                    "based on application trust boundaries, interfaces, data, and AI usage."
                ),
            }
        )

    if not any(r.get("name") == "guardrails-standards" for r in rules_meta):
        rules_meta.append(
            {
                "name": "guardrails-standards",
                "description": (
                    "Always-on project-specific standards for input, output, access, "
                    "data protection, integration, and AI safety controls."
                ),
                "always_apply": True,
                "globs": [],
            }
        )
        
    n_rules = len(rules_meta)
    await _emit(
        on_progress,
        f"Parallel expand for {platform.value}: "
        f"{len(agents_meta)} agents · {len(skills_meta)} skills"
        + (f" · {n_rules} rules" if n_rules else "")
        + " · project structure · WORKBREAKDOWN.md · SDD.md…",
    )

    # Shared concurrency for LLM body writes (agents/skills/rules).
    sem = asyncio.Semaphore(10)

    vapt_expand_hint = (
        "MANDATORY VAPT coverage in the body (name each class): "
        "SQL/NoSQL injection (parameterized/ORM only), XSS (output encoding), "
        "log injection (sanitize CR/LF; no raw untrusted log lines), "
        "command injection, path traversal, SSRF, IDOR/broken authZ, CSRF, "
        "mass assignment, XXE/insecure deserialization, ReDoS (no user regex), "
        "secrets/PII never in logs. Refuse VAPT handoff until High/Critical fixed or waived."
    )

    async def expand_agent(meta: dict[str, Any]) -> AgentSpec:
        async with sem:
            name = str(meta.get("name") or "agent")
            target = _target_lines(name)
            await _emit(on_progress, f"Writing agent: {name}…")
            vapt_extra = (
                f"\n{vapt_expand_hint}\n"
                if name == "security-vapt-reviewer" or "vapt" in name or name.startswith("security")
                else ""
            )
            data = await llm_client.complete_json(
                [
                    {
                        "role": "system",
                        "content": (
                            "You write ONE IDE agent as JSON only: "
                            "{name, description, system_prompt, tools, skills}.\n"
                            f"Target IDE: {platform.value}. Match that IDE's agent conventions.\n"
                            "system_prompt = markdown body only (no frontmatter).\n"
                            f"SOFT TARGET ~{target} lines (not exact). Prefer {_BODY_PREF}. "
                            f"HARD MAX {_BODY_MAX} (<50). Hitting the max is a failure — stop early. "
                            "Vary length; never pad with filler bullets.\n"
                            "Ground content in the provided document/problem context.\n"
                            "Structure: # Role, ## Goals, ## Workflow, ## Example, ## Acceptance.\n"
                            "## Goals MUST be a markdown bullet list (`-` one goal per line). "
                            "NEVER write Goals as a single paragraph or comma-separated wall of text. "
                            "For security-vapt-reviewer: one bullet per vulnerability class."
                            f"{vapt_extra}"
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"Platform: {platform.value}\nAgent meta: {meta}\n\nContext:\n{expand_context}\n\n"
                            f"Write a concise system_prompt (~{target} lines). "
                            "Goals section = bullet points only. Return JSON only."
                        ),
                    },
                ],
                max_tokens=1100,
                temperature=0.35,
            )
            data["name"] = data.get("name") or meta.get("name")
            data["description"] = data.get("description") or meta.get("description") or ""
            data["tools"] = data.get("tools") or meta.get("tools") or ["Read", "Grep", "Glob"]
            data["skills"] = data.get("skills") or meta.get("skills") or []
            prompt = ensure_goals_bullets(
                data.get("system_prompt") or "",
                prefer_vapt_alt=bool(
                    name == "security-vapt-reviewer"
                    or "vapt" in name.lower()
                    or name.lower().startswith("security")
                ),
            )
            data["system_prompt"] = _clip_lines(prompt, max_lines=_BODY_MAX)
            return AgentSpec.model_validate(data)

    async def expand_skill(meta: dict[str, Any]) -> SkillSpec:
        async with sem:
            name = str(meta.get("name") or "skill")
            target = _target_lines(name)
            await _emit(on_progress, f"Writing skill: {name}…")
            vapt_extra = (
                f"\n{vapt_expand_hint}\nInclude a checklist covering each class.\n"
                if name == "pre-vapt-secure-coding" or "vapt" in name or "secure" in name
                else ""
            )
            data = await llm_client.complete_json(
                [
                    {
                        "role": "system",
                        "content": (
                            "You write ONE Agent Skill as JSON: "
                            "{name, description, instructions, scripts?, references?}.\n"
                            f"Target IDE: {platform.value}. Use that IDE's skill folder layout "
                            "(skill-name/SKILL.md + optional scripts/*.py, references/*.md).\n"
                            "Frontmatter fields only name+description (omit disable_model_invocation).\n"
                            f"instructions SOFT TARGET ~{target} lines; prefer {_BODY_PREF}; "
                            f"HARD MAX {_BODY_MAX} (<50). Never pad.\n"
                            "Ground steps in the document/problem context.\n"
                            "Structure: # Title, ## Steps, ## Checks, ## Example.\n"
                            "If the skill has a deterministic checklist/validator, ALSO include "
                            "scripts: {\"scripts/validate.py\": \"<python source>\"} and mention "
                            "running scripts/validate.py in Steps. Otherwise omit scripts."
                            f"{vapt_extra}"
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"Platform: {platform.value}\nSkill meta: {meta}\n\nContext:\n{expand_context}\n\n"
                            f"Write concise instructions (~{target} lines). Return JSON only."
                        ),
                    },
                ],
                max_tokens=1600,
                temperature=0.35,
            )
            data["name"] = data.get("name") or meta.get("name")
            data["description"] = data.get("description") or meta.get("description") or ""
            data["instructions"] = _clip_lines(data.get("instructions") or "", max_lines=_BODY_MAX)
            data.pop("disable_model_invocation", None)
            data["disable_model_invocation"] = None

            refs: dict[str, str] = {}
            raw_refs = data.pop("references", None) or {}
            if isinstance(raw_refs, dict):
                for k, v in raw_refs.items():
                    if isinstance(k, str) and isinstance(v, str) and v.strip():
                        path = k if "/" in k else f"references/{k}"
                        refs[path.replace("\\", "/")] = v.strip()
            raw_scripts = data.pop("scripts", None) or {}
            if isinstance(raw_scripts, dict):
                for k, v in raw_scripts.items():
                    if isinstance(k, str) and isinstance(v, str) and v.strip():
                        path = k if "/" in k else f"scripts/{k}"
                        if not path.startswith("scripts/"):
                            path = f"scripts/{path.split('/')[-1]}"
                        refs[path.replace("\\", "/")] = v.strip()

            skill = SkillSpec.model_validate({**data, "references": refs})
            return _ensure_skill_script(skill)

    async def expand_rule(meta: dict[str, Any]) -> RuleSpec:
        async with sem:
            name = str(meta.get("name") or "rule")
            target = max(10, _target_lines(name) - 2)
            await _emit(on_progress, f"Writing rule: {name}…")
            ide_rule_hint = (
                "Cursor: .cursor/rules/*.mdc with alwaysApply or globs. "
                if platform == Platform.CURSOR
                else "Windsurf: .windsurf/agents/*/AGENT.md, .windsurf/skills/*/SKILL.md, .windsurf/rules/*.md. "
                if platform == Platform.WINDSURF
                # Claude Code has no frontmatter dialect: rules are plain markdown that
                # CLAUDE.md imports with @path, so the scope has to be stated in the body.
                else (
                    "Claude Code: .claude/rules/*.md, plain markdown with NO frontmatter. "
                    "Open the body with an 'Applies to: always.' or "
                    "'Applies to: `<glob>`.' line, since there is no field for it. "
                    if platform == Platform.CLAUDE_CODE
                    else ""
                )
            )
            # GitHub Copilot: additive platform-specific rule guidance.
            # Existing Claude Code, Cursor, and Windsurf logic above remains unchanged.
            if platform == Platform.GITHUB_COPILOT:
                ide_rule_hint = (
                    "GitHub Copilot: use .github/copilot-instructions.md for "
                    "repository-wide instructions and "
                    ".github/instructions/*.instructions.md for path-specific instructions. "
                )

            vapt_extra = (
                f"\n{vapt_expand_hint}\n"
                if name == "secure-coding-vapt" or "vapt" in name
                else ""
            )
            data = await llm_client.complete_json(
                [
                    {
                        "role": "system",
                        "content": (
                            "You write ONE project rule as JSON: "
                            "{name, description, body, always_apply, globs}.\n"
                            f"Target IDE: {platform.value}. {ide_rule_hint}"
                            "Omit alwaysApply:false; use always_apply:true OR globs.\n"
                            f"body SOFT TARGET ~{target} lines; HARD MAX {_BODY_MAX} (<50). Never pad.\n"
                            "Ground the rule in the document/problem context."
                            f"{vapt_extra}"
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"Platform: {platform.value}\nRule meta: {meta}\n\nContext:\n{expand_context}\n\n"
                            f"Write a concise body (~{target} lines). Return JSON only."
                        ),
                    },
                ],
                max_tokens=900,
                temperature=0.3,
            )
            data["name"] = data.get("name") or meta.get("name")
            data["description"] = data.get("description") or meta.get("description") or ""
            data["always_apply"] = bool(data.get("always_apply", meta.get("always_apply", False)))
            data["globs"] = data.get("globs") or meta.get("globs") or []
            data["body"] = _clip_lines(data.get("body") or "", max_lines=_BODY_MAX)
            return RuleSpec.model_validate(data)

    async def expand_source_tree(meta: list[dict[str, str]]) -> list[SourceFileSpec]:
        """Build modular app tree in parallel with LLM expands — no extra LLM round-trip."""
        await _emit(on_progress, "Building modular project folder structure…")
        project_name = str(blueprint.get("project_name") or "agentcraft-project")[:64]
        summary = str(blueprint.get("summary") or working.problem_statement[:400] or "")

        def _build() -> list[SourceFileSpec]:
            # Always analyze brief → full modular layout, then overlay blueprint paths.
            base = build_source_tree_from_brief(
                working, project_name=project_name, summary=summary
            )
            return merge_llm_source_files(
                meta,
                [{"path": f.path, "purpose": f.purpose} for f in base],
                working,
                project_name=project_name,
                summary=summary,
            )

        return await asyncio.to_thread(_build)

    project_name = str(blueprint.get("project_name") or "agentcraft-project")[:64]
    summary = str(blueprint.get("summary") or working.problem_statement[:400] or "Generated plan")

    def _stub_agent(meta: dict[str, Any]) -> AgentSpec:
        return AgentSpec(
            name=str(meta.get("name") or "agent"),
            description=str(meta.get("description") or ""),
            system_prompt="(generating…)",
            tools=list(meta.get("tools") or ["Read", "Grep", "Glob"]),
            skills=list(meta.get("skills") or []),
        )

    def _stub_skill(meta: dict[str, Any]) -> SkillSpec:
        return SkillSpec(
            name=str(meta.get("name") or "skill"),
            description=str(meta.get("description") or ""),
            instructions="(generating…)",
        )

    wbs_stub = ProjectPlan(
        project_name=project_name,
        summary=summary,
        agents=[_stub_agent(m) for m in agents_meta],
        skills=[_stub_skill(m) for m in skills_meta],
        rules=[],
        source_tree=[],
    )

    # Started before the document tasks because SDD.md's Development View is drawn from the
    # real scaffold paths. This build is deterministic and runs in a thread — no LLM round
    # trip — so waiting on it costs milliseconds, not a call.
    source_task = asyncio.create_task(expand_source_tree(source_meta))

    async def _run_wbs() -> tuple[str, bool]:
        return await generate_work_breakdown(
            working, wbs_stub, platform, on_progress=on_progress
        )

    async def _run_sdd() -> tuple[str, bool, dict]:
        tree = await source_task
        return await generate_solution_design(
            working,
            wbs_stub.model_copy(update={"source_tree": list(tree)}),
            platform,
            on_progress=on_progress,
        )

    wbs_task = asyncio.create_task(_run_wbs())
    sdd_task = asyncio.create_task(_run_sdd())

    # True parallel fan-out: agents, skills, rules, project structure, WORKBREAKDOWN.md AND
    # SDD.md. The two documents are six more Bedrock calls, all of them concurrent with the
    # agent/skill expansion, so generate costs one call's latency rather than the sum.
    agent_task = asyncio.gather(*[expand_agent(m) for m in agents_meta])
    skill_task = asyncio.gather(*[expand_skill(m) for m in skills_meta])
    rule_task = asyncio.gather(*[expand_rule(m) for m in rules_meta])
    agents, skills, rules, source_tree = await asyncio.gather(
        agent_task, skill_task, rule_task, source_task
    )

    await _emit(on_progress, f"Assembling final {platform.value} workspace plan…")
    mcp_servers = [McpServerSpec.model_validate(m) for m in mcp_meta if m]

    final_stub = ProjectPlan(
        project_name=project_name,
        summary=summary,
        agents=list(agents),
        skills=list(skills),
        rules=list(rules),
        mcp_servers=mcp_servers,
        source_tree=list(source_tree),
    )
    work_breakdown_llm = False
    work_breakdown_complete = False
    try:
        work_breakdown, work_breakdown_llm = await wbs_task
        work_breakdown_complete = is_work_breakdown_complete(work_breakdown)
        work_breakdown_llm = work_breakdown_llm and work_breakdown_complete
    except Exception as exc:  # noqa: BLE001
        logger.warning("Work breakdown generation failed: %s", exc)
        work_breakdown = fallback_work_breakdown(working, final_stub, platform)
        work_breakdown_complete = is_work_breakdown_complete(work_breakdown)

    solution_design_llm = False
    solution_design_complete = False
    try:
        solution_design, solution_design_llm, architecture_views = await sdd_task
        solution_design_complete = is_solution_design_complete(solution_design)
        solution_design_llm = solution_design_llm and solution_design_complete
    except Exception as exc:  # noqa: BLE001
        logger.warning("Solution design generation failed: %s", exc)
        solution_design = fallback_solution_design(working, final_stub, platform)
        solution_design_complete = is_solution_design_complete(solution_design)
        architecture_views = fallback_solution_design_views(working, final_stub)

    return ProjectPlan(
        project_name=project_name,
        summary=summary,
        agents=list(agents),
        skills=list(skills),
        rules=list(rules),
        mcp_servers=mcp_servers,
        source_tree=list(source_tree),
        work_breakdown=work_breakdown,
        work_breakdown_llm=work_breakdown_llm,
        work_breakdown_complete=work_breakdown_complete,
        solution_design=solution_design,
        solution_design_llm=solution_design_llm,
        solution_design_complete=solution_design_complete,
        architecture_views=architecture_views,
    )


# Keep demo_plan in this module (imported by tests / --demo)
def demo_plan(brief: ProjectBrief, platform: Platform) -> ProjectPlan:
    from app.services.deduction import demo_fallback

    return demo_fallback.build_demo_plan(brief, platform)
