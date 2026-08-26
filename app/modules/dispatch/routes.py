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


@router.get("/logistics", response_class=HTMLResponse)
def logistics_report(request: Request, db: Session = Depends(get_db)):
    """Batch 129 — Logistics report: region-wise bag counts by customer
    (image 13). Groups packing_dispatch by region + customer, summing bags and
    portions. CSV export supported via ?export=csv."""
    require_area(request, "dispatch")
    _ensure_delivery_confirmation_schema(db)
    q = request.query_params
    from_date = (q.get("from_date") or "").strip()
    to_date = (q.get("to_date") or "").strip()
    where = "1=1"
    params: dict = {}
    if from_date:
        where += " AND dispatch_date >= :fd"; params["fd"] = from_date
    if to_date:
        where += " AND dispatch_date <= :td"; params["td"] = to_date
    rows = db.execute(text(f"""
        SELECT COALESCE(NULLIF(region,''),'Unassigned') AS region,
               COALESCE(customer_name,'—') AS customer_name,
               COUNT(*) AS orders,
               COALESCE(SUM(packed_bags),0) AS bags,
               COALESCE(SUM(packed_portions),0) AS portions
        FROM packing_dispatch
        WHERE {where}
        GROUP BY COALESCE(NULLIF(region,''),'Unassigned'), customer_name
        ORDER BY region, customer_name
    """), params).mappings().all()

    # group into region -> [customer rows], with region totals
    regions: dict = {}
    for r in rows:
        regions.setdefault(r["region"], {"rows": [], "bags": 0, "orders": 0, "portions": 0})
        g = regions[r["region"]]
        g["rows"].append(r)
        g["bags"] += int(r["bags"] or 0)
        g["orders"] += int(r["orders"] or 0)
        g["portions"] += float(r["portions"] or 0)

    if q.get("export") == "csv":
        import csv, io
        out = io.StringIO(); w = csv.writer(out)
        w.writerow(["Region", "Customer", "Orders", "Bags", "Portions"])
        for region, g in regions.items():
            for r in g["rows"]:
                w.writerow([region, r["customer_name"], r["orders"], r["bags"], f'{float(r["portions"]):.2f}'])
            w.writerow([f"{region} TOTAL", "", g["orders"], g["bags"], f'{g["portions"]:.2f}'])
        out.seek(0)
        from fastapi.responses import StreamingResponse
        return StreamingResponse(iter(['\ufeff' + out.getvalue()]), media_type="text/csv",
                                 headers={"Content-Disposition": "attachment; filename=logistics_report.csv"})

    grand = {"bags": sum(g["bags"] for g in regions.values()),
             "orders": sum(g["orders"] for g in regions.values()),
             "portions": sum(g["portions"] for g in regions.values())}
    return render(request, "dispatch/logistics.html",
                  {"regions": regions, "grand": grand,
                   "filters": {"from_date": from_date, "to_date": to_date},
                   "page_title": "Logistics Report"})


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
    region: str = Form(""),
    packed_bags: str = Form(""),
    delivery_otp_input: str = Form(""),
    pod_photo: Optional[UploadFile] = File(None),
    db: Session = Depends(get_db),
):
    require_action(request, "dispatch", "edit")
    _ensure_delivery_confirmation_schema(db)
    row = db.query(PackingDispatch).filter(PackingDispatch.id == dispatch_id).first()
    if not row:
        return _redirect_with_error("/dispatch", "Dispatch record not found.")

    # Batch 129: region + editable bag count. Persisted regardless of delivery
    # status transition (they're logistics attributes, not proof-of-delivery).
    _region = (region or "").strip()
    if _region:
        row.region = _region
    if (packed_bags or "").strip():
        try:
            row.packed_bags = int(float(packed_bags))
        except (TypeError, ValueError):
            pass

    # ------------------------------------------------------------------
    # Batch 100 — DELIVERED IS FINAL. Lock the record once delivery closes.
    #
    # Before this, a Delivered dispatch stayed fully editable: the packed
    # quantity, the driver, the vehicle, even the status could be changed
    # back afterwards. That breaks the whole point of the Batch 80
    # proof-of-delivery gate — you can satisfy it with a photo, save, and
    # then quietly rewrite the numbers the proof was attached to. It also
    # makes the delivery note and the AR invoice raised from it
    # unreconcilable with the record they came from.
    #
    # A delivered dispatch is a closed financial and legal document. If
    # something genuinely needs correcting, that is a credit note or a
    # returns process, not a silent edit — and neither exists yet, so the
    # honest behaviour is to refuse rather than to pretend.
    #
    # Deliberately checked BEFORE the proof gate below: a locked record must
    # not even reach the validation that could change it.
    # ------------------------------------------------------------------
    if (row.dispatch_status or "") == "Delivered":
        return _redirect_with_error(
            "/dispatch",
            f"{row.order_no} is already Delivered and is locked. "
            "A completed delivery cannot be edited — raise a customer complaint "
            "or a credit note if something needs correcting.")

    # Batch 80: proof-of-delivery gate. A delivery can't be marked Delivered
    # on trust alone anymore — the driver needs to provide EITHER a photo
    # (uploaded here) OR the OTP code the customer read back to them
    # (generated via "Generate Delivery Code" and confirmed against what's
    # stored). Neither present/matching -> the status change is rejected
    # and everything else on the form is left exactly as it was.
    # Batch 129: the delivery-code / photo proof-of-delivery UI was removed at
    # the client's request, so "Delivered" no longer requires proof here. The
    # record is still locked once Delivered (checked above), preserving the
    # "no silent edits after delivery" guarantee. If proof-of-delivery is
    # reinstated later, restore the gate that previously lived here.
    if dispatch_status == "Delivered":
        db.execute(text("UPDATE packing_dispatch SET delivery_confirmed_by='Manual' WHERE id=:i"),
                   {"i": dispatch_id})
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
