# app/core/stock_ledger.py
# =============================================================================
# Batch 23 — SINGLE SOURCE OF TRUTH FOR STOCK MOVEMENTS
# -----------------------------------------------------------------------------
# ROOT CAUSE THIS FIXES
# ---------------------
# `inventory_transactions` exists in TWO shapes in the wild:
#
#   LEGACY (from app/models/inventory.py, created by SQLAlchemy on old installs)
#       transaction_no   VARCHAR(80)  NOT NULL UNIQUE   <-- no default!
#       ingredient_code  VARCHAR(50)  NOT NULL          <-- no default!
#       ingredient_name  VARCHAR(255) NOT NULL          <-- no default!
#       transaction_type VARCHAR(50)  NOT NULL          <-- no default!
#       qty_standard, standard_uom, transaction_date, ...
#
#   NEW (valuation schema used by the inventory screens)
#       inventory_code, item_name, uom, qty_in, qty_out, unit_cost,
#       movement_type, txn_date, reference_no, ...
#
# Procurement's GRN used to INSERT only the NEW columns. On a database with the
# LEGACY table those four NOT NULL columns got no value, MySQL rejected the row,
# and the INSERT was wrapped in `except Exception: pass` — so the PO flipped to
# "RECEIVED" while the stock ledger stayed empty. That is exactly why the PO
# register showed RECEIVED but Stock on Hand showed 0 and "Items in Stock 0".
#
# THE FIX
# -------
# post_stock_movement() inspects the REAL columns of the table at runtime and
# fills every column that exists — new names AND legacy names — so the row is
# accepted on either schema. It returns True/False and logs failures instead of
# swallowing them, so a broken ledger can never again hide behind a green
# "RECEIVED" badge.
#
# Every module that moves stock (procurement GRN, store issuance, kitchen
# transfers, adjustments) should call THIS function.
# =============================================================================

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# Movement types that ADD stock. Anything else is treated as an OUT movement.
IN_TYPES = {
    "GRN_IN", "GRN", "RETURN", "ADJUSTMENT_IN", "OPENING", "OPENING_STOCK",
    "PRODUCTION_IN", "TRANSFER_IN",
}


def _columns(db: Session, table: str = "inventory_transactions") -> set[str]:
    """Real column names of the ledger table (empty set if it doesn't exist)."""
    try:
        rows = db.execute(text(f"SHOW COLUMNS FROM {table}")).mappings().all()
        return {r["Field"] for r in rows}
    except Exception:
        return set()


def ensure_qc_status_column(db: Session) -> None:
    """Batch 93 — adds inventory_transactions.qc_status if it's not there
    yet. Existing rows backfill to 'Passed' (the DB-level DEFAULT), so
    every movement posted before this migration is treated as already
    cleared — nothing already in stock gets newly held. Only NEW GRN
    receipts posted after this migration go through the gate.
    """
    try:
        exists = db.execute(text("""
            SELECT COUNT(*) FROM information_schema.columns
            WHERE table_schema = DATABASE() AND table_name = 'inventory_transactions' AND column_name = 'qc_status'
        """)).scalar()
        if not exists:
            db.execute(text(
                "ALTER TABLE inventory_transactions ADD COLUMN qc_status VARCHAR(20) DEFAULT 'Passed'"
            ))
            db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass


def ensure_ledger_schema(db: Session) -> None:
    """Batch 94 — repair a LEGACY-shaped inventory_transactions table.

    THE BUG THIS FIXES (found while testing this batch against a fresh
    database, not reported):

    Two different definitions of this table exist in the codebase and they
    disagree with each other.

      * app/models/inventory.py::InventoryTransaction declares the ORIGINAL
        shape:  transaction_no / ingredient_code / ingredient_name /
                transaction_type / qty_standard / standard_uom
      * _ensure_procurement_schema() declares the CURRENT shape:
                company_id / inventory_code / item_name / uom /
                qty_in / qty_out / unit_cost / movement_type

    Whichever one runs first on a given database wins permanently, because
    CREATE TABLE IF NOT EXISTS silently does nothing when the table already
    exists. On an established database the raw-SQL version ran first years
    ago and everything is fine. But on a FRESH database created through
    init_db.py / init_production_db.py — which is exactly what happens on a
    new machine after a git clone — Base.metadata.create_all() runs first
    and produces the legacy shape. From that point on every stock READ in
    the system fails with "Unknown column 'qty_in'": inventory valuation,
    the BOM shortage check, the dashboards, all of it. Writes appear to work
    (post_stock_movement is schema-adaptive and fills whatever exists), so
    the failure looks like "the reports are broken" rather than "the table
    is wrong".

    This adds the missing modern columns to a legacy table and backfills
    them from the legacy ones, so both shapes converge on the current one.
    Safe to run on every startup: each column is checked individually
    against information_schema first (ADD COLUMN IF NOT EXISTS is not
    supported on the target MySQL version), and the backfill only touches
    rows where the new column is still NULL.
    """
    cols = _columns(db)
    if not cols:
        return  # table doesn't exist yet — the CREATE TABLE path will make it correctly

    additions = {
        "company_id": "INT NULL",
        "inventory_code": "VARCHAR(80) NULL",
        "item_name": "VARCHAR(255) NULL",
        "uom": "VARCHAR(50) NULL",
        "qty_in": "DECIMAL(18,6) NOT NULL DEFAULT 0",
        "qty_out": "DECIMAL(18,6) NOT NULL DEFAULT 0",
        "unit_cost": "DECIMAL(18,6) NOT NULL DEFAULT 0",
        "movement_type": "VARCHAR(60) NULL",
        "txn_date": "DATETIME NULL DEFAULT CURRENT_TIMESTAMP",
        "created_by": "VARCHAR(120) NULL",
        "created_at": "DATETIME DEFAULT CURRENT_TIMESTAMP",
    }
    added = []
    for col, ddl in additions.items():
        if col in cols:
            continue
        try:
            db.execute(text(f"ALTER TABLE inventory_transactions ADD COLUMN {col} {ddl}"))
            added.append(col)
        except Exception:
            db.rollback()
    if added:
        db.commit()

    if not added:
        return

    # Backfill the newly added columns from their legacy equivalents, so
    # historical movements stay visible to the modern read queries instead
    # of silently reading as zero.
    cols = _columns(db)
    try:
        if "inventory_code" in added and "ingredient_code" in cols:
            db.execute(text("UPDATE inventory_transactions SET inventory_code = ingredient_code "
                            "WHERE inventory_code IS NULL"))
        if "item_name" in added and "ingredient_name" in cols:
            db.execute(text("UPDATE inventory_transactions SET item_name = ingredient_name "
                            "WHERE item_name IS NULL"))
        if "uom" in added and "standard_uom" in cols:
            db.execute(text("UPDATE inventory_transactions SET uom = standard_uom WHERE uom IS NULL"))
        if "movement_type" in added and "transaction_type" in cols:
            db.execute(text("UPDATE inventory_transactions SET movement_type = transaction_type "
                            "WHERE movement_type IS NULL"))
        if "txn_date" in added and "transaction_date" in cols:
            db.execute(text("UPDATE inventory_transactions SET txn_date = transaction_date "
                            "WHERE txn_date IS NULL"))
        if "created_by" in added and "performed_by" in cols:
            db.execute(text("UPDATE inventory_transactions SET created_by = performed_by "
                            "WHERE created_by IS NULL"))

        # Direction comes from the movement type, exactly as post_stock_movement
        # decides it — the legacy schema stored one unsigned qty_standard plus a
        # type, so in/out has to be reconstructed rather than copied.
        if ("qty_in" in added or "qty_out" in added) and "qty_standard" in cols and "transaction_type" in cols:
            in_list = ", ".join(f"'{t}'" for t in sorted(IN_TYPES))
            db.execute(text(f"""
                UPDATE inventory_transactions
                SET qty_in  = CASE WHEN UPPER(COALESCE(transaction_type,'')) IN ({in_list})
                                   THEN COALESCE(qty_standard, 0) ELSE 0 END,
                    qty_out = CASE WHEN UPPER(COALESCE(transaction_type,'')) IN ({in_list})
                                   THEN 0 ELSE COALESCE(qty_standard, 0) END
                WHERE COALESCE(qty_in, 0) = 0 AND COALESCE(qty_out, 0) = 0
            """))
        db.commit()
    except Exception:
        db.rollback()


def next_txn_no(db: Session, prefix: str = "TXN") -> str:
    """Unique transaction_no for the legacy NOT NULL UNIQUE column."""
    stamp = datetime.now().strftime("%Y%m%d%H%M%S%f")[:-3]
    return f"{prefix}-{stamp}"


def post_stock_movement(
    db: Session,
    *,
    company_id: int,
    inventory_code: str,
    item_name: str,
    uom: str,
    qty: float,
    movement_type: str,
    reference_no: str = "",
    unit_cost: float = 0.0,
    remarks: str = "",
    created_by: str = "system",
    lot_no: str = "",
    from_location: str = "",
    to_location: str = "",
    qc_status: str = "Passed",
    commit: bool = False,
) -> bool:
    """Write ONE stock movement, compatible with the legacy AND new schema.

    `qty` is always POSITIVE; direction comes from `movement_type`
    (see IN_TYPES). Returns True when the row was written.

    `qc_status` (Batch 93): defaults to "Passed" so every existing caller
    is completely unaffected. Goods receipt is the one caller that should
    explicitly pass "Pending" — that's what puts a GRN's stock into
    quarantine, excluded from "available" until Incoming QC inspects it.
    See ensure_qc_status_column() / the /qc/inspection screen.
    """
    if not inventory_code or not qty:
        return False

    cols = _columns(db)
    if not cols:
        logger.error("stock_ledger: inventory_transactions table not found")
        return False

    qty = abs(float(qty))
    is_in = movement_type.upper() in IN_TYPES
    now = datetime.now()

    # Build the row from every column name this table might have.
    candidate: dict[str, object] = {
        # --- new valuation schema ---
        "company_id": company_id,
        "inventory_code": inventory_code,
        "item_name": item_name or inventory_code,
        "uom": uom or "",
        "qty_in": qty if is_in else 0,
        "qty_out": 0 if is_in else qty,
        "unit_cost": unit_cost or 0,
        "movement_type": movement_type,
        "txn_date": now,
        "reference_no": reference_no or "",
        "remarks": remarks or "",
        "created_by": created_by or "system",
        "created_at": now,
        "qc_status": qc_status or "Passed",
        # --- legacy schema (NOT NULL on old installs) ---
        "transaction_no": next_txn_no(db),
        "transaction_date": now,
        "ingredient_code": inventory_code,
        "ingredient_name": item_name or inventory_code,
        "transaction_type": movement_type,
        "qty_standard": qty if is_in else -qty,
        "standard_uom": uom or "Kg",
        "lot_no": lot_no or None,
        "from_location": from_location or None,
        "to_location": to_location or None,
        "reference_type": movement_type,
        "performed_by": created_by or "system",
    }

    payload = {k: v for k, v in candidate.items() if k in cols}
    if not payload:
        logger.error("stock_ledger: no matching columns for insert")
        return False

    collist = ", ".join(payload.keys())
    binds = ", ".join(f":{k}" for k in payload.keys())
    try:
        db.execute(text(
            f"INSERT INTO inventory_transactions ({collist}) VALUES ({binds})"
        ), payload)
        if commit:
            db.commit()
        return True
    except Exception as exc:
        # Do NOT swallow silently — this is what hid the bug before.
        logger.error("stock_ledger: failed to post %s for %s: %s",
                     movement_type, inventory_code, exc)
        if commit:
            db.rollback()
        return False
