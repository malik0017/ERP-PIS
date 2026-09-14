# app/modules/exports/routes_pdf.py
# =============================================================================
# Batch 222 — SERVER-SIDE TABLE PDF (so Arabic can actually be printed)
# -----------------------------------------------------------------------------
# The browser-side exporter (jsPDF) cannot draw Arabic: its built-in fonts have
# no Arabic glyphs, so Batch 122 stripped Arabic text before export rather than
# emit black boxes. That was the right call at the time, but it means an
# Arabic-speaking customer receives a document with the Arabic missing.
#
# Rather than embed a font and a shaping engine in the browser, the table is
# posted here and rendered with ReportLab, which already produces the picking
# list and the QC certificate. One endpoint serves every `data-isfc-table` in
# the system, and the styling matches the house design from Batch 211.
#
# The browser keeps its client-side path for Latin-only tables — it is instant
# and needs no round trip. The PDF button switches to this endpoint only when
# the page is in Arabic or the table actually contains Arabic text.
# =============================================================================
from __future__ import annotations

import io
import json
from datetime import datetime
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app.core.pdf_arabic import font_names, has_arabic, is_rtl, shape
from app.core.rbac import require_area

router = APIRouter(prefix="/export", tags=["Exports"])

NAVY = colors.HexColor("#132947")
GRID = colors.HexColor("#dfe7f0")
ZEBRA = colors.HexColor("#f7fafd")

# Same tinting vocabulary as the client-side exporter, so a document looks the
# same whichever path produced it.
TINTS = [
    (("short",), "#fde8e8", "#a42323"),
    (("excess", "over-issued", "over issued"), "#fff1e0", "#b45309"),
    (("pending", "waiting"), "#fff8e0", "#92600a"),
    (("issued", "exact", "passed", "delivered", "transferred", "complete"), "#e2f6e9", "#0a7a33"),
    (("reject", "fail", "delay", "late", "overdue"), "#fde8e8", "#a42323"),
]


def _tint(value: str):
    low = value.strip().lower()
    if not low or len(low) > 28:
        return None
    for words, bg, fg in TINTS:
        if any(w in low for w in words):
            return colors.HexColor(bg), colors.HexColor(fg)
    return None


@router.post("/table-pdf")
async def table_pdf(request: Request):
    """Render a posted table as a PDF. Arabic-capable.

    Body: {title, subtitle, meta: [[label, value]…], head: [...], body: [[...]],
           lang, landscape: bool}
    """
    # Same gate as the screen the table came from: anyone who can read the
    # dashboard can export what is already on their screen. The payload is the
    # user's own rendered table — no new data is reachable through this route.
    require_area(request, "dashboard")

    try:
        payload = json.loads((await request.body()).decode("utf-8") or "{}")
    except Exception:
        payload = {}

    title = str(payload.get("title") or "Export")
    subtitle = str(payload.get("subtitle") or "")
    meta = payload.get("meta") or []
    head = [str(h) for h in (payload.get("head") or [])]
    body = [[("" if c is None else str(c)) for c in row] for row in (payload.get("body") or [])]
    lang = payload.get("lang") or request.session.get("lang") or "en"
    wide = bool(payload.get("landscape", len(head) > 7))

    sample = " ".join([title, subtitle] + head[:6] + [" ".join(r[:4]) for r in body[:8]])
    regular, bold = font_names(lang, sample)
    rtl = is_rtl(lang) or has_arabic(sample)
    align = TA_RIGHT if rtl else TA_LEFT

    styles = getSampleStyleSheet()
    cell = ParagraphStyle("c", parent=styles["Normal"], fontName=regular, fontSize=7,
                          leading=9, alignment=align, wordWrap="RTL" if rtl else None)
    cell_h = ParagraphStyle("h", parent=cell, fontName=bold, textColor=colors.white)
    title_s = ParagraphStyle("t", parent=styles["Title"], fontName=bold, fontSize=15,
                             leading=18, alignment=align, textColor=colors.white, spaceAfter=0)
    sub_s = ParagraphStyle("s", parent=styles["Normal"], fontName=regular, fontSize=7.5,
                           alignment=align, textColor=colors.HexColor("#cfe0f5"))
    fact_l = ParagraphStyle("fl", parent=styles["Normal"], fontName=regular, fontSize=6.5,
                            alignment=align, textColor=colors.HexColor("#6b7a90"))
    fact_v = ParagraphStyle("fv", parent=styles["Normal"], fontName=bold, fontSize=9,
                            alignment=align, textColor=NAVY)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4) if wide else A4,
                            leftMargin=12 * mm, rightMargin=12 * mm,
                            topMargin=10 * mm, bottomMargin=12 * mm, title=title)

    stamp = f"{datetime.now().strftime('%Y-%m-%d %H:%M')} · ISFC PIMS"
    band = Table([[Paragraph(shape(title), title_s)],
                  [Paragraph(shape(" · ".join(x for x in [subtitle, stamp] if x)), sub_s)]],
                 colWidths=[doc.width])
    band.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), NAVY),
        ("LEFTPADDING", (0, 0), (-1, -1), 10), ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (0, 0), 8), ("BOTTOMPADDING", (0, -1), (-1, -1), 8),
    ]))
    elems = [band, Spacer(1, 4 * mm)]

    if meta:
        per = 4
        row, grid = [], []
        for m in meta:
            row.append(Table([[Paragraph(shape(str(m[0])).upper(), fact_l)],
                              [Paragraph(shape(str(m[1])), fact_v)]]))
            if len(row) == per:
                grid.append(row); row = []
        if row:
            row += [""] * (per - len(row))
            grid.append(row)
        facts = Table(grid, colWidths=[doc.width / per] * per)
        facts.setStyle(TableStyle([
            ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#d6deea")),
            ("INNERGRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#d6deea")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 6), ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        elems += [facts, Spacer(1, 4 * mm)]

    if head:
        # RTL reads right to left, so the column order is mirrored too —
        # shaping the words but leaving the columns in Latin order would put
        # the first column where an Arabic reader expects the last.
        cols = list(reversed(head)) if rtl else head
        rows = [list(reversed(r)) if rtl else r for r in body]
        data = [[Paragraph(shape(h), cell_h) for h in cols]]
        tints = []
        for r in rows:
            data.append([Paragraph(shape(c), cell) for c in r])
            for ci, raw in enumerate(r):
                t = _tint(raw)
                if t:
                    tints.append((len(data) - 1, ci, t[0], t[1]))
        style = [
            ("BACKGROUND", (0, 0), (-1, 0), NAVY),
            ("GRID", (0, 0), (-1, -1), 0.4, GRID),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, ZEBRA]),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]
        for ri, ci, bg, fg in tints:
            style.append(("BACKGROUND", (ci, ri), (ci, ri), bg))
        tbl = Table(data, colWidths=[doc.width / max(len(cols), 1)] * len(cols), repeatRows=1)
        tbl.setStyle(TableStyle(style))
        elems.append(tbl)

    doc.build(elems)
    buf.seek(0)
    # HTTP headers are latin-1. An Arabic title crashes the response unless the
    # filename is ASCII, so send an ASCII fallback plus RFC 5987 filename* with
    # the real one — browsers use the second and save the Arabic name.
    ascii_name = "".join(ch if ch.isascii() and (ch.isalnum() or ch in "-_") else "_"
                         for ch in title)[:60].strip("_") or "export"
    utf8_name = quote(f"{title[:80]}.pdf")
    return StreamingResponse(
        buf, media_type="application/pdf",
        headers={"Content-Disposition":
                 f'attachment; filename="{ascii_name}.pdf"; filename*=UTF-8\'\'{utf8_name}'})
