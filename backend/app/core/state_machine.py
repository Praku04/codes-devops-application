"""Explicit project finite state machine."""

from __future__ import annotations

from enum import Enum
from typing import Iterable


class ProjectState(str, Enum):
    CREATED = "CREATED"
    AWAITING_DOCS = "AWAITING_DOCS"
    AWAITING_INTERVIEW = "AWAITING_INTERVIEW"
    CONTEXT_READY = "CONTEXT_READY"
    PLATFORM_SELECTED = "PLATFORM_SELECTED"
    GENERATING = "GENERATING"
    READY_FOR_REVIEW = "READY_FOR_REVIEW"
    EXPORTED = "EXPORTED"


class ProjectPath(str, Enum):
    DOCS = "docs"
    INTERVIEW = "interview"

#added GitHub Copilot as a platform
class Platform(str, Enum):
    CLAUDE_CODE = "claude_code"
    CURSOR = "cursor"
    WINDSURF = "windsurf"
    GITHUB_COPILOT = "github_copilot"


# action -> (allowed_from_states, next_state)
TRANSITIONS: dict[str, tuple[set[ProjectState], ProjectState | None]] = {
    "choose_path_docs": ({ProjectState.CREATED}, ProjectState.AWAITING_DOCS),
    "choose_path_interview": ({ProjectState.CREATED}, ProjectState.AWAITING_INTERVIEW),
    "submit_documents": ({ProjectState.AWAITING_DOCS}, ProjectState.CONTEXT_READY),
    "complete_interview": ({ProjectState.AWAITING_INTERVIEW}, ProjectState.CONTEXT_READY),
    "set_platform": (
        {ProjectState.CONTEXT_READY},
        ProjectState.PLATFORM_SELECTED,
    ),
    "start_generate": ({ProjectState.PLATFORM_SELECTED}, ProjectState.GENERATING),
    "generate_ok": ({ProjectState.GENERATING}, ProjectState.READY_FOR_REVIEW),
    "generate_failed": ({ProjectState.GENERATING}, ProjectState.PLATFORM_SELECTED),
    "edit_plan": ({ProjectState.READY_FOR_REVIEW, ProjectState.EXPORTED}, ProjectState.READY_FOR_REVIEW),
    "export": ({ProjectState.READY_FOR_REVIEW, ProjectState.EXPORTED}, ProjectState.EXPORTED),
    # One-step rollback (wizard "Back") — locked after generate (same as UI)
    "rollback_to_created": (
        {ProjectState.AWAITING_DOCS, ProjectState.AWAITING_INTERVIEW},
        ProjectState.CREATED,
    ),
    "rollback_to_docs": ({ProjectState.CONTEXT_READY}, ProjectState.AWAITING_DOCS),
    "rollback_to_interview": ({ProjectState.CONTEXT_READY}, ProjectState.AWAITING_INTERVIEW),
    "rollback_to_context": ({ProjectState.PLATFORM_SELECTED}, ProjectState.CONTEXT_READY),
}


class IllegalTransitionError(Exception):
    def __init__(self, action: str, current: ProjectState, allowed: Iterable[ProjectState]):
        self.action = action
        self.current = current
        self.allowed = list(allowed)
        super().__init__(
            f"Cannot '{action}' from state {current.value}. "
            f"Allowed from: {[s.value for s in self.allowed]}"
        )


def assert_can(action: str, current: ProjectState) -> ProjectState | None:
    if action not in TRANSITIONS:
        raise ValueError(f"Unknown action: {action}")
    allowed, nxt = TRANSITIONS[action]
    if current not in allowed:
        raise IllegalTransitionError(action, current, allowed)
    return nxt


def blockers_for(state: ProjectState, *, has_platform: bool, has_plan: bool) -> list[str]:
    """Human-readable next-step hints for UI/CLI."""
    mapping: dict[ProjectState, list[str]] = {
        ProjectState.CREATED: ["Choose a path: docs or interview"],
        ProjectState.AWAITING_DOCS: ["Upload documents and/or provide a problem statement"],
        ProjectState.AWAITING_INTERVIEW: ["Complete the interview questions"],
        ProjectState.CONTEXT_READY: ["Select an IDE platform (claude_code, cursor, windsurf, github_copilot)"],
        ProjectState.PLATFORM_SELECTED: [
            "Run generate to build agents, skills, and rules for the selected IDE"
        ],
        ProjectState.GENERATING: ["Wait for generation to finish"],
        ProjectState.READY_FOR_REVIEW: ["Review/edit the plan, then export (no step back after generate)"],
        ProjectState.EXPORTED: ["Open Export to download again, or start a new project"],
    }
    tips = list(mapping.get(state, []))
    if state == ProjectState.CONTEXT_READY and not has_platform:
        tips.append("Platform is required before generate")
    if state in {ProjectState.READY_FOR_REVIEW, ProjectState.EXPORTED} and not has_plan:
        tips.append("Plan is missing — regenerate")
    return tips
