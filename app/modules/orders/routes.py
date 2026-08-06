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
    require_area(request, "order_portal")
    company_id = _company_id_from_session(request)
    context = _master_dropdown_context(db, company_id)
    context.update({"page_title": "Sale Requisitions"})
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
