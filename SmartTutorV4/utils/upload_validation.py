"""
Strict upload validation for VAPT: Double Extension File Upload.

- Allow-list final extension only
- Reject dangerous intermediate extensions (e.g. file.exe.pdf, file.php.jpg)
- Verify content via magic bytes / structure (never trust client Content-Type alone)
- Normalize storage names to a single safe extension (randomized)
"""
from __future__ import annotations

import os
import uuid
import zipfile
from io import BytesIO
from typing import FrozenSet, Optional, Tuple

# Intermediate / executable / script extensions that must never appear before the final ext.
DANGEROUS_EXTENSIONS: FrozenSet[str] = frozenset(
    {
        "exe",
        "bat",
        "cmd",
        "com",
        "scr",
        "pif",
        "msi",
        "msp",
        "dll",
        "sys",
        "drv",
        "js",
        "jse",
        "mjs",
        "vbs",
        "vbe",
        "ws",
        "wsf",
        "wsh",
        "ps1",
        "psm1",
        "psd1",
        "php",
        "php3",
        "php4",
        "php5",
        "phtml",
        "phar",
        "asp",
        "aspx",
        "ashx",
        "asmx",
        "jsp",
        "jspx",
        "cgi",
        "pl",
        "py",
        "rb",
        "sh",
        "bash",
        "zsh",
        "jar",
        "war",
        "ear",
        "class",
        "hta",
        "cpl",
        "msc",
        "reg",
        "inf",
        "lnk",
        "url",
        "iso",
        "img",
        "vhd",
        "vhdx",
        "apk",
        "deb",
        "rpm",
        "html",
        "htm",
        "shtml",
        "svg",  # XSS vector if served inline as image
        "swf",
    }
)

MODULE_DOC_EXTENSIONS: FrozenSet[str] = frozenset(
    {"pdf", "docx", "doc", "pptx", "ppt", "png", "jpg", "jpeg"}
)

VOICE_SCENARIO_EXTENSIONS: FrozenSet[str] = frozenset(
    {"pdf", "docx", "txt", "md", "csv", "wav", "mp3", "m4a", "flac", "ogg", "amr", "webm"}
)

VOICE_AUDIO_EXTENSIONS: FrozenSet[str] = frozenset(
    {"wav", "mp3", "m4a", "flac", "ogg", "amr", "webm"}
)

CONTENT_TYPE_BY_EXT = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "doc": "application/msword",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "ppt": "application/vnd.ms-powerpoint",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
}

_MAX_MODULE_DOC_BYTES = 100 * 1024 * 1024  # 100 MB
_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def split_extensions(filename: Optional[str]) -> Tuple[str, list[str]]:
    """
    Return (basename_stem_without_exts, list_of_extension_segments_lower).

    Example: 'dummy-pdf.exe.pdf' -> ('dummy-pdf', ['exe', 'pdf'])
    """
    base = os.path.basename((filename or "").strip())
    if not base or base in {".", ".."}:
        return "", []
    # Drop trailing dots / spaces (Windows ADS / empty-ext tricks)
    base = base.rstrip(" .")
    if not base or "." not in base:
        return base, []
    parts = base.split(".")
    stem = parts[0]
    exts = [p.lower() for p in parts[1:] if p]
    return stem, exts


def final_extension(filename: Optional[str]) -> str:
    _, exts = split_extensions(filename)
    return exts[-1] if exts else ""


def has_dangerous_double_extension(filename: Optional[str]) -> bool:
    """
    True when any non-final extension segment is dangerous
    (e.g. malware.exe.pdf, shell.php.jpg).
    """
    _, exts = split_extensions(filename)
    if len(exts) < 2:
        return False
    return any(seg in DANGEROUS_EXTENSIONS for seg in exts[:-1])


def filename_has_null_or_path(filename: Optional[str]) -> bool:
    raw = filename or ""
    if "\x00" in raw:
        return True
    if "/" in raw or "\\" in raw:
        # basename-only callers should strip first; reject raw pathing
        if os.path.basename(raw) != raw.replace("\\", "/").split("/")[-1]:
            return True
    return False


def is_pdf_magic(data: bytes) -> bool:
    return bool(data) and data[:4] == b"%PDF"


def is_png_magic(data: bytes) -> bool:
    return bool(data) and data[:8] == b"\x89PNG\r\n\x1a\n"


def is_jpeg_magic(data: bytes) -> bool:
    return bool(data) and len(data) >= 3 and data[:3] == b"\xff\xd8\xff"


def is_ole_magic(data: bytes) -> bool:
    """Legacy .doc / .ppt (OLE Compound Document)."""
    return bool(data) and len(data) >= 8 and data[:8] == _OLE_MAGIC


def is_docx_magic(data: bytes) -> bool:
    if not data or len(data) < 4 or data[:2] != b"PK":
        return False
    try:
        with zipfile.ZipFile(BytesIO(data)) as archive:
            names = archive.namelist()
            if not names:
                return False
            return "[Content_Types].xml" in names and any(
                n.startswith("word/") for n in names
            )
    except (zipfile.BadZipFile, OSError):
        return False


def is_pptx_magic(data: bytes) -> bool:
    if not data or len(data) < 4 or data[:2] != b"PK":
        return False
    try:
        with zipfile.ZipFile(BytesIO(data)) as archive:
            names = archive.namelist()
            if not names:
                return False
            return "[Content_Types].xml" in names and any(
                n.startswith("ppt/") for n in names
            )
    except (zipfile.BadZipFile, OSError):
        return False


def content_matches_extension(ext: str, data: bytes) -> bool:
    ext = (ext or "").lower()
    if ext == "pdf":
        return is_pdf_magic(data)
    if ext == "png":
        return is_png_magic(data)
    if ext in {"jpg", "jpeg"}:
        return is_jpeg_magic(data)
    if ext == "docx":
        return is_docx_magic(data)
    if ext == "pptx":
        return is_pptx_magic(data)
    if ext in {"doc", "ppt"}:
        return is_ole_magic(data)
    return False


def randomized_storage_name(validated_ext: str) -> str:
    """Single-extension storage name outside any user-controlled deception."""
    ext = (validated_ext or "bin").lower().lstrip(".")
    if ext not in MODULE_DOC_EXTENSIONS:
        ext = "bin"
    return f"{uuid.uuid4().hex}.{ext}"


def safe_content_disposition(
    filename: str,
    *,
    disposition: str = "inline",
) -> str:
    """Build a header-safe Content-Disposition (ASCII filename only)."""
    base = os.path.basename(filename or "download")
    cleaned: list[str] = []
    for ch in base:
        if ch.isalnum() or ch in "._-":
            cleaned.append(ch)
    name = "".join(cleaned).strip("._") or "download"
    # Collapse to a single final extension segment in the header value
    stem, exts = split_extensions(name)
    if exts:
        ext = exts[-1]
        # Defence in depth: never hand the browser a dangerous download name, even if
        # a deceptive value somehow reached storage (VAPT: Double Extension File Upload).
        if ext in DANGEROUS_EXTENSIONS:
            ext = "bin"
        name = f"{stem or 'download'}.{ext}"
    else:
        name = stem or "download"
    disp = "attachment" if disposition.lower() == "attachment" else "inline"
    return f'{disp}; filename="{name}"'


def validate_module_document_upload(
    filename: Optional[str],
    data: bytes,
    *,
    max_bytes: int = _MAX_MODULE_DOC_BYTES,
) -> Tuple[bool, str, str, str]:
    """
    Validate a module-generation upload.

    Returns:
        (ok, error_message, storage_filename, canonical_content_type)
    """
    if not filename or not str(filename).strip():
        return False, "Filename is missing.", "", ""

    raw_name = os.path.basename(str(filename).strip())
    if filename_has_null_or_path(raw_name) or "\x00" in (filename or ""):
        return False, "Invalid filename.", "", ""

    if has_dangerous_double_extension(raw_name):
        return (
            False,
            (
                "Rejected deceptive filename "
                f"'{raw_name}'. Names with executable/script extensions "
                "(for example .exe.pdf or .php.jpg) are not allowed."
            ),
            "",
            "",
        )

    ext = final_extension(raw_name)
    if ext not in MODULE_DOC_EXTENSIONS:
        return (
            False,
            (
                f"Unsupported file format: {ext or '(none)'}. "
                "Supported formats: PDF, DOCX, DOC, PPTX, PPT, PNG, JPG, JPEG"
            ),
            "",
            "",
        )

    # Final segment itself must never be a dangerous type (defence in depth)
    if ext in DANGEROUS_EXTENSIONS:
        return False, f"File type '.{ext}' is not allowed.", "", ""

    if not data:
        return False, "The uploaded file is empty.", "", ""

    if len(data) > max_bytes:
        return False, "File is too large (maximum 100 MB).", "", ""

    if not content_matches_extension(ext, data):
        return (
            False,
            (
                f"File content does not match the declared '.{ext}' type. "
                "Upload a genuine PDF, Office document, or image."
            ),
            "",
            "",
        )

    storage_name = randomized_storage_name(ext)
    content_type = CONTENT_TYPE_BY_EXT.get(ext, "application/octet-stream")
    return True, "", storage_name, content_type


def assert_safe_upload_filename(filename: Optional[str], allowed_exts: FrozenSet[str]) -> Tuple[bool, str]:
    """
    Lightweight name-only gate for other upload paths (video, voice, etc.).
    Still rejects dangerous double extensions; magic checks stay in those modules.
    """
    if not filename or not str(filename).strip():
        return False, "Filename is missing."
    raw_name = os.path.basename(str(filename).strip())
    if "\x00" in raw_name:
        return False, "Invalid filename."
    if has_dangerous_double_extension(raw_name):
        return (
            False,
            (
                "Rejected deceptive filename. "
                "Names with executable/script extensions "
                "(for example .exe.pdf or .php.jpg) are not allowed."
            ),
        )
    ext = final_extension(raw_name)
    if ext not in allowed_exts:
        return False, f"Unsupported file format: .{ext or '(none)'}."
    if ext in DANGEROUS_EXTENSIONS:
        return False, f"File type '.{ext}' is not allowed."
    return True, ""


def _looks_like_executable_payload(data: bytes) -> bool:
    """True for common PE / ELF / Mach-O / script shebang headers."""
    if not data:
        return False
    if data[:2] == b"MZ":
        return True
    if data[:4] == b"\x7fELF":
        return True
    if data[:4] in {b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf", b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe"}:
        return True
    # UTF-8 / ASCII shebang scripts
    head = data[:64].lstrip()
    return head.startswith(b"#!")


def validate_voice_scenario_upload(
    filename: Optional[str],
    data: bytes,
) -> Tuple[bool, str]:
    """
    Voice Training scenario generate-rich uploads.

    Name allow-list + double-extension reject, then content checks:
    - PDF/DOCX: magic / ZIP structure (same as module docs)
    - Text/CSV/MD: reject executable masquerades
    - Audio: reject PDF/image/executable payloads disguised as audio
    """
    name_ok, name_err = assert_safe_upload_filename(filename, VOICE_SCENARIO_EXTENSIONS)
    if not name_ok:
        return False, name_err
    if not data:
        return False, "The uploaded file is empty."

    ext = final_extension(filename)
    if ext in {"pdf", "docx"}:
        if not content_matches_extension(ext, data):
            return (
                False,
                f"File content does not match the declared '.{ext}' type.",
            )
        return True, ""

    if ext in {"txt", "md", "csv"}:
        if _looks_like_executable_payload(data):
            return False, "Text uploads cannot contain executable content."
        return True, ""

    # Audio extensions — reject common non-audio containers / executables
    if _looks_like_executable_payload(data):
        return False, "Audio uploads cannot contain executable content."
    if is_pdf_magic(data) or is_png_magic(data) or is_jpeg_magic(data):
        return False, "Audio uploads must be genuine audio, not a document or image."
    if ext == "wav":
        if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
            return False, "The selected file is not a valid WAV recording."
    return True, ""
