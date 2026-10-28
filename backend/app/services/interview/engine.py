"""Interactive interview question bank for Path B.

Only the problem statement (``goal``) is mandatory. Once it is answered, a short
set of *project-specific* follow-ups is generated from the goal itself — every
one of them is skippable.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from app.models.schemas import InterviewQuestion, ProjectBrief
from app.services.llm.client import llm_client

logger = logging.getLogger("agentcraft.interview")

_QUESTIONS_PROMPT = (
    Path(__file__).resolve().parents[2] / "prompts" / "interview_questions_system.txt"
)

#: Sentinel stored as the answer when a user skips an optional question.
SKIP_ANSWER = "—"

#: Max project-specific follow-ups appended after the goal.
MAX_PROJECT_QUESTIONS = 4

GOAL_QUESTION = InterviewQuestion(
    id="goal",
    prompt="What product or problem are you building? Describe the goal in a few sentences.",
    required=True,
)

CORE_QUESTIONS: list[InterviewQuestion] = [GOAL_QUESTION]

#: Generic question ids used before project-specific generation existed. Sessions
#: created back then are migrated to tailored questions on the next goal answer.
LEGACY_OPTIONAL_IDS = frozenset(
    {"users", "stack", "domains", "constraints", "integrations"}
)


def fresh_interview() -> list[InterviewQuestion]:
    return [q.model_copy(deep=True) for q in CORE_QUESTIONS]


def is_skipped(answer: str | None) -> bool:
    """True when an answer only carries the skip sentinel."""
    text = (answer or "").strip()
    return bool(text) and text.strip("—-–— \t") == ""


def is_answered(question: InterviewQuestion) -> bool:
    """Answered *or* explicitly skipped — either way the user is done with it."""
    return bool((question.answer or "").strip())


def apply_answers(
    questions: list[InterviewQuestion], answers: dict[str, str]
) -> list[InterviewQuestion]:
    updated: list[InterviewQuestion] = []
    for q in questions:
        if q.id in answers and answers[q.id].strip():
            updated.append(q.model_copy(update={"answer": answers[q.id].strip()}))
        else:
            updated.append(q)
    goal = answers.get("goal", "").strip()
    if goal and is_structured_expanded_brief(goal):
        updated = prefill_from_expanded_goal(updated, goal)
    return updated


def _extract_brief_sections(text: str) -> dict[str, str]:
    """Map ## headings from an expanded brief to normalized keys."""
    sections: dict[str, str] = {}
    for chunk in re.split(r"^##\s+", (text or "").strip(), flags=re.M):
        chunk = chunk.strip()
        if not chunk:
            continue
        lines = chunk.splitlines()
        title = lines[0].strip().lower()
        body = "\n".join(lines[1:]).strip()
        sections[title] = body
    return sections


def is_structured_expanded_brief(text: str) -> bool:
    """True when the goal answer is a multi-section expanded brief (not a short line)."""
    t = (text or "").strip()
    if len(t) < 120:
        return False
    if not re.search(r"^##\s+", t, flags=re.M):
        return False
    headings = re.findall(r"^##\s+\S+", t, flags=re.M)
    return len(headings) >= 3


def brief_fields_from_expanded_goal(goal_answer: str) -> dict[str, str]:
    """Derive users / stack / domains / constraints / integrations from brief sections."""
    if not is_structured_expanded_brief(goal_answer):
        return {}

    secs = _extract_brief_sections(goal_answer)

    def section(*needles: str) -> str:
        for title, body in secs.items():
            if any(n in title for n in needles):
                return body.strip()
        return ""

    product = section("product")
    return {
        "users": (section("user", "persona", "audience", "stakeholder") or product)[:600],
        "stack": section("stack", "technology", "tech")[:800],
        "domains": section("domain", "specialist")[:500],
        "constraints": section("constraint", "compliance", "standard", "security")[:700],
        "integrations": section("integration", "external", "mcp", "tool")[:500],
    }


def prefill_from_expanded_goal(
    questions: list[InterviewQuestion], goal_answer: str
) -> list[InterviewQuestion]:
    """Auto-fill stack/domains/etc. from expanded goal brief — skip repeat questions."""
    prefills = brief_fields_from_expanded_goal(goal_answer)
    if not prefills:
        return questions

    out: list[InterviewQuestion] = []
    for q in questions:
        val = prefills.get(q.id, "")
        if val and not (q.answer or "").strip():
            out.append(q.model_copy(update={"answer": val}))
        else:
            out.append(q)
    return out


def goal_answer(questions: list[InterviewQuestion]) -> str:
    return next((q.answer or "" for q in questions if q.id == "goal"), "").strip()


def _pending_legacy_questions(questions: list[InterviewQuestion]) -> bool:
    """Old generic question set still sitting unanswered — replace with tailored ones."""
    extras = [q for q in questions if q.id != "goal"]
    if not extras:
        return False
    return all(q.id in LEGACY_OPTIONAL_IDS and not is_answered(q) for q in extras)


def needs_project_questions(questions: list[InterviewQuestion]) -> bool:
    """True when the goal is a plain statement and tailored follow-ups aren't asked yet."""
    goal = goal_answer(questions)
    if not goal or is_structured_expanded_brief(goal):
        return False
    extras = [q for q in questions if q.id != "goal"]
    return not extras or _pending_legacy_questions(questions)


def with_project_questions(
    questions: list[InterviewQuestion], generated: list[InterviewQuestion]
) -> list[InterviewQuestion]:
    """Replace pending generic follow-ups with the tailored set, keeping the goal first."""
    kept = [q for q in questions if q.id == "goal" or is_answered(q)]
    if _pending_legacy_questions(questions):
        kept = [q for q in kept if q.id == "goal" or q.id not in LEGACY_OPTIONAL_IDS]
    taken = {q.id for q in kept}
    for q in generated:
        if q.id in taken:
            continue
        taken.add(q.id)
        kept.append(q)
    return kept


def _slug_id(raw: str, index: int) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", (raw or "").strip().lower()).strip("_")
    return (slug or f"detail_{index + 1}")[:40]


def fallback_project_questions(goal: str) -> list[InterviewQuestion]:
    """Deterministic, still goal-anchored questions when the LLM is unavailable."""
    subject = re.sub(r"\s+", " ", (goal or "").strip())
    subject = re.sub(r"^(build|create|make|develop|design)\s+(a|an|the)?\s*", "", subject, flags=re.I)
    subject = subject.split(".")[0][:80] or "this product"
    return [
        InterviewQuestion(
            id="primary_users",
            prompt=f"Who will use {subject} day to day, and what do they do first?",
            required=False,
        ),
        InterviewQuestion(
            id="core_workflow",
            prompt=f"Walk through the single most important workflow in {subject}.",
            required=False,
        ),
        InterviewQuestion(
            id="data_and_systems",
            prompt=f"What data or existing systems does {subject} read from or write to?",
            required=False,
        ),
        InterviewQuestion(
            id="hard_requirements",
            prompt=f"Any hard requirement for {subject} — stack, compliance, or deadline?",
            required=False,
        ),
    ]


async def generate_project_questions(goal: str) -> list[InterviewQuestion]:
    """Ask the LLM for a few project-specific, skippable follow-ups. Fails open."""
    goal = (goal or "").strip()
    if not goal:
        return []
    try:
        system = _QUESTIONS_PROMPT.read_text(encoding="utf-8")
    except OSError:
        logger.warning("interview questions prompt missing: %s", _QUESTIONS_PROMPT)
        return fallback_project_questions(goal)

    try:
        data = await llm_client.complete_json(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": f"Goal / problem statement:\n{goal[:4000]}"},
            ],
            temperature=0.3,
            max_tokens=900,
            timeout=45,
        )
    except Exception as exc:  # noqa: BLE001 — interview must never hard-fail here
        logger.warning("project question generation failed, using fallback: %s", exc)
        return fallback_project_questions(goal)

    raw = data.get("questions") if isinstance(data, dict) else None
    out: list[InterviewQuestion] = []
    seen: set[str] = {"goal"}
    for i, item in enumerate(raw or []):
        if len(out) >= MAX_PROJECT_QUESTIONS:
            break
        if not isinstance(item, dict):
            continue
        prompt = str(item.get("prompt") or "").strip()
        if len(prompt) < 8:
            continue
        qid = _slug_id(str(item.get("id") or ""), i)
        if qid in seen:
            qid = f"detail_{i + 1}"
        if qid in seen:
            continue
        seen.add(qid)
        out.append(InterviewQuestion(id=qid, prompt=prompt, required=False))

    return out or fallback_project_questions(goal)


def is_complete(questions: list[InterviewQuestion]) -> bool:
    """Goal is mandatory; optional follow-ups must be answered or skipped."""
    goal = goal_answer(questions)
    if not goal:
        return False
    if is_structured_expanded_brief(goal):
        return True
    return all(is_answered(q) for q in questions if q.id != "goal")


def unanswered(questions: list[InterviewQuestion]) -> list[InterviewQuestion]:
    """Required unanswered only (goal). Optional questions may stay empty."""
    return [q for q in questions if q.required and not is_answered(q)]


def next_interview_question(questions: list[InterviewQuestion]) -> InterviewQuestion | None:
    """Goal first, then the first optional follow-up that is neither answered nor skipped."""
    goal = next((q for q in questions if q.id == "goal"), None)
    if goal and not is_answered(goal):
        return goal
    return next((q for q in questions if q.id != "goal" and not is_answered(q)), None)


def brief_from_interview(questions: list[InterviewQuestion]) -> ProjectBrief:
    """Build the generation brief — skipped answers are dropped, not fed to the LLM."""
    answers = {
        q.id: (q.answer or "").strip()
        for q in questions
        if is_answered(q) and not is_skipped(q.answer)
    }
    goal = answers.get("goal", "")

    fields = dict(brief_fields_from_expanded_goal(goal))
    for key in ("users", "stack", "domains", "constraints", "integrations"):
        if answers.get(key):
            fields[key] = answers[key]

    domains = [d.strip() for d in (fields.get("domains") or "").split(",") if d.strip()]
    integrations = [
        i.strip()
        for i in (fields.get("integrations") or "").split(",")
        if i.strip() and i.lower() != "none"
    ]
    return ProjectBrief(
        problem_statement=goal,
        interview_answers=answers,
        tech_stack=fields.get("stack", ""),
        domains=domains,
        constraints=fields.get("constraints", ""),
        integrations=integrations,
    )
