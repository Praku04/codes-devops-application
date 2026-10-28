"""Validate that a problem statement aligns with uploaded document content.

Primary check is semantic/context similarity via LLM.
Keyword overlap is only supporting evidence (not sufficient alone).
"""

from __future__ import annotations

import logging
import re

from app.core.exceptions import AppError
from app.services.parser.documents import ALLOWED_LABEL

logger = logging.getLogger("agentcraft.match")

_STOP = {
    "about",
    "above",
    "after",
    "again",
    "also",
    "and",
    "any",
    "are",
    "because",
    "been",
    "before",
    "being",
    "between",
    "both",
    "but",
    "can",
    "could",
    "did",
    "does",
    "doing",
    "each",
    "for",
    "from",
    "had",
    "has",
    "have",
    "having",
    "here",
    "how",
    "into",
    "its",
    "just",
    "more",
    "most",
    "not",
    "only",
    "other",
    "our",
    "out",
    "over",
    "same",
    "should",
    "some",
    "such",
    "than",
    "that",
    "the",
    "their",
    "then",
    "there",
    "these",
    "they",
    "this",
    "those",
    "through",
    "under",
    "until",
    "very",
    "was",
    "were",
    "what",
    "when",
    "where",
    "which",
    "while",
    "who",
    "will",
    "with",
    "would",
    "your",
    "build",
    "building",
    "create",
    "creating",
    "using",
    "need",
    "needs",
    "want",
    "wants",
    "like",
    "make",
    "system",
    "application",
    "platform",
    "project",
    "please",
}


def _tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9][a-z0-9_-]{2,}", (text or "").lower())
    return {w for w in words if w not in _STOP}


def heuristic_match_score(statement: str, document_text: str) -> tuple[float, int, int]:
    """Return (ratio, hits, total_significant_statement_terms). Supporting signal only."""
    stmt_words = _tokens(statement)
    if not stmt_words:
        return 1.0, 0, 0
    doc_lower = (document_text or "").lower()
    hits = sum(1 for w in stmt_words if w in doc_lower)
    return hits / len(stmt_words), hits, len(stmt_words)


def heuristic_matches(statement: str, document_text: str) -> tuple[bool, str]:
    """Legacy keyword gate — used only when LLM is disabled (tests / offline)."""
    ratio, hits, total = heuristic_match_score(statement, document_text)
    if total == 0:
        return True, "ok"
    if ratio >= 0.28 or hits >= 6:
        return True, "ok"
    return (
        False,
        (
            f"Problem statement does not appear to match the uploaded documents "
            f"(found {hits}/{total} key terms from the statement in the files). "
            "Update the statement so it describes the same product/domain as the documents, "
            "or upload documents that match this problem."
        ),
    )


async def assert_statement_matches_documents(
    statement: str,
    document_text: str,
    *,
    use_llm: bool = True,
) -> None:
    """
    Raise AppError(400) when statement and documents are not contextually similar.

    With use_llm=True (default): fast async semantic similarity check (single LLM call).
    Keyword overlap alone is never enough to pass when LLM is available.
    """
    stmt = (statement or "").strip()
    docs = (document_text or "").strip()
    if len(stmt) < 12:
        raise AppError(
            "Problem statement is required (describe the product or problem in a few sentences).",
            status_code=400,
        )
    if not docs:
        raise AppError(
            "At least one readable document is required "
            f"({ALLOWED_LABEL}).",
            status_code=400,
        )

    ratio, hits, total = heuristic_match_score(stmt, docs)

    if use_llm:
        try:
            matched, reason, score = await _llm_semantic_similarity(
                stmt, docs, keyword_ratio=ratio
            )
            if matched and score >= 0.55:
                logger.info(
                    "docs_match ok semantic=%.2f keywords=%.2f (%s/%s) reason=%s",
                    score,
                    ratio,
                    hits,
                    total,
                    reason,
                )
                return
            detail = reason or (
                "Problem statement does not match the context of the uploaded documents. "
                "Align the statement with the same product, goals, and domain as the files."
            )
            if total:
                detail += f" (keyword overlap {hits}/{total}; semantic score {score:.2f})"
            raise AppError(detail, status_code=400)
        except AppError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("Semantic match LLM failed, falling back to keywords: %s", exc)

    ok, detail = heuristic_matches(stmt, docs)
    if ok:
        return
    raise AppError(detail, status_code=400)


async def _llm_semantic_similarity(
    statement: str,
    document_text: str,
    *,
    keyword_ratio: float,
) -> tuple[bool, str, float]:
    """
    Fast async semantic/context similarity (one compact LLM call, no long retries).

    Returns (match, reason, score 0..1).
    """
    from app.services.llm.client import llm_client

    # Keep the prompt small: this call sits in front of the user's Continue
    # click, so input/output size is the only latency lever we control.
    # Measured when this was tuned, on whichever Sonnet was configured then:
    # 8000ch/220tok ≈ 3.24s, 3000ch/80tok ≈ 2.72s. The ratio is the point, not the absolute times.
    # A domain judgement does not improve with more of the document.
    excerpt = document_text[:3000]
    seed = statement[:1500]
    data = await llm_client.complete_json(
        [
            {
                "role": "system",
                "content": (
                    "Do the statement and documents describe the SAME product, domain and goals? "
                    "Shared tech words alone ≠ match. "
                    'Reply ONLY: {"match":true|false,"score":0.0-1.0,"reason":"<=12 words"}'
                ),
            },
            {
                "role": "user",
                "content": (
                    f"## Problem statement\n{seed}\n\n"
                    f"## Documents\n{excerpt}\n\n"
                    f"keyword_ratio≈{keyword_ratio:.2f} (supporting only). JSON now."
                ),
            },
        ],
        temperature=0.0,
        max_tokens=96,
        retries=0,  # fail fast — auth errors should not spin 6×
        # Without this the call inherits the 300s ceiling; 25s is far past the
        # ~3s this judgement needs, and the keyword fallback covers a timeout.
        timeout=25,
    )
    if not isinstance(data, dict):
        return False, "Could not evaluate document context similarity.", 0.0

    match = bool(data.get("match"))
    reason = str(data.get("reason") or "").strip()
    try:
        score = float(data.get("score", 0.7 if match else 0.2))
    except (TypeError, ValueError):
        score = 0.7 if match else 0.2
    score = max(0.0, min(1.0, score))

    # Require both model match flag and a reasonable similarity score
    if match and score < 0.55:
        match = False
        reason = reason or "Semantic similarity score too low for the statement and documents."
    if not match and not reason:
        reason = (
            "Problem statement context does not align with the uploaded documents. "
            "Update the statement or upload matching files."
        )
    return match, reason, score
