# app/core/kitchen_production.py
# =============================================================================
# Batch 72 — KITCHEN PRODUCTION state machine (P1)
# -----------------------------------------------------------------------------
# The existing kitchen layer tracks INGREDIENT lines (receive → process → waste →
# transfer, each ingredient independently). What was missing is the PRODUCT-level
# flow a real kitchen actually runs:
#
#   For each RECIPE in a section:
#     PENDING  → (receive all its ingredients) → RECEIVED
#              → (chef cooks it as ONE final product)    → PRODUCED
#              → (move that finished product forward)     → TRANSFERRED
#
# This module adds that state machine ON TOP of the ingredient transactions
# without changing them. One row per (order_no, section, recipe_no) in
# `kitchen_production`. The ingredient-level receive/waste/return still works;
# "Produce Final Product" simply rolls all of a recipe's received ingredients
# into a finished-product record and marks them processed, and "Transfer" moves
# the whole product to the next section (re-homing its ingredient rows so the
# next section sees them).
#
# The section route order (who comes after whom) is defined in SECTION_ROUTE.
# =============================================================================

from __future__ import annotations

from datetime import datetime
from sqlalchemy import text
from sqlalchemy.orm import Session

# Canonical production route. A produced item flows to the next section, and
# the last kitchen section hands off to QC → Packing → Dispatch.
SECTION_ROUTE = [
    "Cutting", "Butchery", "Hot Kitchen", "Cold Kitchen", "Bakery/Pastry",
    "QC", "Trayline / Packing", "Dispatch",
]

# States
PENDING = "PENDING"
RECEIVED = "RECEIVED"
PRODUCED = "PRODUCED"
TRANSFERRED = "TRANSFERRED"


def next_section(current: str) -> str:
    """The default next stop after `current`. Falls back to QC."""
    try:
        i = SECTION_ROUTE.index(current)
        return SECTION_ROUTE[i + 1] if i + 1 < len(SECTION_ROUTE) else "QC"
    except ValueError:
        return "QC"


def ensure_schema(db: Session) -> None:
    try:
        db.execute(text("""
            CREATE TABLE IF NOT EXISTS kitchen_production (
                id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                company_id INT NULL,
                order_no VARCHAR(80) NOT NULL,
                section VARCHAR(100) NOT NULL,
                recipe_no VARCHAR(50) NULL,
                recipe_name VARCHAR(255) NULL,
                planned_portions DECIMAL(18,4) NOT NULL DEFAULT 0,
                produced_portions DECIMAL(18,4) NOT NULL DEFAULT 0,
                waste_portions DECIMAL(18,4) NOT NULL DEFAULT 0,
                status VARCHAR(30) NOT NULL DEFAULT 'PENDING',
                next_section VARCHAR(100) NULL,
                produced_by VARCHAR(255) NULL,
                produced_at DATETIME NULL,
                transferred_by VARCHAR(255) NULL,
                transferred_at DATETIME NULL,
                remarks TEXT NULL,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                UNIQUE KEY uq_kp (order_no, section, recipe_no)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """))
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass


def _row(db: Session, order_no: str, section: str, recipe_no: str):
    try:
        return db.execute(text("""
            SELECT * FROM kitchen_production
            WHERE order_no=:o AND section=:s AND COALESCE(recipe_no,'')=:r
        """), {"o": order_no, "s": section, "r": recipe_no or ""}).mappings().first()
    except Exception:
        return None


def recipe_states(db: Session, order_no: str, section: str) -> dict:
    """Return {recipe_no: state_row} for one order+section, built by combining
    the ingredient transactions (ground truth of receiving) with any existing
    kitchen_production rows (the product-level state)."""
    ensure_schema(db)

    # ingredient-level rollup per recipe (what's issued/received/processed)
    ing = db.execute(text("""
        SELECT COALESCE(recipe_no,'') AS recipe_no,
               MAX(recipe_name) AS recipe_name,
               COUNT(*) AS ing_lines,
               SUM(CASE WHEN COALESCE(received_qty_standard,0) > 0 THEN 1 ELSE 0 END) AS ing_received,
               ROUND(SUM(COALESCE(issued_qty_standard,0)),4) AS issued,
               ROUND(SUM(COALESCE(received_qty_standard,0)),4) AS received,
               ROUND(SUM(COALESCE(processed_qty_standard,0)),4) AS processed
        FROM kitchen_section_transactions
        WHERE order_no=:o AND current_section=:s
        GROUP BY COALESCE(recipe_no,'')
        ORDER BY recipe_name
    """), {"o": order_no, "s": section}).mappings().all()

    # existing product-level states
    kp = {}
    for r in db.execute(text("""
        SELECT * FROM kitchen_production WHERE order_no=:o AND section=:s
    """), {"o": order_no, "s": section}).mappings().all():
        kp[r["recipe_no"] or ""] = dict(r)

    # Batch 134: per-recipe ingredient lines so the Production View can render a
    # collapsible ingredient breakdown under each recipe card (hidden by default).
    detail_rows = db.execute(text("""
        SELECT COALESCE(k.recipe_no,'') AS recipe_no, k.ingredient_code, k.ingredient_name,
               ROUND(COALESCE(k.issued_qty_standard,0),4) AS issued,
               ROUND(COALESCE(k.received_qty_standard,0),4) AS received,
               ROUND(COALESCE(k.processed_qty_standard,0),4) AS processed,
               k.standard_uom, k.transaction_status,
               (SELECT ri.cutting_portion_size FROM recipe_ingredients ri
                  WHERE ri.inventory_code = k.ingredient_code
                    AND ri.cutting_portion_size IS NOT NULL LIMIT 1) AS cutting_portion_size
        FROM kitchen_section_transactions k
        WHERE k.order_no=:o AND k.current_section=:s
        ORDER BY COALESCE(k.recipe_no,''), k.ingredient_name
    """), {"o": order_no, "s": section}).mappings().all()
    details_by_recipe: dict[str, list] = {}
    for d in detail_rows:
        details_by_recipe.setdefault(d["recipe_no"] or "", []).append(dict(d))

    out = {}
    for r in ing:
        rno = r["recipe_no"] or ""
        state = kp.get(rno)
        derived = PENDING
        if state:
            derived = state["status"]
        elif int(r["ing_received"] or 0) >= int(r["ing_lines"] or 0) and int(r["ing_lines"] or 0) > 0:
            derived = RECEIVED
        elif int(r["ing_received"] or 0) > 0:
            derived = RECEIVED  # partially received still counts as receivable-complete via receive-all
        out[rno] = {
            "recipe_no": rno,
            "recipe_name": r["recipe_name"] or rno,
            "ing_lines": int(r["ing_lines"] or 0),
            "ing_received": int(r["ing_received"] or 0),
            "issued": float(r["issued"] or 0),
            "received": float(r["received"] or 0),
            "processed": float(r["processed"] or 0),
            "status": derived,
            "produced_portions": float(state["produced_portions"]) if state else 0.0,
            "waste_portions": float(state["waste_portions"]) if state else 0.0,
            "next_section": (state["next_section"] if state else next_section(section)),
            "produced_by": state["produced_by"] if state else None,
            "details": details_by_recipe.get(rno, []),
        }
    return out


class SectionLocked(Exception):
    """Raised when a section tries to act on work it has already handed on.

    Batch 100. Carries a message written for the person on the line, not a
    stack trace — the routes surface it directly as a toast.
    """


def _product_status(db: Session, order_no: str, section: str, recipe_no: str) -> str | None:
    """Current status of one recipe within one section, or None if untouched."""
    try:
        return db.execute(text("""
            SELECT status FROM kitchen_production
            WHERE order_no=:o AND section=:s AND COALESCE(recipe_no,'')=:r
            LIMIT 1
        """), {"o": order_no, "s": section, "r": recipe_no or ""}).scalar()
    except Exception:
        return None


def produce_final_product(db: Session, order_no: str, section: str, recipe_no: str,
                          produced_portions: float, waste_portions: float,
                          user: str, remarks: str = "") -> None:
    """Chef marks one recipe cooked as a single finished product. Rolls all of
    that recipe's received ingredients into 'processed' and records the product."""
    ensure_schema(db)
    recipe_no = recipe_no or ""

    # Batch 100 — a section cannot re-cook what it has already handed on.
    #
    # The INSERT below uses ON DUPLICATE KEY UPDATE and unconditionally sets
    # status back to 'PRODUCED'. Without this guard, a chef opening an old
    # order in a section that had already transferred it would silently drag
    # the product BACK out of the next section's queue — the next section
    # would simply stop seeing work it had already been given, with no error
    # anywhere and no way to tell what happened.
    if _product_status(db, order_no, section, recipe_no) == TRANSFERRED:
        raise SectionLocked(
            f"{recipe_no or 'This product'} has already been transferred out of {section}. "
            f"It is locked here — make any correction in the section that holds it now.")

    # mark every received ingredient line for this recipe as processed
    db.execute(text("""
        UPDATE kitchen_section_transactions
        SET processed_qty_standard = COALESCE(received_qty_standard, issued_qty_standard, 0),
            processed_by = :u, processed_at = :now,
            transaction_status = 'Produced', updated_at = :now
        WHERE order_no=:o AND current_section=:s AND COALESCE(recipe_no,'')=:r
    """), {"u": user, "now": datetime.utcnow(), "o": order_no, "s": section, "r": recipe_no})

    name = db.execute(text("""
        SELECT MAX(recipe_name) FROM kitchen_section_transactions
        WHERE order_no=:o AND current_section=:s AND COALESCE(recipe_no,'')=:r
    """), {"o": order_no, "s": section, "r": recipe_no}).scalar() or recipe_no

    db.execute(text("""
        INSERT INTO kitchen_production
            (company_id, order_no, section, recipe_no, recipe_name, produced_portions,
             waste_portions, status, next_section, produced_by, produced_at, remarks)
        VALUES (1, :o, :s, :r, :name, :pp, :wp, 'PRODUCED', :ns, :u, :now, :rm)
        ON DUPLICATE KEY UPDATE
            produced_portions=:pp, waste_portions=:wp, status='PRODUCED',
            produced_by=:u, produced_at=:now, remarks=:rm
    """), {"o": order_no, "s": section, "r": recipe_no, "name": name,
           "pp": produced_portions, "wp": waste_portions,
           "ns": next_section(section), "u": user, "now": datetime.utcnow(), "rm": remarks})
    db.commit()


def transfer_product(db: Session, order_no: str, section: str, recipe_no: str,
                     to_section: str, user: str) -> None:
    """Move a produced recipe forward: re-home its ingredient transactions to the
    next section so that section picks them up, and mark the product transferred."""
    ensure_schema(db)
    recipe_no = recipe_no or ""
    to_section = to_section or next_section(section)

    # Batch 100 — two gates before work leaves a section.
    status = _product_status(db, order_no, section, recipe_no)

    # 1. Already gone. Transferring twice re-homes the ingredient rows a
    #    second time and stamps a new transferred_at, destroying the audit
    #    trail of when it actually moved.
    if status == TRANSFERRED:
        raise SectionLocked(
            f"{recipe_no or 'This product'} has already been transferred from {section} "
            f"and is locked. It cannot be sent forward twice.")

    # 2. Nothing produced yet. "Without complete information the order cannot
    #    move to the next section" — a section must record what it actually
    #    made (and wasted) before handing on, otherwise yield and wastage
    #    reporting has a silent hole exactly where the work happened.
    if status != PRODUCED:
        raise SectionLocked(
            f"Record production for {recipe_no or 'this product'} before transferring it out of "
            f"{section}. Enter produced and waste portions first.")

    # QC / Packing / Dispatch are handled by their own modules — when the kitchen
    # hands off there, we still re-home the rows so those screens can see them.
    db.execute(text("""
        UPDATE kitchen_section_transactions
        SET from_section = :s, current_section = :to,
            to_section = NULL,
            transferred_qty_standard = COALESCE(processed_qty_standard, received_qty_standard, 0),
            transferred_by = :u, transferred_at = :now,
            transaction_status = 'Transferred', updated_at = :now
        WHERE order_no=:o AND current_section=:s AND COALESCE(recipe_no,'')=:r
    """), {"s": section, "to": to_section, "u": user, "now": datetime.utcnow(),
           "o": order_no, "r": recipe_no})

    db.execute(text("""
        UPDATE kitchen_production
        SET status='TRANSFERRED', next_section=:to, transferred_by=:u, transferred_at=:now
        WHERE order_no=:o AND section=:s AND COALESCE(recipe_no,'')=:r
    """), {"to": to_section, "u": user, "now": datetime.utcnow(),
           "o": order_no, "s": section, "r": recipe_no})
    db.commit()


def section_summary(db: Session, section: str) -> dict:
    """Counts for the section header cards."""
    ensure_schema(db)
    try:
        row = db.execute(text("""
            SELECT
              COUNT(DISTINCT order_no) AS orders,
              SUM(CASE WHEN status='PRODUCED' THEN 1 ELSE 0 END) AS produced,
              SUM(CASE WHEN status='TRANSFERRED' THEN 1 ELSE 0 END) AS transferred
            FROM kitchen_production WHERE section=:s
        """), {"s": section}).mappings().first()
        return {"orders": int(row["orders"] or 0), "produced": int(row["produced"] or 0),
                "transferred": int(row["transferred"] or 0)} if row else {}
    except Exception:
        return {}
