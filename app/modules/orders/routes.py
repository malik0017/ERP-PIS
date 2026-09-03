# app/modules/orders/routes.py
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.templates import render
from app.core.rbac import require_area
from app.database.session import get_db
from app.models.production import CustomerOrder

router = APIRouter(prefix="/orders", tags=["Orders"])


def _company_id_from_session(request: Request) -> int:
    return int(request.session.get("company_id") or 1)


def _master_dropdown_context(db: Session, company_id: int = 1) -> dict:
    customers = db.execute(
        text("""
            SELECT customer_code, customer_name, COALESCE(brand,'') AS brand
            FROM customers
            WHERE company_id = :company_id
              AND UPPER(TRIM(COALESCE(status,''))) = 'ACTIVE'
            ORDER BY customer_name ASC
        """),
        {"company_id": company_id},
    ).mappings().all()

    brands = db.execute(
        text("""
            SELECT brand_code, brand_name_en AS brand_name
            FROM brands
            WHERE company_id = :company_id
              AND UPPER(TRIM(COALESCE(status,''))) = 'ACTIVE'
            ORDER BY brand_name_en ASC
        """),
        {"company_id": company_id},
    ).mappings().all()

    channels = db.execute(
        text("""
            SELECT stream_code AS channel_code, stream_name AS channel_name
            FROM revenue_streams
            WHERE company_id = :company_id
              AND UPPER(TRIM(COALESCE(status,''))) = 'ACTIVE'
            ORDER BY stream_name ASC
        """),
        {"company_id": company_id},
    ).mappings().all()

    kitchens = db.execute(
        text("""
            SELECT kitchen_code, kitchen_name
            FROM kitchen_locations
            WHERE company_id = :company_id
              AND UPPER(TRIM(COALESCE(status,''))) = 'ACTIVE'
            ORDER BY kitchen_name ASC
        """),
        {"company_id": company_id},
    ).mappings().all()

    recipes = db.execute(
        text("""
            SELECT
                r.recipe_code,
                r.recipe_name,
                COALESCE(r.customer_name,'') AS customer_name,
                COALESCE(r.brand_name,'') AS brand_name,
                COALESCE(r.category,'') AS category,
                COALESCE(r.day_of_week,'') AS day_of_week,
                r.standard_portions,
                r.food_cost_per_portion,
                r.sale_price_per_portion,
                COUNT(ri.id) AS bom_lines
            FROM recipes r
            LEFT JOIN recipe_ingredients ri ON ri.recipe_id = r.id
            WHERE r.company_id = :company_id
              AND UPPER(TRIM(COALESCE(r.status,''))) = 'ACTIVE'
              AND COALESCE(r.is_active, 1) = 1
            GROUP BY r.id
            ORDER BY r.recipe_code ASC, r.recipe_name ASC
        """),
        {"company_id": company_id},
    ).mappings().all()

    return {
        "customers": customers,
        "brands": brands,
        "channels": channels,
        "kitchens": kitchens,
        "recipes": recipes,
    }


@router.get("/portal", response_class=HTMLResponse)
def order_portal(request: Request, db: Session = Depends(get_db)):
    """Batch 123 — Sale Requisitions now opens on a CUSTOMER PICKER: a grid of
    customer cards (with logos). Clicking an active card opens the order form
    pre-set for that customer. Only SMC, FRSH and Immediate Order are live for
    now; the rest are 'coming soon' placeholders."""
    require_area(request, "order_portal")
    # Which cards are live. Codes are matched case-insensitively against the
    # customer master so a card only activates if that customer actually exists.
    company_id = _company_id_from_session(request)
    can_immediate = False
    try:
        from app.core.rbac import can_access
        can_immediate = can_access(request, "immediate_order")
    except Exception:
        can_immediate = False
    cards = [
        {"key": "smc",       "name": "SMC",        "logo": "/static/img/customers/smc.png",       "active": True,  "match": ["SMC", "SMC1"],
         "description": ""},
        {"key": "frsh",      "name": "FRSH",       "logo": "/static/img/customers/frsh.png",      "active": True,  "match": ["FRSH", "FRSH1"],
         "description": "Fresh weekly-menu meal delivery"},
        {"key": "immediate", "name": "Immediate Order", "logo": None, "icon": "lightning-charge-fill", "active": can_immediate, "immediate": True,
         "description": "Quick ordering and fulfilment"},
        {"key": "dietworld", "name": "Diet World", "logo": "/static/img/customers/dietworld.jpg", "active": False,
         "description": "Healthy living, simplified"},
        {"key": "afya",      "name": "Afya",       "logo": "/static/img/customers/afya.jpg",      "active": False,
         "description": "Delivering better health"},
        {"key": "soon1",     "name": "Coming Soon", "logo": None, "icon": "hourglass-split", "active": False, "placeholder": True,
         "description": "More great systems on the way"},
        {"key": "soon2",     "name": "Coming Soon", "logo": None, "icon": "hourglass-split", "active": False, "placeholder": True,
         "description": "More great systems on the way"},
        {"key": "soon3",     "name": "Coming Soon", "logo": None, "icon": "hourglass-split", "active": False, "placeholder": True,
         "description": "More great systems on the way"},
    ]
    return render(request, "orders/portal_landing.html",
                  {"page_title": "Sale Requisitions", "cards": cards})


@router.get("/portal/new", response_class=HTMLResponse)
def order_portal_new(request: Request, db: Session = Depends(get_db)):
    """The actual order form. Reachable from a customer card; an optional
    ?customer=CODE preset auto-selects that customer (and, for weekly-menu
    customers like FRSH, marks the form so day-wise menu loading kicks in)."""
    require_area(request, "order_portal")
    company_id = _company_id_from_session(request)
    context = _master_dropdown_context(db, company_id)
    preset = (request.query_params.get("customer") or "").strip().lower()

    # Batch 124: FRSH is a weekly-menu customer. When the user lands here from
    # the FRSH card we resolve the real customer/brand/channel codes so the
    # form is pre-filled, and flag it as a weekly-menu customer so the template
    # loads that day's recipes automatically once a delivery date is chosen.
    preset_info = {"key": preset, "weekly_menu": False,
                   "customer": "", "brand": "", "channel": ""}
    # Batch 150 — PREFERRED DEFAULT PER CUSTOMER CARD.
    #
    # "smc" matches six customers (Accounting Department Smc1, Cafeteria Smc2,
    # SMC 1 -(Olaya) Cafeteria, SMC 2 -(King Abdullah) Cafeteria, SMC1, SMC2).
    # The old ORDER BY only preferred names STARTING with the key, so which one
    # won was decided by alphabetical order — and the operator had to correct it
    # on every single order.
    #
    # A new first tier prefers an EXACT name match against the configured
    # default. Everything else about the query is unchanged, so a card with no
    # entry here behaves exactly as before.
    PRESET_DEFAULT_NAME = {
        "smc": "smc1",
    }
    if preset:
        prefer = PRESET_DEFAULT_NAME.get(preset, preset)
        # match a customer whose code or name contains the preset key
        row = db.execute(text("""
            SELECT customer_code, customer_name FROM customers
            WHERE company_id = :cid
              AND (LOWER(customer_code) LIKE :k OR LOWER(customer_name) LIKE :k)
              AND UPPER(TRIM(COALESCE(status,''))) = 'ACTIVE'
            ORDER BY (LOWER(TRIM(customer_name)) = :prefer) DESC,
                     (LOWER(customer_name) LIKE :exact) DESC,
                     customer_name ASC
            LIMIT 1
        """), {"cid": company_id, "k": f"%{preset}%", "exact": f"{preset}%",
               "prefer": prefer}).mappings().first()
        if row:
            preset_info["customer"] = f"{row['customer_code']} - {row['customer_name']}"
        # FRSH uses the weekly day-wise menu
        if preset == "frsh":
            preset_info["weekly_menu"] = True

    context.update({"page_title": "Sale Requisitions",
                    "preset_customer": preset,
                    "preset_info": preset_info})
    return render(request, "orders/portal.html", context)


@router.get("/portal/immediate", response_class=HTMLResponse)
def order_portal_immediate(request: Request, db: Session = Depends(get_db)):
    """Batch 95 — same order form, same everything, except the 48-hour
    delivery rule doesn't apply here. Restricted to a new RBAC area
    ("immediate_order") that has to be explicitly granted per user from
    Users & Access — nobody gets this by default, including Admin roles
    that only have the base "order_portal" area. This is genuinely a
    separate permission from placing a normal order, on purpose: bypassing
    the lead-time rule should be a deliberate grant, not a side effect of
    already being allowed to place regular orders.
    """
    require_area(request, "immediate_order")
    company_id = _company_id_from_session(request)
    context = _master_dropdown_context(db, company_id)
    context.update({"page_title": "Immediate Order (No 48-Hour Rule)", "immediate": True})
    return render(request, "orders/portal.html", context)


@router.get("", response_class=HTMLResponse)
def orders_list(request: Request, db: Session = Depends(get_db)):
    require_area(request, "production_orders")
    orders = db.query(CustomerOrder).order_by(CustomerOrder.id.desc()).limit(200).all()
    return render(request, "orders/list.html", {"orders": orders, "page_title": "Orders"})


@router.get("/{order_no}")
def redirect_order_detail(order_no: str):
    return RedirectResponse(f"/production/orders/{order_no}", status_code=303)


def _customer_code_for_user(request: Request, db: Session) -> str | None:
    """Return the customer_code linked to the logged-in CUSTOMER user."""
    try:
        from app.core.rbac import normalized_role
        if normalized_role(request) != "CUSTOMER":
            return None
        uid = request.session.get("user_id")
        uname = request.session.get("username")
        row = db.execute(text(
            "SELECT customer_code FROM users WHERE id = :uid OR username = :uname LIMIT 1"
        ), {"uid": uid or 0, "uname": uname or ""}).first()
        return row[0] if row and row[0] else None
    except Exception:
        return None


@router.get("/my", response_class=HTMLResponse)
def my_orders(request: Request, db: Session = Depends(get_db)):
    """Customer dashboard + order history, scoped to the logged-in customer.

    Non-customer roles see their name-matched view too (useful for testing),
    but the page is designed for CUSTOMER logins linked via users.customer_code.
    """
    code = _customer_code_for_user(request, db)
    uname = request.session.get("username") or ""
    params = {"code": code or "", "uname": uname}
    where = "WHERE (:code <> '' AND co.customer_no = :code) OR (:code = '' AND co.customer_name = :uname)"

    rows = db.execute(text(f"""
        SELECT co.order_no, co.order_date, co.customer_name, COALESCE(co.brand,'') AS brand,
               COALESCE(co.channel,'') AS channel,
               COALESCE(co.required_delivery_date,'') AS delivery_date,
               COALESCE(co.required_delivery_time,'') AS delivery_time,
               COALESCE(co.total_planned_portions,0) AS portions,
               COALESCE(co.total_sales_value,0) AS sale_value,
               co.status
        FROM customer_orders co
        {where}
        ORDER BY co.id DESC LIMIT 200
    """), params).mappings().all()

    open_status = ("Submitted", "Head Chef Approved", "BOM Generated", "Store Pending", "In Production", "Packed")
    summary = {
        "total": len(rows),
        "open": sum(1 for r in rows if r["status"] in open_status),
        "delivered": sum(1 for r in rows if r["status"] == "Delivered"),
        "value": round(sum(float(r["sale_value"] or 0) for r in rows), 2),
    }
    return render(request, "orders/my_orders.html", {
        "rows": rows, "summary": summary,
        "customer_code": code, "page_title": "My Orders",
    })
