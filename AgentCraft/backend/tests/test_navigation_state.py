"""Back/forward navigation must never discard answers or the saved brief."""

from app.core.state_machine import ProjectPath, ProjectState
from app.services.projects import project_service


def _interview_project():
    status = project_service.create(user_id="nav-tester")
    return project_service.set_path(status.id, ProjectPath.INTERVIEW)


def test_rollback_to_created_keeps_interview_answers():
    status = _interview_project()

    # Seed a goal answer directly so the test never touches the LLM.
    from app.services.interview.engine import apply_answers
    from app.services.projects import repo

    seeded = apply_answers(status.interview, {"goal": "Build an SOP tutor for trainers."})
    repo.update(status.id, interview=seeded)

    back = project_service.rollback(status.id)
    assert back.state == ProjectState.CREATED
    assert back.path is None
    goal = next(q for q in back.interview if q.id == "goal")
    assert goal.answer == "Build an SOP tutor for trainers."


def test_reentering_interview_path_restores_answers():
    status = _interview_project()

    from app.services.interview.engine import apply_answers
    from app.services.projects import repo

    seeded = apply_answers(status.interview, {"goal": "Invoice reconciliation agent."})
    repo.update(status.id, interview=seeded)

    project_service.rollback(status.id)
    again = project_service.set_path(status.id, ProjectPath.INTERVIEW)

    goal = next(q for q in again.interview if q.id == "goal")
    assert goal.answer == "Invoice reconciliation agent."


def test_rollback_from_context_ready_keeps_expanded_brief():
    """A single Back click used to clear the last answer, destroying the brief."""
    from app.services.interview.engine import apply_answers
    from app.services.projects import repo

    status = _interview_project()
    brief_text = (
        "## Product\n\nSmart Tutor AI.\n\n## Problem\n\nSOP training is slow.\n\n"
        "## Goals\n\n- Convert SOPs\n\n## Stack\n\n- FastAPI\n"
    )
    seeded = apply_answers(status.interview, {"goal": brief_text})
    repo.update(
        status.id,
        state=ProjectState.CONTEXT_READY,
        path=ProjectPath.INTERVIEW,
        interview=seeded,
    )

    back = project_service.rollback(status.id)
    assert back.state == ProjectState.AWAITING_INTERVIEW
    goal = next(q for q in back.interview if q.id == "goal")
    # apply_answers strips surrounding whitespace; the body must be intact.
    assert goal.answer == brief_text.strip()


def test_rollback_from_context_ready_docs_keeps_statement():
    from app.models.schemas import ProjectBrief
    from app.services.projects import repo

    status = project_service.create(user_id="nav-tester")
    project_service.set_path(status.id, ProjectPath.DOCS)
    brief = ProjectBrief(
        problem_statement="Automate warehouse restocking alerts.",
        document_text="### spec.md\nRestocking rules.",
    )
    repo.update(status.id, state=ProjectState.CONTEXT_READY, brief=brief)

    back = project_service.rollback(status.id)
    assert back.state == ProjectState.AWAITING_DOCS
    assert back.brief is not None
    assert back.brief.problem_statement == "Automate warehouse restocking alerts."
    # Filenames survive so the UI can name what needs re-attaching.
    assert "### spec.md" in back.brief.document_text
