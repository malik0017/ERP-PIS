# app/modules/requisitions/routes.py
# =============================================================================
# Batch 83 — Requisitions: an approval gate BEFORE an order is created.
# -----------------------------------------------------------------------------
# Where this sits in the pipeline:
#
#   Requisition (NEW) --approved--> Order -> Head Chef -> BOM -> Store ->
#   Kitchen -> QC -> Packing -> Dispatch -> Finance
#
# A Requisition is a request to produce something — raised by Sales, a Head
# Chef, or any department — that needs a manager's sign-off BEFORE material
# and kitchen time get committed to it. Nothing about the existing 9-stage
# order pipeline changes: once a requisition is approved, "Convert to
# Order" calls the exact same create_order() service every other order
# creation path already uses (manual entry, customer portal, subscriptions),
# so from that point on it's a completely normal order going through the
# unchanged existing flow.
#
# Table auto-creates (ensure_schema), matching every other raw-SQL module
# in this codebase (Finance, HR, Subscriptions).
# =============================================================================
from __future__ import annotations

from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import text
from sqlalchemy.orm import Session
from starlette.status import HTTP_303_SEE_OTHER

from app.core.templates import render
from app.core.rbac import require_area, require_action
from app.core.notifications import notify_role
from app.core.company import get_current_company_id
from app.database.session import get_db
from app.services.production_service import create_order
from app.schemas.production import CustomerOrderCreate, OrderLineIn

router = APIRouter(prefix="/requisitions", tags=["Requisitions"])


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
def ensure_schema(db: Session) -> None:
    stmts = [
        """CREATE TABLE IF NOT EXISTS requisitions (
            id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
            company_id INT NULL,
            requisition_no VARCHAR(30) NOT NULL UNIQUE,
            requested_by VARCHAR(120) NULL,
            department VARCHAR(80) NULL,
            customer_no VARCHAR(80) NULL,
            customer_name VARCHAR(255) NULL,
            brand VARCHAR(100) NULL,
            channel VARCHAR(100) NULL,
            required_date DATE NULL,
            justification VARCHAR(500) NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'Pending',
            approved_by VARCHAR(120) NULL,
            approved_at DATETIME NULL,
            rejection_reason VARCHAR(300) NULL,
            order_no VARCHAR(80) NULL,
            created_by VARCHAR(120) NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",
        """CREATE TABLE IF NOT EXISTS requisition_lines (
            id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
            requisition_id INT NOT NULL,
            recipe_no VARCHAR(50) NOT NULL,
            recipe_name VARCHAR(255) NULL,
            portions FLOAT NOT NULL DEFAULT 0,
            KEY idx_rl_req (requisition_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",
    ]
    for s in stmts:
        try:
            db.execute(text(s))
            db.commit()
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass


def _rows(db: Session, sql: str, params: dict | None = None) -> list[dict]:
    try:
        return [dict(r) for r in db.execute(text(sql), params or {}).mappings().all()]
    except Exception:
        return []


def _one(db: Session, sql: str, params: dict | None = None) -> dict | None:
    try:
        r = db.execute(text(sql), params or {}).mappings().first()
        return dict(r) if r else None
    except Exception:
        return None


def _next_no(db: Session) -> str:
    n = int(db.execute(text("SELECT COUNT(*) FROM requisitions")).scalar() or 0)
    return f"REQ-{n + 1:05d}"


def _user(request: Request) -> str:
    return request.session.get("username") or "system"


# ---------------------------------------------------------------------------
# Dashboard / list
# ---------------------------------------------------------------------------
@router.get("")
def requisitions_dashboard(request: Request, db: Session = Depends(get_db)):
    require_area(request, "requisitions")
    ensure_schema(db)
    cid = get_current_company_id(request)
    status_filter = (request.query_params.get("status") or "").strip()
    search = (request.query_params.get("search") or "").strip()

    where = "(company_id = :c OR company_id IS NULL)"
    params: dict = {"c": cid}
    if status_filter:
        where += " AND status = :st"
        params["st"] = status_filter
    if search:
        where += " AND (requisition_no LIKE :s OR customer_name LIKE :s OR requested_by LIKE :s)"
        params["s"] = f"%{search}%"

    reqs = _rows(db, f"""
        SELECT * FROM requisitions WHERE {where}
        ORDER BY FIELD(status,'Pending','Approved','Converted','Rejected'), created_at DESC LIMIT 300
    """, params)
    for r in reqs:
        r["line_count"] = int(_one(db, "SELECT COUNT(*) AS n FROM requisition_lines WHERE requisition_id=:i", {"i": r["id"]})["n"])

    all_for_kpi = _rows(db, "SELECT status FROM requisitions WHERE company_id = :c OR company_id IS NULL", {"c": cid})
    kpis = {
        "pending": len([r for r in all_for_kpi if r["status"] == "Pending"]),
        "approved": len([r for r in all_for_kpi if r["status"] == "Approved"]),
        "converted": len([r for r in all_for_kpi if r["status"] == "Converted"]),
        "rejected": len([r for r in all_for_kpi if r["status"] == "Rejected"]),
    }
    return render(request, "requisitions/list.html", {
        "reqs": reqs, "kpis": kpis, "status_filter": status_filter, "search": search,
        "page_title": "Requisitions",
    })


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------
@router.get("/new")
def new_requisition_form(request: Request, db: Session = Depends(get_db)):
    require_area(request, "requisitions")
    ensure_schema(db)
    cid = get_current_company_id(request)
    customers = _rows(db, "SELECT customer_code, customer_name, COALESCE(brand,'') AS brand "
                          "FROM customers ORDER BY customer_name LIMIT 1000")
    # Batch 85 fix: Brand used to be a freeform text box, not linked to
    # anything — now pulled from the real brands master table, same
    # company-scoping pattern used everywhere else.
    brands = _rows(db, "SELECT brand_code, brand_name_en AS brand_name FROM brands "
                       "WHERE (company_id = :cid OR company_id IS NULL) AND UPPER(TRIM(COALESCE(status,'ACTIVE')))='ACTIVE' "
                       "ORDER BY brand_name_en", {"cid": cid})
    recipes = _rows(db, "SELECT recipe_code, recipe_name FROM recipes "
                        "WHERE UPPER(TRIM(status))='ACTIVE' AND is_active=1 "
                        "GROUP BY recipe_code ORDER BY recipe_name LIMIT 1000")
    # No dedicated "departments" master table exists in this system yet —
    # rather than leave it as an ungrounded free-text box, this is a
    # sensible fixed list matching the departments that actually interact
    # with production (Kitchen sections + the non-kitchen teams that raise
    # requisitions), with a manual "Other" option so nothing is blocked.
    departments = ["Kitchen", "Cutting", "Butchery", "Hot Kitchen", "Cold Kitchen", "Bakery/Pastry",
                  "Store", "Procurement", "Sales", "Catering", "Finance", "HR", "Management", "Other"]
    return render(request, "requisitions/form.html", {
        "customers": customers, "recipes": recipes, "brands": brands, "departments": departments,
        "page_title": "New Requisition", "error": request.query_params.get("error"),
    })


@router.post("/new")
async def create_requisition(request: Request, db: Session = Depends(get_db)):
    require_action(request, "requisitions", "add")
    ensure_schema(db)
    form = await request.form()

    customer_name = (form.get("customer_name") or "").strip()
    justification = (form.get("justification") or "").strip()
    if not justification:
        return RedirectResponse("/requisitions/new?error=A reason/justification is required so the approver has context", status_code=303)

    recipe_no = form.getlist("recipe_no")
    recipe_name = form.getlist("recipe_name")
    portions = form.getlist("portions")
    lines = []
    # Batch 84 fix: this used to require recipe_no (the hidden code field,
    # only ever populated when the typed text exactly matched a real
    # recipe in the datalist) to be non-empty, silently dropping the whole
    # line otherwise — exactly what happened typing "Bread"/"Chicken"
    # freely, which don't exist as recipe codes. Now iterates over
    # recipe_name instead (always populated, whether matched or typed
    # freely), and recipe_no becomes optional: a real code when the recipe
    # was matched from the master list, or a generated "MANUAL-..."
    # placeholder when it's a free-text request that isn't in the recipe
    # master yet — exactly the "let me write it in manually" option asked
    # for. Manual lines are visually flagged later so nobody mistakes a
    # free-text request for a costed, linked recipe.
    for i, name in enumerate(recipe_name):
        name = (name or "").strip()
        if not name:
            continue
        try:
            p = float(portions[i]) if i < len(portions) and portions[i] else 0
        except ValueError:
            p = 0
        if p <= 0:
            continue
        code = (recipe_no[i] if i < len(recipe_no) else "").strip()
        if not code:
            slug = "".join(c if c.isalnum() else "-" for c in name.upper())[:24].strip("-")
            code = f"MANUAL-{slug or 'ITEM'}"
        lines.append((code, name, p))
    if not lines:
        return RedirectResponse("/requisitions/new?error=Add at least one recipe (typed or selected) with portions greater than zero", status_code=303)

    cid = get_current_company_id(request)
    req_no = _next_no(db)
    db.execute(text("""
        INSERT INTO requisitions
            (company_id, requisition_no, requested_by, department, customer_no, customer_name,
             brand, channel, required_date, justification, status, created_by)
        VALUES (:cid, :no, :by, :dept, :cno, :cname, :brand, :channel, :rd, :just, 'Pending', :by2)
    """), {
        "cid": cid, "no": req_no, "by": _user(request),
        "dept": (form.get("department") or "").strip() or None,
        "cno": (form.get("customer_no") or "").strip() or None,
        "cname": customer_name or None,
        "brand": (form.get("brand") or "").strip() or None,
        "channel": (form.get("channel") or "").strip() or None,
        "rd": (form.get("required_date") or "").strip() or None,
        "just": justification, "by2": _user(request),
    })
    db.commit()
    req = _one(db, "SELECT id FROM requisitions WHERE requisition_no=:n", {"n": req_no})
    for rn, rname, p in lines:
        db.execute(text("INSERT INTO requisition_lines (requisition_id, recipe_no, recipe_name, portions) VALUES (:r,:rn,:rname,:p)"),
                  {"r": req["id"], "rn": rn, "rname": rname, "p": p})
    db.commit()

    # Real notification — the approver needs to know something is waiting,
    # not have to remember to go check the requisitions list.
    notify_role(db, company_id=cid, role="MANAGER",
                title=f"New requisition {req_no} awaiting approval",
                message=f"{_user(request)} · {customer_name or 'Internal'} · {justification[:120]}",
                url=f"/requisitions/{req['id']}", category="requisition_submitted")
    notify_role(db, company_id=cid, role="HEAD_CHEF",
                title=f"New requisition {req_no} awaiting approval",
                message=f"{_user(request)} · {customer_name or 'Internal'} · {justification[:120]}",
                url=f"/requisitions/{req['id']}", category="requisition_submitted")

    return RedirectResponse(f"/requisitions/{req['id']}?toast=success&title=Requisition Submitted&msg={req_no} sent for approval", status_code=303)


# ---------------------------------------------------------------------------
# Detail + lifecycle
# ---------------------------------------------------------------------------
@router.get("/{req_id}")
def requisition_detail(request: Request, req_id: int, db: Session = Depends(get_db)):
    require_area(request, "requisitions")
    ensure_schema(db)
    req = _one(db, "SELECT * FROM requisitions WHERE id=:i", {"i": req_id})
    if not req:
        return RedirectResponse("/requisitions?toast=danger&title=Not+found&msg=Requisition not found", status_code=303)
    lines = _rows(db, "SELECT * FROM requisition_lines WHERE requisition_id=:i", {"i": req_id})
    return render(request, "requisitions/detail.html", {
        "req": req, "lines": lines, "page_title": req["requisition_no"],
    })


@router.post("/{req_id}/approve")
async def approve_requisition(request: Request, req_id: int, db: Session = Depends(get_db)):
    require_action(request, "requisitions", "edit")
    ensure_schema(db)
    req = _one(db, "SELECT * FROM requisitions WHERE id=:i", {"i": req_id})
    if not req:
        return RedirectResponse("/requisitions?toast=danger&title=Not+found&msg=Requisition not found", status_code=303)
    if req["status"] != "Pending":
        return RedirectResponse(f"/requisitions/{req_id}?toast=warning&title=Already Decided&msg=This requisition is already {req['status']}", status_code=303)

    db.execute(text("UPDATE requisitions SET status='Approved', approved_by=:by, approved_at=:at WHERE id=:i"),
              {"by": _user(request), "at": datetime.utcnow(), "i": req_id})
    db.commit()
    notify_role(db, company_id=req.get("company_id"), role="ADMIN",
                title=f"Requisition {req['requisition_no']} approved",
                message=f"Approved by {_user(request)} — ready to convert to an order.",
                url=f"/requisitions/{req_id}", category="requisition_approved")
    return RedirectResponse(f"/requisitions/{req_id}?toast=success&title=Approved&msg=Requisition approved — convert it to an order when ready", status_code=303)


@router.post("/{req_id}/reject")
async def reject_requisition(request: Request, req_id: int, db: Session = Depends(get_db)):
    require_action(request, "requisitions", "edit")
    ensure_schema(db)
    form = await request.form()
    reason = (form.get("rejection_reason") or "").strip()
    req = _one(db, "SELECT * FROM requisitions WHERE id=:i", {"i": req_id})
    if not req:
        return RedirectResponse("/requisitions?toast=danger&title=Not+found&msg=Requisition not found", status_code=303)
    if req["status"] != "Pending":
        return RedirectResponse(f"/requisitions/{req_id}?toast=warning&title=Already Decided&msg=This requisition is already {req['status']}", status_code=303)

    db.execute(text("UPDATE requisitions SET status='Rejected', approved_by=:by, approved_at=:at, rejection_reason=:r WHERE id=:i"),
              {"by": _user(request), "at": datetime.utcnow(), "r": reason or None, "i": req_id})
    db.commit()
    return RedirectResponse(f"/requisitions/{req_id}?toast=warning&title=Rejected&msg=Requisition rejected", status_code=303)


@router.post("/{req_id}/convert-to-order")
async def convert_to_order(request: Request, req_id: int, db: Session = Depends(get_db)):
    """The one moment this feature touches the existing pipeline: once
    approved, this calls the exact same create_order() every other order
    path uses. From here on it's a completely normal order — Head Chef,
    BOM, Store, Kitchen, QC, Packing, Dispatch, all unchanged."""
    require_action(request, "requisitions", "edit")
    ensure_schema(db)
    req = _one(db, "SELECT * FROM requisitions WHERE id=:i", {"i": req_id})
    if not req:
        return RedirectResponse("/requisitions?toast=danger&title=Not+found&msg=Requisition not found", status_code=303)
    if req["status"] != "Approved":
        return RedirectResponse(f"/requisitions/{req_id}?toast=warning&title=Not Approved&msg=Only an approved requisition can be converted to an order", status_code=303)

    lines = _rows(db, "SELECT * FROM requisition_lines WHERE requisition_id=:i", {"i": req_id})
    if not lines:
        return RedirectResponse(f"/requisitions/{req_id}?toast=danger&title=No Lines&msg=This requisition has no recipe lines to order", status_code=303)

    cid = get_current_company_id(request)
    payload = CustomerOrderCreate(
        customer_no=req.get("customer_no"),
        customer_name=req.get("customer_name") or "Internal Requisition",
        brand=req.get("brand"),
        channel=req.get("channel"),
        order_type="Requisition",
        priority="Normal",
        required_delivery_date=req.get("required_date") or (date.today() + timedelta(days=3)),
        notes=f"Created from requisition {req['requisition_no']} — {req.get('justification') or ''}",
        lines=[OrderLineIn(recipe_no=l["recipe_no"], recipe_name=l.get("recipe_name"),
                           required_portions=float(l["portions"] or 0)) for l in lines],
    )
    try:
        order = create_order(db, payload, created_by=f"{_user(request)} (requisition {req['requisition_no']})", company_id=cid)
    except ValueError as exc:
        return RedirectResponse(f"/requisitions/{req_id}?toast=danger&title=Could Not Create Order&msg={str(exc)[:200]}", status_code=303)

    db.execute(text("UPDATE requisitions SET status='Converted', order_no=:o WHERE id=:i"), {"o": order.order_no, "i": req_id})
    db.commit()
    return RedirectResponse(f"/production/orders/{order.order_no}?toast=success&title=Order Created&msg=Requisition {req['requisition_no']} converted to order {order.order_no} — now in the normal approval pipeline", status_code=303)
