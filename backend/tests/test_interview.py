"""Interview path: goal-only required, tailored skippable follow-ups, expand shortcut."""

import pytest

from app.models.schemas import InterviewQuestion
from app.services.interview.engine import (
    SKIP_ANSWER,
    apply_answers,
    brief_from_interview,
    fallback_project_questions,
    fresh_interview,
    generate_project_questions,
    is_complete,
    is_skipped,
    is_structured_expanded_brief,
    needs_project_questions,
    next_interview_question,
    prefill_from_expanded_goal,
    with_project_questions,
)

EXPANDED_GOAL = """## Product

Smart Tutor AI for trainers and trainees.

## Problem

Manual SOP training is slow.

## Goals

- Convert SOPs to courses
- AI tutor for trainees

## Stack

- FastAPI backend
- React frontend
- PostgreSQL
- AWS Bedrock

## Domains

API, frontend, AI, security, QA

## Constraints

VAPT before release; RBAC for trainer vs trainee

## Integrations

GitHub, S3, Bedrock
"""

PLAIN_GOAL = "Build a smart tutor that turns corporate SOP documents into guided courses."


def _tailored() -> list[InterviewQuestion]:
    return [
        InterviewQuestion(id="grading_mode", prompt="Grade free text or MCQ only?", required=False),
        InterviewQuestion(id="sop_ingest", prompt="Upload SOP PDFs or sync SharePoint?", required=False),
    ]


def test_structured_expanded_brief_detected():
    assert is_structured_expanded_brief(EXPANDED_GOAL)
    assert not is_structured_expanded_brief("Just a short idea")


def test_fresh_interview_only_asks_the_goal():
    qs = fresh_interview()
    assert [q.id for q in qs] == ["goal"]
    assert qs[0].required


def test_plain_goal_requests_project_questions():
    qs = apply_answers(fresh_interview(), {"goal": PLAIN_GOAL})
    assert needs_project_questions(qs)
    # is_complete stays True for a goal-only list on purpose: answer_interview
    # tailors questions *before* checking completeness, so a goal can never be
    # stranded if question generation returns nothing.
    assert is_complete(qs)


def test_expanded_goal_completes_without_followups():
    qs = apply_answers(fresh_interview(), {"goal": EXPANDED_GOAL})
    assert not needs_project_questions(qs)
    assert is_complete(qs)


def test_tailored_questions_are_optional_and_asked_after_goal():
    qs = with_project_questions(apply_answers(fresh_interview(), {"goal": PLAIN_GOAL}), _tailored())
    assert [q.id for q in qs] == ["goal", "grading_mode", "sop_ingest"]
    assert all(not q.required for q in qs[1:])
    assert next_interview_question(qs).id == "grading_mode"


def test_skipping_every_followup_completes_the_interview():
    qs = with_project_questions(apply_answers(fresh_interview(), {"goal": PLAIN_GOAL}), _tailored())
    assert not is_complete(qs)
    qs = apply_answers(qs, {"grading_mode": SKIP_ANSWER})
    assert not is_complete(qs)
    qs = apply_answers(qs, {"sop_ingest": SKIP_ANSWER})
    assert is_complete(qs)
    assert next_interview_question(qs) is None


def test_goal_stays_mandatory():
    qs = fresh_interview()
    assert not is_complete(qs)
    assert next_interview_question(qs).id == "goal"


def test_skipped_answers_are_kept_out_of_the_brief():
    qs = with_project_questions(apply_answers(fresh_interview(), {"goal": PLAIN_GOAL}), _tailored())
    qs = apply_answers(qs, {"grading_mode": SKIP_ANSWER, "sop_ingest": "Sync from SharePoint"})
    brief = brief_from_interview(qs)
    assert "grading_mode" not in brief.interview_answers
    assert brief.interview_answers["sop_ingest"] == "Sync from SharePoint"
    assert brief.problem_statement == PLAIN_GOAL


def test_is_skipped_sentinel():
    assert is_skipped(SKIP_ANSWER)
    assert is_skipped("-")
    assert not is_skipped("MCQ only")
    assert not is_skipped("")


def test_expanded_brief_still_fills_stack_domains_constraints():
    qs = apply_answers(fresh_interview(), {"goal": EXPANDED_GOAL})
    brief = brief_from_interview(qs)
    assert "FastAPI" in brief.tech_stack
    assert "API" in brief.domains
    assert "VAPT" in brief.constraints
    assert "GitHub" in brief.integrations


def test_prefill_does_not_overwrite_existing_answers():
    qs = fresh_interview() + [InterviewQuestion(id="stack", prompt="Stack?", required=False)]
    qs = apply_answers(qs, {"stack": "Custom stack only"})
    qs = prefill_from_expanded_goal(qs, EXPANDED_GOAL)
    by_id = {q.id: q.answer for q in qs}
    assert by_id["stack"] == "Custom stack only"


def test_explicit_answer_beats_expanded_brief_section():
    qs = fresh_interview() + [InterviewQuestion(id="stack", prompt="Stack?", required=False)]
    qs = apply_answers(qs, {"goal": EXPANDED_GOAL, "stack": "Django + HTMX only"})
    brief = brief_from_interview(qs)
    assert brief.tech_stack == "Django + HTMX only"


def test_legacy_generic_questions_are_replaced_with_tailored_ones():
    legacy = fresh_interview() + [
        InterviewQuestion(id="users", prompt="Who are the primary users?", required=False),
        InterviewQuestion(id="stack", prompt="Preferred tech stack?", required=False),
    ]
    legacy = apply_answers(legacy, {"goal": PLAIN_GOAL})
    assert needs_project_questions(legacy)
    updated = with_project_questions(legacy, _tailored())
    assert [q.id for q in updated] == ["goal", "grading_mode", "sop_ingest"]


def test_legacy_questions_with_answers_are_preserved():
    legacy = fresh_interview() + [
        InterviewQuestion(id="users", prompt="Who are the primary users?", required=False),
    ]
    legacy = apply_answers(legacy, {"goal": PLAIN_GOAL, "users": "Plant floor trainers"})
    assert not needs_project_questions(legacy)


def test_fallback_questions_are_goal_anchored_and_optional():
    qs = fallback_project_questions("Build a smart tutor for SOP training")
    assert 1 <= len(qs) <= 4
    assert all(not q.required and q.id != "goal" for q in qs)
    assert any("smart tutor" in q.prompt.lower() for q in qs)


@pytest.mark.asyncio
async def test_generate_project_questions_falls_back_when_llm_fails(monkeypatch):
    async def boom(*_args, **_kwargs):
        raise RuntimeError("bedrock down")

    monkeypatch.setattr("app.services.interview.engine.llm_client.complete_json", boom)
    qs = await generate_project_questions(PLAIN_GOAL)
    assert qs
    assert all(not q.required for q in qs)


@pytest.mark.asyncio
async def test_generate_project_questions_caps_and_normalizes(monkeypatch):
    async def fake(*_args, **_kwargs):
        return {
            "questions": [
                {"id": "Grading Mode!", "prompt": "Grade free text or MCQ only?"},
                {"id": "goal", "prompt": "Duplicate id must be renamed?"},
                {"id": "too_short", "prompt": "no"},
                {"id": "a", "prompt": "Do trainers need offline access?"},
                {"id": "b", "prompt": "Should courses expire after a year?"},
                {"id": "c", "prompt": "Is SCORM export required?"},
            ]
        }

    monkeypatch.setattr("app.services.interview.engine.llm_client.complete_json", fake)
    qs = await generate_project_questions(PLAIN_GOAL)
    assert len(qs) <= 4
    ids = [q.id for q in qs]
    assert "goal" not in ids
    assert "grading_mode" in ids
    assert len(set(ids)) == len(ids)
    assert all(len(q.prompt) >= 8 for q in qs)
