from __future__ import annotations

import subprocess
from pathlib import Path

# Raised when a request waits pool_timeout for a connection and none frees up; the global
# exception handler below turns it into a 503 instead of an opaque 500.
from sqlalchemy.exc import TimeoutError as DBPoolTimeoutError

from core.runtime import *
from utils.security_helpers import GENERIC_API_ERROR_MESSAGE, log_safe


def convert_docx_to_pdf(docx_content: bytes) -> bytes:
    """Convert DOCX content to PDF with enhanced image and formatting preservation."""
    if not DOCX_AVAILABLE:
        raise HTTPException(status_code=500, detail="DOCX processing not available")

    try:
        logger.info("Attempting enhanced COM automation for DOCX to PDF conversion")
        with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as docx_temp:
            docx_temp.write(docx_content)
            docx_path = docx_temp.name

        pdf_path = docx_path.replace(".docx", ".pdf")

        try:
            if COMTYPES_AVAILABLE and os.name == "nt":
                try:
                    import comtypes

                    comtypes.CoInitialize()
                    try:
                        word = comtypes.client.CreateObject("Word.Application")
                        word.Visible = False
                        doc = word.Documents.Open(docx_path)
                        doc.ExportAsFixedFormat(
                            OutputFileName=pdf_path,
                            ExportFormat=17,
                            OpenAfterExport=False,
                            OptimizeFor=0,
                            BitmapMissingFonts=True,
                            DocStructureTags=True,
                            CreateBookmarks=0,
                            UseISO19005_1=False,
                            IncludeStructureTags=True,
                            IncludeMarkup=True,
                            JPEGQuality=0,
                        )
                        doc.Close()
                        word.Quit()
                        with open(pdf_path, "rb") as pdf_file:
                            return pdf_file.read()
                    finally:
                        try:
                            comtypes.CoUninitialize()
                        except Exception:
                            pass
                except Exception as com_error:
                    logger.warning("COM conversion failed: %s, falling back to enhanced extraction", com_error)

            logger.info("Using enhanced fallback method for DOCX to PDF conversion with image extraction")
            from PIL import Image as PILImage
            from reportlab.lib import colors
            from reportlab.lib.enums import TA_JUSTIFY, TA_LEFT
            from reportlab.lib.pagesizes import A4
            from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
            from reportlab.lib.units import inch
            from reportlab.platypus import Image as RLImage
            from reportlab.platypus import ListFlowable, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

            doc = Document(io.BytesIO(docx_content))
            pdf_buffer = io.BytesIO()
            pdf_doc = SimpleDocTemplate(
                pdf_buffer,
                pagesize=A4,
                leftMargin=72,
                rightMargin=72,
                topMargin=72,
                bottomMargin=72,
            )
            styles = getSampleStyleSheet()
            story = []

            title_style = ParagraphStyle(
                "CustomTitle",
                parent=styles["Heading1"],
                fontSize=18,
                spaceAfter=16,
                spaceBefore=12,
                textColor=colors.black,
                fontName="Helvetica-Bold",
                alignment=TA_LEFT,
                leading=22,
            )
            heading2_style = ParagraphStyle(
                "CustomHeading2",
                parent=styles["Heading2"],
                fontSize=15,
                spaceAfter=12,
                spaceBefore=10,
                textColor=colors.black,
                fontName="Helvetica-Bold",
                alignment=TA_LEFT,
                leading=18,
            )
            normal_style = ParagraphStyle(
                "CustomNormal",
                parent=styles["Normal"],
                fontSize=12,
                spaceAfter=10,
                textColor=colors.black,
                fontName="Helvetica",
                alignment=TA_JUSTIFY,
                leading=16,
            )
            list_style = ParagraphStyle(
                "CustomList",
                parent=styles["Normal"],
                fontSize=12,
                spaceAfter=6,
                leftIndent=20,
                textColor=colors.black,
                fontName="Helvetica",
                alignment=TA_LEFT,
                leading=16,
            )

            current_list_items = []
            list_format = None

            for para in doc.paragraphs:
                if not para.text.strip():
                    continue

                is_list_item = False
                if para.text.strip() and (para.text.strip()[0].isdigit() and "." in para.text[:5]):
                    is_list_item = True
                    list_format = "decimal"
                elif para.style.name in ["List Bullet", "List Paragraph"] or para.text.strip().startswith("•"):
                    is_list_item = True
                    list_format = "bullet"

                def _build_formatted_text():
                    formatted_text = ""
                    for run in para.runs:
                        text = run.text
                        if run.bold or para.style.name == "Title":
                            formatted_text += f"<b>{text}</b>"
                        elif run.italic:
                            formatted_text += f"<i>{text}</i>"
                        else:
                            formatted_text += text
                    return formatted_text or para.text

                if para.style.name.startswith("Heading 1") or para.style.name == "Title":
                    story.append(Paragraph(_build_formatted_text(), title_style))
                elif para.style.name.startswith("Heading"):
                    story.append(Paragraph(_build_formatted_text(), heading2_style))
                elif is_list_item:
                    formatted_text = _build_formatted_text()
                    if list_format == "decimal" and formatted_text and formatted_text[0].isdigit():
                        formatted_text = formatted_text.split(".", 1)[1].strip() if "." in formatted_text else formatted_text
                    elif formatted_text.startswith("•"):
                        formatted_text = formatted_text[1:].strip()
                    current_list_items.append(Paragraph(formatted_text, list_style))
                else:
                    if current_list_items:
                        bullet_type = "1" if list_format == "decimal" else "bullet"
                        story.append(ListFlowable(current_list_items, bulletType=bullet_type, start=1))
                        story.append(Spacer(1, 6))
                        current_list_items = []
                    story.append(Paragraph(_build_formatted_text(), normal_style))
                    story.append(Spacer(1, 4))

            if current_list_items:
                bullet_type = "1" if list_format == "decimal" else "bullet"
                story.append(ListFlowable(current_list_items, bulletType=bullet_type, start=1))
                story.append(Spacer(1, 6))

            temp_image_dir = tempfile.mkdtemp()
            try:
                extracted_images = []
                image_count = 0
                for rel in doc.part.rels.values():
                    if "image" not in rel.target_ref:
                        continue
                    try:
                        image_data = rel.target_part.blob
                        image_ext = rel.target_ref.split(".")[-1]
                        temp_image_path = os.path.join(temp_image_dir, f"image_{image_count}.{image_ext}")
                        with open(temp_image_path, "wb") as img_file:
                            img_file.write(image_data)

                        pil_img = PILImage.open(temp_image_path)
                        img_width, img_height = pil_img.size
                        max_width = 5 * inch
                        if img_width > max_width:
                            ratio = max_width / img_width
                            pdf_width = max_width
                            pdf_height = img_height * ratio
                        else:
                            pdf_width = (img_width / 96) * inch
                            pdf_height = (img_height / 96) * inch
                            if pdf_width > max_width:
                                ratio = max_width / pdf_width
                                pdf_width = max_width
                                pdf_height = pdf_height * ratio
                        extracted_images.append({"path": temp_image_path, "width": pdf_width, "height": pdf_height})
                        image_count += 1
                    except Exception as img_error:
                        logger.warning("Failed to extract image from DOCX: %s", img_error)

                if extracted_images:
                    story.append(Spacer(1, 12))
                    for img_info in extracted_images:
                        try:
                            story.append(RLImage(img_info["path"], width=img_info["width"], height=img_info["height"]))
                            story.append(Spacer(1, 12))
                        except Exception as img_error:
                            logger.warning("Failed to add image to PDF: %s", img_error)

                for table in doc.tables:
                    table_data = [[cell.text.strip() for cell in row.cells] for row in table.rows]
                    if not table_data:
                        continue
                    rl_table = Table(table_data, hAlign="LEFT")
                    rl_table.setStyle(
                        TableStyle(
                            [
                                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#4472C4")),
                                ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
                                ("ALIGN", (0, 0), (-1, -1), "LEFT"),
                                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                                ("FONTSIZE", (0, 0), (-1, 0), 12),
                                ("BOTTOMPADDING", (0, 0), (-1, 0), 14),
                                ("TOPPADDING", (0, 0), (-1, 0), 14),
                                ("BACKGROUND", (0, 1), (-1, -1), colors.HexColor("#F2F2F2")),
                                ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
                                ("FONTSIZE", (0, 1), (-1, -1), 11),
                                ("TOPPADDING", (0, 1), (-1, -1), 10),
                                ("BOTTOMPADDING", (0, 1), (-1, -1), 10),
                                ("LEFTPADDING", (0, 0), (-1, -1), 12),
                                ("RIGHTPADDING", (0, 0), (-1, -1), 12),
                                ("GRID", (0, 0), (-1, -1), 1, colors.HexColor("#CCCCCC")),
                                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                            ]
                        )
                    )
                    story.append(Spacer(1, 12))
                    story.append(rl_table)
                    story.append(Spacer(1, 16))

                pdf_doc.build(story)
                logger.info("Enhanced DOCX to PDF conversion completed")
            finally:
                shutil.rmtree(temp_image_dir, ignore_errors=True)

            return pdf_buffer.getvalue()
        finally:
            try:
                os.unlink(docx_path)
                if os.path.exists(pdf_path):
                    os.unlink(pdf_path)
            except Exception:
                pass
    except Exception as e:
        logger.error("Error converting DOCX to PDF: %s", e)
        raise HTTPException(status_code=500, detail=GENERIC_API_ERROR_MESSAGE)


def _wmf_to_png_bytes(wmf_bytes: bytes, width: int = 800, height: int = 600) -> Optional[bytes]:
    try:
        import ctypes
        from ctypes import create_string_buffer, windll

        gdi32 = windll.gdi32
        user32 = windll.user32

        hmf = gdi32.SetWinMetaFileBits(len(wmf_bytes), wmf_bytes, None, None)
        if not hmf:
            buf = create_string_buffer(wmf_bytes)
            hmf = gdi32.SetEnhMetaFileBits(len(wmf_bytes), buf)
            is_emf = True
        else:
            is_emf = False

        if not hmf:
            return None

        hdc_screen = user32.GetDC(None)
        hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [
                ("biSize", ctypes.c_uint32),
                ("biWidth", ctypes.c_int32),
                ("biHeight", ctypes.c_int32),
                ("biPlanes", ctypes.c_uint16),
                ("biBitCount", ctypes.c_uint16),
                ("biCompression", ctypes.c_uint32),
                ("biSizeImage", ctypes.c_uint32),
                ("biXPelsPerMeter", ctypes.c_int32),
                ("biYPelsPerMeter", ctypes.c_int32),
                ("biClrUsed", ctypes.c_uint32),
                ("biClrImportant", ctypes.c_uint32),
            ]

        bmi = BITMAPINFOHEADER()
        bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.biWidth = width
        bmi.biHeight = -height
        bmi.biPlanes = 1
        bmi.biBitCount = 32
        bmi.biCompression = 0

        pbits = ctypes.c_void_p()
        hbmp = gdi32.CreateDIBSection(hdc_mem, ctypes.byref(bmi), 0, ctypes.byref(pbits), None, 0)
        if not hbmp:
            gdi32.DeleteDC(hdc_mem)
            user32.ReleaseDC(None, hdc_screen)
            return None

        old_bmp = gdi32.SelectObject(hdc_mem, hbmp)
        hbrush = gdi32.CreateSolidBrush(0x00FFFFFF)

        class RECT(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

        rc = RECT(0, 0, width, height)
        user32.FillRect(hdc_mem, ctypes.byref(rc), hbrush)
        gdi32.DeleteObject(hbrush)

        if is_emf:
            gdi32.PlayEnhMetaFile(hdc_mem, hmf, ctypes.byref(rc))
            gdi32.DeleteEnhMetaFile(hmf)
        else:
            gdi32.PlayMetaFile(hdc_mem, hmf)
            gdi32.DeleteMetaFile(hmf)

        buf_size = width * height * 4
        buf = (ctypes.c_byte * buf_size)()
        gdi32.GetDIBits(hdc_mem, hbmp, 0, height, buf, ctypes.byref(bmi), 0)

        gdi32.SelectObject(hdc_mem, old_bmp)
        gdi32.DeleteObject(hbmp)
        gdi32.DeleteDC(hdc_mem)
        user32.ReleaseDC(None, hdc_screen)

        arr = np.frombuffer(buf, dtype=np.uint8).reshape((height, width, 4))
        arr = arr[:, :, [2, 1, 0, 3]]
        from PIL import Image as PILImage

        img = PILImage.fromarray(arr, "RGBA")
        out = io.BytesIO()
        img.save(out, format="PNG")
        return out.getvalue()
    except Exception as e:
        logger.warning("[WMF->PNG] Conversion failed: %s", e)
        return None


def convert_pptx_to_pdf(pptx_content: bytes) -> bytes:
    """Convert PPTX content to PDF with enhanced image and formatting preservation."""
    if not PPTX_AVAILABLE:
        raise HTTPException(status_code=500, detail="PPTX processing not available")

    try:
        logger.info("Attempting enhanced COM automation for PPTX to PDF conversion")
        with tempfile.NamedTemporaryFile(suffix=".pptx", delete=False) as pptx_temp:
            pptx_temp.write(pptx_content)
            pptx_path = pptx_temp.name

        pdf_path = pptx_path.replace(".pptx", ".pdf")

        try:
            if COMTYPES_AVAILABLE and os.name == "nt":
                try:
                    import comtypes

                    comtypes.CoInitialize()
                    try:
                        powerpoint = comtypes.client.CreateObject("PowerPoint.Application")
                        powerpoint.Visible = 1
                        presentation = powerpoint.Presentations.Open(pptx_path)
                        presentation.ExportAsFixedFormat(
                            Path=pdf_path,
                            FixedFormatType=2,
                            Intent=1,
                            FrameSlides=0,
                            HandoutOrder=1,
                            OutputType=0,
                            PrintHiddenSlides=0,
                            PrintRange=None,
                            RangeType=1,
                            SlideShowName="",
                            IncludeStructureTags=True,
                            IncludeMarkup=True,
                            BitmapMissingFonts=True,
                            UseISO19005_1=False,
                            DocStructureTags=True,
                            JPEGQuality=0,
                        )
                        presentation.Close()
                        powerpoint.Quit()
                        with open(pdf_path, "rb") as pdf_file:
                            return pdf_file.read()
                    finally:
                        try:
                            comtypes.CoUninitialize()
                        except Exception:
                            pass
                except Exception as com_error:
                    logger.warning("Enhanced COM conversion failed: %s, trying fallback", com_error)

            from PIL import Image as PILImage
            from reportlab.lib import colors
            from reportlab.lib.pagesizes import A4
            from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
            from reportlab.lib.units import inch
            from reportlab.platypus import Image as RLImage
            from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer

            prs = Presentation(io.BytesIO(pptx_content))
            pdf_buffer = io.BytesIO()
            pdf_doc = SimpleDocTemplate(
                pdf_buffer,
                pagesize=A4,
                leftMargin=72,
                rightMargin=72,
                topMargin=72,
                bottomMargin=72,
            )
            styles = getSampleStyleSheet()
            story = []
            slide_title_style = ParagraphStyle(
                "SlideTitle",
                parent=styles["Heading1"],
                fontSize=20,
                spaceAfter=16,
                spaceBefore=12,
                textColor=colors.HexColor("#1f4788"),
                fontName="Helvetica-Bold",
                alignment=1,
                leading=24,
            )
            slide_content_style = ParagraphStyle(
                "SlideContent",
                parent=styles["Normal"],
                fontSize=13,
                spaceAfter=10,
                spaceBefore=4,
                textColor=colors.black,
                fontName="Helvetica",
                leftIndent=20,
                leading=17,
            )
            slide_bullet_style = ParagraphStyle(
                "SlideBullet",
                parent=styles["Normal"],
                fontSize=13,
                spaceAfter=8,
                spaceBefore=2,
                textColor=colors.black,
                fontName="Helvetica",
                leftIndent=30,
                bulletIndent=15,
                leading=17,
            )

            temp_image_dir = tempfile.mkdtemp()
            try:
                for slide_num, slide in enumerate(prs.slides, 1):
                    story.append(Paragraph(f"Slide {slide_num}", slide_title_style))
                    story.append(Spacer(1, 12))

                    slide_images = []
                    slide_texts = []
                    for shape in slide.shapes:
                        if hasattr(shape, "text") and shape.text.strip():
                            slide_texts.append(shape.text.strip())
                        elif hasattr(shape, "image"):
                            try:
                                image = shape.image
                                image_bytes = image.blob
                                image_ext = (image.ext or "").lower()
                                if image_ext in ("wmf", "emf"):
                                    png_bytes = _wmf_to_png_bytes(image_bytes)
                                    if not png_bytes:
                                        continue
                                    image_bytes = png_bytes
                                    image_ext = "png"
                                temp_image_path = os.path.join(
                                    temp_image_dir,
                                    f"slide_{slide_num}_img_{len(slide_images)}.{image_ext}",
                                )
                                with open(temp_image_path, "wb") as img_file:
                                    img_file.write(image_bytes)
                                slide_images.append(temp_image_path)
                            except Exception as img_error:
                                logger.warning("Failed to extract image from slide %s: %s", slide_num, img_error)

                    for text in slide_texts:
                        if text.startswith("•") or text.startswith("-") or text.startswith("*"):
                            story.append(Paragraph(f"• {text.lstrip('•-*').strip()}", slide_bullet_style))
                        else:
                            story.append(Paragraph(text, slide_content_style))
                        story.append(Spacer(1, 6))

                    for img_path in slide_images:
                        try:
                            pil_img = PILImage.open(img_path)
                            img_width, img_height = pil_img.size
                            max_width = 6 * inch
                            if img_width > 0:
                                pdf_width = (img_width / 96) * inch
                                pdf_height = (img_height / 96) * inch
                                if pdf_width > max_width:
                                    ratio = max_width / pdf_width
                                    pdf_width = max_width
                                    pdf_height = pdf_height * ratio
                            else:
                                pdf_width = max_width
                                pdf_height = 4 * inch
                            story.append(Spacer(1, 8))
                            story.append(RLImage(img_path, width=pdf_width, height=pdf_height))
                            story.append(Spacer(1, 16))
                        except Exception as img_error:
                            logger.warning("Failed to add image to PDF: %s", img_error)

                    if slide_num < len(prs.slides):
                        story.append(PageBreak())

                pdf_doc.build(story)
            finally:
                shutil.rmtree(temp_image_dir, ignore_errors=True)

            return pdf_buffer.getvalue()
        finally:
            try:
                os.unlink(pptx_path)
                if os.path.exists(pdf_path):
                    os.unlink(pdf_path)
            except Exception:
                pass
    except Exception as e:
        logger.error("Error converting PPTX to PDF: %s", e)
        raise HTTPException(status_code=500, detail=GENERIC_API_ERROR_MESSAGE)


def convert_document_to_pdf(file_content: bytes, filename: str) -> tuple[bytes, str]:
    file_ext = filename.lower().split(".")[-1]
    logger.info("Converting %s (%s) to PDF...", filename, file_ext.upper())

    if file_ext == "pdf":
        return file_content, filename
    if file_ext in ["docx", "doc"]:
        pdf_content = convert_docx_to_pdf(file_content)
        return pdf_content, filename.rsplit(".", 1)[0] + ".pdf"
    if file_ext in ["pptx", "ppt"]:
        pdf_content = convert_pptx_to_pdf(file_content)
        return pdf_content, filename.rsplit(".", 1)[0] + ".pdf"

    raise HTTPException(
        status_code=400,
        detail=(
            f"Unsupported file format: {file_ext}. "
            "Supported formats: PDF, DOCX, DOC, PPTX, PPT, PNG, JPG, JPEG"
        ),
    )


__all__ = [
    "convert_docx_to_pdf",
    "_wmf_to_png_bytes",
    "convert_pptx_to_pdf",
    "convert_document_to_pdf",
    "export_pptx_slides_as_png",
    "render_pptx_slides_as_png_list",
]
from core.runtime import *
from models.domain import *
from schemas.domain import *
from services.jobs import *
from services.auth_platform import *


def _find_libreoffice_executable() -> Optional[str]:
    """Resolve LibreOffice/soffice binary (Ubuntu deployments use libreoffice)."""
    for candidate in (
        "libreoffice",
        "soffice",
        "/usr/bin/libreoffice",
        "/usr/bin/soffice",
    ):
        path = shutil.which(candidate)
        if path:
            return path
    return None


def _libreoffice_profile_uri(profile_dir: str) -> str:
    """Writable LO user profile (required for headless on Ubuntu/systemd)."""
    return Path(profile_dir).resolve().as_uri()


def _libreoffice_run_convert(
    source_path: str,
    out_dir: str,
    convert_to: str,
    timeout: int = 300,
) -> subprocess.CompletedProcess:
    lo_cmd = _find_libreoffice_executable()
    if not lo_cmd:
        raise RuntimeError("LibreOffice executable not found")

    profile_dir = tempfile.mkdtemp(prefix="lo_profile_")
    profile_uri = _libreoffice_profile_uri(profile_dir)
    env = os.environ.copy()
    env["HOME"] = profile_dir
    env["TMPDIR"] = out_dir
    env["SAL_USE_VCLPLUGIN"] = "svp"

    try:
        return subprocess.run(
            [
                lo_cmd,
                f"-env:UserInstallation={profile_uri}",
                "--headless",
                "--norestore",
                "--nofirststartwizard",
                "--invisible",
                "--convert-to",
                convert_to,
                "--outdir",
                out_dir,
                source_path,
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    finally:
        shutil.rmtree(profile_dir, ignore_errors=True)


def pdf_bytes_are_usable(pdf_bytes: bytes, min_pages: int = 1) -> bool:
    """Return True when bytes look like a readable PDF with at least min_pages."""
    if not pdf_bytes or len(pdf_bytes) < 128:
        return False
    if not pdf_bytes.lstrip().startswith(b"%PDF"):
        return False
    try:
        with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
            return len(doc) >= min_pages
    except Exception:
        return False


def _libreoffice_pdf_convert_args(source_path: str) -> tuple[str, ...]:
    """Pick LibreOffice export filters by source extension (Writer vs Impress)."""
    ext = os.path.splitext(source_path)[1].lower()
    if ext in (".doc", ".docx", ".odt", ".rtf"):
        return ("pdf:writer_pdf_Export", "pdf")
    if ext in (".ppt", ".pptx", ".odp"):
        return ("pdf:impress_pdf_Export:ExportNotesPages=false", "pdf")
    return ("pdf",)


def _libreoffice_convert_file_to_pdf(source_path: str, timeout: int = 300) -> Optional[bytes]:
    """
    Convert an office file to PDF via LibreOffice headless.
    Uses a dedicated user profile so headless mode works on Ubuntu/AWS.
    """
    if not _find_libreoffice_executable():
        return None

    out_dir = tempfile.mkdtemp(prefix="lo_pdf_out_")
    try:
        for convert_arg in _libreoffice_pdf_convert_args(source_path):
            try:
                result = _libreoffice_run_convert(
                    source_path, out_dir, convert_arg, timeout=timeout
                )
            except subprocess.TimeoutExpired:
                logger.warning("LibreOffice PDF conversion timed out for %s", source_path)
                return None
            except RuntimeError as exc:
                logger.warning("%s", exc)
                return None

            base = os.path.splitext(os.path.basename(source_path))[0]
            pdf_path = os.path.join(out_dir, f"{base}.pdf")
            if result.returncode == 0 and os.path.isfile(pdf_path):
                with open(pdf_path, "rb") as pdf_file:
                    pdf_bytes = pdf_file.read()
                if pdf_bytes_are_usable(pdf_bytes):
                    return pdf_bytes
                logger.warning(
                    "LibreOffice PDF (%s) produced unusable output for %s",
                    convert_arg,
                    os.path.basename(source_path),
                )
            else:
                logger.warning(
                    "LibreOffice PDF (%s) failed (rc=%s) for %s: %s",
                    convert_arg,
                    result.returncode,
                    os.path.basename(source_path),
                    (result.stderr or result.stdout or "")[:800],
                )
    except Exception as exc:
        logger.warning("LibreOffice conversion error for %s: %s", source_path, exc)
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)
    return None


def export_pptx_slides_as_png(file_bytes: bytes, suffix: str = ".pptx") -> Optional[list[bytes]]:
    """
    Export each presentation slide to PNG via LibreOffice Impress.
    Preserves exact slide layout (boxes, arrows, fonts) better than text extraction.
    """
    if not _find_libreoffice_executable():
        return None

    work_dir = tempfile.mkdtemp(prefix="lo_png_out_")
    src_path = os.path.join(work_dir, f"presentation{suffix}")
    try:
        with open(src_path, "wb") as src_file:
            src_file.write(file_bytes)

        for convert_arg in (
            "png:impress_png_Export:PixelWidth={}".format(
                1280 if os.name != "nt" else 1920
            ),
            "png",
        ):
            try:
                out_dir = tempfile.mkdtemp(prefix="lo_png_page_", dir=work_dir)
                result = _libreoffice_run_convert(
                    src_path, out_dir, convert_arg, timeout=300
                )
                png_files = [
                    os.path.join(out_dir, name)
                    for name in os.listdir(out_dir)
                    if name.lower().endswith(".png")
                ]
                if result.returncode == 0 and png_files:

                    def _slide_order(path: str) -> int:
                        nums = re.findall(r"\d+", os.path.basename(path))
                        return int(nums[-1]) if nums else 0

                    png_files.sort(key=_slide_order)
                    slides: list[bytes] = []
                    for png_path in png_files:
                        with open(png_path, "rb") as png_file:
                            data = png_file.read()
                        base = os.path.basename(png_path).lower()
                        if base in ("image.png", "presentation.png") and len(data) > 2_000_000:
                            logger.warning("Skipping oversized aggregate PNG %s", base)
                            continue
                        if len(data) > 500:
                            slides.append(data)
                    if slides:
                        logger.info(
                            "LibreOffice exported %s slide PNG(s) using %s",
                            len(slides),
                            convert_arg,
                        )
                        return slides
            except subprocess.TimeoutExpired:
                logger.warning("LibreOffice PNG export timed out (%s)", convert_arg)
            except RuntimeError as exc:
                logger.warning("LibreOffice PNG export: %s", exc)
            except Exception as exc:
                logger.warning("LibreOffice PNG export failed (%s): %s", convert_arg, exc)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
    return None


def render_pptx_slides_as_png_list(file_bytes: bytes, suffix: str = ".pptx") -> Optional[list[bytes]]:
    """
    Return one PNG per slide. Tries LibreOffice direct PNG export, then renders
    each page of a LibreOffice PDF (most reliable on Ubuntu).

    A PARTIAL direct export must not short-circuit the PDF fallback. `--convert-to png`
    frequently writes only the first slide of a deck, and returning that as "the slides"
    meant a multi-slide deck was processed as `Slide 1/1` - every slide after the first got
    no snapshot at all. Whichever route yields more slides wins, and when the deck's real
    slide count is unknown (legacy binary .ppt, which python-pptx cannot open) a single-PNG
    direct export is treated as suspect and the PDF route is attempted anyway.
    """
    expected_slides = None
    if PPTX_AVAILABLE:
        try:
            expected_slides = len(Presentation(io.BytesIO(file_bytes)).slides)
        except Exception:
            pass

    direct = export_pptx_slides_as_png(file_bytes, suffix)
    direct_count = len(direct) if direct else 0
    if direct_count and expected_slides and direct_count >= expected_slides:
        return direct[:expected_slides]
    if direct_count > 1 and not expected_slides:
        # Multiple PNGs with no reference count: nothing suggests the export is truncated.
        return direct

    if direct_count:
        logger.warning(
            "LibreOffice returned %s/%s slide PNG(s); trying the PDF route for the rest",
            direct_count,
            expected_slides or "?",
        )

    pdf_bytes = _libreoffice_convert_bytes_to_pdf(file_bytes, suffix)
    if not pdf_bytes or not pdf_bytes_are_usable(pdf_bytes):
        return direct

    try:
        rendered: list[bytes] = []
        slide_zoom = 1.5 if os.name != "nt" else 2.0
        with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
            for page in doc:
                try:
                    matrix = fitz.Matrix(slide_zoom, slide_zoom)
                    rendered.append(page.get_pixmap(matrix=matrix, alpha=False).tobytes("png"))
                except Exception as page_exc:
                    logger.warning(
                        "PDF slide page render failed: %s",
                        log_safe(page_exc),
                    )
        # Take the PDF pages whenever they cover more slides than the direct export did,
        # even if they still fall short of expected_slides - a partial-but-larger set is
        # strictly better than dropping those slides.
        if rendered and len(rendered) > direct_count:
            logger.info(
                "Rendered %s slide PNG(s) from LibreOffice PDF (%s expected, direct gave %s)",
                len(rendered),
                expected_slides or "?",
                direct_count,
            )
            return rendered[:expected_slides] if expected_slides else rendered
    except Exception as exc:
        logger.warning("PDF per-page slide render failed: %s", exc)
    return direct


def _libreoffice_convert_bytes_to_pdf(file_bytes: bytes, suffix: str) -> Optional[bytes]:
    """Write bytes to a temp file and convert with LibreOffice."""
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(file_bytes)
        source_path = tmp.name
    try:
        return _libreoffice_convert_file_to_pdf(source_path)
    finally:
        try:
            os.unlink(source_path)
        except OSError:
            pass


def convert_docx_to_pdf(docx_content: bytes, source_suffix: str = ".docx") -> bytes:
    """Convert DOCX/DOC content to PDF with enhanced image and formatting preservation"""
    if not DOCX_AVAILABLE:
        raise HTTPException(status_code=500, detail="DOCX processing not available")
    
    try:
        logger.info("Attempting enhanced COM automation for DOCX to PDF conversion")
        # Create temporary files
        with tempfile.NamedTemporaryFile(suffix=source_suffix, delete=False) as docx_temp:
            docx_temp.write(docx_content)
            docx_path = docx_temp.name
        
        pdf_path = os.path.splitext(docx_path)[0] + ".pdf"
        
        try:
            if COMTYPES_AVAILABLE and os.name == 'nt':  # Windows
                try:
                    # Initialize COM
                    import comtypes
                    comtypes.CoInitialize()
                    
                    try:
                        # Use COM automation for conversion with enhanced settings
                        word = comtypes.client.CreateObject('Word.Application')
                        word.Visible = False
                        doc = word.Documents.Open(docx_path)
                        
                        # Enhanced PDF export settings to preserve images and formatting
                        doc.ExportAsFixedFormat(
                            OutputFileName=pdf_path,
                            ExportFormat=17,  # PDF format
                            OpenAfterExport=False,
                            OptimizeFor=0,  # Print optimization
                            BitmapMissingFonts=True,
                            DocStructureTags=True,
                            CreateBookmarks=0,
                            UseISO19005_1=False,
                            IncludeStructureTags=True,
                            IncludeMarkup=True,
                            JPEGQuality=0  # Maximum quality for images
                        )
                        
                        doc.Close()
                        word.Quit()
                        
                        # Read the converted PDF
                        with open(pdf_path, 'rb') as pdf_file:
                            pdf_content = pdf_file.read()
                        
                        if pdf_bytes_are_usable(pdf_content):
                            logger.info("Successfully converted DOCX to PDF using COM automation")
                            return pdf_content
                        logger.warning("COM DOCX conversion produced unusable PDF; trying LibreOffice")
                    finally:
                        # Clean up COM
                        try:
                            comtypes.CoUninitialize()
                        except:
                            pass
                except Exception as com_error:
                    logger.warning('COM conversion failed: %s, trying LibreOffice', ("" if com_error is None else str(com_error).replace("\n", "").replace("\r", "")))
            
            lo_pdf = _libreoffice_convert_bytes_to_pdf(docx_content, source_suffix)
            if lo_pdf and pdf_bytes_are_usable(lo_pdf):
                logger.info("Successfully converted DOCX to PDF using LibreOffice (%s bytes)", len(lo_pdf))
                return lo_pdf
            if lo_pdf:
                logger.warning(
                    "LibreOffice DOCX conversion returned unusable PDF (%s bytes); trying fallback",
                    len(lo_pdf),
                )

            # Last resort: python-docx + reportlab (layout may differ from source)
            logger.info("Using reportlab fallback for DOCX to PDF conversion")
            from reportlab.pdfgen import canvas
            from reportlab.lib.pagesizes import letter, A4
            from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
            from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Image as RLImage, Table, TableStyle, ListFlowable, ListItem
            from reportlab.lib.units import inch
            from reportlab.lib import colors
            from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_JUSTIFY
            from PIL import Image as PILImage
            
            # Extract content from DOCX
            doc = Document(io.BytesIO(docx_content))
            
            # Create enhanced PDF with proper formatting
            pdf_buffer = io.BytesIO()
            pdf_doc = SimpleDocTemplate(pdf_buffer, pagesize=A4, leftMargin=72, rightMargin=72, topMargin=72, bottomMargin=72)
            styles = getSampleStyleSheet()
            story = []
            
            # Custom styles for better formatting with larger, more readable fonts
            title_style = ParagraphStyle(
                'CustomTitle',
                parent=styles['Heading1'],
                fontSize=18,           # Increased for better readability
                spaceAfter=16,
                spaceBefore=12,
                textColor=colors.black,
                fontName='Helvetica-Bold',
                alignment=TA_LEFT,
                leading=22             # Better line spacing
            )
            
            heading2_style = ParagraphStyle(
                'CustomHeading2',
                parent=styles['Heading2'],
                fontSize=15,           # Increased for better hierarchy
                spaceAfter=12,
                spaceBefore=10,
                textColor=colors.black,
                fontName='Helvetica-Bold',
                alignment=TA_LEFT,
                leading=18
            )
            
            normal_style = ParagraphStyle(
                'CustomNormal',
                parent=styles['Normal'],
                fontSize=12,           # Increased from 10 for better readability
                spaceAfter=10,         # More spacing between paragraphs
                textColor=colors.black,
                fontName='Helvetica',
                alignment=TA_JUSTIFY,
                leading=16             # Better line height (1.33x font size)
            )
            
            list_style = ParagraphStyle(
                'CustomList',
                parent=styles['Normal'],
                fontSize=12,           # Increased from 10
                spaceAfter=6,
                leftIndent=20,
                textColor=colors.black,
                fontName='Helvetica',
                alignment=TA_LEFT,
                leading=16
            )
            
            # Track list items for proper numbering
            current_list_items = []
            list_counter = 0
            
            # Process paragraphs with enhanced formatting
            for para in doc.paragraphs:
                if not para.text.strip():
                    continue
                
                # Check if this is a list item
                is_list_item = False
                list_format = None
                
                # Detect numbered lists (1., 2., etc.)
                if para.text.strip() and (para.text.strip()[0].isdigit() and '.' in para.text[:5]):
                    is_list_item = True
                    list_format = 'decimal'
                # Detect bullet lists
                elif para.style.name in ['List Bullet', 'List Paragraph'] or para.text.strip().startswith('•'):
                    is_list_item = True
                    list_format = 'bullet'
                
                # Determine style based on paragraph properties
                if para.style.name.startswith('Heading 1') or para.style.name == 'Title':
                    # Build formatted text with proper bold handling
                    formatted_text = ""
                    for run in para.runs:
                        text = run.text
                        if run.bold or para.style.name == 'Title':
                            formatted_text += f"<b>{text}</b>"
                        elif run.italic:
                            formatted_text += f"<i>{text}</i>"
                        else:
                            formatted_text += text
                    
                    if not formatted_text:
                        formatted_text = f"<b>{para.text}</b>"
                    
                    p = Paragraph(formatted_text, title_style)
                    story.append(p)
                    
                elif para.style.name.startswith('Heading'):
                    # Build formatted text
                    formatted_text = ""
                    for run in para.runs:
                        text = run.text
                        if run.bold:
                            formatted_text += f"<b>{text}</b>"
                        elif run.italic:
                            formatted_text += f"<i>{text}</i>"
                        else:
                            formatted_text += text
                    
                    if not formatted_text:
                        formatted_text = f"<b>{para.text}</b>"
                    
                    p = Paragraph(formatted_text, heading2_style)
                    story.append(p)
                    
                elif is_list_item:
                    # Handle list items with proper formatting
                    text = para.text.strip()
                    
                    # Build formatted text preserving bold/italic
                    formatted_text = ""
                    for run in para.runs:
                        run_text = run.text
                        if run.bold:
                            formatted_text += f"<b>{run_text}</b>"
                        elif run.italic:
                            formatted_text += f"<i>{run_text}</i>"
                        else:
                            formatted_text += run_text
                    
                    if not formatted_text:
                        formatted_text = text
                    
                    # Remove existing numbering/bullets from text
                    if list_format == 'decimal' and formatted_text[0].isdigit():
                        # Remove "1. " or "1." from the beginning
                        formatted_text = formatted_text.split('.', 1)[1].strip() if '.' in formatted_text else formatted_text
                    elif formatted_text.startswith('•'):
                        formatted_text = formatted_text[1:].strip()
                    
                    # Add to list items
                    p = Paragraph(formatted_text, list_style)
                    current_list_items.append(p)
                    
                else:
                    # Flush any pending list items
                    if current_list_items:
                        if list_format == 'decimal':
                            list_flowable = ListFlowable(current_list_items, bulletType='1', start=1)
                        else:
                            list_flowable = ListFlowable(current_list_items, bulletType='bullet')
                        story.append(list_flowable)
                        story.append(Spacer(1, 6))
                        current_list_items = []
                        list_counter = 0
                    
                    # Handle regular paragraphs with bold/italic formatting
                    formatted_text = ""
                    for run in para.runs:
                        text = run.text
                        if run.bold:
                            formatted_text += f"<b>{text}</b>"
                        elif run.italic:
                            formatted_text += f"<i>{text}</i>"
                        else:
                            formatted_text += text
                    
                    if not formatted_text:
                        formatted_text = para.text
                    
                    p = Paragraph(formatted_text, normal_style)
                    story.append(p)
                    story.append(Spacer(1, 4))
            
            # Flush any remaining list items
            if current_list_items:
                if list_format == 'decimal':
                    list_flowable = ListFlowable(current_list_items, bulletType='1', start=1)
                else:
                    list_flowable = ListFlowable(current_list_items, bulletType='bullet')
                story.append(list_flowable)
                story.append(Spacer(1, 6))
            
            # Extract and include images inline with content
            temp_image_dir = tempfile.mkdtemp()
            try:
                # Extract images from DOCX relationships and map to paragraphs
                from docx.oxml.ns import qn
                
                image_count = 0
                extracted_images = []
                
                for rel in doc.part.rels.values():
                    if "image" in rel.target_ref:
                        try:
                            image_data = rel.target_part.blob
                            image_ext = rel.target_ref.split('.')[-1]
                            
                            # Save temporary image
                            temp_image_path = os.path.join(temp_image_dir, f"image_{image_count}.{image_ext}")
                            with open(temp_image_path, 'wb') as img_file:
                                img_file.write(image_data)
                            
                            # Store image info for later insertion
                            try:
                                # Open with PIL to get dimensions
                                pil_img = PILImage.open(temp_image_path)
                                img_width, img_height = pil_img.size
                                
                                # Calculate appropriate size for PDF (max 5 inches wide to fit with margins)
                                max_width = 5 * inch
                                if img_width > max_width:
                                    ratio = max_width / img_width
                                    pdf_width = max_width
                                    pdf_height = img_height * ratio
                                else:
                                    # Convert pixels to points (assuming 96 DPI)
                                    pdf_width = (img_width / 96) * inch
                                    pdf_height = (img_height / 96) * inch
                                    
                                    # Still cap at max width
                                    if pdf_width > max_width:
                                        ratio = max_width / pdf_width
                                        pdf_width = max_width
                                        pdf_height = pdf_height * ratio
                                
                                extracted_images.append({
                                    'path': temp_image_path,
                                    'width': pdf_width,
                                    'height': pdf_height
                                })
                                
                                image_count += 1
                                logger.info('Extracted image %s: %sx%s -> %sx%s points', ("" if image_count is None else str(image_count).replace("\n", "").replace("\r", "")), ("" if img_width is None else str(img_width).replace("\n", "").replace("\r", "")), ("" if img_height is None else str(img_height).replace("\n", "").replace("\r", "")), f"{pdf_width:.1f}", f"{pdf_height:.1f}")
                                
                            except Exception as img_error:
                                logger.warning('Failed to process image %s: %s', ("" if image_count is None else str(image_count).replace("\n", "").replace("\r", "")), ("" if img_error is None else str(img_error).replace("\n", "").replace("\r", "")))
                                
                        except Exception as e:
                            logger.warning('Failed to extract image from DOCX: %s', e)
                
                # Add images to the story (after text content)
                if extracted_images:
                    story.append(Spacer(1, 12))
                    for img_info in extracted_images:
                        try:
                            rl_image = RLImage(img_info['path'], width=img_info['width'], height=img_info['height'])
                            story.append(rl_image)
                            story.append(Spacer(1, 12))
                        except Exception as img_error:
                            logger.warning('Failed to add image to PDF: %s', ("" if img_error is None else str(img_error).replace("\n", "").replace("\r", "")))
                
                # Process tables with better formatting
                for table in doc.tables:
                    table_data = []
                    for row in table.rows:
                        row_data = []
                        for cell in row.cells:
                            row_data.append(cell.text.strip())
                        table_data.append(row_data)
                    
                    if table_data:
                        # Create reportlab table with better styling
                        rl_table = Table(table_data, hAlign='LEFT')
                        rl_table.setStyle(TableStyle([
                            # Header row styling
                            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#4472C4')),  # Professional blue
                            ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
                            ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
                            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                            ('FONTSIZE', (0, 0), (-1, 0), 12),
                            ('BOTTOMPADDING', (0, 0), (-1, 0), 14),
                            ('TOPPADDING', (0, 0), (-1, 0), 14),
                            # Data rows styling
                            ('BACKGROUND', (0, 1), (-1, -1), colors.HexColor('#F2F2F2')),  # Light gray
                            ('FONTNAME', (0, 1), (-1, -1), 'Helvetica'),
                            ('FONTSIZE', (0, 1), (-1, -1), 11),
                            ('TOPPADDING', (0, 1), (-1, -1), 10),
                            ('BOTTOMPADDING', (0, 1), (-1, -1), 10),
                            ('LEFTPADDING', (0, 0), (-1, -1), 12),
                            ('RIGHTPADDING', (0, 0), (-1, -1), 12),
                            # Grid
                            ('GRID', (0, 0), (-1, -1), 1, colors.HexColor('#CCCCCC')),
                            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                        ]))
                        story.append(Spacer(1, 12))
                        story.append(rl_table)
                        story.append(Spacer(1, 16))
                
                # Build PDF
                pdf_doc.build(story)
                logger.info('Enhanced DOCX to PDF conversion completed with %s images preserved', ("" if image_count is None else str(image_count).replace("\n", "").replace("\r", "")))
                
            finally:
                # Clean up temporary images
                shutil.rmtree(temp_image_dir, ignore_errors=True)
            
            return pdf_buffer.getvalue()
                
        finally:
            # Clean up temporary files
            try:
                os.unlink(docx_path)
                if os.path.exists(pdf_path):
                    os.unlink(pdf_path)
            except:
                pass
                
    except Exception as e:
        logger.error('Error converting DOCX to PDF: %s', e)
        raise HTTPException(status_code=500, detail=GENERIC_API_ERROR_MESSAGE)

def _wmf_to_png_bytes(wmf_bytes: bytes, width: int = 800, height: int = 600) -> Optional[bytes]:
    """
    Convert WMF/EMF bytes to PNG bytes using Windows GDI via ctypes.
    Returns PNG bytes on success, None on failure.
    """
    try:
        import ctypes
        from ctypes import windll, wintypes, c_int, c_uint, c_void_p, byref, create_string_buffer

        gdi32   = windll.gdi32
        user32  = windll.user32
        kernel32 = windll.kernel32

        # Load the metafile into memory
        hmf = gdi32.SetWinMetaFileBits(len(wmf_bytes), wmf_bytes, None, None)
        if not hmf:
            # Try as enhanced metafile (EMF)
            buf = create_string_buffer(wmf_bytes)
            hmf = gdi32.SetEnhMetaFileBits(len(wmf_bytes), buf)
            is_emf = True
        else:
            is_emf = False

        if not hmf:
            return None

        # Create a memory DC and bitmap
        hdc_screen = user32.GetDC(None)
        hdc_mem    = gdi32.CreateCompatibleDC(hdc_screen)

        # Create a DIB section (32-bit RGBA)
        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [
                ("biSize",          ctypes.c_uint32),
                ("biWidth",         ctypes.c_int32),
                ("biHeight",        ctypes.c_int32),
                ("biPlanes",        ctypes.c_uint16),
                ("biBitCount",      ctypes.c_uint16),
                ("biCompression",   ctypes.c_uint32),
                ("biSizeImage",     ctypes.c_uint32),
                ("biXPelsPerMeter", ctypes.c_int32),
                ("biYPelsPerMeter", ctypes.c_int32),
                ("biClrUsed",       ctypes.c_uint32),
                ("biClrImportant",  ctypes.c_uint32),
            ]

        bmi = BITMAPINFOHEADER()
        bmi.biSize        = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.biWidth       = width
        bmi.biHeight      = -height  # top-down
        bmi.biPlanes      = 1
        bmi.biBitCount    = 32
        bmi.biCompression = 0  # BI_RGB

        pbits = ctypes.c_void_p()
        hbmp  = gdi32.CreateDIBSection(hdc_mem, ctypes.byref(bmi), 0, ctypes.byref(pbits), None, 0)
        if not hbmp:
            gdi32.DeleteDC(hdc_mem)
            user32.ReleaseDC(None, hdc_screen)
            return None

        old_bmp = gdi32.SelectObject(hdc_mem, hbmp)

        # Fill white background
        hbrush = gdi32.CreateSolidBrush(0x00FFFFFF)
        class RECT(ctypes.Structure):
            _fields_ = [("left",ctypes.c_long),("top",ctypes.c_long),("right",ctypes.c_long),("bottom",ctypes.c_long)]
        rc = RECT(0, 0, width, height)
        user32.FillRect(hdc_mem, ctypes.byref(rc), hbrush)
        gdi32.DeleteObject(hbrush)

        # Play the metafile
        if is_emf:
            gdi32.PlayEnhMetaFile(hdc_mem, hmf, ctypes.byref(rc))
            gdi32.DeleteEnhMetaFile(hmf)
        else:
            class SIZE(ctypes.Structure):
                _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]
            sz = SIZE(width, height)
            gdi32.PlayMetaFile(hdc_mem, hmf)
            gdi32.DeleteMetaFile(hmf)

        # Read the bitmap bits
        buf_size = width * height * 4
        buf = (ctypes.c_byte * buf_size)()
        gdi32.GetDIBits(hdc_mem, hbmp, 0, height, buf, ctypes.byref(bmi), 0)

        # Cleanup GDI
        gdi32.SelectObject(hdc_mem, old_bmp)
        gdi32.DeleteObject(hbmp)
        gdi32.DeleteDC(hdc_mem)
        user32.ReleaseDC(None, hdc_screen)

        # Convert BGRA → RGBA and save as PNG
        import numpy as np
        arr = np.frombuffer(buf, dtype=np.uint8).reshape((height, width, 4))
        arr = arr[:, :, [2, 1, 0, 3]]  # BGRA → RGBA
        from PIL import Image as PILImage
        img = PILImage.fromarray(arr, 'RGBA')
        out = io.BytesIO()
        img.save(out, format='PNG')
        return out.getvalue()

    except Exception as e:
        logger.warning('[WMF→PNG] Conversion failed: %s', e)
        return None


def convert_pptx_to_pdf(pptx_content: bytes, source_suffix: str = ".pptx") -> bytes:
    """Convert PPTX/PPT content to PDF with enhanced image and formatting preservation"""
    if not PPTX_AVAILABLE:
        raise HTTPException(status_code=500, detail="PPTX processing not available")
    
    try:
        logger.info("Attempting enhanced COM automation for PPTX to PDF conversion")
        # Create temporary files
        with tempfile.NamedTemporaryFile(suffix=source_suffix, delete=False) as pptx_temp:
            pptx_temp.write(pptx_content)
            pptx_path = pptx_temp.name
        
        pdf_path = os.path.splitext(pptx_path)[0] + ".pdf"
        
        try:
            if COMTYPES_AVAILABLE and os.name == 'nt':  # Windows
                try:
                    # Initialize COM
                    import comtypes
                    comtypes.CoInitialize()
                    
                    try:
                        # Use COM automation for conversion with enhanced settings
                        powerpoint = comtypes.client.CreateObject('PowerPoint.Application')
                        powerpoint.Visible = 1
                        presentation = powerpoint.Presentations.Open(pptx_path)
                        
                        # Enhanced PDF export settings to preserve images and formatting
                        presentation.ExportAsFixedFormat(
                            Path=pdf_path,
                            FixedFormatType=2,  # PDF format
                            Intent=1,  # Print intent
                            FrameSlides=0,  # Don't frame slides
                            HandoutOrder=1,
                            OutputType=0,  # Full slides
                            PrintHiddenSlides=0,
                            PrintRange=None,
                            RangeType=1,  # All slides
                            SlideShowName="",
                            IncludeStructureTags=True,
                            IncludeMarkup=True,
                            BitmapMissingFonts=True,
                            UseISO19005_1=False,
                            DocStructureTags=True,
                            JPEGQuality=0  # Maximum quality for images
                        )
                        
                        presentation.Close()
                        powerpoint.Quit()
                        
                        # Read the converted PDF
                        with open(pdf_path, 'rb') as pdf_file:
                            pdf_content = pdf_file.read()
                        
                        if pdf_bytes_are_usable(pdf_content):
                            logger.info("Successfully converted PPTX to PDF using COM automation")
                            return pdf_content
                        logger.warning("COM PPTX conversion produced unusable PDF; trying LibreOffice")
                    finally:
                        # Clean up COM
                        try:
                            comtypes.CoUninitialize()
                        except:
                            pass
                except Exception as com_error:
                    logger.warning('Enhanced COM conversion failed: %s, trying LibreOffice', ("" if com_error is None else str(com_error).replace("\n", "").replace("\r", "")))

            lo_pdf = _libreoffice_convert_bytes_to_pdf(pptx_content, source_suffix)
            if lo_pdf and pdf_bytes_are_usable(lo_pdf):
                logger.info("Successfully converted PPTX to PDF using LibreOffice (%s bytes)", len(lo_pdf))
                return lo_pdf
            if lo_pdf:
                logger.warning(
                    "LibreOffice PPTX conversion returned unusable PDF (%s bytes); trying fallback",
                    len(lo_pdf),
                )

            logger.warning(
                "LibreOffice conversion unavailable or failed; using reportlab fallback for PPTX"
            )
            from reportlab.pdfgen import canvas
            from reportlab.lib.pagesizes import letter, A4
            from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
            from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Image as RLImage, PageBreak
            from reportlab.lib.units import inch
            from reportlab.lib import colors
            from PIL import Image as PILImage
            
            # Extract content from PPTX
            prs = Presentation(io.BytesIO(pptx_content))
            
            # Create enhanced PDF with proper formatting and better margins
            pdf_buffer = io.BytesIO()
            pdf_doc = SimpleDocTemplate(
                pdf_buffer, 
                pagesize=A4,
                leftMargin=72,    # 1 inch margins
                rightMargin=72,
                topMargin=72,
                bottomMargin=72
            )
            styles = getSampleStyleSheet()
            story = []
            
            # Custom styles for better formatting with larger, more readable fonts
            slide_title_style = ParagraphStyle(
                'SlideTitle',
                parent=styles['Heading1'],
                fontSize=20,           # Increased for better visibility
                spaceAfter=16,
                spaceBefore=12,
                textColor=colors.HexColor('#1f4788'),  # Professional blue
                fontName='Helvetica-Bold',
                alignment=1,           # Center alignment
                leading=24
            )
            
            slide_content_style = ParagraphStyle(
                'SlideContent',
                parent=styles['Normal'],
                fontSize=13,           # Increased from 12
                spaceAfter=10,
                spaceBefore=4,
                textColor=colors.black,
                fontName='Helvetica',
                leftIndent=20,
                leading=17             # Better line height
            )
            
            slide_bullet_style = ParagraphStyle(
                'SlideBullet',
                parent=styles['Normal'],
                fontSize=13,
                spaceAfter=8,
                spaceBefore=2,
                textColor=colors.black,
                fontName='Helvetica',
                leftIndent=30,
                bulletIndent=15,
                leading=17
            )
            
            # Create temporary directory for extracted images
            temp_image_dir = tempfile.mkdtemp()
            
            try:
                # Process each slide
                for slide_num, slide in enumerate(prs.slides, 1):
                    # Add slide title
                    slide_title = f"Slide {slide_num}"
                    story.append(Paragraph(slide_title, slide_title_style))
                    story.append(Spacer(1, 12))
                    
                    # Extract text and images from shapes
                    slide_images = []
                    slide_texts = []
                    
                    for shape in slide.shapes:
                        # Handle text shapes
                        if hasattr(shape, "text") and shape.text.strip():
                            slide_texts.append(shape.text.strip())
                        
                        # Handle image shapes
                        elif hasattr(shape, "image"):
                            try:
                                image = shape.image
                                image_bytes = image.blob
                                image_ext = (image.ext or '').lower()

                                # Convert WMF/EMF to PNG using Windows GDI
                                if image_ext in ('wmf', 'emf'):
                                    png_bytes = _wmf_to_png_bytes(image_bytes)
                                    if png_bytes:
                                        image_bytes = png_bytes
                                        image_ext   = 'png'
                                        logger.info('Converted %s to PNG for slide %s', ("" if image_ext.upper() is None else str(image_ext.upper()).replace("\n", "").replace("\r", "")), ("" if slide_num is None else str(slide_num).replace("\n", "").replace("\r", "")))
                                    else:
                                        logger.info('Skipping unrenderable %s image on slide %s', ("" if image_ext.upper() is None else str(image_ext.upper()).replace("\n", "").replace("\r", "")), ("" if slide_num is None else str(slide_num).replace("\n", "").replace("\r", "")))
                                        continue

                                # Save temporary image
                                temp_image_path = os.path.join(temp_image_dir, f"slide_{slide_num}_img_{len(slide_images)}.{image_ext}")
                                with open(temp_image_path, 'wb') as img_file:
                                    img_file.write(image_bytes)

                                slide_images.append(temp_image_path)
                                logger.info('Extracted image from slide %s: %s bytes', ("" if slide_num is None else str(slide_num).replace("\n", "").replace("\r", "")), len(image_bytes))

                            except Exception as img_error:
                                logger.warning('Failed to extract image from slide %s: %s', ("" if slide_num is None else str(slide_num).replace("\n", "").replace("\r", "")), ("" if img_error is None else str(img_error).replace("\n", "").replace("\r", "")))
                        
                        # Handle grouped shapes (may contain images)
                        elif hasattr(shape, "shapes"):
                            for sub_shape in shape.shapes:
                                if hasattr(sub_shape, "image"):
                                    try:
                                        image = sub_shape.image
                                        image_bytes = image.blob
                                        image_ext = (image.ext or '').lower()

                                        # Convert WMF/EMF to PNG
                                        if image_ext in ('wmf', 'emf'):
                                            png_bytes = _wmf_to_png_bytes(image_bytes)
                                            if png_bytes:
                                                image_bytes = png_bytes
                                                image_ext   = 'png'
                                            else:
                                                logger.info('Skipping unrenderable %s in group on slide %s', ("" if image_ext.upper() is None else str(image_ext.upper()).replace("\n", "").replace("\r", "")), ("" if slide_num is None else str(slide_num).replace("\n", "").replace("\r", "")))
                                                continue

                                        temp_image_path = os.path.join(temp_image_dir, f"slide_{slide_num}_group_img_{len(slide_images)}.{image_ext}")
                                        with open(temp_image_path, 'wb') as img_file:
                                            img_file.write(image_bytes)
                                        
                                        slide_images.append(temp_image_path)
                                        logger.info('Extracted grouped image from slide %s: %s bytes', ("" if slide_num is None else str(slide_num).replace("\n", "").replace("\r", "")), len(image_bytes))
                                        
                                    except Exception as img_error:
                                        logger.warning('Failed to extract grouped image from slide %s: %s', ("" if slide_num is None else str(slide_num).replace("\n", "").replace("\r", "")), ("" if img_error is None else str(img_error).replace("\n", "").replace("\r", "")))
                    
                    # Add slide text content with better formatting
                    for text in slide_texts:
                        # Handle bullet points and formatting
                        if text.startswith('•') or text.startswith('-') or text.startswith('*'):
                            # Use bullet style for list items
                            formatted_text = f"• {text.lstrip('•-*').strip()}"
                            p = Paragraph(formatted_text, slide_bullet_style)
                        else:
                            # Use regular content style for non-bullet text
                            formatted_text = text
                            p = Paragraph(formatted_text, slide_content_style)
                        
                        story.append(p)
                        story.append(Spacer(1, 6))  # Increased spacing
                    
                    # Add slide images with better sizing
                    for img_path in slide_images:
                        try:
                            # Open with PIL to get dimensions
                            pil_img = PILImage.open(img_path)
                            img_width, img_height = pil_img.size
                            
                            # Calculate appropriate size for PDF (max 6 inches wide for better visibility)
                            max_width = 6 * inch
                            if img_width > 0:
                                # Convert pixels to points (assuming 96 DPI)
                                pdf_width = (img_width / 96) * inch
                                pdf_height = (img_height / 96) * inch
                                
                                # Scale down if too large
                                if pdf_width > max_width:
                                    ratio = max_width / pdf_width
                                    pdf_width = max_width
                                    pdf_height = pdf_height * ratio
                            else:
                                pdf_width = max_width
                                pdf_height = 4 * inch
                            
                            # Add image to story
                            rl_image = RLImage(img_path, width=pdf_width, height=pdf_height)
                            story.append(Spacer(1, 8))  # Space before image
                            story.append(rl_image)
                            story.append(Spacer(1, 16))  # More space after image
                            
                        except Exception as img_error:
                            logger.warning('Failed to add image to PDF: %s', ("" if img_error is None else str(img_error).replace("\n", "").replace("\r", "")))
                    
                    # Add page break between slides (except for the last slide)
                    if slide_num < len(prs.slides):
                        story.append(PageBreak())
                
                # Build PDF
                pdf_doc.build(story)
                logger.info('Enhanced PPTX to PDF conversion completed with %s slides and images preserved', len(prs.slides))
                
            finally:
                # Clean up temporary images
                shutil.rmtree(temp_image_dir, ignore_errors=True)
            
            return pdf_buffer.getvalue()
                
        finally:
            # Clean up temporary files
            try:
                os.unlink(pptx_path)
                if os.path.exists(pdf_path):
                    os.unlink(pdf_path)
            except:
                pass
                
    except Exception as e:
        logger.error('Error converting PPTX to PDF: %s', e)
        raise HTTPException(status_code=500, detail=GENERIC_API_ERROR_MESSAGE)

def convert_document_to_pdf(file_content: bytes, filename: str) -> tuple[bytes, str]:
    """
    Convert various document formats to PDF
    Returns: (pdf_content, converted_filename)
    """
    file_ext = filename.lower().split('.')[-1]
    
    logger.info('Converting %s (%s) to PDF...', ("" if filename is None else str(filename).replace("\n", "").replace("\r", "")), ("" if file_ext.upper() is None else str(file_ext.upper()).replace("\n", "").replace("\r", "")))
    
    if file_ext == 'pdf':
        # Already PDF, return as-is
        logger.info('File %s is already PDF, no conversion needed', ("" if filename is None else str(filename).replace("\n", "").replace("\r", "")))
        return file_content, filename
    elif file_ext in ['docx', 'doc']:
        logger.info('Starting DOCX to PDF conversion for %s', ("" if filename is None else str(filename).replace("\n", "").replace("\r", "")))
        # Convert DOCX to PDF
        pdf_content = convert_docx_to_pdf(file_content, source_suffix=f".{file_ext}")
        pdf_filename = filename.rsplit('.', 1)[0] + '.pdf'
        logger.info('Successfully converted %s to PDF (%s bytes)', ("" if filename is None else str(filename).replace("\n", "").replace("\r", "")), len(pdf_content))
        return pdf_content, pdf_filename
    elif file_ext in ['pptx', 'ppt']:
        logger.info('Starting PPTX to PDF conversion for %s', ("" if filename is None else str(filename).replace("\n", "").replace("\r", "")))
        # Convert PPTX to PDF
        pdf_content = convert_pptx_to_pdf(file_content, source_suffix=f".{file_ext}")
        pdf_filename = filename.rsplit('.', 1)[0] + '.pdf'
        logger.info('Successfully converted %s to PDF (%s bytes)', ("" if filename is None else str(filename).replace("\n", "").replace("\r", "")), len(pdf_content))
        return pdf_content, pdf_filename
    else:
        # Unsupported format
        raise HTTPException(
            status_code=400, 
            detail=f"Unsupported file format: {file_ext}. Supported formats: PDF, DOCX, DOC, PPTX, PPT, PNG, JPG, JPEG"
        )

# ==============================================================================
# 7. CLOUD RESOURCE HELPERS (S3, PINECONE) & ERROR HANDLING
# ==============================================================================
def get_user_vector_store(user_id: str) -> Pinecone:
    return Pinecone(pinecone_index, embedding_model, "text", namespace=user_id)

def clear_all_data_for_user(user_id: str, db: Session = None):
    """
    Clear all data for a user including:
    - Database records (activity logs, reports, sessions, progress, etc.)
    - Pinecone vectors
    - S3 files
    """
    # Clear database records first (to avoid foreign key violations)
    if db:
        try:
            logger.info('Deleting database records for user: %s', ("" if user_id is None else str(user_id).replace("\n", "").replace("\r", "")))
            # Delete activity logs (foreign key constraint)
            db.query(ActivityLog).filter(ActivityLog.user_id == user_id).delete()
            # Delete course progress
            db.query(CourseProgress).filter(CourseProgress.user_id == user_id).delete()
            # Delete trainee attempts
            db.query(TraineeAttempt).filter(TraineeAttempt.trainee_id == user_id).delete()
            
            # Delete chat messages first (they reference chat_sessions)
            user_sessions = db.query(ChatSession).filter(ChatSession.user_id == user_id).all()
            for session in user_sessions:
                db.query(ChatMessage).filter(ChatMessage.session_id == session.id).delete()
            # Now delete chat sessions
            db.query(ChatSession).filter(ChatSession.user_id == user_id).delete()
            
            # Delete shared report edits first (they reference shared_reports)
            db.query(SharedReportEdit).filter(SharedReportEdit.editor_id == user_id).delete()
            # Delete shared reports where user is sharer
            sharer_reports = db.query(SharedReport).filter(SharedReport.sharer_id == user_id).all()
            for report in sharer_reports:
                db.query(SharedReportEdit).filter(SharedReportEdit.shared_report_id == report.id).delete()
            db.query(SharedReport).filter(SharedReport.sharer_id == user_id).delete()
            # Delete shared reports where user is recipient
            recipient_reports = db.query(SharedReport).filter(SharedReport.recipient_id == user_id).all()
            for report in recipient_reports:
                db.query(SharedReportEdit).filter(SharedReportEdit.shared_report_id == report.id).delete()
            db.query(SharedReport).filter(SharedReport.recipient_id == user_id).delete()
            
            # Delete notifications
            db.query(Notification).filter(Notification.user_id == user_id).delete()
            
            # CRITICAL FIX: Delete tutor response cache entries for user's courses
            # This ensures cached tutor responses are cleaned up when user data is cleared
            user_courses = db.query(Course).filter(Course.author_id == user_id).all()
            for course in user_courses:
                cache_entries = db.query(TutorResponseCache).filter(TutorResponseCache.course_id == course.id).all()
                for cache_entry in cache_entries:
                    try:
                        # Delete cached response from S3
                        s3_client.delete_object(Bucket=S3_BUCKET_NAME, Key=cache_entry.response_s3_key)
                        logger.info('[CACHE CLEANUP] Deleted S3 cache: %s', ("" if cache_entry.response_s3_key is None else str(cache_entry.response_s3_key).replace("\n", "").replace("\r", "")))
                    except Exception as e:
                        logger.warning('[CACHE CLEANUP] Failed to delete S3 cache %s: %s', ("" if cache_entry.response_s3_key is None else str(cache_entry.response_s3_key).replace("\n", "").replace("\r", "")), e)
                # Delete cache entries from database
                db.query(TutorResponseCache).filter(TutorResponseCache.course_id == course.id).delete()
                logger.info('[CACHE CLEANUP] Deleted tutor cache for course: %s', ("" if course.id is None else str(course.id).replace("\n", "").replace("\r", "")))
            
            # Commit database deletions
            db.commit()
            logger.info('Successfully deleted database records for user: %s', ("" if user_id is None else str(user_id).replace("\n", "").replace("\r", "")))
        except Exception as e:
            db.rollback()
            logger.error('Failed to delete database records for user %s: %s', ("" if user_id is None else str(user_id).replace("\n", "").replace("\r", "")), e, exc_info=True)
            raise IOError("Failed to clear database records.")
    
    # Clear Pinecone vectors
    try:
        logger.info('Deleting Pinecone vectors in namespace: %s', ("" if user_id is None else str(user_id).replace("\n", "").replace("\r", "")))
        pinecone_index.delete(delete_all=True, namespace=user_id)
    except NotFoundException:
        logger.warning("Namespace '%s' not found in Pinecone during deletion. Nothing to delete.", ("" if user_id is None else str(user_id).replace("\n", "").replace("\r", "")))
        pass
    except Exception as e:
        logger.error('Failed to delete Pinecone data for user %s: %s', ("" if user_id is None else str(user_id).replace("\n", "").replace("\r", "")), e, exc_info=True)
        raise IOError("Failed to clear vector data.")
    
    # Clear S3 files
    try:
        logger.info('Deleting S3 objects under prefix: %s/', ("" if user_id is None else str(user_id).replace("\n", "").replace("\r", "")))
        s3 = boto3.resource('s3')
        bucket = s3.Bucket(S3_BUCKET_NAME)
        bucket.objects.filter(Prefix=f"{user_id}/").delete()
    except Exception as e:
        logger.error('Failed to delete S3 data for user %s: %s', ("" if user_id is None else str(user_id).replace("\n", "").replace("\r", "")), e, exc_info=True)
        raise IOError("Failed to clear file storage data.")

@app.exception_handler(Exception)
async def validation_exception_handler(request: Request, exc: Exception):
    if isinstance(exc, HTTPException):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
    # FastAPI response-model failures are not pydantic.ValidationError — without this
    # they become an opaque "An internal server error occurred."
    try:
        from fastapi.exceptions import ResponseValidationError
        if isinstance(exc, ResponseValidationError):
            logger.error("Response validation error: %s", log_safe(exc.errors()), exc_info=True)
            return JSONResponse(
                status_code=500,
                content={"detail": "Failed to build response. Please try again."},
            )
    except Exception:
        pass
    if isinstance(exc, ValidationError):
        logger.warning("Pydantic Validation Error: %s", log_safe(exc.errors()))
        return JSONResponse(status_code=422, content={"detail": exc.errors()})
    # A pool checkout timeout means the app is saturated, not broken. Answer 503 with a retry
    # hint so the client can back off, and log the pool's own numbers - they say whether
    # DB_POOL_SIZE is too small or a request is holding its session far too long.
    if isinstance(exc, DBPoolTimeoutError):
        logger.error(
            "Database connection pool exhausted (%s): %s",
            log_safe(describe_db_pool()),
            log_safe(exc),
        )
        return JSONResponse(
            status_code=503,
            content={"detail": "The server is busy right now. Please try again in a moment."},
            headers={"Retry-After": "5"},
        )
    logger.error('Unhandled Exception: %s', log_safe(exc), exc_info=True)
    return JSONResponse(status_code=500, content={"detail": "An internal server error occurred."})



__all__ = [name for name in globals() if not name.startswith('_')]
