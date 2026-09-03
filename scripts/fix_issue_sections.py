#!/usr/bin/env python3
"""Batch 162 — re-apply section routing to data created before the map fix.

WHAT THIS IS NOW

A thin CLI wrapper around `backfill_issue_sections()` in
app/services/production_service.py. It contains NO routing logic of its own.

WHY IT CHANGED

Batch 161 shipped a standalone implementation here that recomputed
`store_issuance_lines.issue_to_section` by itself. That was wrong twice over:

  1. It only fixed issuance lines. `bom_lines.default_issue_section` holds the
     same value and is what the Issuance-by-Section screen GROUPS BY, so the
     by-section view would have stayed wrong even after a clean run.

  2. A second implementation of the routing rule is precisely the problem that
     caused this bug — two section maps in two files, one fixed and one missed.
     Adding a third place that decides "which section does this go to" would
     have been repeating the mistake while claiming to fix it.

`backfill_issue_sections()` has existed since Batch 131, covers BOM lines AND
issuance lines, and was already wired to a route. It simply had no button and
no CLI, so nobody could reach it.

WHAT IT DOES

  Pass 1  bom_lines.default_issue_section       recomputed from the recipe
                                                ingredient's kitchen_section
  Pass 2  store_issuance_lines.issue_to_section PENDING lines only

SAFETY

  * Non-pending lines are skipped and counted. Once the store has physically
    issued material, where it went is history; re-pointing it would strand the
    kitchen transactions already created against it.
  * Idempotent — a second run reports 0 / 0.

USAGE

    python scripts/fix_issue_sections.py --dry-run
    python scripts/fix_issue_sections.py
    python scripts/fix_issue_sections.py --company 1

The same repair is in the UI: Store Issuance -> "Re-route Sections".
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database.session import SessionLocal  # noqa: E402
from app.services import production_service as _ps  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would change without writing")
    ap.add_argument("--company", type=int, default=None,
                    help="limit to one company_id (default: all)")
    args = ap.parse_args()

    db = SessionLocal()
    try:
        scope = f" for company {args.company}" if args.company else " (all companies)"
        print(f"Re-applying section routing{scope}...\n")

        if args.dry_run:
            # backfill_issue_sections() commits internally. Rather than
            # duplicating its logic to preview it, neutralise the commit for the
            # duration of the call and roll back afterwards. The counts it
            # returns are then a true preview, computed by the real code path.
            real_commit, real_flush = db.commit, db.flush
            db.commit = lambda: None            # type: ignore[method-assign]
            try:
                counts = _ps.backfill_issue_sections(db, company_id=args.company)
            finally:
                db.commit, db.flush = real_commit, real_flush
                db.rollback()
        else:
            counts = _ps.backfill_issue_sections(db, company_id=args.company)

        print(f"  BOM lines re-routed          : {counts['bom_updated']}")
        print(f"  Pending issue lines re-routed: {counts['issue_updated']}")
        print(f"  Already-issued, left as-is   : {counts['skipped_locked']}")

        if args.dry_run:
            print("\nDRY RUN — rolled back, nothing written.")
        else:
            print("\nDone. Re-run to confirm it reports 0 and 0.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
