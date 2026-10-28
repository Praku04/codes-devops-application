"""Document parse + statement/document match tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.core.exceptions import AppError
from app.services.parser.documents import ALLOWED_EXTENSIONS, IMAGE_EXTENSIONS, parse_bytes
from app.services.parser.match import assert_statement_matches_documents, heuristic_matches


def test_allowed_extensions_include_images_and_docs():
    assert ".pdf" in ALLOWED_EXTENSIONS
    assert ".docx" in ALLOWED_EXTENSIONS
    assert ".md" in ALLOWED_EXTENSIONS
    assert ".txt" in ALLOWED_EXTENSIONS
    assert ".png" in IMAGE_EXTENSIONS
    assert ".jpeg" in IMAGE_EXTENSIONS
    assert ".jpg" in IMAGE_EXTENSIONS


def test_parse_txt_bytes_full_content():
    body = "Payments ledger with webhook HMAC and FastAPI merchants.\n" * 20
    text = parse_bytes("brief.txt", body.encode("utf-8"))
    assert "Payments ledger" in text
    assert text.count("FastAPI") == 20


def test_parse_rejects_unsupported():
    with pytest.raises(AppError, match="Unsupported"):
        parse_bytes("notes.csv", b"a,b,c")


def test_heuristic_match_accepts_aligned_content():
    statement = (
        "Build a multi-tenant payments platform with double-entry ledger, "
        "FastAPI merchants, and webhook HMAC verification."
    )
    docs = (
        "### prd.md\n"
        "Payments platform requirements\n"
        "- Multi-tenant merchants on FastAPI\n"
        "- Double-entry ledger posting\n"
        "- Webhook HMAC signatures for processors\n"
    )
    ok, _ = heuristic_matches(statement, docs)
    assert ok is True


def test_heuristic_match_rejects_unrelated():
    statement = (
        "Telehealth clinic booking with FHIR export and HIPAA video visits."
    )
    docs = (
        "### fleet.txt\n"
        "MQTT GPS telematics, geofence dispatch, TimescaleDB routes for trucks.\n"
    )
    ok, msg = heuristic_matches(statement, docs)
    assert ok is False
    assert "does not appear to match" in msg


@pytest.mark.asyncio
async def test_assert_match_requires_both_sides():
    with pytest.raises(AppError, match="Problem statement"):
        await assert_statement_matches_documents("", "some docs", use_llm=False)
    with pytest.raises(AppError, match="document"):
        await assert_statement_matches_documents(
            "Build a payments API with ledger and webhooks for merchants",
            "",
            use_llm=False,
        )


@pytest.mark.asyncio
async def test_assert_match_blocks_mismatch_without_llm():
    with pytest.raises(AppError, match="does not appear to match"):
        await assert_statement_matches_documents(
            "Clinic telehealth FHIR HIPAA Cognito Twilio video booking",
            "### a.txt\nTruck fleet MQTT GPS TimescaleDB geofence dispatch only",
            use_llm=False,
        )


@pytest.mark.asyncio
async def test_semantic_match_required_even_when_keywords_overlap():
    """Keyword-heavy but unrelated context must still fail semantic check."""
    statement = (
        "Build a multi-tenant FastAPI platform with webhooks and Angular admin."
    )
    docs = (
        "### wrong.md\n"
        "This FastAPI webhook tutorial for a multi-tenant blog CMS with Angular admin "
        "comments has nothing to do with Smart Tutor learning or SOPs.\n"
    )

    async def fake_json(messages, **kwargs):
        return {
            "match": False,
            "score": 0.25,
            "reason": "Shared stack words only; documents are about a blog CMS, not the stated product.",
        }

    with patch(
        "app.services.llm.client.llm_client.complete_json",
        new=AsyncMock(side_effect=fake_json),
    ):
        with pytest.raises(AppError, match="blog CMS|does not match|Semantic|context"):
            await assert_statement_matches_documents(statement, docs, use_llm=True)


@pytest.mark.asyncio
async def test_semantic_match_passes_paraphrase_without_exact_keywords():
    statement = "We need an AI coach that turns company SOPs into courses and quizzes for employees."
    docs = (
        "### brd.md\n"
        "Smart Tutor converts standard operating procedures into modular learning paths, "
        "assessments, and role-based training for the enterprise workforce.\n"
    )

    async def fake_json(messages, **kwargs):
        assert kwargs.get("retries") == 0  # fast path: no long Bedrock retries
        return {
            "match": True,
            "score": 0.88,
            "reason": "Same product intent: SOP-to-training AI platform.",
        }

    with patch(
        "app.services.llm.client.llm_client.complete_json",
        new=AsyncMock(side_effect=fake_json),
    ):
        await assert_statement_matches_documents(statement, docs, use_llm=True)


@pytest.mark.asyncio
async def test_auth_expired_surfaces_clear_error():
    with patch(
        "app.services.llm.client.llm_client.complete_json",
        new=AsyncMock(
            side_effect=AppError(
                "AWS Bedrock credentials expired or invalid. "
                "Update AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, and AWS_SESSION_TOKEN in .env, "
                "then restart the API.",
                status_code=502,
            )
        ),
    ):
        with pytest.raises(AppError, match="credentials expired"):
            await assert_statement_matches_documents(
                "Build Smart Tutor AI that turns SOPs into learning modules for employees.",
                "### brd.md\nSmart Tutor SOP learning modules assessments workforce training.\n",
                use_llm=True,
            )
