#!/usr/bin/env python3
"""Batch 153 — recover output-capture values from remark stamps into columns.

WHY

Before Batch 153 these values were appended to `section_remarks` as text:

    [C12 P30 V0 Y5]         Hot Kitchen carb / protein / vegetable / yield (g)
    [OUT 12Gram Wt250g]     Cold Kitchen / Bakery produced portion + UOM + wt
    [NUT w=250 p=30 c=12]   recipe-level process output

Batch 153 gives each of those a real column, because reporting cannot SUM,
GROUP BY or chart a remark string. This script parses the stamps out of the
existing rows and fills the new columns, so historic orders are not left blank
when the dashboards land.

SAFETY

* Read-then-write, one row at a time, inside a single transaction.
* Only writes a column that is currently NULL — a value entered through the new
  form always wins over one parsed out of old text.
* The remark is NOT modified. The stamp stays as the audit trail of where the
  number came from, and the script stays re-runnable.
* --dry-run reports exactly what it would change and writes nothing.

USAGE

    python scripts/backfill_output_capture.py --dry-run
    python scripts/backfill_output_capture.py

Run it ONCE after starting the app (so the startup schema guard has created the
columns). Re-running is harmless.
"""
from __future__ import annotations

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402

from app.database.session import SessionLocal  # noqa: E402

# [C12 P30 V0 Y5]  — numbers may be decimal or negative-free floats
RE_NUT = re.compile(r"\[C([\d.]+)\s+P([\d.]+)\s+V([\d.]+)\s+Y([\d.]+)\]")
# [OUT 12Gram Wt250g] — qty, then UOM word, then weight
RE_OUT = re.compile(r"\[OUT\s+([\d.]+)\s*([A-Za-z]*)\s+Wt([\d.]+)g\]")
# [NUT w=250 p=30 c=12] — any of the three may be blank
RE_PROC = re.compile(r"\[NUT\s+w=([\d.]*)\s+p=([\d.]*)\s+c=([\d.]*)\]")


def _f(v):
    try:
        return float(v) if str(v).strip() != "" else None
    except (TypeError, ValueError):
        return None


def column_exists(db, table: str, col: str) -> bool:
    return bool(db.execute(text("""
        SELECT COUNT(*) FROM information_schema.columns
        WHERE table_schema = DATABASE() AND table_name = :t AND column_name = :c
    """), {"t": table, "c": col}).scalar())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    db = SessionLocal()
    try:
        required = ["carb_g", "protein_g", "vegetable_g", "yield_g",
                    "produced_portion", "portion_weight_g", "output_uom"]
        missing = [c for c in required
                   if not column_exists(db, "kitchen_section_transactions", c)]
        if missing:
            print("! Columns not present yet: " + ", ".join(missing))
            print("  Start the app once so the startup schema guard creates them,")
            print("  then re-run this script.")
            return 1

        rows = db.execute(text("""
            SELECT id, COALESCE(section_remarks,'') AS rem,
                   carb_g, protein_g, vegetable_g, yield_g,
                   produced_portion, portion_weight_g, output_uom
            FROM kitchen_section_transactions
            WHERE section_remarks LIKE '%[C%'
               OR section_remarks LIKE '%[OUT %'
               OR section_remarks LIKE '%[NUT %'
        """)).mappings().all()

        print(f"Scanning {len(rows)} row(s) with stamps...")
        updated = 0
        skipped = 0

        for r in rows:
            sets: dict = {}
            rem = r["rem"]

            m = RE_NUT.search(rem)
            if m:
                for col, val in zip(("carb_g", "protein_g", "vegetable_g", "yield_g"),
                                    m.groups()):
                    if r[col] is None and _f(val) is not None:
                        sets[col] = _f(val)

            m = RE_OUT.search(rem)
            if m:
                qty, uom, wt = m.groups()
                if r["produced_portion"] is None and _f(qty) is not None:
                    sets["produced_portion"] = _f(qty)
                if r["portion_weight_g"] is None and _f(wt) is not None:
                    sets["portion_weight_g"] = _f(wt)
                if not r["output_uom"] and uom:
                    sets["output_uom"] = uom

            m = RE_PROC.search(rem)
            if m:
                wt, prot, carb = m.groups()
                if r["portion_weight_g"] is None and _f(wt) is not None:
                    sets["portion_weight_g"] = _f(wt)
                if r["protein_g"] is None and _f(prot) is not None:
                    sets["protein_g"] = _f(prot)
                if r["carb_g"] is None and _f(carb) is not None:
                    sets["carb_g"] = _f(carb)

            if not sets:
                skipped += 1
                continue

            updated += 1
            if args.dry_run:
                print(f"  id={r['id']}  {sets}")
            else:
                assign = ", ".join(f"{k} = :{k}" for k in sets)
                db.execute(text(f"UPDATE kitchen_section_transactions SET {assign} WHERE id = :id"),
                           {**sets, "id": r["id"]})

        if args.dry_run:
            print(f"\nDRY RUN — would update {updated} row(s), {skipped} already populated "
                  f"or unparseable. Nothing written.")
        else:
            db.commit()
            print(f"\nUpdated {updated} row(s). {skipped} needed no change.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
