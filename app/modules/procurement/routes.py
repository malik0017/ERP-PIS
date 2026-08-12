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
from app.core.company import get_current_company_id
from app.database.session import get_db
# Batch 23: shared, legacy-aware stock ledger writer (see app/core/stock_ledger.py)
from app.core.stock_ledger import post_stock_movement, ensure_qc_status_column
from app.core.gl_posting import post_grn_journal  # Batch 69: GRN → GL

router = APIRouter(prefix="/procurement", tags=["Procurement"])


def _cid(request: Request):
    return get_current_company_id(request)


def _user(request: Request) -> str:
    return request.session.get("username") or "system"


def _next_no(db: Session, table: str, col: str, prefix: str) -> str:
    today = date.today().strftime("%Y%m%d")
    row = db.execute(text(
        f"SELECT {col} FROM {table} WHERE {col} LIKE :p ORDER BY id DESC LIMIT 1"
    ), {"p": f"{prefix}-{today}-%"}).first()
    seq = int(row[0].rsplit("-", 1)[-1]) + 1 if row else 1
    return f"{prefix}-{today}-{seq:04d}"


def _ensure_supplier_rating_schema(db: Session) -> None:
    """Batch 95 — supplier performance rating. Deliberately raw SQL only,
    not added to the Supplier ORM model at all: this codebase learned the
    hard way (Batch 89) that adding a column to a model without every
    caller knowing to migrate first breaks every OTHER route that touches
    that table via the ORM. Since ratings are only ever read/written
    through this module's own raw SQL, that whole risk class doesn't
    apply here — the ORM never needs to know this column exists.
    """
    try:
        exists = db.execute(text("""
            SELECT COUNT(*) FROM information_schema.columns
            WHERE table_schema = DATABASE() AND table_name = 'suppliers' AND column_name = 'rating'
        """)).scalar()
        if not exists:
            db.execute(text("""
                ALTER TABLE suppliers
                ADD COLUMN rating TINYINT NULL,
                ADD COLUMN rating_notes VARCHAR(500) NULL,
                ADD COLUMN rating_updated_by VARCHAR(120) NULL,
                ADD COLUMN rating_updated_at DATETIME NULL
            """))
            db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass


@router.get("/suppliers/ratings")
def supplier_ratings(request: Request, db: Session = Depends(get_db)):
    require_area(request, "procurement")
    _ensure_supplier_rating_schema(db)
    cid = _cid(request)
    rows = db.execute(text("""
        SELECT s.supplier_code, s.supplier_name, s.category, s.rating, s.rating_notes,
               s.rating_updated_by, s.rating_updated_at,
               COUNT(DISTINCT po.po_no) AS po_count,
               ROUND(COALESCE(SUM(po.total_value), 0), 2) AS total_value,
               SUM(CASE WHEN po.status IN ('Received') AND po.expected_date IS NOT NULL
                        AND EXISTS (SELECT 1 FROM grn_receipts g WHERE g.po_no = po.po_no AND g.received_date <= po.expected_date)
                   THEN 1 ELSE 0 END) AS on_time_count
        FROM suppliers s
        LEFT JOIN purchase_orders po ON po.supplier_name = s.supplier_name AND (po.company_id = :cid OR po.company_id IS NULL)
        WHERE (s.company_id = :cid OR s.company_id IS NULL)
        GROUP BY s.supplier_code, s.supplier_name, s.category, s.rating, s.rating_notes, s.rating_updated_by, s.rating_updated_at
        ORDER BY s.rating DESC, total_value DESC
    """), {"cid": cid}).mappings().all()
    return render(request, "procurement/supplier_ratings.html", {"rows": rows, "page_title": "Supplier Ratings"})


@router.post("/suppliers/{supplier_code}/rating")
async def update_supplier_rating(request: Request, supplier_code: str, db: Session = Depends(get_db)):
    require_action(request, "procurement", "edit")
    _ensure_supplier_rating_schema(db)
    form = await request.form()
    try:
        rating = int(form.get("rating") or 0)
    except ValueError:
        rating = 0
    if rating < 1 or rating > 5:
        return RedirectResponse("/procurement/suppliers/ratings?toast=warning&title=Invalid Rating&msg=Choose 1 to 5 stars", status_code=303)
    db.execute(text("""
        UPDATE suppliers SET rating = :r, rating_notes = :notes, rating_updated_by = :by, rating_updated_at = NOW()
        WHERE supplier_code = :code AND (company_id = :cid OR company_id IS NULL)
    """), {"r": rating, "notes": (form.get("notes") or "").strip() or None, "by": _user(request),
           "code": supplier_code, "cid": _cid(request)})
    db.commit()
    return RedirectResponse("/procurement/suppliers/ratings?toast=success&title=Rating Saved&msg=Supplier rating updated", status_code=303)


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
    cid = _cid(request)
    extra += " AND (po.company_id = :cid OR po.company_id IS NULL)"
    params["cid"] = cid
    if status_f:
        extra += " AND po.status = :status_f"; params["status_f"] = status_f
    if search:
        extra += " AND (po.po_no LIKE :s OR po.supplier_name LIKE :s)"; params["s"] = f"%{search}%"
    pos = db.execute(text(f"""
        SELECT po.*, (SELECT COUNT(*) FROM purchase_order_lines l WHERE l.po_no = po.po_no) AS line_count
        FROM purchase_orders po WHERE 1=1 {extra} ORDER BY po.id DESC LIMIT 300
    """), params).mappings().all()
    suppliers = db.execute(text(
        "SELECT supplier_code, supplier_name, rating FROM suppliers ORDER BY COALESCE(rating,0) DESC, supplier_name LIMIT 1000"
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


@router.post("/po/{po_no}/edit")
async def edit_po(request: Request, po_no: str, db: Session = Depends(get_db)):
    """Batch 88 — the PO edit capability that was missing entirely.
    Auto-generated POs (from the shortage-PO feature, or any manual PO
    where details weren't final yet) could show "Unassigned Supplier"
    and zero unit prices with no way to fix them short of starting over.
    Supplier name and, per line, ordered quantity + unit price can now
    be corrected here. Lines that already have SOME received_qty are
    left alone — editing a quantity or price after stock has already
    been received against it would silently corrupt what's already
    posted to the ledger and GL, so those lines are protected.
    """
    require_action(request, "procurement", "edit")
    _ensure_procurement_schema(db)
    cid = _cid(request)
    po = db.execute(text(
        "SELECT * FROM purchase_orders WHERE po_no = :p AND (company_id = :cid OR company_id IS NULL)"
    ), {"p": po_no, "cid": cid}).mappings().first()
    if not po or po["status"] not in ("Open", "Partially Received"):
        return RedirectResponse(f"/procurement/po/{po_no}?error=PO is not open for editing", status_code=303)

    form = await request.form()
    supplier_name = (form.get("supplier_name") or "").strip()
    supplier_code = (form.get("supplier_code") or "").strip()
    if supplier_name:
        db.execute(text("UPDATE purchase_orders SET supplier_name = :sn, supplier_code = :sc WHERE po_no = :p"),
                  {"sn": supplier_name, "sc": supplier_code or None, "p": po_no})

    line_ids = form.getlist("edit_line_id")
    qtys = form.getlist("edit_ordered_qty")
    prices = form.getlist("edit_unit_price")
    for i, lid in enumerate(line_ids):
        line = db.execute(text("SELECT * FROM purchase_order_lines WHERE id = :i"), {"i": int(lid)}).mappings().first()
        if not line or float(line["received_qty"] or 0) > 0:
            continue  # protected: something has already been received against this line
        try:
            new_qty = float(qtys[i]) if i < len(qtys) and qtys[i] != "" else float(line["ordered_qty"] or 0)
        except ValueError:
            new_qty = float(line["ordered_qty"] or 0)
        try:
            new_price = float(prices[i]) if i < len(prices) and prices[i] != "" else float(line["unit_price"] or 0)
        except ValueError:
            new_price = float(line["unit_price"] or 0)
        db.execute(text("""
            UPDATE purchase_order_lines
            SET ordered_qty = :q, unit_price = :pr, line_value = :q * :pr
            WHERE id = :i
        """), {"q": new_qty, "pr": new_price, "i": int(lid)})

    new_total = db.execute(text("SELECT COALESCE(SUM(line_value),0) FROM purchase_order_lines WHERE po_no = :p"), {"p": po_no}).scalar() or 0
    db.execute(text("UPDATE purchase_orders SET total_value = :tv WHERE po_no = :p"), {"tv": new_total, "p": po_no})
    db.commit()
    return RedirectResponse(f"/procurement/po/{po_no}?toast=success&title=Updated&msg=Purchase order details updated", status_code=303)


@router.get("/po/{po_no}")
async def po_detail(request: Request, po_no: str, db: Session = Depends(get_db)):
    require_area(request, "procurement")
    _ensure_procurement_schema(db)
    po = db.execute(text(
        "SELECT * FROM purchase_orders WHERE po_no = :p AND (company_id = :cid OR company_id IS NULL)"
    ), {"p": po_no, "cid": _cid(request)}).mappings().first()
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
    suppliers = db.execute(text(
        "SELECT supplier_code, supplier_name, rating FROM suppliers WHERE (company_id = :cid OR company_id IS NULL) ORDER BY COALESCE(rating,0) DESC, supplier_name LIMIT 1000"
    ), {"cid": _cid(request)}).mappings().all()
    return render(request, "procurement/po_detail.html", {
        "po": po, "lines": lines, "grns": grns, "suppliers": suppliers, "page_title": f"PO {po_no}",
    })


@router.post("/po/{po_no}/receive")
async def po_receive(request: Request, po_no: str, db: Session = Depends(get_db)):
    """Post a GRN against this PO and write GRN_IN stock movements."""
    require_action(request, "procurement", "edit")
    _ensure_procurement_schema(db)
    ensure_qc_status_column(db)
    form = await request.form()
    po = db.execute(text(
        "SELECT * FROM purchase_orders WHERE po_no = :p AND (company_id = :cid OR company_id IS NULL)"
    ), {"p": po_no, "cid": _cid(request)}).mappings().first()
    if not po or po["status"] not in ("Open", "Partially Received"):
        return RedirectResponse(f"/procurement/po/{po_no}?error=PO is not open for receiving", status_code=303)

    line_ids = form.getlist("line_id")
    recv_qtys = form.getlist("receive_qty")
    recv_prices = form.getlist("receive_price")  # Batch 88: optional price override at receiving
    grn_no = _next_no(db, "grn_receipts", "grn_no", "GRN")
    cid = _cid(request)

    from app.modules.qc.sampling import decide as _qc_sample_decide
    _sample_codes: list[str] = []
    for _i, _lid in enumerate(line_ids):
        try:
            if float(recv_qtys[_i] or 0) <= 0:
                continue
        except (ValueError, IndexError):
            continue
        _l = db.execute(text("SELECT inventory_code FROM purchase_order_lines WHERE id = :i"),
                        {"i": int(_lid)}).first()
        if _l and _l[0]:
            _sample_codes.append(_l[0])
    grn_qc_status, grn_qc_reason = _qc_sample_decide(
        db, company_id=cid, supplier_name=po["supplier_name"] or "", inventory_codes=_sample_codes)
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

        po_price = float(line["unit_price"] or 0)
        actual_price = po_price
        if i < len(recv_prices) and recv_prices[i] not in ("", None):
            try:
                actual_price = float(recv_prices[i])
            except ValueError:
                actual_price = po_price
        db.execute(text("""
            INSERT INTO grn_lines (company_id, grn_no, po_no, inventory_code, item_name, uom,
                                   received_qty, unit_price, lot_no)
            VALUES (:cid, :grn, :po, :code, :name, :uom, :qty, :price, :lot)
        """), {"cid": cid, "grn": grn_no, "po": po_no, "code": line["inventory_code"],
               "name": line["item_name"], "uom": line["uom"], "qty": qty,
               "price": actual_price, "lot": form.get("lot_no") or ""})
        db.execute(text(
            "UPDATE purchase_order_lines SET received_qty = COALESCE(received_qty,0) + :q, "
            "unit_price = :pr, line_value = ordered_qty * :pr WHERE id = :i"
        ), {"q": qty, "pr": actual_price, "i": int(lid)})
 
        ok = post_stock_movement(
            db,
            company_id=cid,
            inventory_code=line["inventory_code"],
            item_name=line["item_name"],
            uom=line["uom"] or "",
            qty=qty,
            movement_type="GRN_IN",
            reference_no=grn_no,
            unit_cost=actual_price,
            remarks=f"GRN against {po_no}",
            created_by=_user(request),
            lot_no=form.get("lot_no") or "",
            to_location=("QC Hold" if grn_qc_status == "Pending" else "Main Store"),
            qc_status=grn_qc_status,  # Batch 94: sampling verdict, not hard-coded
        )
        if not ok:
            ledger_failures.append(line["inventory_code"])
        grn_value += qty * actual_price  # Batch 69
        posted += 1

    extra_code = (form.get("extra_inventory_code") or "").strip()
    extra_qty_raw = form.get("extra_qty") or ""
    if extra_code and extra_qty_raw:
        try:
            extra_qty = float(extra_qty_raw)
        except ValueError:
            extra_qty = 0
        if extra_qty > 0:
            try:
                extra_price = float(form.get("extra_price") or 0)
            except ValueError:
                extra_price = 0
            extra_name = (form.get("extra_item_name") or extra_code).strip()
            extra_uom = (form.get("extra_uom") or "Kg").strip()
            next_line_no = int(db.execute(text("SELECT COALESCE(MAX(line_no),0)+1 FROM purchase_order_lines WHERE po_no = :p"), {"p": po_no}).scalar() or 1)
            new_line_id = db.execute(text("""
                INSERT INTO purchase_order_lines (company_id, po_no, line_no, inventory_code, item_name,
                                                  ordered_qty, received_qty, uom, unit_price, line_value)
                VALUES (:cid, :po, :ln, :code, :name, 0, :qty, :uom, :price, :val)
            """), {"cid": cid, "po": po_no, "ln": next_line_no, "code": extra_code, "name": extra_name,
                   "qty": extra_qty, "uom": extra_uom, "price": extra_price, "val": extra_qty * extra_price}).lastrowid
            db.execute(text("""
                INSERT INTO grn_lines (company_id, grn_no, po_no, inventory_code, item_name, uom,
                                       received_qty, unit_price, lot_no, remarks)
                VALUES (:cid, :grn, :po, :code, :name, :uom, :qty, :price, :lot, 'Not on original PO — added at receiving')
            """), {"cid": cid, "grn": grn_no, "po": po_no, "code": extra_code, "name": extra_name,
                   "uom": extra_uom, "qty": extra_qty, "price": extra_price, "lot": form.get("lot_no") or ""})
            ok = post_stock_movement(
                db, company_id=cid, inventory_code=extra_code, item_name=extra_name, uom=extra_uom,
                qty=extra_qty, movement_type="GRN_IN", reference_no=grn_no, unit_cost=extra_price,
                remarks=f"GRN against {po_no} — not on original PO", created_by=_user(request),
                lot_no=form.get("lot_no") or "",
                to_location=("QC Hold" if grn_qc_status == "Pending" else "Main Store"),
                qc_status=grn_qc_status,
            )
            if not ok:
                ledger_failures.append(extra_code)
            grn_value += extra_qty * extra_price
            posted += 1

    if not posted:
        return RedirectResponse(f"/procurement/po/{po_no}?error=Enter a receive qty on at least one line, or add an unlisted item received", status_code=303)

    db.execute(text("""
        INSERT INTO grn_receipts (company_id, grn_no, po_no, supplier_name, received_date, status, received_by, remarks)
        VALUES (:cid, :grn, :po, :sn, CURDATE(), 'Posted', :rb, :rm)
    """), {"cid": cid, "grn": grn_no, "po": po_no, "sn": po["supplier_name"],
           "rb": _user(request), "rm": form.get("remarks") or ""})
    if grn_qc_status != "Pending":
        try:
            from app.modules.qc.sampling import record_auto_release
            record_auto_release(db, company_id=cid, grn_no=grn_no, po_no=po_no,
                                supplier_name=po["supplier_name"] or "",
                                reason=grn_qc_reason, by=_user(request))
        except Exception:
            pass

    open_lines = db.execute(text("""
        SELECT SUM(CASE WHEN COALESCE(received_qty,0) >= COALESCE(ordered_qty,0) THEN 0 ELSE 1 END)
        FROM purchase_order_lines WHERE po_no = :p
    """), {"p": po_no}).scalar() or 0
    new_status = "Received" if int(open_lines) == 0 else "Partially Received"
    db.execute(text("UPDATE purchase_orders SET status = :s WHERE po_no = :p"), {"s": new_status, "p": po_no})
    db.commit()
    try:
        post_grn_journal(db, request, grn_no, grn_value, supplier=po["supplier_name"] or "")
    except Exception:
        pass
    try:
        if grn_value and float(grn_value) > 0 and po["supplier_name"] and po["supplier_name"] != "Unassigned Supplier":
            from app.modules.finance.routes import create_ap_invoice_core
            create_ap_invoice_core(
                db, request, cid, _user(request),
                supplier_name=po["supplier_name"], po_no=po_no, grn_no=grn_no,
                amount=float(grn_value), remarks=f"Auto-created on GRN {grn_no}",
            )
    except Exception:
        pass 
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
#  PO CLOSE / REOPEN  (document control)
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

    po = db.execute(text(
                        "SELECT * FROM purchase_orders WHERE po_no = :p AND (company_id = :cid OR company_id IS NULL)"
                    ), {"p": po_no, "cid": _cid(request)}).mappings().first()
    if not po:
        return RedirectResponse("/procurement?error=PO not found", status_code=303)

    
    if new_status == "Cancelled":
        received = db.execute(text(
            "SELECT COALESCE(SUM(COALESCE(received_qty,0)),0) FROM purchase_order_lines WHERE po_no = :p"
        ), {"p": po_no}).scalar() or 0
        if float(received) > 0:
            return RedirectResponse(
                f"/procurement/po/{po_no}?error=Cannot cancel: goods already received. "
                f"Use Close instead so stock history is preserved.",
                status_code=303)

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
