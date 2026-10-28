import os
import re
import subprocess
from collections import defaultdict
from typing import List, Optional

from utils.aws_helper import (
    analyze_transcript_structure,
    batch_process_images_parallel,
    extract_visual_cue_timestamps,
    _strip_bullet_toc_markdown_links,
    _strip_numbered_toc_markdown_links,
)
from utils.security_helpers import log_safe
from utils.video_processor import (
    compute_perceptual_hash,
    extract_frame_at_time,
    extract_frame_with_fallback,
    get_video_duration,
    normalize_video_timestamp,
)

from .dependencies import VideoAnalysisDependencies
from .frame_store import register_session_frame_s3, upload_session_video_file
from .session import VideoAnalysisSession, video_analysis_sessions
from .status_store import persist_video_analysis_status


class VideoAnalysisCancelledError(Exception):
    """Raised when the user cancels an in-progress video analysis session."""


_FIGURE_TIMESTAMP_PATTERN = re.compile(
    r"(?:\*\*Timestamp:\*\*\s*`(\d{1,2}:\d{2})`"
    r"|\*\*Figure\s+\d+\*\*\s*\|\s*Timestamp:\s*(\d{1,2}:\d{2}))"
)


def _try_normalize_figure_heading_line(line: str) -> Optional[str]:
    """
    ``#{3,6} figure[-:]? N`` → ``### Figure N`` (linear; Sonar S2631).
    Returns None if the line is not a figure heading.
    """
    s = line or ""
    n = len(s)
    i = 0
    while i < n and s[i].isspace():
        i += 1
    hash_start = i
    while i < n and (i - hash_start) < 6 and s[i] == "#":
        i += 1
    level = i - hash_start
    if level < 3 or level > 6:
        return None
    if i < n and s[i] == "#":
        return None
    while i < n and s[i].isspace():
        i += 1
    if not s.lower().startswith("figure", i):
        return None
    i += 6
    if i < n and s[i] in "-:":
        i += 1
    while i < n and s[i].isspace():
        i += 1
    if i >= n or not s[i].isdigit():
        return None
    num_start = i
    while i < n and s[i].isdigit():
        i += 1
    num = int(s[num_start:i])
    while i < n and s[i].isspace():
        i += 1
    if i != n:
        return None
    return f"### Figure {num}"


def _try_normalize_step_heading_line(line: str) -> Optional[str]:
    """
    ``#{3,6} Step N: title`` → ``### Step NN: title`` (linear; Sonar S2631).
    Returns None if the line is not a step heading.
    """
    s = line or ""
    n = len(s)
    i = 0
    while i < n and s[i].isspace():
        i += 1
    hash_start = i
    while i < n and (i - hash_start) < 6 and s[i] == "#":
        i += 1
    level = i - hash_start
    if level < 3 or level > 6:
        return None
    if i < n and s[i] == "#":
        return None
    while i < n and s[i].isspace():
        i += 1
    if not s.lower().startswith("step", i):
        return None
    i += 4
    if i >= n or not s[i].isspace():
        return None
    while i < n and s[i].isspace():
        i += 1
    if i >= n or not s[i].isdigit():
        return None
    num_start = i
    while i < n and s[i].isdigit():
        i += 1
    step_no = int(s[num_start:i])
    while i < n and s[i].isspace():
        i += 1
    if i >= n or s[i] != ":":
        return None
    i += 1
    while i < n and s[i].isspace():
        i += 1
    title = s[i:].rstrip()
    if not title:
        return None
    return f"### Step {step_no:02d}: {title}"


def _normalize_figure_and_step_headings(text: str) -> str:
    """Apply linear figure/step heading normalization line-by-line."""
    if not text:
        return text
    out: List[str] = []
    for line in text.split("\n"):
        fig = _try_normalize_figure_heading_line(line)
        if fig is not None:
            out.append(fig)
            continue
        step = _try_normalize_step_heading_line(line)
        if step is not None:
            out.append(step)
            continue
        out.append(line)
    return "\n".join(out)


def _sync_extracted_frames_for_markdown(session: VideoAnalysisSession, logger) -> None:
    """Ensure every figure timestamp in the report has a cached frame on disk."""
    markdown = getattr(session, "generated_markdown", "") or ""
    video_path = getattr(session, "video_path", None)
    temp_dir = getattr(session, "temp_dir", None)
    if not markdown or not video_path or not temp_dir or not os.path.isfile(video_path):
        return

    if not hasattr(session, "extracted_frames") or session.extracted_frames is None:
        session.extracted_frames = {}

    seen = set()
    for match in _FIGURE_TIMESTAMP_PATTERN.finditer(markdown):
        raw_ts = match.group(1) or match.group(2)
        normalized = normalize_video_timestamp(raw_ts)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)

        existing = session.extracted_frames.get(normalized)
        if not existing:
            for key, path in session.extracted_frames.items():
                if normalize_video_timestamp(key) == normalized:
                    existing = path
                    break

        if existing and os.path.isfile(existing) and os.path.getsize(existing) > 0:
            try:
                base = os.path.realpath(temp_dir)
                full = os.path.realpath(existing)
                if full == base or full.startswith(base + os.sep):
                    session.extracted_frames[normalized] = full
                    register_session_frame_s3(session, normalized, full)
                    continue
            except OSError:
                pass

        frame_path = extract_frame_with_fallback(
            video_path,
            normalized,
            temp_dir,
            crop_participants=False,
        )
        if frame_path:
            try:
                base = os.path.realpath(temp_dir)
                full = os.path.realpath(frame_path)
                if full == base or full.startswith(base + os.sep):
                    session.extracted_frames[normalized] = full
                    register_session_frame_s3(session, normalized, full)
            except OSError:
                pass
        else:
            logger.warning(
                "[VIDEO ANALYSIS] Could not extract preview frame for timestamp %s",
                log_safe(normalized),
            )


def raise_if_cancelled(session: VideoAnalysisSession) -> None:
    if getattr(session, "cancelled", False):
        raise VideoAnalysisCancelledError()


def _sync_status(session: VideoAnalysisSession) -> None:
    persist_video_analysis_status(session)


def compress_video_ffmpeg(input_path: str, logger) -> str:
    """
    Compress video using ffmpeg before processing.
    Targets 480p max, 10fps, low bitrate for fast frame extraction.
    Returns path to compressed file, or original path if ffmpeg unavailable/fails.
    """
    try:
        result = subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=5)
        if result.returncode != 0:
            return input_path
    except (FileNotFoundError, subprocess.TimeoutExpired):
        logger.warning("[VIDEO COMPRESS] ffmpeg not found, skipping server-side compression")
        return input_path

    try:
        compressed_path = input_path + "_compressed.mp4"
        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            input_path,
            "-vf",
            "scale=854:480:force_original_aspect_ratio=decrease,pad=854:480:(ow-iw)/2:(oh-ih)/2",
            "-r",
            "10",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "32",
            "-an",
            "-movflags",
            "+faststart",
            compressed_path,
        ]
        proc = subprocess.run(cmd, capture_output=True, timeout=600)
        if proc.returncode != 0:
            logger.warning(
                f"[VIDEO COMPRESS] ffmpeg failed (code {proc.returncode}): {proc.stderr.decode()[-500:]}"
            )
            cmd2 = [
                "ffmpeg",
                "-y",
                "-i",
                input_path,
                "-vf",
                "scale=-2:480",
                "-r",
                "10",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-crf",
                "32",
                "-an",
                compressed_path,
            ]
            proc = subprocess.run(cmd2, capture_output=True, timeout=600)
        if proc.returncode == 0 and os.path.exists(compressed_path):
            orig_mb = os.path.getsize(input_path) / (1024 * 1024)
            comp_mb = os.path.getsize(compressed_path) / (1024 * 1024)
            logger.info(
                f"[VIDEO COMPRESS] Server compression: {orig_mb:.1f}MB → {comp_mb:.1f}MB "
                f"({100 * (1 - comp_mb / orig_mb):.0f}% reduction)"
            )
            return compressed_path

        logger.warning(
            "[VIDEO COMPRESS] ffmpeg failed: %s",
            log_safe(proc.stderr.decode()[-500:]),
        )
        return input_path
    except subprocess.TimeoutExpired:
        logger.warning("[VIDEO COMPRESS] ffmpeg timed out, using original")
        return input_path
    except Exception as exc:
        logger.warning(
            "[VIDEO COMPRESS] Compression error: %s, using original",
            log_safe(exc),
        )
        return input_path


def generate_pdf_title_from_content(markdown_content: str, deps: VideoAnalysisDependencies) -> str:
    """
    Resolve the report title for history cards + download names.

    Prefer the markdown H1 used as the PDF/DOCX cover title so All Generated
    Reports matches the document. Fall back to a short Bedrock title only when
    no usable H1 exists.
    """
    from utils.video_analysis.markdown import extract_document_title_from_markdown

    cover_title = extract_document_title_from_markdown(markdown_content)
    if cover_title:
        return cover_title

    try:
        lines = markdown_content.split("\n")[:10]
        content_preview = "\n".join(lines)

        prompt = (
            "Based on this meeting documentation content, generate a concise, "
            "professional title (max 12 words) for the report file.\n"
            "The title should capture the main topic or purpose of the meeting.\n\n"
            f"Content preview:\n{content_preview}\n\n"
            "Generate ONLY the title, nothing else.\n"
            "Examples: \"AWS Migration Strategy Discussion\", "
            "\"Q4 Sales Review Meeting\", \"Product Launch Planning Session\"\n\n"
            "Title:"
        )

        response = deps.bedrock_client.converse(
            modelId=deps.generation_model,
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig={"maxTokens": 50, "temperature": 0.3},
        )

        title = response["output"]["message"]["content"][0]["text"].strip()
        title = title.replace('"', "").replace("'", "").strip()
        if len(title) > 200 or len(title) < 5:
            title = "Meeting Documentation"
        return title
    except Exception as exc:
        deps.logger.error("Error generating PDF title: %s", log_safe(exc))
        return "Meeting Documentation"


def build_process_video_background(deps: VideoAnalysisDependencies):
    logger = deps.logger

    def process_video_background(session_id: str, user_id: str, db):
        """Background task to process video and generate markdown."""
        session = video_analysis_sessions[session_id]
        try:
            raise_if_cancelled(session)
            session.status = "processing"
            session.progress = 5
            _sync_status(session)

            is_ec2 = os.environ.get("ENVIRONMENT", "local").lower() == "production"
            raw_path = session.video_path
            if is_ec2:
                session.message = "Compressing video for fast processing. This may take a few minutes depending on the size of the video."
                _sync_status(session)
                compressed_path = compress_video_ffmpeg(raw_path, logger)
                if compressed_path != raw_path:
                    session.video_path = compressed_path
                    try:
                        os.remove(raw_path)
                    except Exception:
                        pass
            else:
                logger.info("[VIDEO COMPRESS] Skipping compression — running in local environment")
                session.message = "Preparing video for processing (compression skipped locally)..."

            upload_session_video_file(session)

            session.progress = 10
            session.message = (
                "Analyzing transcript with AI (enhanced sequence mode)..."
                if session.enhanced_sequence_analysis
                else "Analyzing transcript with AI..."
            )
            _sync_status(session)
            raise_if_cancelled(session)

            effective_instructions = session.custom_instructions
            if session.enhanced_sequence_analysis:
                enhanced_directive = """
Mandatory enhanced mode requirements:
- Build a strict chronological, step-by-step action narrative of what happens in the video.
- Prioritize progression actions and SOP flow over static/repeated moments.
- Cover important operational evidence: system logs, alerts, status indicators, error messages, console output, and key UI state changes when present.
- Avoid duplicate or near-duplicate descriptions for the same static moment.
- Keep the existing report structure and style intact.
"""
                effective_instructions = (
                    (effective_instructions + "\n\n" + enhanced_directive).strip()
                    if effective_instructions
                    else enhanced_directive.strip()
                )

            md_content = analyze_transcript_structure(session.transcript_text, effective_instructions)
            raise_if_cancelled(session)
            session.progress = 30
            session.message = "Extracting visual cue timestamps..."
            _sync_status(session)

            def _dedupe_image_markdown_timestamps(text: str) -> str:
                """Remove duplicate [IMAGE: MM:SS] lines."""
                seen_ts = set()
                kept = []
                for line in text.split("\n"):
                    stripped = line.strip()
                    match = re.match(
                        r"\[IMAGE:\s*(\d{1,2}:\d{2}(?::\d{2})?)\s*\]\s*$",
                        stripped,
                        re.IGNORECASE,
                    )
                    if match:
                        ts_key = match.group(1)
                        if ts_key in seen_ts:
                            continue
                        seen_ts.add(ts_key)
                    kept.append(line)
                return "\n".join(kept)

            image_count = len(re.findall(r"\[IMAGE:", md_content, re.IGNORECASE))

            if image_count < 20:
                session.message = "Analyzing transcript for visual demonstration moments..."
                duration = get_video_duration(session.video_path)

                def _to_seconds(ts: str) -> Optional[int]:
                    try:
                        parts = [int(x) for x in ts.split(":")]
                        if len(parts) == 2:
                            return parts[0] * 60 + parts[1]
                        if len(parts) == 3:
                            return parts[0] * 3600 + parts[1] * 60 + parts[2]
                        return None
                    except Exception:
                        return None

                def _to_mmss(total_seconds: int) -> str:
                    total_seconds = max(0, int(total_seconds))
                    minutes = total_seconds // 60
                    seconds = total_seconds % 60
                    return f"{minutes:02d}:{seconds:02d}"

                def _merge_timestamps(raw_timestamps: List[str], video_duration: float) -> List[str]:
                    merged_seconds = []
                    for ts in raw_timestamps:
                        sec = _to_seconds(ts)
                        if sec is not None:
                            merged_seconds.append(sec)

                    if video_duration and video_duration > 0:
                        max_sec = max(0, int(video_duration) - 1)
                        merged_seconds = sorted(set(max(0, min(max_sec, s)) for s in merged_seconds))
                    else:
                        merged_seconds = sorted(set(max(0, s) for s in merged_seconds))

                    if not merged_seconds:
                        return []

                    observed_span = (
                        merged_seconds[-1] - merged_seconds[0] if len(merged_seconds) > 1 else 0
                    )
                    min_gap = max(4, int(observed_span / max(len(merged_seconds) * 2, 1)))
                    kept = []
                    for sec in merged_seconds:
                        if not kept:
                            kept.append(sec)
                            continue
                        if sec - kept[-1] < min_gap:
                            continue
                        kept.append(sec)
                    return [_to_mmss(sec) for sec in kept]

                def _duration_fallback_timestamps(video_duration: float, desired_count: int) -> List[str]:
                    """Generate evenly spaced timestamps from video duration."""
                    if not video_duration or video_duration <= 0:
                        return []

                    max_sec = max(1, int(video_duration) - 1)
                    desired_count = max(1, desired_count)
                    min_gap = 12 if session.enhanced_sequence_analysis else 18
                    max_count_by_gap = max(1, max_sec // min_gap)
                    count = min(desired_count, max_count_by_gap)
                    if count <= 1:
                        return [_to_mmss(max(0, min(max_sec, 10)))]

                    start_sec = 8
                    end_sec = max(start_sec + 1, max_sec - 3)
                    span = max(1, end_sec - start_sec)
                    points = []
                    for idx in range(count):
                        sec = start_sec + round(idx * span / max(count - 1, 1))
                        sec = max(0, min(max_sec, sec))
                        points.append(sec)
                    return [_to_mmss(sec) for sec in sorted(set(points))]

                target_count = 48 if session.enhanced_sequence_analysis else 30
                intelligent_timestamps = extract_visual_cue_timestamps(
                    session.transcript_text,
                    target_count=target_count,
                )
                intelligent_timestamps = _merge_timestamps(intelligent_timestamps or [], duration)

                min_required = 12 if session.enhanced_sequence_analysis else 6
                if len(intelligent_timestamps) < min_required:
                    fallback_desired = max(
                        min_required,
                        target_count // 2 if session.enhanced_sequence_analysis else min_required,
                    )
                    fallback_timestamps = _duration_fallback_timestamps(duration, fallback_desired)
                    if fallback_timestamps:
                        combined = _merge_timestamps(
                            intelligent_timestamps + fallback_timestamps,
                            duration,
                        )
                        if len(combined) > len(intelligent_timestamps):
                            logger.warning(
                                f"[VIDEO ANALYSIS] Transcript cues produced only "
                                f"{len(intelligent_timestamps)} timestamps; duration fallback expanded "
                                f"to {len(combined)}"
                            )
                            intelligent_timestamps = combined

                if intelligent_timestamps:
                    sections = re.split(r"\n## ", md_content)
                    if len(sections) > 2:
                        main_content = "\n## ".join(sections[:-2])
                        ending_sections = "\n## ".join(sections[-2:])
                        injected_content = main_content + "\n\n### Visual Demonstrations\n\n"
                        for ts in intelligent_timestamps:
                            injected_content += f"[IMAGE: {ts}]\n\n"
                        md_content = injected_content + "\n## " + ending_sections
                    else:
                        injected_content = md_content.rstrip() + "\n\n### Visual Demonstrations\n\n"
                        for ts in intelligent_timestamps:
                            injected_content += f"[IMAGE: {ts}]\n\n"
                        md_content = injected_content
                else:
                    logger.warning(
                        "No visual cue timestamps found in transcript; keeping AI-generated "
                        "structure without injected hardcoded timeline points"
                    )

            session.progress = 40
            session.message = (
                "Extracting and analyzing video frames with deduplication..."
                if session.enhanced_sequence_analysis
                else "Extracting and analyzing video frames..."
            )
            _sync_status(session)
            raise_if_cancelled(session)

            md_content = _dedupe_image_markdown_timestamps(md_content)
            image_tags = re.findall(r"\[IMAGE: \d{1,2}:\d{2}\]", md_content)
            total_images = len(image_tags)

            if total_images > 0:
                frame_data = []
                seen_hashes = []
                parts = re.split(r"(\[IMAGE: \d{1,2}:\d{2}\])", md_content)
                extracted_count = 0
                duplicate_count = 0

                for idx, part in enumerate(parts):
                    raise_if_cancelled(session)
                    if re.match(r"\[IMAGE: \d{1,2}:\d{2}\]", part):
                        extracted_count += 1
                        session.progress = 40 + int((extracted_count / total_images) * 20)
                        session.message = f"Extracting frame {extracted_count}/{total_images}..."
                        if (
                            extracted_count == 1
                            or extracted_count == total_images
                            or extracted_count % 5 == 0
                        ):
                            _sync_status(session)

                        context_chunk = ""
                        if idx > 0:
                            context_chunk = parts[idx - 1]
                            if idx > 2 and not re.match(r"\[IMAGE: \d{1,2}:\d{2}\]", parts[idx - 2]):
                                context_chunk = parts[idx - 2][-1000:] + "\n\n" + context_chunk

                        time_str = part.strip()[8:-1]

                        def parse_time(t_str):
                            values = list(map(int, t_str.split(":")))
                            return values[0] * 60 + values[1]

                        def format_time(seconds):
                            minutes = seconds // 60
                            seconds = seconds % 60
                            return f"{minutes:02d}:{seconds:02d}"

                        original_seconds = parse_time(time_str)
                        video_duration = get_video_duration(session.video_path) or 0
                        offset_window = (
                            max(14, min(75, int(video_duration * 0.04)))
                            if video_duration > 0
                            else 35
                        )
                        offset_step = max(4, min(12, int(offset_window / 4)))
                        backward_offsets = sorted({-offset_step, -int(offset_step / 2)})
                        forward_offsets = list(range(0, offset_window + 1, offset_step))
                        offsets = sorted(set(backward_offsets + forward_offsets))

                        best_frame_path = None
                        final_time_str = time_str

                        for offset in offsets:
                            curr_seconds = original_seconds + offset
                            if curr_seconds < 0:
                                continue

                            curr_time_str = format_time(curr_seconds)
                            frame_path = extract_frame_at_time(
                                session.video_path,
                                curr_time_str,
                                session.temp_dir,
                            )

                            if frame_path:
                                frame_hash = compute_perceptual_hash(frame_path)
                                if frame_hash is None:
                                    continue

                                is_duplicate = False
                                for seen_hash in seen_hashes:
                                    hash_distance = frame_hash - seen_hash
                                    duplicate_threshold = 0 if session.enhanced_sequence_analysis else 2
                                    if hash_distance <= duplicate_threshold:
                                        is_duplicate = True
                                        break

                                if is_duplicate:
                                    duplicate_count += 1
                                    continue

                                best_frame_path = frame_path
                                final_time_str = curr_time_str
                                seen_hashes.append(frame_hash)
                                frame_data.append(
                                    (frame_path, frame_hash, context_chunk, final_time_str, idx)
                                )
                                break

                _sync_status(session)
                session.progress = 60
                session.message = f"Processing {len(frame_data)} unique frames with AI..."
                _sync_status(session)
                raise_if_cancelled(session)

                if frame_data:
                    image_tasks = []
                    for idx, (frame_path, frame_hash, context, timestamp, orig_idx) in enumerate(
                        frame_data,
                        1,
                    ):
                        image_tasks.append(
                            (frame_path, context, session.transcript_text, idx, timestamp)
                        )

                    results = batch_process_images_parallel(image_tasks, max_workers=10)
                    raise_if_cancelled(session)
                    session.progress = 90
                    session.message = "Assembling final document..."
                    _sync_status(session)

                    new_markdown = ""
                    valid_count = 0
                    timestamp_to_result = {}
                    processed_hashes = []

                    for result in results:
                        success, fig_num, timestamp, description, frame_path = result
                        if success and description:
                            result_hash = compute_perceptual_hash(frame_path)
                            is_final_duplicate = False
                            if result_hash:
                                for proc_hash in processed_hashes:
                                    final_duplicate_threshold = (
                                        0 if session.enhanced_sequence_analysis else 1
                                    )
                                    if result_hash - proc_hash <= final_duplicate_threshold:
                                        is_final_duplicate = True
                                        break

                            if not is_final_duplicate:
                                timestamp_to_result[timestamp] = description
                                if frame_path and os.path.isfile(frame_path):
                                    norm_ts = normalize_video_timestamp(timestamp) or timestamp
                                    try:
                                        base = os.path.realpath(session.temp_dir)
                                        full = os.path.realpath(frame_path)
                                        if full == base or full.startswith(base + os.sep):
                                            session.extracted_frames[norm_ts] = full
                                            register_session_frame_s3(session, norm_ts, full)
                                    except OSError:
                                        pass
                                if result_hash:
                                    processed_hashes.append(result_hash)

                    def _ts_to_seconds(ts):
                        try:
                            minutes, seconds = ts.split(":")
                            return int(minutes) * 60 + int(seconds)
                        except Exception:
                            return 0

                    def _normalize_desc(desc: str) -> str:
                        desc = re.sub(r"[`*_#>\-\[\]\(\)]", " ", desc or "")
                        desc = re.sub(r"\s+", " ", desc).strip().lower()
                        return desc

                    def _description_overlap_score(a: str, b: str) -> float:
                        a_tokens = {token for token in a.split() if len(token) > 2}
                        b_tokens = {token for token in b.split() if len(token) > 2}
                        if not a_tokens or not b_tokens:
                            return 0.0
                        inter = len(a_tokens.intersection(b_tokens))
                        union = len(a_tokens.union(b_tokens))
                        return inter / max(union, 1)

                    def _is_important_progression(desc: str) -> bool:
                        if not desc:
                            return False
                        important_patterns = re.compile(
                            r"\b("
                            r"click|select|open|close|submit|save|deploy|run|execute|start|stop|restart|navigate|"
                            r"create|delete|update|install|configure|build|trigger|approve|publish|"
                            r"log|error|warn|warning|exception|trace|timeout|failed|status|success|"
                            r"terminal|console|output|api|request|response|"
                            r"step|phase|workflow|sop|screen|panel|dialog|modal"
                            r")\b",
                            re.IGNORECASE,
                        )
                        return bool(important_patterns.search(desc))

                    def _extract_step_details(desc: str):
                        cleaned = re.sub(r"\s+", " ", (desc or "")).strip()
                        pieces = [
                            piece.strip()
                            for piece in re.split(r"(?<=[.!?])\s+", cleaned)
                            if piece.strip()
                        ]
                        action = pieces[0] if pieces else cleaned[:220]
                        detail = pieces[1] if len(pieces) > 1 else cleaned[:320]
                        return action[:240], detail[:360]

                    def _infer_evidence_type(desc: str) -> str:
                        log_keywords = re.compile(
                            r"\b(log|error|warn|warning|exception|trace|status\s*code|console|failed|timeout)\b",
                            re.IGNORECASE,
                        )
                        ui_keywords = re.compile(
                            r"\b(button|screen|page|panel|modal|dialog|menu|form|field|tab|dashboard|login|landing)\b",
                            re.IGNORECASE,
                        )
                        action_keywords = re.compile(
                            r"\b(click|select|open|close|submit|save|create|delete|update|run|deploy|execute)\b",
                            re.IGNORECASE,
                        )
                        if log_keywords.search(desc or ""):
                            return "System/Log Signal"
                        if ui_keywords.search(desc or ""):
                            return "UI State/Screen Change"
                        if action_keywords.search(desc or ""):
                            return "User Action/Workflow Step"
                        return "Visual Progression"

                    text_segments = []
                    for part in parts:
                        if not re.match(r"\[IMAGE: \d{1,2}:\d{2}\]", part):
                            text_segments.append(part)

                    sorted_images = sorted(
                        [(ts, desc) for ts, desc in timestamp_to_result.items()],
                        key=lambda item: _ts_to_seconds(item[0]),
                    )

                    progression_images = []
                    prev_ts_seconds = None
                    prev_desc_norm = None
                    previous_was_important = False

                    for ts, desc in sorted_images:
                        curr_seconds = _ts_to_seconds(ts)
                        curr_norm = _normalize_desc(desc)
                        if not curr_norm:
                            continue

                        curr_is_important = _is_important_progression(desc)
                        is_redundant = False
                        if prev_ts_seconds is not None and prev_desc_norm is not None:
                            delta = max(0, curr_seconds - prev_ts_seconds)
                            overlap = _description_overlap_score(curr_norm, prev_desc_norm)
                            if session.enhanced_sequence_analysis:
                                if (
                                    delta <= 8
                                    and overlap >= 0.92
                                    and not curr_is_important
                                    and not previous_was_important
                                ):
                                    is_redundant = True
                                elif overlap >= 0.97 and not curr_is_important and delta <= 20:
                                    is_redundant = True
                                elif (
                                    overlap >= 0.98
                                    and not curr_is_important
                                    and not previous_was_important
                                ):
                                    is_redundant = True
                            elif overlap >= 0.97:
                                is_redundant = True

                        if is_redundant:
                            continue

                        progression_images.append((ts, desc))
                        prev_ts_seconds = curr_seconds
                        prev_desc_norm = curr_norm
                        previous_was_important = curr_is_important

                    if (
                        session.enhanced_sequence_analysis
                        and len(progression_images) < 8
                        and len(sorted_images) > len(progression_images)
                    ):
                        fallback_images = []
                        last_ts = None
                        last_norm = None
                        for ts, desc in sorted_images:
                            curr_seconds = _ts_to_seconds(ts)
                            curr_norm = _normalize_desc(desc)
                            if not curr_norm:
                                continue
                            if last_ts is not None:
                                delta = max(0, curr_seconds - last_ts)
                                overlap = _description_overlap_score(curr_norm, last_norm or "")
                                if delta <= 4 and overlap >= 0.985:
                                    continue
                            fallback_images.append((ts, desc))
                            last_ts = curr_seconds
                            last_norm = curr_norm
                        if len(fallback_images) > len(progression_images):
                            logger.warning(
                                f"[VIDEO ANALYSIS] Enhanced mode fallback activated: using "
                                f"{len(fallback_images)} images instead of {len(progression_images)} "
                                f"to preserve step-by-step flow"
                            )
                            progression_images = fallback_images

                    informal_patterns = [
                        r"[Dd]uring the meeting[,.]?\s*",
                        r"[Ww]as discussed in the meeting[,.]?\s*",
                        r"[Aa]s seen in the meeting[,.]?\s*",
                        r"[Tt]he meeting covered[,.]?\s*",
                    ]

                    def _format_image_block(fig_num, time_str, description):
                        for pattern in informal_patterns:
                            description = re.sub(pattern, "", description)
                        cleaned_desc = re.sub(r"\n{3,}", "\n\n", (description or "").strip())
                        caption_part = cleaned_desc
                        analysis_part = ""
                        if "**Analysis:**" in cleaned_desc:
                            parts_desc = cleaned_desc.split("**Analysis:**", 1)
                            caption_part = parts_desc[0].strip()
                            analysis_part = parts_desc[1].strip()

                        caption_part = re.sub(
                            r"^\*\*Caption:\*\*\s*",
                            "",
                            caption_part,
                            flags=re.IGNORECASE,
                        ).strip()
                        caption_part = caption_part.rstrip(".")
                        analysis_part = analysis_part.strip()

                        block = (
                            f"\n\n---\n\n"
                            f"### Figure {fig_num}\n"
                            f"**Timestamp:** `{time_str}`\n"
                            f"**Caption:** {caption_part or 'System screenshot captured for workflow evidence.'}\n\n"
                        )
                        if analysis_part:
                            block += f"**Analysis:**\n{analysis_part}\n\n"
                        return block

                    n_images = len(progression_images)
                    n_text = len(text_segments)

                    if n_images == 0:
                        new_markdown = "".join(text_segments)
                    elif n_text <= 1:
                        new_markdown = "".join(text_segments)
                        for idx, (ts, desc) in enumerate(progression_images, 1):
                            new_markdown += _format_image_block(idx, ts, desc)
                    else:
                        final_section_patterns = re.compile(
                            r"##\s*(Conclusion|Recommendations|Action Items|Next Steps|Risks|Mitigation)",
                            re.IGNORECASE,
                        )
                        last_safe_seg = n_text - 1
                        for seg_idx, seg in enumerate(text_segments):
                            if final_section_patterns.search(seg):
                                last_safe_seg = max(0, seg_idx - 1)
                                break

                        img_after_seg = defaultdict(list)
                        for img_idx, (ts, desc) in enumerate(progression_images):
                            seg_idx = round(img_idx * last_safe_seg / max(n_images - 1, 1))
                            seg_idx = min(seg_idx, last_safe_seg)
                            img_after_seg[seg_idx].append((ts, desc))

                        for seg_idx, seg_text in enumerate(text_segments):
                            new_markdown += seg_text
                            for ts, desc in img_after_seg.get(seg_idx, []):
                                valid_count += 1
                                new_markdown += _format_image_block(valid_count, ts, desc)

                    md_content = new_markdown
                    md_content = re.sub(r"\n{3,}", "\n\n", md_content)
                    md_content = _strip_numbered_toc_markdown_links(md_content)
                    md_content = _strip_bullet_toc_markdown_links(md_content)
                    for pattern in [
                        r"[Dd]uring the meeting[,.]?\s*",
                        r"[Ww]as discussed in the meeting[,.]?\s*",
                        r"[Aa]t this point in the meeting[,.]?\s*",
                        r"[Tt]he meeting covered[,.]?\s*",
                    ]:
                        md_content = re.sub(pattern, "", md_content)

                    def _clean_markdown_presentation(text: str) -> str:
                        cleaned = text or ""
                        cleaned = re.sub(
                            r"^\s*#{2,6}\s*(sop step[- ]by[- ]step execution narrative)\s*$",
                            r"## SOP Step-by-Step Execution Narrative",
                            cleaned,
                            flags=re.IGNORECASE | re.MULTILINE,
                        )
                        cleaned = _normalize_figure_and_step_headings(cleaned)
                        cleaned = re.sub(
                            r"^\s*Caption:\s*",
                            "**Caption:** ",
                            cleaned,
                            flags=re.IGNORECASE | re.MULTILINE,
                        )
                        cleaned = re.sub(
                            r"^\s*Timestamp:\s*",
                            "**Timestamp:** ",
                            cleaned,
                            flags=re.IGNORECASE | re.MULTILINE,
                        )
                        cleaned = re.sub(
                            r"^\s*Analysis:\s*",
                            "**Analysis:** ",
                            cleaned,
                            flags=re.IGNORECASE | re.MULTILINE,
                        )
                        cleaned = re.sub(r"(\n---\n){2,}", "\n---\n", cleaned)
                        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip() + "\n"
                        return cleaned

                    md_content = _clean_markdown_presentation(md_content)

                    if session.enhanced_sequence_analysis and progression_images:
                        narrative_lines = [
                            "## SOP Step-by-Step Execution Narrative",
                            "",
                            "The sequence below captures unique progressed actions in chronological order "
                            "(deduplicated to remove repeated static moments while preserving important "
                            "workflow transitions).",
                            "",
                        ]
                        log_keywords = re.compile(
                            r"\b(log|error|warn|warning|exception|trace|status\s*code|console|failed|timeout)\b",
                            re.IGNORECASE,
                        )
                        for idx, (ts, desc) in enumerate(progression_images, 1):
                            action_text, detail_text = _extract_step_details(desc)
                            evidence_type = _infer_evidence_type(desc)
                            narrative_lines.append(f"### Step {idx:02d}: {action_text}")
                            narrative_lines.append(f"- **Timestamp:** `{ts}`")
                            narrative_lines.append(f"- **Observed Detail:** {detail_text}")
                            narrative_lines.append(f"- **Evidence Type:** {evidence_type}")
                            narrative_lines.append(
                                "- **SOP Intent:** Verify this progression before proceeding to the next operational step."
                            )
                            if log_keywords.search(desc or ""):
                                narrative_lines.append(
                                    "- **System Signal:** Log/error/status evidence is visible at this step and should be retained in final SOP review."
                                )
                            narrative_lines.append("")

                        narrative_block = "\n".join(narrative_lines).strip() + "\n\n"
                        final_section_match = re.search(
                            r"\n##\s*(Conclusion|Recommendations|Action Items|Next Steps|Risks|Mitigation)\b",
                            md_content,
                            re.IGNORECASE,
                        )
                        if final_section_match:
                            insert_at = final_section_match.start()
                            md_content = (
                                md_content[:insert_at].rstrip()
                                + "\n\n"
                                + narrative_block
                                + md_content[insert_at:]
                            )
                        else:
                            md_content = md_content.rstrip() + "\n\n" + narrative_block

            session.generated_markdown = md_content
            _sync_extracted_frames_for_markdown(session, logger)
            _sync_status(session)
            session.status = "completed"
            session.progress = 100
            session.message = "Processing complete!"
            _sync_status(session)

            mode_name = (
                "enhanced_sequence" if session.enhanced_sequence_analysis else "standard"
            )
            deps.log_activity(
                db,
                user_id,
                "VIDEO_ANALYSIS_PROCESS",
                f"Processed video analysis ({mode_name}) with {total_images} timestamps",
            )
        except VideoAnalysisCancelledError:
            logger.info("Video analysis cancelled for session %s", log_safe(session_id))
            session.status = "cancelled"
            session.message = "Cancelled by user"
            _sync_status(session)
            try:
                session.cleanup()
            except Exception:
                pass
        except Exception as exc:
            logger.error("Error processing video analysis: %s", log_safe(exc), exc_info=True)
            session.status = "error"
            session.message = "Processing failed. Please try again."
            _sync_status(session)

    return process_video_background
