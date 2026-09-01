#!/usr/bin/env python3
"""Batch DATA — SMC master import (recipes + ingredients + weekly menu + meal_order).

SMC's workbook has the SAME layout as FRSH (Menu / Master – Recipes / Recipe
Ingredients / Raw material list) plus a **Meal Order** column (BREAKFAST / LUNCH /
DINNER). This importer reuses the FRSH importer's helpers and only differs in:
  - customer_name = 'SMC'
  - it captures Menu → Meal Order into recipes.meal_order
  - it clears stale SMC recipes' day_of_week/meal_order before the menu pass

Usage:
  python scripts/import_smc_master.py --file "SMC_Master_Recipes_with_Inventory_Codes...V2.xlsx" --company 1 [--dry-run]

Prereqs: run the app once so the recipes.meal_order guard (Batch 158) has added
the column; ingredients (raw materials) must exist in Master Data first.
"""
import argparse, sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import openpyxl                       # noqa: E402
from sqlalchemy import text           # noqa: E402
try:
    from app.database.session import SessionLocal
except Exception:                     # pragma: no cover
    from app.db import SessionLocal   # type: ignore

# Reuse the FRSH importer's building blocks so behaviour stays identical.
from scripts.import_frsh_master import (   # noqa: E402
    _find_header, col, _s, import_recipe_ingredients, import_raw_materials,
)

CUSTOMER = "SMC"
ORDER = ["Saturday", "Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]


def import_menu_smc(db, ws, company_id, dry):
    """Menu pass with Meal Order capture (SMC-specific)."""
    hrow, hdr = _find_header(ws, [("recipe code", "recipe ref"), ("days", "day")])
    if hrow is None:
        print("  ! Menu header not found — skipping")
        return 0, 0
    c_day = col(hdr, "days", "day")
    c_code = col(hdr, "recipe code", "recipe ref")
    c_name = col(hdr, "recipe names", "recipe name")
    c_cat = col(hdr, "category")
    c_meal = col(hdr, "meal order", "meal")   # SMC column

    agg: dict[str, dict] = {}
    for row in ws.iter_rows(min_row=hrow + 1, values_only=True):
        code = _s(row[c_code]) if c_code is not None else ""
        if not code:
            continue
        day = _s(row[c_day]) if c_day is not None else ""
        name = _s(row[c_name]) if c_name is not None else code
        cat = _s(row[c_cat]) if c_cat is not None else ""
        meal = (_s(row[c_meal]).upper() if c_meal is not None else "")
        a = agg.setdefault(code, {"name": name, "cat": cat, "days": set(), "meal": meal})
        if day:
            a["days"].add(day.strip().title())
        if cat and not a["cat"]:
            a["cat"] = cat
        if meal and not a["meal"]:
            a["meal"] = meal

    ok = fail = 0
    for code, a in agg.items():
        day_value = " & ".join(d for d in ORDER if d in a["days"])
        if dry:
            ok += 1
            continue
        try:
            db.execute(text("""
                INSERT INTO recipes
                    (company_id, recipe_code, recipe_name, customer_name, category,
                     day_of_week, meal_order, status, is_active, approval_status,
                     version, standard_portions, created_at, updated_at)
                VALUES
                    (:cid, :code, :name, :cust, :cat, :day, :meal, 'ACTIVE', 1,
                     'APPROVED', 1, 1, NOW(), NOW())
                ON DUPLICATE KEY UPDATE
                    day_of_week = VALUES(day_of_week),
                    meal_order = VALUES(meal_order),
                    category = COALESCE(NULLIF(recipes.category,''), VALUES(category)),
                    -- Batch 140: re-assert approval. A recipe carried over from
                    -- an earlier partial import can be stuck on 'Pending', which
                    -- silently removes it from /api/menu/for-date even though the
                    -- import reports it as "ok".
                    approval_status = 'APPROVED',
                    customer_name = :cust, status='ACTIVE', is_active=1, updated_at=NOW()
            """), {"cid": company_id, "code": code, "name": a["name"],
                   "cust": CUSTOMER, "cat": a["cat"], "day": day_value, "meal": a["meal"]})
            db.commit()
            ok += 1
        except Exception as exc:
            db.rollback()
            fail += 1
            if fail <= 5:
                print(f"    menu '{code}' failed: {exc}")
    return ok, fail


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True)
    ap.add_argument("--company", type=int, default=1)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    print(f"Importing SMC into company_id={args.company}")
    wb = openpyxl.load_workbook(args.file, read_only=True, data_only=True)
    names = {s.lower().strip(): s for s in wb.sheetnames}
    db = SessionLocal()

    # 1) raw materials (ingredient master) — same helper as FRSH
    for key in ("raw material list", "raw materials", "raw material"):
        if key in names:
            ok, fail = import_raw_materials(db, wb[names[key]], args.company, args.dry_run)
            print(f"  Ingredients (raw materials): {ok} ok, {fail} failed")
            break

    # 2) recipe ingredient lines — same helper as FRSH (customer read from sheet)
    for key in ("recipe ingredients", "recipe ingredient"):
        if key in names:
            ok, lines, fail = import_recipe_ingredients(db, wb[names[key]], args.company, args.dry_run)
            print(f"  Recipes: {ok} ok, {fail} failed  |  Recipe lines: {lines}")
            break

    # 3) menu with meal_order — SMC-specific
    for key in ("menu",):
        if key in names:
            # clear stale SMC day/meal before rewriting so ghost days don't survive
            if not args.dry_run:
                db.execute(text("""
                    UPDATE recipes SET day_of_week=NULL, meal_order=NULL, updated_at=NOW()
                    WHERE company_id=:cid AND customer_name=:cust
                """), {"cid": args.company, "cust": CUSTOMER})
                db.commit()
            ok, fail = import_menu_smc(db, wb[names[key]], args.company, args.dry_run)
            print(f"  Menu day/meal rows: {ok} ok, {fail} failed")
            break

    db.close()
    print("Done. SMC master data imported.")


if __name__ == "__main__":
    main()
