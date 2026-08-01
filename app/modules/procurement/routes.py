# app/modules/procurement/routes.py
"""Procurement — Purchase Orders -> GRN -> REAL inventory movements.

This module is where stock finally enters the system:
    Create PO (Open) -> Receive against PO (GRN) -> grn_receipts/grn_lines
    + a GRN_IN row in inventory_transactions (the stock ledger).

Screens:
  GET  /procurement                 PO register + inline create form
  GET  /procurement/po/{po_no}      PO detail + GRN receive form + GRN history
  POST /procurement/po/create       create PO with lines
  POST /procurement/po/{po_no}/receive   post a GRN
Access: uses the 'masters' area (store/procurement managers + admins).
"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.templates import render
from app.core.rbac import require_area, require_action
from app.database.session import get_db
# Batch 23: shared, legacy-aware stock ledger writer (see app/core/stock_ledger.py)
from app.core.stock_ledger import post_stock_movement
from app.core.gl_posting import post_grn_journal  # Batch 69: GRN → GL

router = APIRouter(prefix="/procurement", tags=["Procurement"])


def _cid(request: Request):
    return request.session.get("company_id")


def _user(request: Request) -> str:
    return request.session.get("username") or "system"


def _next_no(db: Session, table: str, col: str, prefix: str) -> str:
    today = date.today().strftime("%Y%m%d")
    row = db.execute(text(
        f"SELECT {col} FROM {table} WHERE {col} LIKE :p ORDER BY id DESC LIMIT 1"
    ), {"p": f"{prefix}-{today}-%"}).first()
    seq = int(row[0].rsplit("-", 1)[-1]) + 1 if row else 1
    return f"{prefix}-{today}-{seq:04d}"


def _ensure_procurement_schema(db: Session) -> None:
    db.execute(text("""
        CREATE TABLE IF NOT EXISTS purchase_orders (
            id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
            company_id INT NULL, po_no VARCHAR(80) NOT NULL UNIQUE, po_date DATE NULL,
            supplier_code VARCHAR(80) NULL, supplier_name VARCHAR(255) NOT NULL,
            expected_date DATE NULL, status VARCHAR(40) NOT NULL DEFAULT 'Open',
            total_value DECIMAL(18,4) NOT NULL DEFAULT 0, remarks TEXT NULL,
            created_by VARCHAR(120) NULL, created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            KEY idx_po_status (status), KEY idx_po_supplier (supplier_code)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """))
    db.execute(text("""
        CREATE TABLE IF NOT EXISTS purchase_order_lines (
            id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
            company_id INT NULL, po_no VARCHAR(80) NOT NULL, line_no INT NOT NULL,
            inventory_code VARCHAR(80) NOT NULL, item_name VARCHAR(255) NULL,
            ordered_qty DECIMAL(18,6) NOT NULL DEFAULT 0, received_qty DECIMAL(18,6) NOT NULL DEFAULT 0,
            uom VARCHAR(50) NULL, unit_price DECIMAL(18,6) NOT NULL DEFAULT 0,
            line_value DECIMAL(18,4) NOT NULL DEFAULT 0,
            KEY idx_pol_po (po_no), KEY idx_pol_item (inventory_code)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """))
    db.execute(text("""
        CREATE TABLE IF NOT EXISTS grn_receipts (
            id INT NOT NULL AUTO_INCREMENT PRIMARY KEY, company_id INT NULL,
            grn_no VARCHAR(80) NOT NULL UNIQUE, po_no VARCHAR(80) NOT NULL,
            supplier_name VARCHAR(255) NULL, status VARCHAR(40) NOT NULL DEFAULT 'Posted',
            received_date DATETIME NULL DEFAULT CURRENT_TIMESTAMP, received_by VARCHAR(120) NULL,
            remarks TEXT NULL, created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            KEY idx_grn_po (po_no)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """))
    for col, definition in {"supplier_name":"VARCHAR(255) NULL AFTER po_no", "status":"VARCHAR(40) NOT NULL DEFAULT 'Posted' AFTER supplier_name"}.items():
        exists = db.execute(text("""
            SELECT COUNT(*) FROM information_schema.columns WHERE table_schema=DATABASE() AND table_name='grn_receipts' AND column_name=:col
        """), {"col": col}).scalar()
        if not exists:
            db.execute(text(f"ALTER TABLE grn_receipts ADD COLUMN {col} {definition}"))
    db.execute(text("""
        CREATE TABLE IF NOT EXISTS grn_lines (
            id INT NOT NULL AUTO_INCREMENT PRIMARY KEY, company_id INT NULL, grn_no VARCHAR(80) NOT NULL,
            po_no VARCHAR(80) NOT NULL, inventory_code VARCHAR(80) NOT NULL, item_name VARCHAR(255) NULL,
            uom VARCHAR(50) NULL, received_qty DECIMAL(18,6) NOT NULL DEFAULT 0,
            unit_price DECIMAL(18,6) NOT NULL DEFAULT 0, lot_no VARCHAR(120) NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP, KEY idx_grnl_grn (grn_no), KEY idx_grnl_item (inventory_code)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """))
    db.execute(text("""
        CREATE TABLE IF NOT EXISTS inventory_transactions (
            id INT NOT NULL AUTO_INCREMENT PRIMARY KEY, company_id INT NULL,
            txn_date DATETIME NULL DEFAULT CURRENT_TIMESTAMP, transaction_date DATETIME NULL DEFAULT CURRENT_TIMESTAMP,
            inventory_code VARCHAR(80) NOT NULL, item_name VARCHAR(255) NULL, uom VARCHAR(50) NULL,
            qty_in DECIMAL(18,6) NOT NULL DEFAULT 0, qty_out DECIMAL(18,6) NOT NULL DEFAULT 0,
            unit_cost DECIMAL(18,6) NOT NULL DEFAULT 0, movement_type VARCHAR(60) NULL,
            reference_no VARCHAR(120) NULL, remarks TEXT NULL, created_by VARCHAR(120) NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP, KEY idx_inv_txn_code (inventory_code), KEY idx_inv_txn_ref (reference_no)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """))
    db.commit()


def _items(db: Session) -> list:
    return db.execute(text("""
        SELECT ingredient_code AS inventory_code, name AS item_name,
               COALESCE(standard_uom, purchase_uom, recipe_uom, '') AS uom
        FROM ingredients ORDER BY ingredient_code LIMIT 3000
    """)).mappings().all()


@router.get("")
async def po_register(request: Request, db: Session = Depends(get_db)):
    require_area(request, "procurement")
    _ensure_procurement_schema(db)
    q = request.query_params
    status_f = (q.get("status") or "").strip()
    search = (q.get("search") or "").strip()
    extra, params = "", {}
    if status_f:
        extra += " AND po.status = :status_f"; params["status_f"] = status_f
    if search:
        extra += " AND (po.po_no LIKE :s OR po.supplier_name LIKE :s)"; params["s"] = f"%{search}%"
    pos = db.execute(text(f"""
        SELECT po.*, (SELECT COUNT(*) FROM purchase_order_lines l WHERE l.po_no = po.po_no) AS line_count
        FROM purchase_orders po WHERE 1=1 {extra} ORDER BY po.id DESC LIMIT 300
    """), params).mappings().all()
    suppliers = db.execute(text(
        "SELECT supplier_code, supplier_name FROM suppliers ORDER BY supplier_name LIMIT 1000"
    )).mappings().all()
    summary = {
        "open": sum(1 for p in pos if p["status"] == "Open"),
        "partial": sum(1 for p in pos if p["status"] == "Partially Received"),
        "received": sum(1 for p in pos if p["status"] == "Received"),
        "value": round(sum(float(p["total_value"] or 0) for p in pos), 2),
    }
    return render(request, "procurement/index.html", {
        "pos": pos, "suppliers": suppliers, "items": _items(db), "summary": summary,
        "filters": {"status": status_f, "search": search, "from_date": "", "to_date": ""},
        "status_options": ["Open", "Partially Received", "Received", "Closed"],
        "page_title": "Procurement - Purchase Orders",
    })


@router.post("/po/create")
async def create_po(
    request: Request,
    supplier_code: str = Form(""),
    supplier_name: str = Form(...),
    expected_date: str = Form(""),
    remarks: str = Form(""),
    inventory_code: list[str] = Form([]),
    item_name: list[str] = Form([]),
    ordered_qty: list[float] = Form([]),
    uom: list[str] = Form([]),
    unit_price: list[float] = Form([]),
    db: Session = Depends(get_db),
):
    require_action(request, "procurement", "add")
    _ensure_procurement_schema(db)
    po_no = _next_no(db, "purchase_orders", "po_no", "PO")
    total, lines = 0.0, []
    for i, code in enumerate(inventory_code):
        code = (code or "").strip()
        qty = float(ordered_qty[i] or 0) if i < len(ordered_qty) else 0
        if not code or qty <= 0:
            continue
        price = float(unit_price[i] or 0) if i < len(unit_price) else 0
        value = round(qty * price, 4)
        total += value
        lines.append({
            "po_no": po_no, "line_no": len(lines) + 1, "inventory_code": code,
            "item_name": (item_name[i] if i < len(item_name) else "") or code,
            "ordered_qty": qty, "uom": (uom[i] if i < len(uom) else "") or "",
            "unit_price": price, "line_value": value, "company_id": _cid(request),
        })
    if not lines:
        return RedirectResponse("/procurement?error=Add at least one line with quantity", status_code=303)

    db.execute(text("""
        INSERT INTO purchase_orders (company_id, po_no, po_date, supplier_code, supplier_name,
                                     expected_date, status, total_value, remarks, created_by)
        VALUES (:cid, :po_no, :po_date, :sc, :sn, :ed, 'Open', :tv, :rm, :cb)
    """), {"cid": _cid(request), "po_no": po_no, "po_date": date.today(), "sc": supplier_code,
           "sn": supplier_name, "ed": expected_date or None, "tv": total, "rm": remarks,
           "cb": _user(request)})
    for l in lines:
        db.execute(text("""
            INSERT INTO purchase_order_lines (company_id, po_no, line_no, inventory_code, item_name,
                                              ordered_qty, uom, unit_price, line_value)
            VALUES (:company_id, :po_no, :line_no, :inventory_code, :item_name,
                    :ordered_qty, :uom, :unit_price, :line_value)
        """), l)
    db.commit()
    return RedirectResponse(f"/procurement/po/{po_no}?success=Purchase order {po_no} created", status_code=303)


@router.get("/po/{po_no}")
async def po_detail(request: Request, po_no: str, db: Session = Depends(get_db)):
    require_area(request, "procurement")
    _ensure_procurement_schema(db)
    po = db.execute(text("SELECT * FROM purchase_orders WHERE po_no = :p"), {"p": po_no}).mappings().first()
    if not po:
        return RedirectResponse("/procurement?error=PO not found", status_code=303)
    lines = db.execute(text("SELECT * FROM purchase_order_lines WHERE po_no = :p ORDER BY line_no"),
                       {"p": po_no}).mappings().all()
    grns = db.execute(text("""
        SELECT g.grn_no, g.received_date, g.received_by, g.remarks,
               ROUND(SUM(COALESCE(l.received_qty,0)),4) AS qty
        FROM grn_receipts g LEFT JOIN grn_lines l ON l.grn_no = g.grn_no
        WHERE g.po_no = :p
        GROUP BY g.grn_no, g.received_date, g.received_by, g.remarks
        ORDER BY MAX(g.id) DESC
    """), {"p": po_no}).mappings().all()
    return render(request, "procurement/po_detail.html", {
        "po": po, "lines": lines, "grns": grns, "page_title": f"PO {po_no}",
    })


@router.post("/po/{po_no}/receive")
async def po_receive(request: Request, po_no: str, db: Session = Depends(get_db)):
    """Post a GRN against this PO and write GRN_IN stock movements."""
    require_action(request, "procurement", "edit")
    _ensure_procurement_schema(db)
    form = await request.form()
    po = db.execute(text("SELECT * FROM purchase_orders WHERE po_no = :p"), {"p": po_no}).mappings().first()
    if not po or po["status"] not in ("Open", "Partially Received"):
        return RedirectResponse(f"/procurement/po/{po_no}?error=PO is not open for receiving", status_code=303)

    line_ids = form.getlist("line_id")
    recv_qtys = form.getlist("receive_qty")
    grn_no = _next_no(db, "grn_receipts", "grn_no", "GRN")
    cid = _cid(request)
    posted = 0
    grn_value = 0.0  # Batch 69: accumulate received value for the GL journal
    ledger_failures: list[str] = []  # Batch 23: surface ledger problems, never hide them

    for i, lid in enumerate(line_ids):
        try:
            qty = float(recv_qtys[i] or 0)
        except (ValueError, IndexError):
            qty = 0
        if qty <= 0:
            continue
        line = db.execute(text("SELECT * FROM purchase_order_lines WHERE id = :i"), {"i": int(lid)}).mappings().first()
        if not line:
            continue
        db.execute(text("""
            INSERT INTO grn_lines (company_id, grn_no, po_no, inventory_code, item_name, uom,
                                   received_qty, unit_price, lot_no)
            VALUES (:cid, :grn, :po, :code, :name, :uom, :qty, :price, :lot)
        """), {"cid": cid, "grn": grn_no, "po": po_no, "code": line["inventory_code"],
               "name": line["item_name"], "uom": line["uom"], "qty": qty,
               "price": line["unit_price"], "lot": form.get("lot_no") or ""})
        db.execute(text(
            "UPDATE purchase_order_lines SET received_qty = COALESCE(received_qty,0) + :q WHERE id = :i"
        ), {"q": qty, "i": int(lid)})
        # ---- THE REAL STOCK MOVEMENT (GRN_IN in the ledger) ----
        # Batch 23 FIX: this used to INSERT only the NEW column names inside a
        # bare `except: pass`. On databases where inventory_transactions still
        # has the LEGACY NOT NULL columns (transaction_no / ingredient_code /
        # ingredient_name / transaction_type) the INSERT was rejected and the
        # error was swallowed — the PO went to "RECEIVED" while stock stayed 0.
        # post_stock_movement() fills legacy AND new columns and reports failure.
        ok = post_stock_movement(
            db,
            company_id=cid,
            inventory_code=line["inventory_code"],
            item_name=line["item_name"],
            uom=line["uom"] or "",
            qty=qty,
            movement_type="GRN_IN",
            reference_no=grn_no,
            unit_cost=float(line["unit_price"] or 0),
            remarks=f"GRN against {po_no}",
            created_by=_user(request),
            lot_no=form.get("lot_no") or "",
            to_location="Main Store",
        )
        if not ok:
            ledger_failures.append(line["inventory_code"])
        grn_value += qty * float(line["unit_price"] or 0)  # Batch 69
        posted += 1

    if not posted:
        return RedirectResponse(f"/procurement/po/{po_no}?error=Enter a receive qty on at least one line", status_code=303)

    db.execute(text("""
        INSERT INTO grn_receipts (company_id, grn_no, po_no, supplier_name, received_date, status, received_by, remarks)
        VALUES (:cid, :grn, :po, :sn, CURDATE(), 'Posted', :rb, :rm)
    """), {"cid": cid, "grn": grn_no, "po": po_no, "sn": po["supplier_name"],
           "rb": _user(request), "rm": form.get("remarks") or ""})

    open_lines = db.execute(text("""
        SELECT SUM(CASE WHEN COALESCE(received_qty,0) >= COALESCE(ordered_qty,0) THEN 0 ELSE 1 END)
        FROM purchase_order_lines WHERE po_no = :p
    """), {"p": po_no}).scalar() or 0
    new_status = "Received" if int(open_lines) == 0 else "Partially Received"
    db.execute(text("UPDATE purchase_orders SET status = :s WHERE po_no = :p"), {"s": new_status, "p": po_no})
    db.commit()

    # Batch 69: auto-post the GRN to the GL — Dr 1130 Inventory / Cr 2200 GR accrual.
    # This is the inventory→GL bridge that was missing, so stock value now feeds
    # the P&L/Balance Sheet. Idempotent per GRN; never blocks the receive.
    try:
        post_grn_journal(db, request, grn_no, grn_value, supplier=po["supplier_name"] or "")
    except Exception:
        pass
    # Batch 23: if any line failed to reach the stock ledger, SAY SO. Previously
    # this always reported success even when zero stock had actually moved.
    if ledger_failures:
        bad = ", ".join(ledger_failures[:5])
        return RedirectResponse(
            f"/procurement/po/{po_no}?error=GRN {grn_no} saved but the stock ledger "
            f"rejected {len(ledger_failures)} line(s): {bad}. Check the server log "
            f"and run the Batch 23 migration.",
            status_code=303)
    return RedirectResponse(
        f"/procurement/po/{po_no}?success=GRN {grn_no} posted - stock received into ledger",
        status_code=303)


# ============================================================================
# Batch 26 — PO CLOSE / REOPEN  (Oracle & SAP B1 style document control)
# ----------------------------------------------------------------------------
# In Oracle Purchasing and SAP B1 a buyer can CLOSE a purchase order manually,
# even when it was never fully received. Typical reasons:
#   * the supplier short-shipped and will not send the remainder
#   * the balance is no longer needed
#   * the PO was raised in error
#
# A closed PO stops appearing in the "open commitments" list and can no longer
# receive a GRN, but all its history (lines, GRNs, stock movements, journals)
# is preserved — closing is NOT deleting.
#
# Statuses:
#   Open               awaiting receipt
#   Partially Received some lines received
#   Received           fully received
#   Closed             manually closed; no further GRN allowed
#   Cancelled          voided before any receipt
# ============================================================================
@router.post("/po/{po_no}/status")
async def set_po_status(request: Request, po_no: str, db: Session = Depends(get_db)):
    require_action(request, "procurement", "edit")
    _ensure_procurement_schema(db)
    form = await request.form()
    new_status = (form.get("new_status") or "").strip()
    reason = (form.get("reason") or "").strip()

    allowed = {"Open", "Closed", "Cancelled"}
    if new_status not in allowed:
        return RedirectResponse(
            f"/procurement/po/{po_no}?error=Invalid status '{new_status}'",
            status_code=303)

    po = db.execute(text("SELECT * FROM purchase_orders WHERE po_no = :p"),
                    {"p": po_no}).mappings().first()
    if not po:
        return RedirectResponse("/procurement?error=PO not found", status_code=303)

    # Cancelling is only safe before ANY goods were received — otherwise stock
    # already moved and the document must be Closed, not voided.
    if new_status == "Cancelled":
        received = db.execute(text(
            "SELECT COALESCE(SUM(COALESCE(received_qty,0)),0) FROM purchase_order_lines WHERE po_no = :p"
        ), {"p": po_no}).scalar() or 0
        if float(received) > 0:
            return RedirectResponse(
                f"/procurement/po/{po_no}?error=Cannot cancel: goods already received. "
                f"Use Close instead so stock history is preserved.",
                status_code=303)

    # Re-opening recomputes the true status from the received quantities so we
    # never re-open into a misleading state.
    if new_status == "Open":
        open_lines = db.execute(text("""
            SELECT SUM(CASE WHEN COALESCE(received_qty,0) >= COALESCE(ordered_qty,0) THEN 0 ELSE 1 END)
            FROM purchase_order_lines WHERE po_no = :p
        """), {"p": po_no}).scalar() or 0
        any_received = db.execute(text(
            "SELECT COALESCE(SUM(COALESCE(received_qty,0)),0) FROM purchase_order_lines WHERE po_no = :p"
        ), {"p": po_no}).scalar() or 0
        if int(open_lines) == 0:
            new_status = "Received"
        elif float(any_received) > 0:
            new_status = "Partially Received"

    note = f"[{new_status} by {_user(request)}]"
    if reason:
        note += f" {reason}"
    db.execute(text("""
        UPDATE purchase_orders
           SET status = :s,
               remarks = TRIM(CONCAT(COALESCE(remarks,''), ' ', :note))
         WHERE po_no = :p
    """), {"s": new_status, "note": note, "p": po_no})
    db.commit()
    return RedirectResponse(
        f"/procurement/po/{po_no}?success=PO {po_no} is now {new_status}",
        status_code=303)
