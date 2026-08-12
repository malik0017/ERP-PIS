# app/modules/inventory/routes.py
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.templates import render
from app.core.rbac import require_area, require_action
from app.core.company import get_current_company_id
from app.database.session import get_db

router = APIRouter(prefix="/inventory", tags=["Inventory"])


def _table_exists(db: Session, name: str) -> bool:
    return bool(db.execute(text("""
        SELECT COUNT(*) FROM information_schema.tables
        WHERE table_schema = DATABASE() AND table_name = :name
    """), {"name": name}).scalar())


def _column_exists(db: Session, table: str, column: str) -> bool:
    return bool(db.execute(text("""
        SELECT COUNT(*) FROM information_schema.columns
        WHERE table_schema = DATABASE() AND table_name = :table AND column_name = :column
    """), {"table": table, "column": column}).scalar())


def _ensure_inventory_ledger(db: Session) -> None:
    db.execute(text("""
        CREATE TABLE IF NOT EXISTS inventory_transactions (
            id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
            company_id INT NULL,
            txn_date DATETIME NULL DEFAULT CURRENT_TIMESTAMP,
            transaction_date DATETIME NULL DEFAULT CURRENT_TIMESTAMP,
            inventory_code VARCHAR(80) NOT NULL,
            item_name VARCHAR(255) NULL,
            uom VARCHAR(50) NULL,
            qty_in DECIMAL(18,6) NOT NULL DEFAULT 0,
            qty_out DECIMAL(18,6) NOT NULL DEFAULT 0,
            unit_cost DECIMAL(18,6) NOT NULL DEFAULT 0,
            movement_type VARCHAR(60) NULL,
            reference_no VARCHAR(120) NULL,
            remarks TEXT NULL,
            created_by VARCHAR(120) NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            KEY idx_inv_txn_code (inventory_code),
            KEY idx_inv_txn_ref (reference_no),
            KEY idx_inv_txn_type (movement_type)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """))
    db.commit()
    _migrate_legacy_ledger(db)


def _migrate_legacy_ledger(db: Session) -> None:
    table = "inventory_transactions"
    # 1) Add any missing new columns
    new_cols = {
        "inventory_code": "VARCHAR(80) NULL",
        "item_name": "VARCHAR(255) NULL",
        "uom": "VARCHAR(50) NULL",
        "qty_in": "DECIMAL(18,6) NOT NULL DEFAULT 0",
        "qty_out": "DECIMAL(18,6) NOT NULL DEFAULT 0",
        "unit_cost": "DECIMAL(18,6) NOT NULL DEFAULT 0",
        "movement_type": "VARCHAR(60) NULL",
        "txn_date": "DATETIME NULL",
        "company_id": "INT NULL",
        "reference_no": "VARCHAR(120) NULL",
        "remarks": "TEXT NULL",
        "created_by": "VARCHAR(120) NULL",
        "created_at": "DATETIME NULL DEFAULT CURRENT_TIMESTAMP",
    }
    changed = False
    for col, ddl in new_cols.items():
        if not _column_exists(db, table, col):
            db.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}"))
            changed = True

    has_legacy_code = _column_exists(db, table, "ingredient_code")
    has_legacy_qty = _column_exists(db, table, "qty_standard")
    has_legacy_type = _column_exists(db, table, "transaction_type")

    # 2) Backfill new columns from legacy data (only rows not yet migrated)
    if has_legacy_code:
        db.execute(text(f"""
            UPDATE {table}
            SET inventory_code = ingredient_code
            WHERE (inventory_code IS NULL OR inventory_code = '') AND ingredient_code IS NOT NULL
        """))
        if _column_exists(db, table, "ingredient_name"):
            db.execute(text(f"""
                UPDATE {table}
                SET item_name = ingredient_name
                WHERE (item_name IS NULL OR item_name = '') AND ingredient_name IS NOT NULL
            """))
    if _column_exists(db, table, "standard_uom"):
        db.execute(text(f"""
            UPDATE {table} SET uom = standard_uom
            WHERE (uom IS NULL OR uom = '') AND standard_uom IS NOT NULL
        """))
    if has_legacy_qty and has_legacy_type:
        # IN movements: GRN / Return / positive Adjustment. OUT: Issue / Waste / Transfer.
        db.execute(text(f"""
            UPDATE {table}
            SET qty_in  = CASE WHEN UPPER(COALESCE(transaction_type,'')) IN ('GRN','GRN_IN','RETURN','ADJUSTMENT_IN','OPENING') THEN ABS(COALESCE(qty_standard,0)) ELSE qty_in END,
                qty_out = CASE WHEN UPPER(COALESCE(transaction_type,'')) IN ('ISSUE','WASTE','TRANSFER','ADJUSTMENT_OUT','STORE_ISSUE') THEN ABS(COALESCE(qty_standard,0)) ELSE qty_out END,
                movement_type = COALESCE(NULLIF(movement_type,''), transaction_type)
            WHERE COALESCE(qty_in,0)=0 AND COALESCE(qty_out,0)=0 AND COALESCE(qty_standard,0) <> 0
        """))
    if _column_exists(db, table, "transaction_date"):
        db.execute(text(f"""
            UPDATE {table} SET txn_date = transaction_date
            WHERE txn_date IS NULL AND transaction_date IS NOT NULL
        """))
    db.execute(text(f"UPDATE {table} SET inventory_code = CONCAT('LEGACY-', id) WHERE inventory_code IS NULL OR inventory_code = ''"))
    if changed:
        try:
            db.execute(text(f"ALTER TABLE {table} ADD KEY idx_inv_txn_code (inventory_code)"))
        except Exception:
            pass  # index may already exist
    db.commit()


def _ingredient_rows(db: Session, search: str) -> list[dict]:
    params = {}
    extra = ""
    if search:
        extra = "WHERE ingredient_code LIKE :like OR name LIKE :like OR COALESCE(category,'') LIKE :like"
        params["like"] = f"%{search}%"
    return [dict(r) for r in db.execute(text(f"""
        SELECT ingredient_code AS inventory_code,
               name AS item_name,
               COALESCE(standard_uom, purchase_uom, recipe_uom, '') AS uom,
               0 AS total_in,
               0 AS total_out,
               0 AS on_hand,
               COALESCE(unit_cost_standard,0) AS avg_cost,
               0 AS stock_value,
               updated_at AS last_movement
        FROM ingredients {extra}
        ORDER BY ingredient_code
        LIMIT 1000
    """), params).mappings().all()]


@router.get("")
def stock_valuation(request: Request, db: Session = Depends(get_db)):
    require_area(request, "inventory_valuation")
    try:
        _ensure_inventory_ledger(db)
    except Exception:
        db.rollback()
    q = request.query_params
    search = (q.get("search") or "").strip()
    cid = get_current_company_id(request)

    params = {"cid": cid}
    extra = "AND (t.company_id = :cid OR t.company_id IS NULL)"
    if search:
        extra += " AND (t.inventory_code LIKE :like OR COALESCE(t.item_name,'') LIKE :like)"
        params["like"] = f"%{search}%"

    if not _table_exists(db, "inventory_transactions"):
        rows = _ingredient_rows(db, search)
    else:
        rows = [dict(r) for r in db.execute(text(f"""
            SELECT
                t.inventory_code,
                COALESCE(MAX(NULLIF(t.item_name,'')), t.inventory_code) AS item_name,
                COALESCE(MAX(NULLIF(t.uom,'')), '') AS uom,
                ROUND(SUM(COALESCE(t.qty_in,0)), 4) AS total_in,
                ROUND(SUM(COALESCE(t.qty_out,0)), 4) AS total_out,
                ROUND(SUM(COALESCE(t.qty_in,0)) - SUM(COALESCE(t.qty_out,0)), 4) AS on_hand,
                -- Batch 93: informational only — how much of on_hand is
                -- still sitting in QC Hold, not yet cleared for use.
                -- Kept separate from on_hand/stock_value here since this
                -- page is a financial/valuation view (everything received
                -- is a real owned asset regardless of QC status); the
                -- actual availability GATE for production is enforced in
                -- production_service.py's shortage check, not here.
                ROUND(SUM(CASE WHEN t.qc_status = 'Pending' THEN COALESCE(t.qty_in,0) ELSE 0 END), 4) AS qc_hold_qty,
                ROUND(CASE WHEN SUM(COALESCE(t.qty_in,0)) > 0
                      THEN SUM(COALESCE(t.qty_in,0) * COALESCE(t.unit_cost,0)) / SUM(COALESCE(t.qty_in,0))
                      ELSE 0 END, 4) AS avg_cost,
                ROUND((SUM(COALESCE(t.qty_in,0)) - SUM(COALESCE(t.qty_out,0))) *
                      CASE WHEN SUM(COALESCE(t.qty_in,0)) > 0
                      THEN SUM(COALESCE(t.qty_in,0) * COALESCE(t.unit_cost,0)) / SUM(COALESCE(t.qty_in,0))
                      ELSE 0 END, 2) AS stock_value,
                MAX(COALESCE(t.txn_date, t.transaction_date, t.created_at)) AS last_movement
            FROM inventory_transactions t
            WHERE 1=1 {extra}
            GROUP BY t.inventory_code
            ORDER BY stock_value DESC, t.inventory_code
            LIMIT 1000
        """), params).mappings().all()]
        if not rows and _table_exists(db, "ingredients"):
            rows = _ingredient_rows(db, search)

    kpis = {
        "items": len(rows),
        "on_hand_lines": sum(1 for r in rows if float(r.get("on_hand") or 0) > 0),
        "negative": sum(1 for r in rows if float(r.get("on_hand") or 0) < 0),
        "value": round(sum(float(r.get("stock_value") or 0) for r in rows), 2),
    }
    top = [r for r in rows if float(r.get("stock_value") or 0) > 0][:10]
    return render(request, "inventory/index.html", {
        "rows": rows, "kpis": kpis,
        "chart_labels": [r.get("inventory_code") for r in top],
        "chart_values": [float(r.get("stock_value") or 0) for r in top],
        "filters": {"search": search, "status": "", "from_date": "", "to_date": ""},
        "status_options": [],
        "page_title": "Inventory Valuation",
    })


@router.get("/ledger/{inventory_code}")
def item_ledger(request: Request, inventory_code: str, db: Session = Depends(get_db)):
    require_area(request, "inventory_valuation")
    _ensure_inventory_ledger(db)
    cid = get_current_company_id(request)
    rows = [dict(r) for r in db.execute(text("""
        SELECT t.*, COALESCE(t.txn_date, t.transaction_date, t.created_at) AS movement_date
        FROM inventory_transactions t
        WHERE t.inventory_code = :c
          AND (t.company_id = :cid OR t.company_id IS NULL)
        ORDER BY t.id DESC LIMIT 500
    """), {"c": inventory_code, "cid": cid}).mappings().all()]

    running: list[float] = []
    balance = 0.0
    for r in reversed(rows):                      # oldest -> newest
        try:
            balance += float(r.get("qty_in") or 0) - float(r.get("qty_out") or 0)
        except (TypeError, ValueError):
            pass
        running.append(round(balance, 4))
    running.reverse()                             # back to newest -> oldest

    return render(request, "inventory/ledger.html", {
        "rows": rows,
        "running": running,
        "code": inventory_code,
        "item_name": rows[0].get("item_name") if rows else inventory_code,
        "page_title": f"Ledger {inventory_code}",
    })

@router.get("/verification")
def ledger_verification(request: Request, db: Session = Depends(get_db)):
    require_area(request, "inventory_valuation")
    try:
        _ensure_inventory_ledger(db)
    except Exception:
        db.rollback()

    rows = []
    cid = get_current_company_id(request)
    try:
        rows = db.execute(text("""
            SELECT
                t.inventory_code,
                COALESCE(MAX(NULLIF(t.item_name,'')), t.inventory_code) AS item_name,
                ROUND(SUM(COALESCE(t.qty_in,0)), 4) AS ledger_in,
                ROUND(SUM(COALESCE(t.qty_out,0)), 4) AS ledger_out,
                ROUND(SUM(COALESCE(t.qty_in,0)) - SUM(COALESCE(t.qty_out,0)), 4) AS ledger_balance
            FROM inventory_transactions t
            WHERE (t.company_id = :cid OR t.company_id IS NULL)
            GROUP BY t.inventory_code
            HAVING ledger_balance < -0.001
            ORDER BY ledger_balance ASC
            LIMIT 500
        """), {"cid": cid}).mappings().all()
    except Exception:
        rows = []

    total_items = db.execute(text("""
        SELECT COUNT(DISTINCT inventory_code) FROM inventory_transactions
        WHERE (company_id = :cid OR company_id IS NULL)
    """), {"cid": cid}).scalar() or 0

    items = [dict(r) for r in rows]
    kpis = {
        "total": int(total_items),
        "matched": int(total_items) - len(items),
        "mismatched": len(items),
        "variance_value": round(sum(abs(float(r["ledger_balance"] or 0)) for r in items), 3),
    }
    return render(request, "inventory/verification.html", {
        "items": items, "kpis": kpis,
        "page_title": "Stock Ledger Verification",
    })


@router.post("/verification/align/{inventory_code}")
def align_master_to_ledger(request: Request, inventory_code: str, db: Session = Depends(get_db)):

    require_action(request, "inventory_valuation", "edit")
    return RedirectResponse(
        "/inventory/verification?toast=warning&title=Use a Real Correction&msg="
        "A negative balance needs an actual correcting transaction (a GRN or a store-issuance adjustment) — "
        "there's no longer a separate master figure to just overwrite.",
        status_code=303)
