"""Generate rich SOP-style assessment PDF reports (reference: asm-bea1a45c40b5 template)."""

from __future__ import annotations

import html
from collections import defaultdict
from datetime import datetime

from utils.timezone_helpers import format_ist_display, now_ist
from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    HRFlowable,
    KeepTogether,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

from utils.voice_training.schemas import AssessmentResult

# ── Palette ───────────────────────────────────────────────────────────────────
C_NAVY = colors.HexColor("#0f172a")
C_BLUE = colors.HexColor("#0369a1")
C_ACCENT = colors.HexColor("#0ea5e9")
C_STEEL = colors.HexColor("#334155")
C_GRAY = colors.HexColor("#64748b")
C_LIGHT = colors.HexColor("#f1f5f9")
C_BORDER = colors.HexColor("#e2e8f0")
C_GREEN = colors.HexColor("#15803d")
C_GREEN_BG = colors.HexColor("#dcfce7")
C_RED = colors.HexColor("#dc2626")
C_RED_BG = colors.HexColor("#fee2e2")
C_ORANGE = colors.HexColor("#d97706")
C_ORANGE_BG = colors.HexColor("#fffbeb")
C_WHITE = colors.white

PAGE_W, PAGE_H = letter
MARGIN = 0.75 * inch
HEADER_H = 0.76 * inch
FOOTER_H = 0.65 * inch
CONTENT_W = PAGE_W - 2 * MARGIN

EVAL_CATEGORY_ORDER = [
    "Intent Understanding",
    "Resolution",
    "SOP Compliance",
    "Tone",
    "Voice Quality",
    "Dialogue Management",
    "Performance",
]


def _clean_text(text: str) -> str:
    """Normalize text and decode HTML entities (e.g. &#x27; → ')."""
    return html.unescape(str(text or "")).replace("\n", " ").strip()


def _esc(text: str) -> str:
    return html.escape(_clean_text(text))


def _split_csv_tail(value: str) -> list[str]:
    """Split 'Label: a, b, c' into ['a', 'b', 'c']."""
    tail = value.split(":", 1)[1] if ":" in value else value
    return [p.strip() for p in tail.replace("…", "").split(",") if p.strip()]


def _parse_highlights(highlights: list[str]) -> dict[str, list[str]]:
    parsed: dict[str, list[str]] = {
        "completed_sop": [],
        "missed_sop": [],
        "strengths": [],
        "improve": [],
        "other": [],
    }
    for raw in highlights:
        h = _clean_text(raw)
        lower = h.lower()
        if lower.startswith("completed sop"):
            parsed["completed_sop"] = _split_csv_tail(h)
        elif lower.startswith("missed required"):
            parsed["missed_sop"] = _split_csv_tail(h)
        elif lower.startswith("strong areas"):
            parsed["strengths"] = _split_csv_tail(h)
        elif lower.startswith("improve"):
            parsed["improve"] = _split_csv_tail(h)
        elif lower.startswith("contextual turns"):
            parsed["other"].append(h)
        else:
            parsed["other"].append(h)
    return parsed


def _bullets_html(items: list[str], indent: bool = False) -> str:
    if not items:
        return ""
    prefix = "&nbsp;&nbsp;" if indent else ""
    return "".join(f"{prefix}• {_esc(item)}<br/>" for item in items)


def _structured_highlights_html(parsed: dict[str, list[str]]) -> str:
    parts: list[str] = []
    if parsed["strengths"]:
        parts.append('<font color="#15803d"><b>✓ Strengths</b></font><br/>')
        parts.append(_bullets_html(parsed["strengths"]))
        parts.append("<br/>")
    if parsed["improve"]:
        parts.append('<font color="#dc2626"><b>× Areas to Improve</b></font><br/>')
        parts.append(_bullets_html(parsed["improve"]))
        parts.append("<br/>")
    if parsed["completed_sop"]:
        parts.append("<b>Completed SOP steps</b><br/>")
        parts.append(_bullets_html(parsed["completed_sop"]))
        parts.append("<br/>")
    if parsed["missed_sop"]:
        parts.append("<b>Missed SOP steps</b><br/>")
        parts.append(_bullets_html(parsed["missed_sop"]))
        parts.append("<br/>")
    if parsed["other"]:
        parts.append(_bullets_html(parsed["other"]))
    return "".join(parts).strip() or _esc("No highlights recorded.")


def _metric_strengths_html(metrics) -> str:
    passed = [m for m in metrics if m.passed]
    lines = ['<font color="#15803d"><b>✓ Strengths</b></font><br/><br/>']
    if not passed:
        lines.append(_esc("No parameters passed threshold.") + "<br/>")
    else:
        for m in passed[:8]:
            lines.append(f"• <b>{_esc(m.parameter)}</b> — {m.score:.1f}/5<br/>")
    return "".join(lines)


def _metric_weaknesses_html(metrics) -> str:
    weak = [m for m in metrics if not m.passed]
    lines = ['<font color="#dc2626"><b>× Areas to Improve</b></font><br/><br/>']
    if not weak:
        lines.append(_esc("All parameters met expectations.") + "<br/>")
    else:
        for m in weak[:6]:
            lines.append(f"• <b>{_esc(m.parameter)}</b> — {m.score:.1f}/5<br/>")
            if m.remarks:
                lines.append(f"&nbsp;&nbsp;&nbsp;<i>{_esc(m.remarks)}</i><br/>")
            lines.append("<br/>")
    return "".join(lines)


def _cell_para(text: str, style_name: str = "TableCell") -> Paragraph:
    return Paragraph(_esc(text), _styles()[style_name])


def _sentiment_val(val) -> str:
    if val is None:
        return "neutral"
    if hasattr(val, "value"):
        return str(val.value)
    return str(val)


def _fmt_dt(dt) -> str:
    if dt is None:
        return format_ist_display(now_ist())
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
        except ValueError:
            return dt[:16]
    return format_ist_display(dt)


def _category_label(category) -> str:
    val = category.value if hasattr(category, "value") else str(category)
    return val.replace("_", " ").title()


def _mini_bar(score: float, max_score: float = 5.0, width: float = 1.15 * inch) -> Table:
    pct = max(0.0, min(float(score or 0) / max_score, 1.0))
    if pct <= 0.01:
        empty = Table([[""]], colWidths=[width], rowHeights=[6])
        empty.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), C_BORDER),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ]))
        return empty
    fill_w = max(width * pct, 0.02)
    empty_w = max(width - fill_w, 0.02)
    bar_color = C_GREEN if score >= 3 else C_ORANGE if score >= 2.5 else C_RED
    bar = Table(
        [[""], [""]],
        colWidths=[fill_w],
        rowHeights=[3, 3],
    )
    bar.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), bar_color),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    track = Table(
        [[bar, ""]],
        colWidths=[fill_w, empty_w],
        rowHeights=[6],
    )
    track.setStyle(TableStyle([
        ("BACKGROUND", (1, 0), (1, 0), C_BORDER),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    return track


def _status_cell(passed: bool, score: float | None = None) -> Paragraph:
    if passed:
        return Paragraph('<font color="#15803d"><b>PASS</b></font>', _styles()["Tiny"])
    if score is not None and score < 3:
        return Paragraph('<font color="#dc2626"><b>NEEDS WORK</b></font>', _styles()["Tiny"])
    return Paragraph('<font color="#dc2626"><b>REVIEW</b></font>', _styles()["Tiny"])


def _section_title(text: str) -> list:
    st = _styles()
    return [
        Spacer(1, 0.12 * inch),
        Paragraph(text, st["SectionH"]),
        HRFlowable(width="100%", thickness=2, color=C_ACCENT, spaceAfter=8, spaceBefore=2),
    ]


_STYLE_CACHE: dict[str, ParagraphStyle] | None = None


def _styles() -> dict[str, ParagraphStyle]:
    global _STYLE_CACHE
    if _STYLE_CACHE is not None:
        return _STYLE_CACHE

    base = getSampleStyleSheet()

    def add(name: str, parent="Normal", **kw):
        if name in base:
            return
        base.add(ParagraphStyle(name=name, parent=base[parent], **kw))

    add("SectionH", fontSize=14, textColor=C_NAVY, fontName="Helvetica-Bold", spaceAfter=4)
    add("SubH", fontSize=11, textColor=C_BLUE, fontName="Helvetica-Bold", spaceBefore=8, spaceAfter=4)
    add("Body", fontSize=9.5, leading=13, textColor=C_STEEL, spaceAfter=6)
    add("Small", fontSize=8, leading=10, textColor=C_GRAY, spaceAfter=3)
    add("Tiny", fontSize=7.5, leading=9, textColor=C_GRAY)
    add("HeroTitle", fontSize=16, leading=20, textColor=C_NAVY, fontName="Helvetica-Bold", spaceAfter=2)
    add("HeroSub", fontSize=9, textColor=C_GRAY, spaceAfter=6)
    add("GradeBig", fontSize=28, textColor=C_BLUE, fontName="Helvetica-Bold", alignment=TA_CENTER)
    add("GradePct", fontSize=14, textColor=C_BLUE, fontName="Helvetica-Bold", alignment=TA_CENTER)
    add("GradeSub", fontSize=8, textColor=C_GRAY, alignment=TA_CENTER)
    add("KpiLabel", fontSize=7, textColor=C_GRAY, fontName="Helvetica-Bold", alignment=TA_CENTER)
    add("KpiVal", fontSize=11, textColor=C_NAVY, fontName="Helvetica-Bold", alignment=TA_CENTER)
    add("CalloutTitle", fontSize=9, textColor=C_NAVY, fontName="Helvetica-Bold", spaceAfter=3)
    add("Bullet", fontSize=8.5, leading=12, textColor=C_STEEL, leftIndent=10, spaceAfter=2)
    add("BulletItem", fontSize=8.5, leading=13, textColor=C_STEEL, leftIndent=14, spaceBefore=1, spaceAfter=3)
    add("TableHeader", fontSize=7.5, leading=10, textColor=C_WHITE, fontName="Helvetica-Bold")
    add("TableCell", fontSize=7.5, leading=11, textColor=C_STEEL, spaceAfter=1)
    add("TableCellDesc", fontSize=7.5, leading=12, textColor=C_STEEL, spaceAfter=2)
    add("TranscriptCustomer", fontSize=8.5, leading=11, textColor=C_STEEL,
        backColor=colors.HexColor("#fff7ed"), spaceAfter=4)
    add("TranscriptAgent", fontSize=8.5, leading=11, textColor=C_STEEL,
        backColor=colors.HexColor("#eff6ff"), spaceAfter=4)
    add("TurnHead", fontSize=9, textColor=C_NAVY, fontName="Helvetica-Bold", spaceAfter=2)

    _STYLE_CACHE = dict(base.byName)
    return _STYLE_CACHE


def _header_footer(canvas, doc):
    canvas.saveState()
    w, h = PAGE_W, PAGE_H
    canvas.setFillColor(C_NAVY)
    canvas.rect(0, h - HEADER_H, w, HEADER_H, fill=1, stroke=0)
    canvas.setFillColor(C_ACCENT)
    canvas.rect(0, h - HEADER_H - 0.04 * inch, w, 0.04 * inch, fill=1, stroke=0)

    canvas.setFillColor(C_WHITE)
    canvas.setFont("Helvetica-Bold", 10.5)
    canvas.drawString(MARGIN, h - 0.38 * inch, "AI Voice Agent Training Platform")
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(C_ACCENT)
    canvas.drawString(MARGIN, h - 0.54 * inch, "Assessment Report \u00b7 Confidential")

    title = getattr(doc, "scenario_title", "Assessment")
    title_short = (title[:40] + "\u2026") if len(title) > 41 else title
    canvas.setFillColor(C_WHITE)
    canvas.setFont("Helvetica-Bold", 9.5)
    canvas.drawRightString(w - MARGIN, h - 0.38 * inch, title_short)
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(C_ACCENT)
    canvas.drawRightString(w - MARGIN, h - 0.54 * inch, getattr(doc, "assessment_id", ""))

    canvas.setStrokeColor(C_BORDER)
    canvas.line(MARGIN, FOOTER_H - 0.08 * inch, w - MARGIN, FOOTER_H - 0.08 * inch)
    canvas.setFillColor(C_GRAY)
    canvas.setFont("Helvetica", 7.5)
    canvas.drawString(MARGIN, 0.38 * inch, f"Generated {getattr(doc, 'generated_at', '')}")
    canvas.drawCentredString(w / 2, 0.38 * inch, "Voice Agent Training Platform \u2014 Internal Use Only")
    canvas.drawRightString(w - MARGIN, 0.38 * inch, f"Page {doc.page}")
    canvas.restoreState()


def _hero_block(assessment: AssessmentResult, summary: dict) -> list:
    st = _styles()
    meta = assessment.metadata or {}
    avg_ctx = meta.get("avg_combined_match_pct") or meta.get("avg_context_match_pct", 0)
    if not avg_ctx and meta.get("turn_analysis"):
        turns = meta["turn_analysis"]
        avg_ctx = sum(t.get("combined_pct", 0) for t in turns) / max(len(turns), 1)

    sent_start = _sentiment_val(assessment.sentiment_start)
    sent_end = _sentiment_val(assessment.sentiment_end)
    outcome = summary.get("call_outcome") or ("Resolved" if assessment.fcr_achieved else "In progress")

    grade_cell = Table([
        [Paragraph(assessment.grade, st["GradeBig"])],
        [Paragraph(f"{assessment.final_percentage:.1f}%", st["GradePct"])],
        [Paragraph(f"{assessment.final_score:.2f} / 5.0 Overall", st["GradeSub"])],
    ], colWidths=[1.1 * inch])
    grade_cell.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f0f9ff")),
        ("BOX", (0, 0), (-1, -1), 1, C_ACCENT),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))

    title_row = Table([
        [
            Paragraph(_esc(assessment.scenario_title), st["HeroTitle"]),
            grade_cell,
        ]
    ], colWidths=[CONTENT_W - 1.25 * inch, 1.25 * inch])
    title_row.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ]))

    kpi_data = [
        [Paragraph("SOP COMPLIANCE", st["KpiLabel"]), Paragraph("FIRST CONTACT RESOLUTION", st["KpiLabel"]),
         Paragraph("CUSTOMER SENTIMENT", st["KpiLabel"]), Paragraph("COMBINED MATCH", st["KpiLabel"]),
         Paragraph("RESPONSES / TURNS", st["KpiLabel"])],
        [
            Paragraph(f"{assessment.sop_compliance_rate:.1f}%", st["KpiVal"]),
            Paragraph("Achieved" if assessment.fcr_achieved else "Not achieved", st["KpiVal"]),
            Paragraph(f"{sent_start.title()} \u2192 {sent_end.title()}", st["KpiVal"]),
            Paragraph(f"{avg_ctx:.0f}%", st["KpiVal"]),
            Paragraph(
                f"{summary.get('agent_response_count', 0)} / {assessment.metadata.get('total_turns', len(assessment.conversation))}",
                st["KpiVal"],
            ),
        ],
    ]
    kpi_table = Table(kpi_data, colWidths=[CONTENT_W / 5] * 5)
    kpi_table.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.5, C_BORDER),
        ("INNERGRID", (0, 0), (-1, -1), 0.25, C_BORDER),
        ("BACKGROUND", (0, 0), (-1, 0), C_LIGHT),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))

    quick_stats = Table([
        [
            Paragraph(f"<b>Outcome</b><br/>{_esc(outcome)}", st["Small"]),
            Paragraph(f"<b>Sentiment</b><br/>{sent_start.title()} \u2192 {sent_end.title()}", st["Small"]),
            Paragraph(f"<b>Grade</b><br/>{assessment.grade} ({assessment.final_percentage:.1f}%)", st["Small"]),
        ]
    ], colWidths=[CONTENT_W / 3] * 3)
    quick_stats.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f0f9ff")),
        ("BOX", (0, 0), (-1, -1), 0.5, C_BORDER),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
    ]))

    highlights = summary.get("highlights") or []
    actions = summary.get("key_actions") or []
    parsed_hl = _parse_highlights(highlights)
    hl_html = _structured_highlights_html(parsed_hl)
    act_html = _bullets_html([_clean_text(a) for a in actions[:6]]) or _esc("No agent actions detected.")

    highlights_box = Table([
        [Paragraph("<b>Key Highlights</b>", st["CalloutTitle"])],
        [Paragraph(hl_html, st["BulletItem"])],
    ], colWidths=[CONTENT_W / 2 - 0.12 * inch])
    highlights_box.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.5, C_BORDER),
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f8fafc")),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
    ]))

    actions_box = Table([
        [Paragraph("<b>Agent Actions</b>", st["CalloutTitle"])],
        [Paragraph(act_html, st["BulletItem"])],
    ], colWidths=[CONTENT_W / 2 - 0.12 * inch])
    actions_box.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.5, C_BORDER),
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f8fafc")),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
    ]))

    two_col = Table([[highlights_box, actions_box]], colWidths=[CONTENT_W / 2, CONTENT_W / 2])
    two_col.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))

    preview_lines = []
    for item in (summary.get("transcript_preview") or [])[:6]:
        who = "Customer" if item.get("speaker") == "customer" else "Agent"
        preview_lines.append(
            f"#{item.get('turn', '?')} {who}: {_esc(item.get('preview', ''))}"
        )
    remaining = max(0, len(assessment.conversation) - 6)
    snap_text = "<br/>".join(preview_lines)
    if remaining:
        snap_text += f"<br/><i>\u2026 and {remaining} more messages in full transcript.</i>"

    return [
        title_row,
        Paragraph(
            f"{_category_label(assessment.category)} \u00b7 {_fmt_dt(assessment.created_at)}",
            st["HeroSub"],
        ),
        Spacer(1, 0.08 * inch),
        kpi_table,
        Spacer(1, 0.12 * inch),
        Paragraph("Call Summary", st["SectionH"]),
        quick_stats,
        Spacer(1, 0.08 * inch),
        Paragraph(_esc(summary.get("narrative") or assessment.remarks), st["Body"]),
        Spacer(1, 0.1 * inch),
        two_col,
        Spacer(1, 0.12 * inch),
        Paragraph("Transcript Snapshot", st["SubH"]),
        Paragraph(snap_text, st["Small"]),
    ]


def _details_table(assessment: AssessmentResult) -> list:
    st = _styles()
    sent = f"{_sentiment_val(assessment.sentiment_start).title()} \u2192 {_sentiment_val(assessment.sentiment_end).title()}"
    rows = [
        ["Assessment ID", assessment.assessment_id, "Session ID", assessment.session_id],
        ["User ID", assessment.user_id, "Category", _category_label(assessment.category)],
        ["Date Evaluated", _fmt_dt(assessment.created_at), "Mode", getattr(assessment.mode, "value", assessment.mode or "assessment").title()],
        ["Sentiment Journey", sent, "Grade", assessment.grade],
    ]
    data = []
    for r in rows:
        data.append([
            Paragraph(f"<b>{_esc(r[0])}</b>", st["Small"]),
            Paragraph(_esc(r[1]), st["Small"]),
            Paragraph(f"<b>{_esc(r[2])}</b>", st["Small"]),
            Paragraph(_esc(r[3]), st["Small"]),
        ])
    tbl = Table(data, colWidths=[1.15 * inch, 2.0 * inch, 1.15 * inch, 2.0 * inch])
    tbl.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.5, C_BORDER),
        ("INNERGRID", (0, 0), (-1, -1), 0.25, C_BORDER),
        ("BACKGROUND", (0, 0), (0, -1), C_LIGHT),
        ("BACKGROUND", (2, 0), (2, -1), C_LIGHT),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
    ]))
    return _section_title("Assessment Details") + [tbl]


def _metrics_section(assessment: AssessmentResult) -> list:
    st = _styles()
    metrics = assessment.metrics or []
    passed = sum(1 for m in metrics if m.passed)
    below = len(metrics) - passed
    weighted = assessment.final_score

    summary_row = Table([
        [
            Paragraph(f"<b>{passed}</b><br/><font size='7'>PASSED</font>", st["Small"]),
            Paragraph(f"<b><font color='#dc2626'>{below}</font></b><br/><font size='7'>NEEDS WORK</font>", st["Small"]),
            Paragraph(f"<b>{len(metrics)}</b><br/><font size='7'>TOTAL</font>", st["Small"]),
            Paragraph(f"<b>{weighted:.2f}</b><br/><font size='7'>WEIGHTED / 5</font>", st["Small"]),
        ]
    ], colWidths=[CONTENT_W / 4] * 4)
    summary_row.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.5, C_BORDER),
        ("INNERGRID", (0, 0), (-1, -1), 0.25, C_BORDER),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f8fafc")),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))

    by_cat: dict[str, list] = defaultdict(list)
    for m in metrics:
        by_cat[m.category].append(m)

    story: list = _section_title("Evaluation Metrics") + [summary_row, Spacer(1, 0.1 * inch)]

    for cat in EVAL_CATEGORY_ORDER:
        items = by_cat.get(cat)
        if not items:
            continue
        avg = sum(m.score for m in items) / len(items)
        story.append(Paragraph(f"{_esc(cat)} \u00b7 Avg {avg:.1f}/5", st["SubH"]))

        header = [
            Paragraph("<b>Parameter</b>", st["Tiny"]),
            Paragraph("<b>Score</b>", st["Tiny"]),
            Paragraph("<b>Bar</b>", st["Tiny"]),
            Paragraph("<b>Weight</b>", st["Tiny"]),
            Paragraph("<b>Pts</b>", st["Tiny"]),
            Paragraph("<b>Status</b>", st["Tiny"]),
            Paragraph("<b>Remarks</b>", st["Tiny"]),
        ]
        rows = [header]
        for m in items:
            rows.append([
                Paragraph(_esc(m.parameter), st["Tiny"]),
                Paragraph(f"{m.score:.1f}/5", st["Tiny"]),
                _mini_bar(m.score),
                Paragraph(f"{m.weight:.1%}", st["Tiny"]),
                Paragraph(f"{m.weighted_score:.2f}", st["Tiny"]),
                _status_cell(m.passed, m.score),
                Paragraph(_esc(m.remarks)[:200], st["Tiny"]),
            ])
        tbl = Table(
            rows,
            colWidths=[1.15 * inch, 0.55 * inch, 1.2 * inch, 0.55 * inch, 0.45 * inch, 0.75 * inch, 2.0 * inch],
            repeatRows=1,
        )
        tbl.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), C_NAVY),
            ("TEXTCOLOR", (0, 0), (-1, 0), C_WHITE),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("BOX", (0, 0), (-1, -1), 0.5, C_BORDER),
            ("INNERGRID", (0, 0), (-1, -1), 0.25, C_BORDER),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [C_WHITE, C_LIGHT]),
        ]))
        story.append(tbl)
        story.append(Spacer(1, 0.08 * inch))
    return story


def _sop_coaching_section(assessment: AssessmentResult, summary: dict, sop_activities: list) -> list:
    st = _styles()
    story = _section_title("SOP Compliance & Coaching Insights")

    if sop_activities:
        story.append(Paragraph("Standard Operating Procedure Checklist", st["SubH"]))
        st_tbl = _styles()
        header = [
            Paragraph("<b>#</b>", st_tbl["TableHeader"]),
            Paragraph("<b>Activity</b>", st_tbl["TableHeader"]),
            Paragraph("<b>Description</b>", st_tbl["TableHeader"]),
            Paragraph("<b>Required</b>", st_tbl["TableHeader"]),
            Paragraph("<b>Status</b>", st_tbl["TableHeader"]),
        ]
        rows = [header]
        for i, act in enumerate(sop_activities, 1):
            if isinstance(act, dict):
                name = act.get("name") or ""
                desc = act.get("description") or ""
                req = act.get("required", True)
                done = act.get("completed")
            else:
                name, desc, req, done = act.name, act.description, act.required, act.completed
            status_html = (
                '<font color="#15803d"><b>✓ COMPLETED</b></font>'
                if done
                else '<font color="#dc2626"><b>✗ MISSED</b></font>'
            )
            rows.append([
                Paragraph(str(i), st_tbl["TableCell"]),
                _cell_para(name, "TableCell"),
                _cell_para(desc, "TableCellDesc"),
                Paragraph("Yes" if req else "No", st_tbl["TableCell"]),
                Paragraph(status_html, st_tbl["TableCell"]),
            ])
        # Activity + Description use Paragraph cells so long text wraps instead of overlapping.
        tbl = Table(
            rows,
            colWidths=[0.28 * inch, 1.45 * inch, 3.62 * inch, 0.48 * inch, 0.82 * inch],
            repeatRows=1,
        )
        tbl.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), C_NAVY),
            ("BOX", (0, 0), (-1, -1), 0.5, C_BORDER),
            ("INNERGRID", (0, 0), (-1, -1), 0.25, C_BORDER),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [C_WHITE, C_LIGHT]),
        ]))
        story.append(tbl)
        story.append(Spacer(1, 0.1 * inch))

    str_box = Table([
        [Paragraph(_metric_strengths_html(assessment.metrics), st["BulletItem"])],
    ], colWidths=[CONTENT_W / 2 - 0.12 * inch])
    str_box.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.5, C_GREEN),
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f0fdf4")),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
    ]))
    weak_box = Table([
        [Paragraph(_metric_weaknesses_html(assessment.metrics), st["BulletItem"])],
    ], colWidths=[CONTENT_W / 2 - 0.12 * inch])
    weak_box.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.5, C_RED),
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#fef2f2")),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
    ]))
    story.append(Table([[str_box, weak_box]], colWidths=[CONTENT_W / 2, CONTENT_W / 2]))
    story.append(Spacer(1, 0.1 * inch))
    story.append(Paragraph("Coach Summary", st["SubH"]))
    story.append(Paragraph(_esc(assessment.remarks), st["Body"]))
    return story


def _turn_analysis_section(turn_analysis: list, avg_ctx: float) -> list:
    st = _styles()
    story = _section_title("Contextual Turn Analysis")
    story.append(Paragraph(
        f"Paraphrasing is allowed \u2014 scores reflect intent and relevance, not exact wording. "
        f"Average context match: {avg_ctx:.0f}%.",
        st["Small"],
    ))
    story.append(Spacer(1, 0.06 * inch))

    for t in turn_analysis:
        passed = t.get("passed")
        status = "PASS" if passed else "NEEDS WORK"
        color = "#15803d" if passed else "#dc2626"
        head = (
            f"<b>Turn {t.get('turn_index', '?')}</b> \u2014 "
            f"{t.get('combined_pct', 0):.0f}% match "
            f"(context {t.get('context_match_pct', 0):.0f}%, relevance {t.get('response_relevance_pct', 0):.0f}%) "
            f"<font color='{color}'><b>{status}</b></font>"
        )
        blocks = [
            Paragraph(head, st["TurnHead"]),
            Paragraph(f"<b>Your reply:</b> {_esc(t.get('trainee_text', ''))}", st["Small"]),
        ]
        ref = t.get("reference_text") or t.get("reference_intent") or ""
        if ref:
            blocks.append(Paragraph(f"<b>Reference intent:</b> {_esc(ref)}", st["Small"]))
        blocks.append(Paragraph(f"<i>{_esc(t.get('feedback', ''))}</i>", st["Small"]))
        box = Table([[b] for b in blocks], colWidths=[CONTENT_W])
        box.setStyle(TableStyle([
            ("BOX", (0, 0), (-1, -1), 0.5, C_GREEN if passed else C_ORANGE),
            ("BACKGROUND", (0, 0), (-1, -1), C_GREEN_BG if passed else C_ORANGE_BG),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
            ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ]))
        story.append(KeepTogether([box, Spacer(1, 0.06 * inch)]))
    return story


def _transcript_section(assessment: AssessmentResult, summary: dict) -> list:
    st = _styles()
    sent = f"{_sentiment_val(assessment.sentiment_start).title()} \u2192 {_sentiment_val(assessment.sentiment_end).title()}"
    stats = Table([
        [
            Paragraph(f"<b>{len(assessment.conversation)}</b><br/>MESSAGES", st["Small"]),
            Paragraph(f"<b>{summary.get('agent_response_count', 0)}</b><br/>AGENT REPLIES", st["Small"]),
            Paragraph(f"<b>{summary.get('customer_message_count', 0)}</b><br/>CUSTOMER", st["Small"]),
            Paragraph(f"<b>{sent}</b><br/>SENTIMENT ARC", st["Small"]),
        ]
    ], colWidths=[CONTENT_W / 4] * 4)
    stats.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.5, C_BORDER),
        ("INNERGRID", (0, 0), (-1, -1), 0.25, C_BORDER),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("BACKGROUND", (0, 0), (-1, -1), C_LIGHT),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))

    story = _section_title("Conversation Transcript") + [
        stats,
        Spacer(1, 0.08 * inch),
        Paragraph(
            '<font color="#0ea5e9">\u25a0</font> You (Agent) &nbsp;&nbsp; '
            '<font color="#f97316">\u25a0</font> Customer',
            st["Small"],
        ),
        Spacer(1, 0.06 * inch),
    ]

    for i, turn in enumerate(assessment.conversation, 1):
        is_customer = turn.speaker == "customer"
        role = "CUSTOMER" if is_customer else "YOU (AGENT)"
        ts = ""
        if turn.timestamp:
            try:
                ts = turn.timestamp.strftime("%H:%M")
            except Exception:
                ts = ""
        label = f"<b>{role}</b> \u00b7 Turn {i}" + (f" \u00b7 {ts}" if ts else "")
        pstyle = st["TranscriptCustomer"] if is_customer else st["TranscriptAgent"]
        block = Table([
            [Paragraph(label, st["Tiny"])],
            [Paragraph(_esc(turn.text), pstyle)],
        ], colWidths=[CONTENT_W])
        border = colors.HexColor("#fdba74") if is_customer else C_ACCENT
        bg = colors.HexColor("#fff7ed") if is_customer else colors.HexColor("#eff6ff")
        block.setStyle(TableStyle([
            ("BOX", (0, 0), (-1, -1), 0.5, border),
            ("BACKGROUND", (0, 0), (-1, -1), bg),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ]))
        story.append(block)
        story.append(Spacer(1, 0.04 * inch))
    return story


def generate_assessment_pdf_bytes(assessment: AssessmentResult) -> bytes:
    """Build a multi-page SOP-standard assessment PDF."""
    buffer = BytesIO()
    meta = assessment.metadata or {}
    summary = meta.get("transcript_summary") or {}
    turn_analysis = meta.get("turn_analysis") or []
    sop_activities = meta.get("sop_activities") or []
    avg_ctx = meta.get("avg_context_match_pct", 0)
    if not avg_ctx and turn_analysis:
        avg_ctx = sum(t.get("combined_pct", 0) for t in turn_analysis) / len(turn_analysis)

    doc = BaseDocTemplate(
        buffer,
        pagesize=letter,
        rightMargin=MARGIN,
        leftMargin=MARGIN,
        topMargin=HEADER_H + 0.2 * inch,
        bottomMargin=FOOTER_H + 0.1 * inch,
        title=f"Assessment Report — {assessment.scenario_title}",
    )
    doc.assessment_id = assessment.assessment_id
    doc.scenario_title = assessment.scenario_title
    doc.generated_at = _fmt_dt(assessment.created_at)

    frame = Frame(
        MARGIN,
        FOOTER_H,
        CONTENT_W,
        PAGE_H - HEADER_H - FOOTER_H - 0.3 * inch,
        id="body",
    )
    doc.addPageTemplates([PageTemplate(id="main", frames=[frame], onPage=_header_footer)])

    story: list = []
    story.extend(_hero_block(assessment, summary))
    story.append(PageBreak())
    story.extend(_details_table(assessment))
    story.append(PageBreak())
    story.extend(_metrics_section(assessment))
    story.append(PageBreak())
    story.extend(_sop_coaching_section(assessment, summary, sop_activities))
    if turn_analysis:
        story.append(PageBreak())
        story.extend(_turn_analysis_section(turn_analysis, avg_ctx))
    story.append(PageBreak())
    story.extend(_transcript_section(assessment, summary))

    doc.build(story)
    return buffer.getvalue()
