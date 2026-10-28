"""
Generated root files: `.gitignore` everywhere, `CLAUDE.md` on Claude Code only.

Both are rendered from the plan rather than stored in `source_tree`, which is what makes
them appear for existing projects with no migration. These tests pin that, the
stack-conditional ignore blocks, and that neither file inflates the "Source files" count.
"""

import pytest

from app.core.state_machine import Platform
from app.models.schemas import ProjectBrief, ProjectPlan, SourceFileSpec
from app.services.deduction.demo_fallback import build_demo_plan
from app.services.export.renderers import render_files, scaffold_file_count
from app.services.export.root_files import (
    CLAUDE_MEMORY_FILENAME,
    GITIGNORE_FILENAME,
    detect_stack_tags,
    render_claude_md,
    render_gitignore,
)

PLATFORMS = [Platform.CURSOR, Platform.CLAUDE_CODE, Platform.WINDSURF, Platform.GITHUB_COPILOT,]


def _plan(platform: Platform, statement: str = "payments API with ledger") -> ProjectPlan:
    return build_demo_plan(ProjectBrief(problem_statement=statement), platform)


def _tree(*paths: str) -> ProjectPlan:
    return ProjectPlan(
        project_name="demo",
        summary="demo",
        source_tree=[SourceFileSpec(path=p, purpose="x") for p in paths],
    )


@pytest.mark.parametrize("platform", PLATFORMS)
def test_gitignore_exported_for_every_platform(platform: Platform):
    files = render_files(_plan(platform), platform)
    assert GITIGNORE_FILENAME in files
    assert files[GITIGNORE_FILENAME].strip()


@pytest.mark.parametrize("platform", PLATFORMS)
def test_claude_md_only_for_claude_code(platform: Platform):
    """Cursor reads .cursor/rules and Windsurf reads AGENTS.md — a CLAUDE.md there is dead weight."""
    files = render_files(_plan(platform), platform)
    assert (CLAUDE_MEMORY_FILENAME in files) is (platform == Platform.CLAUDE_CODE)


def test_claude_md_is_at_the_repo_root_not_inside_dot_claude():
    """Claude Code only auto-loads project memory from the root, beside main.py."""
    files = render_files(_plan(Platform.CLAUDE_CODE), Platform.CLAUDE_CODE)
    assert CLAUDE_MEMORY_FILENAME in files
    assert "/" not in CLAUDE_MEMORY_FILENAME
    assert f".claude/{CLAUDE_MEMORY_FILENAME}" not in files


def test_claude_md_stays_short_and_delegates():
    """
    It is loaded into context on every prompt, so length is a real cost.

    A memory file that restates the README gets skimmed; this one has to point at it.
    """
    body = render_claude_md(_plan(Platform.CLAUDE_CODE))
    assert len(body.splitlines()) < 60
    assert "README.md" in body
    assert "WORKBREAKDOWN.md" in body


def test_claude_md_names_the_plans_actual_agents_and_skills():
    plan = _plan(Platform.CLAUDE_CODE)
    body = render_claude_md(plan)
    assert plan.agents and plan.skills
    assert f"`{plan.agents[0].name}`" in body
    assert f"`{plan.skills[0].name}`" in body


def test_claude_md_omits_layout_lines_for_absent_directories():
    """A bullet for a frontend that was never scaffolded sends Claude hunting for nothing."""
    body = render_claude_md(_tree("main.py", "backend/services/x.py", "requirements.txt"))
    assert "backend/services/" in body
    assert "frontend/" not in body


def test_gitignore_always_protects_env_but_keeps_the_example():
    body = render_gitignore(_plan(Platform.CURSOR))
    lines = [ln.strip() for ln in body.splitlines()]
    assert ".env" in lines
    assert "!.env.example" in lines


@pytest.mark.parametrize("platform", PLATFORMS)
def test_gitignore_does_not_ignore_the_exported_ai_workspace(platform: Platform):
    """
    The agents/skills/rules are the point of the export — ignoring them would undo it.

    Only machine-local IDE state (`settings.local.json`) may be ignored.
    """
    lines = [ln.strip() for ln in render_gitignore(_plan(platform)).splitlines()]
    for entry in (".claude/", ".cursor/", ".windsurf/", "AGENTS.md", "CLAUDE.md"):
        assert entry not in lines
    assert ".claude/settings.local.json" in lines


def test_gitignore_omits_node_rules_for_a_python_only_project():
    """A backend-only API should not ship node_modules noise."""
    body = render_gitignore(_tree("main.py", "backend/api/app.py", "requirements.txt"))
    assert "__pycache__/" in body
    assert "node_modules/" not in body


def test_gitignore_adds_node_rules_when_a_frontend_exists():
    body = render_gitignore(
        _tree("main.py", "frontend/app/layout.tsx", "frontend/package.json", "requirements.txt")
    )
    assert "node_modules/" in body
    assert ".next/" in body


def test_gitignore_adds_sqlite_rules_only_when_sqlite_is_used():
    with_db = render_gitignore(_tree("main.py", "backend/core/sqlite_store.py"))
    without = render_gitignore(_tree("main.py", "backend/core/database.py"))
    assert "*.sqlite3" in with_db
    assert "*.sqlite3" not in without


def test_angular_is_not_mislabelled_as_next():
    """Both put `.ts` under `frontend/`, but they need different build-output rules."""
    body = render_gitignore(
        _tree("main.py", "frontend/src/app/app.component.ts", "frontend/package.json")
    )
    assert ".angular/" in body
    assert ".next/" not in body


def test_stack_tags_come_from_paths_so_old_plans_work():
    """Detection never reads the brief — an existing plan has scaffold paths regardless."""
    tags = detect_stack_tags(_tree("main.py", "frontend/app/page.tsx", "requirements.txt"))
    assert {"python", "node", "next"} <= tags


@pytest.mark.parametrize("platform", PLATFORMS)
def test_generated_root_files_do_not_inflate_the_source_count(platform: Platform):
    """
    They are workspace files, not scaffold — the Files badge must not move.

    `_is_ide_or_readme` covers them, so a plan that somehow lists them in `source_tree`
    still reports the same figure.
    """
    plan = _plan(platform)
    before = scaffold_file_count(plan)
    polluted = plan.model_copy(
        update={
            "source_tree": [
                *(plan.source_tree or []),
                SourceFileSpec(path=GITIGNORE_FILENAME, purpose="leaked"),
                SourceFileSpec(path=CLAUDE_MEMORY_FILENAME, purpose="leaked"),
            ]
        }
    )
    assert scaffold_file_count(polluted) == before


@pytest.mark.parametrize("platform", PLATFORMS)
def test_readme_documents_the_generated_root_files(platform: Platform):
    files = render_files(_plan(platform), platform)
    readme = files["README.md"]
    assert f"`{GITIGNORE_FILENAME}`" in readme
    assert (f"`{CLAUDE_MEMORY_FILENAME}`" in readme) is (platform == Platform.CLAUDE_CODE)


@pytest.mark.parametrize("platform", PLATFORMS)
def test_user_edits_to_generated_root_files_survive(platform: Platform):
    """Saving CLAUDE.md / .gitignore in the UI writes a file_override, applied last."""
    plan = _plan(platform).model_copy(
        update={"file_overrides": {GITIGNORE_FILENAME: "# mine only\n*.tmp\n"}}
    )
    files = render_files(plan, platform)
    assert files[GITIGNORE_FILENAME] == "# mine only\n*.tmp\n"


def test_render_is_stable_across_calls():
    """Refresh must not change the workspace count or the file bodies."""
    plan = _plan(Platform.CLAUDE_CODE)
    first = render_files(plan, Platform.CLAUDE_CODE)
    second = render_files(plan, Platform.CLAUDE_CODE)
    assert sorted(first) == sorted(second)
    assert first[CLAUDE_MEMORY_FILENAME] == second[CLAUDE_MEMORY_FILENAME]
    assert first[GITIGNORE_FILENAME] == second[GITIGNORE_FILENAME]
