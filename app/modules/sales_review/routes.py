# app/modules/sales_review/routes.py

from __future__ import annotations

from datetime import date, datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.templates import render
from app.core.rbac import require_area, require_action
from app.core.notifications import notify_role
from app.database.session import get_db
from app.models.production import CustomerOrder, OrderLine
from app.services.production_service import preview_bom_shortages

router = APIRouter(prefix="/sales-requests", tags=["Sales Requests"])


def _cid(request: Request) -> int:
    return int(request.session.get("company_id") or 1)


def _user(request: Request) -> str:
    return request.session.get("username", "system")


def _company_filter(cid: int):
    """Legacy rows created before multi-company scoping have company_id NULL.
    Same (company_id = :cid OR company_id IS NULL) pattern used everywhere
    else in this codebase — dropping the NULL branch would hide historical
    orders entirely."""
    return (CustomerOrder.company_id == cid) | (CustomerOrder.company_id.is_(None))


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------
@router.get("")
def sales_request_list(request: Request, db: Session = Depends(get_db)):
    require_area(request, "sales_review")
    from app.modules.production.routes import _ensure_sales_review_schema
    _ensure_sales_review_schema(db)

    cid = _cid(request)
    status_f = (request.query_params.get("status") or "Pending").strip()
    search = (request.query_params.get("search") or "").strip()
    date_from = (request.query_params.get("date_from") or "").strip()
    date_to = (request.query_params.get("date_to") or "").strip()

    q = db.query(CustomerOrder).filter(_company_filter(cid))
    if status_f and status_f.lower() != "all":
        q = q.filter(func_coalesce_status() == status_f)
    if search:
        like = f"%{search}%"
        q = q.filter((CustomerOrder.order_no.like(like)) | (CustomerOrder.customer_name.like(like)))
    if date_from:
        q = q.filter(CustomerOrder.order_date >= date_from)
    if date_to:
        q = q.filter(CustomerOrder.order_date <= date_to)

    orders = q.order_by(
        CustomerOrder.required_delivery_date.is_(None),
        CustomerOrder.required_delivery_date.asc(),
        CustomerOrder.id.desc(),
    ).limit(200).all()


    stock: dict[str, dict] = {}
    for o in orders:
        try:
            sh = preview_bom_shortages(db, o.order_no)
        except Exception:
            sh = []
        line_count = db.query(OrderLine).filter(OrderLine.order_no == o.order_no).count()
        stock[o.order_no] = {
            "short_count": len(sh),
            "verdict": "SHORT" if sh else ("EMPTY" if line_count == 0 else "OK"),
            "worst": sh[0] if sh else None,
            "shortages": sh[:5],
        }

    all_q = db.query(CustomerOrder).filter(_company_filter(cid))
    counts = {
        "pending": all_q.filter(func_coalesce_status() == "Pending").count(),
        "approved": all_q.filter(func_coalesce_status() == "Approved").count(),
        "rejected": all_q.filter(func_coalesce_status() == "Rejected").count(),
        "short": sum(1 for v in stock.values() if v["verdict"] == "SHORT"),
    }

    return render(request, "sales_review/list.html", {
        "orders": orders, "stock": stock, "counts": counts,
        "filters": {"status": status_f, "search": search,
                    "date_from": date_from, "date_to": date_to},
        "status_options": ["Pending", "Approved", "Rejected", "All"],
        "today": date.today(),
        "page_title": "Sales Requests — Approval",
    })


def func_coalesce_status():
    """COALESCE(sales_review_status, 'Approved').

    The DB-level default on the column is 'Approved' so historical orders
    from before Batch 88 aren't retroactively blocked, but rows that predate
    the column being added can still be NULL. Treating NULL as 'Approved'
    keeps old orders flowing instead of silently trapping them in a review
    queue that didn't exist when they were raised.
    """
    from sqlalchemy import func
    return func.coalesce(CustomerOrder.sales_review_status, "Approved")


# ---------------------------------------------------------------------------
# Detail
# ---------------------------------------------------------------------------
@router.get("/{order_no}")
def sales_request_detail(request: Request, order_no: str, db: Session = Depends(get_db)):
    require_area(request, "sales_review")
    from app.modules.production.routes import _ensure_sales_review_schema
    _ensure_sales_review_schema(db)

    cid = _cid(request)
    order = db.query(CustomerOrder).filter(
        CustomerOrder.order_no == order_no, _company_filter(cid)
    ).first()
    if not order:
        return RedirectResponse(
            f"/sales-requests?toast=danger&title=Not found&msg={order_no} not found for this company.",
            status_code=303)

    lines = db.query(OrderLine).filter(OrderLine.order_no == order_no).order_by(OrderLine.line_no).all()
    shortages = preview_bom_shortages(db, order_no)

    # Everything the order needs, short or not — the screen has to answer
    # "is the inventory there or empty", which means showing the covered
    # items too, not only the gaps.
    coverage = _full_coverage(db, order_no, cid)

    existing_prs = db.execute(text("""
        SELECT pr_no, status, created_at, converted_po_nos
        FROM purchase_requisitions
        WHERE source_type = 'Order Shortage' AND source_ref = :o
        ORDER BY id DESC
    """), {"o": order_no}).mappings().all() if _pr_table_exists(db) else []

    return render(request, "sales_review/detail.html", {
        "order": order, "lines": lines, "shortages": shortages,
        "coverage": coverage, "existing_prs": existing_prs,
        "page_title": f"Sales Request {order_no}",
    })


def _pr_table_exists(db: Session) -> bool:
    try:
        return bool(db.execute(text("""
            SELECT COUNT(*) FROM information_schema.tables
            WHERE table_schema = DATABASE() AND table_name = 'purchase_requisitions'
        """)).scalar())
    except Exception:
        return False


def _full_coverage(db: Session, order_no: str, cid: int) -> list[dict]:
    """Every ingredient this request needs, with on-hand and a verdict.

    Built from the shortage explosion plus the BOM's own required list so
    the reviewer sees full coverage, not just the failures. Deliberately
    reuses the shortage service's numbers for the short items so the two
    views on this screen can never disagree with each other.
    """
    shortages = {s["ingredient_code"]: s for s in preview_bom_shortages(db, order_no)}
    rows = db.execute(text("""
        SELECT DISTINCT ri.inventory_code, ri.item_name,
               COALESCE(i.standard_uom, ri.uom, '') AS uom
        FROM order_lines ol
        JOIN recipes r ON r.recipe_code = ol.recipe_no
        JOIN recipe_ingredients ri ON ri.recipe_id = r.id
        LEFT JOIN ingredients i ON i.ingredient_code = ri.inventory_code
        WHERE ol.order_no = :o AND ri.inventory_code IS NOT NULL AND ri.inventory_code <> ''
        ORDER BY ri.item_name
    """), {"o": order_no}).mappings().all()

    if not rows:
        return []

    from app.modules.purchase_req.routes import on_hand_map
    on_hand = on_hand_map(db, [r["inventory_code"] for r in rows], cid)

    out = []
    for r in rows:
        code = r["inventory_code"]
        s = shortages.get(code)
        oh = on_hand.get(code, 0.0)
        out.append({
            "inventory_code": code,
            "item_name": r["item_name"],
            "uom": s["standard_uom"] if s else r["uom"],
            "required_qty": s["required_qty"] if s else None,
            "on_hand": round(oh, 3),
            "shortfall": s["shortfall"] if s else 0,
            "verdict": "SHORT" if s else ("EMPTY" if oh <= 0 else "OK"),
        })
    return sorted(out, key=lambda x: (x["verdict"] != "SHORT", x["verdict"] != "EMPTY", x["item_name"]))


# ---------------------------------------------------------------------------
# Approve / Reject
# ---------------------------------------------------------------------------
@router.post("/{order_no}/approve")
async def approve(request: Request, order_no: str, db: Session = Depends(get_db)):
    """Approve the request. THIS is the only thing that makes the order
    visible to Head Chef Planning — before it, the order exists but is
    invisible downstream."""
    require_action(request, "sales_review", "edit")
    from app.modules.production.routes import _ensure_sales_review_schema
    _ensure_sales_review_schema(db)

    cid = _cid(request)
    order = db.query(CustomerOrder).filter(
        CustomerOrder.order_no == order_no, _company_filter(cid)).first()
    if not order:
        return RedirectResponse("/sales-requests?toast=danger&title=Not found&msg=Request not found",
                                status_code=303)

    current = order.sales_review_status or "Approved"
    if current != "Pending":
        return RedirectResponse(
            f"/sales-requests?toast=warning&title=Already reviewed"
            f"&msg={order_no} is already {current}.", status_code=303)

    order.sales_review_status = "Approved"
    order.sales_reviewed_by = _user(request)
    order.sales_reviewed_at = datetime.utcnow()
    db.commit()

    notify_role(db, company_id=order.company_id or cid, role="HEAD_CHEF",
                title=f"Order {order_no} approved for planning",
                message=f"{order.customer_name} — approved by {_user(request)}, ready for Head Chef scheduling.",
                url=f"/production/orders/{order_no}", category="sales_review_approved")

    nxt = _next_pending(db, cid)
    target = f"/sales-requests/{nxt}" if nxt else "/sales-requests"
    return RedirectResponse(
        f"{target}?toast=success&title=Approved"
        f"&msg={order_no} approved — now visible to Head Chef Planning."
        + ("" if nxt else " No more requests pending."),
        status_code=303)


@router.post("/{order_no}/reject")
async def reject(request: Request, order_no: str, db: Session = Depends(get_db)):
    require_action(request, "sales_review", "edit")
    from app.modules.production.routes import _ensure_sales_review_schema
    _ensure_sales_review_schema(db)

    form = await request.form()
    reason = (form.get("reason") or "").strip()
    if not reason:
        return RedirectResponse(
            f"/sales-requests/{order_no}?toast=warning&title=Reason required"
            "&msg=Record why this request was rejected before rejecting it.", status_code=303)

    cid = _cid(request)
    order = db.query(CustomerOrder).filter(
        CustomerOrder.order_no == order_no, _company_filter(cid)).first()
    if not order:
        return RedirectResponse("/sales-requests?toast=danger&title=Not found&msg=Request not found",
                                status_code=303)
    if (order.sales_review_status or "Approved") != "Pending":
        return RedirectResponse(
            f"/sales-requests?toast=warning&title=Already reviewed&msg={order_no} is already reviewed.",
            status_code=303)

    order.sales_review_status = "Rejected"
    order.sales_reviewed_by = _user(request)
    order.sales_reviewed_at = datetime.utcnow()
    order.sales_review_reason = reason[:500]
    db.commit()

    nxt = _next_pending(db, cid)
    target = f"/sales-requests/{nxt}" if nxt else "/sales-requests"
    return RedirectResponse(
        f"{target}?toast=warning&title=Rejected&msg={order_no} rejected — it will not reach Head Chef Planning.",
        status_code=303)


def _next_pending(db: Session, cid: int) -> str | None:
    """After a decision, jump straight to the next request awaiting review.
    Reviewing 40 requests should not mean 40 round-trips through the list."""
    row = db.query(CustomerOrder.order_no).filter(
        _company_filter(cid), func_coalesce_status() == "Pending"
    ).order_by(
        CustomerOrder.required_delivery_date.is_(None),
        CustomerOrder.required_delivery_date.asc(),
        CustomerOrder.id.asc(),
    ).first()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# Raise a Purchase Requisition for the shortage
# ---------------------------------------------------------------------------
@router.post("/{order_no}/raise-pr")
async def raise_pr(request: Request, order_no: str, db: Session = Depends(get_db)):
    """Shortage -> Purchase REQUISITION (not a Purchase Order).

    This replaces Batch 86's generate-shortage-po. The difference matters:
    that route created a live PO, which meant anyone who could see a
    shortage could effectively commit the company to a purchase. Now it
    raises a request that Procurement has to approve and price before any
    PO exists.
    """
    require_action(request, "sales_review", "edit")
    from app.modules.purchase_req.routes import create_requisition, ensure_schema as pr_ensure
    pr_ensure(db)

    cid = _cid(request)
    order = db.query(CustomerOrder).filter(
        CustomerOrder.order_no == order_no, _company_filter(cid)).first()
    if not order:
        return RedirectResponse("/sales-requests?toast=danger&title=Not found&msg=Request not found",
                                status_code=303)

    shortages = preview_bom_shortages(db, order_no)
    if not shortages:
        return RedirectResponse(
            f"/sales-requests/{order_no}?toast=warning&title=No shortage"
            "&msg=Every ingredient for this request is covered by available stock right now.",
            status_code=303)

    codes = [s["ingredient_code"] for s in shortages]
    placeholders = ",".join(f":c{i}" for i in range(len(codes)))
    params = {f"c{i}": c for i, c in enumerate(codes)}
    
    sup_rows = db.execute(text(f"""
        SELECT ingredient_code, COALESCE(default_supplier,'') AS supplier,
               COALESCE(unit_cost_standard, 0) AS price
        FROM ingredients WHERE ingredient_code IN ({placeholders})
    """), params).mappings().all()
    meta = {r["ingredient_code"]: r for r in sup_rows}

    lines = [{
        "inventory_code": s["ingredient_code"],
        "item_name": s["ingredient_name"],
        "uom": s["standard_uom"],
        "required_qty": s["shortfall"],
        "on_hand_qty": s["available_qty"],
        "suggested_supplier": (meta.get(s["ingredient_code"], {}) or {}).get("supplier", ""),
        "estimated_price": float((meta.get(s["ingredient_code"], {}) or {}).get("price", 0) or 0),
        "line_remarks": f"Needed for {order_no} ({', '.join(s['recipes'][:2])})"[:255],
    } for s in shortages]

    pr_no = create_requisition(
        db, company_id=cid, requested_by=_user(request), lines=lines,
        department="Sales / Production Planning",
        source_type="Order Shortage", source_ref=order_no,
        required_date=order.required_delivery_date,
        justification=f"Ingredient shortage blocking order {order_no} "
                      f"for {order.customer_name or 'internal'}.",
    )

    return RedirectResponse(
        f"/sales-requests/{order_no}?toast=success&title=Requisition Raised"
        f"&msg={pr_no} sent to Procurement for review — no Purchase Order has been created yet.",
        status_code=303)
