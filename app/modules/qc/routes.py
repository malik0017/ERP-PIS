# app/modules/qc/routes.py
from datetime import datetime
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import text
from sqlalchemy.orm import Session
from starlette.status import HTTP_303_SEE_OTHER
from app.core.templates import render
from app.core.rbac import require_area, require_action
from app.core.notifications import notify_role
from app.database.session import get_db
from app.models.production import CustomerOrder, KitchenSectionTransaction, PackingDispatch, QCCheck
from app.modules.production.routes import scoped_order

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
    require_area(request, "qc")
    q = request.query_params
    search = (q.get("search") or "").strip()
    from_date = (q.get("from_date") or "").strip()
    to_date = (q.get("to_date") or "").strip()
    status_f = (q.get("status") or "").strip()
    pending_orders = _qc_orders(db, search=search, from_date=from_date, to_date=to_date)
    # Batch 122: QC History now shows customer / brand / category by joining
    # customer_orders, instead of the bare QCCheck rows (image 15).
    hist_where = "1=1"
    hist_params: dict = {}
    if status_f:
        hist_where += " AND k.qc_status = :st"
        hist_params["st"] = status_f
    if search:
        hist_where += " AND k.order_no LIKE :sr"
        hist_params["sr"] = f"%{search}%"
    rows = db.execute(text(f"""
        SELECT k.qc_no, k.order_no, k.check_type, k.qc_status, k.overall_score,
               k.checked_by, k.checked_at, k.issue_found,
               COALESCE(co.customer_name, '') AS customer_name,
               COALESCE(co.brand, '') AS brand,
               COALESCE(NULLIF(co.channel,''), co.order_type, '') AS category
        FROM qc_checks k
        LEFT JOIN customer_orders co ON co.order_no = k.order_no
        WHERE {hist_where}
        ORDER BY k.id DESC
        LIMIT 200
    """), hist_params).mappings().all()
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
    require_area(request, "qc")
    order = scoped_order(db, request, order_no)
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

    # Batch 127: parse the [NUT w= p= c=] tag Hot Kitchen writes into the remark
    # so QC can show weight/protein/carb per portion in dedicated columns.
    import re as _re
    tx_rows = []
    for t in txs:
        rm = t.section_remarks or ""
        w = p = c = ""
        m = _re.search(r"\[NUT\s+([^\]]*)\]", rm)
        if m:
            for kv in m.group(1).split():
                if kv.startswith("w="):
                    w = kv[2:]
                elif kv.startswith("p="):
                    p = kv[2:]
                elif kv.startswith("c="):
                    c = kv[2:]
        tx_rows.append({
            "recipe_no": t.recipe_no, "recipe_name": t.recipe_name,
            "ingredient_code": t.ingredient_code, "ingredient_name": t.ingredient_name,
            "from_section": t.from_section, "issued_qty_standard": t.issued_qty_standard,
            "received_qty_standard": t.received_qty_standard, "standard_uom": t.standard_uom,
            "transaction_status": t.transaction_status,
            "nut_w": w, "nut_p": p, "nut_c": c,
            "clean_remark": _re.sub(r"\s*\[NUT[^\]]*\]", "", rm).strip(),
        })
    totals = {
        "lines": len(txs),
        "input_qty": sum(float(t.issued_qty_standard or 0) for t in txs),
        "received_qty": sum(float(t.received_qty_standard or 0) for t in txs),
        "balance_qty": sum(float(t.balance_qty_standard or 0) for t in txs),
    }
    return render(
        request,
        "qc/order.html",
        {"order": order, "txs": txs, "tx_rows": tx_rows, "totals": totals, "page_title": f"QC - {order_no}", "error": request.query_params.get("error")},
    )


@router.post("/orders/{order_no}/receive-all")
def qc_receive_all(request: Request, order_no: str, db: Session = Depends(get_db)):
    require_action(request, "qc", "edit")
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
    order = scoped_order(db, request, order_no)
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
    require_action(request, "qc", "edit")
    order = scoped_order(db, request, order_no)
    if not order:
        return _redirect_with_error("/qc", "Order not found.")
    txs = db.query(KitchenSectionTransaction).filter(
        KitchenSectionTransaction.order_no == order_no,
        KitchenSectionTransaction.current_section == "QC",
    ).all()
    if not txs:
        return _redirect_with_error("/qc", "No QC lines found for this order.")

    not_received = [t for t in txs if float(t.received_qty_standard or 0) <= 0]
    if not_received:
        return _redirect_with_error(
            f"/qc/orders/{order_no}",
            f"{len(not_received)} of {len(txs)} line(s) still show 0 received qty. "
            f"Use 'Receive All QC Lines' (or receive each line) before submitting a QC decision.")

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

    if status in ("Rejected", "Hold"):
        notify_role(
            db, company_id=getattr(order, "company_id", None) or int(request.session.get("company_id") or 1),
            role="HEAD_CHEF",
            title=f"QC {status.lower()} on order {order_no}",
            message=(issue_found or corrective_action or f"Overall score {overall_score}")[:200],
            url=f"/qc/orders/{order_no}",
            category="qc_" + status.lower(),
        )
    # Batch 121: professional, decision-aware success toast on redirect.
    if status == "Passed":
        _msg = f"QC {qc.qc_no} passed (score {overall_score}/10). Order {order_no} released to Packing."
        _title = "QC Passed"
    elif status == "Rejected":
        _msg = f"QC {qc.qc_no} rejected. Order {order_no} is blocked pending correction."
        _title = "QC Rejected"
    else:
        _msg = f"QC {qc.qc_no} placed on hold. Order {order_no} is blocked pending review."
        _title = "QC Hold"
    from urllib.parse import quote as _q
    return RedirectResponse(
        f"/qc?toast=success&title={_q(_title)}&msg={_q(_msg)}",
        status_code=HTTP_303_SEE_OTHER,
    )

def _ensure_incoming_qc_schema(db: Session) -> None:
    try:
        db.execute(text("""
            CREATE TABLE IF NOT EXISTS qc_incoming_inspections (
                id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                company_id INT NULL,
                grn_no VARCHAR(50) NOT NULL,
                po_no VARCHAR(50) NULL,
                supplier_name VARCHAR(255) NULL,
                temperature_ok TINYINT(1) NULL,
                temperature_reading VARCHAR(50) NULL,
                packaging_ok TINYINT(1) NULL,
                expiry_ok TINYINT(1) NULL,
                documentation_ok TINYINT(1) NULL,
                decision VARCHAR(20) NOT NULL,
                notes VARCHAR(500) NULL,
                inspected_by VARCHAR(120) NULL,
                inspected_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                KEY idx_qii_grn (grn_no)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """))
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass


@router.get("/inspection")
def incoming_qc_list(request: Request, db: Session = Depends(get_db)):
    require_area(request, "qc")
    _ensure_incoming_qc_schema(db)
    from app.core.stock_ledger import ensure_qc_status_column
    ensure_qc_status_column(db)
    cid = int(request.session.get("company_id") or 1)

    pending = db.execute(text("""
        SELECT g.grn_no, g.po_no, g.supplier_name, g.received_date, g.received_by,
               COUNT(DISTINCT t.inventory_code) AS item_count,
               ROUND(SUM(t.qty_in), 3) AS total_qty
        FROM grn_receipts g
        JOIN inventory_transactions t ON t.reference_no = g.grn_no AND t.movement_type = 'GRN_IN'
        WHERE t.qc_status = 'Pending' AND (t.company_id = :cid OR t.company_id IS NULL)
        GROUP BY g.grn_no, g.po_no, g.supplier_name, g.received_date, g.received_by
        ORDER BY g.received_date DESC
    """), {"cid": cid}).mappings().all()

    recent = db.execute(text("""
        SELECT * FROM qc_incoming_inspections
        WHERE (company_id = :cid OR company_id IS NULL)
        ORDER BY id DESC LIMIT 30
    """), {"cid": cid}).mappings().all()

    return render(request, "qc/inspection_list.html", {
        "pending": pending, "recent": recent, "page_title": "Incoming QC Inspection",
    })


@router.get("/inspection/{grn_no}")
def incoming_qc_detail(request: Request, grn_no: str, db: Session = Depends(get_db)):
    require_area(request, "qc")
    _ensure_incoming_qc_schema(db)
    cid = int(request.session.get("company_id") or 1)

    grn = db.execute(text("SELECT * FROM grn_receipts WHERE grn_no = :g"), {"g": grn_no}).mappings().first()
    if not grn:
        raise HTTPException(404, "GRN not found")

    lines = db.execute(text("""
        SELECT t.inventory_code, t.item_name, t.uom, t.qty_in, t.lot_no, t.qc_status
        FROM inventory_transactions t
        WHERE t.reference_no = :g AND t.movement_type = 'GRN_IN' AND (t.company_id = :cid OR t.company_id IS NULL)
    """), {"g": grn_no, "cid": cid}).mappings().all()

    already = db.execute(text(
        "SELECT * FROM qc_incoming_inspections WHERE grn_no = :g ORDER BY id DESC LIMIT 1"
    ), {"g": grn_no}).mappings().first()

    return render(request, "qc/inspection_detail.html", {
        "grn": grn, "lines": lines, "already": already, "page_title": f"Incoming QC — {grn_no}",
    })


@router.post("/inspection/{grn_no}/decide")
async def incoming_qc_decide(request: Request, grn_no: str, db: Session = Depends(get_db)):
    require_action(request, "qc", "edit")
    _ensure_incoming_qc_schema(db)
    from app.core.stock_ledger import ensure_qc_status_column
    ensure_qc_status_column(db)
    cid = int(request.session.get("company_id") or 1)

    form = await request.form()
    decision = (form.get("decision") or "").strip()
    if decision not in ("Passed", "Failed"):
        return _redirect_with_error(f"/qc/inspection/{grn_no}", "Choose Pass or Fail before submitting.")

    grn = db.execute(text("SELECT * FROM grn_receipts WHERE grn_no = :g"), {"g": grn_no}).mappings().first()
    if not grn:
        raise HTTPException(404, "GRN not found")

    def _flag(name: str) -> int | None:
        v = form.get(name)
        if v == "yes":
            return 1
        if v == "no":
            return 0
        return None

    db.execute(text("""
        INSERT INTO qc_incoming_inspections
            (company_id, grn_no, po_no, supplier_name, temperature_ok, temperature_reading,
             packaging_ok, expiry_ok, documentation_ok, decision, notes, inspected_by)
        VALUES (:cid, :grn, :po, :sup, :temp_ok, :temp_read, :pack_ok, :exp_ok, :doc_ok, :dec, :notes, :by)
    """), {
        "cid": cid, "grn": grn_no, "po": grn["po_no"], "sup": grn["supplier_name"],
        "temp_ok": _flag("temperature_ok"), "temp_read": (form.get("temperature_reading") or "").strip() or None,
        "pack_ok": _flag("packaging_ok"), "exp_ok": _flag("expiry_ok"), "doc_ok": _flag("documentation_ok"),
        "dec": decision, "notes": (form.get("notes") or "").strip() or None, "by": _user(request),
    })

    db.execute(text("""
        UPDATE inventory_transactions
        SET qc_status = :dec
        WHERE reference_no = :g AND movement_type = 'GRN_IN' AND (company_id = :cid OR company_id IS NULL)
    """), {"dec": decision, "g": grn_no, "cid": cid})
    db.commit()

    if decision == "Failed":
        notify_role(db, company_id=cid, role="ADMIN",
                    title=f"Incoming QC failed — {grn_no}",
                    message=f"{grn['supplier_name'] or 'Supplier'} · {(form.get('notes') or '').strip()[:150]}",
                    url=f"/qc/inspection/{grn_no}", category="incoming_qc_failed")
        return RedirectResponse(f"/qc/inspection?toast=danger&title=QC Failed&msg={grn_no} held — stock stays excluded from available inventory. Raise a supplier return from Procurement.", status_code=303)

    return RedirectResponse(f"/qc/inspection?toast=success&title=QC Passed&msg={grn_no} cleared — stock is now available for production.", status_code=303)


def _ensure_complaints_schema(db: Session) -> None:
    try:
        db.execute(text("""
            CREATE TABLE IF NOT EXISTS qc_complaints (
                id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                company_id INT NULL,
                complaint_no VARCHAR(30) NOT NULL UNIQUE,
                order_no VARCHAR(80) NULL,
                customer_name VARCHAR(255) NULL,
                complaint_date DATE NULL,
                category VARCHAR(50) NULL,
                description VARCHAR(1000) NULL,
                traced_section VARCHAR(80) NULL,
                traced_shift VARCHAR(80) NULL,
                root_cause VARCHAR(1000) NULL,
                corrective_action VARCHAR(1000) NULL,
                status VARCHAR(20) NOT NULL DEFAULT 'Open',
                logged_by VARCHAR(120) NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                resolved_at DATETIME NULL
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """))
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass


def _next_complaint_no(db: Session) -> str:
    n = int(db.execute(text("SELECT COUNT(*) FROM qc_complaints")).scalar() or 0)
    return f"CMP-{n + 1:05d}"


@router.get("/complaints")
def complaints_list(request: Request, db: Session = Depends(get_db)):
    require_area(request, "qc")
    _ensure_complaints_schema(db)
    cid = int(request.session.get("company_id") or 1)
    status_filter = (request.query_params.get("status") or "").strip()
    where = "(company_id = :cid OR company_id IS NULL)"
    params: dict = {"cid": cid}
    if status_filter:
        where += " AND status = :st"
        params["st"] = status_filter
    rows = db.execute(text(f"SELECT * FROM qc_complaints WHERE {where} ORDER BY id DESC LIMIT 300"), params).mappings().all()
    kpis = {
        "open": db.execute(text("SELECT COUNT(*) FROM qc_complaints WHERE status='Open' AND (company_id = :cid OR company_id IS NULL)"), {"cid": cid}).scalar() or 0,
        "investigating": db.execute(text("SELECT COUNT(*) FROM qc_complaints WHERE status='Investigating' AND (company_id = :cid OR company_id IS NULL)"), {"cid": cid}).scalar() or 0,
        "resolved": db.execute(text("SELECT COUNT(*) FROM qc_complaints WHERE status='Resolved' AND (company_id = :cid OR company_id IS NULL)"), {"cid": cid}).scalar() or 0,
    }
    return render(request, "qc/complaints_list.html", {"rows": rows, "kpis": kpis, "status_filter": status_filter, "page_title": "Customer Complaints"})


@router.get("/complaints/new")
def new_complaint_form(request: Request, db: Session = Depends(get_db)):
    require_area(request, "qc")
    order_no = (request.query_params.get("order_no") or "").strip()
    order = None
    sections = []
    if order_no:
        order = scoped_order(db, request, order_no)
        sections = db.execute(text(
            "SELECT DISTINCT current_section FROM kitchen_section_transactions WHERE order_no = :o"
        ), {"o": order_no}).scalars().all()
    return render(request, "qc/complaint_form.html", {"order": order, "order_no": order_no, "sections": sections, "page_title": "Log Complaint"})


@router.post("/complaints/new")
async def create_complaint(request: Request, db: Session = Depends(get_db)):
    require_action(request, "qc", "add")
    _ensure_complaints_schema(db)
    form = await request.form()
    description = (form.get("description") or "").strip()
    if not description:
        return _redirect_with_error("/qc/complaints/new", "Describe the complaint before submitting.")

    cid = int(request.session.get("company_id") or 1)
    complaint_no = _next_complaint_no(db)
    db.execute(text("""
        INSERT INTO qc_complaints
            (company_id, complaint_no, order_no, customer_name, complaint_date, category,
             description, traced_section, traced_shift, status, logged_by)
        VALUES (:cid, :no, :order_no, :cust, CURDATE(), :cat, :desc, :sec, :shift, 'Open', :by)
    """), {
        "cid": cid, "no": complaint_no, "order_no": (form.get("order_no") or "").strip() or None,
        "cust": (form.get("customer_name") or "").strip() or None,
        "cat": (form.get("category") or "").strip() or None, "desc": description,
        "sec": (form.get("traced_section") or "").strip() or None,
        "shift": (form.get("traced_shift") or "").strip() or None, "by": _user(request),
    })
    db.commit()
    notify_role(db, company_id=cid, role="ADMIN",
                title=f"New customer complaint — {complaint_no}",
                message=description[:150], url=f"/qc/complaints/{complaint_no}", category="complaint_logged")
    return RedirectResponse(f"/qc/complaints/{complaint_no}?toast=success&title=Complaint Logged&msg={complaint_no} recorded", status_code=303)


@router.get("/complaints/{complaint_no}")
def complaint_detail(request: Request, complaint_no: str, db: Session = Depends(get_db)):
    require_area(request, "qc")
    row = db.execute(text("SELECT * FROM qc_complaints WHERE complaint_no = :n"), {"n": complaint_no}).mappings().first()
    if not row:
        raise HTTPException(404, "Complaint not found")
    return render(request, "qc/complaint_detail.html", {"c": row, "page_title": complaint_no})


@router.post("/complaints/{complaint_no}/update")
async def update_complaint(request: Request, complaint_no: str, db: Session = Depends(get_db)):
    require_action(request, "qc", "edit")
    form = await request.form()
    status = (form.get("status") or "Open").strip()
    resolved_at_sql = ", resolved_at = NOW()" if status == "Resolved" else ""
    db.execute(text(f"""
        UPDATE qc_complaints
        SET root_cause = :rc, corrective_action = :ca, status = :st {resolved_at_sql}
        WHERE complaint_no = :n
    """), {
        "rc": (form.get("root_cause") or "").strip() or None,
        "ca": (form.get("corrective_action") or "").strip() or None,
        "st": status, "n": complaint_no,
    })
    db.commit()
    return RedirectResponse(f"/qc/complaints/{complaint_no}?toast=success&title=Updated&msg=Complaint updated", status_code=303)
