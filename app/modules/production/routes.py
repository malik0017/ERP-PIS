# app/modules/production/routes.py
from datetime import date, datetime, timedelta
from typing import List, Optional
import csv
import io

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, StreamingResponse
from sqlalchemy import func, text
from sqlalchemy.orm import Session
from starlette.status import HTTP_303_SEE_OTHER

from app.core.templates import render
from app.core.rbac import require_area, require_action
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
    receive_transaction,
    transfer_transaction,
    update_store_issuance_line,
)

router = APIRouter(prefix="/production", tags=["Production"])


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


def _filtered_orders_query(db: Session, request: Request, date_column: str = "order_date"):
    view, status, date_from, date_to = _date_filter_params(request)
    q = db.query(CustomerOrder)

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


def _order_flow_status(db: Session, order_no: str) -> dict:
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
    require_area(request, "production_orders")
    q, filters = _filtered_orders_query(db, request, date_column="order_date")
    orders = q.order_by(CustomerOrder.id.desc()).limit(200).all()
    context = master_dropdown_context(db, _company_id_from_session(request))
    context.update(
        {
            "orders": orders,
            "stats": _order_stats(db),
            "filters": filters,
            "page_title": "Production Orders",
            "error": request.query_params.get("error"),
            "created": request.query_params.get("created"),
        }
    )
    return render(request, "production/orders.html", context)


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
    db: Session = Depends(get_db),
):
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
        return redirect_with_error("/production/orders", "Please enter at least one recipe with portions greater than zero.")

    # Batch 69: 48-hour rule — delivery must be at least 48 hours from now.
    # Enforced server-side so it can't be bypassed by editing the form. Admins
    # and internal staff placing back-dated/urgent orders can be exempted later
    # via a permission if needed; for now the rule applies to all order entry.
    _deliv = _parse_date(required_delivery_date)
    if _deliv:
        try:
            _t = (required_delivery_time or "00:00")[:5]
            _hh, _mm = (int(x) for x in _t.split(":")[:2])
        except Exception:
            _hh, _mm = 0, 0
        _deliv_dt = datetime(_deliv.year, _deliv.month, _deliv.day, _hh, _mm)
        if _deliv_dt < datetime.now() + timedelta(hours=48):
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
        order = create_order(db, payload, created_by=current_user_name(request))
    except ValueError as exc:
        return redirect_with_error("/production/orders", str(exc))

    return RedirectResponse(f"/production/orders?created={order.order_no}", status_code=HTTP_303_SEE_OTHER)


@router.get("/orders/{order_no}")
async def order_detail(request: Request, order_no: str, db: Session = Depends(get_db)):
    require_area(request, "production_orders")
    order = db.query(CustomerOrder).filter(CustomerOrder.order_no == order_no).first()
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
            "flow_status": _order_flow_status(db, order_no),
            "page_title": order_no,
            "error": request.query_params.get("error"),
        },
    )


@router.post("/orders/{order_no}/generate-bom")
async def generate_bom(request: Request, order_no: str, db: Session = Depends(get_db)):
    require_action(request, "bom", "add")
    try:
        order = db.query(CustomerOrder).filter(CustomerOrder.order_no == order_no).first()
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
        order = db.query(CustomerOrder).filter(CustomerOrder.order_no == order_no).first()
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

    order = db.query(CustomerOrder).filter(CustomerOrder.order_no == order_no).first()
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
    order = db.query(CustomerOrder).filter(CustomerOrder.order_no == order_no).first()
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
    if order and (order.status or "") not in ("Store Pending", "BOM Generated", "Head Chef Approved", "In Production"):
        return redirect_with_error("/production/store-issuance", f"Order {line.order_no} is already {order.status} — reissue not allowed.")
    line.finalized = False
    db.commit()
    return RedirectResponse(f"/production/store-issuance?toast=success&title=Re-issue&msg=Line {line_id} reopened for editing", status_code=HTTP_303_SEE_OTHER)


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
    order = db.query(CustomerOrder).filter(CustomerOrder.order_no == order_no).first()
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

    msg = f"{ok} line(s) processed and transferred"
    if failed:
        msg += f", {failed} skipped (already locked or not receivable)"
    kind = "success" if ok else "warning"
    return RedirectResponse(
        f"/production/section/{_section_slug(section)}/orders/{order_no}?toast={kind}&title=Bulk+Process&msg={msg}",
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
    stats.update({
        "awaiting_head_chef": db.query(CustomerOrder).filter(CustomerOrder.status == "Submitted").count(),
        "scheduled": db.query(CustomerOrder).filter(CustomerOrder.cooking_date.isnot(None)).count(),
        "bom_ready": db.query(CustomerOrder).filter(CustomerOrder.status == "Head Chef Approved").count(),
    })
    return render(request, "production/head_chef.html", {"orders": orders, "stats": stats, "filters": filters, "urgency": urgency, "page_title": "Head Chef Planning", "error": request.query_params.get("error")})


@router.get("/store-issuance")
async def store_issuance_dashboard(request: Request, db: Session = Depends(get_db)):
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
    show = (request.query_params.get("show") or "pending").strip()
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

    rows = db.execute(text(f"""
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

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Section", "Order", "Customer", "Delivery Date", "Recipe Code", "Recipe Name",
                      "Ingredient Code", "Ingredient Name", "Required Qty", "Issued Qty", "UOM",
                      "Status", "Finalized"])
    for r in rows:
        writer.writerow([r["section"], r["order_no"], r["customer_name"], r["delivery_date"],
                          r["recipe_no"], r["recipe_name"], r["ingredient_code"], r["ingredient_name"],
                          r["required_qty"], r["issued_qty"], r["uom"], r["issuance_status"], r["finalized"]])
    output.seek(0)
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
    show = (request.query_params.get("show") or "pending").strip()  # pending | all
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
    """
    require_area(request, "kitchen_summary")
    rows = db.execute(text("""
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
        GROUP BY k.current_section
        ORDER BY FIELD(k.current_section, 'Cutting','Butchery','Hot Kitchen','Cold Kitchen','Bakery/Pastry','QC','Trayline / Packing'), k.current_section
    """)).mappings().all()

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
