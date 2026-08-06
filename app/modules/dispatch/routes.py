# app/modules/dispatch/routes.py
import os
import secrets
from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, Depends, Form, Request, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import text
from sqlalchemy.orm import Session
from starlette.status import HTTP_303_SEE_OTHER

from app.core.templates import render
from app.core.rbac import require_area, require_action
from app.database.session import get_db
from app.models.production import CustomerOrder, PackingDispatch

router = APIRouter(prefix="/dispatch", tags=["Dispatch"])

_POD_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "static", "uploads", "delivery_proof")


def _parse_date(value: Optional[str]):
    try:
        return date.fromisoformat(value) if value else None
    except Exception:
        return None


def _redirect_with_error(url: str, message: str) -> RedirectResponse:
    sep = "&" if "?" in url else "?"
    return RedirectResponse(f"{url}{sep}error={message}", status_code=HTTP_303_SEE_OTHER)


def _column_exists(db: Session, table: str, column: str) -> bool:
    return bool(db.execute(text("""
        SELECT COUNT(*) FROM information_schema.columns
        WHERE table_schema = DATABASE() AND table_name = :table AND column_name = :column
    """), {"table": table, "column": column}).scalar())


def _ensure_delivery_confirmation_schema(db: Session) -> None:
    """Batch 80 — proof-of-delivery columns on packing_dispatch, added
    defensively (MySQL 8 on this project's version doesn't reliably support
    ADD COLUMN IF NOT EXISTS, and phpMyAdmin has bitten this project on
    similar syntax before), so this checks information_schema first."""
    cols = {
        "delivery_otp": "VARCHAR(10) NULL",
        "delivery_otp_generated_at": "DATETIME NULL",
        "delivery_confirmed_by": "VARCHAR(20) NULL",
        "pod_photo_path": "VARCHAR(300) NULL",
    }
    for col, ddl in cols.items():
        if not _column_exists(db, "packing_dispatch", col):
            try:
                db.execute(text(f"ALTER TABLE packing_dispatch ADD COLUMN {col} {ddl}"))
                db.commit()
            except Exception:
                db.rollback()


@router.get("", response_class=HTMLResponse)
def dispatch_dashboard(request: Request, db: Session = Depends(get_db)):
    require_area(request, "dispatch")
    _ensure_delivery_confirmation_schema(db)
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


@router.post("/{dispatch_id}/generate-otp")
def generate_delivery_otp(request: Request, dispatch_id: int, db: Session = Depends(get_db)):
    """Batch 80 — generates a one-time code for delivery confirmation.

    This project has no SMS/email gateway configured yet (confirmed absent
    in the last security/architecture review), so this can't text the
    customer directly today. The intended flow: dispatch staff calls the
    customer, reads out this code, the customer reads it back to the
    driver at the door, and the driver enters it here to confirm delivery.
    Once SMS/WhatsApp integration exists, this same field is what a real
    "text the customer" step would populate automatically — no workflow
    change needed on this end, just wiring in the sender.
    """
    require_action(request, "dispatch", "edit")
    _ensure_delivery_confirmation_schema(db)
    row = db.query(PackingDispatch).filter(PackingDispatch.id == dispatch_id).first()
    if not row:
        return _redirect_with_error("/dispatch", "Dispatch record not found.")
    otp = f"{secrets.randbelow(10000):04d}"
    db.execute(text("UPDATE packing_dispatch SET delivery_otp=:o, delivery_otp_generated_at=:t WHERE id=:i"),
              {"o": otp, "t": datetime.utcnow(), "i": dispatch_id})
    db.commit()
    return RedirectResponse(
        f"/dispatch?toast=success&title=Delivery+Code+Generated&msg=Code {otp} for {row.order_no} — share it with the customer by phone, then have the driver enter it to confirm delivery.",
        status_code=HTTP_303_SEE_OTHER)


@router.post("/{dispatch_id}/update")
async def update_dispatch(
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
    delivery_otp_input: str = Form(""),
    pod_photo: Optional[UploadFile] = File(None),
    db: Session = Depends(get_db),
):
    require_action(request, "dispatch", "edit")
    _ensure_delivery_confirmation_schema(db)
    row = db.query(PackingDispatch).filter(PackingDispatch.id == dispatch_id).first()
    if not row:
        return _redirect_with_error("/dispatch", "Dispatch record not found.")

    # Batch 80: proof-of-delivery gate. A delivery can't be marked Delivered
    # on trust alone anymore — the driver needs to provide EITHER a photo
    # (uploaded here) OR the OTP code the customer read back to them
    # (generated via "Generate Delivery Code" and confirmed against what's
    # stored). Neither present/matching -> the status change is rejected
    # and everything else on the form is left exactly as it was.
    if dispatch_status == "Delivered":
        photo_ok = False
        stored = db.execute(text("SELECT delivery_otp, pod_photo_path FROM packing_dispatch WHERE id=:i"),
                            {"i": dispatch_id}).mappings().first() or {}
        otp_ok = bool(delivery_otp_input) and delivery_otp_input.strip() == (stored.get("delivery_otp") or "")

        if pod_photo is not None and pod_photo.filename:
            ext = (pod_photo.filename or "").rsplit(".", 1)[-1].lower()
            if ext not in {"jpg", "jpeg", "png", "webp", "heic"}:
                return _redirect_with_error("/dispatch", "Delivery photo must be JPG/PNG/WEBP.")
            data = await pod_photo.read()
            if len(data) > 5 * 1024 * 1024:
                return _redirect_with_error("/dispatch", "Delivery photo must be under 5MB.")
            os.makedirs(_POD_DIR, exist_ok=True)
            fname = f"dispatch_{dispatch_id}_{int(datetime.utcnow().timestamp())}.{ext}"
            with open(os.path.join(_POD_DIR, fname), "wb") as fh:
                fh.write(data)
            db.execute(text("UPDATE packing_dispatch SET pod_photo_path=:p, delivery_confirmed_by='Photo' WHERE id=:i"),
                      {"p": f"/static/uploads/delivery_proof/{fname}", "i": dispatch_id})
            photo_ok = True
        elif otp_ok:
            db.execute(text("UPDATE packing_dispatch SET delivery_confirmed_by='OTP' WHERE id=:i"), {"i": dispatch_id})

        if not photo_ok and not otp_ok:
            return _redirect_with_error(
                "/dispatch",
                f"Cannot mark {row.order_no} as Delivered without proof: upload a delivery photo, "
                f"or generate a delivery code and enter the one the customer confirmed.")
        db.commit()

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
