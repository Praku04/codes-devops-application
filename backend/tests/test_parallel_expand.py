"""Confirm parallel gather for agents/skills/rules + fast source tree."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app.core.state_machine import Platform
from app.models.schemas import ProjectBrief
from app.services.deduction.engine import deduce_plan


@pytest.mark.asyncio
async def test_deduce_plan_expands_agents_skills_rules_in_parallel():
    in_flight = 0
    max_in_flight = 0
    lock = asyncio.Lock()
    kinds: list[str] = []

    async def fake_complete_json(messages, **kwargs):
        nonlocal in_flight, max_in_flight
        system = messages[0]["content"]
        user = messages[-1]["content"]

        # Blueprint call (single, sequential)
        if "compact blueprint" in user.lower() or "blueprint JSON" in user:
            return {
                "project_name": "Smart Tutor AI",
                "summary": "Tutoring workspace.",
                "agents": [
                    {"name": "lesson-planner", "description": "Plans lessons", "tools": ["Read"], "skills": []},
                    {"name": "security-vapt-reviewer", "description": "Sec", "tools": ["Read"], "skills": ["pre-vapt-secure-coding"]},
                ],
                "skills": [
                    {"name": "quiz-builder", "description": "Builds quizzes"},
                    {"name": "pre-vapt-secure-coding", "description": "Secure coding"},
                ],
                "rules": [
                    {"name": "secure-coding-vapt", "description": "VAPT", "always_apply": True, "globs": []},
                ],
                "mcp_servers": [],
                "source_tree": [{"path": "main.py", "purpose": "Entrypoint"}],
            }

        async with lock:
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            if "ONE IDE agent" in system or "ONE Cursor" in system or "system_prompt" in system:
                kinds.append("agent")
            elif "Agent Skill" in system:
                kinds.append("skill")
            elif "project rule" in system:
                kinds.append("rule")
        await asyncio.sleep(0.05)
        async with lock:
            in_flight -= 1

        if "agent" in system.lower() and "system_prompt" in system:
            return {
                "name": "lesson-planner",
                "description": "Plans lessons",
                "system_prompt": "# Role\nTutor agent.\n## Goals\n- Plan\n## Workflow\n- Step\n## Example\n- Ex\n## Acceptance\n- Done",
                "tools": ["Read"],
                "skills": [],
            }
        if "Skill" in system:
            return {
                "name": "quiz-builder",
                "description": "Builds quizzes",
                "instructions": "# Title\nQuiz\n## Steps\n- A\n## Checks\n- B\n## Example\n- C",
            }
        return {
            "name": "secure-coding-vapt",
            "description": "VAPT",
            "body": "# Rule\nSecure coding.",
            "always_apply": True,
            "globs": [],
        }

    brief = ProjectBrief(
        problem_statement="Smart Tutor AI for personalized lessons and quizzes with FastAPI and Angular.",
        document_text="### brief.md\nBuild Smart Tutor AI with lesson planning and quiz generation.",
    )

    with patch(
        "app.services.deduction.engine.llm_client.complete_json",
        new=AsyncMock(side_effect=fake_complete_json),
    ):
        plan = await deduce_plan(brief, Platform.CURSOR)

    assert plan.project_name
    assert len(plan.agents) >= 2
    assert len(plan.skills) >= 2
    assert len(plan.rules) >= 1
    assert any(f.path == "main.py" for f in plan.source_tree)
    assert any(f.path.startswith("backend/") for f in plan.source_tree)
    # Agents/skills/rules overlapped in flight
    assert max_in_flight >= 2
    assert "agent" in kinds and "skill" in kinds and "rule" in kinds


@pytest.mark.asyncio
async def test_claude_expands_rules_in_parallel_expand():
    async def fake_complete_json(messages, **kwargs):
        user = messages[-1]["content"]
        if "blueprint JSON" in user or "compact blueprint" in user.lower():
            return {
                "project_name": "Demo",
                "summary": "Demo",
                "agents": [{"name": "security-vapt-reviewer", "description": "Sec", "tools": ["Read"], "skills": []}],
                "skills": [{"name": "pre-vapt-secure-coding", "description": "Sec"}],
                "rules": [{"name": "secure-coding-vapt", "description": "R", "always_apply": True, "globs": []}],
                "mcp_servers": [],
                "source_tree": [{"path": "main.py", "purpose": "Entry"}],
            }
        system = messages[0]["content"]
        if "project rule" in system:
            # Claude Code rules are plain markdown; the prompt must not offer a
            # frontmatter dialect it does not have.
            assert ".claude/rules/*.md" in system
            assert "NO frontmatter" in system
            return {
                "name": "secure-coding-vapt",
                "description": "VAPT",
                "body": "# Rule\n\n**Applies to: always.**\n\nSecure coding.",
                "always_apply": True,
                "globs": [],
            }
        if "system_prompt" in system:
            return {
                "name": "security-vapt-reviewer",
                "description": "Sec",
                "system_prompt": "# Role\nSec\n## Goals\n- A\n## Workflow\n- B\n## Example\n- C\n## Acceptance\n- D",
                "tools": ["Read"],
                "skills": [],
            }
        return {
            "name": "pre-vapt-secure-coding",
            "description": "Sec",
            "instructions": "# Title\nS\n## Steps\n- A\n## Checks\n- B\n## Example\n- C",
        }

    brief = ProjectBrief(problem_statement="Demo FastAPI API for tutoring.")
    with patch(
        "app.services.deduction.engine.llm_client.complete_json",
        new=AsyncMock(side_effect=fake_complete_json),
    ):
        plan = await deduce_plan(brief, Platform.CLAUDE_CODE)

    assert [r.name for r in plan.rules] == ["secure-coding-vapt"]
    assert plan.source_tree
