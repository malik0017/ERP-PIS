"""Batch 225 — repair kitchen lines duplicated by a self-transferring route.

    python scripts/repair_duplicate_section_lines.py            # report only
    python scripts/repair_duplicate_section_lines.py --apply    # fix

WHAT WENT WRONG
A route template that named the same section twice in a row
("Store, Hot Kitchen, Hot Kitchen, QC") made the section transfer to itself.
The transfer created a second row in the SAME section, so the workstation
listed every ingredient twice — 54 lines for a 27-ingredient recipe — once as
"Transferred to Hot Kitchen" and once as "Received, issue to QC".

Batch 225 stops it happening: every route written to the database is deduped,
and a transfer never hands work to the section it came from. This script
repairs orders that already carry the duplicate.

WHAT IT DOES
For each (order, recipe, ingredient, section) with more than one row:
  * keeps the LAST row — the one holding the current quantities and the
    correct onward section;
  * deletes only the earlier rows that were fully transferred INTO the same
    section (the self-transfer artefacts) and carry no waste, no return and
    no nutrition capture.
Anything else is left alone and listed, because a genuine repeat visit to a
section (prep, then finish after QC) looks similar and must not be deleted.

It never touches a row whose quantities would be lost.
"""
from __future__ import annotations

import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402

from app.database.session import SessionLocal  # noqa: E402

APPLY = "--apply" in sys.argv


def main() -> int:
    db = SessionLocal()
    try:
        rows = db.execute(text("""
            SELECT id, order_no, recipe_no, ingredient_code, current_section, to_section,
                   COALESCE(received_qty_standard,0)   AS recv,
                   COALESCE(transferred_qty_standard,0) AS trans,
                   COALESCE(waste_qty_standard,0)      AS waste,
                   COALESCE(returned_qty_standard,0)   AS ret,
                   carb_g, protein_g, vegetable_g, transaction_status
            FROM kitchen_section_transactions
            ORDER BY order_no, recipe_no, ingredient_code, current_section, id
        """)).mappings().all()
    except Exception as exc:
        print("Could not read kitchen_section_transactions:", exc)
        return 1

    groups = defaultdict(list)
    for r in rows:
        groups[(r["order_no"], r["recipe_no"], r["ingredient_code"], r["current_section"])].append(r)

    removable, kept_back, affected_orders = [], [], set()
    for key, rs in groups.items():
        if len(rs) < 2:
            continue
        affected_orders.add(key[0])
        for r in rs[:-1]:                      # every row except the last
            status = str(r["transaction_status"] or "").upper()
            self_transfer = (r["to_section"] or "") == (r["current_section"] or "")
            safe = (
                self_transfer
                and status.startswith("TRANSFERRED")
                and float(r["waste"]) == 0 and float(r["ret"]) == 0
                and r["carb_g"] is None and r["protein_g"] is None and r["vegetable_g"] is None
            )
            (removable if safe else kept_back).append(r)

    print("=" * 70)
    print("Duplicate kitchen section lines")
    print("=" * 70)
    print(f"  duplicate groups        : {sum(1 for v in groups.values() if len(v) > 1)}")
    print(f"  orders affected         : {len(affected_orders)}")
    print(f"  rows safe to remove     : {len(removable)}")
    print(f"  rows kept for review    : {len(kept_back)}")

    if kept_back:
        print("\n  Kept (not a self-transfer, or carries waste/return/nutrition):")
        for r in kept_back[:20]:
            print(f"    id={r['id']} {r['order_no']} {r['ingredient_code']} "
                  f"{r['current_section']} -> {r['to_section']} status={r['transaction_status']}")
        if len(kept_back) > 20:
            print(f"    … and {len(kept_back) - 20} more")

    if not removable:
        print("\nNothing to repair.")
        return 0

    if not APPLY:
        print("\nDry run. Re-run with --apply to delete the rows listed as safe.")
        return 0

    ids = [r["id"] for r in removable]
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        db.execute(text("DELETE FROM kitchen_section_transactions WHERE id IN :ids")
                   .bindparams(__import__("sqlalchemy").bindparam("ids", expanding=True)),
                   {"ids": chunk})
    db.commit()
    print(f"\nDeleted {len(ids)} duplicate row(s). Reopen an affected order to confirm "
          f"the ingredient count matches the recipe.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
