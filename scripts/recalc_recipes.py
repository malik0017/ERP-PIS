#!/usr/bin/env python3
"""
Batch 127 — Recipe header-cost recalculator.

Fixes image 2: the recipe VIEW page showed Food Cost / Total Cost / Sale Price /
Missing Cost Lines = 0.00, even though the EDIT page (which recalculates live)
showed real values. Cause: the FRSH importer inserted the recipe LINES but never
populated the aggregate cost columns on the `recipes` header row.

This script loads every recipe (optionally only one customer) via the ORM and
calls the app's own `recalc_recipe()` — the exact same function the Edit → Save
path uses — so the header costs match the live-costing panel precisely. Then it
commits.

USAGE
    # all recipes:
    python scripts/recalc_recipes.py --company 1
    # only FRSH:
    python scripts/recalc_recipes.py --company 1 --customer Frsh
    # preview only (no write):
    python scripts/recalc_recipes.py --company 1 --dry-run
"""
from __future__ import annotations
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from app.database.session import SessionLocal
except Exception:  # pragma: no cover
    from app.db import SessionLocal  # type: ignore

from app.models.recipe import Recipe  # noqa: E402
from app.services.recipe_service import recalc_recipe  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="Recalculate recipe header costs")
    ap.add_argument("--company", type=int, default=1)
    ap.add_argument("--customer", default="", help="Only recipes with this customer_name (e.g. Frsh)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    db = SessionLocal()
    try:
        q = db.query(Recipe).filter(Recipe.company_id == args.company)
        if args.customer:
            q = q.filter(Recipe.customer_name == args.customer)
        recipes = q.all()
        print(f"{'DRY RUN — ' if args.dry_run else ''}Recalculating {len(recipes)} recipe(s)"
              + (f" for customer '{args.customer}'" if args.customer else ""))

        done = 0
        for r in recipes:
            try:
                recalc_recipe(r)  # mutates r.food_cost / total_cost / sale_price / missing_cost_lines
                done += 1
                if done <= 5:
                    print(f"  {r.recipe_code}: food={float(r.food_cost or 0):.2f} "
                          f"total={float(r.total_cost or 0):.2f} "
                          f"sale={float(r.sale_price or 0):.2f} "
                          f"missing={r.missing_cost_lines}")
            except Exception as exc:
                print(f"  ! {r.recipe_code} failed: {exc}")

        if args.dry_run:
            db.rollback()
            print(f"Dry run complete — {done} recipe(s) would be updated. Nothing written.")
        else:
            db.commit()
            print(f"Committed. {done} recipe(s) recalculated.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
