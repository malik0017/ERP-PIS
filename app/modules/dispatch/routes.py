# app/modules/dispatch/routes.py
from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import text
from sqlalchemy.orm import Session
from starlette.status import HTTP_303_SEE_OTHER

from app.core.templates import render
from app.database.session import get_db
from app.models.production import CustomerOrder, PackingDispatch

router = APIRouter(prefix="/dispatch", tags=["Dispatch"])


def _parse_date(value: Optional[str]):
    try:
        return date.fromisoformat(value) if value else None
    except Exception:
        return None


def _redirect_with_error(url: str, message: str) -> RedirectResponse:
    sep = "&" if "?" in url else "?"
    return RedirectResponse(f"{url}{sep}error={message}", status_code=HTTP_303_SEE_OTHER)


@router.get("", response_class=HTMLResponse)
def dispatch_dashboard(request: Request, db: Session = Depends(get_db)):
    q = request.query_params
    search = (q.get("search") or "").strip()
    from_date = (q.get("from_date") or "").strip()
    to_date = (q.get("to_date") or "").strip()
    status_f = (q.get("status") or "").strip()
    query = db.query(PackingDispatch).filter(PackingDispatch.dispatch_status.in_(["Packed", "Out for Delivery", "Delivered"]))
    if status_f:
        query = query.filter(PackingDispatch.dispatch_status == status_f)
    if search:
        query = query.filter((PackingDispatch.order_no.like(f"%{search}%")) | (PackingDispatch.customer_name.like(f"%{search}%")))
    if from_date:
        query = query.filter(PackingDispatch.dispatch_date >= from_date)
    if to_date:
        query = query.filter(PackingDispatch.dispatch_date <= to_date)
    rows = query.order_by(PackingDispatch.id.desc()).limit(200).all()
    summary = {
        "pending": db.query(PackingDispatch).filter(PackingDispatch.dispatch_status.in_(["Packed", "Out for Delivery"])).count(),
        "delivered": db.query(PackingDispatch).filter(PackingDispatch.dispatch_status == "Delivered").count(),
        "rejected": db.query(PackingDispatch).filter(PackingDispatch.rejected_portions > 0).count(),
        "portions": db.execute(text("SELECT COALESCE(SUM(packed_portions),0) FROM packing_dispatch")).scalar() or 0,
    }
    return render(request, "dispatch/index.html", {"rows": rows, "summary": summary, "page_title": "Dispatch / Delivery",
                                                    "filters": {"search": search, "from_date": from_date, "to_date": to_date, "status": status_f},
                                                    "error": request.query_params.get("error")})


@router.post("/{dispatch_id}/update")
def update_dispatch(
    request: Request,
    dispatch_id: int,
    packed_portions: float = Form(0),
    rejected_portions: float = Form(0),
    dispatch_date: Optional[str] = Form(None),
    vehicle_no: str = Form(""),
    driver_name: str = Form(""),
    delivery_temperature_c: float = Form(0),
    dispatch_status: str = Form("Packed"),
    remarks: str = Form(""),
    db: Session = Depends(get_db),
):
    row = db.query(PackingDispatch).filter(PackingDispatch.id == dispatch_id).first()
    if not row:
        return _redirect_with_error("/dispatch", "Dispatch record not found.")
    row.packed_portions = packed_portions
    row.rejected_portions = rejected_portions
    row.dispatch_date = _parse_date(dispatch_date) or row.dispatch_date
    row.vehicle_no = vehicle_no or None
    row.driver_name = driver_name or None
    row.delivery_temperature_c = delivery_temperature_c or None
    row.dispatch_status = dispatch_status
    row.remarks = remarks or None

    order = db.query(CustomerOrder).filter(CustomerOrder.order_no == row.order_no).first()
    if order:
        if dispatch_status == "Delivered":
            order.status = "Dispatched"
        elif dispatch_status == "Out for Delivery":
            order.status = "Out for Delivery"
        elif dispatch_status == "Packed":
            order.status = "Packed"
        else:
            order.status = "Packing Pending"
    db.commit()

    # Batch 69: on delivery, auto-post COGS — Dr 5100 COGS / Cr 1130 Inventory,
    # valued at the order's estimated food cost. Idempotent per order.
    if order and dispatch_status == "Delivered":
        try:
            from app.core.gl_posting import post_dispatch_cogs_journal
            post_dispatch_cogs_journal(
                db, request, row.order_no,
                float(getattr(order, "total_estimated_food_cost", 0) or 0),
                customer=getattr(order, "customer_name", "") or "")
        except Exception:
            pass

    return RedirectResponse("/dispatch", status_code=HTTP_303_SEE_OTHER)
