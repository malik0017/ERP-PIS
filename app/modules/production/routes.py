# app/modules/production/routes.py
from datetime import date, datetime, timedelta
from typing import List, Optional
import csv
import io

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, StreamingResponse
from sqlalchemy import bindparam, func, text
from sqlalchemy.orm import Session
from starlette.status import HTTP_303_SEE_OTHER

from app.core.templates import render
from app.core.rbac import require_area, require_action, can_access
from app.core.notifications import notify_role
from app.database.session import get_db
from app.models.production import (
    BOMLine,
    CustomerOrder,
    KitchenSectionTransaction,
    OrderLine,
    StoreIssuanceLine,
)
from app.models.recipe import Recipe
from app.schemas.production import CustomerOrderCreate, OrderLineIn
from app.services.production_service import (
    approve_head_chef_plan,
    approve_order_before_bom,
    bakery_pastry_consolidated,
    process_bakery_pastry_recipe,
    consolidated_bom,
    create_order,
    finalize_store_issuance,
    generate_bom_for_order,
    preview_bom_shortages,
    receive_transaction,
    transfer_transaction,
    update_store_issuance_line,
)

router = APIRouter(prefix="/production", tags=["Production"])


def _ensure_sales_review_schema(db: Session) -> None:
    """Batch 88 — adds the Sales review checkpoint columns to
    customer_orders if they're not there yet. New column's DB-level
    DEFAULT is 'Approved' so every order that already existed before this
    migration is backfilled automatically and isn't newly blocked; the
    ORM model's own default ('Pending') takes over for every order
    created going forward, which is what actually makes the gate apply.
    """
    try:
        exists = db.execute(text("""
            SELECT COUNT(*) FROM information_schema.columns
            WHERE table_schema = DATABASE() AND table_name = 'customer_orders' AND column_name = 'sales_review_status'
        """)).scalar()
        if not exists:
            db.execute(text("""
                ALTER TABLE customer_orders
                ADD COLUMN sales_review_status VARCHAR(20) DEFAULT 'Approved',
                ADD COLUMN sales_reviewed_by VARCHAR(255) NULL,
                ADD COLUMN sales_reviewed_at DATETIME NULL,
                ADD COLUMN sales_review_reason TEXT NULL
            """))
            db.commit()
    except Exception:
        db.rollback()


@router.post("/orders/{order_no}/sales-approve")
async def sales_approve_order(request: Request, order_no: str, db: Session = Depends(get_db)):
    """Batch 88 — the Sale Requisition approval screen: an explicit
    check on the raw order itself before it's ever visible to the Head
    Chef, distinct from the Head Chef's own scheduling approval later.
    """
    require_action(request, "production_orders", "edit")
    _ensure_sales_review_schema(db)
    order = scoped_order(db, request, order_no)
    if not order:
        raise HTTPException(404, "Order not found")
    if order.sales_review_status == "Pending":
        order.sales_review_status = "Approved"
        order.sales_reviewed_by = current_user_name(request)
        order.sales_reviewed_at = datetime.utcnow()
        db.commit()
        notify_role(db, company_id=order.company_id, role="HEAD_CHEF",
                    title=f"Order {order_no} approved for planning",
                    message=f"{order.customer_name} — approved by {current_user_name(request)}, ready for Head Chef review.",
                    url=f"/production/orders/{order_no}", category="sales_review_approved")
    return RedirectResponse(f"/production/head-chef?toast=success&title=Approved&msg=Order {order_no} approved — now visible to the Head Chef for scheduling", status_code=303)


@router.post("/orders/{order_no}/sales-reject")
async def sales_reject_order(request: Request, order_no: str, db: Session = Depends(get_db)):
    require_action(request, "production_orders", "edit")
    _ensure_sales_review_schema(db)
    form = await request.form()
    reason = (form.get("reason") or "").strip()
    order = scoped_order(db, request, order_no)
    if not order:
        raise HTTPException(404, "Order not found")
    if order.sales_review_status == "Pending":
        order.sales_review_status = "Rejected"
        order.sales_reviewed_by = current_user_name(request)
        order.sales_reviewed_at = datetime.utcnow()
        order.sales_review_reason = reason or None
        db.commit()
    return RedirectResponse(f"/production/orders/{order_no}?toast=warning&title=Rejected&msg=Order rejected — it will not proceed to Head Chef", status_code=303)


@router.post("/orders/{order_no}/generate-shortage-po")
async def generate_shortage_pr(request: Request, order_no: str, db: Session = Depends(get_db)):
    
    require_action(request, "production_orders", "edit")
    from app.modules.purchase_req.routes import create_requisition, ensure_schema as _pr_ensure
    _pr_ensure(db)

    cid = _company_id_from_session(request)
    order = scoped_order(db, request, order_no)
    if not order:
        raise HTTPException(404, "Order not found")

    shortages = preview_bom_shortages(db, order_no)
    if not shortages:
        return RedirectResponse(
            f"/production/orders/{order_no}?toast=warning&title=No Shortage"
            "&msg=No ingredient shortage found for this order right now.", status_code=303)

    open_pr = db.execute(text("""
        SELECT pr_no FROM purchase_requisitions
        WHERE source_type = 'Order Shortage' AND source_ref = :o
          AND status IN ('Pending', 'Approved')
        ORDER BY id DESC LIMIT 1
    """), {"o": order_no}).first()
    if open_pr:
        return RedirectResponse(
            f"/purchase-requisitions/{open_pr[0]}?toast=warning&title=Already Raised"
            f"&msg={open_pr[0]} is already open for this order's shortage — review that one instead of raising a duplicate.",
            status_code=303)

    codes = [s["ingredient_code"] for s in shortages]
    ph = ",".join(f":c{i}" for i in range(len(codes)))
    params = {f"c{i}": c for i, c in enumerate(codes)}
    meta_rows = db.execute(text(f"""
        SELECT ingredient_code, COALESCE(default_supplier,'') AS supplier,
               COALESCE(unit_cost_standard, 0) AS price
        FROM ingredients WHERE ingredient_code IN ({ph})
    """), params).mappings().all()
    meta = {r["ingredient_code"]: r for r in meta_rows}

    lines = [{
        "inventory_code": s["ingredient_code"],
        "item_name": s["ingredient_name"],
        "uom": s["standard_uom"],
        "required_qty": s["shortfall"],
        "on_hand_qty": s["available_qty"],
        "suggested_supplier": (meta.get(s["ingredient_code"]) or {}).get("supplier", ""),
        "estimated_price": float((meta.get(s["ingredient_code"]) or {}).get("price", 0) or 0),
        "line_remarks": f"Shortage on {order_no}"[:255],
    } for s in shortages]

    pr_no = create_requisition(
        db, company_id=cid, requested_by=current_user_name(request), lines=lines,
        department="Production Planning", source_type="Order Shortage", source_ref=order_no,
        required_date=order.material_receiving_date or order.required_delivery_date,
        justification=f"Ingredient shortage blocking order {order_no}"
                      f" for {order.customer_name or 'internal production'}.",
    )

    return RedirectResponse(
        f"/production/orders/{order_no}?toast=success&title=Requisition Raised"
        f"&msg={pr_no} sent to Procurement for review. No purchase order has been created — Procurement decides supplier, price and approval.",
        status_code=303)


def current_user_name(request: Request) -> str:
    return request.session.get("username", "system")


def redirect_with_error(url: str, message: str) -> RedirectResponse:
    sep = "&" if "?" in url else "?"
    return RedirectResponse(f"{url}{sep}error={message}", status_code=HTTP_303_SEE_OTHER)


def _company_id_from_session(request: Request) -> int:
    return int(request.session.get("company_id") or 1)


def _parse_date(value: str | None):
    try:
        return date.fromisoformat(value) if value else None
    except Exception:
        return None


def _date_filter_params(request: Request):
    """Return reusable date/status filters for operational list screens."""
    today = date.today()
    view = request.query_params.get("view", "all")
    status = request.query_params.get("status", "")
    date_from = _parse_date(request.query_params.get("date_from"))
    date_to = _parse_date(request.query_params.get("date_to"))

    if view == "today":
        date_from = today
        date_to = today
    elif view == "week":
        date_from = today - timedelta(days=today.weekday())
        date_to = date_from + timedelta(days=6)
    elif view == "month":
        date_from = today.replace(day=1)
        date_to = today
    elif view == "previous":
        date_to = today - timedelta(days=1)
    elif view == "pending":
        status = status or "pending"

    return view, status, date_from, date_to


def scoped_order(db: Session, request: Request, order_no: str):
    """Batch 96 — fetch an order ONLY if it belongs to the caller's company.

    THE LEAK THIS CLOSES (found by scripts/test_multicompany.py, not reported):

        scoped_order(db, request, order_no)

    appeared 14 times across the production and QC modules with no company
    filter at all. The LIST screens were correctly scoped, so the system
    looked airtight — but any logged-in user of any company could open
    /production/orders/ORD-20260806-0002 directly and read another company's
    order: customer name, recipes, costs, margins, the whole document. Order
    numbers are sequential and guessable.

    List filtering is not access control. This is.

    Returns None when the order belongs to another company, so callers 404
    exactly as they would for a genuinely non-existent order — deliberately
    NOT a 403, which would confirm the order exists.

    The `OR company_id IS NULL` branch is kept for legacy rows written before
    multi-company scoping existed; dropping it would hide historical orders
    from everyone.
    """
    cid = _company_id_from_session(request)
    return (db.query(CustomerOrder)
              .filter(CustomerOrder.order_no == order_no)
              .filter((CustomerOrder.company_id == cid) | (CustomerOrder.company_id.is_(None)))
              .first())


def _filtered_orders_query(db: Session, request: Request, date_column: str = "order_date",
                           exclude_pending_review: bool = True):
   
    view, status, date_from, date_to = _date_filter_params(request)
    q = db.query(CustomerOrder)

    if exclude_pending_review:
        # Rejected is excluded as well as Pending. Caught by the Batch 94
        # functional test: filtering only on "Pending" let a REJECTED request
        # reappear in Head Chef Planning the moment it was rejected, which is
        # worse than the original bug — the reviewer explicitly said no and
        # the order showed up in planning anyway.
        q = q.filter(func.coalesce(CustomerOrder.sales_review_status, "Approved").notin_(
            ["Pending", "Rejected"]))

    col = CustomerOrder.order_date
    if date_column == "delivery":
        col = CustomerOrder.required_delivery_date
    elif date_column == "cooking":
        col = CustomerOrder.cooking_date

    if date_from:
        q = q.filter(col >= date_from)
    if date_to:
        q = q.filter(col <= date_to)

    if status:
        if status == "pending":
            q = q.filter(CustomerOrder.status.in_(["Submitted", "Head Chef Approved", "BOM Generated", "Store Pending", "Packing Pending", "QC Hold"]))
        elif status == "process":
            q = q.filter(CustomerOrder.status.in_(["In Production", "QC In Progress", "Packing In Progress", "Out for Delivery"]))
        else:
            q = q.filter(CustomerOrder.status == status)
    return q, {"view": view, "status": status, "date_from": date_from, "date_to": date_to}


def _order_stats(db: Session):
    today = date.today()
    return {
        "total_orders": db.query(CustomerOrder).count(),
        "today_orders": db.query(CustomerOrder).filter(CustomerOrder.order_date == today).count(),
        "pending_orders": db.query(CustomerOrder).filter(CustomerOrder.status.in_(["Submitted", "Head Chef Approved", "BOM Generated", "Store Pending", "Packing Pending", "QC Hold"])).count(),
        "process_orders": db.query(CustomerOrder).filter(CustomerOrder.status.in_(["In Production", "QC In Progress", "Packing In Progress", "Out for Delivery"])).count(),
        "completed_orders": db.query(CustomerOrder).filter(CustomerOrder.status.in_(["Packed", "Dispatched", "Delivered"])).count(),
    }


def _order_flow_status(db: Session, order_no: str, order=None) -> dict:
    """Batch 97 FIX — NameError: name 'request' is not defined.

    My mistake in Batch 96. The cross-company scoping fix mechanically
    replaced 15 occurrences of

        db.query(CustomerOrder).filter(CustomerOrder.order_no == order_no).first()

    with scoped_order(db, request, order_no). Fourteen of those were inside
    route handlers that take `request`. This one is NOT a route — it is a
    plain helper with the signature (db, order_no), so `request` was simply
    not in scope and the call raised NameError at runtime.

    That made /production/orders/{order_no} 500 on EVERY request, which is
    the single most-used screen in the system.

    The fix is not to thread `request` down into a helper that has no business
    knowing about HTTP. The caller has already fetched (and already scoped)
    the order — so it passes that object in, and the helper stops re-querying
    it entirely. One less query, and the scoping question can't come back
    here because there is no longer a lookup to scope.

    `order` stays optional so any other caller keeps working; when it isn't
    supplied the helper falls back to an UNSCOPED lookup, which is safe only
    because every current caller is a scoped route. That fallback is the
    reason the signature is documented rather than silently changed.
    """
    if order is None:
        order = db.query(CustomerOrder).filter(CustomerOrder.order_no == order_no).first()
    if not order:
        return {}
    bom_count = db.query(BOMLine).filter(BOMLine.order_no == order_no).count()
    store_count = db.query(StoreIssuanceLine).filter(StoreIssuanceLine.order_no == order_no).count()
    store_done = db.query(StoreIssuanceLine).filter(StoreIssuanceLine.order_no == order_no, StoreIssuanceLine.finalized == True).count()
    tx_count = db.query(KitchenSectionTransaction).filter(KitchenSectionTransaction.order_no == order_no).count()
    tx_sections = db.execute(text("""
        SELECT current_section, COUNT(*) AS total_lines
        FROM kitchen_section_transactions
        WHERE order_no = :order_no
          AND UPPER(COALESCE(transaction_status,'')) NOT LIKE 'COMPLETED%'
          AND UPPER(COALESCE(transaction_status,'')) NOT IN ('TRANSFERRED','QC PASSED')
        GROUP BY current_section
        ORDER BY current_section
    """), {"order_no": order_no}).mappings().all()
    qc_count = db.execute(text("SELECT COUNT(*) FROM qc_checks WHERE order_no = :order_no"), {"order_no": order_no}).scalar() or 0
    packing_count = db.execute(text("SELECT COUNT(*) FROM packing_dispatch WHERE order_no = :order_no"), {"order_no": order_no}).scalar() or 0
    if order.status in {"Submitted"}:
        current = "Head Chef scheduling required"
        next_action = "Open Head Chef Planning and approve cooking/material schedule."
    elif order.status == "Head Chef Approved":
        current = "BOM generation required"
        next_action = "Generate production BOM from active recipe master."
    elif order.status == "BOM Generated":
        current = "Release BOM to Store required"
        next_action = "Release generated BOM so Store can issue materials."
    elif order.status == "Store Pending":
        current = "Store issuance required"
        next_action = "Open Store Issuance, adjust issue quantities/sections, then finalize."
    elif order.status in {"In Production", "QC In Progress"}:
        active = ", ".join([f"{r.current_section} ({r.total_lines})" for r in tx_sections]) or "Kitchen/QC"
        current = f"In production: {active}"
        next_action = "Sections receive, process, record waste, and transfer to QC/Packing."
    elif order.status in {"Packing Pending", "Packing In Progress"}:
        current = "Trayline / Packing"
        next_action = "Pack final portions and release to Dispatch."
    elif order.status in {"Packed", "Out for Delivery"}:
        current = "Dispatch / Delivery"
        next_action = "Assign vehicle/driver and close delivery."
    else:
        current = order.status or "Unknown"
        next_action = "Review order document flow."
    return {
        "bom_count": bom_count,
        "store_count": store_count,
        "store_done": store_done,
        "tx_count": tx_count,
        "tx_sections": tx_sections,
        "qc_count": qc_count,
        "packing_count": packing_count,
        "current": current,
        "next_action": next_action,
    }


def _store_order_summary(db: Session, request: Request):
    q, filters = _filtered_orders_query(db, request, date_column="cooking")
    rows = q.order_by(CustomerOrder.cooking_date.desc(), CustomerOrder.required_delivery_date.desc(), CustomerOrder.id.desc()).limit(300).all()
    ids = [o.order_no for o in rows]
    line_map = {}
    if ids:
        placeholders = ",".join([f":o{i}" for i, _ in enumerate(ids)])
        params = {f"o{i}": v for i, v in enumerate(ids)}
        data = db.execute(text(f"""
            SELECT order_no,
                   COUNT(*) AS total_lines,
                   SUM(CASE WHEN finalized = 1 THEN 1 ELSE 0 END) AS finalized_lines,
                   SUM(CASE WHEN COALESCE(issuance_status,'') = 'Short Issued' THEN 1 ELSE 0 END) AS short_lines,
                   ROUND(SUM(COALESCE(required_qty_with_waste_standard,0)),4) AS required_qty,
                   ROUND(SUM(COALESCE(issued_qty_standard,0)),4) AS issued_qty
            FROM store_issuance_lines
            WHERE order_no IN ({placeholders})
            GROUP BY order_no
        """), params).mappings().all()
        line_map = {r.order_no: r for r in data}
    return rows, line_map, filters


def master_dropdown_context(db: Session, company_id: int = 1) -> dict:
    customers = db.execute(text("""
        SELECT customer_code, customer_name, COALESCE(brand,'') AS brand
        FROM customers
        WHERE company_id = :company_id AND UPPER(TRIM(COALESCE(status,''))) = 'ACTIVE'
        ORDER BY customer_name ASC
    """), {"company_id": company_id}).mappings().all()
    brands = db.execute(text("""
        SELECT brand_code, brand_name_en AS brand_name
        FROM brands
        WHERE company_id = :company_id AND UPPER(TRIM(COALESCE(status,''))) = 'ACTIVE'
        ORDER BY brand_name_en ASC
    """), {"company_id": company_id}).mappings().all()
    channels = db.execute(text("""
        SELECT stream_code AS channel_code, stream_name AS channel_name
        FROM revenue_streams
        WHERE company_id = :company_id AND UPPER(TRIM(COALESCE(status,''))) = 'ACTIVE'
        ORDER BY stream_name ASC
    """), {"company_id": company_id}).mappings().all()
    kitchens = db.execute(text("""
        SELECT kitchen_code, kitchen_name
        FROM kitchen_locations
        WHERE company_id = :company_id AND UPPER(TRIM(COALESCE(status,''))) = 'ACTIVE'
        ORDER BY kitchen_name ASC
    """), {"company_id": company_id}).mappings().all()
    recipes = db.execute(text("""
        SELECT r.recipe_code, r.recipe_name, COALESCE(r.customer_name,'') AS customer_name,
               COALESCE(r.brand_name,'') AS brand_name, COALESCE(r.category,'') AS category,
               r.food_cost_per_portion, r.sale_price_per_portion, COUNT(ri.id) AS bom_lines
        FROM recipes r
        LEFT JOIN recipe_ingredients ri ON ri.recipe_id = r.id
        WHERE r.company_id = :company_id
          AND UPPER(TRIM(COALESCE(r.status,''))) = 'ACTIVE'
          AND COALESCE(r.is_active,1) = 1
        GROUP BY r.id
        ORDER BY r.recipe_code ASC, r.recipe_name ASC
    """), {"company_id": company_id}).mappings().all()
    return {"customers": customers, "brands": brands, "channels": channels, "kitchens": kitchens, "recipes": recipes}


@router.get("/orders")
async def orders_page(request: Request, db: Session = Depends(get_db)):
    # Batch 91: Production Orders and Head Chef Planning showed the same
    # underlying list of orders — consolidated into one screen as asked.
    # This route now redirects rather than rendering its own page, so the
    # ~10 existing internal links that still point here (Back to Orders
    # buttons, breadcrumbs, etc.) land on the right place automatically
    # instead of needing every one of them individually updated.
    qs = str(request.url.query)
    target = "/production/head-chef" + (f"?{qs}" if qs else "")
    return RedirectResponse(target, status_code=307)


@router.post("/orders/create")
async def create_order_form(
    request: Request,
    customer_no: str = Form(""),
    customer_name: str = Form(""),
    customer_display: str = Form(""),
    brand: str = Form(""),
    brand_display: str = Form(""),
    channel: str = Form(""),
    channel_display: str = Form(""),
    kitchen: str = Form(""),
    required_delivery_date: Optional[str] = Form(None),
    required_delivery_time: Optional[str] = Form(None),
    cooking_date: Optional[str] = Form(None),
    cooking_time: Optional[str] = Form(None),
    material_receiving_date: Optional[str] = Form(None),
    material_receiving_time: Optional[str] = Form(None),
    recipe_no: List[str] = Form([]),
    recipe_name: List[str] = Form([]),
    recipe_display: List[str] = Form([]),
    required_portions: List[float] = Form([]),
    immediate: str = Form(""),
    db: Session = Depends(get_db),
):
    _ensure_sales_review_schema(db)
    customer_name = (customer_name or customer_display or "").strip()
    brand = (brand or brand_display or "").strip()
    channel = (channel or channel_display or "").strip()
    kitchen = (kitchen or "").strip()

    lines = []

    for idx, rcp in enumerate(recipe_no):
        if not rcp:
            continue

        portions = required_portions[idx] if idx < len(required_portions) else 0
        if portions <= 0:
            continue

        recipe = (
            db.query(Recipe)
            .filter(
                Recipe.recipe_code == rcp,
                func.upper(func.trim(Recipe.status)) == "ACTIVE",
                Recipe.is_active == True,
            )
            .order_by(Recipe.version.desc(), Recipe.id.desc())
            .first()
        )

        lines.append(
            OrderLineIn(
                recipe_no=rcp,
                recipe_name=(recipe.recipe_name if recipe else (recipe_name[idx] if idx < len(recipe_name) and recipe_name[idx] else rcp)),
                required_portions=portions,
                selling_price_per_portion=float(recipe.sale_price_per_portion or 0) if recipe else 0,
            )
        )

    if not lines:
        # Batch 91 fix: this always redirected to /production/orders
        # regardless of which form the user actually submitted from —
        # someone filling out the Sale Requisition form (/orders/portal)
        # got bounced to a completely different screen on a validation
        # error. Now returns to wherever the request actually came from,
        # with a safe fallback if that can't be determined.
        origin = request.headers.get("referer") or "/orders/portal"
        return redirect_with_error(origin, "Please enter at least one recipe with portions greater than zero.")

    # Batch 95: 48-hour rule — delivery must be at least 48 hours from now.
    # Enforced server-side so it can't be bypassed by editing the form.
    # Users explicitly granted the "immediate_order" area (via Users &
    # Access — nobody has it by default) skip this check entirely, for
    # genuinely urgent orders. Not a silent bypass: notes records who
    # placed it and that the rule was skipped, same principle as every
    # other deviation-needs-a-reason pattern elsewhere in this system.
    is_immediate = immediate == "1" and can_access(request, "immediate_order")
    _deliv = _parse_date(required_delivery_date)
    if _deliv:
        try:
            _t = (required_delivery_time or "00:00")[:5]
            _hh, _mm = (int(x) for x in _t.split(":")[:2])
        except Exception:
            _hh, _mm = 0, 0
        _deliv_dt = datetime(_deliv.year, _deliv.month, _deliv.day, _hh, _mm)
        if not is_immediate and _deliv_dt < datetime.now() + timedelta(hours=48):
            return redirect_with_error(
                "/orders/portal",
                "Orders must be placed at least 48 hours before the delivery date and time.")
    else:
        return redirect_with_error("/orders/portal",
                                   "A delivery date is required.")

    payload = CustomerOrderCreate(
        customer_no=customer_no or None,
        customer_name=customer_name,
        brand=brand or None,
        channel=channel or None,
        kitchen=kitchen or None,
        required_delivery_date=_parse_date(required_delivery_date),
        required_delivery_time=required_delivery_time or None,
        cooking_date=_parse_date(cooking_date),
        cooking_time=cooking_time or None,
        material_receiving_date=_parse_date(material_receiving_date),
        material_receiving_time=material_receiving_time or None,
        notes=None,
        lines=lines,
    )

    try:
        # Batch 94 FIX: company_id was never passed here, so every order raised
        # from the Sale Requisition / Immediate Order form landed with
        # company_id = NULL — the single highest-volume order entry point in
        # the system was writing unscoped rows. Subscriptions and the old
        # Requisitions module always passed it; these two paths never did.
        order = create_order(db, payload, created_by=current_user_name(request),
                             company_id=_company_id_from_session(request))
    except ValueError as exc:
        return redirect_with_error("/production/orders", str(exc))

    return RedirectResponse(
    f"/sales-requests?toast=success&title=Request Submitted",
    status_code=HTTP_303_SEE_OTHER
)


@router.get("/orders/{order_no}")
async def order_detail(request: Request, order_no: str, db: Session = Depends(get_db)):
    require_area(request, "production_orders")
    _ensure_sales_review_schema(db)
    order = scoped_order(db, request, order_no)
    if not order:
        raise HTTPException(404, "Order not found")

    lines = db.query(OrderLine).filter(OrderLine.order_no == order_no).all()
    bom = db.query(BOMLine).filter(BOMLine.order_no == order_no).all()
    cons = consolidated_bom(db, order_no=order_no)
    issuance = db.query(StoreIssuanceLine).filter(StoreIssuanceLine.order_no == order_no).all()
    txs = db.query(KitchenSectionTransaction).filter(KitchenSectionTransaction.order_no == order_no).all()
    recipe_meta = {}
    if lines:
        recipe_codes = [l.recipe_no for l in lines]
        recipes = db.query(Recipe).filter(Recipe.recipe_code.in_(recipe_codes), func.upper(func.trim(Recipe.status)) == "ACTIVE", Recipe.is_active == True).all()
        recipe_meta = {r.recipe_code: r for r in recipes}

    # Batch 80: stock shortage preview — only worth computing before the BOM
    # is generated (once BOM/store issuance exist, those are the authoritative
    # numbers). Advisory only; does not block approval.
    shortages = preview_bom_shortages(db, order_no) if not bom else []

    return render(
        request,
        "production/order_detail.html",
        {
            "order": order,
            "lines": lines,
            "bom": bom,
            "consolidated": cons,
            "issuance": issuance,
            "txs": txs,
            "recipe_meta": recipe_meta,
            "shortages": shortages,
            # ==========================================================
            # Batch 119 — THE BOM BUTTON BUG.
            #
            # order_detail.html gates the whole "BOM Generation & Store
            # Release" block on `is_approved`, and gates the head-chef
            # schedule form on `sales_pending` / `sales_rejected`. None of
            # the three were ever passed into the template.
            #
            # In Jinja an undefined name is FALSY, not an error. So
            # `{% if not is_approved %}` was permanently true: the schedule
            # form showed forever and the BOM / Release-to-Store block could
            # never render — no matter how many times the Head Chef approved.
            # That is why approving the cooking schedule appeared to do
            # nothing.
            #
            # This is the failure mode that makes undefined-name-is-falsy
            # dangerous: no error, no log line, just a button that never
            # appears.
            # ==========================================================
            "is_approved": (order.status or "") in (
                "Head Chef Approved", "BOM Generated", "Store Pending",
                "In Production", "QC In Progress", "Packing",
                "Out for Delivery", "Delivered",
            ),
            "sales_pending": (order.sales_review_status or "") == "Pending",
            "sales_rejected": (order.sales_review_status or "") == "Rejected",
            # Batch 97: pass the already-scoped order rather than making the
            # helper look it up again.
            "flow_status": _order_flow_status(db, order_no, order=order),
            "page_title": order_no,
            "error": request.query_params.get("error"),
        },
    )


@router.post("/orders/{order_no}/generate-bom")
async def generate_bom(request: Request, order_no: str, db: Session = Depends(get_db)):
    require_action(request, "bom", "add")
    try:
        order = scoped_order(db, request, order_no)
        if order and order.status not in {"Head Chef Approved", "BOM Generated", "Store Pending", "Store Issued", "In Production"}:
            raise ValueError("Head Chef approval is required before BOM generation")
        generate_bom_for_order(db, order_no)
    except ValueError as exc:
        return redirect_with_error(f"/production/orders/{order_no}", str(exc))
    return RedirectResponse(f"/production/orders/{order_no}", status_code=HTTP_303_SEE_OTHER)


@router.post("/orders/{order_no}/approve-plan")
async def approve_plan(
    request: Request,
    order_no: str,
    cooking_date: Optional[str] = Form(None),
    cooking_time: Optional[str] = Form(None),
    material_receiving_date: Optional[str] = Form(None),
    material_receiving_time: Optional[str] = Form(None),
    db: Session = Depends(get_db),
):
    """Head Chef scheduling and approval.

    Customer/internal portal captures the requested delivery date/time. Head Chef
    decides cooking date/time and material receiving date/time before BOM.
    """
    require_action(request, "head_chef", "edit")
    try:
        order = scoped_order(db, request, order_no)
        if not order:
            raise ValueError("Order not found")

        order.cooking_date = _parse_date(cooking_date)
        order.cooking_time = cooking_time or None
        order.material_receiving_date = _parse_date(material_receiving_date)
        order.material_receiving_time = material_receiving_time or None

        if not order.cooking_date:
            raise ValueError("Head Chef must enter cooking date")
        if not order.material_receiving_date:
            raise ValueError("Head Chef must enter material receiving date")

        approve_order_before_bom(db, order_no, approved_by=current_user_name(request))
    except ValueError as exc:
        db.rollback()
        return redirect_with_error(f"/production/orders/{order_no}", str(exc))
    return RedirectResponse(f"/production/orders/{order_no}", status_code=HTTP_303_SEE_OTHER)


@router.post("/orders/{order_no}/release-store")
async def release_to_store(request: Request, order_no: str, db: Session = Depends(get_db)):
    require_action(request, "store_issuance", "add")
    try:
        approve_head_chef_plan(db, order_no, approved_by=current_user_name(request))
    except ValueError as exc:
        return redirect_with_error(f"/production/orders/{order_no}", str(exc))
    return RedirectResponse(f"/production/orders/{order_no}/store-issuance", status_code=HTTP_303_SEE_OTHER)


@router.get("/orders/{order_no}/bom-report")
async def bom_report(request: Request, order_no: str, group_by: str = "item", export: str | None = None, db: Session = Depends(get_db)):
    """Professional BOM view/export screen.

    Client requested all BOM view options to be active. This route now shows an
    attractive table UI by default, while still allowing JSON verification using
    ?export=json.
    """
    valid_groups = {"item", "customer", "brand", "category", "sub_category", "section"}
    if group_by not in valid_groups:
        group_by = "item"

    order = scoped_order(db, request, order_no)
    if not order:
        raise HTTPException(404, "Order not found")

    group_sql = {
        "item": "CONCAT(bl.ingredient_code, ' - ', bl.ingredient_name)",
        "customer": "COALESCE(co.customer_name, 'Unassigned Customer')",
        "brand": "COALESCE(co.brand, 'Unassigned Brand')",
        "category": "COALESCE(NULLIF(bl.ingredient_main_category,''), NULLIF(bl.ingredient_category,''), 'Unassigned Main Category')",
        "sub_category": "COALESCE(NULLIF(bl.ingredient_sub_category,''), 'Unassigned Sub Category')",
        "section": "COALESCE(NULLIF(bl.default_issue_section,''), 'Unassigned Section')",
    }[group_by]

    rows = db.execute(text(f"""
        SELECT
            {group_sql} AS group_value,
            bl.ingredient_code,
            bl.ingredient_name,
            COALESCE(NULLIF(bl.ingredient_main_category,''), NULLIF(bl.ingredient_category,''), '') AS main_category,
            COALESCE(NULLIF(bl.ingredient_sub_category,''), '') AS sub_category,
            COALESCE(NULLIF(bl.default_issue_section,''), '') AS issue_section,
            bl.standard_uom,
            SUM(bl.total_required_with_waste_standard) AS required_qty,
            SUM(bl.estimated_cost) AS estimated_cost
        FROM bom_lines bl
        LEFT JOIN customer_orders co ON co.order_no = bl.order_no
        WHERE bl.order_no = :order_no
        GROUP BY group_value, bl.ingredient_code, bl.ingredient_name, bl.ingredient_main_category, bl.ingredient_category, bl.ingredient_sub_category, bl.default_issue_section, bl.standard_uom
        ORDER BY group_value, bl.ingredient_name
    """), {"order_no": order_no}).mappings().all()

    report_rows = [dict(r) for r in rows]
    total_qty = sum(float(r.get("required_qty") or 0) for r in report_rows)
    total_cost = sum(float(r.get("estimated_cost") or 0) for r in report_rows)
    group_count = len({r.get("group_value") for r in report_rows})

    if export == "json":
        return {"order_no": order_no, "group_by": group_by, "rows": report_rows}
    if export == "csv":
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["Group", "Item Code", "Item Name", "Main Category", "Sub Category", "Issue Section", "Required Qty", "UOM", "Estimated Cost"])
        for r in report_rows:
            writer.writerow([
                r.get("group_value", ""), r.get("ingredient_code", ""), r.get("ingredient_name", ""),
                r.get("main_category", ""), r.get("sub_category", ""), r.get("issue_section", ""),
                r.get("required_qty", 0), r.get("standard_uom", ""), r.get("estimated_cost", 0),
            ])
        output.seek(0)
        return StreamingResponse(
            iter(['\ufeff' + output.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename={order_no}_{group_by}_bom.csv"},
        )

    labels = {
        "item": "Item-wise BOM",
        "customer": "Customer-wise BOM",
        "brand": "Brand-wise BOM",
        "category": "Main Category-wise BOM",
        "sub_category": "Sub Category-wise BOM",
        "section": "Section-wise BOM",
    }

    return render(
        request,
        "production/bom_report.html",
        {
            "order": order,
            "order_no": order_no,
            "group_by": group_by,
            "group_label": labels[group_by],
            "rows": report_rows,
            "total_qty": total_qty,
            "total_cost": total_cost,
            "group_count": group_count,
            "page_title": labels[group_by],
        },
    )


@router.get("/orders/{order_no}/store-issuance")
async def store_issuance_page(request: Request, order_no: str, db: Session = Depends(get_db)):
    require_area(request, "store_issuance")
    order = scoped_order(db, request, order_no)
    lines = (
        db.query(StoreIssuanceLine)
        .filter(StoreIssuanceLine.order_no == order_no)
        .order_by(StoreIssuanceLine.recipe_name)
        .all()
    )
    return render(
        request,
        "production/store_issuance.html",
        {
            "order": order,
            "lines": lines,
            "page_title": "Store Issuance",
            "error": request.query_params.get("error"),
        },
    )


@router.post("/store-issuance/{line_id}/update")
async def update_store_issue(
    request: Request,
    line_id: int,
    input_material_issued: float = Form(...),
    issued_uom: str = Form(...),
    issue_to_section: str = Form(...),
    lot_no: str = Form(""),
    supplier_name: str = Form(""),
    store_remarks: str = Form(""),
    db: Session = Depends(get_db),
):
    require_action(request, "store_issuance", "edit")

    # ---- Re-issue AUDIT TRAIL: snapshot the line BEFORE the change ----
    before = db.query(StoreIssuanceLine).filter(StoreIssuanceLine.id == line_id).first()

    # Batch 122: the store keeper may correct an issued quantity via Re-Issue at
    # any time (the upsert path prevents duplicate rows). The Batch 121 store
    # view-only gate is intentionally NOT applied here; kitchen/QC/packing steps
    # still lock downstream.
    old_qty = float(getattr(before, "input_material_issued", 0) or 0) if before else 0
    old_uom = getattr(before, "issued_uom", "") if before else ""
    old_section = getattr(before, "issue_to_section", "") if before else ""

    try:
        line = update_store_issuance_line(
            db,
            line_id,
            input_material_issued,
            issued_uom,
            issue_to_section,
            lot_no,
            supplier_name,
            store_remarks,
        )
    except ValueError as exc:
        return redirect_with_error("/production/orders", str(exc))

    # Write the audit row (never let audit failure break the operation)
    try:
        db.execute(text("""
            INSERT INTO store_issue_audit
                (company_id, order_no, line_id, ingredient_code, ingredient_name,
                 old_qty, new_qty, old_uom, new_uom, old_section, new_section,
                 reason, changed_by, changed_at)
            VALUES
                (:company_id, :order_no, :line_id, :ing_code, :ing_name,
                 :old_qty, :new_qty, :old_uom, :new_uom, :old_section, :new_section,
                 :reason, :changed_by, NOW())
        """), {
            "company_id": getattr(line, "company_id", None) or request.session.get("company_id"),
            "order_no": line.order_no,
            "line_id": line_id,
            "ing_code": getattr(line, "ingredient_code", ""),
            "ing_name": getattr(line, "ingredient_name", ""),
            "old_qty": old_qty,
            "new_qty": float(input_material_issued or 0),
            "old_uom": old_uom, "new_uom": issued_uom,
            "old_section": old_section, "new_section": issue_to_section,
            "reason": store_remarks or "",
            "changed_by": current_user_name(request),
        })
        db.commit()
    except Exception:
        db.rollback()

    return RedirectResponse(
        f"/production/orders/{line.order_no}/store-issuance?toast=success&title=Line Saved&msg={line.ingredient_name}: issued {line.input_material_issued} {line.issued_uom} to {line.issue_to_section}",
        status_code=HTTP_303_SEE_OTHER)


@router.post("/store-issuance/{line_id}/reissue")
async def reissue_store_line(request: Request, line_id: int, db: Session = Depends(get_db)):
    """Batch 19: the Re-Issue/Edit button posted here but the route did not
    exist (404). Un-finalizes ONE line so the store can correct the issued
    quantity, provided the order has not moved past the store stage."""
    require_action(request, "store_issuance", "edit")
    line = db.query(StoreIssuanceLine).filter(StoreIssuanceLine.id == line_id).first()
    if not line:
        raise HTTPException(404, "Store issuance line not found")
    order = db.query(CustomerOrder).filter(CustomerOrder.order_no == line.order_no).first()
    # Batch 122: Re-Issue is always available to the store keeper for quantity
    # corrections; the upsert path keeps a single row per ingredient.
    line.finalized = False
    db.commit()
    # Batch 121: return to the per-ORDER line page (where the store keeper is
    # actually working), not the whole-queue list. Clearer, colored message.
    from urllib.parse import quote as _q
    # _msg = _q(f"{line.ingredient_name} reopened for editing — adjust the issue qty and press Save.")
    return RedirectResponse(
        f"/production/orders/{line.order_no}/store-issuance?toast=warning&title={_q('Line Reopened')}#line-{line_id}",
        status_code=HTTP_303_SEE_OTHER,
    )


@router.post("/orders/{order_no}/finalize-store")
async def finalize_store(request: Request, order_no: str, db: Session = Depends(get_db)):
    require_action(request, "store_issuance", "edit")
    try:
        finalize_store_issuance(db, order_no, issued_by=current_user_name(request))
    except ValueError as exc:
        return redirect_with_error(f"/production/orders/{order_no}/store-issuance", str(exc))

    # Batch 69: auto-post store issuance to the GL — Dr 5100 WIP/COGS / Cr 1130
    # Inventory, valued at issued-qty × ingredient standard cost. This is when
    # inventory value becomes production cost. Idempotent per order; never blocks.
    try:
        from app.core.gl_posting import post_issuance_journal
        val = db.execute(text("""
            SELECT COALESCE(SUM(
                COALESCE(s.issued_qty_standard, s.input_material_issued, 0) *
                COALESCE(i.unit_cost_standard, 0)
            ), 0)
            FROM store_issuance_lines s
            LEFT JOIN ingredients i ON i.ingredient_code = s.ingredient_code
            WHERE s.order_no = :o
        """), {"o": order_no}).scalar() or 0
        post_issuance_journal(db, request, order_no, float(val))
    except Exception:
        pass

    return RedirectResponse(
        f"/production/store-issuance?toast=success&title=Store Issue Finalized&msg=Order {order_no}: selected lines locked and material sent to kitchen sections",
        status_code=HTTP_303_SEE_OTHER)


# Batch 20: Thawing/Marination retired — handled inside Butchery.
CANONICAL_SECTIONS = [
    "Cutting", "Butchery",
    "Hot Kitchen", "Cold Kitchen", "Bakery/Pastry", "Trayline / Packing",
]


def _section_from_slug(section_name: str) -> str:
    """Map a URL slug to the canonical section name, case-insensitively.

    'Hot-Kitchen', 'hot-kitchen', 'HOT KITCHEN' all resolve to 'Hot Kitchen',
    which keeps SQL filters, template comparisons and active nav chips in sync.
    """
    raw = (section_name or "").strip()
    if raw in {"Trayline-Packing", "Trayline---Packing"}:
        return "Trayline / Packing"
    normalized = raw.replace("-", " ").replace("/", " ").lower()
    normalized = " ".join(normalized.split())
    for canonical in CANONICAL_SECTIONS:
        c_norm = " ".join(canonical.replace("/", " ").lower().split())
        if normalized == c_norm:
            return canonical
    return raw.replace("-", " ")


def _section_slug(section: str) -> str:
    if section == "Bakery/Pastry":
        return "Bakery-Pastry"
    if section == "Trayline / Packing":
        return "Trayline-Packing"
    return section.replace(" ", "-")


@router.get("/section/{section_name}")
async def section_page(request: Request, section_name: str, db: Session = Depends(get_db)):
    require_area(request, "kitchen")
    """Section workstation landing page.

    Instead of showing hundreds of separate ingredient cards, this screen now works
    like Store Issuance: one row per production order, with an Open Receiving
    button. Users can understand the workload first, then drill into one order.
    """
    section = _section_from_slug(section_name)

    search = (request.query_params.get("search") or "").strip()
    status_filter = (request.query_params.get("status") or "").strip()
    params = {"section": section, "search": f"%{search}%"}
    extra_where = ""
    if search:
        extra_where += " AND (k.order_no LIKE :search OR COALESCE(co.customer_name,'') LIKE :search OR COALESCE(co.brand,'') LIKE :search OR COALESCE(k.recipe_name,'') LIKE :search)"
    if status_filter == "pending_receive":
        extra_where += " AND COALESCE(k.received_qty_standard,0) <= 0"
    elif status_filter == "received":
        extra_where += " AND COALESCE(k.received_qty_standard,0) > 0"
    elif status_filter == "completed":
        extra_where += " AND (UPPER(COALESCE(k.transaction_status,'')) LIKE 'COMPLETED%' OR UPPER(COALESCE(k.transaction_status,'')) = 'TRANSFERRED')"

    order_rows = db.execute(text(f"""
        SELECT
            k.order_no,
            COALESCE(MAX(co.customer_name), '') AS customer_name,
            COALESCE(MAX(co.brand), '') AS brand,
            COALESCE(MAX(co.required_delivery_date), '') AS delivery_date,
            COALESCE(MAX(co.required_delivery_time), '') AS delivery_time,
            COUNT(*) AS total_lines,
            SUM(CASE WHEN COALESCE(k.received_qty_standard,0) > 0 THEN 1 ELSE 0 END) AS received_lines,
            SUM(CASE WHEN UPPER(COALESCE(k.transaction_status,'')) LIKE 'COMPLETED%' OR UPPER(COALESCE(k.transaction_status,'')) = 'TRANSFERRED' THEN 1 ELSE 0 END) AS completed_lines,
            ROUND(SUM(COALESCE(k.issued_qty_standard,0)), 4) AS issued_qty,
            ROUND(SUM(COALESCE(k.received_qty_standard,0)), 4) AS received_qty,
            ROUND(SUM(COALESCE(k.balance_qty_standard,0)), 4) AS balance_qty,
            MAX(k.updated_at) AS last_activity
        FROM kitchen_section_transactions k
        LEFT JOIN customer_orders co ON co.order_no = k.order_no
        WHERE k.current_section = :section
        {extra_where}
        GROUP BY k.order_no
        ORDER BY MAX(k.updated_at) DESC, k.order_no DESC
    """), params).mappings().all()

    if request.query_params.get("export") == "csv":
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["Order", "Customer", "Brand", "Delivery Date", "Delivery Time", "Total Lines", "Received Lines", "Completed Lines", "Issued Qty", "Received Qty", "Balance Qty", "Last Activity"])
        for r in order_rows:
            writer.writerow([
                r.get("order_no", ""), r.get("customer_name", ""), r.get("brand", ""),
                r.get("delivery_date", ""), r.get("delivery_time", ""), r.get("total_lines", 0),
                r.get("received_lines", 0), r.get("completed_lines", 0), r.get("issued_qty", 0),
                r.get("received_qty", 0), r.get("balance_qty", 0), r.get("last_activity", ""),
            ])
        output.seek(0)
        return StreamingResponse(iter(['\ufeff' + output.getvalue()]), media_type="text/csv", headers={"Content-Disposition": f"attachment; filename={_section_slug(section)}_orders.csv"})

    total_orders = len(order_rows)
    total_lines = sum(int(r.get("total_lines") or 0) for r in order_rows)
    received_lines = sum(int(r.get("received_lines") or 0) for r in order_rows)
    completed_lines = sum(int(r.get("completed_lines") or 0) for r in order_rows)

    return render(
        request,
        "production/section.html",
        {
            "section": section,
            "section_slug": _section_slug(section),
            "orders": order_rows,
            "total_orders": total_orders,
            "total_lines": total_lines,
            "received_lines": received_lines,
            "completed_lines": completed_lines,
            "filters": {"search": search, "status": status_filter},
            "page_title": f"{section} Workstation",
            "error": request.query_params.get("error"),
        },
    )


@router.get("/section/{section_name}/orders/{order_no}")
async def section_order_page(request: Request, section_name: str, order_no: str, db: Session = Depends(get_db)):
    require_area(request, "kitchen")
    section = _section_from_slug(section_name)
    order = scoped_order(db, request, order_no)
    txs = (
        db.query(KitchenSectionTransaction)
        .filter(KitchenSectionTransaction.current_section == section, KitchenSectionTransaction.order_no == order_no)
        .order_by(KitchenSectionTransaction.recipe_name, KitchenSectionTransaction.ingredient_name)
        .all()
    )
    if not txs:
        return redirect_with_error(f"/production/section/{_section_slug(section)}", "No section receiving lines found for this order.")

    recipe_groups = []
    if section == "Bakery/Pastry":
        recipe_groups = bakery_pastry_consolidated(db, order_no=order_no)

    totals = {
        "lines": len(txs),
        "issued": sum(float(t.issued_qty_standard or 0) for t in txs),
        "received": sum(float(t.received_qty_standard or 0) for t in txs),
        "balance": sum(float(t.balance_qty_standard or 0) for t in txs),
        "completed": sum(1 for t in txs if str(t.transaction_status or '').upper().startswith('COMPLETED') or str(t.transaction_status or '').upper() == 'TRANSFERRED'),
    }

    if request.query_params.get("export") == "csv":
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["Order", "Section", "Recipe Code", "Recipe Name", "Ingredient Code", "Ingredient Name", "Issued Qty", "Received Qty", "Processed Qty", "Waste Qty", "Return Qty", "Transfer Qty", "Balance Qty", "UOM", "Status"])
        for tx in txs:
            writer.writerow([tx.order_no, section, tx.recipe_no, tx.recipe_name, tx.ingredient_code, tx.ingredient_name, tx.issued_qty_standard, tx.received_qty_standard, tx.processed_qty_standard, tx.waste_qty_standard, tx.returned_qty_standard, tx.transferred_qty_standard, tx.balance_qty_standard, tx.standard_uom, tx.transaction_status])
        output.seek(0)
        return StreamingResponse(iter(['\ufeff' + output.getvalue()]), media_type="text/csv", headers={"Content-Disposition": f"attachment; filename={order_no}_{_section_slug(section)}_receiving.csv"})

    next_sections = ["Cutting", "Butchery", "Hot Kitchen", "Cold Kitchen", "Bakery/Pastry", "QC", "Trayline / Packing", "Dispatch"]  # Batch 19: Thawing/Marination retired; Packing unified into Trayline / Packing

    return render(
        request,
        "production/section_order.html",
        {
            "section": section,
            "section_slug": _section_slug(section),
            "order": order,
            "order_no": order_no,
            "txs": txs,
            "recipe_groups": recipe_groups,
            "totals": totals,
            "next_sections": next_sections,
            "page_title": f"{section} Receiving - {order_no}",
            "error": request.query_params.get("error"),
        },
    )


@router.post("/section/{section_name}/orders/{order_no}/receive-all")
async def receive_section_order_all(request: Request, section_name: str, order_no: str, db: Session = Depends(get_db)):
    require_action(request, "kitchen", "edit")
    section = _section_from_slug(section_name)
    # Batch 121: STEP-LOCK — kitchen is view-only once the order is past it.
    _ord = db.query(CustomerOrder).filter(CustomerOrder.order_no == order_no).first()
    _status = getattr(_ord, "status", "") if _ord else ""
    from app.core.stage_lock import is_stage_locked, lock_reason
    if is_stage_locked(_status, "kitchen"):
        from urllib.parse import quote as _q
        return RedirectResponse(
            f"/production/section/{_section_slug(section)}/orders/{order_no}?toast=warning&title={_q('Step Locked')}&msg={_q(lock_reason(_status, 'kitchen'))}",
            status_code=HTTP_303_SEE_OTHER)
    txs = db.query(KitchenSectionTransaction).filter(
        KitchenSectionTransaction.current_section == section,
        KitchenSectionTransaction.order_no == order_no,
    ).all()
    if not txs:
        return redirect_with_error(f"/production/section/{_section_slug(section)}", "No lines found to receive.")
    user = current_user_name(request)
    received_n, skipped_n = 0, 0
    for tx in txs:
        if float(tx.received_qty_standard or 0) <= 0 and not str(tx.transaction_status or '').upper().startswith('COMPLETED'):
            qty = float(tx.issued_qty_standard or tx.balance_qty_standard or 0)
            tx.received_qty_standard = qty
            tx.balance_qty_standard = qty
            tx.received_by = user
            tx.received_at = tx.received_at or datetime.utcnow()
            tx.transaction_status = "Received"
            received_n += 1
        else:
            skipped_n += 1
    db.commit()
    # Batch 19: real feedback instead of a silent redirect.
    if received_n:
        msg = f"{received_n} line(s) received at full issued quantity" + (f", {skipped_n} already received/completed" if skipped_n else "")
        return RedirectResponse(f"/production/section/{_section_slug(section)}/orders/{order_no}?toast=success&title=Received&msg={msg}", status_code=HTTP_303_SEE_OTHER)
    return RedirectResponse(f"/production/section/{_section_slug(section)}/orders/{order_no}?toast=info&title=Nothing to receive&msg=All {skipped_n} line(s) were already received or completed", status_code=HTTP_303_SEE_OTHER)


@router.post("/tx/{tx_id}/receive")
async def receive_tx(
    request: Request,
    tx_id: int,
    received_qty_standard: float = Form(...),
    db: Session = Depends(get_db),
):
    require_action(request, "kitchen", "edit")
    try:
        tx = receive_transaction(db, tx_id, received_qty_standard, current_user_name(request))
    except ValueError as exc:
        return redirect_with_error("/production/orders", str(exc))

    return RedirectResponse(request.headers.get("referer") or f"/production/section/{tx.current_section.replace('/', '-')}", status_code=HTTP_303_SEE_OTHER)


@router.post("/section/{section_name}/orders/{order_no}/bulk-transfer")
async def bulk_transfer_section_order(request: Request, section_name: str, order_no: str, db: Session = Depends(get_db)):
    """UI fix: chefs can tick multiple already-Received lines and process +
    transfer them together in one action, instead of one form per line.
    Each selected line passes through at full received quantity with zero
    waste/return (the common case); anyone who needs to record waste or a
    partial transfer still uses the per-line 'Process & Transfer' panel."""
    require_action(request, "kitchen", "edit")
    section = _section_from_slug(section_name)
    form = await request.form()
    tx_ids = []
    for raw in form.getlist("tx_ids"):
        try:
            tx_ids.append(int(raw))
        except (TypeError, ValueError):
            continue
    next_section = (form.get("bulk_next_section") or "").strip()

    if not tx_ids:
        return redirect_with_error(
            f"/production/section/{_section_slug(section)}/orders/{order_no}",
            "Select at least one received line to process.")

    # Batch 121: STEP-LOCK. Once the order has moved past Kitchen (into QC/
    # Packing/Dispatch), kitchen sections are view-only.
    from app.core.stage_lock import is_stage_locked, lock_reason
    _ord = db.query(CustomerOrder).filter(CustomerOrder.order_no == order_no).first()
    _status = getattr(_ord, "status", "") if _ord else ""
    if is_stage_locked(_status, "kitchen"):
        from urllib.parse import quote as _q
        return RedirectResponse(
            f"/production/section/{_section_slug(section)}/orders/{order_no}?toast=warning&title={_q('Step Locked')}&msg={_q(lock_reason(_status, 'kitchen'))}",
            status_code=HTTP_303_SEE_OTHER)

    user = current_user_name(request)
    ok, failed = 0, 0
    for tx_id in tx_ids:
        tx = db.query(KitchenSectionTransaction).filter(KitchenSectionTransaction.id == tx_id).first()
        if not tx:
            failed += 1
            continue
        received_qty = float(tx.received_qty_standard or tx.issued_qty_standard or 0)
        try:
            transfer_transaction(
                db, tx_id, received_qty, 0, 0, received_qty, user,
                None, "Bulk processed & transferred", next_section or None,
            )
            ok += 1
        except ValueError:
            failed += 1

    # Batch 121: the old message never said WHERE the lines went, which read as
    # "wrong" when the chef had picked a specific destination. Name it clearly.
    dest = next_section if next_section else "each line's own next section"
    if ok:
        msg = f"{ok} line(s) processed and transferred to {dest}."
    else:
        msg = "No lines were transferred."
    if failed:
        msg += f" {failed} skipped (already locked or not yet received)."
    kind = "success" if ok else "warning"
    from urllib.parse import quote as _q
    return RedirectResponse(
        f"/production/section/{_section_slug(section)}/orders/{order_no}?toast={kind}&title={_q('Process & Transfer')}&msg={_q(msg)}",
        status_code=HTTP_303_SEE_OTHER)


@router.post("/tx/{tx_id}/transfer")
async def transfer_tx(
    request: Request,
    tx_id: int,
    processed_qty_standard: float = Form(0),
    waste_qty_standard: float = Form(0),
    returned_qty_standard: float = Form(0),
    transferred_qty_standard: float = Form(0),
    waste_reason: str = Form(""),
    section_remarks: str = Form(""),
    next_section: str = Form(""),
    db: Session = Depends(get_db),
):
    require_action(request, "kitchen", "edit")
    tx = db.query(KitchenSectionTransaction).filter(KitchenSectionTransaction.id == tx_id).first()
    fallback_section = tx.current_section.replace("/", "-") if tx else "Hot-Kitchen"

    # Batch 121: STEP-LOCK — kitchen is view-only once the order is past it.
    if tx is not None:
        from app.core.stage_lock import is_stage_locked, lock_reason
        _ord = db.query(CustomerOrder).filter(CustomerOrder.order_no == tx.order_no).first()
        _status = getattr(_ord, "status", "") if _ord else ""
        if is_stage_locked(_status, "kitchen"):
            return redirect_with_error(
                f"/production/section/{fallback_section}/orders/{tx.order_no}",
                lock_reason(_status, "kitchen"))

    # Batch 94: server-side twin of the client-side check — editing
    # Transfer away from the full amount needs a reason on record, and
    # that has to be enforced here too, not just in JS someone could
    # bypass by submitting the form directly.
    if tx is not None:
        full_amount = float(tx.balance_qty_standard or tx.received_qty_standard or tx.issued_qty_standard or 0)
        if abs(float(transferred_qty_standard) - full_amount) > 0.0001 and not waste_reason.strip():
            return redirect_with_error(f"/production/section/{fallback_section}",
                                        "Transfer quantity was changed from the full amount — a reason is required.")

    try:
        tx = transfer_transaction(
            db,
            tx_id,
            processed_qty_standard,
            waste_qty_standard,
            returned_qty_standard,
            transferred_qty_standard,
            current_user_name(request),
            waste_reason,
            section_remarks,
            next_section or None,
        )
    except ValueError as exc:
        return redirect_with_error(f"/production/section/{fallback_section}", str(exc))

    return RedirectResponse(request.headers.get("referer") or f"/production/section/{tx.current_section.replace('/', '-')}", status_code=HTTP_303_SEE_OTHER)


@router.get("/bakery-pastry")
async def bakery_page(request: Request, db: Session = Depends(get_db)):
    require_area(request, "kitchen")
    section_filter = (request.query_params.get("section") or "").strip()
    date_from = _parse_date(request.query_params.get("date_from"))
    date_to = _parse_date(request.query_params.get("date_to"))
    search = (request.query_params.get("search") or "").strip()
    params = {"search": f"%{search}%"}
    where = "WHERE 1=1"
    if section_filter:
        where += " AND k.current_section = :section"
        params["section"] = _section_from_slug(section_filter)
    if date_from:
        where += " AND COALESCE(co.cooking_date, co.required_delivery_date, co.order_date) >= :date_from"
        params["date_from"] = date_from
    if date_to:
        where += " AND COALESCE(co.cooking_date, co.required_delivery_date, co.order_date) <= :date_to"
        params["date_to"] = date_to
    if search:
        where += " AND (k.order_no LIKE :search OR COALESCE(k.recipe_name,'') LIKE :search OR COALESCE(k.ingredient_name,'') LIKE :search OR COALESCE(co.customer_name,'') LIKE :search)"
    rows = db.execute(text(f"""
        SELECT k.current_section, k.order_no, COALESCE(MAX(co.customer_name),'') AS customer_name,
               COALESCE(MAX(co.brand),'') AS brand, COALESCE(MAX(co.cooking_date), MAX(co.required_delivery_date)) AS plan_date,
               COALESCE(k.recipe_no,'') AS recipe_no, COALESCE(k.recipe_name,'') AS recipe_name,
               COUNT(*) AS ingredient_lines, ROUND(SUM(COALESCE(k.issued_qty_standard,0)),4) AS issued_qty,
               ROUND(SUM(COALESCE(k.received_qty_standard,0)),4) AS received_qty,
               ROUND(SUM(COALESCE(k.processed_qty_standard,0)),4) AS processed_qty,
               ROUND(SUM(COALESCE(k.waste_qty_standard,0)),4) AS waste_qty,
               MAX(k.transaction_status) AS status, MAX(k.updated_at) AS last_activity
        FROM kitchen_section_transactions k
        LEFT JOIN customer_orders co ON co.order_no = k.order_no
        {where}
        GROUP BY k.current_section, k.order_no, k.recipe_no, k.recipe_name
        ORDER BY plan_date DESC, k.current_section, k.order_no DESC
        LIMIT 500
    """), params).mappings().all()
    sections = ["Cutting","Butchery","Hot Kitchen","Cold Kitchen","Bakery/Pastry"]
    totals = {
        "orders": len({r.order_no for r in rows}),
        "recipes": len(rows),
        "issued": sum(float(r.issued_qty or 0) for r in rows),
        "processed": sum(float(r.processed_qty or 0) for r in rows),
        "waste": sum(float(r.waste_qty or 0) for r in rows),
    }
    if request.query_params.get("export") == "csv":
        output = io.StringIO(); writer = csv.writer(output)
        writer.writerow(["Section","Order","Customer","Brand","Plan Date","Recipe","Ingredient Lines","Issued","Received","Processed","Waste","Status","Last Activity"])
        for r in rows:
            writer.writerow([r.current_section, r.order_no, r.customer_name, r.brand, r.plan_date, f"{r.recipe_no} - {r.recipe_name}", r.ingredient_lines, r.issued_qty, r.received_qty, r.processed_qty, r.waste_qty, r.status, r.last_activity])
        output.seek(0)
        return StreamingResponse(iter(['\ufeff' + output.getvalue()]), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=section_summary.csv"})
    return render(request, "production/bakery_pastry.html", {"rows": rows, "sections": sections, "totals": totals, "filters": {"section": section_filter, "date_from": date_from, "date_to": date_to, "search": search}, "page_title": "All Section Summary", "error": request.query_params.get("error")})

@router.post("/bakery-pastry/process")
async def process_bakery_recipe(
    request: Request,
    order_no: str = Form(...),
    recipe_no: str = Form(...),
    output_qty: float = Form(...),
    output_uom: str = Form("Portions"),
    waste_qty: float = Form(0),
    next_section: str = Form("Trayline / Packing"),
    remarks: str = Form(""),
    db: Session = Depends(get_db),
):
    require_action(request, "kitchen", "edit")
    try:
        process_bakery_pastry_recipe(
            db,
            order_no=order_no,
            recipe_no=recipe_no,
            output_qty=output_qty,
            output_uom=output_uom,
            waste_qty=waste_qty,
            next_section=next_section,
            user=current_user_name(request),
            remarks=remarks,
        )
    except ValueError as exc:
        return redirect_with_error("/production/bakery-pastry", str(exc))
    return RedirectResponse("/production/bakery-pastry", status_code=HTTP_303_SEE_OTHER)


@router.get("/head-chef")
async def head_chef_dashboard(request: Request, db: Session = Depends(get_db)):
    _ensure_sales_review_schema(db)
    require_area(request, "head_chef")
    q, filters = _filtered_orders_query(db, request, date_column="delivery")
    # Batch 17: planning order = SOONEST delivery first (was newest-first),
    # NULL delivery dates sink to the bottom so dated work is always on top.
    orders = (q.order_by(CustomerOrder.required_delivery_date.is_(None),
                         CustomerOrder.required_delivery_date.asc(),
                         CustomerOrder.required_delivery_time.asc(),
                         CustomerOrder.id.asc())
                .limit(200).all())
    from datetime import date as _date, timedelta as _td
    _today = _date.today()
    urgency: dict[str, str] = {}
    for o in orders:
        d = o.required_delivery_date
        done = (o.status or "") in ("Delivered", "Closed", "Cancelled")
        if not d or done:
            urgency[o.order_no] = ""
        elif d < _today:
            urgency[o.order_no] = "LATE"
        elif d == _today:
            urgency[o.order_no] = "TODAY"
        elif d == _today + _td(days=1):
            urgency[o.order_no] = "TOMORROW"
        else:
            urgency[o.order_no] = ""
    stats = _order_stats(db)
    # Batch 81 fix: these 3 operational KPIs (Awaiting/Scheduled/BOM Ready)
    # used to always count every order in the system, completely ignoring
    # the Today/This Week/This Month/Pending/All view and the Delivery
    # From/To/Status filter above — so the cards told a different story
    # than the table right below them. They now count within the SAME
    # filtered date range as the table (re-using `q` before .limit() is
    # applied), while still breaking out by their own specific status so
    # all 3 cards stay meaningful together rather than collapsing to the
    # same number.
    stats.update({
        # Batch 94: the sales-review exclusion moved into _filtered_orders_query
        # itself, so `q` is already gated here. Keeping the filter duplicated on
        # this one card was what made the cards and the table disagree in the
        # first place — the card knew about the gate, the table didn't.
        "awaiting_head_chef": q.filter(CustomerOrder.status == "Submitted").count(),
        "scheduled": q.filter(CustomerOrder.cooking_date.isnot(None)).count(),
        "bom_ready": q.filter(CustomerOrder.status == "Head Chef Approved").count(),
        "filtered_total": q.count(),
    })
    return render(request, "production/head_chef.html", {"orders": orders, "stats": stats, "filters": filters, "urgency": urgency, "page_title": "Head Chef Planning", "error": request.query_params.get("error")})


@router.get("/store-issuance")
async def store_issuance_dashboard(request: Request, db: Session = Depends(get_db)):
    _ensure_sales_review_schema(db)
    require_area(request, "store_issuance")
    orders, line_map, filters = _store_order_summary(db, request)
    stats = {
        "orders": len(orders),
        "store_pending": db.query(CustomerOrder).filter(CustomerOrder.status == "Store Pending").count(),
        "in_production": db.query(CustomerOrder).filter(CustomerOrder.status == "In Production").count(),
        "total_lines": db.query(StoreIssuanceLine).count(),
        "finalized_lines": db.query(StoreIssuanceLine).filter(StoreIssuanceLine.finalized == True).count(),
        "pending_lines": db.query(StoreIssuanceLine).filter(StoreIssuanceLine.finalized == False).count(),
    }
    return render(request, "production/store_issuance.html", {"orders": orders, "line_map": line_map, "stats": stats, "filters": filters, "lines": [], "page_title": "Store Issuance"})


# ============================================================================
# Batch 20 — Store: consolidated section-wise issuance view
# The store keeper can see all pending material grouped BY KITCHEN SECTION
# (across every released order), sorted by delivery-date priority, and jump
# straight to the owning order's issuance screen.
# ============================================================================
@router.get("/store-issuance/by-section/export")
async def store_issuance_by_section_export(request: Request, db: Session = Depends(get_db)):
    """UI fix: let the store keeper download the section-grouped picking
    list as CSV — either one section (?section=Cutting) or everything under
    the current filters (order/customer/date range apply the same as the
    on-screen view, giving an orderwise download by filtering to one order)."""
    require_area(request, "store_issuance")
    section_filter = (request.query_params.get("section") or "").strip()
    show = (request.query_params.get("show") or "all").strip()
    order_filter = (request.query_params.get("order") or "").strip()
    customer_filter = (request.query_params.get("customer") or "").strip()
    date_from = (request.query_params.get("date_from") or "").strip()
    date_to = (request.query_params.get("date_to") or "").strip()

    where = "1=1"
    params: dict = {}
    if show == "pending":
        where += " AND COALESCE(s.finalized, 0) = 0"
    if section_filter:
        where += " AND s.issue_to_section = :sec"
        params["sec"] = section_filter
    if order_filter:
        where += " AND s.order_no LIKE :ord"
        params["ord"] = f"%{order_filter}%"
    if customer_filter:
        where += " AND COALESCE(co.customer_name,'') LIKE :cust"
        params["cust"] = f"%{customer_filter}%"
    if date_from:
        where += " AND COALESCE(co.required_delivery_date,'') >= :df"
        params["df"] = date_from
    if date_to:
        where += " AND COALESCE(co.required_delivery_date,'') <= :dt"
        params["dt"] = date_to

    raw_rows = db.execute(text(f"""
        SELECT s.issue_to_section AS section, s.order_no, co.customer_name,
               s.recipe_no, s.recipe_name, s.ingredient_code, s.ingredient_name,
               COALESCE(s.required_qty_with_waste_standard, s.required_qty_standard, 0) AS required_qty,
               COALESCE(s.input_material_issued, 0) AS issued_qty,
               COALESCE(s.standard_uom, 'Kg') AS uom,
               COALESCE(s.issuance_status, 'Pending') AS issuance_status,
               COALESCE(s.finalized, 0) AS finalized,
               COALESCE(co.required_delivery_date, '') AS delivery_date
        FROM store_issuance_lines s
        LEFT JOIN customer_orders co ON co.order_no = s.order_no
        WHERE {where}
        ORDER BY (co.required_delivery_date IS NULL), co.required_delivery_date ASC,
                 s.issue_to_section, s.ingredient_name
        LIMIT 5000
    """), params).mappings().all()

    # ------------------------------------------------------------------
    # Batch 123 FIX (images 9,10): these buttons are labelled "Consolidated
    # picking list" but were dumping every raw store-issuance line. Consolidate
    # by (section, ingredient) — sum required + issued, and collect the distinct
    # orders/customers so the store still sees who the pull is for. The raw dump
    # is still available with ?consolidated=0 if ever needed.
    # ------------------------------------------------------------------
    consolidated = (request.query_params.get("consolidated") or "1").strip() != "0"
    if consolidated:
        agg: dict = {}
        for r in raw_rows:
            key = (r["section"], r["ingredient_code"], r["uom"])
            if key not in agg:
                agg[key] = {
                    "section": r["section"], "ingredient_code": r["ingredient_code"],
                    "ingredient_name": r["ingredient_name"], "uom": r["uom"],
                    "required_qty": 0.0, "issued_qty": 0.0,
                    "orders": set(), "customers": set(),
                    "all_finalized": True,
                }
            a = agg[key]
            a["required_qty"] += float(r["required_qty"] or 0)
            a["issued_qty"] += float(r["issued_qty"] or 0)
            if r["order_no"]:
                a["orders"].add(r["order_no"])
            if r["customer_name"]:
                a["customers"].add(r["customer_name"])
            if not int(r["finalized"] or 0):
                a["all_finalized"] = False
        rows = []
        for a in sorted(agg.values(), key=lambda x: (x["section"] or "", x["ingredient_name"] or "")):
            rows.append({
                "section": a["section"],
                "ingredient_code": a["ingredient_code"],
                "ingredient_name": a["ingredient_name"],
                "uom": a["uom"],
                "required_qty": a["required_qty"],
                "issued_qty": a["issued_qty"],
                "orders": ", ".join(sorted(a["orders"])),
                "customers": ", ".join(sorted(a["customers"])),
                "issuance_status": "Issued" if a["all_finalized"] else "Partial / Pending",
            })
    else:
        rows = [dict(r) for r in raw_rows]

    output = io.StringIO()
    writer = csv.writer(output)
    if consolidated:
        writer.writerow(["Section", "Ingredient Code", "Ingredient Name",
                          "Total Required", "Total Issued", "UOM",
                          "Orders", "Customers", "Status"])
        for r in rows:
            writer.writerow([r["section"], r["ingredient_code"], r["ingredient_name"],
                              f'{r["required_qty"]:.3f}', f'{r["issued_qty"]:.3f}', r["uom"],
                              r["orders"], r["customers"], r["issuance_status"]])
    else:
        writer.writerow(["Section", "Order", "Customer", "Delivery Date", "Recipe Code", "Recipe Name",
                          "Ingredient Code", "Ingredient Name", "Required Qty", "Issued Qty", "UOM",
                          "Status", "Finalized"])
        for r in rows:
            writer.writerow([r["section"], r["order_no"], r["customer_name"], r["delivery_date"],
                              r["recipe_no"], r["recipe_name"], r["ingredient_code"], r["ingredient_name"],
                              r["required_qty"], r["issued_qty"], r["uom"], r["issuance_status"], r["finalized"]])
    output.seek(0)

    # Batch 121: PDF export — same consolidated section/table data as the CSV.
    export_fmt = (request.query_params.get("format") or "csv").strip().lower()
    if export_fmt == "pdf":
        try:
            from reportlab.lib import colors
            from reportlab.lib.pagesizes import A4, landscape
            from reportlab.lib.units import mm
            from reportlab.platypus import (SimpleDocTemplate, Table, TableStyle,
                                            Paragraph, Spacer)
            from reportlab.lib.styles import getSampleStyleSheet

            buf = io.BytesIO()
            doc = SimpleDocTemplate(buf, pagesize=landscape(A4),
                                    leftMargin=12 * mm, rightMargin=12 * mm,
                                    topMargin=12 * mm, bottomMargin=12 * mm)
            styles = getSampleStyleSheet()
            elems = [
                Paragraph("Store Issuance — " + ("Consolidated Picking List" if consolidated else "Full Line Detail"), styles["Title"]),
                Paragraph(
                    f"Section: {section_filter or 'All sections'} &nbsp;·&nbsp; "
                    f"Show: {show} &nbsp;·&nbsp; Generated: {date.today().isoformat()} &nbsp;·&nbsp; ISFC ERP",
                    styles["Normal"]),
                Spacer(1, 6 * mm),
            ]
            head = ["Section", "Ingredient Code", "Ingredient", "Total Required",
                    "Total Issued", "UOM", "Orders", "Customers", "Status"] if consolidated else \
                   ["Section", "Order", "Customer", "Recipe", "Ingredient",
                    "Required", "Issued", "UOM", "Status"]
            data = [head]
            for r in rows:
                if consolidated:
                    data.append([
                        r["section"], r["ingredient_code"], r["ingredient_name"] or "",
                        f'{r["required_qty"]:.3f}', f'{r["issued_qty"]:.3f}', r["uom"],
                        r["orders"], r["customers"], r["issuance_status"],
                    ])
                else:
                    data.append([
                        r["section"], r["order_no"], r["customer_name"] or "",
                        f'{r["recipe_no"] or ""} {r["recipe_name"] or ""}'.strip(),
                        f'{r["ingredient_code"] or ""} {r["ingredient_name"] or ""}'.strip(),
                        f'{float(r["required_qty"] or 0):.2f}',
                        f'{float(r["issued_qty"] or 0):.2f}',
                        r["uom"], r["issuance_status"],
                    ])
            tbl = Table(data, repeatRows=1)
            tbl.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1e3a5f")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTSIZE", (0, 0), (-1, -1), 6.5),
                ("FONTSIZE", (0, 0), (-1, 0), 7),
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#d8e2ef")),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f5f8fc")]),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 3),
                ("RIGHTPADDING", (0, 0), (-1, -1), 3),
            ]))
            elems.append(tbl)
            doc.build(elems)
            buf.seek(0)
            pfname = f"store-issuance_{section_filter or 'all-sections'}_{date.today().isoformat()}.pdf".replace(" ", "-").replace("/", "-")
            return StreamingResponse(iter([buf.getvalue()]), media_type="application/pdf",
                                     headers={"Content-Disposition": f"attachment; filename={pfname}"})
        except Exception as exc:
            # Never let a PDF library issue block the store keeper — fall back to CSV.
            import logging as _logging
            _logging.getLogger("isfc.production").warning(
                "By-section PDF export failed, falling back to CSV: %s", exc)

    fname = f"store-issuance_{section_filter or 'all-sections'}_{date.today().isoformat()}.csv".replace(" ", "-").replace("/", "-")
    return StreamingResponse(iter(['\ufeff' + output.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f"attachment; filename={fname}"})


@router.post("/store-issuance/{line_id}/quick-issue")
async def quick_issue_store_line(request: Request, line_id: int, db: Session = Depends(get_db)):
    """UI fix: issue a line at its full required quantity directly from the
    'Store Issuance — By Kitchen Section' grouped view, without navigating
    to the order-level page first. Uses the exact same update path (and
    audit trail) as the manual per-order issuance form."""
    require_action(request, "store_issuance", "edit")
    line = db.query(StoreIssuanceLine).filter(StoreIssuanceLine.id == line_id).first()
    if not line:
        return redirect_with_error("/production/store-issuance/by-section", "Store issuance line not found.")
    full_qty = float(getattr(line, "required_qty_with_waste_standard", None)
                      or getattr(line, "required_qty_standard", None) or 0)
    uom = getattr(line, "standard_uom", None) or "Kg"
    target_section = getattr(line, "issue_to_section", None) or ""

    try:
        update_store_issuance_line(db, line_id, full_qty, uom, target_section, "", "",
                                   "Quick issued from Store Issuance by Section view")
    except ValueError as exc:
        return redirect_with_error("/production/store-issuance/by-section", str(exc))

    ref = request.headers.get("referer") or "/production/store-issuance/by-section"
    sep = "&" if "?" in ref else "?"
    return RedirectResponse(f"{ref}{sep}toast=success&title=Issued&msg={line.ingredient_name}: quick-issued {full_qty} {uom}",
                            status_code=HTTP_303_SEE_OTHER)


@router.get("/store-issuance/by-section")
async def store_issuance_by_section(request: Request, db: Session = Depends(get_db)):
    require_area(request, "store_issuance")
    section_filter = (request.query_params.get("section") or "").strip()
    show = (request.query_params.get("show") or "all").strip()  # Batch 121: default to all lines
    order_filter = (request.query_params.get("order") or "").strip()
    customer_filter = (request.query_params.get("customer") or "").strip()
    date_from = (request.query_params.get("date_from") or "").strip()
    date_to = (request.query_params.get("date_to") or "").strip()

    where = "1=1"
    params: dict = {}
    if show == "pending":
        where += " AND COALESCE(s.finalized, 0) = 0"
    if section_filter:
        where += " AND s.issue_to_section = :sec"
        params["sec"] = section_filter
    # Batch 72: extra store-keeper filters (order / customer / delivery date range)
    if order_filter:
        where += " AND s.order_no LIKE :ord"
        params["ord"] = f"%{order_filter}%"
    if customer_filter:
        where += " AND COALESCE(co.customer_name,'') LIKE :cust"
        params["cust"] = f"%{customer_filter}%"
    if date_from:
        where += " AND COALESCE(co.required_delivery_date,'') >= :df"
        params["df"] = date_from
    if date_to:
        where += " AND COALESCE(co.required_delivery_date,'') <= :dt"
        params["dt"] = date_to

    rows = db.execute(text(f"""
        SELECT s.id, s.order_no, s.issue_to_section AS section,
               s.recipe_no, s.recipe_name, s.ingredient_code, s.ingredient_name,
               COALESCE(s.required_qty_with_waste_standard, s.required_qty_standard, 0) AS required_qty,
               COALESCE(s.input_material_issued, 0) AS issued_qty,
               COALESCE(s.standard_uom, 'Kg') AS uom,
               COALESCE(s.issuance_status, 'Pending') AS issuance_status,
               COALESCE(s.finalized, 0) AS finalized,
               COALESCE(co.required_delivery_date, '') AS delivery_date,
               COALESCE(co.required_delivery_time, '') AS delivery_time,
               COALESCE(co.customer_name, '') AS customer_name
        FROM store_issuance_lines s
        LEFT JOIN customer_orders co ON co.order_no = s.order_no
        WHERE {where}
        ORDER BY (co.required_delivery_date IS NULL), co.required_delivery_date ASC,
                 s.issue_to_section, s.ingredient_name
        LIMIT 2000
    """), params).mappings().all()

    # Group by section, with a consolidated per-ingredient rollup inside each.
    groups: dict = {}
    for r in rows:
        sec = r["section"] or "Unassigned"
        g = groups.setdefault(sec, {"section": sec, "lines": [], "total_required": 0.0,
                                     "total_issued": 0.0, "pending": 0, "consolidated": {}})
        g["lines"].append(dict(r))
        g["total_required"] += float(r["required_qty"] or 0)
        g["total_issued"] += float(r["issued_qty"] or 0)
        if not r["finalized"]:
            g["pending"] += 1
        key = (r["ingredient_code"], r["uom"])
        c = g["consolidated"].setdefault(key, {"ingredient_code": r["ingredient_code"],
                                               "ingredient_name": r["ingredient_name"],
                                               "uom": r["uom"], "required": 0.0, "issued": 0.0, "orders": set()})
        c["required"] += float(r["required_qty"] or 0)
        c["issued"] += float(r["issued_qty"] or 0)
        c["orders"].add(r["order_no"])

    section_groups = []
    for sec in sorted(groups):
        g = groups[sec]
        g["consolidated"] = sorted(
            ({**c, "orders": sorted(c["orders"])} for c in g["consolidated"].values()),
            key=lambda c: c["ingredient_name"])
        section_groups.append(g)

    all_sections = [r[0] for r in db.execute(text(
        "SELECT DISTINCT issue_to_section FROM store_issuance_lines WHERE issue_to_section IS NOT NULL ORDER BY 1")).all()]

    return render(request, "production/store_issuance_by_section.html", {
        "groups": section_groups, "all_sections": all_sections,
        "filters": {"section": section_filter, "show": show,
                    "order": order_filter, "customer": customer_filter,
                    "date_from": date_from, "date_to": date_to},
        "page_title": "Store Issuance by Section",
    })


# JSON APIs for future React/mobile screens
@router.get("/api/orders/{order_no}/bom/consolidated")
async def api_consolidated_bom(order_no: str, db: Session = Depends(get_db)):
    return consolidated_bom(db, order_no=order_no)


@router.get("/api/bakery-pastry/consolidated")
async def api_bakery_consolidated(db: Session = Depends(get_db)):
    return bakery_pastry_consolidated(db)


@router.get("/kitchen-summary")
async def kitchen_summary(request: Request, db: Session = Depends(get_db)):
    """All Section Summary - one row per kitchen section with live workload.

    Fixes the sidebar 404: /production/kitchen-summary now exists.
    Batch 81: added date-range + order-number filtering — this page
    previously had no filter at all, so "today's workload" vs. "everything
    ever processed" were impossible to tell apart.
    """
    require_area(request, "kitchen_summary")
    date_from = (request.query_params.get("date_from") or "").strip()
    date_to = (request.query_params.get("date_to") or "").strip()
    order_search = (request.query_params.get("search") or "").strip()

    where = "1=1"
    params: dict = {}
    if date_from:
        where += " AND DATE(k.updated_at) >= :df"
        params["df"] = date_from
    if date_to:
        where += " AND DATE(k.updated_at) <= :dt"
        params["dt"] = date_to
    if order_search:
        where += " AND k.order_no LIKE :s"
        params["s"] = f"%{order_search}%"

    rows = db.execute(text(f"""
        SELECT
            k.current_section AS section,
            COUNT(DISTINCT k.order_no) AS orders,
            COUNT(*) AS total_lines,
            SUM(CASE WHEN COALESCE(k.received_qty_standard,0) > 0 THEN 1 ELSE 0 END) AS received_lines,
            SUM(CASE WHEN UPPER(COALESCE(k.transaction_status,'')) LIKE 'COMPLETED%' OR UPPER(COALESCE(k.transaction_status,'')) = 'TRANSFERRED' THEN 1 ELSE 0 END) AS completed_lines,
            ROUND(SUM(COALESCE(k.issued_qty_standard,0)), 2) AS issued_qty,
            ROUND(SUM(COALESCE(k.received_qty_standard,0)), 2) AS received_qty,
            ROUND(SUM(COALESCE(k.waste_qty_standard,0)), 2) AS waste_qty,
            ROUND(SUM(COALESCE(k.balance_qty_standard,0)), 2) AS balance_qty,
            MAX(k.updated_at) AS last_activity
        FROM kitchen_section_transactions k
        WHERE {where}
        GROUP BY k.current_section
        ORDER BY FIELD(k.current_section, 'Cutting','Butchery','Hot Kitchen','Cold Kitchen','Bakery/Pastry','QC','Trayline / Packing'), k.current_section
    """), params).mappings().all()

    totals = {
        "orders": sum(int(r.get("orders") or 0) for r in rows),
        "lines": sum(int(r.get("total_lines") or 0) for r in rows),
        "received": sum(int(r.get("received_lines") or 0) for r in rows),
        "completed": sum(int(r.get("completed_lines") or 0) for r in rows),
    }
    return render(request, "production/kitchen_summary.html", {
        "rows": rows,
        "totals": totals,
        "section_slugs": {s: _section_slug(s) for s in CANONICAL_SECTIONS},
        "filters": {"date_from": date_from, "date_to": date_to, "search": order_search},
        "page_title": "All Section Summary",
    })


@router.get("/orders/{order_no}/store-issuance/history")
async def store_issuance_history(request: Request, order_no: str, db: Session = Depends(get_db)):
    """Re-issue / edit audit history for one order's store issuance."""
    require_area(request, "store_issuance")
    try:
        rows = db.execute(text("""
            SELECT * FROM store_issue_audit
            WHERE order_no = :order_no
            ORDER BY changed_at DESC, id DESC
        """), {"order_no": order_no}).mappings().all()
    except Exception:
        rows = []
    return render(request, "production/store_issue_history.html", {
        "rows": rows, "order_no": order_no,
        "page_title": f"Issuance History {order_no}",
    })
