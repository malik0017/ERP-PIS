# app/core/pdf_arabic.py

from __future__ import annotations

import logging
import os
import re
from functools import lru_cache

logger = logging.getLogger(__name__)

FONT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static", "fonts")
AR_REGULAR, AR_BOLD = "Amiri", "Amiri-Bold"
FALLBACK_REGULAR, FALLBACK_BOLD = "Helvetica", "Helvetica-Bold"

_ARABIC = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]")


def has_arabic(value) -> bool:
    """True when the value contains any Arabic-script character."""
    return bool(value) and bool(_ARABIC.search(str(value)))


@lru_cache(maxsize=1)
def register_fonts() -> bool:
    """Register Amiri with ReportLab once per process. False if unavailable."""
    try:
        from reportlab.lib.fonts import addMapping
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont

        reg = os.path.join(FONT_DIR, "Amiri-Regular.ttf")
        bold = os.path.join(FONT_DIR, "Amiri-Bold.ttf")
        if not os.path.exists(reg):
            logger.warning("Arabic PDF font missing at %s — PDFs will use Helvetica", reg)
            return False
        pdfmetrics.registerFont(TTFont(AR_REGULAR, reg))
        pdfmetrics.registerFont(TTFont(AR_BOLD, bold if os.path.exists(bold) else reg))
        # So <b> inside a Paragraph resolves to the bold face instead of
        # silently falling back to Helvetica mid-sentence.
        addMapping(AR_REGULAR, 0, 0, AR_REGULAR)
        addMapping(AR_REGULAR, 1, 0, AR_BOLD)
        return True
    except Exception as exc:
        logger.warning("Arabic PDF font registration failed: %s", exc)
        return False


@lru_cache(maxsize=1)
def _shapers():
    try:
        import arabic_reshaper
        from bidi.algorithm import get_display
        return arabic_reshaper.reshape, get_display
    except Exception as exc:
        logger.warning("Arabic shaping unavailable (%s) — Arabic text will print "
                       "unshaped. Install arabic-reshaper and python-bidi.", exc)
        return None, None


def shape(value) -> str:
    """Make a string safe to DRAW in a PDF: shaped, then bidi-reordered.

    Latin-only text is returned untouched — running every cell through the
    bidi algorithm would be wasted work on a mostly-English document.
    """
    if value is None:
        return ""
    text = strip_unsupported(str(value))
    if not _ARABIC.search(text):
        return text
    reshape, get_display = _shapers()
    if not reshape:
        return text
    try:
        return get_display(reshape(strip_unsupported(text)))
    except Exception as exc:
        logger.warning("Arabic shaping failed: %s", exc)
        return text


_UNSUPPORTED = str.maketrans({"\u2713": "Y", "\u2717": "N", "\u2718": "N",
                              "\u25b2": "^", "\u25bc": "v", "\u21c5": ""})


def strip_unsupported(text: str) -> str:
    """Amiri covers Arabic and Latin, not dingbats. A tick or a sort arrow
    would render as a missing-glyph box, so map the few we actually emit."""
    return (text or "").translate(_UNSUPPORTED)


def font_names(lang: str | None = None, sample: str | None = None) -> tuple[str, str]:
    """(regular, bold) to use for this document.

    Arabic font is chosen when the UI language is Arabic OR the content itself
    contains Arabic — a customer with an Arabic name on an English document
    still needs the glyphs.
    """
    wants_ar = (lang or "").lower().startswith("ar") or has_arabic(sample)
    if wants_ar and register_fonts():
        return AR_REGULAR, AR_BOLD
    return FALLBACK_REGULAR, FALLBACK_BOLD


def is_rtl(lang: str | None) -> bool:
    return (lang or "").lower().startswith("ar")
