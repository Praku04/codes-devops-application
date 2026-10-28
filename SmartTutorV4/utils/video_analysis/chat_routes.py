"""Video Analysis dedicated chat API (scoped RAG + session history)."""

from __future__ import annotations

import json
import logging
import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from features.general.router import stream_agent_response
from models.domain import ChatMessage, ChatSession, Notification, User, VideoAnalysisHistory
from services.agents import smart_tutor_agent
from services.auth_platform import AuthenticatedCaller, authenticated_caller_without_holding_db
from core.runtime import session_scope
from utils.security_helpers import iter_image_tag_spans, log_safe
from utils.video_analysis.file_validation import display_source_video_label
from utils.video_analysis.markdown import resolve_report_pdf_title
from services.video_analysis_kb import (
    begin_video_analysis_tool_context,
    index_video_analysis_report,
    prefetch_video_analysis_search_context,
    sanitize_video_analysis_response,
    search_video_analysis_reports,
    video_analysis_tool_context,
)
from utils.video_analysis.chat_helpers import (
    VIDEO_ANALYSIS_NO_MATCH_REPLY,
    context_supports_query,
    video_analysis_search_is_miss,
)

# Module-level logger for the module-level helpers below. The route handlers inside
# register_video_analysis_chat_routes() shadow this with deps.logger, which is the
# app-configured logger; both write through the same logging package.
logger = logging.getLogger(__name__)


def _strip_user_id_image_tags(text: str) -> str:
    """Remove ``[IMAGE: user_id/...]`` tags (linear; former ReDoS-prone regex)."""
    if not text or "[IMAGE:" not in text:
        return text
    if "user_id/" not in text.lower():
        return text
    pieces: List[str] = []
    last = 0
    for start, end, key in iter_image_tag_spans(text):
        pieces.append(text[last:start])
        key_stripped = (key or "").strip()
        # Former pattern required user_id/ + at least one more char ([^\]]+)
        if not (
            key_stripped.lower().startswith("user_id/")
            and len(key_stripped) > len("user_id/")
        ):
            pieces.append(text[start:end])
        last = end
    pieces.append(text[last:])
    return "".join(pieces)


def _strip_user_id_markdown_images(text: str) -> str:
    """Remove ``![alt](...user_id/...)`` markdown images (linear; Sonar S5852)."""
    if not text or "user_id/" not in text.lower():
        return text
    out: List[str] = []
    i = 0
    while True:
        start = text.find("![", i)
        if start < 0:
            out.append(text[i:])
            break
        alt_end = text.find("](", start + 2)
        if alt_end < 0:
            out.append(text[i:])
            break
        url_start = alt_end + 2
        url_end = text.find(")", url_start)
        if url_end < 0:
            out.append(text[i:])
            break
        url = text[url_start:url_end]
        out.append(text[i:start])
        # Former regex required user_id/ + at least one char before ')'
        low = url.lower()
        uid = low.find("user_id/")
        if uid < 0 or uid + len("user_id/") >= len(url.rstrip()):
            out.append(text[start : url_end + 1])
        i = url_end + 1
    return "".join(out)


def _strip_invented_va_stream_chunk(text: str) -> str:
    if not text:
        return text
    text = _strip_user_id_image_tags(text)
    text = _strip_user_id_markdown_images(text)
    return text


async def stream_video_analysis_chat(
    full_prompt: str,
    history: list,
    tools: list,
    session_id: str,
    sources_ref: list,
    namespace_user_id: str,
):
    """
    Stream chat tokens and strip invented image paths during the response.

    Takes no session: ``stream_agent_response`` opens its own for the writes that finish the
    turn, so nothing is held across the model answer.
    """
    async for chunk in stream_agent_response(
        smart_tutor_agent,
        full_prompt,
        history,
        tools,
        session_id,
        sources_ref,
        chat_type="video_analysis",
        image_namespace_user_id=namespace_user_id,
    ):
        if chunk:
            yield _strip_invented_va_stream_chunk(
                chunk.decode("utf-8", errors="ignore")
            ).encode("utf-8")


VIDEO_ANALYSIS_CHAT_PROMPT = """You are a Smart Tutor assistant specialized in video analysis meeting reports.

You help trainers and admins understand content from their Teams meeting documentation (exported as PDF/DOCX).

Rules:
- A **RETRIEVED CONTEXT** block is provided below from the user's indexed reports. Treat it as your primary source.
- You may call search_video_analysis_reports again for a narrower follow-up, but never ignore non-empty RETRIEVED CONTEXT.
- Answer only from indexed report content (RETRIEVED CONTEXT and/or tool results). Do not use outside knowledge.
- If RETRIEVED CONTEXT says no matching passages were found, reply briefly in plain language that nothing related was found in the indexed reports. Do NOT include a Sources section or cite report titles.
- Never claim reports are missing or not indexed when RETRIEVED CONTEXT contains substantive Content sections.
- When you used real report content, end with a short **Sources** section listing report title(s) you used.
- Do NOT output <thinking> tags, tool names, or internal reasoning — only the final answer in markdown.
- Use markdown headings, bullets, and bold for readability.
- When context includes an **AVAILABLE IMAGES** section, you may copy those exact `[IMAGE: ...]` tags into your answer.
- Show at most 2–3 relevant screenshots per answer — never paste every frame from the report.
- NEVER invent image filenames, NEVER use `user_id` as a path prefix, and NEVER use markdown `![](...)` for images.
- Prefer showing relevant frames for visual, architecture, workflow, or UI questions when valid tags exist.
"""


async def stream_video_analysis_no_match(
    message: str,
    session_id: str,
):
    """Return a plain no-match reply without LLM call or source citations."""
    yield message.encode("utf-8")
    try:
        with session_scope() as db:
            db.add(
                ChatMessage(
                    session_id=session_id,
                    role="ai",
                    content=message,
                    sources=None,
                )
            )
            db.commit()
    except Exception as exc:
        logger.error(
            "Failed to save video analysis no-match reply: %s",
            log_safe(exc),
            exc_info=True,
        )


class VideoAnalysisChatRequest(BaseModel):
    query: str
    session_id: str
    session_name: Optional[str] = None
    history_ids: Optional[List[str]] = Field(
        default=None,
        description="Optional list of video_analysis_history IDs to scope search",
    )
    target_user_id: Optional[str] = Field(
        default=None,
        description="Super admin: Pinecone namespace owner (trainer who uploaded reports)",
    )


class VideoAnalysisSessionRename(BaseModel):
    new_name: str


def register_video_analysis_chat_routes(router: APIRouter, deps) -> None:
    logger = deps.logger

    def require_video_analysis_access(current_user):
        if current_user.role not in ["trainer", "super_admin"]:
            raise HTTPException(status_code=403, detail="Access denied.")

    def resolve_namespace_user_id(
        current_user: User,
        target_user_id: Optional[str],
        db: Session,
    ) -> str:
        if target_user_id and target_user_id != current_user.id:
            if current_user.role != "super_admin":
                raise HTTPException(
                    status_code=403,
                    detail="Only super admins can query another user's reports.",
                )
            owner = db.query(User).filter(User.id == target_user_id).first()
            if not owner:
                raise HTTPException(status_code=404, detail="Target user not found")
            return owner.id
        return current_user.id

    def validate_history_scope(
        namespace_user_id: str,
        history_ids: Optional[List[str]],
        db: Session,
    ) -> Optional[List[str]]:
        if not history_ids:
            return None
        rows = (
            db.query(VideoAnalysisHistory)
            .filter(
                VideoAnalysisHistory.id.in_(history_ids),
                VideoAnalysisHistory.user_id == namespace_user_id,
            )
            .all()
        )
        if len(rows) != len(set(history_ids)):
            raise HTTPException(
                status_code=400,
                detail="One or more report IDs are invalid for this user.",
            )
        not_indexed = [
            r.id for r in rows if getattr(r, "embedding_status", None) != "indexed"
        ]
        if not_indexed:
            raise HTTPException(
                status_code=400,
                detail="One or more selected reports are not indexed for chat.",
            )
        return history_ids

    @router.post("/index/{history_id}")
    async def index_report_for_chat(
        history_id: str,
        current_user=Depends(deps.get_current_user),
        db: Session = Depends(deps.get_db),
    ):
        """Manually generate embeddings for an existing video analysis report."""
        require_video_analysis_access(current_user)

        if current_user.role == "super_admin":
            history = (
                db.query(VideoAnalysisHistory)
                .filter(VideoAnalysisHistory.id == history_id)
                .first()
            )
        else:
            history = (
                db.query(VideoAnalysisHistory)
                .filter(
                    VideoAnalysisHistory.id == history_id,
                    VideoAnalysisHistory.user_id == current_user.id,
                )
                .first()
            )
        if not history:
            raise HTTPException(status_code=404, detail="Report not found")
        if not history.markdown_content:
            raise HTTPException(
                status_code=400,
                detail="Report has no markdown content to index.",
            )

        was_already_indexed = (
            getattr(history, "embedding_status", None) == "indexed"
        )

        # No video is pre-downloaded here: the indexer reuses frames this report or its analysis
        # session already stored in S3, and only fetches the source video if some figure still has
        # no frame. Downloading first made every reindex pay for a video it usually did not need.
        result = index_video_analysis_report(db, history)
        if not result.get("success"):
            logger.error(
                "Video analysis indexing failed for %s: %s",
                log_safe(history_id),
                log_safe(result.get("error")),
            )
            raise HTTPException(status_code=500, detail="Indexing failed")

        safe_result = {
            "chunk_count": result.get("chunk_count"),
            "source_doc_name": result.get("source_doc_name"),
            "frame_cache_synced": result.get("frame_cache_synced"),
            "frames_embedded": result.get("frames_embedded"),
            "warning": result.get("warning"),
        }

        if (
            current_user.role == "super_admin"
            and history.user_id
            and history.user_id != current_user.id
        ):
            report_title = resolve_report_pdf_title(
                getattr(history, "pdf_title", None),
                getattr(history, "markdown_content", None),
            )
            action = "reindexed" if was_already_indexed else "indexed"
            db.add(
                Notification(
                    user_id=history.user_id,
                    message=(
                        f"Super Admin {current_user.email} {action} your video analysis "
                        f'report "{report_title}" for Chat with Reports.'
                    ),
                    related_link="video-analysis",
                )
            )
            db.commit()

        return {"success": True, "data": safe_result}

    @router.get("/embeddable-reports")
    async def list_embeddable_reports(
        target_user_id: Optional[str] = None,
        current_user=Depends(deps.get_current_user),
        db: Session = Depends(deps.get_db),
    ):
        """Indexed reports only — non-indexed reports belong in View All Reports."""
        require_video_analysis_access(current_user)
        namespace_user_id = resolve_namespace_user_id(
            current_user, target_user_id, db
        )
        rows = (
            db.query(VideoAnalysisHistory)
            .filter(VideoAnalysisHistory.user_id == namespace_user_id)
            .order_by(VideoAnalysisHistory.created_at.desc())
            .all()
        )
        from services.video_analysis_kb import history_embedding_payload

        data = []
        for row in rows:
            has_export = row.pdf_s3_key or getattr(row, "docx_s3_key", None)
            if not has_export:
                continue
            if getattr(row, "embedding_status", None) != "indexed":
                continue
            pdf_title = resolve_report_pdf_title(
                row.pdf_title, getattr(row, "markdown_content", None)
            )
            payload = {
                "id": row.id,
                "pdf_title": pdf_title,
                "video_filename": display_source_video_label(
                    row.video_filename, pdf_title
                ),
                "created_at": row.created_at.isoformat(),
                "export_format": getattr(row, "export_format", None),
            }
            payload.update(history_embedding_payload(row))
            data.append(payload)
        return {"success": True, "data": data, "namespace_user_id": namespace_user_id}

    @router.post("/chat/query")
    async def video_analysis_chat_query(
        req: VideoAnalysisChatRequest,
        caller: AuthenticatedCaller = Depends(authenticated_caller_without_holding_db),
    ):
        """
        Stream a video-analysis chat answer without holding a pooled DB connection.

        Setup reads/writes run inside ``session_scope()`` and close before the stream starts.
        Holding ``Depends(get_db)`` across the model answer is what exhausted the pool in UAT
        while trainees were only trying to load modules or hear tutor audio.
        """
        # AuthenticatedCaller is a snapshot — rebuild a minimal stand-in for role checks.
        class _Caller:
            id = caller.id
            role = caller.role
            email = caller.email

        current_user = _Caller()
        require_video_analysis_access(current_user)

        with session_scope() as db:
            namespace_user_id = resolve_namespace_user_id(
                current_user, req.target_user_id, db
            )
            # Always search all indexed reports — user does not select documents in the UI.
            scoped_history_ids = None

            session = (
                db.query(ChatSession)
                .filter(
                    ChatSession.id == req.session_id,
                    ChatSession.user_id == current_user.id,
                )
                .first()
            )
            if not session:
                session = ChatSession(
                    id=req.session_id,
                    user_id=current_user.id,
                    session_name=req.session_name or f"Video Chat: {req.query[:30]}...",
                    chat_type="video_analysis",
                    kb_namespace_user_id=namespace_user_id,
                    video_history_ids=json.dumps(scoped_history_ids)
                    if scoped_history_ids
                    else None,
                )
                db.add(session)
                db.commit()
                db.refresh(session)
            else:
                session.chat_type = "video_analysis"
                session.kb_namespace_user_id = namespace_user_id
                if scoped_history_ids is not None:
                    session.video_history_ids = json.dumps(scoped_history_ids)
                db.commit()

            all_messages = session.messages
            recent = all_messages[-20:] if len(all_messages) > 20 else all_messages
            history = [
                {
                    "role": ("assistant" if m.role == "ai" else "user"),
                    "content": m.content,
                }
                for m in recent
            ]
            db.add(ChatMessage(session_id=session.id, role="user", content=req.query))
            db.commit()

            indexed_titles = [
                resolve_report_pdf_title(
                    row.pdf_title, getattr(row, "markdown_content", None)
                )
                for row in db.query(VideoAnalysisHistory)
                .filter(VideoAnalysisHistory.user_id == namespace_user_id)
                .all()
                if getattr(row, "embedding_status", None) == "indexed"
                and (row.pdf_s3_key or getattr(row, "docx_s3_key", None))
            ]
            session_id = session.id

        # Per-request, in this request's own task: the tools below run while streaming, so a shared
        # object would let a second chat overwrite the namespace this one searches. Tools open their
        # own short-lived sessions, so no session is published here.
        begin_video_analysis_tool_context(
            current_user.id, namespace_user_id, scoped_history_ids
        )

        prefetched_context = prefetch_video_analysis_search_context(
            req.query,
            namespace_user_id,
            scoped_history_ids,
            history=history,
            report_titles=indexed_titles,
        )

        has_context = (
            not video_analysis_search_is_miss(prefetched_context)
            and context_supports_query(req.query, prefetched_context)
        )
        if not has_context:
            video_analysis_tool_context.last_search_sources = []
            return StreamingResponse(
                stream_video_analysis_no_match(
                    VIDEO_ANALYSIS_NO_MATCH_REPLY,
                    session_id,
                ),
                media_type="text/plain",
            )

        context_block = (
            f"## RETRIEVED CONTEXT FROM INDEXED REPORTS\n\n{prefetched_context}"
        )

        full_prompt = (
            f"{VIDEO_ANALYSIS_CHAT_PROMPT}\n\n"
            f"{context_block}\n\n"
            f"## User query\n{req.query}"
        )
        sources_ref = []
        tools = [search_video_analysis_reports]
        return StreamingResponse(
            stream_video_analysis_chat(
                full_prompt,
                history,
                tools,
                session_id,
                sources_ref,
                namespace_user_id,
            ),
            media_type="text/plain",
        )

    @router.get("/chat/sessions")
    async def list_video_chat_sessions(
        target_user_id: Optional[str] = None,
        current_user=Depends(deps.get_current_user),
        db: Session = Depends(deps.get_db),
    ):
        require_video_analysis_access(current_user)
        q = db.query(ChatSession).filter(
            ChatSession.user_id == current_user.id,
            ChatSession.chat_type == "video_analysis",
        )
        if target_user_id and current_user.role == "super_admin":
            q = q.filter(ChatSession.kb_namespace_user_id == target_user_id)
        sessions = q.order_by(ChatSession.created_at.desc()).all()
        return {
            "success": True,
            "data": [
                {
                    "id": s.id,
                    "session_name": s.session_name,
                    "created_at": s.created_at.isoformat(),
                    "kb_namespace_user_id": getattr(s, "kb_namespace_user_id", None),
                    "video_history_ids": json.loads(s.video_history_ids)
                    if getattr(s, "video_history_ids", None)
                    else None,
                }
                for s in sessions
            ],
        }

    @router.get("/chat/sessions/{session_id}")
    async def get_video_chat_session(
        session_id: str,
        current_user=Depends(deps.get_current_user),
        db: Session = Depends(deps.get_db),
    ):
        require_video_analysis_access(current_user)
        session = (
            db.query(ChatSession)
            .filter(
                ChatSession.id == session_id,
                ChatSession.user_id == current_user.id,
                ChatSession.chat_type == "video_analysis",
            )
            .first()
        )
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        messages = []
        for m in session.messages:
            sources = None
            if m.sources:
                try:
                    sources = json.loads(m.sources)
                except json.JSONDecodeError:
                    sources = None
            messages.append(
                {
                    "role": m.role,
                    "content": m.content,
                    "timestamp": m.timestamp.isoformat(),
                    "sources": sources,
                }
            )
        return {
            "success": True,
            "data": {
                "id": session.id,
                "session_name": session.session_name,
                "messages": messages,
                "kb_namespace_user_id": getattr(session, "kb_namespace_user_id", None),
                "video_history_ids": json.loads(session.video_history_ids)
                if getattr(session, "video_history_ids", None)
                else None,
            },
        }

    @router.post("/chat/sessions/{session_id}/update")
    async def rename_video_chat_session(
        session_id: str,
        body: VideoAnalysisSessionRename,
        current_user=Depends(deps.get_current_user),
        db: Session = Depends(deps.get_db),
    ):
        require_video_analysis_access(current_user)
        session = (
            db.query(ChatSession)
            .filter(
                ChatSession.id == session_id,
                ChatSession.user_id == current_user.id,
                ChatSession.chat_type == "video_analysis",
            )
            .first()
        )
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        session.session_name = body.new_name
        db.commit()
        return {"success": True}

    @router.post("/chat/sessions/{session_id}/delete")
    async def delete_video_chat_session(
        session_id: str,
        current_user=Depends(deps.get_current_user),
        db: Session = Depends(deps.get_db),
    ):
        require_video_analysis_access(current_user)
        session = (
            db.query(ChatSession)
            .filter(
                ChatSession.id == session_id,
                ChatSession.user_id == current_user.id,
                ChatSession.chat_type == "video_analysis",
            )
            .first()
        )
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        db.query(ChatMessage).filter(ChatMessage.session_id == session_id).delete()
        db.delete(session)
        db.commit()
        return {"success": True}
