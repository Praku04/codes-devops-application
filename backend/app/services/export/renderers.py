"""Platform exporters for Claude Code, Cursor, Windsurf, and GitHub Copilot."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from typing import Callable

from app.core.state_machine import Platform
from app.models.schemas import ProjectBrief, ProjectPlan, RuleSpec, SkillSpec
from app.services.deduction.project_rules import (
    CLAUDE_RULES_DIR,
    claude_rule_path,
    ensure_claude_rules,
    ensure_project_rules,
    rules_dir,
)
from app.services.deduction.solution_design import (
    SDD_FILENAME,
    fallback_solution_design,
    plan_solution_design_body,
)
from app.services.deduction.work_breakdown import (
    WORK_BREAKDOWN_FILENAME,
    fallback_work_breakdown,
    plan_work_breakdown_body,
    refresh_rules_section,
)
from app.services.diagrams.architecture import ARCH_DOC_DIR
from app.services.export.root_files import (
    CLAUDE_MEMORY_FILENAME,
    GITIGNORE_FILENAME,
    render_claude_md,
    render_gitignore,
)


def _clean(text: str) -> str:
    return " ".join((text or "").strip().split())


def _short(text: str, limit: int = 240) -> str:
    src = _clean(text)
    if len(src) <= limit:
        return src
    return src[: max(0, limit - 3)].rstrip() + "..."


def _rules_apply_mode(always_apply: bool, globs: list[str]) -> str:
    if always_apply:
        return "always on"
    if globs:
        return f"scoped by globs ({', '.join(globs)})"
    return "model decision"


def _ascii_tree(paths: list[str]) -> list[str]:
    """Compact indented tree from a flat path list."""
    root: dict = {}
    for raw in sorted(set(p.replace("\\", "/").lstrip("/") for p in paths if p)):
        node = root
        parts = raw.split("/")
        for i, part in enumerate(parts):
            node = node.setdefault(part, {})
            if i == len(parts) - 1:
                node.setdefault("__file__", True)

    lines: list[str] = []

    def walk(n: dict, prefix: str = "") -> None:
        entries = sorted(k for k in n if k != "__file__")
        for i, name in enumerate(entries):
            last = i == len(entries) - 1
            branch = "└── " if last else "├── "
            child = n[name]
            is_dir = any(k != "__file__" for k in child)
            lines.append(f"{prefix}{branch}{name}{'/' if is_dir and '__file__' not in child else ''}")
            if is_dir:
                ext = "    " if last else "│   "
                walk(child, prefix + ext)

    walk(root)
    return lines or ["(empty)"]


def _is_ide_or_readme(path: str) -> bool:
    """
    True for paths the exporter generates rather than scaffolds.

    `.gitignore` and `CLAUDE.md` are here because `render_gitignore` / `render_claude_md`
    produce them from the plan on every render — they are workspace files, never source
    files, so counting them as scaffold would inflate the Files badge by two.
    """
    p = path.replace("\\", "/").lstrip("/")
    return p in {
        "README.md",
        "README.agentcraft.md",
        "AGENTS.md",
        ".mcp.json",
        GITIGNORE_FILENAME,
        CLAUDE_MEMORY_FILENAME,
        ".github/copilot-instructions.md",
    } or any(
        p.startswith(pref) for pref in (".cursor/", ".claude/", ".windsurf/", ".agents/", ".github/instructions/", ".github/agents/",".github/skills/",".github/prompts/",)
    )


def scaffold_file_count(plan: ProjectPlan) -> int:
    """
    Application scaffold files only — the number the UI's **Files** tab shows.

    This is `source_tree` minus anything that is really an IDE artifact, so it matches
    what `_attach_source_tree` actually writes rather than the raw list length.
    """
    return sum(
        1
        for spec in (plan.source_tree or [])
        if (spec.path or "").strip() and not _is_ide_or_readme(spec.path)
    )


# The README is written *into* the file set it describes, so the total is not known
# while rendering it. Exporters emit this placeholder and `render_files` substitutes the
# real count once every path exists — the number can then never drift from the output.
WORKSPACE_COUNT_TOKEN = "{{AGENTCRAFT_WORKSPACE_FILE_COUNT}}"


def _attach_source_tree(files: dict[str, str], plan: ProjectPlan) -> None:
    """Write modular application scaffold beside IDE folders (docstring/role only)."""
    from app.services.deduction.source_tree import docstring_only_content

    for spec in plan.source_tree or []:
        path = spec.path.replace("\\", "/").lstrip("/")
        if not path or _is_ide_or_readme(path):
            continue
        # Never export executable scaffold code — Role docstring/comment only
        body = docstring_only_content(path, spec.purpose or f"Scaffold for `{path}`").strip()
        files[path] = body if body.endswith("\n") else body + "\n"


def _work_breakdown_markdown(plan: ProjectPlan, platform: Platform, brief=None) -> str:
    from app.models.schemas import ProjectBrief

    body = plan_work_breakdown_body(plan)
    if body:
        # A stored body predates Claude rules and still tells the reader they are not
        # exported. Refresh just that section so existing projects read correctly while
        # every hand-written phase is left untouched.
        body = refresh_rules_section(body, plan, platform)
        return body if body.endswith("\n") else body + "\n"
    return fallback_work_breakdown(brief or ProjectBrief(), plan, platform)


def _solution_design_markdown(
    plan: ProjectPlan, platform: Platform, brief: ProjectBrief | None = None
) -> str:
    """
    `SDD.md` for the export.

    No `refresh_*` counterpart: unlike WORKBREAKDOWN.md, this document never described the
    IDE rules folder, so a stored body from an earlier version has nothing stale in it to
    correct. A hand-edited copy is returned exactly as the user left it.

    `brief` is a last resort. The service layer stores a brief-derived document on the plan
    before anything renders (`_ensure_solution_design` in services/projects.py), so the
    empty-brief path here only runs for a plan that has never been previewed or exported.
    """
    body = plan_solution_design_body(plan)
    if body:
        return body if body.endswith("\n") else body + "\n"
    return fallback_solution_design(brief or ProjectBrief(), plan, platform)


def _overview_readme(
    plan: ProjectPlan, platform: Platform, *, rules: list[RuleSpec] | None = None
) -> str:
    """
    Project README.

    `rules` lets each exporter pass the rules it actually wrote. Rules are derived from the
    plan, so `plan.rules` can be empty — always for an existing Claude project, and for any
    plan the LLM returned no rules for — and reading it would report "0 rules" beside a
    populated rules folder.
    """
    platform_label = {
        Platform.CLAUDE_CODE: "Claude Code",
        Platform.CURSOR: "Cursor",
        Platform.WINDSURF: "Windsurf",
        Platform.GITHUB_COPILOT: "GitHub Copilot",
    }[platform]
    exported_rules = list(rules) if rules is not None else list(plan.rules or [])
    has_rules = bool(exported_rules)

    lines: list[str] = [
        f"# {plan.project_name}",
        "",
        _clean(plan.summary) or "Generated workspace plan.",
        "",
        "## Workspace purpose",
        f"Generated for **{platform_label}**: IDE agents/skills/rules plus a modular app scaffold "
        "analyzed from the brief (entrypoint `main.py`, typically `backend/` + optional `frontend/`).",
        "",
        "## Artifact counts",
        f"- Agents: {len(plan.agents)} | Skills: {len(plan.skills)} | "
        f"Rules: {len(exported_rules)} | MCP: {len(plan.mcp_servers)} | "
        f"Files: {scaffold_file_count(plan)}",
        "",
        "Two file counts, because they measure different things:",
        f"- **Source files: {scaffold_file_count(plan)}** — the application scaffold listed "
        "below. This is the number on the UI's **Files** tab.",
        f"- **Workspace files: {WORKSPACE_COUNT_TOKEN}** — every path in this export: the "
        f"scaffold plus the IDE folder{' and rules' if has_rules else ''}, this README, "
        f"`{WORK_BREAKDOWN_FILENAME}` and `{SDD_FILENAME}`. This is what the file tree lists "
        "and what the zip contains.",
        "",
        "## Project structure",
        "Realistic modular layout for this product (not a fixed template). "
        "Backend prefers `backend/api|services|repositories|models|schemas|core|utils|tests`; "
        "frontend (when present) prefers `frontend/app|components|features|hooks|lib|services|types|tests`.",
        "```",
    ]
    ide_roots = {
        Platform.CLAUDE_CODE: [".claude/agents/", ".claude/skills/", f"{CLAUDE_RULES_DIR}/"],
        Platform.CURSOR: [".cursor/agents/", ".cursor/skills/", ".cursor/rules/"],
        Platform.WINDSURF: [".windsurf/agents/", ".windsurf/skills/", ".windsurf/rules/"],
        Platform.GITHUB_COPILOT: [".github/agents/",".github/skills/",".github/instructions/", ".github/copilot-instructions.md",],
    }[platform]
    for root in ide_roots:
        lines.append(root)
    tree_paths = [s.path for s in (plan.source_tree or [])]
    extras = [WORK_BREAKDOWN_FILENAME, "README.md", GITIGNORE_FILENAME]
    if platform == Platform.CLAUDE_CODE:
        extras.append(CLAUDE_MEMORY_FILENAME)
    for extra in extras:
        if extra not in tree_paths:
            tree_paths.append(extra)
    lines.extend(_ascii_tree(sorted(set(tree_paths))))
    lines.append("```")
    lines.append("")
    lines.append("## File purposes")
    lines.append(
        f"- **`{WORK_BREAKDOWN_FILENAME}`** — Phased implementation plan: scope, tech stack, "
        "agents/skills/rules, per-phase tasks, testing, and acceptance gates. Start here after this README."
    )
    lines.append("- **`README.md`** — This overview: structure, agents, skills, rules, and getting started.")
    if platform == Platform.CLAUDE_CODE:
        lines.append(
            f"- **`{CLAUDE_MEMORY_FILENAME}`** — Project memory Claude Code loads into context at "
            "the start of every session: layout, layer boundaries, and run commands. Kept short "
            "on purpose — it is re-read on every prompt, so it points here instead of repeating "
            "this file. Edit it as the project's conventions change."
        )
    lines.append(
        f"- **`{GITIGNORE_FILENAME}`** — Ignore rules for this tech stack. Excludes `.env`, "
        "caches, build output and local IDE state; the `.claude/` / `.cursor/` / `.windsurf/` "
        "workspace is deliberately **tracked** so the whole team shares the same agents."
    )
    if plan.source_tree:
        for spec in plan.source_tree:
            purpose = _clean(spec.purpose) or "Scaffold module for this product."
            lines.append(f"- `{spec.path}` — {purpose}")
    else:
        lines.append("- No application source tree attached.")
    lines.append("")
    lines.append("## Agents and purpose")
    if plan.agents:
        for a in plan.agents:
            tools = ", ".join(a.tools) if a.tools else "default"
            skills = ", ".join(a.skills) if a.skills else "none"
            lines.append(f"- `{a.name}` — {_short(a.description, 320)} (tools: {tools}; skills: {skills})")
    else:
        lines.append("- No agents generated.")
    lines.append("")
    lines.append("## Skills and purpose")
    if plan.skills:
        for s in plan.skills:
            extra = f"; refs: {', '.join(sorted(s.references.keys()))}" if s.references else ""
            lines.append(f"- `{s.name}` — {_short(s.description, 320)}{extra}")
    else:
        lines.append("- No skills generated.")
    lines.append("")
    lines.append("## Rules and purpose")
    if exported_rules:
        ext = ".mdc" if platform == Platform.CURSOR else ".md"
        lines.append(f"Exported under `{rules_dir(platform)}` as `{ext}` files.")
        lines.append("")
        if platform == Platform.CLAUDE_CODE:
            lines.append(
                f"Claude Code rules are plain markdown — no `alwaysApply`/`globs` frontmatter. "
                f"Always-on rules are imported from `{CLAUDE_MEMORY_FILENAME}` with `@` references so "
                "they are in context every session; glob-scoped rules state their scope in their "
                f"own header and are listed in `{CLAUDE_MEMORY_FILENAME}` for Claude to read when it "
                "touches those paths."
            )
        elif platform == Platform.CURSOR:
            lines.append(
                "Apply mode is `alwaysApply: true` frontmatter for always-on rules and a "
                "`globs:` list for scoped ones, so Cursor loads each at the right moment."
            )
        elif platform == Platform.GITHUB_COPILOT:
            lines.append(
                "GitHub Copilot instructions use the `applyTo` frontmatter field to scope "
                "instructions to matching files. Repository-wide instructions are stored in "
                "`.github/copilot-instructions.md`."
            )
        else:
            lines.append(
                "Apply mode is the `trigger` frontmatter field — `always_on`, `glob` (with "
                "`globs:`), or `model_decision` — so Cascade loads each at the right moment."
            )
        lines.append("")
        lines.append(
            "These are project-specific: they name this project's own domain slices, agents, "
            "and skills rather than generic advice."
        )
        lines.append("")
        for r in exported_rules:
            lines.append(
                f"- `{r.name}` — {_short(r.description, 320)} "
                f"({_rules_apply_mode(r.always_apply, r.globs)})"
            )
    else:
        lines.append("- No rules generated for this plan.")
    lines.append("")
    lines.append("## MCP servers")
    if plan.mcp_servers:
        for s in plan.mcp_servers:
            if s.command:
                lines.append(f"- `{s.name}` — `{' '.join([s.command, *s.args])}`")
            elif s.url:
                lines.append(f"- `{s.name}` — {s.url}")
            else:
                lines.append(f"- `{s.name}`")
    else:
        lines.append("- None configured.")
    lines.append("")
    lines.append(f"## {WORK_BREAKDOWN_FILENAME} — phased implementation plan")
    lines.append("")
    lines.append(
        f"**`{WORK_BREAKDOWN_FILENAME}`** is indexed in the project structure above. "
        "It is a document-grounded, phase-by-phase engineering plan derived from your "
        "problem statement and uploaded documents."
    )
    lines.append("")
    lines.append("What it contains:")
    lines.append("- Product scope, tech stack, and assumptions from your brief")
    lines.append("- **Agents**, **Skills**, and **Rules** mapped to implementation work")
    lines.append("- Numbered **phases** with tasks, testing strategy, acceptance gates, and copy-paste agent prompts")
    lines.append("")
    lines.append("### How to get started with WORKBREAKDOWN.md")
    lines.append("1. Open **`WORKBREAKDOWN.md`** beside this README.")
    lines.append("2. Read **Phase overview**, then start with **Phase 1** only.")
    lines.append("3. In your IDE agent chat, paste: `Implement Phase 1 from WORKBREAKDOWN.md`.")
    lines.append("4. Complete every item under **Test & acceptance** and **Phase completion checklist** for that phase.")
    lines.append("5. Mark tasks `- [x]`, then repeat for Phase 2, Phase 3, …")
    ide_dir = {
        Platform.CLAUDE_CODE: ".claude/",
        Platform.CURSOR: ".cursor/",
        Platform.WINDSURF: ".windsurf/",
        Platform.GITHUB_COPILOT: ".github/",
    }[platform]
    lines.append(
        f"6. Use the agents/skills/rules listed in WORKBREAKDOWN.md and exported under `{ide_dir}`."
    )
    lines.append("")
    lines.append(f"## {SDD_FILENAME} — solution design document")
    lines.append("")
    lines.append(
        f"**`{SDD_FILENAME}`** is the design behind the plan. Where `{WORK_BREAKDOWN_FILENAME}` "
        "says *in what order* the work happens, this says *what is being built and why it is "
        "shaped this way* — the document you review before engineering starts, and the one to "
        "reach for when a decision needs justifying later."
    )
    lines.append("")
    lines.append("What it contains, as numbered sections:")
    lines.append("1. **Project Summary** — problem statement and objective")
    lines.append("2. **Scope** — in-scope, functional and non-functional requirements, out of scope")
    lines.append(
        "3. **Solution Definition** — overview, the three **architectural views** (logical, "
        f"development, deployment) as drawn diagrams in `{ARCH_DOC_DIR}/`, development "
        "framework, setup and configuration/migration requirements, Docker deployment, coding "
        "best practices, and AI guardrails & data security"
    )
    lines.append("4. **Dependencies** — what has to arrive from elsewhere, and who owns it")
    lines.append("5. **Assumptions** — every decision taken without a confirmed answer")
    lines.append("6. **Challenges and Risks** — with likelihood, impact, mitigation and owner")
    lines.append("7. **Acceptance Criteria** — each with the method that proves it")
    lines.append("")
    lines.append(
        "The requirement IDs in §2.2 are what §7 and the phase gates in "
        f"`{WORK_BREAKDOWN_FILENAME}` refer back to, so the three documents read as one trail: "
        "requirement → phase → acceptance. Each architectural view is a PNG in "
        f"`{ARCH_DOC_DIR}/` with the same content repeated as a table beneath it, so the "
        "picture works wherever the file is opened and the detail stays searchable."
    )
    lines.append("")
    lines.append("## How to use this workspace")
    lines.append(f"0. Skim **{SDD_FILENAME}** §1–§3 so you know what you are building and why.")
    lines.append("1. Read **WORKBREAKDOWN.md** and pick Phase 1.")
    lines.append("2. Read **File purposes** below and start from `main.py` / `backend/api/app.py`.")
    lines.append("3. Implement in `backend/services` + `repositories`; keep routes thin.")
    lines.append("4. Build UI under `frontend/` (Next.js or Jinja templates + static JS) when present.")
    rules_path = (
        ".github/instructions/"
        if platform == Platform.GITHUB_COPILOT
        else f"{ide_dir}rules/"
    )
    lines.append(
        f"5. Use matching agents/skills; the rules under `{rules_path}` apply as you edit."
    )
    lines.append("6. Run phase tests before starting the next phase.")
    lines.append("")
    lines.append(f"Generated for {platform_label} by AgentCraft.")
    lines.append("")
    return "\n".join(lines)


def _yaml_escape(value: str) -> str:
    if any(c in value for c in [":", "#", "\n", '"', "'"]):
        escaped = value.replace('"', '\\"')
        return f'"{escaped}"'
    return value


def _frontmatter(fields: dict[str, object]) -> str:
    lines = ["---"]
    for key, val in fields.items():
        if val is None:
            continue
        if isinstance(val, bool):
            # Only emit true flags; omit false noise (Cursor/Claude docs prefer omit-or-true)
            if not val:
                continue
            lines.append(f"{key}: true")
        elif isinstance(val, list):
            if not val:
                continue
            if all(isinstance(x, str) for x in val):
                if key == "globs" and len(val) > 1:
                    lines.append(f"{key}:")
                    for item in val:
                        lines.append(f"  - {_yaml_escape(item)}")
                elif key == "globs" and len(val) == 1:
                    lines.append(f"{key}: {_yaml_escape(val[0])}")
                else:
                    # tools / skills / args: comma-separated (Claude Code / Cursor agent style)
                    lines.append(f"{key}: {', '.join(_yaml_escape(x) for x in val)}")
            else:
                lines.append(f"{key}: {val}")
        else:
            lines.append(f"{key}: {_yaml_escape(str(val))}")
    lines.append("---")
    return "\n".join(lines)


def _skill_frontmatter(skill: SkillSpec) -> dict[str, object]:
    """
    Cursor/Claude/Windsurf Agent Skills:
    - required: name, description
    - disable-model-invocation: only when true (explicit/slash invoke)
    - omit the key for auto-discovery skills (do NOT write false)
    """
    fm: dict[str, object] = {"name": skill.name, "description": skill.description}
    if skill.disable_model_invocation is True:
        fm["disable-model-invocation"] = True
    return fm


def _claude_rule_markdown(rule: RuleSpec) -> str:
    """
    One `.claude/rules/<name>.md` file.

    No YAML frontmatter: Claude Code reads these as plain markdown imported from
    `CLAUDE.md`, and an unrecognised `alwaysApply:`/`globs:` block would just render as
    text at the top of the rule. The apply mode is written into the body instead — the
    only place Claude will actually read it — and inserted here rather than left to the
    author, so an LLM- or user-written rule states its scope too instead of silently
    losing it on the way from `RuleSpec` to markdown.
    """
    body = rule.body.strip()
    title, rest = (
        (body.split("\n", 1) + [""])[:2] if body.startswith("#") else (f"# {rule.name}", body)
    )
    if "Applies to:" in body:
        return f"{title}\n{rest}".rstrip() + "\n"

    if rule.always_apply or not rule.globs:
        scope = "**Applies to: always.**"
    else:
        scope = f"**Applies to: `{'`, `'.join(rule.globs)}`.**"
    return f"{title}\n\n{scope}\n{rest}".rstrip() + "\n"


def export_claude_code(plan: ProjectPlan) -> dict[str, str]:
    files: dict[str, str] = {}
    for agent in plan.agents:
        body = "\n".join(
            [
                _frontmatter(
                    {
                        "name": agent.name,
                        "description": agent.description,
                        "tools": agent.tools or None,
                        "skills": agent.skills or None,
                    }
                ),
                "",
                agent.system_prompt.strip(),
                "",
            ]
        )
        files[f".claude/agents/{agent.name}.md"] = body

    for skill in plan.skills:
        content = f"{_frontmatter(_skill_frontmatter(skill))}\n\n# {skill.name}\n\n{skill.instructions.strip()}\n"
        files[f".claude/skills/{skill.name}/SKILL.md"] = content
        for ref_name, ref_body in skill.references.items():
            files[f".claude/skills/{skill.name}/{ref_name}"] = ref_body

    if plan.mcp_servers:
        mcp = {
            "mcpServers": {
                s.name: {
                    **({"command": s.command, "args": s.args} if s.command else {}),
                    **({"url": s.url} if s.url else {}),
                    **({"env": s.env} if s.env else {}),
                }
                for s in plan.mcp_servers
            }
        }
        files[".mcp.json"] = json.dumps(mcp, indent=2) + "\n"

    # Claude Code rules are plain markdown under .claude/rules — no `.mdc` frontmatter
    # dialect. Derived from the plan, so existing projects (which have `rules: []`, since
    # Claude used to be stripped of them) gain them on their next render.
    rules = ensure_claude_rules(plan)
    for rule in rules:
        files[claude_rule_path(rule.name)] = _claude_rule_markdown(rule)

    files["README.md"] = _overview_readme(plan, Platform.CLAUDE_CODE, rules=rules)
    files[WORK_BREAKDOWN_FILENAME] = _work_breakdown_markdown(plan, Platform.CLAUDE_CODE)
    files[SDD_FILENAME] = _solution_design_markdown(plan, Platform.CLAUDE_CODE)
    # Claude Code auto-loads CLAUDE.md from the project root on every session, so it goes
    # at the root beside main.py — not inside .claude/, where it would be ignored.
    files[CLAUDE_MEMORY_FILENAME] = render_claude_md(plan, rules=rules)
    files[GITIGNORE_FILENAME] = render_gitignore(plan)
    _attach_source_tree(files, plan)
    return files


def export_cursor(plan: ProjectPlan) -> dict[str, str]:
    files: dict[str, str] = {}
    # Same derived floor as the other IDEs: a plan the LLM returned no rules for still gets
    # project-specific architecture/security/workflow rules instead of an empty folder.
    rules = ensure_project_rules(plan, Platform.CURSOR)
    for rule in rules:
        # Cursor .mdc: alwaysApply:true OR globs (omit alwaysApply:false noise)
        fields: dict[str, object] = {"description": rule.description}
        if rule.always_apply:
            fields["alwaysApply"] = True
        elif rule.globs:
            fields["globs"] = rule.globs
        else:
            # intelligent apply via description only
            pass
        files[f".cursor/rules/{rule.name}.mdc"] = (
            f"{_frontmatter(fields)}\n\n# {rule.name}\n\n{rule.body.strip()}\n"
        )

    for skill in plan.skills:
        files[f".cursor/skills/{skill.name}/SKILL.md"] = (
            f"{_frontmatter(_skill_frontmatter(skill))}\n\n# {skill.name}\n\n{skill.instructions.strip()}\n"
        )
        for ref_name, ref_body in skill.references.items():
            rel = ref_name.replace("\\", "/").lstrip("/")
            files[f".cursor/skills/{skill.name}/{rel}"] = ref_body

    for agent in plan.agents:
        files[f".cursor/agents/{agent.name}.md"] = (
            f"{_frontmatter({'name': agent.name, 'description': agent.description})}\n\n"
            f"{agent.system_prompt.strip()}\n"
        )

    if plan.mcp_servers:
        mcp = {
            "mcpServers": {
                s.name: {
                    **({"command": s.command, "args": s.args} if s.command else {}),
                    **({"url": s.url} if s.url else {}),
                    **({"env": s.env} if s.env else {}),
                }
                for s in plan.mcp_servers
            }
        }
        files[".cursor/mcp.json"] = json.dumps(mcp, indent=2) + "\n"

    files["README.md"] = _overview_readme(plan, Platform.CURSOR, rules=rules)
    files[WORK_BREAKDOWN_FILENAME] = _work_breakdown_markdown(plan, Platform.CURSOR)
    files[SDD_FILENAME] = _solution_design_markdown(plan, Platform.CURSOR)
    # No CLAUDE.md: Cursor reads .cursor/rules, and a memory file it never loads is dead
    # weight in the repo.
    files[GITIGNORE_FILENAME] = render_gitignore(plan)
    _attach_source_tree(files, plan)
    return files


def export_windsurf(plan: ProjectPlan) -> dict[str, str]:
    """
    Windsurf / Cascade layout (docs.devin.ai):
    - Skills: .windsurf/skills/<name>/SKILL.md  (workspace — do NOT also write .agents/skills)
    - Rules:  .windsurf/rules/<name>.md with trigger frontmatter
    - Agents: .windsurf/agents/<name>/AGENT.md
    - AGENTS.md at repo root (directory-scoped Cascade instructions)
    - README.md project overview
    """
    files: dict[str, str] = {}
    # Derived floor as on Claude and Cursor — see `ensure_project_rules`.
    rules = ensure_project_rules(plan, Platform.WINDSURF)
    overview = _overview_readme(plan, Platform.WINDSURF, rules=rules)
    files["AGENTS.md"] = overview

    for rule in rules:
        if rule.always_apply:
            fields: dict[str, object] = {
                "trigger": "always_on",
                "description": rule.description,
            }
        elif rule.globs:
            fields = {
                "trigger": "glob",
                "description": rule.description,
                "globs": rule.globs,
            }
        else:
            fields = {"trigger": "model_decision", "description": rule.description}
        files[f".windsurf/rules/{rule.name}.md"] = (
            f"{_frontmatter(fields)}\n\n{rule.body.strip()}\n"
        )

    for skill in plan.skills:
        # Official workspace path only — duplicating under .agents/skills makes Cascade
        # (and our Files tree) show every skill twice.
        body = (
            f"{_frontmatter(_skill_frontmatter(skill))}\n\n"
            f"# {skill.name}\n\n{skill.instructions.strip()}\n"
        )
        files[f".windsurf/skills/{skill.name}/SKILL.md"] = body
        for ref_name, ref_body in skill.references.items():
            rel = ref_name.replace("\\", "/").lstrip("/")
            files[f".windsurf/skills/{skill.name}/{rel}"] = ref_body

    for agent in plan.agents:
        content = (
            f"{_frontmatter({'name': agent.name, 'description': agent.description})}\n\n"
            f"{agent.system_prompt.strip()}\n"
        )
        files[f".windsurf/agents/{agent.name}/AGENT.md"] = content

    files["README.md"] = overview
    files[WORK_BREAKDOWN_FILENAME] = _work_breakdown_markdown(plan, Platform.WINDSURF)
    files[SDD_FILENAME] = _solution_design_markdown(plan, Platform.WINDSURF)
    # Windsurf's equivalent of CLAUDE.md is AGENTS.md, already written above.
    files[GITIGNORE_FILENAME] = render_gitignore(plan)
    _attach_source_tree(files, plan)
    return files

def export_github_copilot(plan: ProjectPlan) -> dict[str, str]:
    """GitHub Copilot workspace customization."""
    files: dict[str, str] = {}

    rules = ensure_project_rules(plan, Platform.GITHUB_COPILOT)

    # Repository-wide Copilot instructions.
    files[".github/copilot-instructions.md"] = (
        f"# {plan.project_name}\n\n"
        f"{_clean(plan.summary) or 'Generated workspace plan.'}\n\n"
        "## Working instructions\n\n"
        "- Follow the architecture, security, observability, guardrails, workflow, and testing instructions "
        "under `.github/instructions/`.\n"
        "- Use the specialized agents under `.github/agents/` when appropriate.\n"
        "- Use project skills under `.github/skills/` when their descriptions match "
        "the current task.\n"
        f"- Follow `{WORK_BREAKDOWN_FILENAME}` one phase at a time.\n"
        f"- Treat `{SDD_FILENAME}` as the solution-design reference.\n"
    )

    # Path-specific / repository instruction files.
    for rule in rules:
        apply_to = "**/*"
        if not rule.always_apply and rule.globs:
            apply_to = ",".join(rule.globs)

        fields: dict[str, object] = {
            "applyTo": apply_to,
        }

        files[f".github/instructions/{rule.name}.instructions.md"] = (
            f"{_frontmatter(fields)}\n\n"
            f"{rule.body.strip()}\n"
        )

    # Copilot custom agents.
    for agent in plan.agents:
        files[f".github/agents/{agent.name}.agent.md"] = (
            f"{_frontmatter({'name': agent.name, 'description': agent.description})}\n\n"
            f"{agent.system_prompt.strip()}\n"
        )

    # Copilot Agent Skills.
    for skill in plan.skills:
        files[f".github/skills/{skill.name}/SKILL.md"] = (
            f"{_frontmatter(_skill_frontmatter(skill))}\n\n"
            f"# {skill.name}\n\n"
            f"{skill.instructions.strip()}\n"
        )

        for ref_name, ref_body in skill.references.items():
            rel = ref_name.replace('\\', '/').lstrip('/')
            files[f".github/skills/{skill.name}/{rel}"] = ref_body

    files["README.md"] = _overview_readme(
        plan,
        Platform.GITHUB_COPILOT,
        rules=rules,
    )

    files[WORK_BREAKDOWN_FILENAME] = _work_breakdown_markdown(
        plan,
        Platform.GITHUB_COPILOT,
    )

    files[SDD_FILENAME] = _solution_design_markdown(
        plan,
        Platform.GITHUB_COPILOT,
    )

    files[GITIGNORE_FILENAME] = render_gitignore(plan)

    _attach_source_tree(files, plan)

    return files

EXPORTERS: dict[Platform, Callable[[ProjectPlan], dict[str, str]]] = {
    Platform.CLAUDE_CODE: export_claude_code,
    Platform.CURSOR: export_cursor,
    Platform.WINDSURF: export_windsurf,
    Platform.GITHUB_COPILOT: export_github_copilot,
}


def render_files(
    plan: ProjectPlan, platform: Platform, *, extra_workspace_files: int = 0
) -> dict[str, str]:
    """Render every text file of the workspace.

    `extra_workspace_files` counts paths the caller adds afterwards that are not text — the
    blueprint PNGs. They cannot be members of this dict (it is `str` throughout, see
    `write_directory`), but they are in the tree and in the zip, so the README's workspace
    total has to include them or it contradicts what the user can see.
    """
    files = EXPORTERS[platform](plan)
    # Apply durable user edits last so Save on Export survives reopen/refresh.
    for raw_path, content in (plan.file_overrides or {}).items():
        path = str(raw_path or "").replace("\\", "/").lstrip("/")
        if not path or ".." in path.split("/"):
            continue
        if content is None:
            continue
        files[path] = str(content)

    # Overrides are applied verbatim, which would put a stored WORKBREAKDOWN.md back over
    # the refreshed copy — and a body stored before rules were derived lists the wrong set
    # (an existing Claude project's still says they are not exported at all). Re-run the
    # refresh on the winning content so the correction holds while every hand-edited phase
    # in the override survives.
    if WORK_BREAKDOWN_FILENAME in files:
        files[WORK_BREAKDOWN_FILENAME] = (
            refresh_rules_section(files[WORK_BREAKDOWN_FILENAME], plan, platform).rstrip() + "\n"
        )

    # Now that every path exists, the README can state the real total. Done here rather
    # than in the exporters because overrides may add or replace files above.
    total = str(len(files) + max(0, extra_workspace_files))
    for path, content in files.items():
        if WORKSPACE_COUNT_TOKEN in content:
            files[path] = content.replace(WORKSPACE_COUNT_TOKEN, total)
    return files


def write_directory(
    files: dict[str, str],
    root: Path,
    *,
    binary_files: dict[str, bytes] | None = None,
) -> list[str]:
    """Write the workspace out. `binary_files` carries anything that is not text.

    Separate from `files` rather than a union type: every exporter and every override path
    produces `dict[str, str]`, and widening that would mean auditing all of them. The only
    binary members today are the blueprint PNGs.
    """
    written: list[str] = []
    root.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        written.append(str(path))
    for rel, blob in (binary_files or {}).items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(blob)
        written.append(str(path))
    return written


def build_zip_bytes(
    files: dict[str, str],
    root_folder: str | None = None,
    *,
    binary_files: dict[str, bytes] | None = None,
) -> bytes:
    """Zip export files; optional root_folder wraps entries (e.g. my-project/.cursor/...)."""
    prefix = ""
    if root_folder:
        safe = root_folder.strip().strip("/\\")
        if safe:
            prefix = f"{safe}/"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel, content in files.items():
            zf.writestr(f"{prefix}{rel}", content)
        for rel, blob in (binary_files or {}).items():
            zf.writestr(f"{prefix}{rel}", blob)
    return buf.getvalue()
