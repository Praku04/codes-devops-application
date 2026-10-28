"""Document parsers for PDF, DOCX, spreadsheets (CSV/XLSX/XLS), text, and images (PNG/JPEG)."""

from __future__ import annotations

import asyncio
import base64
import csv
import datetime as dt
import io
import logging
import re
import uuid
from pathlib import Path
from typing import Any, Iterable, Sequence

from app.core.exceptions import AppError

logger = logging.getLogger("agentcraft.parser")

#: Tabular uploads. A BRD's requirement matrix, an interface list or a backlog arrives as a
#: spreadsheet at least as often as prose, and every sheet in a workbook is content -- "Sheet2"
#: is where the field mappings usually live.
SHEET_EXTENSIONS = {".csv", ".tsv", ".xlsx", ".xlsm", ".xls"}
TEXT_EXTENSIONS = {".pdf", ".docx", ".md", ".markdown", ".txt"} | SHEET_EXTENSIONS
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg"}
ALLOWED_EXTENSIONS = TEXT_EXTENSIONS | IMAGE_EXTENSIONS

#: One spelling of the allowed list, because it appears in three error messages here and in the
#: UI, the CLI and the wizard's copy -- a list that drifts between them tells the user a format
#: is unsupported while the parser happily reads it.
ALLOWED_LABEL = "png, jpeg, pdf, docx, md, txt, csv, tsv, xlsx, xls"

#: Caps for tabular extraction. A spreadsheet can be far larger than any prose document -- an
#: export with 200k rows would otherwise be read into memory, stored in the project row and sent
#: at prompts that cap at ~28k chars anyway. Per *sheet*, so a workbook of small sheets keeps all
#: of them, and the cut is announced in the text rather than passing off a slice as the whole file.
MAX_SHEET_ROWS = 2000
MAX_ROW_CELLS = 100
MAX_CELL_CHARS = 500

_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}

_CONTENT_TYPES = {
    **_MIME,
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".txt": "text/plain",
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xlsm": "application/vnd.ms-excel.sheet.macroEnabled.12",
    ".xls": "application/vnd.ms-excel",
}


def guess_content_type(filename: str) -> str:
    """MIME type for an upload, by extension. Empty string when unknown."""
    return _CONTENT_TYPES.get(Path(filename).suffix.lower(), "")


def is_image(filename: str) -> bool:
    return Path(filename).suffix.lower() in IMAGE_EXTENSIONS


def parse_file(path: Path) -> str:
    ext = path.suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise AppError(
            f"Unsupported file type: {ext}. Allowed: {ALLOWED_LABEL}",
            status_code=400,
        )
    if ext in IMAGE_EXTENSIONS:
        raise AppError(
            "Image text extraction requires async parse (OCR/vision)",
            status_code=500,
        )
    if ext == ".pdf":
        return _parse_pdf(path)
    if ext == ".docx":
        return _parse_docx(path)
    if ext in SHEET_EXTENSIONS:
        # Read as bytes, not text: the sheet parsers do their own decoding (a CSV saved by Excel
        # is cp1252 with a BOM far more often than it is UTF-8) and the xlsx/xls readers take the
        # raw bytes, so there is one code path whether the upload arrived as a file or in memory.
        return _parse_sheet(path.name, path.read_bytes())
    return path.read_text(encoding="utf-8", errors="replace")


def parse_bytes(filename: str, data: bytes) -> str:
    """Sync parse for text-based uploads (not images)."""
    ext = Path(filename).suffix.lower()
    if ext in IMAGE_EXTENSIONS:
        raise AppError(
            f"Image '{filename}' must be processed with vision OCR",
            status_code=400,
        )
    if ext not in TEXT_EXTENSIONS:
        raise AppError(
            f"Unsupported file type: {ext}. Allowed: {ALLOWED_LABEL}",
            status_code=400,
        )
    # md/txt need no temp file at all — skip two disk round-trips.
    if ext in {".md", ".markdown", ".txt"}:
        return data.decode("utf-8", errors="replace")
    # Spreadsheets likewise: csv is decoded in memory, and both Excel readers accept bytes.
    if ext in SHEET_EXTENSIONS:
        return _parse_sheet(filename, data)
    # Unique temp name: concurrent uploads of the same filename used to
    # overwrite and unlink each other's scratch file.
    tmp = Path("uploads") / f"_tmp_{uuid.uuid4().hex}_{Path(filename).name}"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_bytes(data)
    try:
        return parse_file(tmp)
    finally:
        tmp.unlink(missing_ok=True)


async def extract_upload_text(filename: str, data: bytes) -> str:
    """Extract full text from any allowed upload (including PNG/JPEG via vision)."""
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise AppError(
            f"Unsupported file type: {ext}. Allowed: {ALLOWED_LABEL}",
            status_code=400,
        )
    if ext in IMAGE_EXTENSIONS:
        return await _ocr_image(filename, data)
    # pdfplumber / python-docx / openpyxl are blocking and CPU-bound. Off-thread so several
    # uploads actually extract concurrently instead of serialising the event loop.
    text = await asyncio.to_thread(parse_bytes, filename, data)
    return (text or "").strip()


async def _ocr_image(filename: str, data: bytes) -> str:
    """Read all visible text from an image using the configured Bedrock vision model."""
    from app.services.llm.client import llm_client

    ext = Path(filename).suffix.lower()
    mime = _MIME.get(ext, "image/png")
    b64 = base64.b64encode(data).decode("ascii")
    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": (
                        f"The attached image is an uploaded project document named '{filename}'. "
                        "Extract ALL readable text completely (headings, paragraphs, tables, labels, "
                        "lists, footnotes). Preserve reading order. Do not summarize. "
                        "If there is no text, reply with exactly: [NO_TEXT_FOUND]"
                    ),
                },
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{b64}"},
                },
            ],
        }
    ]
    try:
        raw = await llm_client.complete(messages, temperature=0.0, max_tokens=8192)
    except AppError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("Image OCR failed for %s", filename)
        raise AppError(
            f"Could not read text from image {filename}: {exc}",
            status_code=502,
        ) from exc

    text = (raw or "").strip()
    if not text or "[NO_TEXT_FOUND]" in text.upper().replace(" ", ""):
        raise AppError(
            f"No readable text found in image '{filename}'. "
            "Upload a clearer image or a PDF/DOCX/MD/TXT instead.",
            status_code=400,
        )
    return text


def _parse_pdf(path: Path) -> str:
    try:
        import pdfplumber

        chunks: list[str] = []
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                text = page.extract_text() or ""
                if text.strip():
                    chunks.append(text)
        if chunks:
            return "\n\n".join(chunks)
    except Exception as exc:
        logger.warning("pdfplumber failed, falling back to pypdf: %s", exc)

    from pypdf import PdfReader

    reader = PdfReader(str(path))
    return "\n\n".join((page.extract_text() or "") for page in reader.pages)


def _decode_text(data: bytes) -> str:
    """Text from bytes that may not be UTF-8.

    "Save as CSV" in Excel writes cp1252 with a BOM, so strict UTF-8 raises on the first accented
    character or smart quote in a header -- a whole upload rejected over punctuation. utf-8-sig
    strips the BOM when there is one; latin-1 cannot fail on any byte, so this always returns.
    """
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _cell_text(value: Any) -> str:
    """One cell as text: readable, single-line, and without spreadsheet artefacts.

    `1.0` is written back as `1` because openpyxl returns every number as a float and a
    requirement id reading "REQ-1.0" is noise. Dates arrive as datetimes and become ISO strings,
    which is both unambiguous and what the downstream prompts expect.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, dt.datetime):
        # A date-only cell still arrives as a datetime at midnight, so `2026-09-15 00:00:00` is
        # how every due-date column would read. Print the time only when there is one.
        return value.date().isoformat() if value.time() == dt.time(0) else value.isoformat(sep=" ")
    if isinstance(value, (dt.date, dt.time)):
        return value.isoformat()
    text = re.sub(r"\s*\n\s*", " ", str(value)).strip()
    return text[:MAX_CELL_CHARS]


def _rows_to_text(rows: Iterable[Sequence[Any]], *, label: str) -> str:
    """Rows as `a | b | c` lines under a heading, or "" when the sheet holds nothing.

    Pipe-separated to match how `_parse_docx` already renders Word tables, so the deduction
    prompts see one table shape whatever the upload was. Trailing empty cells are dropped and
    blank rows skipped: spreadsheets are full of formatting-only rows and columns that would
    otherwise arrive as `| | | |` and spend context saying nothing.
    """
    lines: list[str] = []
    truncated = False
    for row in rows:
        if len(lines) >= MAX_SHEET_ROWS:
            truncated = True
            break
        cells = [_cell_text(cell) for cell in list(row)[:MAX_ROW_CELLS]]
        while cells and not cells[-1]:
            cells.pop()
        if not any(cells):
            continue
        lines.append(" | ".join(cells))
    if not lines:
        return ""
    out = [f"## {label}", *lines]
    if truncated:
        out.append(f"[... truncated: first {MAX_SHEET_ROWS} non-empty rows of '{label}' shown]")
    return "\n".join(out)


def _parse_sheet(filename: str, data: bytes) -> str:
    """Full text of a CSV/TSV file or of *every* sheet in a workbook."""
    ext = Path(filename).suffix.lower()
    if ext in {".csv", ".tsv"}:
        return _parse_csv(filename, data, ext)
    if ext == ".xls":
        return _parse_xls(filename, data)
    return _parse_xlsx(filename, data)


def _parse_csv(filename: str, data: bytes, ext: str) -> str:
    text = _decode_text(data)
    delimiter = "\t"
    if ext != ".tsv":
        # Sniff, because a "CSV" exported on a machine with a comma decimal separator is
        # semicolon-delimited, and reading it as one column per row loses every column boundary.
        try:
            delimiter = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|").delimiter
        except csv.Error:
            delimiter = ","  # a single-column file has no delimiter to find; comma is harmless
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    return _rows_to_text(reader, label=Path(filename).name)


def _parse_xlsx(filename: str, data: bytes) -> str:
    """Every sheet of an .xlsx/.xlsm workbook, in tab order, each under its own heading."""
    try:
        from openpyxl import load_workbook
    except ImportError as exc:  # pragma: no cover - dependency is in requirements.txt
        raise AppError(
            "Reading .xlsx needs openpyxl: pip install -r backend/requirements.txt",
            status_code=500,
        ) from exc

    try:
        # data_only: cached formula *results*, not `=SUM(B2:B9)` — a totals column is a number to
        # anyone reading the document. read_only keeps a large export off the heap.
        workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001 - openpyxl raises a zoo of types on a bad file
        raise AppError(
            f"Could not read spreadsheet '{filename}': {exc}. "
            "If it is an older .xls, re-save it as .xlsx.",
            status_code=400,
        ) from exc

    blocks: list[str] = []
    try:
        for sheet in workbook.worksheets:
            # Hidden sheets included on purpose: "old requirements" hidden from a reviewer is
            # still content the user uploaded, and skipping it silently loses part of the file.
            block = _rows_to_text(sheet.iter_rows(values_only=True), label=f"Sheet: {sheet.title}")
            if block:
                blocks.append(block)
    finally:
        workbook.close()

    if not blocks:
        logger.info("Workbook %s has no non-empty cells in any of its sheets", filename)
    return "\n\n".join(blocks)


def _parse_xls(filename: str, data: bytes) -> str:
    """Every sheet of a legacy .xls workbook (xlrd 2.x reads this format and only this one)."""
    try:
        import xlrd
    except ImportError as exc:  # pragma: no cover - dependency is in requirements.txt
        raise AppError(
            "Reading .xls needs xlrd: pip install -r backend/requirements.txt",
            status_code=500,
        ) from exc

    try:
        book = xlrd.open_workbook(file_contents=data)
    except Exception as exc:  # noqa: BLE001
        raise AppError(
            f"Could not read spreadsheet '{filename}': {exc}. Re-save it as .xlsx and retry.",
            status_code=400,
        ) from exc

    blocks: list[str] = []
    for sheet in book.sheets():
        rows = (sheet.row_values(index) for index in range(sheet.nrows))
        block = _rows_to_text(rows, label=f"Sheet: {sheet.name}")
        if block:
            blocks.append(block)
    return "\n\n".join(blocks)


def _parse_docx(path: Path) -> str:
    from docx import Document

    doc = Document(str(path))
    parts: list[str] = []
    for p in doc.paragraphs:
        if p.text.strip():
            parts.append(p.text)
    # Tables are often where requirements live — include them fully
    for table in doc.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text and c.text.strip()]
            if cells:
                parts.append(" | ".join(cells))
    return "\n".join(parts)
