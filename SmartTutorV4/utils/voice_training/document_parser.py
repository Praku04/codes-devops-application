"""Extract plain text from uploaded training documents (PDF, DOCX, TXT)."""

from io import BytesIO

MAX_EXTRACT_CHARS = 14_000
TEXT_TRANSCRIPT_EXTENSIONS = frozenset({"txt", "md", "csv"})


def is_text_transcript_filename(filename: str) -> bool:
    ext = (filename.rsplit(".", 1)[-1] if "." in filename else "").lower()
    return ext in TEXT_TRANSCRIPT_EXTENSIONS


def extract_text_from_bytes(content: bytes, filename: str) -> str:
    ext = (filename.rsplit(".", 1)[-1] if "." in filename else "").lower()
    if ext in ("txt", "md", "csv"):
        return _decode_text(content)
    if ext == "pdf":
        return _extract_pdf(content)
    if ext == "docx":
        return _extract_docx(content)
    raise ValueError(
        f"Unsupported file type: .{ext}. Use PDF, DOCX, TXT, MD, CSV, or audio (WAV, MP3)."
    )


def _decode_text(content: bytes) -> str:
    for encoding in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace")


def _extract_pdf(content: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(BytesIO(content))
    parts: list[str] = []
    for page in reader.pages:
        text = page.extract_text() or ""
        if text.strip():
            parts.append(text.strip())
    return "\n\n".join(parts)


def _extract_docx(content: bytes) -> str:
    from docx import Document

    doc = Document(BytesIO(content))
    parts = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
    return "\n".join(parts)


def combine_document_texts(extracted: list[tuple[str, str]]) -> str:
    if not extracted:
        return ""
    blocks: list[str] = []
    total = 0
    for name, text in extracted:
        # Preserve line breaks so Agent/Customer labels survive for transcript parsing.
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        cleaned = "\n".join(lines)
        if not cleaned:
            continue
        header = f"--- {name} ---\n"
        remaining = MAX_EXTRACT_CHARS - total - len(header)
        if remaining <= 0:
            break
        snippet = cleaned[:remaining]
        blocks.append(header + snippet)
        total += len(header) + len(snippet)
    return "\n\n".join(blocks)
