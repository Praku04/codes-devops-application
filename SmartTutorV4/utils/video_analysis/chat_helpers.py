"""Pure helpers for Video Analysis chat — no AWS/DB imports (fast to test)."""

from __future__ import annotations

import re
from typing import List, Optional

VA_CHAT_MIN_RELEVANCE_SCORE = 0.38
VIDEO_ANALYSIS_NO_MATCH_PHRASE = "No relevant information found"

VIDEO_ANALYSIS_NO_MATCH_REPLY = (
    "I couldn't find anything related to that in your indexed meeting reports. "
    "Please try asking about topics covered in those reports."
)

_QUERY_STOPWORDS = frozenset({
    "what", "when", "where", "which", "who", "whom", "whose", "how", "why",
    "does", "do", "did", "that", "this", "those", "these", "with", "from",
    "about", "your", "have", "been", "were", "will", "would", "could", "should",
    "there", "their", "them", "they", "into", "than", "then", "also", "just",
    "like", "some", "such", "only", "more", "most", "very", "much", "many",
    "tell", "give", "show", "explain", "describe", "please", "help", "need",
    "want", "know", "related", "anything", "something", "nothing", "any",
})

_NO_MATCH_MARKERS = (
    "couldn't find anything related",
    "could not find anything related",
    "nothing related was found",
    "indexed reports do not cover",
    "indexed reports do not contain",
    "do not contain any information related",
    "do not cover that topic",
    "cannot provide any relevant details",
    "no relevant information found",
    "not contain any information",
)


def query_content_tokens(query: str) -> set[str]:
    """Meaningful query tokens used to verify retrieval relevance."""
    return {
        part.lower()
        for part in re.split(r"[^\w]+", query or "")
        if len(part) >= 3 and part.lower() not in _QUERY_STOPWORDS
    }


def context_supports_query(query: str, context: str) -> bool:
    """True when retrieved text shares at least one meaningful token with the query."""
    tokens = query_content_tokens(query)
    if not tokens:
        return True
    ctx_lower = (context or "").lower()
    return any(token in ctx_lower for token in tokens)


def video_analysis_search_is_miss(context: str) -> bool:
    """True when semantic search found nothing sufficiently related to the query."""
    if not context or not str(context).strip():
        return True
    return VIDEO_ANALYSIS_NO_MATCH_PHRASE in context


def is_video_analysis_no_match_reply(text: str) -> bool:
    """True when the assistant reply indicates the query is outside indexed reports."""
    lowered = (text or "").strip().lower()
    if not lowered:
        return False
    return any(marker in lowered for marker in _NO_MATCH_MARKERS)


def normalize_video_analysis_no_match_reply(text: str) -> str:
    """Return the canonical short no-match message when the reply is out-of-scope."""
    if is_video_analysis_no_match_reply(text):
        return VIDEO_ANALYSIS_NO_MATCH_REPLY
    return (text or "").strip()


def query_overlaps_report_titles(query: str, report_titles: Optional[List[str]]) -> bool:
    """True when the user query shares a meaningful token with an indexed report title."""
    if not query or not report_titles:
        return False
    q_tokens = {
        part.lower()
        for part in re.split(r"[^\w]+", query)
        if len(part) >= 4
    }
    if not q_tokens:
        return False
    for title in report_titles:
        title_tokens = {
            part.lower()
            for part in re.split(r"[^\w]+", title or "")
            if len(part) >= 4
        }
        if q_tokens & title_tokens:
            return True
    return False
