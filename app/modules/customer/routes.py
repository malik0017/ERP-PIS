# app/modules/customer/routes.py
"""Customer Portal — the customer's own view of the ERP.

A CUSTOMER-role user sees ONLY their own orders:
  /my            -> customer dashboard (KPIs + recent orders + status mix)
  /my/orders     -> full order history with filters

How the customer link works (resolution order):
  1. users.customer_code column (set it on the user in Users & Access)
  2. fallback: users.full_name matched against customers.customer_name
Admins / internal roles can pass ?customer=<code or name> to preview any portal.
"""
from __future__ import annotations

import logging  # Batch 23: log recipe-lookup issues instead of hiding them

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.templates import render
from app.core.rbac import normalized_role, ADMIN_ROLES
from app.database.session import get_db

router = APIRouter(prefix="/my", tags=["Customer Portal"])

STATUS_ACCENTS = {
    "Submitted": "warning", "Head Chef Approved": "info", "BOM Generated": "info",
    "Store Pending": "primary", "In Production": "primary", "Packed": "success",
    "Out for Delivery": "success", "Delivered": "success", "Closed": "secondary",
}


def _resolve_customer(request: Request, db: Session) -> dict | None:
    """Find which customer this user represents."""
    role = normalized_role(request)
    override = (request.query_params.get("customer") or "").strip()
    # Batch 21: keep the admin preview sticky across /my pages so the
    # New Order / My Orders / Statement buttons keep working without
    # re-passing ?customer= every time. ?customer=exit clears it.
    if override.lower() == "exit" and role in ADMIN_ROLES:
        request.session.pop("portal_customer_override", None)
        override = ""
    if not override and role in ADMIN_ROLES:
        override = str(request.session.get("portal_customer_override") or "")
    if override and role in ADMIN_ROLES:
        row = db.execute(text("""
            SELECT customer_code, customer_name, COALESCE(brand,'') AS brand
            FROM customers
            WHERE customer_code = :v OR customer_name LIKE :like
            LIMIT 1
        """), {"v": override, "like": f"%{override}%"}).mappings().first()
        if row:
            request.session["portal_customer_override"] = row["customer_code"]
            return dict(row)

    username = request.session.get("username") or ""
    user_row = None
    try:
        user_row = db.execute(text("""
            SELECT COALESCE(customer_code,'') AS customer_code, full_name, email
            FROM users WHERE username = :u LIMIT 1
        """), {"u": username}).mappings().first()
    except Exception:
        # customer_code column not migrated yet -> fall back to name match
        user_row = db.execute(text("""
            SELECT '' AS customer_code, full_name, email
            FROM users WHERE username = :u LIMIT 1
        """), {"u": username}).mappings().first()
    if not user_row:
        return None

    if user_row["customer_code"]:
        row = db.execute(text("""
            SELECT customer_code, customer_name, COALESCE(brand,'') AS brand
            FROM customers WHERE customer_code = :c LIMIT 1
        """), {"c": user_row["customer_code"]}).mappings().first()
        if row:
            return dict(row)

    row = db.execute(text("""
        SELECT customer_code, customer_name, COALESCE(brand,'') AS brand
        FROM customers WHERE customer_name = :n LIMIT 1
    """), {"n": user_row["full_name"]}).mappings().first()
    return dict(row) if row else None


def _orders_for(db: Session, customer: dict, status: str = "", search: str = "",
                from_date: str = "", to_date: str = "", limit: int = 200) -> list:
    extra, params = "", {
        "name": customer["customer_name"],
        "code": customer["customer_code"],
        "limit": limit,
    }
    if status:
        extra += " AND status = :status"; params["status"] = status
    if search:
        extra += " AND (order_no LIKE :like OR COALESCE(brand,'') LIKE :like)"; params["like"] = f"%{search}%"
    if from_date:
        extra += " AND COALESCE(required_delivery_date,'') >= :fd"; params["fd"] = from_date
    if to_date:
        extra += " AND COALESCE(required_delivery_date,'') <= :td"; params["td"] = to_date
    return db.execute(text(f"""
        SELECT order_no, order_date, COALESCE(brand,'') AS brand, COALESCE(channel,'') AS channel,
               COALESCE(required_delivery_date,'') AS delivery_date,
               COALESCE(required_delivery_time,'') AS delivery_time,
               COALESCE(total_planned_portions,0) AS portions,
               COALESCE(total_estimated_selling_value,0) AS sale_value,
               status
        FROM customer_orders
        WHERE (customer_name = :name OR customer_no = :code)
        {extra}
        ORDER BY id DESC
        LIMIT :limit
    """), params).mappings().all()


@router.get("")
async def customer_dashboard(request: Request, db: Session = Depends(get_db)):
    customer = _resolve_customer(request, db)
    if not customer:
        # Batch 21: admins are not linked to a customer — instead of a dead
        # end, show a customer picker so they can preview ANY portal via
        # /my?customer=<code>. Non-admin users still see the "ask your
        # administrator" message.
        role = normalized_role(request)
        pick_list = []
        if role in ADMIN_ROLES:
            pick_list = [dict(r) for r in db.execute(text("""
                SELECT customer_code, customer_name, COALESCE(brand,'') AS brand
                FROM customers ORDER BY customer_name LIMIT 1000
            """)).mappings().all()]
        return render(request, "customer/dashboard.html", {
            "customer": None, "orders": [], "kpis": {}, "status_mix": [],
            "page_title": "Customer Portal", "accents": STATUS_ACCENTS,
            "admin_pick_list": pick_list, "is_admin_preview": bool(pick_list),
        })

    orders = _orders_for(db, customer, limit=8)
    all_orders = _orders_for(db, customer, limit=1000)
    open_states = ("Submitted", "Head Chef Approved", "BOM Generated", "Store Pending", "In Production")
    kpis = {
        "total": len(all_orders),
        "open": sum(1 for o in all_orders if o["status"] in open_states),
        "delivered": sum(1 for o in all_orders if o["status"] in ("Delivered", "Closed")),
        "portions": round(sum(float(o["portions"] or 0) for o in all_orders), 1),
        "value": round(sum(float(o["sale_value"] or 0) for o in all_orders), 2),
    }
    mix: dict = {}
    for o in all_orders:
        mix[o["status"]] = mix.get(o["status"], 0) + 1
    status_mix = sorted(mix.items(), key=lambda kv: -kv[1])

    return render(request, "customer/dashboard.html", {
        "customer": customer, "orders": orders, "kpis": kpis,
        "status_mix": status_mix, "accents": STATUS_ACCENTS,
        "page_title": "Customer Portal",
        "preview_mode": bool(request.session.get("portal_customer_override"))
                        and normalized_role(request) in ADMIN_ROLES,
    })


@router.get("/orders")
async def customer_orders(request: Request, db: Session = Depends(get_db)):
    customer = _resolve_customer(request, db)
    if not customer:
        return RedirectResponse("/my", status_code=303)
    q = request.query_params
    filters = {
        "status": (q.get("status") or "").strip(),
        "search": (q.get("search") or "").strip(),
        "from_date": (q.get("from_date") or "").strip(),
        "to_date": (q.get("to_date") or "").strip(),
    }
    orders = _orders_for(db, customer, **filters)
    return render(request, "customer/orders.html", {
        "customer": customer, "orders": orders, "filters": filters,
        "accents": STATUS_ACCENTS,
        "status_options": list(STATUS_ACCENTS.keys()),
        "page_title": "My Orders",
    })


# ============================================================================
# Batch 12 — Customer Portal completion: order detail + account statement
# ============================================================================

_FLOW_ORDER = ["Submitted", "Head Chef Approved", "BOM Generated", "Store Pending",
               "In Production", "Packed", "Out for Delivery", "Delivered", "Closed"]


def _safe_rows(db: Session, sql: str, params: dict | None = None) -> list:
    try:
        return [dict(r) for r in db.execute(text(sql), params or {}).mappings().all()]
    except Exception:
        return []


@router.get("/orders/{order_no}")
async def customer_order_detail(request: Request, order_no: str, db: Session = Depends(get_db)):
    """Customer view of ONE order: header, recipe lines, status timeline, delivery doc.
    Strictly scoped: the order must belong to the resolved customer."""
    if order_no.lower() == "new":  # Batch 16: this route registered first; hand off to the form
        return await customer_order_new(request, db)
    customer = _resolve_customer(request, db)
    if not customer:
        return RedirectResponse("/my", status_code=303)

    order = db.execute(text("""
        SELECT order_no, order_date, COALESCE(brand,'') AS brand, COALESCE(channel,'') AS channel,
               COALESCE(required_delivery_date,'') AS delivery_date,
               COALESCE(required_delivery_time,'') AS delivery_time,
               COALESCE(total_planned_portions,0) AS portions,
               COALESCE(total_estimated_selling_value,0) AS sale_value,
               status, customer_name
        FROM customer_orders
        WHERE order_no = :o AND (customer_name = :name OR customer_no = :code)
        LIMIT 1
    """), {"o": order_no, "name": customer["customer_name"],
           "code": customer["customer_code"]}).mappings().first()
    if not order:
        return RedirectResponse("/my/orders", status_code=303)

    lines = _safe_rows(db, """
        SELECT COALESCE(recipe_name, recipe_no, '') AS recipe,
               COALESCE(required_portions, 0) AS portions,
               COALESCE(selling_price_per_portion, 0) AS unit_price,
               COALESCE(required_portions, 0) * COALESCE(selling_price_per_portion, 0) AS line_total
        FROM order_lines WHERE order_no = :o ORDER BY COALESCE(line_no, id)
    """, {"o": order_no})

    # Status timeline: done / active / pending against the standard flow.
    cur = str(order["status"] or "Submitted")
    try:
        cur_i = _FLOW_ORDER.index(cur)
    except ValueError:
        cur_i = 0
    timeline = [{"label": st, "state": ("done" if i < cur_i else "active" if i == cur_i else "pending")}
                for i, st in enumerate(_FLOW_ORDER)]

    delivery = _safe_rows(db, """
        SELECT COALESCE(vehicle_no,'') AS vehicle_no, COALESCE(driver_name,'') AS driver_name,
               COALESCE(dispatch_status,'') AS dispatch_status,
               COALESCE(delivery_temperature,'') AS delivery_temperature
        FROM packing_dispatch WHERE order_no = :o ORDER BY id DESC LIMIT 1
    """, {"o": order_no})

    return render(request, "customer/order_detail.html", {
        "customer": customer, "order": dict(order), "lines": lines,
        "timeline": timeline, "delivery": (delivery[0] if delivery else None),
        "accents": STATUS_ACCENTS, "page_title": f"Order {order_no}",
    })


@router.get("/statement")
async def customer_statement(request: Request, db: Session = Depends(get_db)):
    """Simple account statement: every order with value, plus AR invoices when
    the Finance module has issued them (safe fallback if ar_invoices is absent)."""
    customer = _resolve_customer(request, db)
    if not customer:
        return RedirectResponse("/my", status_code=303)

    q = request.query_params
    from_date = (q.get("from_date") or "").strip()
    to_date = (q.get("to_date") or "").strip()
    orders = _orders_for(db, customer, from_date=from_date, to_date=to_date, limit=1000)

    invoices = _safe_rows(db, """
        SELECT invoice_no, COALESCE(order_no,'') AS order_no,
               COALESCE(invoice_date,'') AS invoice_date,
               COALESCE(amount,0) AS total_amount,
               COALESCE(paid_amount,0) AS paid_amount,
               COALESCE(status,'Draft') AS status
        FROM ar_invoices
        WHERE customer_name = :name
        ORDER BY id DESC LIMIT 500
    """, {"name": customer["customer_name"]})

    totals = {
        "orders": len(orders),
        "order_value": round(sum(float(o["sale_value"] or 0) for o in orders), 2),
        "invoiced": round(sum(float(i["total_amount"] or 0) for i in invoices), 2),
        "paid": round(sum(float(i.get("paid_amount") or 0) for i in invoices), 2),
    }
    totals["outstanding"] = round(totals["invoiced"] - totals["paid"], 2)

    return render(request, "customer/statement.html", {
        "customer": customer, "orders": orders, "invoices": invoices, "totals": totals,
        "filters": {"from_date": from_date, "to_date": to_date},
        "accents": STATUS_ACCENTS, "page_title": "Account Statement",
    })


# ============================================================================
# Batch 16 — Customer self-service order creation (/my/orders/new)
# Locked to the resolved customer; reuses the SAME production order service
# as the internal portal, so BOM/planning flow picks these orders up normally.
# ============================================================================
from fastapi import Form
from typing import List, Optional
from datetime import datetime as _dt

from app.schemas.production import CustomerOrderCreate, OrderLineIn
from app.services.production_service import create_order as _svc_create_order


def _active_recipes_for(db: Session, customer: dict) -> list[dict]:
    """Recipes offered to this customer in the portal dropdown.

    Batch 23 FIX — the dropdown was coming back EMPTY. The old query required
    ALL of:
        status = 'ACTIVE'  AND  is_active = 1
        AND (customer_name = <this customer> OR customer_name = '')
    and swallowed every error with `except: pass`. Any of these made it empty:
      * recipes stored with status 'Active'/'active'/NULL (case / null variance)
      * is_active stored as NULL rather than 1
      * every recipe assigned to a DIFFERENT customer_name
      * customer_name padded with spaces so the equality never matched

    The query below is progressively relaxed in three tiers and returns the
    first tier that yields rows, so the customer always sees something sensible:
        Tier 1  this customer's recipes (or unassigned), not archived
        Tier 2  ANY non-archived recipe
        Tier 3  ANY recipe at all (last resort)
    """
    name = (customer or {}).get("customer_name") or ""

    tiers = [
        # Tier 1 — customer's own + unassigned, excluding explicitly inactive.
        ("""
            SELECT recipe_code, recipe_name,
                   COALESCE(sale_price_per_portion, 0) AS price
            FROM recipes
            WHERE UPPER(TRIM(COALESCE(status, 'ACTIVE'))) NOT IN ('INACTIVE','ARCHIVED','DELETED','DRAFT')
              AND COALESCE(is_active, 1) = 1
              AND (TRIM(COALESCE(customer_name, '')) = TRIM(:name)
                   OR TRIM(COALESCE(customer_name, '')) = '')
            ORDER BY CASE WHEN TRIM(COALESCE(customer_name,'')) = TRIM(:name) THEN 0 ELSE 1 END,
                     recipe_name
            LIMIT 500
        """, {"name": name}),
        # Tier 2 — any recipe that is not explicitly inactive/archived.
        ("""
            SELECT recipe_code, recipe_name,
                   COALESCE(sale_price_per_portion, 0) AS price
            FROM recipes
            WHERE UPPER(TRIM(COALESCE(status, 'ACTIVE'))) NOT IN ('INACTIVE','ARCHIVED','DELETED')
            ORDER BY recipe_name
            LIMIT 500
        """, {}),
        # Tier 3 — absolute fallback so the dropdown is never empty.
        ("""
            SELECT recipe_code, recipe_name,
                   COALESCE(sale_price_per_portion, 0) AS price
            FROM recipes
            ORDER BY recipe_name
            LIMIT 500
        """, {}),
    ]

    for sql, params in tiers:
        try:
            rows = db.execute(text(sql), params).mappings().all()
            if rows:
                return [dict(r) for r in rows]
        except Exception as exc:  # log instead of silently hiding
            logging.getLogger(__name__).warning(
                "portal recipe lookup tier failed: %s", exc)
    return []


@router.get("/orders/new")
async def customer_order_new(request: Request, db: Session = Depends(get_db)):
    customer = _resolve_customer(request, db)
    if not customer:
        return RedirectResponse("/my", status_code=303)
    return render(request, "customer/order_new.html", {
        "customer": customer,
        "recipes": _active_recipes_for(db, customer),
        "min_date": _dt.now().strftime("%Y-%m-%d"),
        "page_title": "New Order",
    })


@router.post("/orders/new")
async def customer_order_create(
    request: Request,
    required_delivery_date: Optional[str] = Form(None),
    required_delivery_time: Optional[str] = Form(None),
    channel: str = Form(""),
    notes: str = Form(""),
    recipe_no: List[str] = Form([]),
    required_portions: List[float] = Form([]),
    db: Session = Depends(get_db),
):
    customer = _resolve_customer(request, db)
    if not customer:
        return RedirectResponse("/my", status_code=303)

    lines: list[OrderLineIn] = []
    for idx, rcp in enumerate(recipe_no):
        rcp = (rcp or "").strip()
        portions = float(required_portions[idx]) if idx < len(required_portions) and required_portions[idx] else 0
        if not rcp or portions <= 0:
            continue
        rec = None
        try:
            # Batch 23 FIX: must use the SAME tolerance as the dropdown, or a
            # recipe the customer could select would be silently priced at 0.
            rec = db.execute(text("""
                SELECT recipe_name, COALESCE(sale_price_per_portion,0) AS price
                FROM recipes
                WHERE recipe_code = :c
                  AND UPPER(TRIM(COALESCE(status,'ACTIVE'))) NOT IN ('INACTIVE','ARCHIVED','DELETED')
                ORDER BY COALESCE(version,0) DESC, id DESC LIMIT 1
            """), {"c": rcp}).mappings().first()
        except Exception as exc:
            logging.getLogger(__name__).warning("recipe price lookup failed: %s", exc)
        lines.append(OrderLineIn(
            recipe_no=rcp,
            recipe_name=(rec["recipe_name"] if rec else rcp),
            required_portions=portions,
            selling_price_per_portion=float(rec["price"]) if rec else 0.0,
        ))

    if not lines:
        return RedirectResponse("/my/orders/new?toast=danger&title=Missing lines&msg=Add at least one recipe with portions", status_code=303)

    def _pd(v):
        try:
            return _dt.strptime(v, "%Y-%m-%d").date() if v else None
        except Exception:
            return None

    payload = CustomerOrderCreate(
        customer_no=customer.get("customer_code") or None,
        customer_name=customer["customer_name"],
        brand=(customer.get("brand") or None),
        channel=(channel or None),
        kitchen=None,
        required_delivery_date=_pd(required_delivery_date),
        required_delivery_time=required_delivery_time or None,
        cooking_date=None, cooking_time=None,
        material_receiving_date=None, material_receiving_time=None,
        notes=(notes or None),
        lines=lines,
    )
    try:
        order = _svc_create_order(db, payload,
                                  created_by=f"{request.session.get('username','portal')} (customer portal)")
    except ValueError as exc:
        return RedirectResponse(f"/my/orders/new?toast=danger&title=Could not submit&msg={exc}", status_code=303)

    return RedirectResponse(f"/my/orders/{order.order_no}?toast=success&title=Order Submitted&msg=Order {order.order_no} received", status_code=303)


# ============================================================================
# Batch 20 — Customer order lifecycle rules
#   * A customer can EDIT delivery date/time or CANCEL an order ONLY while it
#     is still 'Submitted' (i.e. before the Head Chef approves the plan).
#   * From 'Head Chef Approved' onwards the order is LOCKED for the customer —
#     production has started committing materials against it.
#   * Admin/internal users still manage everything via the Order Register.
# ============================================================================

CUSTOMER_EDITABLE_STATUSES = ("Submitted",)


def _own_order(db: Session, customer: dict, order_no: str):
    return db.execute(text("""
        SELECT order_no, status FROM customer_orders
        WHERE order_no = :o AND (customer_name = :name OR customer_no = :code)
        LIMIT 1
    """), {"o": order_no, "name": customer["customer_name"],
           "code": customer["customer_code"]}).mappings().first()


@router.post("/orders/{order_no}/update-delivery")
async def customer_update_delivery(
    request: Request,
    order_no: str,
    required_delivery_date: str = Form(""),
    required_delivery_time: str = Form(""),
    db: Session = Depends(get_db),
):
    customer = _resolve_customer(request, db)
    if not customer:
        return RedirectResponse("/my", status_code=303)
    order = _own_order(db, customer, order_no)
    if not order:
        return RedirectResponse("/my/orders", status_code=303)
    if (order["status"] or "") not in CUSTOMER_EDITABLE_STATUSES:
        return RedirectResponse(
            f"/my/orders/{order_no}?toast=warning&title=Order Locked&msg=Order is {order['status']} — delivery changes are no longer allowed. Contact your account manager.",
            status_code=303)
    if not required_delivery_date:
        return RedirectResponse(f"/my/orders/{order_no}?toast=danger&title=Missing date&msg=Choose a delivery date", status_code=303)
    db.execute(text("""
        UPDATE customer_orders
        SET required_delivery_date = :d, required_delivery_time = :t
        WHERE order_no = :o
    """), {"d": required_delivery_date, "t": required_delivery_time or None, "o": order_no})
    db.commit()
    return RedirectResponse(f"/my/orders/{order_no}?toast=success&title=Delivery Updated&msg=New delivery {required_delivery_date} {required_delivery_time}", status_code=303)


@router.post("/orders/{order_no}/cancel")
async def customer_cancel_order(request: Request, order_no: str, db: Session = Depends(get_db)):
    customer = _resolve_customer(request, db)
    if not customer:
        return RedirectResponse("/my", status_code=303)
    order = _own_order(db, customer, order_no)
    if not order:
        return RedirectResponse("/my/orders", status_code=303)
    if (order["status"] or "") not in CUSTOMER_EDITABLE_STATUSES:
        return RedirectResponse(
            f"/my/orders/{order_no}?toast=warning&title=Order Locked&msg=Order is {order['status']} and can no longer be cancelled online. Contact your account manager.",
            status_code=303)
    db.execute(text("UPDATE customer_orders SET status = 'Cancelled' WHERE order_no = :o"), {"o": order_no})
    db.commit()
    return RedirectResponse(f"/my/orders?toast=success&title=Order Cancelled&msg=Order {order_no} was cancelled", status_code=303)
