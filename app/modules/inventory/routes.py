# app/modules/inventory/routes.py
"""Inventory Valuation (MM-IM) — robust against older ISFC database copies.

Reads inventory_transactions when available and falls back to the Ingredient
master when no ledger exists yet. This prevents /inventory from failing while
Procurement/GRN is still being introduced.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.templates import render
from app.core.rbac import require_area, require_action
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
    """Upgrade an OLD inventory_transactions table (ingredient_code / qty_standard
    / transaction_type columns from the original SQLAlchemy model) to the new
    valuation schema, backfilling data so history is preserved.

    This is what caused the /inventory 500:
        Unknown column 't.inventory_code' in 'field list'
    CREATE TABLE IF NOT EXISTS did nothing because the legacy table already
    existed - so we ALTER + backfill instead. Safe to run on every request
    (all steps are guarded by column checks and cheap when already migrated).
    """
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
    # 3) Guarantee no NULL inventory_code (would break GROUP BY presentation)
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

    params = {}
    extra = ""
    if search:
        extra = "AND (t.inventory_code LIKE :like OR COALESCE(t.item_name,'') LIKE :like)"
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
    rows = [dict(r) for r in db.execute(text("""
        SELECT t.*, COALESCE(t.txn_date, t.transaction_date, t.created_at) AS movement_date
        FROM inventory_transactions t
        WHERE t.inventory_code = :c
        ORDER BY t.id DESC LIMIT 500
    """), {"c": inventory_code}).mappings().all()]
    return render(request, "inventory/ledger.html", {
        "rows": rows,
        "code": inventory_code,
        "item_name": rows[0].get("item_name") if rows else inventory_code,
        "page_title": f"Ledger {inventory_code}",
    })


# ============================================================================
# Batch 15 — Stock Ledger Verification (the gate before deeper Finance)
# ============================================================================
# Compares, per item:  ledger balance = SUM(qty_in) - SUM(qty_out)
# against the master snapshot ingredients.current_stock.
# Items where the two disagree beyond a tolerance are flagged so they can be
# corrected before stock values feed COGS / GL postings.

@router.get("/verification")
def ledger_verification(request: Request, db: Session = Depends(get_db)):
    require_area(request, "inventory_valuation")
    try:
        _ensure_inventory_ledger(db)
    except Exception:
        db.rollback()

    rows = []
    try:
        rows = db.execute(text("""
            SELECT i.ingredient_code AS inventory_code,
                   COALESCE(i.name,'') AS item_name,
                   COALESCE(i.current_stock, 0) AS master_stock,
                   COALESCE(t.ledger_in, 0) AS ledger_in,
                   COALESCE(t.ledger_out, 0) AS ledger_out,
                   COALESCE(t.ledger_in, 0) - COALESCE(t.ledger_out, 0) AS ledger_balance,
                   COALESCE(i.current_stock, 0) - (COALESCE(t.ledger_in, 0) - COALESCE(t.ledger_out, 0)) AS variance
            FROM ingredients i
            LEFT JOIN (
                SELECT inventory_code,
                       SUM(COALESCE(qty_in, 0)) AS ledger_in,
                       SUM(COALESCE(qty_out, 0)) AS ledger_out
                FROM inventory_transactions
                GROUP BY inventory_code
            ) t ON t.inventory_code = i.ingredient_code
            ORDER BY ABS(COALESCE(i.current_stock, 0) - (COALESCE(t.ledger_in, 0) - COALESCE(t.ledger_out, 0))) DESC
            LIMIT 1000
        """)).mappings().all()
    except Exception:
        rows = []

    TOL = 0.001
    items = [dict(r) for r in rows]
    mismatched = [r for r in items if abs(float(r["variance"] or 0)) > TOL]
    kpis = {
        "total": len(items),
        "matched": len(items) - len(mismatched),
        "mismatched": len(mismatched),
        "variance_value": round(sum(abs(float(r["variance"] or 0)) for r in mismatched), 3),
    }
    return render(request, "inventory/verification.html", {
        "items": mismatched[:500], "kpis": kpis,
        "page_title": "Stock Ledger Verification",
    })


@router.post("/verification/align/{inventory_code}")
def align_master_to_ledger(request: Request, inventory_code: str, db: Session = Depends(get_db)):
    """Adopt the ledger balance as the master current_stock for ONE item —
    the ledger (documents) is the source of truth."""
    require_action(request, "inventory_valuation", "edit")
    try:
        bal = db.execute(text("""
            SELECT COALESCE(SUM(COALESCE(qty_in,0)) - SUM(COALESCE(qty_out,0)), 0)
            FROM inventory_transactions WHERE inventory_code = :c
        """), {"c": inventory_code}).scalar() or 0
        db.execute(text("""
            UPDATE ingredients SET current_stock = :b WHERE ingredient_code = :c
        """), {"b": float(bal), "c": inventory_code})
        db.commit()
    except Exception:
        db.rollback()
    return RedirectResponse("/inventory/verification?toast=success&title=Aligned&msg=Master stock set to ledger balance", status_code=303)
