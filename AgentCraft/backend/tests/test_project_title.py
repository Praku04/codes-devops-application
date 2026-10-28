"""Project titles stay readable for both the docs and interview paths."""

from app.services.projects import DEFAULT_PROJECT_NAME, derive_project_title

EXPANDED_BRIEF = """## Product

**Smart Tutor AI** is an enterprise AI-powered learning platform. It converts SOPs
into guided courses.

## Problem

Manual SOP training is slow.

## Stack

- FastAPI
"""


def test_expanded_brief_title_strips_markdown():
    title = derive_project_title(EXPANDED_BRIEF)
    assert title.startswith("Smart Tutor AI is an enterprise")
    assert "##" not in title
    assert "**" not in title


def test_plain_statement_uses_first_sentence():
    statement = (
        "Build a smart tutor that turns corporate SOP documents into guided courses. "
        "It should also grade trainees."
    )
    assert derive_project_title(statement) == (
        "Build a smart tutor that turns corporate SOP documents into guided courses"
    )


def test_title_is_capped_at_80_chars():
    assert len(derive_project_title("word " * 100)) <= 80


def test_short_first_sentence_keeps_the_whole_line():
    # "Hi." is too short to stand alone, so the full line is used instead.
    title = derive_project_title("Hi. We need an internal invoice reconciliation agent.")
    assert "invoice reconciliation" in title


def test_bullet_and_heading_only_text_falls_back():
    assert derive_project_title("## Product\n\n") == DEFAULT_PROJECT_NAME
    assert derive_project_title("", fallback="Keep me") == "Keep me"


def test_leading_bullet_is_stripped():
    assert derive_project_title("- Build an onboarding assistant for new hires").startswith(
        "Build an onboarding assistant"
    )


def test_trailing_punctuation_trimmed():
    assert not derive_project_title("An internal payments platform -").endswith("-")
