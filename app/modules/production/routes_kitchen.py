# app/modules/production/routes_kitchen.py
# =============================================================================
# Batch 72 — Kitchen production routes (P1 state machine)
# -----------------------------------------------------------------------------
# Adds the product-level workstation on top of the existing ingredient screens:
#
#   GET  /production/kitchen/{section}/{order_no}    -> recipe cards + state
#   POST /production/kitchen/{section}/{order_no}/receive-all
#   POST /production/kitchen/{section}/{order_no}/produce   (one recipe)
#   POST /production/kitchen/{section}/{order_no}/transfer  (one recipe)
#
# Registered in main.py:
#     from app.modules.production.routes_kitchen import router as kitchen_prod_router
#     app.include_router(kitchen_prod_router)
# =============================================================================

from datetime import datetime
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import text
from sqlalchemy.orm import Session
from starlette.status import HTTP_303_SEE_OTHER

from app.core.templates import render
from app.core.rbac import require_area, require_action
from app.database.session import get_db
from app.core import kitchen_production as kp

router = APIRouter(prefix="/production/kitchen", tags=["Kitchen"])

# reuse the slug helpers from the main production routes
from app.modules.production.routes import _section_from_slug, _section_slug, current_user_name


def _order_head(db, order_no):
    try:
        return db.execute(text("""
            SELECT order_no, COALESCE(customer_name,'') AS customer_name,
                   COALESCE(brand,'') AS brand,
                   COALESCE(required_delivery_date,'') AS delivery_date,
                   COALESCE(required_delivery_time,'') AS delivery_time
            FROM customer_orders WHERE order_no=:o
        """), {"o": order_no}).mappings().first()
    except Exception:
        return None


@router.get("/{section_name}/{order_no}")
async def kitchen_order(request: Request, section_name: str, order_no: str, db: Session = Depends(get_db)):
    require_area(request, "kitchen")
    section = _section_from_slug(section_name)
    states = kp.recipe_states(db, order_no, section)
    recipes = list(states.values())

    summary = {
        "total": len(recipes),
        "received": sum(1 for r in recipes if r["status"] in (kp.RECEIVED, kp.PRODUCED, kp.TRANSFERRED)),
        "produced": sum(1 for r in recipes if r["status"] in (kp.PRODUCED, kp.TRANSFERRED)),
        "transferred": sum(1 for r in recipes if r["status"] == kp.TRANSFERRED),
    }

    return render(request, "production/kitchen_order.html", {
        "section": section, "section_slug": _section_slug(section),
        "order_no": order_no, "order": _order_head(db, order_no),
        "recipes": recipes, "summary": summary,
        "next_default": kp.next_section(section),
        "route_options": kp.SECTION_ROUTE,
        "page_title": f"{section} Production — {order_no}",
    })


@router.post("/{section_name}/{order_no}/receive-all")
async def kitchen_receive_all(request: Request, section_name: str, order_no: str, db: Session = Depends(get_db)):
    require_action(request, "kitchen", "edit")
    section = _section_from_slug(section_name)
    user = current_user_name(request)
    # receive every not-yet-received ingredient line at full issued qty
    db.execute(text("""
        UPDATE kitchen_section_transactions
        SET received_qty_standard = COALESCE(NULLIF(received_qty_standard,0), issued_qty_standard, 0),
            balance_qty_standard  = COALESCE(NULLIF(received_qty_standard,0), issued_qty_standard, 0),
            received_by = :u, received_at = COALESCE(received_at, :now),
            transaction_status = CASE WHEN transaction_status IN ('Produced','Transferred')
                                      THEN transaction_status ELSE 'Received' END,
            updated_at = :now
        WHERE order_no=:o AND current_section=:s
          AND COALESCE(received_qty_standard,0) <= 0
    """), {"u": user, "now": datetime.utcnow(), "o": order_no, "s": section})
    db.commit()
    return RedirectResponse(
        f"/production/kitchen/{_section_slug(section)}/{order_no}?toast=success&title=Received&msg=All pending ingredients received",
        status_code=HTTP_303_SEE_OTHER)


@router.post("/{section_name}/{order_no}/produce")
async def kitchen_produce(request: Request, section_name: str, order_no: str,
                          recipe_no: str = Form(""),
                          produced_portions: float = Form(0),
                          waste_portions: float = Form(0),
                          remarks: str = Form(""),
                          db: Session = Depends(get_db)):
    require_action(request, "kitchen", "edit")
    section = _section_from_slug(section_name)
    # UI fix: portions are physical, countable units — never fractional.
    # Round server-side too, since a client without JS could still post decimals.
    produced_whole = max(0, round(float(produced_portions or 0)))
    waste_whole = max(0, round(float(waste_portions or 0)))
    # Batch 100: a locked section returns a warning toast rather than a 500.
    # SectionLocked carries a message written for the person on the line, so
    # it is surfaced verbatim instead of being replaced with a generic error.
    try:
        kp.produce_final_product(db, order_no, section, recipe_no,
                                 float(produced_whole), float(waste_whole),
                                 current_user_name(request), remarks)
    except kp.SectionLocked as exc:
        return RedirectResponse(
            f"/production/kitchen/{_section_slug(section)}/{order_no}"
            f"?toast=warning&title={quote('Section Locked')}&msg={quote(str(exc))}",
            status_code=HTTP_303_SEE_OTHER)
    return RedirectResponse(
        f"/production/kitchen/{_section_slug(section)}/{order_no}?toast=success&title=Produced&msg=Final product recorded",
        status_code=HTTP_303_SEE_OTHER)


@router.post("/{section_name}/{order_no}/transfer")
async def kitchen_transfer(request: Request, section_name: str, order_no: str,
                           recipe_no: str = Form(""),
                           to_section: str = Form(""),
                           db: Session = Depends(get_db)):
    require_action(request, "kitchen", "edit")
    section = _section_from_slug(section_name)
    try:
        kp.transfer_product(db, order_no, section, recipe_no, to_section, current_user_name(request))
    except kp.SectionLocked as exc:
        return RedirectResponse(
            f"/production/kitchen/{_section_slug(section)}/{order_no}"
            f"?toast=warning&title={quote('Cannot Transfer')}&msg={quote(str(exc))}",
            status_code=HTTP_303_SEE_OTHER)
    return RedirectResponse(
        f"/production/kitchen/{_section_slug(section)}/{order_no}?toast=success&title=Transferred&msg=Product moved to {to_section or kp.next_section(section)}",
        status_code=HTTP_303_SEE_OTHER)
