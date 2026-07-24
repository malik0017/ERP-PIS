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
    commit: bool = False,
) -> bool:
    """Write ONE stock movement, compatible with the legacy AND new schema.

    `qty` is always POSITIVE; direction comes from `movement_type`
    (see IN_TYPES). Returns True when the row was written.
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
