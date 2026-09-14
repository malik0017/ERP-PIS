# app/core/db_read.py
# =============================================================================
# Batch 221 — READ HELPERS THAT DO NOT LIE
# -----------------------------------------------------------------------------
# Nearly every screen in this system has a private helper shaped like:
#
#     def _rows(db, sql, params=None):
#         try:
#             return [dict(r) for r in db.execute(text(sql), params or {}).mappings().all()]
#         except Exception:
#             return []          # <-- the problem
#
# An empty list is indistinguishable from a rejected query, so a broken panel
# renders its "nothing here yet" state and nobody investigates. That exact
# pattern has now produced three separate bugs in this project:
#
#   Batch 203  DELAYED is a reserved word -> four KPI cards silently read 0
#   Batch 212  co.total_portions does not exist -> "Recent Orders" showed
#              "No orders yet." on a database with orders in it, for weeks
#   Batch 216  LINES is a reserved word -> the Order 360 Execution tab was
#              empty with no error anywhere
#
# Each was found by accident. None of them raised, logged, or looked wrong.
#
# This module is the one implementation the whole codebase now shares. It still
# degrades gracefully — a failing panel must not take the page down — but it
# ALWAYS logs, with the caller's name, so the failure is visible in the server
# log the first time it happens rather than months later on a screenshot.
#
#     from app.core.db_read import rows, one, scalar
#
#     data = rows(db, "SELECT ...", {"o": order_no}, label="recent orders")
#
# `label` is optional; without it the caller's module and function are used.
# =============================================================================
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
    """Public entry point for the existing per-module read helpers.

    Batch 221 converted 26 of them in place rather than rewriting every call
    site: each keeps its own signature and return value, and adds one line so
    the failure reaches the log. New code should use rows()/one()/scalar()
    below instead.
    """
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
    """Single value. `default` is returned on failure AND on no rows — pass
    None where those two cases must stay distinguishable."""
    try:
        v = db.execute(text(sql), params or {}).scalar()
        return default if v is None else v
    except Exception as exc:
        _fail(exc, sql, label)
        db.rollback()
        return default


def number(db: Session, sql: str, params: dict | None = None,
           label: str | None = None) -> float:
    """Numeric scalar, 0.0 on failure or no rows.

    Use this only where zero is a sensible reading. For a RATE — waste %,
    yield %, pass rate — return None instead: "nothing produced today" and
    "0% yield" are different facts, and a rate that reads 0 because its query
    broke will be believed.
    """
    v = scalar(db, sql, params, default=0, label=label)
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def rows_or_error(db: Session, sql: str, params: dict | None = None,
                  label: str | None = None) -> tuple[list[dict], str | None]:
    """(rows, error). For panels that should TELL the user they are broken
    rather than claim to be empty — the Recent Orders fix in Batch 212 works
    this way, and any panel showing an empty state should."""
    try:
        return [dict(r) for r in db.execute(text(sql), params or {}).mappings().all()], None
    except Exception as exc:
        _fail(exc, sql, label)
        db.rollback()
        return [], str(exc).splitlines()[0][:200]
