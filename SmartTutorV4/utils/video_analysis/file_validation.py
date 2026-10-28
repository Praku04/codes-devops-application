"""Strict upload validation for Video Analysis (MP4 + PDF/DOCX)."""
from __future__ import annotations

import os
import zipfile
from io import BytesIO
from typing import Optional, Tuple

from utils.upload_validation import assert_safe_upload_filename

# UI + API allow-lists — keep in sync with frontend
ALLOWED_VIDEO_EXTENSIONS = frozenset({"mp4"})
ALLOWED_TRANSCRIPT_EXTENSIONS = frozenset({"pdf", "docx"})

# Teams-style suffix noise stripped before stem pairing
_STEM_NOISE_SUFFIXES = (
    "_transcript",
    "-transcript",
    " transcript",
    "_recording",
    "-recording",
    " recording",
    "_meeting",
    "-meeting",
    " meeting",
    "_vtt",
    "-vtt",
    "_caption",
    "-caption",
)

_MAX_VIDEO_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB
_MAX_TRANSCRIPT_BYTES = 50 * 1024 * 1024  # 50 MB

# ISO BMFF major/compatible brands commonly used by real MP4 files
_MP4_BRANDS = frozenset(
    {
        b"isom",
        b"iso2",
        b"iso3",
        b"iso4",
        b"iso5",
        b"iso6",
        b"mp41",
        b"mp42",
        b"avc1",
        b"MSNV",
        b"M4V ",
        b"M4A ",
        b"dash",
        b"mp4i",
        b"mp4v",
        b"ndas",
        b"ndsc",
        b"ndsh",
        b"ndsm",
        b"ndsp",
        b"ndss",
        b"ndxc",
        b"ndxh",
        b"ndxm",
        b"ndxp",
        b"ndxs",
        b"F4V ",
    }
)


def extension_of(filename: Optional[str]) -> str:
    if not filename or "." not in filename:
        return ""
    return os.path.splitext(filename)[1].lstrip(".").lower()


def sanitize_upload_filename(filename: Optional[str], fallback: str = "upload.bin") -> str:
    """Basename-only filename safe for storage, logs, and HTML display."""
    base = os.path.basename((filename or "").strip()) or fallback
    cleaned_chars = []
    for ch in base:
        code = ord(ch)
        if code < 32 or code == 127:
            continue
        if ch in "<>\"'`\\\r\n":
            continue
        cleaned_chars.append(ch)
    cleaned = "".join(cleaned_chars).strip(" .")
    if not cleaned:
        return fallback
    return cleaned[:180]


def normalize_pair_stem(filename: Optional[str]) -> str:
    """Normalize basename for video ↔ transcript pairing."""
    if not filename:
        return ""
    stem = os.path.splitext(os.path.basename(filename))[0]
    stem = stem.strip().lower()
    # Linear character rewrite — avoid backtracking regex (ReDoS-safe)
    pieces: list[str] = []
    prev_space = False
    for ch in stem:
        if ch in " \t_-":
            if not prev_space:
                pieces.append(" ")
            prev_space = True
        else:
            pieces.append(ch)
            prev_space = False
    stem = "".join(pieces).strip()
    changed = True
    while changed and stem:
        changed = False
        for suffix in _STEM_NOISE_SUFFIXES:
            clean = suffix.strip().lower()
            if stem.endswith(clean) and len(stem) > len(clean):
                stem = stem[: -len(clean)].rstrip(" -_")
                changed = True
                break
    # Keep only alphanumeric
    return "".join(ch for ch in stem if ch.isalnum())


def stems_match(video_filename: Optional[str], transcript_filename: Optional[str]) -> bool:
    video_stem = normalize_pair_stem(video_filename)
    transcript_stem = normalize_pair_stem(transcript_filename)
    if not video_stem or not transcript_stem:
        return False
    if video_stem == transcript_stem:
        return True
    # Allow one name containing the other when both are substantial
    if len(video_stem) >= 4 and len(transcript_stem) >= 4:
        return video_stem in transcript_stem or transcript_stem in video_stem
    return False


def is_mp4_content(data: bytes) -> bool:
    """True only for ISO BMFF / MP4 containers (rejects PNG, AVI, MOV-only, etc.)."""
    if not data or len(data) < 12:
        return False
    # Reject common non-video / wrong containers early
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return False
    if data[:3] == b"\xff\xd8\xff":
        return False
    if data[:4] == b"GIF8":
        return False
    if data[:4] == b"RIFF" and len(data) >= 12 and data[8:12] == b"AVI ":
        return False
    if data[:4] == b"OggS":
        return False
    if data[:4] == b"%PDF":
        return False

    # MP4/ISO BMFF: size(4) + 'ftyp'(4) + brand(4)
    if data[4:8] != b"ftyp":
        return False
    brand = data[8:12]
    # Reject pure QuickTime MOV containers even if the extension was renamed to .mp4
    if brand == b"qt  ":
        window = data[8:64]
        return b"mp41" in window or b"mp42" in window or b"isom" in window or b"iso2" in window
    if brand in _MP4_BRANDS:
        return True
    window = data[8:64]
    return b"mp41" in window or b"mp42" in window or b"isom" in window or b"iso2" in window


def is_pdf_content(data: bytes) -> bool:
    return bool(data) and data[:4] == b"%PDF"


def is_docx_content(data: bytes) -> bool:
    """DOCX is a ZIP package that must contain word/ documents."""
    if not data or len(data) < 4 or data[:2] != b"PK":
        return False
    try:
        with zipfile.ZipFile(BytesIO(data)) as archive:
            names = archive.namelist()
            if not names:
                return False
            has_content_types = "[Content_Types].xml" in names
            has_word = any(name.startswith("word/") for name in names)
            return has_content_types and has_word
    except (zipfile.BadZipFile, OSError):
        return False


def validate_video_upload(
    filename: Optional[str], data: bytes
) -> Tuple[bool, str]:
    if not filename or not filename.strip():
        return False, "Video filename is missing. Please upload an MP4 file."
    # Extension allow-list runs first so the caller gets the format-specific hint
    # ("upload an .mp4") instead of the generic guard message. The double-extension
    # guard below still rejects deceptive names such as clip.exe.mp4, whose final
    # segment passes this check.
    ext = extension_of(filename)
    if ext not in ALLOWED_VIDEO_EXTENSIONS:
        return (
            False,
            "Only MP4 video files are allowed. Remove MOV/AVI/PNG or other formats and upload an .mp4 file.",
        )
    name_ok, name_err = assert_safe_upload_filename(filename, ALLOWED_VIDEO_EXTENSIONS)
    if not name_ok:
        return False, name_err
    if not data:
        return False, "The video file is empty. Please choose a valid MP4 recording."
    if len(data) > _MAX_VIDEO_BYTES:
        return False, "Video file is too large (maximum 2 GB)."
    if not is_mp4_content(data):
        return (
            False,
            "The selected file is not a valid MP4 video. PNG/images and other formats are not accepted.",
        )
    return True, ""


def validate_transcript_upload(
    filename: Optional[str], data: bytes
) -> Tuple[bool, str]:
    if not filename or not filename.strip():
        return False, "Transcript filename is missing. Please upload a PDF or DOCX file."
    # Same ordering as validate_video_upload: format-specific message first, then the
    # deceptive-name guard (which still catches transcript.php.pdf and friends).
    ext = extension_of(filename)
    if ext not in ALLOWED_TRANSCRIPT_EXTENSIONS:
        return (
            False,
            "Only PDF or DOCX transcript files are allowed. Other formats cannot be used.",
        )
    name_ok, name_err = assert_safe_upload_filename(
        filename, ALLOWED_TRANSCRIPT_EXTENSIONS
    )
    if not name_ok:
        return False, name_err
    if not data:
        return False, "The transcript file is empty. Please choose a valid PDF or DOCX file."
    if len(data) > _MAX_TRANSCRIPT_BYTES:
        return False, "Transcript file is too large (maximum 50 MB)."
    if ext == "pdf" and not is_pdf_content(data):
        return False, "The selected transcript is not a valid PDF file."
    if ext == "docx" and not is_docx_content(data):
        return False, "The selected transcript is not a valid DOCX file."
    return True, ""


def validate_video_transcript_pair(
    video_filename: Optional[str], transcript_filename: Optional[str]
) -> Tuple[bool, str]:
    if not stems_match(video_filename, transcript_filename):
        video_label = sanitize_upload_filename(
            os.path.basename(video_filename or ""), "video.mp4"
        )
        transcript_label = sanitize_upload_filename(
            os.path.basename(transcript_filename or ""), "transcript.pdf"
        )
        return (
            False,
            (
                "Video and transcript names do not match. "
                "Use the same meeting name for both files "
                "(example: MeetingName.mp4 with MeetingName.pdf or MeetingName.docx). "
                f"Received video '{video_label}' and transcript '{transcript_label}'."
            ),
        )
    return True, ""


def safe_download_basename(name: Optional[str], fallback: str = "Meeting_Documentation") -> str:
    """Sanitize a basename segment for Content-Disposition / S3 keys."""
    raw = (name or "").strip() or fallback
    stem = os.path.splitext(os.path.basename(raw))[0] or fallback
    cleaned_chars: list[str] = []
    prev_sep = False
    for ch in stem:
        if ch.isalnum():
            cleaned_chars.append(ch)
            prev_sep = False
        elif ch in (" ", "-", "_"):
            if not prev_sep:
                cleaned_chars.append("_")
            prev_sep = True
        else:
            if not prev_sep:
                cleaned_chars.append("_")
            prev_sep = True
    cleaned = "".join(cleaned_chars).strip("_")
    return (cleaned[:80] or fallback)


# Windows-safe download basename budget (leave headroom under 255 + Content-Disposition)
_MAX_EXPORT_BASENAME_LEN = 100
_MAX_EXPORT_STEM_LEN = 60
_FALLBACK_EXPORT_STEM = "Meeting_Documentation"
_SHORT_FALLBACK_EXPORT_STEM = "VA_Report"

# Longest-first so "meeting_recording" is peeled before "meeting" / "recording"
_EXPORT_NOISE_SUFFIXES = (
    "meeting_recording",
    "meeting-recording",
    "meetingrecording",
    "transcript",
    "recording",
    "meeting",
    "caption",
    "vtt",
)


def _strip_trailing_separators(value: str) -> str:
    return value.rstrip("_- ")


def _strip_trailing_date_stamp(stem: str) -> Optional[str]:
    """
    Peel one trailing YYYYMMDD_HHMMSS / YYYYMMDD-HHMMSS style stamp.
    Fixed-width digit checks only — no ambiguous regex (ReDoS-safe).
    """
    if len(stem) < 14:
        return None

    # _YYYYMMDD_HHMMSS or -YYYYMMDD-HHMMSS
    if len(stem) >= 15:
        tail = stem[-15:]
        if (
            tail[0] in "_-"
            and tail[9] in "_-"
            and tail[1:9].isdigit()
            and tail[10:16].isdigit()
        ):
            return _strip_trailing_separators(stem[:-15])

    # YYYYMMDD_HHMMSS or YYYYMMDD-HHMMSS
    if len(stem) >= 15:
        tail = stem[-15:]
        if tail[8] in "_-" and tail[:8].isdigit() and tail[9:].isdigit():
            return _strip_trailing_separators(stem[:-15])

    # YYYYMMDDHHMMSS
    tail14 = stem[-14:]
    if tail14.isdigit():
        return _strip_trailing_separators(stem[:-14])

    return None


def _strip_trailing_noise_word(stem: str) -> Optional[str]:
    """Peel one known Teams noise suffix. Case-insensitive exact suffix match only."""
    lower = stem.lower()
    for suffix in _EXPORT_NOISE_SUFFIXES:
        if not lower.endswith(suffix):
            continue
        if len(lower) == len(suffix):
            return None
        cut = len(stem) - len(suffix)
        before = _strip_trailing_separators(stem[:cut])
        if before:
            return before
    return None


def _collapse_separators(stem: str) -> str:
    """Collapse repeated _ / - without backtracking regex."""
    if not stem:
        return ""
    out: list[str] = []
    prev_sep = False
    for ch in stem:
        if ch in "_-":
            if not prev_sep:
                out.append("_")
            prev_sep = True
        else:
            out.append(ch)
            prev_sep = False
    return "".join(out).strip("_-")


def _clean_export_stem(name: Optional[str]) -> str:
    """Sanitize and strip Teams-style date/recording tails from a name stem."""
    stem = safe_download_basename(name, "")
    if not stem:
        return ""
    # Bounded iterations on already-sanitized short stems (max 80 chars)
    for _ in range(8):
        next_stem = _strip_trailing_date_stamp(stem)
        if next_stem is not None and next_stem != stem:
            stem = next_stem
            continue
        next_stem = _strip_trailing_noise_word(stem)
        if next_stem is not None and next_stem != stem:
            stem = next_stem
            continue
        break
    return _collapse_separators(stem)


def _is_export_timestamp(value: str) -> bool:
    """True for YYYYMMDD_HHMMSS (fixed format; no regex)."""
    if len(value) != 15 or value[8] != "_":
        return False
    return value[:8].isdigit() and value[9:].isdigit()


def build_export_download_name(
    *,
    video_filename: Optional[str],
    report_title: Optional[str],
    file_ext: str,
    timestamp: Optional[str] = None,
) -> str:
    """
    Build a length-safe PDF/DOCX download name with fallbacks.

    Preferred:  {CleanReportTitle}_{YYYYMMDD_HHMMSS}.{ext}  (same as PDF cover title)
    Fallback 1: {CleanMeetingStem}_{YYYYMMDD_HHMMSS}.{ext}
    Fallback 2: Meeting_Documentation_{YYYYMMDD_HHMMSS}.{ext}
    Fallback 3: VA_Report_{YYYYMMDD_HHMMSS}.{ext}
    """
    ext = (file_ext or "pdf").lower().lstrip(".")
    if ext not in {"pdf", "docx"}:
        ext = "pdf"

    ts = (timestamp or "").strip()
    if not _is_export_timestamp(ts):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        ts = datetime.now(ZoneInfo("Asia/Kolkata")).strftime("%Y%m%d_%H%M%S")

    suffix = f"_{ts}.{ext}"
    # Keep suffix intact; budget remaining chars for stem
    max_stem = min(
        _MAX_EXPORT_STEM_LEN,
        max(8, _MAX_EXPORT_BASENAME_LEN - len(suffix)),
    )

    # Prefer report/cover title so download name matches All Generated Reports + PDF
    candidates = [
        _clean_export_stem(report_title),
        _clean_export_stem(video_filename),
        _FALLBACK_EXPORT_STEM,
        _SHORT_FALLBACK_EXPORT_STEM,
    ]

    for candidate in candidates:
        if not candidate:
            continue
        stem = candidate[:max_stem].rstrip("_-")
        if not stem:
            continue
        basename = f"{stem}{suffix}"
        if len(basename) <= _MAX_EXPORT_BASENAME_LEN:
            return basename

    # Absolute last resort — cannot exceed limits with short stem + timestamp
    return f"{_SHORT_FALLBACK_EXPORT_STEM}{suffix}"[:_MAX_EXPORT_BASENAME_LEN]


def clamp_export_filename(filename: Optional[str], file_ext: str = "pdf") -> str:
    """Final guard: clamp any download name to safe length/charset or fall back."""
    ext = (file_ext or "pdf").lower().lstrip(".")
    if ext not in {"pdf", "docx"}:
        ext = "pdf"
    raw = os.path.basename((filename or "").strip())
    cleaned_chars: list[str] = []
    prev_sep = False
    for ch in raw:
        if ch.isalnum() or ch == ".":
            cleaned_chars.append(ch)
            prev_sep = False
        elif ch in "-_":
            if not prev_sep:
                cleaned_chars.append("_")
            prev_sep = True
    cleaned = "".join(cleaned_chars).strip("._")
    if not cleaned.lower().endswith(f".{ext}"):
        stem = os.path.splitext(cleaned)[0] if cleaned else _SHORT_FALLBACK_EXPORT_STEM
        cleaned = f"{stem}.{ext}"
    if 1 < len(cleaned) <= _MAX_EXPORT_BASENAME_LEN:
        return cleaned
    return build_export_download_name(
        video_filename=None,
        report_title=None,
        file_ext=ext,
    )


def is_ephemeral_video_filename(filename: Optional[str]) -> bool:
    """
    True for tempfile / ffmpeg compressed path basenames that must not be shown in UI.
    Examples: tmpp3vm1kva.mp4, tmp5rat0u1w.mp4_compressed.mp4
    """
    base = os.path.basename((filename or "").strip())
    if not base:
        return True
    lower = base.lower()
    if "_compressed" in lower:
        return True
    stem = os.path.splitext(lower)[0]
    if stem.endswith("_compressed"):
        stem = stem[: -len("_compressed")]
    if not stem.startswith("tmp"):
        return False
    rest = stem[3:].replace("_", "")
    return bool(rest) and len(rest) <= 16 and rest.isalnum()


def _looks_like_underscore_slug(filename: Optional[str]) -> bool:
    """True when the basename is an underscore slug (not a natural title)."""
    stem = os.path.splitext(os.path.basename((filename or "").strip()))[0]
    if not stem or " " in stem:
        return False
    return "_" in stem


_DISPLAY_NOISE_TAILS = (
    " meeting recording",
    "-meeting recording",
    "_meeting_recording",
    "-meeting_recording",
    " meeting_recording",
    "_meeting recording",
)


def _strip_display_date_tail(stem: str) -> str:
    """Remove one trailing date stamp (YYYYMMDD_HHMMSS style) from a display stem."""
    s = stem.rstrip(" -_")
    if len(s) < 14:
        return s

    if len(s) >= 16 and s[-16] in " _-":
        stamp = s[-15:]
        if (
            stamp[0] in "_-"
            and stamp[9] in "_-"
            and stamp[1:9].isdigit()
            and stamp[10:16].isdigit()
        ):
            return s[:-16].rstrip(" -_")

    if len(s) >= 15:
        stamp = s[-15:]
        if stamp[8] in "_-" and stamp[:8].isdigit() and stamp[9:].isdigit():
            if len(s) >= 16 and s[-16] in " _-":
                return s[:-16].rstrip(" -_")
            return s[:-15].rstrip(" -_")

    if len(s) >= 15 and s[-15] in " _-" and s[-14:].isdigit():
        return s[:-15].rstrip(" -_")

    if len(s) >= 14 and s[-14:].isdigit():
        return s[:-14].rstrip(" -_")

    return s


def _strip_display_noise_tails(stem: str) -> str:
    s = stem
    for _ in range(6):
        lower = s.lower()
        matched = False
        for tail in _DISPLAY_NOISE_TAILS:
            if lower.endswith(tail) and len(s) > len(tail):
                s = s[: -len(tail)].rstrip(" -_")
                matched = True
                break
        if not matched:
            break
    return s


def normalize_report_display_title(text: Optional[str]) -> str:
    """Turn a filename or report title into a clean UI title (spaces, no extension)."""
    raw = (text or "").strip() or "Meeting Recording"
    stem = os.path.splitext(os.path.basename(raw))[0]
    # Noise ("Meeting Recording") first so date stamps become trailing and peel cleanly
    stem = _strip_display_noise_tails(stem)
    stem = _strip_display_date_tail(stem)
    stem = _strip_display_noise_tails(stem)
    chars: list[str] = []
    prev_space = False
    for ch in stem:
        if ch in "_\t-":
            if not prev_space:
                chars.append(" ")
            prev_space = True
            continue
        if ch in "<>\"'`\\/|?*":
            continue
        code = ord(ch)
        if code < 32 or code == 127:
            continue
        if ch == " ":
            if not prev_space:
                chars.append(" ")
            prev_space = True
            continue
        chars.append(ch)
        prev_space = False
    title = "".join(chars).strip(" -_")
    while "  " in title:
        title = title.replace("  ", " ")
    return (title[:120] or "Meeting Recording")


def archive_source_video_filename(
    video_filename: Optional[str],
    pdf_title: Optional[str] = None,
) -> str:
    """Safe archived basename for DB (always ends with .mp4)."""
    candidate = sanitize_upload_filename(video_filename or "", "")
    if candidate and not is_ephemeral_video_filename(candidate):
        if not candidate.lower().endswith(".mp4"):
            stem = os.path.splitext(candidate)[0] or candidate
            candidate = f"{stem}.mp4"
        return candidate[:180]
    title_stem = _clean_export_stem(pdf_title) or _FALLBACK_EXPORT_STEM
    title_stem = title_stem[:80].rstrip("_-") or _FALLBACK_EXPORT_STEM
    return f"{title_stem}.mp4"


def display_source_video_label(
    video_filename: Optional[str],
    pdf_title: Optional[str] = None,
) -> str:
    """
    Proper title for All Reports / Super Admin video lines.
    Uses spaces (not underscores), strips dates / Meeting Recording / .mp4.
    Prefer report title when the stored video name is temp or an underscore slug.
    """
    pdf = (pdf_title or "").strip()
    raw_video = (video_filename or "").strip()

    if pdf and (
        not raw_video
        or is_ephemeral_video_filename(raw_video)
        or _looks_like_underscore_slug(raw_video)
    ):
        return normalize_report_display_title(pdf)

    if raw_video and not is_ephemeral_video_filename(raw_video):
        return normalize_report_display_title(raw_video)

    if pdf:
        return normalize_report_display_title(pdf)
    return "Meeting Recording"
