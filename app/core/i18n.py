# app/core/i18n.py
"""EN/AR localization.

Usage in templates (injected globally by core/templates.py):
    {{ t('sidebar.dashboard') }}
    <html lang="{{ lang }}" dir="{{ 'rtl' if is_rtl else 'ltr' }}">

Dictionaries live in app/i18n/<lang>.json. Missing keys fall back to English,
then to the key itself, so an incomplete Arabic file never breaks a page.
Add languages by dropping a new JSON file (e.g. ur.json) - no code changes.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

I18N_DIR = Path(__file__).resolve().parent.parent / "i18n"
DEFAULT_LANG = "en"
RTL_LANGS = {"ar", "he", "fa", "ur"}
SUPPORTED = ["en", "ar"]


@lru_cache(maxsize=8)
def _load(lang: str) -> dict:
    path = I18N_DIR / f"{lang}.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def translate(key: str, lang: str = DEFAULT_LANG) -> str:
    """Dot-notation lookup: t('sidebar.dashboard')."""
    for candidate in (lang, DEFAULT_LANG):
        node = _load(candidate)
        for part in key.split("."):
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                node = None
                break
        if isinstance(node, str):
            return node
    return key


def lang_from_request(request) -> str:
    lang = (request.session.get("lang") or DEFAULT_LANG).lower()
    return lang if lang in SUPPORTED else DEFAULT_LANG


def is_rtl(lang: str) -> bool:
    return lang in RTL_LANGS
