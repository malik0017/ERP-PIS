# app/modules/customer/routes.py
from __future__ import annotations
import logging 
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
        
        role = normalized_role(request)
        pick_list = []
        statuses = []
        
        f = {
            "q": (request.query_params.get("q") or "").strip(),
            "status": (request.query_params.get("status") or "").strip(),
            "from_date": (request.query_params.get("from_date") or "").strip(),
            "to_date": (request.query_params.get("to_date") or "").strip(),
        }
        if role in ADMIN_ROLES:
            where = ["1=1"]
            params: dict = {}
            if f["status"]:
                where.append("o.status = :st"); params["st"] = f["status"]
            if f["from_date"]:
                where.append("COALESCE(o.required_delivery_date,'') >= :fd"); params["fd"] = f["from_date"]
            if f["to_date"]:
                where.append("COALESCE(o.required_delivery_date,'') <= :td"); params["td"] = f["to_date"]
            if f["q"]:
                where.append("(c.customer_name LIKE :like OR c.customer_code LIKE :like)")
                params["like"] = f"%{f['q']}%"
            W = " AND ".join(where)
            try:
                pick_list = [dict(r) for r in db.execute(text(f"""
                    SELECT c.customer_code, c.customer_name, COALESCE(c.brand,'') AS brand,
                           COUNT(o.id) AS order_count,
                           MAX(o.required_delivery_date) AS last_delivery,
                           ROUND(COALESCE(SUM(o.total_estimated_selling_value),0),2) AS total_value
                    FROM customers c
                    JOIN customer_orders o
                      ON (o.customer_name = c.customer_name OR o.customer_no = c.customer_code)
                    WHERE {W}
                    GROUP BY c.customer_code, c.customer_name, c.brand
                    HAVING order_count > 0
                    ORDER BY last_delivery DESC, c.customer_name ASC
                    LIMIT 1000
                """), params).mappings().all()]
            except Exception:
                pick_list = []
            statuses = [r["s"] for r in db.execute(text(
                "SELECT DISTINCT COALESCE(status,'') AS s FROM customer_orders ORDER BY 1"
            )).mappings().all() if r["s"]]
        return render(request, "customer/dashboard.html", {
            "customer": None, "orders": [], "kpis": {}, "status_mix": [],
            "page_title": "Customer Portal", "accents": STATUS_ACCENTS,
            "admin_pick_list": pick_list, "is_admin_preview": bool(role in ADMIN_ROLES),
            "pick_filters": f, "pick_statuses": statuses,
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
    if order_no.lower() == "new": 
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

    can_cancel_48h = True
    try:
        _ds = str(order["delivery_date"])[:10]
        if _ds:
            _y, _m, _d = (int(x) for x in _ds.split("-")[:3])
            _t = (str(order["delivery_time"]) or "00:00")[:5]
            _hh, _mm = (int(x) for x in _t.split(":")[:2]) if ":" in _t else (0, 0)
            can_cancel_48h = _dt(_y, _m, _d, _hh, _mm) >= _dt.now() + _td(hours=48)
    except Exception:
        can_cancel_48h = True

    return render(request, "customer/order_detail.html", {
        "customer": customer, "order": dict(order), "lines": lines,
        "timeline": timeline, "delivery": (delivery[0] if delivery else None),
        "accents": STATUS_ACCENTS, "can_cancel_48h": can_cancel_48h,
        "page_title": f"Order {order_no}",
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


from fastapi import Form
from typing import List, Optional
from datetime import datetime as _dt
from datetime import timedelta as _td  # Batch 70: 48-hour rule

from app.schemas.production import CustomerOrderCreate, OrderLineIn
from app.services.production_service import create_order as _svc_create_order


def _active_recipes_for(db: Session, customer: dict) -> list[dict]:
   
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
        # Batch 70: earliest selectable delivery date is 48 hours from now.
        "min_date": (_dt.now() + _td(hours=48)).strftime("%Y-%m-%d"),
        "min_datetime": (_dt.now() + _td(hours=48)).strftime("%Y-%m-%dT%H:%M"),
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

    _dd = _pd(required_delivery_date)
    if not _dd:
        return RedirectResponse("/my/orders/new?toast=danger&title=Delivery date required&msg=Please choose a delivery date", status_code=303)
    try:
        _t = (required_delivery_time or "00:00")[:5]
        _hh, _mm = (int(x) for x in _t.split(":")[:2])
    except Exception:
        _hh, _mm = 0, 0
    _deliv_dt = _dt(_dd.year, _dd.month, _dd.day, _hh, _mm)
    if _deliv_dt < _dt.now() + _td(hours=48):
        return RedirectResponse(
            "/my/orders/new?toast=danger&title=Too soon&msg=Orders must be placed at least 48 hours before delivery",
            status_code=303)

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
                                  created_by=f"{request.session.get('username','portal')} (customer portal)",
                                  company_id=int(request.session.get("company_id") or 1))
    except ValueError as exc:
        return RedirectResponse(f"/my/orders/new?toast=danger&title=Could not submit&msg={exc}", status_code=303)

    return RedirectResponse(f"/my/orders/{order.order_no}?toast=success&title=Order Submitted&msg=Order {order.order_no} received", status_code=303)

CUSTOMER_EDITABLE_STATUSES = ("Submitted",)


def _own_order(db: Session, customer: dict, order_no: str):
    return db.execute(text("""
        SELECT order_no, status,
               COALESCE(required_delivery_date,'') AS required_delivery_date,
               COALESCE(required_delivery_time,'') AS required_delivery_time
        FROM customer_orders
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

    _dd = order["required_delivery_date"]
    _tt = order["required_delivery_time"]
    if _dd:
        try:
            _ds = str(_dd)[:10]
            _y, _m, _d = (int(x) for x in _ds.split("-")[:3])
            _t = (str(_tt) or "00:00")[:5]
            _hh, _mm = (int(x) for x in _t.split(":")[:2]) if ":" in _t else (0, 0)
            _deliv = _dt(_y, _m, _d, _hh, _mm)
            if _deliv < _dt.now() + _td(hours=48):
                return RedirectResponse(
                    f"/my/orders/{order_no}?toast=warning&title=Cannot Cancel&msg=Delivery is less than 48 hours away, so this order can no longer be cancelled online. Please contact your account manager.",
                    status_code=303)
        except Exception:
            pass

    db.execute(text("UPDATE customer_orders SET status = 'Cancelled' WHERE order_no = :o"), {"o": order_no})
    db.commit()
    return RedirectResponse(f"/my/orders?toast=success&title=Order Cancelled&msg=Order {order_no} was cancelled", status_code=303)
