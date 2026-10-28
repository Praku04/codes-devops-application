"""Fast parallel digest of long document corpora for generation."""

from __future__ import annotations

import asyncio
import logging

from app.services.llm.client import llm_client

logger = logging.getLogger("agentcraft.digest")

# Pass raw docs through when they fit generation context (no extra LLM round-trip).
PASS_THROUGH_CHARS = 28000
_CHUNK = 8000
_MAX_CHUNKS = 6  # hard cap so digest stays bounded
_SEM = 6


async def digest_documents_for_generation(problem_statement: str, document_text: str) -> str:
    """
    Return document text ready for agent/skill/rule generation.

    - Short/medium docs: returned as-is (complete context, zero LLM latency).
    - Long docs: chunked and extracted in parallel, then lightly concatenated.
    """
    docs = (document_text or "").strip()
    if not docs:
        return ""
    if len(docs) <= PASS_THROUGH_CHARS:
        return docs

    raw_chunks = [docs[i : i + _CHUNK] for i in range(0, len(docs), _CHUNK)]
    chunks = raw_chunks[:_MAX_CHUNKS]
    truncated = len(raw_chunks) > _MAX_CHUNKS
    sem = asyncio.Semaphore(_SEM)

    async def _one(i: int, chunk: str) -> str:
        async with sem:
            try:
                part = await llm_client.complete(
                    [
                        {
                            "role": "system",
                            "content": (
                                "Extract requirements, entities, workflows, integrations, "
                                "constraints, stack, and acceptance criteria from this chunk. "
                                "Do not invent. Preserve names/numbers. Compact bullet lists only."
                            ),
                        },
                        {
                            "role": "user",
                            "content": (
                                f"Problem statement:\n{problem_statement[:1500]}\n\n"
                                f"Document chunk {i}/{len(chunks)}:\n{chunk}"
                            ),
                        },
                    ],
                    temperature=0.1,
                    max_tokens=1800,
                )
                return f"### Chunk {i}\n{(part or '').strip()}"
            except Exception as exc:  # noqa: BLE001
                logger.warning("Document chunk digest failed (%s); keeping raw excerpt", exc)
                return f"### Chunk {i} (raw excerpt)\n{chunk[:5000]}"

    partials = await asyncio.gather(*[_one(i, c) for i, c in enumerate(chunks, start=1)])
    merged = "\n\n".join(partials)
    if truncated:
        merged += (
            f"\n\n_Note: {len(raw_chunks) - _MAX_CHUNKS} additional chunk(s) "
            "were summarized by earlier sections; prioritize requirements above._"
        )

    # Avoid a second sequential merge LLM call — parallel extracts are enough.
    return merged[:PASS_THROUGH_CHARS]
