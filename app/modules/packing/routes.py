# app/modules/packing/routes.py
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import text
from sqlalchemy.orm import Session
from starlette.status import HTTP_303_SEE_OTHER

from app.core.templates import render
from app.core.rbac import require_area, require_action
from app.database.session import get_db
from app.models.production import CustomerOrder, PackingDispatch

router = APIRouter(prefix="/packing", tags=["Trayline / Packing"])


def _parse_date(value: Optional[str]):
    try:
        return date.fromisoformat(value) if value else None
    except Exception:
        return None


def _redirect_with_error(url: str, message: str) -> RedirectResponse:
    sep = "&" if "?" in url else "?"
    return RedirectResponse(f"{url}{sep}error={message}", status_code=HTTP_303_SEE_OTHER)


@router.get("", response_class=HTMLResponse)
def packing_dashboard(request: Request, db: Session = Depends(get_db)):
    require_area(request, "packing")
    q = request.query_params
    search = (q.get("search") or "").strip()
    from_date = (q.get("from_date") or "").strip()
    to_date = (q.get("to_date") or "").strip()
    status_f = (q.get("status") or "").strip()
    extra = ""
    params = {}
    if search:
        extra += " AND (pd.order_no LIKE :search OR COALESCE(pd.customer_name,'') LIKE :search OR COALESCE(co.brand,'') LIKE :search)"
        params["search"] = f"%{search}%"
    if from_date:
        extra += " AND COALESCE(co.required_delivery_date,'') >= :from_date"
        params["from_date"] = from_date
    if to_date:
        extra += " AND COALESCE(co.required_delivery_date,'') <= :to_date"
        params["to_date"] = to_date
    if status_f:
        extra += " AND COALESCE(pd.dispatch_status,'Packing Pending') = :status_f"
        params["status_f"] = status_f
    rows = db.execute(text(f"""
        SELECT
            pd.id, pd.dispatch_no, pd.order_no, pd.customer_name,
            COALESCE(co.brand,'') AS brand,
            COALESCE(co.channel,'') AS channel,
            COALESCE(co.required_delivery_date,'') AS delivery_date,
            COALESCE(co.required_delivery_time,'') AS delivery_time,
            COALESCE(co.total_planned_portions, pd.packed_portions, 0) AS planned_portions,
            COALESCE(pd.packed_portions,0) AS packed_portions,
            COALESCE(pd.rejected_portions,0) AS rejected_portions,
            COALESCE(pd.dispatch_status,'Packing Pending') AS packing_status,
            COALESCE(pd.remarks,'') AS remarks,
            pd.created_at
        FROM packing_dispatch pd
        LEFT JOIN customer_orders co ON co.order_no = pd.order_no
        WHERE COALESCE(pd.dispatch_status,'Packing Pending') IN ('Packing Pending','Packing In Progress','Packed','Pending')
        {extra}
        ORDER BY pd.id DESC
    """), params).mappings().all()
    summary = {
        "pending": db.execute(text("SELECT COUNT(*) FROM packing_dispatch WHERE COALESCE(dispatch_status,'Packing Pending') IN ('Packing Pending','Packing In Progress','Pending')")).scalar() or 0,
        "packed": db.execute(text("SELECT COUNT(*) FROM packing_dispatch WHERE dispatch_status = 'Packed'")).scalar() or 0,
        "rejected": db.execute(text("SELECT COALESCE(SUM(rejected_portions),0) FROM packing_dispatch")).scalar() or 0,
        "portions": db.execute(text("SELECT COALESCE(SUM(packed_portions),0) FROM packing_dispatch WHERE dispatch_status IN ('Packed','Out for Delivery','Delivered')")).scalar() or 0,
    }
    return render(request, "packing/index.html", {"rows": rows, "summary": summary, "page_title": "Trayline / Packing",
                                                   "filters": {"search": search, "from_date": from_date, "to_date": to_date, "status": status_f},
                                                   "error": request.query_params.get("error")})


@router.get("/{packing_id}", response_class=HTMLResponse)
def packing_order(request: Request, packing_id: int, db: Session = Depends(get_db)):
    require_area(request, "packing")
    row = db.query(PackingDispatch).filter(PackingDispatch.id == packing_id).first()
    if not row:
        return _redirect_with_error("/packing", "Packing record not found.")
    order = db.query(CustomerOrder).filter(CustomerOrder.order_no == row.order_no).first()
    qc_rows = db.execute(text("""
        SELECT qc_no, qc_status, overall_score, checked_by, checked_at, issue_found, corrective_action
        FROM qc_checks
        WHERE order_no = :order_no
        ORDER BY id DESC
        LIMIT 5
    """), {"order_no": row.order_no}).mappings().all()
    return render(request, "packing/order.html", {"row": row, "order": order, "qc_rows": qc_rows, "page_title": f"Packing - {row.order_no}", "error": request.query_params.get("error")})


@router.post("/{packing_id}/update")
def update_packing(
    request: Request,
    packing_id: int,
    packed_portions: float = Form(0),
    rejected_portions: float = Form(0),
    dispatch_date: Optional[str] = Form(None),
    packing_status: str = Form("Packed"),
    remarks: str = Form(""),
    db: Session = Depends(get_db),
):
    require_action(request, "packing", "edit")
    row = db.query(PackingDispatch).filter(PackingDispatch.id == packing_id).first()
    if not row:
        return _redirect_with_error("/packing", "Packing record not found.")
    if packing_status not in {"Packing Pending", "Packing In Progress", "Packed"}:
        packing_status = "Packed"
    row.packed_portions = packed_portions
    row.rejected_portions = rejected_portions
    row.dispatch_date = _parse_date(dispatch_date) or row.dispatch_date
    row.dispatch_status = packing_status
    row.remarks = remarks or row.remarks

    order = db.query(CustomerOrder).filter(CustomerOrder.order_no == row.order_no).first()
    if order:
        order.status = packing_status
    db.commit()
    return RedirectResponse("/packing", status_code=HTTP_303_SEE_OTHER)
