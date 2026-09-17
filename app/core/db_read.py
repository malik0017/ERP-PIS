# app/core/db_read.py

from __future__ import annotations

import inspect
import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger("isfc.db_read")


def _caller(label: str | None) -> str:
    if label:
        return label
    try:
        f = inspect.stack()[4]
        return f"{f.frame.f_globals.get('__name__', '?')}.{f.function}"
    except Exception:
        return "?"


def log_failure(exc: Exception, sql: str = "", label: str | None = None) -> None:
   
    _fail(exc, sql if isinstance(sql, str) else "", label)


def _fail(exc: Exception, sql: str, label: str | None) -> None:
    first = str(exc).splitlines()[0][:300]
    logger.warning("query failed in %s: %s | SQL: %s",
                   _caller(label), first, " ".join(sql.split())[:220])


def rows(db: Session, sql: str, params: dict | None = None,
         label: str | None = None) -> list[dict]:
    """All rows as dicts. Returns [] on failure — and says so in the log."""
    try:
        return [dict(r) for r in db.execute(text(sql), params or {}).mappings().all()]
    except Exception as exc:
        _fail(exc, sql, label)
        db.rollback()
        return []


def one(db: Session, sql: str, params: dict | None = None,
        label: str | None = None) -> dict:
    """First row as a dict, or {} — an empty dict, never a half-built one."""
    r = rows(db, sql, params, label)
    return r[0] if r else {}


def scalar(db: Session, sql: str, params: dict | None = None,
           default: Any = None, label: str | None = None) -> Any:
    try:
        v = db.execute(text(sql), params or {}).scalar()
        return default if v is None else v
    except Exception as exc:
        _fail(exc, sql, label)
        db.rollback()
        return default


def number(db: Session, sql: str, params: dict | None = None,
           label: str | None = None) -> float:
    
    v = scalar(db, sql, params, default=0, label=label)
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def rows_or_error(db: Session, sql: str, params: dict | None = None,
                  label: str | None = None) -> tuple[list[dict], str | None]:
    
    try:
        return [dict(r) for r in db.execute(text(sql), params or {}).mappings().all()], None
    except Exception as exc:
        _fail(exc, sql, label)
        db.rollback()
        return [], str(exc).splitlines()[0][:200]
