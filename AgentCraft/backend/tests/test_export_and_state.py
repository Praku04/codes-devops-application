"""Exporter and state machine unit tests (no Bedrock)."""

from pathlib import Path

import pytest

from app.core.state_machine import IllegalTransitionError, ProjectState, assert_can
from app.models.schemas import ProjectBrief
from app.core.state_machine import Platform
from app.services.deduction.engine import demo_plan
from app.services.export.renderers import render_files, build_zip_bytes, write_directory


def test_state_happy_path():
    assert assert_can("choose_path_docs", ProjectState.CREATED) == ProjectState.AWAITING_DOCS
    assert assert_can("submit_documents", ProjectState.AWAITING_DOCS) == ProjectState.CONTEXT_READY
    assert assert_can("set_platform", ProjectState.CONTEXT_READY) == ProjectState.PLATFORM_SELECTED
    assert assert_can("start_generate", ProjectState.PLATFORM_SELECTED) == ProjectState.GENERATING
    assert assert_can("generate_ok", ProjectState.GENERATING) == ProjectState.READY_FOR_REVIEW
    assert assert_can("export", ProjectState.READY_FOR_REVIEW) == ProjectState.EXPORTED


def test_illegal_transition():
    with pytest.raises(IllegalTransitionError):
        assert_can("start_generate", ProjectState.CREATED)


def test_rollback_transitions():
    assert assert_can("rollback_to_created", ProjectState.AWAITING_DOCS) == ProjectState.CREATED
    assert assert_can("rollback_to_created", ProjectState.AWAITING_INTERVIEW) == ProjectState.CREATED
    assert assert_can("rollback_to_docs", ProjectState.CONTEXT_READY) == ProjectState.AWAITING_DOCS
    assert assert_can("rollback_to_interview", ProjectState.CONTEXT_READY) == ProjectState.AWAITING_INTERVIEW
    assert assert_can("rollback_to_context", ProjectState.PLATFORM_SELECTED) == ProjectState.CONTEXT_READY
    with pytest.raises(IllegalTransitionError):
        assert_can("rollback_to_created", ProjectState.GENERATING)
    with pytest.raises(IllegalTransitionError):
        assert_can("rollback_to_context", ProjectState.READY_FOR_REVIEW)
    with pytest.raises(IllegalTransitionError):
        assert_can("set_platform", ProjectState.READY_FOR_REVIEW)


@pytest.mark.parametrize("platform", list(Platform))
def test_exporters_produce_expected_roots(platform: Platform, tmp_path: Path):
    plan = demo_plan(
        ProjectBrief(
            problem_statement="Build a payments API with ledger and webhooks",
            integrations=["github"],
        ),
        platform,
    )
    assert any(a.name == "security-vapt-reviewer" for a in plan.agents)
    assert any(r.name == "secure-coding-vapt" for r in plan.rules)
    for agent in plan.agents:
        n = len(agent.system_prompt.splitlines())
        assert n < 50
        assert n <= 49
    for skill in plan.skills:
        n = len(skill.instructions.splitlines())
        assert n < 50
        assert n <= 49
        assert skill.disable_model_invocation is None  # auto-invoke; never emit false
    for rule in plan.rules:
        assert len(rule.body.splitlines()) < 50

    files = render_files(plan, platform)
    assert files

    readme = files.get("README.md", "")
    assert "## Agents and purpose" in readme
    assert "## Skills and purpose" in readme
    assert "## Rules and purpose" in readme
    assert "## Project structure" in readme
    assert "## File purposes" in readme
    assert "## How to use this workspace" in readme
    assert "main.py" in readme
    assert "README.agentcraft.md" not in files

    assert plan.source_tree, "demo plan must include modular source_tree"
    assert any(s.path == "main.py" for s in plan.source_tree)
    assert any(s.path.startswith("backend/") for s in plan.source_tree)
    assert any(s.path.endswith("__init__.py") for s in plan.source_tree)
    assert "main.py" in files
    assert any(p.startswith("backend/") and p.endswith("__init__.py") for p in files)
    # Payments brief → payments modules under backend/
    assert any("payment" in s.path for s in plan.source_tree)
    # Compact README: bullet purposes, no ### gaps
    assert "- `main.py` —" in readme or "- `main.py`" in readme
    assert "### `" not in readme

    # Docs-aligned frontmatter: never emit disable-model-invocation: false
    skill_bodies = [c for p, c in files.items() if p.endswith("SKILL.md")]
    assert skill_bodies
    for body in skill_bodies:
        assert "disable-model-invocation: false" not in body
        assert "disable_model_invocation: false" not in body
        assert body.lstrip().startswith("---")
        assert "name:" in body
        assert "description:" in body

    if platform == Platform.CURSOR:
        assert any(p.startswith(".cursor/rules/") and p.endswith(".mdc") for p in files)
        assert any(p.startswith(".cursor/skills/") for p in files)
        # alwaysApply:false omitted; true only on always-on rules
        for path, content in files.items():
            if path.endswith(".mdc"):
                assert "alwaysApply: false" not in content
                if "secure-coding-vapt" in path or "core-standards" in path:
                    assert "alwaysApply: true" in content
                if "backend-files" in path:
                    assert "alwaysApply:" not in content
                    assert "globs:" in content
                    assert "backend/" in content
    elif platform == Platform.CLAUDE_CODE:
        assert any(p.startswith(".claude/agents/") for p in files)
        assert any("/SKILL.md" in p for p in files)
        # Rules ship as plain markdown — Claude Code has no `.mdc` frontmatter dialect,
        # so an `alwaysApply:` block would just render as text at the top of the rule.
        rule_paths = [p for p in files if p.startswith(".claude/rules/")]
        assert rule_paths
        assert all(p.endswith(".md") for p in rule_paths)
        for path in rule_paths:
            body = files[path]
            assert not body.lstrip().startswith("---")
            assert "alwaysApply" not in body
            assert "Applies to:" in body
        # Always-on rules reach context via CLAUDE.md `@` imports.
        assert "@.claude/rules/" in files["CLAUDE.md"]
    elif platform == Platform.GITHUB_COPILOT:
        assert ".github/copilot-instructions.md" in files
        assert any(
            p.startswith(".github/instructions/") and p.endswith(".instructions.md")
            for p in files
        )
        assert any(
            p.startswith(".github/skills/") and p.endswith("/SKILL.md")
            for p in files
        )
        assert any(
            p.startswith(".github/agents/") and p.endswith(".agent.md")
            for p in files
        )
    elif platform == Platform.WINDSURF:
        assert "AGENTS.md" in files
        assert "README.md" in files
        assert any(p.startswith(".windsurf/rules/") for p in files)
        assert any(p.startswith(".windsurf/skills/") and p.endswith("/SKILL.md") for p in files)
        assert any(p.startswith(".windsurf/agents/") and p.endswith("/AGENT.md") for p in files)
        # Official Cascade path only — do not duplicate skills under .agents/skills
        assert not any(p.startswith(".agents/") for p in files)
        assert "## Agents and purpose" in files["AGENTS.md"]
        for path, content in files.items():
            if path.startswith(".windsurf/rules/"):
                assert "trigger:" in content

    assert any("scripts/" in p for p in files), "security skills should export scripts/*.py"

    z = build_zip_bytes(files, root_folder="demo-project")
    assert len(z) > 50
    import io
    import zipfile

    with zipfile.ZipFile(io.BytesIO(z)) as zf:
        names = zf.namelist()
        assert any(n.startswith("demo-project/") for n in names)
    written = write_directory(files, tmp_path / "out")
    assert written


def test_export_includes_work_breakdown():
    plan = demo_plan(
        ProjectBrief(problem_statement="Build a payments API with ledger"),
        Platform.CURSOR,
    )
    files = render_files(plan, Platform.CURSOR)
    assert "WORKBREAKDOWN.md" in files
    assert "## Phase" in files["WORKBREAKDOWN.md"]
    assert "WORKBREAKDOWN.md" in files["README.md"]


def test_file_overrides_persist_on_render():
    plan = demo_plan(
        ProjectBrief(problem_statement="Build a payments API with ledger"),
        Platform.CURSOR,
    )
    plan = plan.model_copy(
        update={
            "file_overrides": {
                "README.md": "# Custom README\n\nUser edit that must stick.\n",
            }
        }
    )
    files = render_files(plan, Platform.CURSOR)
    assert files["README.md"].startswith("# Custom README")
    assert "User edit that must stick" in files["README.md"]


    