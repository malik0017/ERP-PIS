"""ISFC PIMS — reset transactional data, keep master data (Batch 118).

    python scripts/reset_transactions.py                 # DRY RUN — shows counts, changes nothing
    python scripts/reset_transactions.py --confirm       # actually deletes
    python scripts/reset_transactions.py --confirm --wipe-audit
    python scripts/reset_transactions.py --confirm --keep-opening-stock

WHY THIS SCRIPT EXISTS RATHER THAN A LIST OF DELETE STATEMENTS
--------------------------------------------------------------
Three things make a hand-written cleanup dangerous, and all three are handled
here:

1. GETTING THE CLASSIFICATION WRONG. Some tables look transactional and are
   not. `customer_subscriptions` is the CONTRACT ("this customer gets lunch
   every weekday") — the orders it generates are transactional, the contract
   is master data. Delete it and the customer stops receiving anything.
   Every table in this database is classified below, explicitly, with nothing
   left to a naming guess.

2. FOREIGN KEY ORDER. Deleting parents before children fails or orphans rows.
   The order below is child-first throughout.

3. DOCUMENT NUMBERING. Deleting the orders does not reset the counters, so the
   first order after a clean-out would be ORD-...-0042. The sequences are reset
   so numbering starts at 1 and the test data reads like real first-use.

WHAT IS DELIBERATELY NOT DELETED
--------------------------------
* audit_logs   — the record of who did what, including this reset. Wiping it
                 removes the evidence that the reset happened at all. Use
                 --wipe-audit if you genuinely want it gone.
* opening stock — OPENING_STOCK ledger movements are your real 4.6M of stock
                 on hand, loaded from the inventory report. They are not test
                 data. --keep-opening-stock preserves them so you can test the
                 order flow against real balances; the default clears them so
                 you start from zero stock.
"""
from __future__ import annotations

import os as _os
import sys as _sys

_here = _os.path.dirname(_os.path.abspath(__file__))
_root = _here
for _ in range(4):
    if _os.path.isdir(_os.path.join(_root, "app")):
        break
    _p = _os.path.dirname(_root)
    if _p == _root:
        break
    _root = _p
if not _os.path.isdir(_os.path.join(_root, "app")):
    _sys.stderr.write("ERROR: run this from the project folder (the one containing app/).\n")
    _sys.exit(2)
if _root not in _sys.path:
    _sys.path.insert(0, _root)
_os.chdir(_root)

from sqlalchemy import text

from app.database.session import SessionLocal

CONFIRM = "--confirm" in _sys.argv
WIPE_AUDIT = "--wipe-audit" in _sys.argv
KEEP_OPENING = "--keep-opening-stock" in _sys.argv

# ---------------------------------------------------------------------------
# TRANSACTIONAL — deleted. Child tables listed before their parents.
# ---------------------------------------------------------------------------
TRANSACTIONAL = [
    # --- Sales & order pipeline ---
    ("order_lines", "Order line items"),
    ("head_chef_plans", "Head chef cooking schedules"),
    ("bom_lines", "BOM explosions per order"),
    ("store_issuance_lines", "Material issued to kitchen"),
    ("store_topup_requests", "Section top-up requests"),
    ("kitchen_section_transactions", "Section-to-section handovers"),
    ("kitchen_production", "Kitchen production records"),
    ("qc_checks", "In-kitchen QC checks"),
    ("packing_dispatch", "Packing and dispatch records"),
    ("customer_orders", "Customer orders"),

    # --- Subscription-generated orders (the CONTRACT is kept) ---
    ("subscription_lines", "Subscription order lines"),
    ("subscription_orders", "Orders generated from subscriptions"),

    # --- Procurement ---
    ("purchase_requisition_lines", "Requisition lines"),
    ("purchase_requisitions", "Purchase requisitions"),
    ("purchase_order_lines", "PO lines"),
    ("grn_lines", "Goods receipt lines"),
    ("qc_incoming_inspections", "Incoming QC inspections"),
    ("grn_receipts", "Goods receipts"),
    ("purchase_orders", "Purchase orders"),

    # --- Landed cost (attached to receipts) ---
    ("landed_cost_allocations", "Landed cost allocations"),
    ("landed_cost_lines", "Landed cost lines"),
    ("landed_cost_charges", "Landed cost charges"),

    # --- Finance ---
    ("finance_payments", "Payments"),
    ("ap_invoices", "Supplier invoices"),
    ("ar_invoices", "Customer invoices"),
    ("gl_journal_lines", "Journal lines"),
    ("gl_journals", "Journal headers"),

    # --- Approvals (the TIERS are config and are kept) ---
    ("approval_steps", "Approval signatures on documents"),

    # --- Quality ---
    ("qc_complaints", "Customer complaints"),

    # --- Notifications ---
    ("notifications", "In-app notifications"),
]

# Handled separately because of the opening-stock rule.
STOCK_TABLES = [
    ("stock_lots", "Lot balances"),
    ("inventory_transactions", "Stock ledger movements"),
]

# ---------------------------------------------------------------------------
# MASTER / CONFIG — never touched by this script.
# Listed so the classification is auditable rather than implied by omission.
# ---------------------------------------------------------------------------
PRESERVED = [
    ("customers", "Customer master"),
    ("suppliers", "Supplier master"),
    ("ingredients", "Ingredient / inventory master"),
    ("brands", "Brands"),
    ("chefs", "Chefs"),
    ("kitchen_sections", "Kitchen sections"),
    ("kitchen_locations", "Kitchen locations"),
    ("revenue_streams", "Sales channels"),
    ("recipes", "Recipe master"),
    ("recipe_ingredients", "Recipe ingredient lines"),
    ("customer_subscriptions", "Subscription CONTRACTS (not their orders)"),
    ("gl_accounts", "Chart of accounts"),
    ("gl_periods", "Fiscal periods"),
    ("cost_centers", "Cost centres"),
    ("budgets", "Budget lines"),
    ("approval_tiers", "Approval ladder configuration"),
    ("qc_sampling_config", "QC sampling rules"),
    ("users", "Users"),
    ("roles", "Roles"),
    ("permissions", "Permissions"),
    ("role_permissions", "Role permission grants"),
    ("user_page_access", "Per-user access matrix"),
    ("companies", "Companies"),
    ("system_settings", "System settings"),
    ("module_visibility", "Module visibility"),
    ("saved_reports", "Saved custom reports"),
    ("report_schedules", "Scheduled reports"),
    ("master_records", "Generic master records"),
    ("audit_logs", "Audit trail (use --wipe-audit to clear)"),
]


def count(db, table: str) -> int:
    try:
        return int(db.execute(text(f"SELECT COUNT(*) FROM `{table}`")).scalar() or 0)
    except Exception:
        return -1   # table absent on this install


def main() -> int:
    db = SessionLocal()

    print()
    print("=" * 74)
    print("  ISFC PIMS — TRANSACTIONAL RESET")
    print("=" * 74)
    if not CONFIRM:
        print("  DRY RUN — nothing will be deleted. Add --confirm to run for real.")
    else:
        print("  LIVE RUN — data WILL be deleted.")
    print()

    # --- what will go ---
    print("  WILL BE DELETED")
    print("  " + "-" * 70)
    total = 0
    plan = list(TRANSACTIONAL)
    for tbl, label in plan:
        n = count(db, tbl)
        if n < 0:
            continue
        total += n
        if n:
            print(f"    {tbl:<32} {n:>8,}   {label}")

    # stock, with the opening-stock rule
    print()
    for tbl, label in STOCK_TABLES:
        n = count(db, tbl)
        if n < 0:
            continue
        if tbl == "inventory_transactions" and KEEP_OPENING:
            keep = int(db.execute(text(
                "SELECT COUNT(*) FROM inventory_transactions "
                "WHERE movement_type = 'OPENING_STOCK'")).scalar() or 0)
            print(f"    {tbl:<32} {n - keep:>8,}   {label} "
                  f"(keeping {keep:,} opening balances)")
            total += n - keep
        else:
            print(f"    {tbl:<32} {n:>8,}   {label}")
            total += n

    if WIPE_AUDIT:
        n = count(db, "audit_logs")
        if n > 0:
            print(f"    {'audit_logs':<32} {n:>8,}   Audit trail (--wipe-audit)")
            total += n

    print()
    print(f"    TOTAL ROWS TO DELETE: {total:,}")

    # --- what stays ---
    print()
    print("  WILL BE KEPT")
    print("  " + "-" * 70)
    for tbl, label in PRESERVED:
        if WIPE_AUDIT and tbl == "audit_logs":
            continue
        n = count(db, tbl)
        if n > 0:
            print(f"    {tbl:<32} {n:>8,}   {label}")

    if not CONFIRM:
        print()
        print("  " + "=" * 70)
        print("  DRY RUN COMPLETE. Nothing was changed.")
        print()
        print("  BEFORE RUNNING FOR REAL, TAKE A BACKUP:")
        print("      mysqldump -u root -p isfc_db > isfc_backup_before_reset.sql")
        print()
        print("  Then:  python scripts/reset_transactions.py --confirm")
        print("  " + "=" * 70)
        print()
        db.close()
        return 0

    # ------------------------------------------------------------------
    # Live run.
    #
    # FK checks are disabled for the duration. The delete order below is
    # child-first and should not need it, but a partially-migrated database
    # can carry constraints this script does not know about, and a reset that
    # stops half-way is worse than one that completes: you would be left with
    # orders whose lines are gone.
    # ------------------------------------------------------------------
    print()
    print("  DELETING…")
    db.execute(text("SET FOREIGN_KEY_CHECKS = 0"))

    deleted = 0
    for tbl, _label in plan:
        if count(db, tbl) < 0:
            continue
        try:
            res = db.execute(text(f"DELETE FROM `{tbl}`"))
            n = getattr(res, "rowcount", 0) or 0
            deleted += n
            if n:
                print(f"    {tbl:<32} {n:>8,} deleted")
        except Exception as exc:
            print(f"    {tbl:<32} FAILED — {type(exc).__name__}: {str(exc)[:70]}")

    for tbl, _label in STOCK_TABLES:
        if count(db, tbl) < 0:
            continue
        try:
            if tbl == "inventory_transactions" and KEEP_OPENING:
                res = db.execute(text(
                    "DELETE FROM inventory_transactions "
                    "WHERE COALESCE(movement_type,'') <> 'OPENING_STOCK'"))
            else:
                res = db.execute(text(f"DELETE FROM `{tbl}`"))
            n = getattr(res, "rowcount", 0) or 0
            deleted += n
            if n:
                print(f"    {tbl:<32} {n:>8,} deleted")
        except Exception as exc:
            print(f"    {tbl:<32} FAILED — {type(exc).__name__}: {str(exc)[:70]}")

    if WIPE_AUDIT:
        try:
            res = db.execute(text("DELETE FROM audit_logs"))
            n = getattr(res, "rowcount", 0) or 0
            deleted += n
            print(f"    {'audit_logs':<32} {n:>8,} deleted")
        except Exception:
            pass

    # Reset numbering so the first new order is ORD-<date>-0001 rather than
    # continuing from wherever the deleted data left off.
    try:
        db.execute(text("UPDATE document_sequences SET last_number = 0"))
        print(f"    {'document_sequences':<32} {'reset':>8}   numbering restarts at 1")
    except Exception:
        pass

    db.execute(text("SET FOREIGN_KEY_CHECKS = 1"))
    db.commit()

    print()
    print("  " + "=" * 70)
    print(f"  DONE — {deleted:,} row(s) deleted. Master data untouched.")
    print("  " + "=" * 70)
    print()
    print("  Next: restart uvicorn, then work through TESTING_ROADMAP.md")
    print()
    db.close()
    return 0


if __name__ == "__main__":
    _sys.exit(main())
