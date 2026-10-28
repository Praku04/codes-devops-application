import asyncio
import io
import os
import tempfile
import uuid
from utils.timezone_helpers import now_ist
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from utils.aws_helper import get_nova_image_description
from utils.security_helpers import GENERIC_API_ERROR_MESSAGE, log_safe
from utils.pdf_processor import (
    create_docx_from_markdown,
    create_pdf_from_markdown,
    extract_text_from_file,
)
from utils.video_processor import (
    extract_frame_at_time,
    extract_frame_with_fallback,
    normalize_video_timestamp,
)

from services.video_analysis_kb import (
    adopt_session_video_for_history,
    delete_video_analysis_report_assets,
    history_embedding_payload,
    index_video_analysis_report,
    save_history_video_to_s3,
)

from .chat_routes import register_video_analysis_chat_routes
from .dependencies import VideoAnalysisDependencies
from .file_validation import (
    archive_source_video_filename,
    build_export_download_name,
    clamp_export_filename,
    display_source_video_label,
    sanitize_upload_filename,
    validate_transcript_upload,
    validate_video_transcript_pair,
    validate_video_upload,
)
from .frame_store import ensure_local_video_path, get_frame_cache_headers, resolve_session_frame_bytes
from .markdown import normalize_video_analysis_markdown
from .markdown import resolve_report_pdf_title
from .processing import build_process_video_background, generate_pdf_title_from_content
from .session import VideoAnalysisSession, video_analysis_sessions
from .status_store import (
    create_video_analysis_status,
    get_video_analysis_status_from_db,
    hydrate_video_analysis_session_from_db,
    persist_video_analysis_status,
    save_video_analysis_markdown_to_db,
)


def create_video_analysis_router(deps: VideoAnalysisDependencies) -> APIRouter:
    router = APIRouter(prefix="/video-analysis", tags=["Video Analysis"])
    logger = deps.logger
    process_video_background = build_process_video_background(deps)

    def require_video_analysis_access(current_user):
        if current_user.role not in ["trainer", "super_admin"]:
            raise HTTPException(
                status_code=403,
                detail="Access denied. Only trainers and super admins can use this feature.",
            )

    def require_super_admin(current_user):
        if current_user.role != "super_admin":
            raise HTTPException(
                status_code=403,
                detail="Access denied. Only super admins can access this.",
            )

    def get_session_or_404(session_id: str, current_user=None) -> VideoAnalysisSession:
        if session_id not in video_analysis_sessions:
            raise HTTPException(status_code=404, detail="Session not found")
        session = video_analysis_sessions[session_id]
        if current_user is not None:
            owner_id = getattr(session, "user_id", None)
            if owner_id and owner_id != current_user.id and current_user.role != "super_admin":
                raise HTTPException(status_code=403, detail="Access denied")
        return session

    @router.post("/upload")
    async def upload_video_analysis_files(
        video: UploadFile = File(...),
        transcript: UploadFile = File(...),
        custom_instructions: Optional[str] = Form(None),
        enhanced_sequence_analysis: Optional[str] = Form("false"),
        current_user=Depends(deps.get_current_user),
        db: Session = Depends(deps.get_db),
    ):
        require_video_analysis_access(current_user)

        session = None
        try:
            video_filename = sanitize_upload_filename(video.filename, "video.mp4")
            transcript_filename = sanitize_upload_filename(transcript.filename, "transcript.pdf")

            video_content = await video.read()
            ok_video, video_error = validate_video_upload(video_filename, video_content)
            if not ok_video:
                raise HTTPException(status_code=400, detail=video_error)

            transcript_content = await transcript.read()
            ok_transcript, transcript_error = validate_transcript_upload(
                transcript_filename, transcript_content
            )
            if not ok_transcript:
                raise HTTPException(status_code=400, detail=transcript_error)

            ok_pair, pair_error = validate_video_transcript_pair(
                video_filename, transcript_filename
            )
            if not ok_pair:
                raise HTTPException(status_code=400, detail=pair_error)

            session_id = str(uuid.uuid4())
            session = VideoAnalysisSession(session_id)
            session.user_id = current_user.id
            session.video_original_filename = video_filename
            session.transcript_original_filename = transcript_filename

            video_temp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
            video_temp.write(video_content)
            video_temp.close()
            session.video_path = video_temp.name

            transcript_file = io.BytesIO(transcript_content)
            file_extension = os.path.splitext(transcript_filename)[1][1:].lower()
            session.transcript_text = extract_text_from_file(transcript_file, file_extension)

            if not session.transcript_text or session.transcript_text.startswith("Error"):
                logger.warning(
                    "Transcript parse failed for %s: %s",
                    log_safe(transcript_filename),
                    log_safe(session.transcript_text or "empty"),
                )
                session.cleanup()
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Could not read the transcript file. "
                        "Upload a valid PDF or DOCX Teams transcript."
                    ),
                )

            if len(session.transcript_text.split()) < 5:
                session.cleanup()
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Transcript appears empty or too short. "
                        "Upload a PDF/DOCX transcript that matches this meeting video."
                    ),
                )

            if custom_instructions and custom_instructions.strip():
                session.custom_instructions = custom_instructions.strip()
                # Do not log instruction text (Sonar pythonsecurity:S5145 log-injection).
                logger.info(
                    "[VIDEO ANALYSIS] Custom instructions provided (chars=%s)",
                    len(session.custom_instructions),
                )
            else:
                session.custom_instructions = None

            enhanced_flag = str(enhanced_sequence_analysis or "false").strip().lower() in {
                "1",
                "true",
                "yes",
                "on",
            }
            session.enhanced_sequence_analysis = enhanced_flag
            logger.info(
                "[VIDEO ANALYSIS] Enhanced sequence analysis mode: %s",
                session.enhanced_sequence_analysis,
            )

            session.status = "uploaded"
            session.message = "Files uploaded successfully"
            video_analysis_sessions[session_id] = session
            create_video_analysis_status(db, session_id, current_user.id)
            persist_video_analysis_status(session)

            deps.log_activity(
                db,
                current_user.id,
                "VIDEO_ANALYSIS_UPLOAD",
                (
                    f"Uploaded video '{log_safe(video_filename)}' "
                    f"and transcript '{log_safe(transcript_filename)}'"
                ),
            )

            return {
                "session_id": session_id,
                "status": "success",
                "message": "Files uploaded successfully",
                "video_filename": video_filename,
                "transcript_filename": transcript_filename,
                "transcript_word_count": len(session.transcript_text.split()),
                "has_custom_instructions": session.custom_instructions is not None,
                "enhanced_sequence_analysis": session.enhanced_sequence_analysis,
            }
        except HTTPException:
            if session is not None and session.session_id not in video_analysis_sessions:
                try:
                    session.cleanup()
                except Exception as cleanup_exc:
                    logger.debug(
                        "Upload validation cleanup skipped: %s",
                        log_safe(cleanup_exc),
                    )
            raise
        except Exception as exc:
            if session is not None and session.session_id not in video_analysis_sessions:
                try:
                    session.cleanup()
                except Exception as cleanup_exc:
                    logger.debug(
                        "Upload error cleanup skipped: %s",
                        log_safe(cleanup_exc),
                    )
            logger.error("Error uploading video analysis files: %s", log_safe(exc), exc_info=True)
            raise HTTPException(status_code=500, detail=GENERIC_API_ERROR_MESSAGE)

    @router.post("/process/{session_id}")
    async def process_video_analysis(
        session_id: str,
        background_tasks: BackgroundTasks,
        current_user=Depends(deps.get_current_user),
        db: Session = Depends(deps.get_db),
    ):
        require_video_analysis_access(current_user)
        get_session_or_404(session_id, current_user)

        background_tasks.add_task(process_video_background, session_id, current_user.id, db)
        return {
            "session_id": session_id,
            "status": "processing",
            "message": "Processing started in background",
        }

    @router.get("/status/{session_id}")
    async def get_video_analysis_status(
        session_id: str,
        current_user=Depends(deps.get_current_user),
        db: Session = Depends(deps.get_db),
    ):
        require_video_analysis_access(current_user)

        if session_id in video_analysis_sessions:
            session = video_analysis_sessions[session_id]
            owner_id = getattr(session, "user_id", None)
            if (
                owner_id
                and owner_id != current_user.id
                and current_user.role != "super_admin"
            ):
                raise HTTPException(status_code=403, detail="Access denied")
            return {
                "session_id": session_id,
                "status": session.status,
                "progress": session.progress,
                "message": session.message,
                "markdown": session.generated_markdown if session.status == "completed" else "",
                "word_count": len(session.generated_markdown.split()) if session.generated_markdown else 0,
            }

        db_status = get_video_analysis_status_from_db(
            db,
            session_id,
            current_user.id,
            allow_super_admin=True,
            is_super_admin=current_user.role == "super_admin",
        )
        if db_status:
            return db_status

        raise HTTPException(status_code=404, detail="Session not found")

    @router.post("/cancel/{session_id}")
    async def cancel_video_analysis(
        session_id: str,
        current_user=Depends(deps.get_current_user),
        db: Session = Depends(deps.get_db),
    ):
        require_video_analysis_access(current_user)
        if session_id in video_analysis_sessions:
            session = get_session_or_404(session_id, current_user)
            session.cancelled = True
            if session.status in {"processing", "uploaded"}:
                session.status = "cancelled"
                session.message = "Cancelled by user"
            persist_video_analysis_status(session)
        else:
            db_status = get_video_analysis_status_from_db(
                db,
                session_id,
                current_user.id,
                allow_super_admin=True,
                is_super_admin=current_user.role == "super_admin",
            )
            if not db_status:
                return {
                    "session_id": session_id,
                    "status": "cancelled",
                    "message": "Session already closed",
                }
            if db_status["status"] in {"processing", "uploaded"}:
                session = VideoAnalysisSession(session_id)
                session.user_id = current_user.id
                session.status = "cancelled"
                session.message = "Cancelled by user"
                session.cancelled = True
                session.progress = db_status.get("progress", 0)
                persist_video_analysis_status(session)
        return {
            "session_id": session_id,
            "status": "cancelled",
            "message": "Cancellation requested",
        }

    def _resolve_mutable_session(
        session_id: str,
        current_user,
        db: Session,
    ) -> VideoAnalysisSession:
        if session_id in video_analysis_sessions:
            session = get_session_or_404(session_id, current_user)
            # Also for a live session: the first export deletes the local video, so a second export
            # would otherwise build its figures with no frame source at all.
            ensure_local_video_path(session)
            return session

        session = hydrate_video_analysis_session_from_db(
            db,
            session_id,
            current_user.id,
            is_super_admin=current_user.role == "super_admin",
        )
        if not session:
            raise HTTPException(status_code=404, detail="Session not found")
        ensure_local_video_path(session)
        return session

    @router.post("/update-markdown/{session_id}")
    async def update_video_analysis_markdown(
        session_id: str,
        markdown: str = Form(...),
        current_user=Depends(deps.get_current_user),
        db: Session = Depends(deps.get_db),
    ):
        require_video_analysis_access(current_user)
        normalized_md = normalize_video_analysis_markdown(markdown)
        saved = save_video_analysis_markdown_to_db(
            db,
            session_id,
            current_user.id,
            normalized_md,
            is_super_admin=current_user.role == "super_admin",
        )
        if not saved:
            raise HTTPException(status_code=404, detail="Session not found")

        if session_id in video_analysis_sessions:
            session = get_session_or_404(session_id, current_user)
            session.generated_markdown = normalized_md

        return {"status": "success", "message": "Markdown updated and normalized"}

    def _build_export_response(
        file_content: bytes,
        media_type: str,
        filename: str,
        extra_headers: Optional[dict] = None,
        file_ext: str = "pdf",
    ):
        safe_name = clamp_export_filename(filename, file_ext=file_ext)
        headers = {"Content-Disposition": f'attachment; filename="{safe_name}"'}
        if extra_headers:
            headers.update(extra_headers)
        return StreamingResponse(
            io.BytesIO(file_content),
            media_type=media_type,
            headers=headers,
        )

    @router.post("/generate-report/{session_id}")
    async def generate_video_analysis_report(
        session_id: str,
        export_format: str = Query("pdf", alias="format", pattern="^(pdf|docx)$"),
        current_user=Depends(deps.get_current_user),
        db: Session = Depends(deps.get_db),
    ):
        require_video_analysis_access(current_user)
        session = _resolve_mutable_session(session_id, current_user, db)

        if not session.generated_markdown:
            raise HTTPException(status_code=400, detail="No markdown content available")

        export_format = export_format.lower().strip()
        try:
            normalized_markdown = normalize_video_analysis_markdown(session.generated_markdown)
            session.generated_markdown = normalized_markdown
            report_title = generate_pdf_title_from_content(normalized_markdown, deps)

            buffer = io.BytesIO()
            if export_format == "docx":
                success = create_docx_from_markdown(
                    buffer,
                    normalized_markdown,
                    session.video_path,
                )
                if not success:
                    raise HTTPException(status_code=500, detail="Failed to generate DOCX")
                content_type = (
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                )
                file_ext = "docx"
                activity_action = "VIDEO_ANALYSIS_DOCX"
            else:
                success = create_pdf_from_markdown(
                    buffer,
                    normalized_markdown,
                    session.video_path,
                )
                if not success:
                    raise HTTPException(status_code=500, detail="Failed to generate PDF")
                content_type = "application/pdf"
                file_ext = "pdf"
                activity_action = "VIDEO_ANALYSIS_PDF"

            buffer.seek(0)
            file_content = buffer.read()

            timestamp = now_ist().strftime("%Y%m%d_%H%M%S")
            original_video_name = getattr(session, "video_original_filename", None) or ""
            original_transcript_name = (
                getattr(session, "transcript_original_filename", None) or ""
            )
            stored_video_label = archive_source_video_filename(
                original_video_name, report_title
            )
            download_basename = build_export_download_name(
                video_filename=original_video_name or stored_video_label,
                report_title=report_title,
                file_ext=file_ext,
                timestamp=timestamp,
            )
            download_basename = clamp_export_filename(download_basename, file_ext=file_ext)
            # S3 object key stays short and unique even if display name is truncated
            s3_key = f"video-analysis/{current_user.id}/{timestamp}_{session_id[:8]}.{file_ext}"

            try:
                deps.s3_client.put_object(
                    Bucket=deps.s3_bucket_name,
                    Key=s3_key,
                    Body=file_content,
                    ContentType=content_type,
                )
                # Avoid logging S3 key path (may include user-derived segments; S5145).
                logger.info("%s saved to S3 successfully", file_ext.upper())
            except Exception as s3_error:
                logger.error(
                    "Error saving %s to S3: %s",
                    file_ext.upper(),
                    log_safe(s3_error),
                )

            history_kwargs = {
                "user_id": current_user.id,
                "session_id": session_id,
                "video_filename": stored_video_label,
                "transcript_filename": original_transcript_name or "transcript",
                "pdf_title": (report_title or "Meeting Documentation")[:200],
                "markdown_content": normalized_markdown,
                "export_format": export_format,
            }
            if export_format == "docx":
                history_kwargs["docx_s3_key"] = s3_key
            else:
                history_kwargs["pdf_s3_key"] = s3_key

            history_entry = deps.history_model(**history_kwargs)
            db.add(history_entry)
            db.commit()
            db.refresh(history_entry)

            # Give the report its own source video so Reindex keeps working later. The local copy is
            # deleted after the first export (and is absent entirely when the export is served by a
            # different worker than the one that processed the video), so fall back to the copy the
            # analysis worker put in S3 instead of leaving video_s3_key empty.
            if session.video_path and os.path.isfile(session.video_path):
                video_key = save_history_video_to_s3(
                    current_user.id, history_entry.id, session.video_path
                )
            else:
                video_key = adopt_session_video_for_history(
                    current_user.id,
                    history_entry.id,
                    getattr(session, "video_s3_key", None) or "",
                )
            if video_key:
                history_entry.video_s3_key = video_key
                db.commit()

            index_meta = {
                "status": "pending",
                "frames_embedded": "0",
                "chunk_count": "0",
                "warning": "",
            }
            try:
                index_result = index_video_analysis_report(
                    db,
                    history_entry,
                    video_path=session.video_path,
                    frame_paths=getattr(session, "extracted_frames", None) or None,
                )
                if index_result.get("success"):
                    index_meta["status"] = "indexed"
                    index_meta["frames_embedded"] = str(
                        index_result.get("frames_embedded", 0)
                    )
                    index_meta["chunk_count"] = str(index_result.get("chunk_count", 0))
                    index_meta["warning"] = index_result.get("warning") or ""
                else:
                    index_meta["status"] = "failed"
                    index_meta["warning"] = index_result.get("error") or "Indexing failed"
            except Exception as index_exc:
                index_meta["status"] = "failed"
                index_meta["warning"] = "Indexing failed"
                logger.warning(
                    "Auto-index after export failed (report still saved): %s",
                    log_safe(index_exc),
                )

            deps.log_activity(
                db,
                current_user.id,
                activity_action,
                f"Generated {file_ext.upper()}: {log_safe(report_title)}",
            )

            # Clear local source video/transcript working data after SOP export
            try:
                if session.video_path and os.path.isfile(session.video_path):
                    os.remove(session.video_path)
                session.video_path = None
                session.transcript_text = ""
                session.extracted_frames = {}
            except OSError as cleanup_exc:
                logger.warning(
                    "Post-export local cleanup warning: %s",
                    log_safe(cleanup_exc),
                )

            return _build_export_response(
                file_content,
                content_type,
                download_basename,
                file_ext=file_ext,
                extra_headers={
                    "X-VA-Index-Status": index_meta["status"],
                    "X-VA-Frames-Embedded": index_meta["frames_embedded"],
                    "X-VA-Chunk-Count": index_meta["chunk_count"],
                    "X-VA-Index-Warning": (index_meta["warning"] or "")[:500],
                },
            )
        except HTTPException:
            raise
        except Exception as exc:
            logger.error(
                "Error generating %s: %s",
                export_format.upper(),
                log_safe(exc),
                exc_info=True,
            )
            raise HTTPException(status_code=500, detail=GENERIC_API_ERROR_MESSAGE)

    @router.post("/generate-pdf/{session_id}")
    async def generate_video_analysis_pdf(
        session_id: str,
        current_user=Depends(deps.get_current_user),
        db: Session = Depends(deps.get_db),
    ):
        """Backward-compatible alias for PDF export."""
        return await generate_video_analysis_report(
            session_id,
            export_format="pdf",
            current_user=current_user,
            db=db,
        )

    @router.post("/session/{session_id}/delete")
    async def delete_video_analysis_session(
        session_id: str,
        current_user=Depends(deps.get_current_user),
    ):
        require_video_analysis_access(current_user)
        if session_id not in video_analysis_sessions:
            return {"status": "success", "message": "Session already closed"}
        session = get_session_or_404(session_id, current_user)
        try:
            session.cleanup()
        except Exception as exc:
            logger.error(
                "Error cleaning up session %s: %s",
                log_safe(session.session_id),
                log_safe(exc),
            )
        del video_analysis_sessions[session_id]
        return {"status": "success", "message": "Session deleted"}

    def _normalize_timestamp(ts: str) -> str:
        ts = (ts or "").strip()
        parts = ts.split(":")
        try:
            if len(parts) == 2:
                return f"{int(parts[0]):02d}:{int(parts[1]):02d}"
            if len(parts) == 3:
                return f"{int(parts[0]):02d}:{int(parts[1]):02d}:{int(parts[2]):02d}"
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid timestamp format") from exc
        raise HTTPException(status_code=400, detail="Invalid timestamp format")

    def _validated_frame_path(frame_path: str, base_dir: str) -> str:
        if not frame_path or not base_dir:
            raise HTTPException(status_code=404, detail="Frame not found")
        base = os.path.realpath(base_dir)
        full = os.path.realpath(frame_path)
        if full != base and not full.startswith(base + os.sep):
            raise HTTPException(status_code=404, detail="Frame not found")
        if not os.path.isfile(full):
            raise HTTPException(status_code=404, detail="Frame not found")
        return full

    def _cache_validated_frame_path(
        session: VideoAnalysisSession,
        normalized_ts: str,
        frame_path: str,
    ) -> Optional[str]:
        if not frame_path or not session.temp_dir:
            return None
        try:
            safe_path = _validated_frame_path(frame_path, session.temp_dir)
        except HTTPException:
            return None
        if os.path.getsize(safe_path) <= 0:
            return None
        extracted = getattr(session, "extracted_frames", None) or {}
        extracted[normalized_ts] = safe_path
        session.extracted_frames = extracted
        return safe_path

    def _resolve_frame_path(session: VideoAnalysisSession, normalized_ts: str, raw_ts: str) -> Optional[str]:
        extracted = getattr(session, "extracted_frames", None) or {}
        candidates = []
        for candidate in (extracted.get(normalized_ts), extracted.get(raw_ts)):
            if candidate and candidate not in candidates:
                candidates.append(candidate)
        for key, path in extracted.items():
            if normalize_video_timestamp(key) == normalized_ts and path not in candidates:
                candidates.append(path)

        for candidate in candidates:
            safe_path = _cache_validated_frame_path(session, normalized_ts, candidate)
            if safe_path:
                return safe_path

        if not session.video_path or not os.path.isfile(session.video_path):
            return None

        frame_path = extract_frame_with_fallback(
            session.video_path,
            normalized_ts,
            session.temp_dir,
            crop_participants=False,
        )
        return _cache_validated_frame_path(session, normalized_ts, frame_path or "")

    @router.get("/frame/{session_id}/{timestamp}")
    async def get_video_frame(
        session_id: str,
        timestamp: str,
        current_user=Depends(deps.get_current_user),
        db: Session = Depends(deps.get_db),
    ):
        require_video_analysis_access(current_user)

        try:
            normalized_ts = _normalize_timestamp(timestamp)
            frame_bytes = await asyncio.to_thread(
                resolve_session_frame_bytes,
                db,
                session_id,
                current_user.id,
                normalized_ts,
                is_super_admin=current_user.role == "super_admin",
            )
            if not frame_bytes:
                raise HTTPException(status_code=404, detail="Frame not found")
            return StreamingResponse(
                io.BytesIO(frame_bytes),
                media_type="image/jpeg",
                headers=get_frame_cache_headers(),
            )
        except HTTPException:
            raise
        except Exception as exc:
            logger.error("Error getting frame: %s", log_safe(exc))
            raise HTTPException(status_code=500, detail="Failed to get frame")

    @router.post("/extract-manual-frame/{session_id}")
    async def extract_manual_frame(
        session_id: str,
        timestamp: str = Form(...),
        current_user=Depends(deps.get_current_user),
        db: Session = Depends(deps.get_db),
    ):
        require_video_analysis_access(current_user)
        session = get_session_or_404(session_id, current_user)

        # Validate the form value up front (same gate as GET /frame) so a malformed
        # timestamp returns 400 instead of a generic "could not extract" 404.
        normalized_ts = _normalize_timestamp(timestamp)

        try:
            frame_path = extract_frame_at_time(
                session.video_path, normalized_ts, session.temp_dir
            )
            if not frame_path:
                raise HTTPException(status_code=404, detail="Could not extract frame")

            description = get_nova_image_description(
                frame_path,
                session.transcript_text[:2000],
                session.transcript_text,
                1,
                normalized_ts,
            )

            # Log/echo the normalized value, never the raw form input (Sonar S5145).
            deps.log_activity(
                db,
                current_user.id,
                "VIDEO_ANALYSIS_MANUAL_FRAME",
                f"Extracted frame at {normalized_ts}",
            )

            return {
                "success": True,
                "timestamp": normalized_ts,
                "description": description,
            }
        except HTTPException:
            raise
        except Exception as exc:
            logger.error("Error extracting manual frame: %s", log_safe(exc), exc_info=True)
            raise HTTPException(status_code=500, detail=GENERIC_API_ERROR_MESSAGE)

    @router.get("/history")
    async def get_video_analysis_history(
        current_user=Depends(deps.get_current_user),
        db: Session = Depends(deps.get_db),
    ):
        require_video_analysis_access(current_user)

        try:
            history = (
                db.query(deps.history_model)
                .filter(deps.history_model.user_id == current_user.id)
                .order_by(deps.history_model.created_at.desc())
                .all()
            )

            logger.debug(
                "Found %s video analysis history entries for user %s",
                len(history),
                log_safe(current_user.id),
            )

            result_data = []
            for history_row in history:
                try:
                    pdf_title = resolve_report_pdf_title(
                        getattr(history_row, "pdf_title", None),
                        getattr(history_row, "markdown_content", None),
                    )
                except Exception as exc:
                    logger.warning("Error accessing pdf_title: %s", log_safe(exc))
                    pdf_title = "Meeting Documentation"

                export_fmt = getattr(history_row, "export_format", None) or (
                    "docx" if getattr(history_row, "docx_s3_key", None) else "pdf"
                )
                row_payload = {
                    "id": history_row.id,
                    "session_id": history_row.session_id,
                    "video_filename": display_source_video_label(
                        history_row.video_filename, pdf_title
                    ),
                    "transcript_filename": history_row.transcript_filename,
                    "pdf_title": pdf_title,
                    "pdf_s3_key": history_row.pdf_s3_key,
                    "docx_s3_key": getattr(history_row, "docx_s3_key", None),
                    "export_format": export_fmt,
                    "created_at": history_row.created_at.isoformat(),
                    "has_pdf": history_row.pdf_s3_key is not None,
                    "has_docx": getattr(history_row, "docx_s3_key", None) is not None,
                    "has_report": (
                        history_row.pdf_s3_key is not None
                        or getattr(history_row, "docx_s3_key", None) is not None
                    ),
                }
                row_payload.update(history_embedding_payload(history_row))
                result_data.append(row_payload)

            logger.debug("Returning %s history entries", len(result_data))
            return {"success": True, "data": result_data}
        except Exception as exc:
            logger.error("Error getting video analysis history: %s", log_safe(exc), exc_info=True)
            raise HTTPException(status_code=500, detail=GENERIC_API_ERROR_MESSAGE)

    @router.get("/view-pdf/{history_id}")
    async def view_video_analysis_pdf(
        history_id: str,
        current_user=Depends(deps.get_current_user),
        db: Session = Depends(deps.get_db),
    ):
        require_video_analysis_access(current_user)

        try:
            if current_user.role == "super_admin":
                history = (
                    db.query(deps.history_model)
                    .filter(deps.history_model.id == history_id)
                    .first()
                )
            else:
                history = (
                    db.query(deps.history_model)
                    .filter(
                        deps.history_model.id == history_id,
                        deps.history_model.user_id == current_user.id,
                    )
                    .first()
                )

            if not history:
                logger.warning("[VIDEO ANALYSIS] History entry not found")
                raise HTTPException(status_code=404, detail="History entry not found")

            if not history.pdf_s3_key:
                logger.warning(
                    "[VIDEO ANALYSIS] PDF not found in S3 for history %s",
                    log_safe(history_id),
                )
                raise HTTPException(status_code=404, detail="PDF not found in S3")

            logger.info(
                "[VIDEO ANALYSIS] Retrieving PDF from S3: %s",
                log_safe(history.pdf_s3_key),
            )
            try:
                response = deps.s3_client.get_object(
                    Bucket=deps.s3_bucket_name,
                    Key=history.pdf_s3_key,
                )
                pdf_content = response["Body"].read()
                logger.info(
                    "[VIDEO ANALYSIS] Successfully retrieved PDF: %s bytes",
                    len(pdf_content),
                )
                return StreamingResponse(
                    io.BytesIO(pdf_content),
                    media_type="application/pdf",
                    headers={
                        "Content-Disposition": (
                            "inline; filename=\""
                            f"{clamp_export_filename(os.path.basename(history.pdf_s3_key), 'pdf')}\""
                        )
                    },
                )
            except Exception as s3_error:
                logger.error(
                    "[VIDEO ANALYSIS] Error retrieving PDF from S3: %s",
                    log_safe(s3_error),
                    exc_info=True,
                )
                raise HTTPException(status_code=404, detail="PDF not found in S3")
        except HTTPException:
            raise
        except Exception as exc:
            logger.error("[VIDEO ANALYSIS] Error viewing PDF: %s", log_safe(exc), exc_info=True)
            raise HTTPException(status_code=500, detail=GENERIC_API_ERROR_MESSAGE)

    @router.get("/view-docx/{history_id}")
    async def view_video_analysis_docx(
        history_id: str,
        current_user=Depends(deps.get_current_user),
        db: Session = Depends(deps.get_db),
    ):
        require_video_analysis_access(current_user)

        try:
            if current_user.role == "super_admin":
                history = (
                    db.query(deps.history_model)
                    .filter(deps.history_model.id == history_id)
                    .first()
                )
            else:
                history = (
                    db.query(deps.history_model)
                    .filter(
                        deps.history_model.id == history_id,
                        deps.history_model.user_id == current_user.id,
                    )
                    .first()
                )

            if not history:
                raise HTTPException(status_code=404, detail="History entry not found")

            docx_key = getattr(history, "docx_s3_key", None)
            if not docx_key:
                raise HTTPException(status_code=404, detail="DOCX not found in S3")

            response = deps.s3_client.get_object(
                Bucket=deps.s3_bucket_name,
                Key=docx_key,
            )
            docx_content = response["Body"].read()
            return StreamingResponse(
                io.BytesIO(docx_content),
                media_type=(
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                ),
                headers={
                    "Content-Disposition": (
                        "inline; filename=\""
                        f"{clamp_export_filename(os.path.basename(docx_key), 'docx')}\""
                    )
                },
            )
        except HTTPException:
            raise
        except Exception as exc:
            logger.error("[VIDEO ANALYSIS] Error viewing DOCX: %s", log_safe(exc), exc_info=True)
            raise HTTPException(status_code=500, detail="Failed to view DOCX")

    @router.post("/delete/{history_id}/delete")
    async def delete_video_analysis_report(
        history_id: str,
        current_user=Depends(deps.get_current_user),
        db: Session = Depends(deps.get_db),
    ):
        require_video_analysis_access(current_user)

        try:
            if current_user.role == "super_admin":
                history = (
                    db.query(deps.history_model)
                    .filter(deps.history_model.id == history_id)
                    .first()
                )
            else:
                history = (
                    db.query(deps.history_model)
                    .filter(
                        deps.history_model.id == history_id,
                        deps.history_model.user_id == current_user.id,
                    )
                    .first()
                )

            if not history:
                logger.warning(
                    f"[VIDEO ANALYSIS] History entry not found for deletion: {history_id}"
                )
                raise HTTPException(status_code=404, detail="History entry not found")

            report_owner = (
                db.query(deps.user_model)
                .filter(deps.user_model.id == history.user_id)
                .first()
            )
            pdf_title = resolve_report_pdf_title(
                getattr(history, "pdf_title", None),
                getattr(history, "markdown_content", None),
            )

            for s3_key, label in (
                (history.pdf_s3_key, "PDF"),
                (getattr(history, "docx_s3_key", None), "DOCX"),
            ):
                if not s3_key:
                    continue
                try:
                    deps.s3_client.delete_object(
                        Bucket=deps.s3_bucket_name,
                        Key=s3_key,
                    )
                    logger.info(
                        "[VIDEO ANALYSIS] Deleted %s from S3: %s",
                        label,
                        str(s3_key).replace("\r", "").replace("\n", ""),
                    )
                except Exception as s3_error:
                    logger.warning(
                        "[VIDEO ANALYSIS] Error deleting %s from S3: %s",
                        label,
                        log_safe(s3_error),
                    )

            try:
                delete_video_analysis_report_assets(history.user_id, history)
            except Exception as vec_exc:
                logger.warning(
                    "[VIDEO ANALYSIS] Failed to delete report assets: %s",
                    log_safe(vec_exc),
                )

            db.delete(history)
            db.commit()
            logger.info(
                "[VIDEO ANALYSIS] Deleted video analysis report: %s",
                log_safe(history_id),
            )

            if current_user.role == "super_admin" and report_owner and report_owner.id != current_user.id:
                try:
                    notification = deps.notification_model(
                        id=str(uuid.uuid4()),
                        user_id=report_owner.id,
                        message=f'Your video analysis report "{pdf_title}" has been deleted by admin {current_user.email}',
                        is_read=False,
                        created_at=now_ist(),
                    )
                    db.add(notification)
                    db.commit()
                    logger.info(
                        "[VIDEO ANALYSIS] Notification sent to report owner about admin delete"
                    )
                except Exception as notif_error:
                    logger.warning(
                        "[VIDEO ANALYSIS] Error sending notification: %s",
                        log_safe(notif_error),
                    )

            return {"success": True, "message": "Report deleted successfully"}
        except HTTPException:
            raise
        except Exception as exc:
            logger.error(
                "[VIDEO ANALYSIS] Error deleting report: %s",
                log_safe(exc),
                exc_info=True,
            )
            db.rollback()
            raise HTTPException(status_code=500, detail=GENERIC_API_ERROR_MESSAGE)

    @router.get("/all-reports")
    async def get_all_video_analysis_reports(
        process_filter: Optional[str] = Query(None),
        role_filter: Optional[str] = Query(None),
        user_filter: Optional[str] = Query(None),
        current_user=Depends(deps.get_current_user),
        db: Session = Depends(deps.get_db),
    ):
        if current_user.role != "super_admin":
            raise HTTPException(
                status_code=403,
                detail="Access denied. Only super admins can view all reports.",
            )

        try:
            # Log filter presence only — not raw query values (S5145).
            logger.info(
                "[VIDEO ANALYSIS] Fetching reports with filters: process=%s role=%s user=%s",
                "set" if process_filter else "none",
                "set" if role_filter else "none",
                "set" if user_filter else "none",
            )

            query = db.query(deps.history_model).join(deps.user_model)
            if process_filter:
                query = query.filter(deps.user_model.process_id == process_filter)
            if role_filter:
                query = query.filter(deps.user_model.role == role_filter)
            if user_filter:
                query = query.filter(deps.history_model.user_id == user_filter)

            history_rows = query.order_by(deps.history_model.created_at.desc()).all()
            logger.info(
                "[VIDEO ANALYSIS] Found %s reports for super admin",
                len(history_rows),
            )

            # Resolve every report's author once, with their process, instead of two round-trips
            # per report: this tab lists every report in the system, so the per-row lookup made
            # the request grow with the report count.
            from sqlalchemy.orm import joinedload

            users_by_id = {}
            report_user_ids = {r.user_id for r in history_rows if r.user_id}
            if report_user_ids:
                users_by_id = {
                    row.id: row
                    for row in db.query(deps.user_model)
                    .options(joinedload(deps.user_model.process))
                    .filter(deps.user_model.id.in_(report_user_ids))
                    .all()
                }

            result_data = []
            for history_row in history_rows:
                try:
                    user = users_by_id.get(history_row.user_id)
                    if not user:
                        logger.warning(
                            "[VIDEO ANALYSIS] User %s not found for report %s",
                            log_safe(history_row.user_id),
                            log_safe(history_row.id),
                        )
                        continue

                    process_name = user.process.name if user.process else "N/A"
                    pdf_title = resolve_report_pdf_title(
                        getattr(history_row, "pdf_title", None),
                        getattr(history_row, "markdown_content", None),
                    )
                    export_fmt = getattr(history_row, "export_format", None) or (
                        "docx" if getattr(history_row, "docx_s3_key", None) else "pdf"
                    )
                    admin_row = {
                        "id": history_row.id,
                        "session_id": history_row.session_id,
                        "video_filename": display_source_video_label(
                            history_row.video_filename, pdf_title
                        ),
                        "transcript_filename": history_row.transcript_filename,
                        "pdf_title": pdf_title,
                        "pdf_s3_key": history_row.pdf_s3_key,
                        "docx_s3_key": getattr(history_row, "docx_s3_key", None),
                        "export_format": export_fmt,
                        "created_at": history_row.created_at.isoformat(),
                        "has_pdf": history_row.pdf_s3_key is not None,
                        "has_docx": getattr(history_row, "docx_s3_key", None) is not None,
                        "has_report": (
                            history_row.pdf_s3_key is not None
                            or getattr(history_row, "docx_s3_key", None) is not None
                        ),
                        "user_id": history_row.user_id,
                        "user_email": user.email,
                        "user_role": user.role,
                        "process_name": process_name,
                        "process_id": user.process_id,
                    }
                    admin_row.update(history_embedding_payload(history_row))
                    result_data.append(admin_row)
                except Exception as exc:
                    logger.warning(
                        "[VIDEO ANALYSIS] Error processing report %s: %s",
                        log_safe(history_row.id),
                        log_safe(exc),
                    )
                    continue

            logger.info("[VIDEO ANALYSIS] Returning %s reports", len(result_data))
            return {"success": True, "data": result_data}
        except Exception as exc:
            logger.error(
                "[VIDEO ANALYSIS] Error getting all reports: %s",
                log_safe(exc),
                exc_info=True,
            )
            raise HTTPException(status_code=500, detail=GENERIC_API_ERROR_MESSAGE)

    @router.get("/filter-options")
    async def get_video_analysis_filter_options(
        current_user=Depends(deps.get_current_user),
        db: Session = Depends(deps.get_db),
    ):
        require_super_admin(current_user)

        try:
            logger.info("[VIDEO ANALYSIS] Fetching filter options for super admin")

            all_processes = db.query(deps.process_model).all()
            logger.info("[VIDEO ANALYSIS] Found %s total processes", len(all_processes))

            users_with_reports = db.query(deps.user_model).join(deps.history_model).distinct().all()
            logger.info("[VIDEO ANALYSIS] Found %s users with reports", len(users_with_reports))

            process_options = [{"id": process.id, "name": process.name} for process in all_processes]
            user_options = [
                {
                    "id": user.id,
                    "email": user.email,
                    "role": user.role,
                    "process_id": user.process_id,
                }
                for user in users_with_reports
            ]

            logger.info(
                "[VIDEO ANALYSIS] Returning filter options: %s processes, %s users",
                len(process_options),
                len(user_options),
            )
            return {"success": True, "data": {"processes": process_options, "users": user_options}}
        except Exception as exc:
            logger.error(
                "[VIDEO ANALYSIS] Error getting filter options: %s",
                log_safe(exc),
                exc_info=True,
            )
            return {"success": True, "data": {"processes": [], "users": []}}

    @router.get("/process-stats")
    async def get_video_analysis_process_stats(
        current_user=Depends(deps.get_current_user),
        db: Session = Depends(deps.get_db),
    ):
        require_super_admin(current_user)

        try:
            logger.info("[VIDEO ANALYSIS] Fetching process stats for super admin")

            from sqlalchemy import func

            process_stats = (
                db.query(
                    deps.process_model.id,
                    deps.process_model.name,
                    func.count(deps.history_model.id).label("report_count"),
                )
                .outerjoin(deps.user_model, deps.process_model.id == deps.user_model.process_id)
                .outerjoin(deps.history_model, deps.user_model.id == deps.history_model.user_id)
                .group_by(deps.process_model.id, deps.process_model.name)
                .all()
            )

            stats = [
                {
                    "process_id": stat[0],
                    "process_name": stat[1],
                    "report_count": stat[2] or 0,
                }
                for stat in process_stats
            ]

            logger.info(
                "[VIDEO ANALYSIS] Process stats: %s processes with report counts",
                len(stats),
            )
            for stat in stats:
                logger.info(
                    "[VIDEO ANALYSIS]   - %s: %s reports",
                    stat["process_name"],
                    stat["report_count"],
                )

            return {"success": True, "data": stats}
        except Exception as exc:
            logger.error(
                "[VIDEO ANALYSIS] Error getting process stats: %s",
                log_safe(exc),
                exc_info=True,
            )
            raise HTTPException(status_code=500, detail=GENERIC_API_ERROR_MESSAGE)

    register_video_analysis_chat_routes(router, deps)
    return router
