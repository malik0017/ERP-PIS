# app/core/company.py

from __future__ import annotations

from fastapi import Request
from sqlalchemy import or_
from sqlalchemy import text
from sqlalchemy.orm import Session

DEFAULT_COMPANY_ID = 1
INCLUDE_UNASSIGNED = True
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


def order_company_id(db: Session, order_no: str):
    try:
        return db.execute(
            text("SELECT company_id FROM customer_orders WHERE order_no = :o LIMIT 1"),
            {"o": order_no},
        ).scalar()
    except Exception:
        db.rollback()
        return None


def order_in_scope(db: Session, request: Request, order_no: str) -> bool:
    """True when `order_no` belongs to the caller's company (or is unassigned)."""
    if not order_no:
        return False
    owner = order_company_id(db, order_no)
    if owner is None:
        return True
    return int(owner) == get_current_company_id(request)


def require_order_scope(db: Session, request: Request, order_no: str) -> None:
    if not order_in_scope(db, request, order_no):
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Order not found")


def company_clause(alias: str = "", column: str = "company_id", param: str = "scope_cid") -> str:
    p = f"{alias}." if alias else ""
    return f" AND ({p}{column} = :{param} OR {p}{column} IS NULL)"


def require_record_scope(db: Session, request: Request, table: str, pk: int,
                         column: str = "company_id") -> None:
    if pk is None:
        return
    try:
        owner = db.execute(text(f"SELECT {column} FROM {table} WHERE id = :i LIMIT 1"),
                           {"i": pk}).scalar()
    except Exception:
        db.rollback()
        return
    if owner is not None and int(owner) != get_current_company_id(request):
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Record not found")
