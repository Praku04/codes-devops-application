"""Parallel async brief expansion tests."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app.services.brief_expand import (
    _SECTION_ORDER,
    expand_problem_statement,
    structure_to_markdown,
)


def test_structure_to_markdown_order():
    md = structure_to_markdown(
        {
            "goals": ["Ship MVP"],
            "product": "Payments API",
            "stack": "FastAPI",
        }
    )
    assert md.index("## Product") < md.index("## Goals")
    assert md.index("## Goals") < md.index("## Stack")


@pytest.mark.asyncio
async def test_expand_problem_statement_runs_sections_in_parallel():
    in_flight = 0
    max_in_flight = 0
    lock = asyncio.Lock()

    async def fake_complete_json(messages, **kwargs):
        nonlocal in_flight, max_in_flight
        user = messages[-1]["content"]
        section = next((k for k in _SECTION_ORDER if f"({k})" in user), "product")
        async with lock:
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0.08)
        async with lock:
            in_flight -= 1
        return {"section": section, "body": f"- detail for {section}"}

    with patch(
        "app.services.brief_expand.llm_client.complete_json",
        new=AsyncMock(side_effect=fake_complete_json),
    ):
        md = await expand_problem_statement(
            "Build a multi-tenant payments ledger with FastAPI"
        )

    assert "## Product" in md
    assert "## Goals" in md
    assert "## Stack" in md
    assert "## Success" in md
    assert "detail for product" in md
    assert "detail for goals" in md
    # Parallel: more than one section call overlapped
    assert max_in_flight >= 2
    assert max_in_flight >= 4  # true parallel: most sections overlap


@pytest.mark.asyncio
async def test_expand_long_seed_still_calls_llm():
    """Regression: long multi-line answers must not skip expansion."""
    calls = {"n": 0}

    async def fake_complete_json(messages, **kwargs):
        calls["n"] += 1
        user = messages[-1]["content"]
        section = next((k for k in _SECTION_ORDER if f"({k})" in user), "product")
        return {"section": section, "body": f"- enriched {section}"}

    long_seed = ("Enterprise learning platform paragraph. " * 40) + "\nMore line.\nThird line.\nFourth."
    assert len(long_seed) > 800

    with patch(
        "app.services.brief_expand.llm_client.complete_json",
        new=AsyncMock(side_effect=fake_complete_json),
    ):
        md = await expand_problem_statement(long_seed)

    assert calls["n"] == len(_SECTION_ORDER)
    assert "## Product" in md
    assert "## Goals" in md
    assert long_seed not in md
    assert "enriched product" in md
