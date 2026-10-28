"""Document digest speed / parallel behaviour."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app.services.parser.digest import PASS_THROUGH_CHARS, digest_documents_for_generation


@pytest.mark.asyncio
async def test_short_docs_pass_through_without_llm():
    docs = "Requirements\n- FastAPI payments ledger\n- HMAC webhooks\n" * 20
    assert len(docs) < PASS_THROUGH_CHARS
    with patch("app.services.parser.digest.llm_client.complete", new=AsyncMock()) as mock:
        out = await digest_documents_for_generation("Payments platform", docs)
    assert out == docs.strip()
    mock.assert_not_called()


@pytest.mark.asyncio
async def test_long_docs_digest_chunks_in_parallel():
    docs = ("Requirement line about multi-tenant payments and ledger posting.\n" * 2000)
    assert len(docs) > PASS_THROUGH_CHARS

    in_flight = 0
    max_in_flight = 0
    lock = asyncio.Lock()

    async def fake_complete(messages, **kwargs):
        nonlocal in_flight, max_in_flight
        async with lock:
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0.05)
        async with lock:
            in_flight -= 1
        return "- extracted requirement"

    with patch(
        "app.services.parser.digest.llm_client.complete",
        new=AsyncMock(side_effect=fake_complete),
    ):
        out = await digest_documents_for_generation("Payments platform", docs)

    assert "### Chunk" in out
    assert "extracted requirement" in out
    assert max_in_flight >= 2
