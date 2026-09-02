from fastapi.responses import RedirectResponse
from fastapi import APIRouter, Depends, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.templates import render
from app.core.rbac import require_area
from app.database.session import get_db

router = APIRouter(tags=["dashboard"])


def _one(db: Session, sql: str, params: dict | None = None):
    try:
        return db.execute(text(sql), params or {}).scalar() or 0
    except Exception:
        return 0


def _rows(db: Session, sql: str, params: dict | None = None):
    try:
        return [dict(r) for r in db.execute(text(sql), params or {}).mappings().all()]
    except Exception:
        return []


def _pct(value: float, base: float) -> float:
    try:
        return round((float(value or 0) / float(base or 0)) * 100, 2) if float(base or 0) else 0
    except Exception:
        return 0


@router.get("/dashboard", name="dashboard")
async def dashboard(request: Request, db: Session = Depends(get_db)):
    require_area(request, "dashboard")
    company_id = int(request.session.get("company_id") or 1)

    active_recipes = _one(db, """
        SELECT COUNT(*) FROM recipes
        WHERE company_id = :company_id
          AND UPPER(TRIM(COALESCE(status,''))) = 'ACTIVE'
          AND COALESCE(is_active,1) = 1
    """, {"company_id": company_id})
    total_recipes = _one(db, "SELECT COUNT(*) FROM recipes WHERE company_id = :company_id", {"company_id": company_id})
    pending_recipes = _one(db, """
        SELECT COUNT(*) FROM recipes
        WHERE company_id = :company_id
          AND UPPER(TRIM(COALESCE(status,''))) = 'PENDING'
    """, {"company_id": company_id})
    recipe_bom_lines = _one(db, """
        SELECT COUNT(*)
        FROM recipe_ingredients ri
        JOIN recipes r ON r.id = ri.recipe_id
        WHERE r.company_id = :company_id
          AND UPPER(TRIM(COALESCE(r.status,''))) = 'ACTIVE'
          AND COALESCE(r.is_active,1) = 1
    """, {"company_id": company_id})

    order_totals = _rows(db, """
        SELECT
          COUNT(*) AS orders,
          COALESCE(SUM(total_planned_portions),0) AS portions,
          COALESCE(SUM(total_estimated_food_cost),0) AS food_cost,
          COALESCE(SUM(total_estimated_selling_value),0) AS selling_value,
          COALESCE(SUM(total_estimated_margin),0) AS margin
        FROM customer_orders
    """)
    order_total = order_totals[0] if order_totals else {"orders": 0, "portions": 0, "food_cost": 0, "selling_value": 0, "margin": 0}
    margin_pct = _pct(order_total.get("margin"), order_total.get("selling_value"))

    kpis = {
        "active_recipes": active_recipes,
        "total_recipes": total_recipes,
        "pending_recipes": pending_recipes,
        "recipe_bom_lines": recipe_bom_lines,
        "inventory_items": _one(db, "SELECT COUNT(*) FROM ingredients WHERE company_id = :company_id OR company_id IS NULL", {"company_id": company_id}),
        "customers": _one(db, "SELECT COUNT(*) FROM customers WHERE company_id = :company_id", {"company_id": company_id}),
        "suppliers": _one(db, "SELECT COUNT(*) FROM suppliers WHERE company_id = :company_id", {"company_id": company_id}),
        "open_orders": _one(db, """
            SELECT COUNT(*) FROM customer_orders
            WHERE COALESCE(status,'') NOT IN ('Dispatched','Closed','Cancelled')
        """),
        "orders": order_total.get("orders", 0),
        "portions": order_total.get("portions", 0),
        "food_cost": order_total.get("food_cost", 0),
        "selling_value": order_total.get("selling_value", 0),
        "margin": order_total.get("margin", 0),
        "margin_pct": margin_pct,
        "bom_lines": _one(db, "SELECT COUNT(*) FROM bom_lines"),
        "store_lines": _one(db, "SELECT COUNT(*) FROM store_issuance_lines"),
        "store_finalized": _one(db, "SELECT COUNT(*) FROM store_issuance_lines WHERE COALESCE(finalized,0)=1"),
        "section_txns": _one(db, "SELECT COUNT(*) FROM kitchen_section_transactions"),
        "qc_checks": _one(db, "SELECT COUNT(*) FROM qc_checks"),
        "packing_pending": _one(db, "SELECT COUNT(*) FROM packing_dispatch WHERE COALESCE(dispatch_status,'Packing Pending') IN ('Packing Pending','Packing In Progress','Pending')"),
        "packed_orders": _one(db, "SELECT COUNT(*) FROM packing_dispatch WHERE dispatch_status = 'Packed'"),
        "dispatches": _one(db, "SELECT COUNT(*) FROM packing_dispatch"),
        "qc_waiting_orders": _one(db, """
            SELECT COUNT(DISTINCT order_no) FROM kitchen_section_transactions
            WHERE current_section = 'QC'
              AND UPPER(COALESCE(transaction_status,'')) NOT IN ('QC PASSED','QC REJECTED')
        """),
        "dispatch_pending": _one(db, """
            SELECT COUNT(*) FROM packing_dispatch
            WHERE COALESCE(dispatch_status,'') IN ('Packed','Out for Delivery')
        """),
        "delivered_orders": _one(db, "SELECT COUNT(*) FROM packing_dispatch WHERE dispatch_status = 'Delivered'"),
    }

    kpis["store_progress_pct"] = _pct(kpis["store_finalized"], kpis["store_lines"])
    kpis["recipe_live_pct"] = _pct(kpis["active_recipes"], kpis["total_recipes"])
    kpis["bom_per_order"] = round(float(kpis["bom_lines"] or 0) / float(kpis["orders"] or 1), 2) if kpis["orders"] else 0

    order_status = _rows(db, """
        SELECT COALESCE(NULLIF(status,''),'Submitted') AS label, COUNT(*) AS total
        FROM customer_orders
        GROUP BY COALESCE(NULLIF(status,''),'Submitted')
        ORDER BY total DESC, label ASC
    """)

    recipe_categories = _rows(db, """
        SELECT COALESCE(NULLIF(category,''),'Unassigned') AS label, COUNT(*) AS total
        FROM recipes
        WHERE company_id = :company_id
          AND UPPER(TRIM(COALESCE(status,''))) = 'ACTIVE'
          AND COALESCE(is_active,1) = 1
        GROUP BY COALESCE(NULLIF(category,''),'Unassigned')
        ORDER BY total DESC, label ASC
        LIMIT 10
    """, {"company_id": company_id})

    recipe_customer_mix = _rows(db, """
        SELECT COALESCE(NULLIF(customer_name,''),'Unassigned') AS label, COUNT(*) AS total
        FROM recipes
        WHERE company_id = :company_id
          AND UPPER(TRIM(COALESCE(status,''))) = 'ACTIVE'
          AND COALESCE(is_active,1) = 1
        GROUP BY COALESCE(NULLIF(customer_name,''),'Unassigned')
        ORDER BY total DESC, label ASC
        LIMIT 8
    """, {"company_id": company_id})

    recent_orders = _rows(db, """
        SELECT order_no, customer_name, brand, channel, status,
               COALESCE(required_delivery_date,'') AS delivery_date,
               COALESCE(required_delivery_time,'') AS delivery_time,
               COALESCE(total_planned_portions,0) AS portions,
               COALESCE(total_estimated_food_cost,0) AS food_cost,
               COALESCE(total_estimated_selling_value,0) AS selling_value,
               COALESCE(total_estimated_margin,0) AS margin,
               created_at
        FROM customer_orders
        ORDER BY id DESC
        LIMIT 10
    """)

    top_bom_sections = _rows(db, """
        SELECT x.label, COUNT(*) AS lines, ROUND(SUM(x.qty), 2) AS qty, ROUND(SUM(x.cost), 2) AS cost
        FROM (
            SELECT
                COALESCE(NULLIF(bl.default_issue_section,''), NULLIF(si.issue_to_section,''), NULLIF(i.default_issue_section,''), 'Unassigned') AS label,
                COALESCE(bl.total_required_with_waste_standard, bl.required_qty_standard, si.required_qty_with_waste_standard, si.required_qty_standard, 0) AS qty,
                COALESCE(NULLIF(bl.estimated_cost,0),
                         COALESCE(bl.total_required_with_waste_standard, bl.required_qty_standard, si.required_qty_with_waste_standard, si.required_qty_standard, 0)
                         * COALESCE(NULLIF(bl.unit_cost_standard,0), NULLIF(i.unit_cost_standard,0), 0), 0) AS cost
            FROM bom_lines bl
            LEFT JOIN store_issuance_lines si ON si.bom_line_id = bl.id
            LEFT JOIN ingredients i ON LOWER(TRIM(i.ingredient_code)) = LOWER(TRIM(bl.ingredient_code))
        ) x
        GROUP BY x.label
        HAVING lines > 0
        ORDER BY cost DESC, lines DESC
        LIMIT 8
    """)

    bom_category_cost = _rows(db, """
        SELECT x.label, COUNT(*) AS lines, ROUND(SUM(x.cost), 2) AS cost, ROUND(SUM(x.qty), 2) AS qty
        FROM (
            SELECT
                COALESCE(NULLIF(bl.ingredient_main_category,''), NULLIF(si.ingredient_main_category,''), NULLIF(i.main_category,''), NULLIF(bl.ingredient_category,''), NULLIF(i.category,''), 'Unassigned') AS label,
                COALESCE(bl.total_required_with_waste_standard, bl.required_qty_standard, si.required_qty_with_waste_standard, si.required_qty_standard, 0) AS qty,
                COALESCE(NULLIF(bl.estimated_cost,0),
                         COALESCE(bl.total_required_with_waste_standard, bl.required_qty_standard, si.required_qty_with_waste_standard, si.required_qty_standard, 0)
                         * COALESCE(NULLIF(bl.unit_cost_standard,0), NULLIF(i.unit_cost_standard,0), 0), 0) AS cost
            FROM bom_lines bl
            LEFT JOIN store_issuance_lines si ON si.bom_line_id = bl.id
            LEFT JOIN ingredients i ON LOWER(TRIM(i.ingredient_code)) = LOWER(TRIM(bl.ingredient_code))
        ) x
        GROUP BY x.label
        HAVING lines > 0
        ORDER BY cost DESC, lines DESC
        LIMIT 8
    """)

    store_status = _rows(db, """
        SELECT COALESCE(NULLIF(issuance_status,''),'Pending') AS label, COUNT(*) AS total
        FROM store_issuance_lines
        GROUP BY COALESCE(NULLIF(issuance_status,''),'Pending')
        ORDER BY total DESC, label ASC
    """)

    production_flow = [
        {"label": "Orders", "total": _one(db, "SELECT COUNT(*) FROM customer_orders"), "url": "/production/orders"},
        {"label": "Head Chef", "total": _one(db, "SELECT COUNT(*) FROM head_chef_plans"), "url": "/production/head-chef"},
        {"label": "BOM Lines", "total": _one(db, "SELECT COUNT(*) FROM bom_lines"), "url": "/production/orders"},
        {"label": "Store Lines", "total": _one(db, "SELECT COUNT(*) FROM store_issuance_lines"), "url": "/production/store-issuance"},
        {"label": "Section Txns", "total": _one(db, "SELECT COUNT(*) FROM kitchen_section_transactions"), "url": "/production/section/Bakery-Pastry"},
        {"label": "QC Queue", "total": kpis["qc_waiting_orders"], "url": "/qc"},
        {"label": "QC Checks", "total": _one(db, "SELECT COUNT(*) FROM qc_checks"), "url": "/qc"},
        {"label": "Packing", "total": kpis["packing_pending"] + kpis["packed_orders"], "url": "/packing"},
        {"label": "Dispatch", "total": kpis["dispatch_pending"], "url": "/dispatch"},
    ]

    action_items = []
    if kpis["pending_recipes"]:
        action_items.append({"level": "warning", "title": "Pending recipe approvals", "value": kpis["pending_recipes"], "url": "/recipes/pending"})
    if kpis["open_orders"]:
        action_items.append({"level": "info", "title": "Open production orders", "value": kpis["open_orders"], "url": "/production/orders"})
    if kpis["store_lines"] and kpis["store_progress_pct"] < 100:
        action_items.append({"level": "warning", "title": "Store lines not finalized", "value": kpis["store_lines"] - kpis["store_finalized"], "url": "/production/store-issuance"})
    if kpis.get("qc_waiting_orders"):
        action_items.append({"level": "warning", "title": "Orders waiting for QC", "value": kpis["qc_waiting_orders"], "url": "/qc"})
    if kpis.get("packing_pending"):
        action_items.append({"level": "warning", "title": "Orders pending trayline / packing", "value": kpis["packing_pending"], "url": "/packing"})
    if kpis.get("packed_orders"):
        action_items.append({"level": "info", "title": "Packed orders ready for dispatch", "value": kpis["packed_orders"], "url": "/dispatch"})
    if kpis.get("dispatch_pending"):
        action_items.append({"level": "info", "title": "Orders pending dispatch", "value": kpis["dispatch_pending"], "url": "/dispatch"})
    if not action_items:
        action_items.append({"level": "success", "title": "No urgent exceptions", "value": "OK", "url": "/dashboard"})

    return render(
        request,
        "dashboard/dashboard.html",
        {
            "username": request.session.get("username", "Guest"),
            "user_role": request.session.get("user_role", "UNKNOWN"),
            "user_id": request.session.get("user_id"),
            "page_title": "Executive Dashboard - ISFC PIMS",
            "app_name": "ISFC PIMS",
            "kpis": kpis,
            "order_status": order_status,
            "recipe_categories": recipe_categories,
            "recipe_customer_mix": recipe_customer_mix,
            "recent_orders": recent_orders,
            "top_bom_sections": top_bom_sections,
            "bom_category_cost": bom_category_cost,
            "store_status": store_status,
            "production_flow": production_flow,
            "action_items": action_items,
        },
    )


@router.get("/production", name="production_home")
async def production(request: Request):
   
    require_area(request, "dashboard")
    return RedirectResponse("/production/head-chef", status_code=303)
