"""Helpers for CodeQL-safe logging and linear image-tag parsing."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Iterator, List, Tuple

from werkzeug.utils import secure_filename

GENERIC_API_ERROR_MESSAGE = "An unexpected error occurred. Please try again later."
SERVICE_UNAVAILABLE_MESSAGE = "The service is temporarily unavailable. Please try again later."


def safe_client_error_detail(
    exc: BaseException,
    *,
    fallback: str = GENERIC_API_ERROR_MESSAGE,
    max_len: int = 200,
) -> str:
    """
    Return a short, single-line client-facing message from a ValueError we raised,
    or ``fallback`` when the exception text looks unsafe / library-sourced (CodeQL).
    """
    raw = str(exc or "").replace("\r", " ").replace("\n", " ").strip()
    if not raw or len(raw) > max_len:
        return fallback
    lower = raw.lower()
    if any(
        marker in lower
        for marker in (
            "traceback",
            "file \"",
            "sqlalchemy",
            "psycopg",
            "botocore",
            "accessdenied",
            "stack trace",
        )
    ):
        return fallback
    return raw


_IMAGE_TAG_PREFIX = "[IMAGE:"
_IMAGE_URL_TAG_PREFIX = "[IMAGE_URL:"


def _safe_single_filename(name: str) -> str:
    """Return a sanitized single path segment (no directories)."""
    if not name or name in {".", ".."}:
        raise ValueError("invalid path segment")
    base_name = Path(name).name
    if base_name != name or not base_name:
        raise ValueError("invalid path segment")
    safe_name = secure_filename(base_name)
    if not safe_name or safe_name != base_name:
        raise ValueError("invalid path segment")
    return safe_name


def _validated_real_path_under_base(base_dir: Path, name: str) -> str:
    """Return realpath for a single file segment under base_dir (file may not exist)."""
    safe_name = _safe_single_filename(name)
    base = os.path.realpath(str(base_dir.resolve()))
    full = os.path.realpath(os.path.join(base, safe_name))
    if full != base and not full.startswith(base + os.sep):
        raise ValueError("path escapes base directory")
    return full


def validated_real_path_under_base(base_dir: Path, name: str) -> str:
    """Public alias for static analysis–friendly path resolution."""
    return _validated_real_path_under_base(base_dir, name)


def is_regular_file_under_base(base_dir: Path, name: str) -> bool:
    try:
        full = _validated_real_path_under_base(base_dir, name)
    except ValueError:
        return False
    return os.path.isfile(full)


def file_size_under_base(base_dir: Path, name: str) -> int:
    full = _validated_real_path_under_base(base_dir, name)
    return os.path.getsize(full)


def write_bytes_under_base(base_dir: Path, name: str, data: bytes) -> None:
    full = _validated_real_path_under_base(base_dir, name)
    parent = os.path.dirname(full)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(full, "wb") as handle:
        handle.write(data)


def strip_surrounding_quotes(value: str, quote_chars: str = "\"'") -> str:
    """Remove leading/trailing quote characters without regex."""
    text = value or ""
    while text and text[0] in quote_chars:
        text = text[1:]
    while text and text[-1] in quote_chars:
        text = text[:-1]
    return text


def unlink_file_under_base(base_dir: Path, name: str) -> bool:
    """Delete a single regular file under base_dir."""
    full = _validated_real_path_under_base(base_dir, name)
    if not os.path.isfile(full):
        return False
    os.unlink(full)
    return True


def media_type_for_image_name(name: str) -> str:
    """Return image Content-Type from a validated single-segment filename."""
    safe_name = _safe_single_filename(name)
    ext = os.path.splitext(safe_name)[1].lower()
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
    }.get(ext, "image/png")


def file_response_under_base(
    base_dir: Path,
    name: str,
    media_type: str,
    headers: dict | None = None,
):
    """
    Return a FileResponse for an existing file under base_dir.
    All path validation and filesystem checks happen in this module.
    """
    from fastapi.responses import FileResponse

    full = _validated_real_path_under_base(base_dir, name)
    if not os.path.isfile(full):
        raise FileNotFoundError(name)
    return FileResponse(
        full,
        media_type=media_type,
        headers=headers or {},
    )


def log_safe(value) -> str:
    """Sanitize user-controlled values before writing to logs (CodeQL log-injection safe)."""
    if value is None:
        return ""
    return str(value).replace("\r", "").replace("\n", "")


def extract_image_tags(content: str) -> List[str]:
    """Extract S3 keys from [IMAGE: ...] tags without regex backtracking."""
    if not content:
        return []
    keys: List[str] = []
    i = 0
    plen = len(_IMAGE_TAG_PREFIX)
    while True:
        start = content.find(_IMAGE_TAG_PREFIX, i)
        if start < 0:
            break
        j = start + plen
        while j < len(content) and content[j].isspace():
            j += 1
        end = content.find("]", j)
        if end < 0:
            break
        if end == j:
            i = start + 1
            continue
        keys.append(content[j:end].strip())
        i = end + 1
    return keys


def count_image_tags(content: str) -> int:
    return len(extract_image_tags(content))


def extract_markdown_image_urls(content: str) -> List[str]:
    """Extract URLs from markdown ![alt](url) tags (linear scan)."""
    if not content:
        return []
    urls: List[str] = []
    i = 0
    while True:
        start = content.find("![", i)
        if start < 0:
            break
        alt_end = content.find("](", start + 2)
        if alt_end < 0:
            break
        url_start = alt_end + 2
        url_end = content.find(")", url_start)
        if url_end < 0:
            break
        urls.append(content[url_start:url_end].strip())
        i = url_end + 1
    return urls


def iter_image_tag_spans(content: str) -> Iterator[Tuple[int, int, str]]:
    """Yield (start, end_exclusive, s3_key) for each [IMAGE: ...] tag."""
    if not content:
        return
    i = 0
    plen = len(_IMAGE_TAG_PREFIX)
    while True:
        start = content.find(_IMAGE_TAG_PREFIX, i)
        if start < 0:
            return
        j = start + plen
        while j < len(content) and content[j].isspace():
            j += 1
        end = content.find("]", j)
        if end < 0:
            return
        if end == j:
            i = start + 1
            continue
        yield start, end + 1, content[j:end].strip()
        i = end + 1


def extract_image_url_tags(content: str) -> List[str]:
    if not content:
        return []
    urls: List[str] = []
    i = 0
    plen = len(_IMAGE_URL_TAG_PREFIX)
    while True:
        start = content.find(_IMAGE_URL_TAG_PREFIX, i)
        if start < 0:
            break
        j = start + plen
        while j < len(content) and content[j] in " \t":
            j += 1
        end = content.find("]", j)
        if end < 0:
            break
        urls.append(content[j:end].strip())
        i = end + 1
    return urls


def strip_bracket_image_tags(content: str) -> str:
    """
    Remove [IMAGE: ...] and [IMAGE_URL: ...] tags with a linear scan.
    Matches former regex ``\\[IMAGE(?:_URL)?:\\s*[^\\]]+\\]`` (Sonar python:S2631).
    """
    if not content:
        return content
    parts: List[str] = []
    i = 0
    n = len(content)
    while i < n:
        start_img = content.find(_IMAGE_TAG_PREFIX, i)
        start_url = content.find(_IMAGE_URL_TAG_PREFIX, i)
        if start_img < 0 and start_url < 0:
            parts.append(content[i:])
            break
        if start_url < 0 or (start_img >= 0 and start_img < start_url):
            start = start_img
            prefix_len = len(_IMAGE_TAG_PREFIX)
        else:
            start = start_url
            prefix_len = len(_IMAGE_URL_TAG_PREFIX)

        body_start = start + prefix_len
        end = content.find("]", body_start)
        # Former regex required [^\\]]+ (at least one character before ']').
        if end < 0 or end == body_start:
            parts.append(content[i : start + 1])
            i = start + 1
            continue
        parts.append(content[i:start])
        i = end + 1
    return "".join(parts)


def strip_invalid_image_url_tags(content: str, keep_url) -> str:
    """keep_url(url) -> bool. Removes tags where keep_url returns False."""
    if not content:
        return content
    parts: List[str] = []
    i = 0
    plen = len(_IMAGE_URL_TAG_PREFIX)
    while True:
        start = content.find(_IMAGE_URL_TAG_PREFIX, i)
        if start < 0:
            parts.append(content[i:])
            break
        parts.append(content[i:start])
        j = start + plen
        while j < len(content) and content[j] in " \t":
            j += 1
        end = content.find("]", j)
        if end < 0:
            parts.append(content[start:])
            break
        url = content[j:end].strip()
        if keep_url(url):
            parts.append(content[start : end + 1])
        i = end + 1
    return "".join(parts)


def strip_non_api_markdown_images(content: str) -> str:
    if not content:
        return content
    parts: List[str] = []
    i = 0
    while True:
        start = content.find("![", i)
        if start < 0:
            parts.append(content[i:])
            break
        parts.append(content[i:start])
        alt_end = content.find("](", start + 2)
        if alt_end < 0:
            parts.append(content[start:])
            break
        alt = content[start + 2 : alt_end]
        url_start = alt_end + 2
        url_end = content.find(")", url_start)
        if url_end < 0:
            parts.append(content[start:])
            break
        src = content[url_start:url_end].strip()
        if src.startswith("/api/images/"):
            parts.append(content[start : url_end + 1])
        i = url_end + 1
    return "".join(parts)


def replace_image_tags(content: str, build_replacement: Callable[[str], str]) -> str:
    if not content:
        return content
    parts: List[str] = []
    i = 0
    plen = len(_IMAGE_TAG_PREFIX)
    while True:
        start = content.find(_IMAGE_TAG_PREFIX, i)
        if start < 0:
            parts.append(content[i:])
            break
        parts.append(content[i:start])
        j = start + plen
        while j < len(content) and content[j] in " \t":
            j += 1
        end = content.find("]", j)
        if end < 0:
            parts.append(content[start:])
            break
        key = content[j:end].strip()
        parts.append(build_replacement(key))
        i = end + 1
    return "".join(parts)


_META_LINE_PREFIXES = (
    "the user",
    "the user's",
    "this is an informational",
    "i need to ",
    "i will ",
    "i should ",
    "let me ",
    "for this question",
    "for this query",
    "based on the user's question",
    "based on the learner's question",
)


def _is_meta_first_line(line: str) -> bool:
    lowered = line.strip().lower()
    if not lowered:
        return False
    return any(lowered.startswith(prefix) for prefix in _META_LINE_PREFIXES)


def _first_non_empty_line(paragraph: str) -> str:
    for line in paragraph.split("\n"):
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _split_once_on_blank_lines(text: str) -> tuple[str, str] | None:
    i = 0
    length = len(text)
    while i < length:
        if i + 1 < length and text[i] == "\n" and text[i + 1] == "\n":
            j = i + 2
            while j < length and text[j] == "\n":
                j += 1
            return text[:i], text[j:]
        i += 1
    return None


def _strip_tool_completion_prefix(text: str) -> str:
    """Remove leading agent tool-call completion lines without regex backtracking."""
    lowered = text.lower()
    marker = "completed in "
    idx = lowered.find(marker)
    if idx < 0:
        return text
    i = idx + len(marker)
    length = len(text)
    while i < length and text[i].isspace():
        i += 1
    while i < length and text[i].isdigit():
        i += 1
    if i < length and text[i] == ".":
        i += 1
        while i < length and text[i].isdigit():
            i += 1
    if i + 1 < length and lowered[i : i + 2] == "s.":
        i += 2
        while i < length and text[i].isspace():
            i += 1
        return text[i:]
    return text


def _find_heading_break_index(text: str) -> int:
    i = 0
    length = len(text)
    while i < length:
        if i + 1 < length and text[i] == "\n" and text[i + 1] == "\n":
            j = i + 2
            while j < length and text[j] == "\n":
                j += 1
            if j < length:
                if text[j] == "#":
                    return i
                if j + 1 < length and text[j] == "*" and text[j + 1] == "*":
                    if text[j + 2].isalpha():
                        return i
        i += 1
    return -1


def _strip_leading_meta_lines(text: str) -> str:
    """
    Drop leading reasoning LINES that the model did not follow with a blank line.

    ``_split_once_on_blank_lines`` only sees a preamble the model ended with a blank line.
    In practice the answer often arrives as ``The user is asking ... I need to translate it
    into Hindi.\\n<answer>`` - one newline, no blank line - so the paragraph pass found
    nothing to split on and the reasoning stayed visible in the chat bubble.

    Only whole leading lines are removed, and only while substantive content remains after
    them, so a response that is nothing but a meta line is returned untouched rather than
    emptied. Plain index scanning, no regex, so there is no backtracking risk
    (Sonar python:S2631).
    """
    cleaned = text
    while True:
        newline = cleaned.find("\n")
        if newline < 0:
            # Single line left: it is the answer, however it happens to start.
            break
        first_line = cleaned[:newline]
        remainder = cleaned[newline + 1:]
        if not _is_meta_first_line(first_line) or not remainder.strip():
            break
        cleaned = remainder.lstrip("\n")
    return cleaned


def strip_agent_meta_commentary(text: str) -> str:
    """Remove leaked agent reasoning before the substantive answer."""
    # Phani | 25-Jun-2026 | DF_27 | Trainee_study | Low
    # Fix: Strip meta preamble from chat and raise-hand responses before save/load.
    if not text:
        return text

    cleaned = _strip_tool_completion_prefix(text).lstrip()
    if not cleaned:
        return cleaned

    for _ in range(6):
        changed = False

        while True:
            parts = _split_once_on_blank_lines(cleaned)
            if not parts:
                break
            first_line = _first_non_empty_line(parts[0])
            if first_line and _is_meta_first_line(first_line):
                cleaned = parts[1].lstrip()
                changed = True
            else:
                break

        heading_at = _find_heading_break_index(cleaned)
        if heading_at > 0:
            prefix = cleaned[:heading_at]
            first_line = _first_non_empty_line(prefix)
            if first_line and _is_meta_first_line(first_line):
                cleaned = cleaned[heading_at:].lstrip()
                changed = True

        # Last, so the paragraph passes above keep first refusal on a multi-line preamble;
        # this one catches the preamble that runs straight into the answer on the next line.
        line_stripped = _strip_leading_meta_lines(cleaned)
        if line_stripped != cleaned:
            cleaned = line_stripped
            changed = True

        if not changed:
            break

    return cleaned.strip()
