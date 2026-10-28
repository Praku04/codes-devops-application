"""Tests for WORKBREAKDOWN.md completeness and fallback."""

from app.services.deduction.work_breakdown import (
    _phase_block_complete,
    fallback_work_breakdown,
    is_work_breakdown_complete,
)
from app.core.state_machine import Platform
from app.models.schemas import ProjectBrief, ProjectPlan


def test_fallback_work_breakdown_is_complete():
    brief = ProjectBrief(
        problem_statement="Enterprise learning platform with AI tutoring and video analysis."
    )
    plan = ProjectPlan(
        project_name="LearnHub",
        summary="SOP-to-course platform with batches, assessments, and AI tutor",
        agents=[],
        skills=[],
    )
    from app.services.deduction.work_breakdown import _phase_blocks

    md = fallback_work_breakdown(brief, plan, Platform.CURSOR)
    assert "### Testing strategy" in md
    assert "### Phase completion checklist" in md
    assert len(_phase_blocks(md)) >= 5
    assert is_work_breakdown_complete(md)


def test_render_concise_wbs_is_complete():
    from app.services.deduction.work_breakdown import _parse_concise_json, _render_concise_wbs, _phase_blocks

    data = _parse_concise_json(
        {
            "product_scope": "Learning platform with AI tutor and batches.",
            "document_requirements": ["Trainer uploads SOPs", "Trainee study paths", "AI chat"],
            "tech_stack": "FastAPI, PostgreSQL, React, Bedrock.",
            "assumptions": ["RBAC enforced", "VAPT before release"],
            "phases": [
                {
                    "number": i,
                    "title": f"Phase topic {i}",
                    "objectives": ["Obj A", "Obj B"],
                    "tasks": ["Task 1", "Task 2", "Task 3", "Task 4"],
                    "testing_strategy": ["Unit tests", "API tests"],
                    "acceptance": ["pytest passes", "403 on wrong role", "UI flow works"],
                    "agent_prompt": f"Implement phase {i} from WORKBREAKDOWN.md.",
                }
                for i in range(1, 6)
            ],
        }
    )
    plan = ProjectPlan(project_name="LearnHub", summary="AI learning", agents=[], skills=[])
    md = _render_concise_wbs(data, plan, Platform.CURSOR)
    assert len(_phase_blocks(md)) == 5
    assert is_work_breakdown_complete(md)


def test_truncated_markdown_not_complete():
    partial = "x" * 9000 + "\n## Phase 1: X\n###\n"
    assert not is_work_breakdown_complete(partial)


def test_dangling_heading_not_complete():
    assert not _phase_block_complete("## Phase 2: Two\n\n###")
