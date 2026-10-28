"""
Project-specific rules for every IDE, derived from the plan.

Two behaviours are under test. First, every platform exports rules in its own dialect —
`.claude/rules/*.md` (plain markdown), `.cursor/rules/*.mdc` (frontmatter),
`.windsurf/rules/*.md` (trigger). Second, they are *derived*, which is what makes the fix
reach **existing** projects: a plan with `rules == []` — every Claude project generated
before rules existed, and any IDE where the LLM returned none — gains them on the next
render with no migration, the same mechanism as `.gitignore` and `CLAUDE.md`.
"""

from __future__ import annotations

import pytest

from app.core.state_machine import Platform
from app.models.schemas import ProjectPlan, RuleSpec, SourceFileSpec, AgentSpec, SkillSpec
from app.services.deduction.project_rules import (
    build_claude_rules,
    build_project_rules,
    claude_rule_import_lines,
    claude_rule_path,
    ensure_claude_rules,
    ensure_project_rules,
    rule_path,
    rules_dir,
)
from app.services.deduction.work_breakdown import RULES_SECTION_HEADING
from app.services.export.renderers import render_files

ALL_PLATFORMS = [Platform.CLAUDE_CODE, Platform.CURSOR, Platform.WINDSURF, Platform.GITHUB_COPILOT,]


def _plan(
    *paths: str,
    rules: list[RuleSpec] | None = None,
    name: str = "payments-api",
) -> ProjectPlan:
    return ProjectPlan(
        project_name=name,
        summary="Payments API with a ledger and webhook delivery.",
        agents=[
            AgentSpec(
                name="ledger-reviewer",
                description="Reviews ledger invariants.",
                system_prompt="# Role\nReview.",
                tools=["Read"],
                skills=["double-entry-check"],
            )
        ],
        skills=[
            SkillSpec(
                name="double-entry-check",
                description="Checks entries balance.",
                instructions="# Title\nCheck.",
            )
        ],
        rules=list(rules or []),
        source_tree=[SourceFileSpec(path=p, purpose="Role: x") for p in paths],
        work_breakdown="## Phase 1\n- do the thing\n",
    )


_PY_TREE = (
    "main.py",
    "requirements.txt",
    "backend/api/routes/payments.py",
    "backend/services/payments_service.py",
    "backend/services/ledger_service.py",
    "backend/repositories/payments_repository.py",
    "backend/core/config.py",
    "backend/tests/test_payments.py",
)


def test_claude_export_includes_rules_folder():
    files = render_files(_plan(*_PY_TREE), Platform.CLAUDE_CODE)
    rule_files = [p for p in files if p.startswith(".claude/rules/")]
    assert rule_files, "Claude Code must export a rules folder like agents and skills"
    assert all(p.endswith(".md") for p in rule_files), "Claude rules are `.md`, not `.mdc`"


def test_rules_are_plain_markdown_with_no_frontmatter():
    """
    Claude Code has no `alwaysApply`/`globs` frontmatter dialect. A YAML block would
    render as literal text at the top of the rule, so the scope goes in the body.
    """
    files = render_files(_plan(*_PY_TREE), Platform.CLAUDE_CODE)
    for path, body in files.items():
        if not path.startswith(".claude/rules/"):
            continue
        assert not body.lstrip().startswith("---")
        assert "alwaysApply" not in body
        assert "globs:" not in body
        assert body.lstrip().startswith("#")
        assert "Applies to:" in body


def test_existing_project_with_no_rules_still_gets_them():
    """The screenshot case: a Claude project generated when rules were stripped."""
    plan = _plan(*_PY_TREE)
    assert plan.rules == []
    assert ensure_claude_rules(plan), "an empty plan must be backfilled, not left bare"


def test_authored_rules_survive_and_only_always_on_ones_backfill():
    """
    A user who deleted a scoped rule should not see it return; a plan missing an
    always-on guarantee should get it back.
    """
    mine = RuleSpec(
        name="my-rule", description="Mine.", body="# Mine\nDo it.", always_apply=False, globs=["x/**"]
    )
    resolved = ensure_claude_rules(_plan(*_PY_TREE, rules=[mine]))
    names = [r.name for r in resolved]
    assert "my-rule" in names
    assert "architecture" in names and "secure-coding-vapt" in names  # always-on backfilled
    assert "testing" not in names, "scoped rules are the user's call, not backfilled"


def test_rules_are_project_specific():
    """Generic boilerplate would be the same for every project — these are not."""
    payments = build_claude_rules(_plan(*_PY_TREE, name="payments-api"))
    tutor = build_claude_rules(
        _plan(
            "main.py",
            "backend/services/lesson_service.py",
            "backend/services/quiz_service.py",
            name="smart-tutor-ai",
        )
    )
    arch_payments = next(r for r in payments if r.name == "architecture").body
    arch_tutor = next(r for r in tutor if r.name == "architecture").body

    assert "payments-api" in arch_payments and "`payments`" in arch_payments
    assert "`ledger`" in arch_payments
    assert "smart-tutor-ai" in arch_tutor and "`quiz`" in arch_tutor
    assert "payments" not in arch_tutor
    assert arch_payments != arch_tutor

    # The workflow rule names this project's real agents and skills.
    workflow = next(r for r in payments if r.name == "workflow").body
    assert "ledger-reviewer" in workflow
    assert "double-entry-check" in workflow


def test_frontend_rule_only_when_a_frontend_exists():
    api_only = [r.name for r in build_claude_rules(_plan(*_PY_TREE))]
    assert "frontend" not in api_only

    angular = build_claude_rules(
        _plan(*_PY_TREE, "frontend/src/app/app.component.ts")
    )
    fe = next(r for r in angular if r.name == "frontend")
    assert fe.globs == ["frontend/src/**"]
    assert "async` pipe" in fe.body or "async" in fe.body

    jinja = build_claude_rules(_plan(*_PY_TREE, "frontend/templates/base.html"))
    fe_jinja = next(r for r in jinja if r.name == "frontend")
    assert "frontend/templates/**" in fe_jinja.globs
    assert "safe" in fe_jinja.body


def test_claude_md_imports_always_on_rules_and_lists_scoped_ones():
    """
    `@path` is Claude Code's import syntax. Importing only always-on rules is what keeps
    `CLAUDE.md` cheap — it is re-read on every prompt.
    """
    rules = build_claude_rules(_plan(*_PY_TREE))
    always, scoped = claude_rule_import_lines(rules)
    assert always and scoped
    assert all(line.startswith("@.claude/rules/") for line in always)
    assert f"@{claude_rule_path('architecture')}" in always
    assert any("testing" in line and "backend/tests/**" in line for line in scoped)

    claude_md = render_files(_plan(*_PY_TREE), Platform.CLAUDE_CODE)["CLAUDE.md"]
    assert "## Rules — always apply" in claude_md
    assert f"@{claude_rule_path('secure-coding-vapt')}" in claude_md
    assert "## Rules — read when relevant" in claude_md
    # Scoped rules are referenced, never imported — that is the whole point.
    assert f"@{claude_rule_path('testing')}" not in claude_md


def test_claude_md_stays_short_even_with_rules():
    """Loaded at the start of every session, so its cost is paid on every prompt."""
    claude_md = render_files(_plan(*_PY_TREE), Platform.CLAUDE_CODE)["CLAUDE.md"]
    assert len(claude_md.splitlines()) < 60


def test_claude_md_does_not_duplicate_rule_content():
    """One copy of the conventions: in the rules, not restated in the memory file."""
    claude_md = render_files(_plan(*_PY_TREE), Platform.CLAUDE_CODE)["CLAUDE.md"]
    assert "## Conventions" not in claude_md


def test_readme_documents_the_rules_for_claude():
    readme = render_files(_plan(*_PY_TREE), Platform.CLAUDE_CODE)["README.md"]
    assert "## Rules and purpose" in readme
    assert ".claude/rules" in readme
    assert "- `architecture` —" in readme
    assert "No rules generated for this plan." not in readme
    # Artifact counts must not report 0 rules beside a populated folder.
    assert "Rules: 0" not in readme


def test_work_breakdown_documents_the_rules_for_claude():
    wb = render_files(_plan(*_PY_TREE), Platform.CLAUDE_CODE)["WORKBREAKDOWN.md"]
    assert "## Rules in this workspace" in wb
    assert "`architecture`" in wb
    assert "`secure-coding-vapt`" in wb
    assert "(none generated for this plan)" not in wb


def test_stored_work_breakdown_has_its_stale_rules_claim_replaced():
    """
    The real existing-project case: WORKBREAKDOWN.md was generated when Claude got no
    rules, and its stored body still says so. Only that section is rewritten — the
    hand-written phases around it must survive.
    """
    stale = "\n".join(
        [
            "# payments-api — Work breakdown",
            "",
            "## Phase 1 — Foundations",
            "- My own edited note about the ledger.",
            "",
            "## Rules in this workspace",
            "",
            "- **Claude Code** exports agents and skills under `.claude/` — "
            "security and style guidance is embedded in agent Goals and skill instructions.",
            "",
            "Platform: **Claude Code**.",
            "",
            "## Phase 2 — Delivery",
            "- Webhook retries.",
            "",
        ]
    )
    plan = _plan(*_PY_TREE).model_copy(update={"work_breakdown": stale})
    wb = render_files(plan, Platform.CLAUDE_CODE)["WORKBREAKDOWN.md"]

    assert "security and style guidance is embedded" not in wb
    assert "`architecture`" in wb and "`secure-coding-vapt`" in wb
    assert ".claude/rules/" in wb
    # Surrounding phases, including the user's own edits, are untouched.
    assert "## Phase 1 — Foundations" in wb
    assert "My own edited note about the ledger." in wb
    assert "## Phase 2 — Delivery" in wb
    assert "- Webhook retries." in wb
    assert wb.count(RULES_SECTION_HEADING) == 1


def test_refresh_appends_the_section_when_a_stored_body_lacks_it():
    plan = _plan(*_PY_TREE).model_copy(
        update={"work_breakdown": "# WB\n\n## Phase 1\n- only phases here\n"}
    )
    wb = render_files(plan, Platform.CLAUDE_CODE)["WORKBREAKDOWN.md"]
    assert RULES_SECTION_HEADING in wb
    assert "- only phases here" in wb


def test_rules_do_not_change_the_source_file_count():
    """
    Rules are IDE artifacts, so they belong to the workspace count only. Counting them
    as scaffold would make the Files badge disagree with the README.
    """
    from app.services.export.renderers import scaffold_file_count

    plan = _plan(*_PY_TREE)
    files = render_files(plan, Platform.CLAUDE_CODE)
    assert scaffold_file_count(plan) == len(_PY_TREE)
    assert all(not p.startswith(".claude/") for p in _PY_TREE)
    assert len(files) > scaffold_file_count(plan)


def test_other_platforms_keep_their_own_rule_dialect():
    """Adding Claude rules must not move Cursor's `.mdc` or Windsurf's `.md`."""
    rules = [
        RuleSpec(
            name="core-standards",
            description="Standards.",
            body="# Core\nBe careful.",
            always_apply=True,
            globs=[],
        )
    ]
    cursor = render_files(_plan(*_PY_TREE, rules=rules), Platform.CURSOR)
    assert any(p.startswith(".cursor/rules/") and p.endswith(".mdc") for p in cursor)
    assert not any(p.startswith(".claude/") for p in cursor)
    assert "CLAUDE.md" not in cursor

    windsurf = render_files(_plan(*_PY_TREE, rules=rules), Platform.WINDSURF)
    assert any(p.startswith(".windsurf/rules/") for p in windsurf)
    assert "CLAUDE.md" not in windsurf


# --- Every IDE, not just Claude -------------------------------------------------------


@pytest.mark.parametrize("platform", ALL_PLATFORMS)
def test_every_ide_exports_project_specific_rules(platform: Platform):
    """
    A plan the LLM returned no rules for used to export an empty rules folder on Cursor
    and Windsurf. Every IDE now gets the derived floor.
    """
    plan = _plan(*_PY_TREE)
    assert plan.rules == []
    files = render_files(plan, platform)

    folder = rules_dir(platform)
    exported = sorted(p for p in files if p.startswith(folder))
    assert exported, f"{platform.value} must export a rules folder"
    for name in ("architecture", "secure-coding-vapt", "workflow"):
        assert rule_path(name, platform) in files, f"{name} missing for {platform.value}"

    # Project-specific, not boilerplate: this project's own slices and artifacts.
    arch = files[rule_path("architecture", platform)]
    assert "payments-api" in arch and "`ledger`" in arch
    workflow = files[rule_path("workflow", platform)]
    assert "ledger-reviewer" in workflow and "double-entry-check" in workflow


@pytest.mark.parametrize("platform", ALL_PLATFORMS)
def test_rule_count_matches_the_exported_folder(platform: Platform):
    """
    What the sessions list and Review tab count must equal what lands on disk — the
    round-14 bug was these two disagreeing.
    """
    plan = _plan(*_PY_TREE)
    resolved = ensure_project_rules(plan, platform)
    files = render_files(plan, platform)
    on_disk = [p for p in files if p.startswith(rules_dir(platform))]
    assert len(on_disk) == len(resolved)
    assert sorted(on_disk) == sorted(rule_path(r.name, platform) for r in resolved)


def test_cursor_rules_carry_apply_mode_in_frontmatter():
    """Cursor reads `alwaysApply`/`globs` from `.mdc` frontmatter, not from the body."""
    files = render_files(_plan(*_PY_TREE), Platform.CURSOR)
    always = files[rule_path("architecture", Platform.CURSOR)]
    assert always.startswith("---")
    assert "alwaysApply: true" in always

    scoped = files[rule_path("testing", Platform.CURSOR)]
    assert "globs:" in scoped
    assert "backend/tests/**/*.py" in scoped
    # No hand-written scope line: the frontmatter is the single source of truth here.
    assert "Applies to:" not in scoped


def test_windsurf_rules_carry_apply_mode_in_the_trigger_field():
    files = render_files(_plan(*_PY_TREE), Platform.WINDSURF)
    always = files[rule_path("architecture", Platform.WINDSURF)]
    assert "trigger: always_on" in always

    scoped = files[rule_path("testing", Platform.WINDSURF)]
    assert "trigger: glob" in scoped
    assert "backend/tests/**/*.py" in scoped
    assert "Applies to:" not in scoped


def test_claude_rules_state_the_scope_in_the_body_instead():
    """
    The one platform with no frontmatter dialect: the renderer writes the scope into the
    body, because that is the only place Claude will read it.
    """
    files = render_files(_plan(*_PY_TREE), Platform.CLAUDE_CODE)
    always = files[rule_path("architecture", Platform.CLAUDE_CODE)]
    assert not always.lstrip().startswith("---")
    assert "**Applies to: always.**" in always

    scoped = files[rule_path("testing", Platform.CLAUDE_CODE)]
    assert "Applies to: `backend/tests/**/*.py`" in scoped


@pytest.mark.parametrize("platform", ALL_PLATFORMS)
def test_workflow_rule_names_this_ides_own_folders(platform: Platform):
    """A Cursor rule telling the user to look in `.claude/agents/` would be wrong."""
    body = next(r for r in build_project_rules(_plan(*_PY_TREE), platform) if r.name == "workflow").body
    root = {
        Platform.CLAUDE_CODE: ".claude",
        Platform.CURSOR: ".cursor",
        Platform.WINDSURF: ".windsurf",
    }[platform]
    assert f"{root}/agents/" in body and f"{root}/skills/" in body
    for other in (".claude", ".cursor", ".windsurf"):
        if other != root:
            assert f"{other}/agents/" not in body


@pytest.mark.parametrize("platform", ALL_PLATFORMS)
def test_every_ide_documents_its_rules_in_readme_and_work_breakdown(platform: Platform):
    files = render_files(_plan(*_PY_TREE), platform)
    # Windsurf's root overview is AGENTS.md, but README.md carries the same content.
    readme = files["README.md"]
    assert "## Rules and purpose" in readme
    assert rules_dir(platform) in readme
    assert "- `architecture` —" in readme
    assert "Rules: 0" not in readme
    assert "No rules generated for this plan." not in readme

    wb = files["WORKBREAKDOWN.md"]
    assert RULES_SECTION_HEADING in wb
    assert "`architecture`" in wb and "`secure-coding-vapt`" in wb
    assert "(none generated for this plan)" not in wb


@pytest.mark.parametrize("platform", ALL_PLATFORMS)
def test_authored_rules_are_kept_on_every_ide(platform: Platform):
    mine = RuleSpec(
        name="core-standards",
        description="Mine.",
        body="# Core\nBe careful.",
        always_apply=True,
        globs=[],
    )
    files = render_files(_plan(*_PY_TREE, rules=[mine]), platform)
    assert rule_path("core-standards", platform) in files
    assert "Be careful." in files[rule_path("core-standards", platform)]
    # …and the always-on floor is still added alongside it.
    assert rule_path("architecture", platform) in files
    assert rule_path("secure-coding-vapt", platform) in files


def test_render_is_stable_across_calls():
    plan = _plan(*_PY_TREE)
    first = render_files(plan, Platform.CLAUDE_CODE)
    second = render_files(plan, Platform.CLAUDE_CODE)
    assert first == second
