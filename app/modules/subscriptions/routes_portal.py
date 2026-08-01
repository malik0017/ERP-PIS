# app/modules/subscriptions/routes_portal.py
# =============================================================================
# Batch 76 — Subscriptions: customer-portal self-service
# -----------------------------------------------------------------------------
# Mirrors the customer-resolution pattern used by app/modules/customer/routes.py
# (users.customer_code -> customers, with an admin ?customer= preview override)
# so a logged-in customer (e.g. Tasneem) can see and pause/resume ONLY their
# own subscriptions, under the existing /my prefix.
#
# Registered in main.py:
#     from app.modules.subscriptions.routes_portal import router as subscriptions_portal_router
#     app.include_router(subscriptions_portal_router)
# =============================================================================
from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.templates import render
from app.core.rbac import normalized_role, ADMIN_ROLES
from app.database.session import get_db
from app.modules.subscriptions.routes import (
    ensure_schema, _next_due_date, _rows, _one, _user,
)

router = APIRouter(prefix="/my/subscriptions", tags=["Customer Portal"])


def _resolve_customer(request: Request, db: Session) -> dict | None:
    role = normalized_role(request)
    override = (request.query_params.get("customer") or "").strip()
    if not override and role in ADMIN_ROLES:
        override = str(request.session.get("portal_customer_override") or "")
    if override and role in ADMIN_ROLES:
        row = db.execute(text("""
            SELECT customer_code, customer_name FROM customers
            WHERE customer_code = :v OR customer_name LIKE :like LIMIT 1
        """), {"v": override, "like": f"%{override}%"}).mappings().first()
        if row:
            return dict(row)

    username = request.session.get("username") or ""
    try:
        user_row = db.execute(text(
            "SELECT COALESCE(customer_code,'') AS customer_code, full_name FROM users WHERE username=:u LIMIT 1"
        ), {"u": username}).mappings().first()
    except Exception:
        user_row = None
    if user_row and user_row.get("customer_code"):
        row = db.execute(text(
            "SELECT customer_code, customer_name FROM customers WHERE customer_code=:c LIMIT 1"
        ), {"c": user_row["customer_code"]}).mappings().first()
        if row:
            return dict(row)
    if user_row and user_row.get("full_name"):
        row = db.execute(text(
            "SELECT customer_code, customer_name FROM customers WHERE customer_name=:n LIMIT 1"
        ), {"n": user_row["full_name"]}).mappings().first()
        if row:
            return dict(row)
    return None


@router.get("")
def my_subscriptions(request: Request, db: Session = Depends(get_db)):
    ensure_schema(db)
    customer = _resolve_customer(request, db)
    if not customer:
        return render(request, "subscriptions/portal_list.html", {
            "subs": [], "customer": None, "page_title": "My Subscriptions",
        })

    subs = _rows(db, """
        SELECT * FROM customer_subscriptions
        WHERE customer_name = :n ORDER BY FIELD(status,'Active','Paused','Cancelled'), created_at DESC
    """, {"n": customer["customer_name"]})
    for s in subs:
        s["lines"] = _rows(db, "SELECT recipe_name, portions FROM subscription_lines WHERE subscription_id=:i", {"i": s["id"]})
        s["next_due"] = _next_due_date(db, s) if s["status"] == "Active" else None
        s["orders_generated"] = int(_one(db, "SELECT COUNT(*) AS n FROM subscription_orders "
                                             "WHERE subscription_id=:i AND status='Generated'", {"i": s["id"]})["n"])

    return render(request, "subscriptions/portal_list.html", {
        "subs": subs, "customer": customer, "page_title": "My Subscriptions",
    })


@router.post("/{sub_id}/pause")
async def my_pause(request: Request, sub_id: int, db: Session = Depends(get_db)):
    ensure_schema(db)
    customer = _resolve_customer(request, db)
    sub = _one(db, "SELECT * FROM customer_subscriptions WHERE id=:i", {"i": sub_id})
    if not customer or not sub or sub["customer_name"] != customer["customer_name"]:
        return RedirectResponse("/my/subscriptions?toast=danger&title=Not+allowed&msg=Subscription not found", status_code=303)

    form = await request.form()
    reason = (form.get("pause_reason") or "Paused by customer").strip()

    warn = ""
    cutoff = date.today() + timedelta(days=2)
    upcoming = _one(db, """
        SELECT order_no, delivery_date FROM subscription_orders
        WHERE subscription_id=:i AND status='Generated' AND delivery_date <= :cut AND delivery_date >= CURDATE()
        ORDER BY delivery_date ASC LIMIT 1
    """, {"i": sub_id, "cut": cutoff})
    if upcoming:
        warn = f" Your delivery on {upcoming['delivery_date']} is inside the 48-hour cutoff and has already been prepared for production — please contact us directly if you need to change that one."

    db.execute(text("""
        UPDATE customer_subscriptions SET status='Paused', pause_reason=:r WHERE id=:i
    """), {"r": reason, "i": sub_id})
    db.commit()
    return RedirectResponse(f"/my/subscriptions?toast=warning&title=Paused&msg=Subscription paused.{warn}", status_code=303)


@router.post("/{sub_id}/resume")
async def my_resume(request: Request, sub_id: int, db: Session = Depends(get_db)):
    ensure_schema(db)
    customer = _resolve_customer(request, db)
    sub = _one(db, "SELECT * FROM customer_subscriptions WHERE id=:i", {"i": sub_id})
    if not customer or not sub or sub["customer_name"] != customer["customer_name"]:
        return RedirectResponse("/my/subscriptions?toast=danger&title=Not+allowed&msg=Subscription not found", status_code=303)
    db.execute(text("UPDATE customer_subscriptions SET status='Active', pause_reason=NULL WHERE id=:i"), {"i": sub_id})
    db.commit()
    return RedirectResponse("/my/subscriptions?toast=success&title=Resumed&msg=Subscription resumed", status_code=303)
