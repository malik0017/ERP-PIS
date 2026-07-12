# app/core/company.py
"""
Multi-company (multi-tenant) context and query-scoping helpers.

Design
------
Every logged-in user belongs to a company (users.company_id). At login we copy
that id into the session as ``company_id``. From then on, every request resolves
its active company from the session, and all company-scoped queries are filtered
through :func:`scope`. Privileged roles (SUPER_ADMIN / ADMIN) may switch the
active company for their session via :func:`set_active_company`.

Transition safety
-----------------
``scope`` is intentionally NULL-tolerant: it returns rows for the active company
*and* rows whose company_id is still NULL (legacy / not-yet-stamped writes). This
guarantees that no record ever "disappears" while we finish stamping every write
path. Once all inserts call :func:`stamp`, you can tighten ``scope`` by setting
INCLUDE_UNASSIGNED = False below for strict tenant isolation.
"""
from __future__ import annotations

from fastapi import Request
from sqlalchemy import or_
from sqlalchemy import text
from sqlalchemy.orm import Session

# The company every legacy row was backfilled to (see the SQL migration).
DEFAULT_COMPANY_ID = 1

# While write paths are being stamped, also show rows with NULL company_id.
# Flip to False for strict isolation once every insert calls stamp().
INCLUDE_UNASSIGNED = True

# Roles allowed to switch between companies in one session.
COMPANY_ADMIN_ROLES = {"SUPER_ADMIN", "ADMIN", "ADMINISTRATOR"}


def get_current_company_id(request: Request) -> int:
    """Resolve the active company id for this request from the session."""
    try:
        cid = request.session.get("company_id")
    except Exception:
        cid = None
    try:
        return int(cid) if cid else DEFAULT_COMPANY_ID
    except (TypeError, ValueError):
        return DEFAULT_COMPANY_ID


def current_company(request: Request) -> int:
    """FastAPI dependency form: ``company_id: int = Depends(current_company)``."""
    return get_current_company_id(request)


def scope(query, model, company_id: int):
    """Filter a SQLAlchemy query by company when the model carries company_id.

    Models without a ``company_id`` attribute are returned untouched, so this is
    safe to call uniformly across the codebase.
    """
    col = getattr(model, "company_id", None)
    if col is None or company_id is None:
        return query
    if INCLUDE_UNASSIGNED:
        return query.filter(or_(col == company_id, col.is_(None)))
    return query.filter(col == company_id)


def stamp(obj, company_id: int):
    """Set company_id on a new ORM object before insert, if it has the column."""
    if hasattr(obj, "company_id") and getattr(obj, "company_id", None) is None:
        obj.company_id = company_id
    return obj


def is_company_admin(request: Request) -> bool:
    role = str(request.session.get("user_role") or "").upper().replace(" ", "_").strip()
    return role in COMPANY_ADMIN_ROLES


def set_active_company(request: Request, company_id: int) -> None:
    """Switch the session's active company (privileged users only — check first)."""
    request.session["company_id"] = int(company_id)


def list_companies(db: Session) -> list[dict]:
    """All companies, for the admin company switcher UI."""
    rows = db.execute(text(
        "SELECT id, name, COALESCE(NULLIF(name_ar, ''), name) AS name_ar "
        "FROM companies ORDER BY id"
    )).mappings().all()
    return [dict(r) for r in rows]