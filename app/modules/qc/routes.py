# app/modules/qc/routes.py
from datetime import datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import text
from sqlalchemy.orm import Session
from starlette.status import HTTP_303_SEE_OTHER

from app.core.templates import render
from app.core.notifications import notify_role
from app.database.session import get_db
from app.models.production import CustomerOrder, KitchenSectionTransaction, PackingDispatch, QCCheck

router = APIRouter(prefix="/qc", tags=["QC"])


def _user(request: Request) -> str:
    return request.session.get("username", "system")


def _redirect_with_error(url: str, message: str) -> RedirectResponse:
    sep = "&" if "?" in url else "?"
    return RedirectResponse(f"{url}{sep}error={message}", status_code=HTTP_303_SEE_OTHER)


def _next_no(db: Session, table: str, col: str, prefix: str) -> str:
    today = datetime.utcnow().strftime("%Y%m%d")
    like = f"{prefix}-{today}-%"
    row = db.execute(text(f"SELECT COUNT(*) FROM {table} WHERE {col} LIKE :like"), {"like": like}).scalar() or 0
    return f"{prefix}-{today}-{int(row) + 1:04d}"


def _qc_orders(db: Session, search: str = "", from_date: str = "", to_date: str = ""):
    extra = ""
    params = {}
    if search:
        extra += " AND (k.order_no LIKE :search OR COALESCE(co.customer_name,'') LIKE :search OR COALESCE(co.brand,'') LIKE :search)"
        params["search"] = f"%{search}%"
    if from_date:
        extra += " AND COALESCE(co.required_delivery_date,'') >= :from_date"
        params["from_date"] = from_date
    if to_date:
        extra += " AND COALESCE(co.required_delivery_date,'') <= :to_date"
        params["to_date"] = to_date
    return db.execute(text(f"""
        SELECT
            k.order_no,
            COALESCE(MAX(co.customer_name), '') AS customer_name,
            COALESCE(MAX(co.brand), '') AS brand,
            COALESCE(MAX(co.required_delivery_date), '') AS delivery_date,
            COALESCE(MAX(co.required_delivery_time), '') AS delivery_time,
            COUNT(*) AS total_lines,
            SUM(CASE WHEN COALESCE(k.received_qty_standard,0) > 0 THEN 1 ELSE 0 END) AS received_lines,
            ROUND(SUM(COALESCE(k.issued_qty_standard,0)), 4) AS input_qty,
            ROUND(SUM(COALESCE(k.received_qty_standard,0)), 4) AS received_qty,
            ROUND(SUM(COALESCE(k.balance_qty_standard,0)), 4) AS balance_qty,
            MAX(k.updated_at) AS last_activity
        FROM kitchen_section_transactions k
        LEFT JOIN customer_orders co ON co.order_no = k.order_no
        WHERE k.current_section = 'QC'
          AND UPPER(COALESCE(k.transaction_status,'')) NOT IN ('QC PASSED','QC REJECTED')
          {extra}
        GROUP BY k.order_no
        ORDER BY MAX(k.updated_at) DESC, k.order_no DESC
    """), params).mappings().all()


@router.get("", response_class=HTMLResponse)
def qc_dashboard(request: Request, db: Session = Depends(get_db)):
    q = request.query_params
    search = (q.get("search") or "").strip()
    from_date = (q.get("from_date") or "").strip()
    to_date = (q.get("to_date") or "").strip()
    status_f = (q.get("status") or "").strip()
    pending_orders = _qc_orders(db, search=search, from_date=from_date, to_date=to_date)
    hq = db.query(QCCheck)
    if status_f:
        hq = hq.filter(QCCheck.qc_status == status_f)
    if search:
        hq = hq.filter(QCCheck.order_no.like(f"%{search}%"))
    rows = hq.order_by(QCCheck.id.desc()).limit(200).all()
    summary = {
        "pending_orders": len(pending_orders),
        "pending_lines": sum(int(r.get("total_lines") or 0) for r in pending_orders),
        "passed": db.query(QCCheck).filter(QCCheck.qc_status == "Passed").count(),
        "hold": db.query(QCCheck).filter(QCCheck.qc_status == "Hold").count(),
        "rejected": db.query(QCCheck).filter(QCCheck.qc_status == "Rejected").count(),
    }
    return render(
        request,
        "qc/index.html",
        {
            "pending_orders": pending_orders,
            "rows": rows,
            "summary": summary,
            "page_title": "Quality Control",
            "filters": {"search": search, "from_date": from_date, "to_date": to_date, "status": status_f},
            "error": request.query_params.get("error"),
        },
    )


@router.get("/orders/{order_no}", response_class=HTMLResponse)
def qc_order(request: Request, order_no: str, db: Session = Depends(get_db)):
    order = db.query(CustomerOrder).filter(CustomerOrder.order_no == order_no).first()
    if not order:
        raise HTTPException(404, "Order not found")
    txs = (
        db.query(KitchenSectionTransaction)
        .filter(KitchenSectionTransaction.order_no == order_no, KitchenSectionTransaction.current_section == "QC")
        .order_by(KitchenSectionTransaction.recipe_name, KitchenSectionTransaction.ingredient_name)
        .all()
    )
    if not txs:
        return _redirect_with_error("/qc", "No QC lines found for this order. Transfer from kitchen section to QC first.")
    totals = {
        "lines": len(txs),
        "input_qty": sum(float(t.issued_qty_standard or 0) for t in txs),
        "received_qty": sum(float(t.received_qty_standard or 0) for t in txs),
        "balance_qty": sum(float(t.balance_qty_standard or 0) for t in txs),
    }
    return render(
        request,
        "qc/order.html",
        {"order": order, "txs": txs, "totals": totals, "page_title": f"QC - {order_no}", "error": request.query_params.get("error")},
    )


@router.post("/orders/{order_no}/receive-all")
def qc_receive_all(request: Request, order_no: str, db: Session = Depends(get_db)):
    txs = db.query(KitchenSectionTransaction).filter(
        KitchenSectionTransaction.order_no == order_no,
        KitchenSectionTransaction.current_section == "QC",
    ).all()
    if not txs:
        return _redirect_with_error("/qc", "No QC lines found to receive.")
    now = datetime.utcnow()
    user = _user(request)
    for tx in txs:
        if float(tx.received_qty_standard or 0) <= 0:
            qty = float(tx.issued_qty_standard or tx.balance_qty_standard or 0)
            tx.received_qty_standard = qty
            tx.balance_qty_standard = qty
            tx.received_by = user
            tx.received_at = now
            tx.transaction_status = "QC Received"
    order = db.query(CustomerOrder).filter(CustomerOrder.order_no == order_no).first()
    if order:
        order.status = "QC In Progress"
    db.commit()
    return RedirectResponse(f"/qc/orders/{order_no}", status_code=HTTP_303_SEE_OTHER)


@router.post("/orders/{order_no}/submit")
def qc_submit(
    request: Request,
    order_no: str,
    check_type: str = Form("Final QC"),
    temperature_c: float = Form(0),
    appearance_score: float = Form(0),
    taste_score: float = Form(0),
    portion_weight_score: float = Form(0),
    packaging_score: float = Form(0),
    hygiene_score: float = Form(0),
    qc_status: str = Form("Passed"),
    issue_found: str = Form(""),
    corrective_action: str = Form(""),
    db: Session = Depends(get_db),
):
    order = db.query(CustomerOrder).filter(CustomerOrder.order_no == order_no).first()
    if not order:
        return _redirect_with_error("/qc", "Order not found.")
    txs = db.query(KitchenSectionTransaction).filter(
        KitchenSectionTransaction.order_no == order_no,
        KitchenSectionTransaction.current_section == "QC",
    ).all()
    if not txs:
        return _redirect_with_error("/qc", "No QC lines found for this order.")

    scores = [appearance_score, taste_score, portion_weight_score, packaging_score, hygiene_score]
    overall_score = round(sum(float(s or 0) for s in scores) / len(scores), 2)
    status = qc_status if qc_status in {"Passed", "Hold", "Rejected"} else "Hold"
    now = datetime.utcnow()

    qc = QCCheck(
        company_id=int(request.session.get("company_id") or 1),
        qc_no=_next_no(db, "qc_checks", "qc_no", "QC"),
        order_no=order_no,
        batch_no=order_no,
        recipe_no=None,
        recipe_name="Order consolidated QC",
        section="QC",
        check_type=check_type,
        temperature_c=temperature_c or None,
        appearance_score=appearance_score,
        taste_score=taste_score,
        portion_weight_score=portion_weight_score,
        packaging_score=packaging_score,
        hygiene_score=hygiene_score,
        overall_score=overall_score,
        qc_status=status,
        checked_by=_user(request),
        checked_at=now,
        issue_found=issue_found or None,
        corrective_action=corrective_action or None,
    )
    db.add(qc)

    for tx in txs:
        if float(tx.received_qty_standard or 0) <= 0:
            qty = float(tx.issued_qty_standard or tx.balance_qty_standard or 0)
            tx.received_qty_standard = qty
            tx.balance_qty_standard = qty
            tx.received_by = tx.received_by or _user(request)
            tx.received_at = tx.received_at or now
        tx.processed_by = _user(request)
        tx.processed_at = now
        tx.section_remarks = corrective_action or issue_found or tx.section_remarks
        if status == "Passed":
            tx.transaction_status = "QC Passed"
            tx.transferred_by = _user(request)
            tx.transferred_at = now
            tx.transferred_qty_standard = float(tx.received_qty_standard or tx.issued_qty_standard or 0)
            tx.balance_qty_standard = 0
        elif status == "Rejected":
            tx.transaction_status = "QC Rejected"
            tx.qc_hold = True
        else:
            tx.transaction_status = "QC Hold"
            tx.qc_hold = True

    if status == "Passed":
        existing = db.query(PackingDispatch).filter(PackingDispatch.order_no == order_no).first()
        if not existing:
            dispatch = PackingDispatch(
                company_id=getattr(order, "company_id", None) or int(request.session.get("company_id") or 1),
                dispatch_no=_next_no(db, "packing_dispatch", "dispatch_no", "DSP"),
                order_no=order_no,
                customer_name=order.customer_name,
                packed_portions=float(order.total_planned_portions or 0),
                rejected_portions=0,
                dispatch_status="Packing Pending",
                remarks=f"Created automatically after QC pass {qc.qc_no}",
            )
            db.add(dispatch)
        order.status = "Packing Pending"
    elif status == "Rejected":
        order.status = "QC Rejected"
    else:
        order.status = "QC Hold"

    db.commit()

    # Batch 78: real notification — a QC failure/hold needs someone's
    # attention right away; a pass just flows on to packing automatically
    # and doesn't need one.
    if status in ("Rejected", "Hold"):
        notify_role(
            db, company_id=getattr(order, "company_id", None) or int(request.session.get("company_id") or 1),
            role="HEAD_CHEF",
            title=f"QC {status.lower()} on order {order_no}",
            message=(issue_found or corrective_action or f"Overall score {overall_score}")[:200],
            url=f"/qc/orders/{order_no}",
            category="qc_" + status.lower(),
        )
    return RedirectResponse("/qc", status_code=HTTP_303_SEE_OTHER)
