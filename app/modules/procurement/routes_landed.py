# app/modules/procurement/routes_landed.py

from __future__ import annotations

from datetime import date, datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import text
from sqlalchemy.orm import Session
from urllib.parse import quote

from app.core.rbac import require_area, require_action
from app.core.templates import render
from app.database.session import get_db

router = APIRouter(prefix="/procurement/landed-cost", tags=["Procurement"])

METHODS = [
    ("value", "By value", "Duty, insurance — scales with what the goods are worth"),
    ("weight", "By weight", "Freight, handling — scales with how heavy it is"),
    ("qty", "By quantity", "Flat per-unit charges"),
]

CHARGE_TYPES = ["Freight", "Customs Duty", "Clearing", "Insurance",
                "Port Handling", "Inland Transport", "Other"]


def _cid(request: Request) -> int:
    return int(request.session.get("company_id") or 1)


def ensure_schema(db: Session) -> None:
    try:
        db.execute(text("""
            CREATE TABLE IF NOT EXISTS landed_cost_charges (
                id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                company_id INT NULL,
                grn_no VARCHAR(80) NOT NULL,
                charge_type VARCHAR(60) NOT NULL,
                method VARCHAR(20) NOT NULL DEFAULT 'value',
                amount DECIMAL(18,4) NOT NULL DEFAULT 0,
                supplier_name VARCHAR(255) NULL,
                reference VARCHAR(120) NULL,
                notes VARCHAR(500) NULL,
                status VARCHAR(20) NOT NULL DEFAULT 'Draft',
                created_by VARCHAR(120) NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                applied_at DATETIME NULL,
                KEY idx_lcc_grn (grn_no),
                KEY idx_lcc_status (status)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """))
        db.execute(text("""
            CREATE TABLE IF NOT EXISTS landed_cost_lines (
                id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                company_id INT NULL,
                grn_no VARCHAR(80) NOT NULL,
                inventory_code VARCHAR(80) NOT NULL,
                lot_no VARCHAR(120) NULL,
                received_qty DECIMAL(18,6) NOT NULL DEFAULT 0,
                original_unit_cost DECIMAL(18,6) NOT NULL DEFAULT 0,
                allocated_amount DECIMAL(18,4) NOT NULL DEFAULT 0,
                landed_unit_cost DECIMAL(18,6) NOT NULL DEFAULT 0,
                applied_at DATETIME NULL,
                KEY idx_lcl_grn (grn_no),
                KEY idx_lcl_item (inventory_code)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """))
        db.commit()
    except Exception:
        db.rollback()


def _receipt_lines(db: Session, grn_no: str, cid: int) -> list[dict]:
    """Receipt lines with the weight needed for by-weight allocation."""
    try:
        return [dict(r) for r in db.execute(text("""
            SELECT g.inventory_code,
                   COALESCE(g.item_name, g.inventory_code) AS item_name,
                   COALESCE(g.uom, '')            AS uom,
                   COALESCE(g.lot_no, '')         AS lot_no,
                   COALESCE(g.received_qty, 0)    AS received_qty,
                   COALESCE(g.unit_price, 0)      AS unit_price,
                   -- Weight per unit from the ingredient master. Items with no
                   -- weight recorded fall back to quantity in the by-weight
                   -- method rather than being allocated nothing, which would
                   -- silently under-cost them.
                   COALESCE(i.weight_per_unit, 0) AS weight_per_unit,
                   COALESCE(i.storage_type, '')   AS storage_type
            FROM grn_lines g
            LEFT JOIN ingredients i ON i.ingredient_code = g.inventory_code
            WHERE g.grn_no = :g AND (g.company_id = :cid OR g.company_id IS NULL)
            ORDER BY g.id
        """), {"g": grn_no, "cid": cid}).mappings().all()]
    except Exception:
        # weight_per_unit may not exist on an older ingredient master.
        try:
            return [dict(r) for r in db.execute(text("""
                SELECT g.inventory_code,
                       COALESCE(g.item_name, g.inventory_code) AS item_name,
                       COALESCE(g.uom, '')         AS uom,
                       COALESCE(g.lot_no, '')      AS lot_no,
                       COALESCE(g.received_qty, 0) AS received_qty,
                       COALESCE(g.unit_price, 0)   AS unit_price,
                       0 AS weight_per_unit, '' AS storage_type
                FROM grn_lines g
                WHERE g.grn_no = :g AND (g.company_id = :cid OR g.company_id IS NULL)
                ORDER BY g.id
            """), {"g": grn_no, "cid": cid}).mappings().all()]
        except Exception:
            return []


def allocate(lines: list[dict], charges: list[dict]) -> list[dict]:
   
    out = []
    for ln in lines:
        qty = float(ln["received_qty"] or 0)
        cost = float(ln["unit_price"] or 0)
        out.append({**ln,
                    "line_value": round(qty * cost, 4),
                    "line_weight": round(qty * float(ln.get("weight_per_unit") or 0), 4),
                    "allocated": 0.0})

    for ch in charges:
        amount = float(ch.get("amount") or 0)
        if amount <= 0:
            continue
        method = (ch.get("method") or "value").lower()

        if method == "weight":
            basis = [x["line_weight"] for x in out]
            if sum(basis) <= 0:
                # No weights recorded — fall back to quantity, which is a
                # closer proxy for freight than value is.
                basis = [float(x["received_qty"] or 0) for x in out]
        elif method == "qty":
            basis = [float(x["received_qty"] or 0) for x in out]
        else:
            basis = [x["line_value"] for x in out]

        total = sum(basis)
        if total <= 0:
            
            share = amount / len(out) if out else 0
            for x in out:
                x["allocated"] = round(x["allocated"] + share, 4)
            continue

        running = 0.0
        for i, x in enumerate(out):
            if i == len(out) - 1:
                part = round(amount - running, 4)
            else:
                part = round(amount * basis[i] / total, 4)
                running += part
            x["allocated"] = round(x["allocated"] + part, 4)

    for x in out:
        qty = float(x["received_qty"] or 0)
        x["landed_unit_cost"] = round(
            float(x["unit_price"] or 0) + (x["allocated"] / qty if qty else 0), 6)
        x["uplift_pct"] = round(
            ((x["landed_unit_cost"] / float(x["unit_price"])) - 1) * 100, 2
        ) if float(x["unit_price"] or 0) > 0 else 0.0
    return out


@router.get("")
def landed_home(request: Request, db: Session = Depends(get_db)):
    require_area(request, "procurement")
    ensure_schema(db)
    cid = _cid(request)
    search = (request.query_params.get("search") or "").strip()

    where = "(r.company_id = :cid OR r.company_id IS NULL)"
    params: dict = {"cid": cid}
    if search:
        where += " AND (r.grn_no LIKE :q OR r.supplier_name LIKE :q OR r.po_no LIKE :q)"
        params["q"] = f"%{search}%"

    try:
        receipts = [dict(r) for r in db.execute(text(f"""
            SELECT r.grn_no, r.po_no, r.supplier_name, r.grn_date,
                   COALESCE(l.lines, 0)      AS line_count,
                   COALESCE(l.goods_value, 0) AS goods_value,
                   COALESCE(c.charges, 0)     AS charge_total,
                   COALESCE(c.applied, 0)     AS applied_count
            FROM grn_receipts r
            LEFT JOIN (SELECT grn_no, COUNT(*) AS lines,
                              SUM(COALESCE(received_qty,0)*COALESCE(unit_price,0)) AS goods_value
                       FROM grn_lines GROUP BY grn_no) l ON l.grn_no = r.grn_no
            LEFT JOIN (SELECT grn_no, SUM(amount) AS charges,
                              SUM(status = 'Applied') AS applied
                       FROM landed_cost_charges GROUP BY grn_no) c ON c.grn_no = r.grn_no
            WHERE {where}
            ORDER BY r.id DESC LIMIT 200
        """), params).mappings().all()]
    except Exception:
        receipts = []

    for r in receipts:
        gv = float(r["goods_value"] or 0)
        ct = float(r["charge_total"] or 0)
        r["uplift_pct"] = round(ct / gv * 100, 2) if gv else 0.0

    return render(request, "procurement/landed_cost.html", {
        "receipts": receipts,
        "filters": {"search": search},
        "totals": {
            "receipts": len(receipts),
            "with_charges": sum(1 for r in receipts if float(r["charge_total"] or 0) > 0),
            "charge_value": round(sum(float(r["charge_total"] or 0) for r in receipts), 2),
        },
        "page_title": "Landed Cost",
    })


@router.get("/{grn_no}")
def landed_detail(request: Request, grn_no: str, db: Session = Depends(get_db)):
    require_area(request, "procurement")
    ensure_schema(db)
    cid = _cid(request)

    receipt = db.execute(text("""
        SELECT * FROM grn_receipts WHERE grn_no = :g AND (company_id = :cid OR company_id IS NULL)
    """), {"g": grn_no, "cid": cid}).mappings().first()
    if not receipt:
        return RedirectResponse(
            f"/procurement/landed-cost?toast=danger&title={quote('Not found')}"
            f"&msg={quote(f'Goods receipt {grn_no} not found.')}", status_code=303)

    lines = _receipt_lines(db, grn_no, cid)
    charges = [dict(r) for r in db.execute(text("""
        SELECT * FROM landed_cost_charges WHERE grn_no = :g ORDER BY id
    """), {"g": grn_no}).mappings().all()]

    allocated = allocate(lines, charges) if lines else []
    goods_value = round(sum(x["line_value"] for x in allocated), 2)
    charge_total = round(sum(float(c["amount"] or 0) for c in charges), 2)
    applied = any((c["status"] or "") == "Applied" for c in charges)

    return render(request, "procurement/landed_detail.html", {
        "receipt": dict(receipt), "lines": allocated, "charges": charges,
        "methods": METHODS, "charge_types": CHARGE_TYPES,
        "totals": {
            "goods_value": goods_value,
            "charge_total": charge_total,
            "landed_total": round(goods_value + charge_total, 2),
            "uplift_pct": round(charge_total / goods_value * 100, 2) if goods_value else 0.0,
        },
        "applied": applied,
        "page_title": f"Landed Cost — {grn_no}",
    })


@router.post("/{grn_no}/charge")
async def add_charge(request: Request, grn_no: str, db: Session = Depends(get_db)):
    require_action(request, "procurement", "add")
    ensure_schema(db)
    form = await request.form()
    try:
        amount = float(form.get("amount") or 0)
    except ValueError:
        amount = 0
    if amount <= 0:
        return RedirectResponse(
            f"/procurement/landed-cost/{grn_no}?toast=warning&title={quote('No amount')}"
            f"&msg={quote('Enter a charge amount above zero.')}", status_code=303)

    db.execute(text("""
        INSERT INTO landed_cost_charges
            (company_id, grn_no, charge_type, method, amount, supplier_name, reference, notes,
             status, created_by)
        VALUES (:cid, :g, :t, :m, :a, :s, :r, :n, 'Draft', :by)
    """), {
        "cid": _cid(request), "g": grn_no,
        "t": (form.get("charge_type") or "Other").strip(),
        "m": (form.get("method") or "value").strip().lower(),
        "a": amount,
        "s": (form.get("supplier_name") or "").strip() or None,
        "r": (form.get("reference") or "").strip() or None,
        "n": (form.get("notes") or "").strip()[:500] or None,
        "by": request.session.get("username", "system"),
    })
    db.commit()
    return RedirectResponse(
        f"/procurement/landed-cost/{grn_no}?toast=success&title={quote('Charge Added')}"
        f"&msg={quote('Allocation preview updated. Nothing is posted until you apply it.')}",
        status_code=303)


@router.post("/{grn_no}/charge/{charge_id}/delete")
async def delete_charge(request: Request, grn_no: str, charge_id: int,
                        db: Session = Depends(get_db)):
    require_action(request, "procurement", "delete")
    ensure_schema(db)
    st = db.execute(text("SELECT status FROM landed_cost_charges WHERE id = :i"),
                    {"i": charge_id}).scalar()
    if (st or "") == "Applied":
        return RedirectResponse(
            f"/procurement/landed-cost/{grn_no}?toast=warning&title={quote('Already applied')}"
            f"&msg={quote('This charge has been posted to stock. Reverse the allocation first.')}",
            status_code=303)
    db.execute(text("DELETE FROM landed_cost_charges WHERE id = :i"), {"i": charge_id})
    db.commit()
    return RedirectResponse(f"/procurement/landed-cost/{grn_no}?toast=success"
                            f"&title={quote('Removed')}&msg={quote('Charge removed.')}",
                            status_code=303)


@router.post("/{grn_no}/apply")
async def apply_allocation(request: Request, grn_no: str, db: Session = Depends(get_db)):
    """Post the allocation: update receipt costs and adjust stock valuation."""
    require_action(request, "procurement", "edit")
    ensure_schema(db)
    cid = _cid(request)

    charges = [dict(r) for r in db.execute(text("""
        SELECT * FROM landed_cost_charges WHERE grn_no = :g AND status = 'Draft'
    """), {"g": grn_no}).mappings().all()]
    if not charges:
        return RedirectResponse(
            f"/procurement/landed-cost/{grn_no}?toast=warning&title={quote('Nothing to apply')}"
            f"&msg={quote('Add at least one charge first.')}", status_code=303)

    lines = _receipt_lines(db, grn_no, cid)
    if not lines:
        return RedirectResponse(
            f"/procurement/landed-cost/{grn_no}?toast=danger&title={quote('No receipt lines')}"
            f"&msg={quote('This goods receipt has no lines to allocate against.')}",
            status_code=303)

    allocated = allocate(lines, charges)
    now = datetime.utcnow()
    user = request.session.get("username", "system")

    from app.core.stock_ledger import post_stock_movement

    for x in allocated:
        if x["allocated"] <= 0:
            continue
        db.execute(text("""
            INSERT INTO landed_cost_lines
                (company_id, grn_no, inventory_code, lot_no, received_qty,
                 original_unit_cost, allocated_amount, landed_unit_cost, applied_at)
            VALUES (:cid, :g, :c, :l, :q, :oc, :aa, :lc, :at)
        """), {"cid": cid, "g": grn_no, "c": x["inventory_code"], "l": x["lot_no"] or None,
               "q": x["received_qty"], "oc": x["unit_price"],
               "aa": x["allocated"], "lc": x["landed_unit_cost"], "at": now})

        # The receipt line now carries the true landed cost, so any later
        # valuation reading grn_lines sees the same figure as the ledger.
        db.execute(text("""
            UPDATE grn_lines SET unit_price = :lc
            WHERE grn_no = :g AND inventory_code = :c
        """), {"lc": x["landed_unit_cost"], "g": grn_no, "c": x["inventory_code"]})

        # A zero-quantity valuation adjustment. Quantity must NOT change — the
        # goods already arrived and were counted; only what they cost changed.
        # Posting a quantity here would inflate stock on every allocation.
        post_stock_movement(
            db, company_id=cid, inventory_code=x["inventory_code"],
            item_name=x["item_name"], uom=x["uom"], qty=0,
            movement_type="LANDED_COST_ADJ", reference_no=grn_no,
            unit_cost=x["landed_unit_cost"],
            remarks=(f"Landed cost {x['allocated']:.2f} allocated — unit cost "
                     f"{x['unit_price']:.4f} → {x['landed_unit_cost']:.4f}"),
            created_by=user, lot_no=x["lot_no"] or "", qc_status="Passed",
        )

        # Keep the ingredient master's standard cost aligned, so future recipe
        # costing uses the landed figure rather than the invoice price.
        try:
            db.execute(text("""
                UPDATE ingredients SET unit_cost_standard = :lc
                WHERE ingredient_code = :c
            """), {"lc": x["landed_unit_cost"], "c": x["inventory_code"]})
        except Exception:
            db.rollback()

    db.execute(text("""
        UPDATE landed_cost_charges SET status = 'Applied', applied_at = :at
        WHERE grn_no = :g AND status = 'Draft'
    """), {"at": now, "g": grn_no})
    db.commit()

    total = sum(float(c["amount"] or 0) for c in charges)
    return RedirectResponse(
        f"/procurement/landed-cost/{grn_no}?toast=success&title={quote('Landed Cost Applied')}"
        f"&msg={quote(f'{total:.2f} allocated across {len(allocated)} line(s). Stock valuation and standard costs updated.')}",
        status_code=303)


@router.post("/{grn_no}/reverse")
async def reverse_allocation(request: Request, grn_no: str, db: Session = Depends(get_db)):
    """Undo an applied allocation, restoring the original invoice costs.

    Reversal exists because landed cost is often applied before the freight
    invoice actually arrives, using an estimate. When the real figure lands you
    need to correct it, and correcting by adding a second charge on top of an
    estimate compounds the error.
    """
    require_action(request, "procurement", "edit")
    ensure_schema(db)
    cid = _cid(request)

    rows = [dict(r) for r in db.execute(text("""
        SELECT * FROM landed_cost_lines WHERE grn_no = :g AND applied_at IS NOT NULL
    """), {"g": grn_no}).mappings().all()]
    if not rows:
        return RedirectResponse(
            f"/procurement/landed-cost/{grn_no}?toast=warning&title={quote('Nothing applied')}"
            f"&msg={quote('There is no applied allocation to reverse.')}", status_code=303)

    from app.core.stock_ledger import post_stock_movement
    user = request.session.get("username", "system")

    for r in rows:
        db.execute(text("""
            UPDATE grn_lines SET unit_price = :oc
            WHERE grn_no = :g AND inventory_code = :c
        """), {"oc": r["original_unit_cost"], "g": grn_no, "c": r["inventory_code"]})
        post_stock_movement(
            db, company_id=cid, inventory_code=r["inventory_code"],
            item_name=r["inventory_code"], uom="", qty=0,
            movement_type="LANDED_COST_REV", reference_no=grn_no,
            unit_cost=float(r["original_unit_cost"] or 0),
            remarks=f"Landed cost reversed — unit cost restored to {float(r['original_unit_cost'] or 0):.4f}",
            created_by=user, lot_no=r["lot_no"] or "", qc_status="Passed",
        )
        try:
            db.execute(text("UPDATE ingredients SET unit_cost_standard = :oc "
                            "WHERE ingredient_code = :c"),
                       {"oc": r["original_unit_cost"], "c": r["inventory_code"]})
        except Exception:
            db.rollback()

    db.execute(text("DELETE FROM landed_cost_lines WHERE grn_no = :g"), {"g": grn_no})
    db.execute(text("UPDATE landed_cost_charges SET status = 'Draft', applied_at = NULL "
                    "WHERE grn_no = :g"), {"g": grn_no})
    db.commit()
    return RedirectResponse(
        f"/procurement/landed-cost/{grn_no}?toast=success&title={quote('Allocation Reversed')}"
        f"&msg={quote('Original invoice costs restored. The charges are back in draft so you can correct and re-apply.')}",
        status_code=303)
