"""Batch 235 (Image 4) — reconcile issued quantities against the recipe master.

WHY THIS SCRIPT EXISTS

On the Butchery workstation, Frz Chicken Breast Tender came in at

    Grilled Chicken Ranch Salad     52.01 g   (expected 52.49)
    Misto chicken broccoli        1092.28 g   (expected 1102.36)
    Pasta Creamy Potato           1456.38 g   (expected 1469.34)

Those are not three unrelated slips. Every one of them is the workbook figure
multiplied by the SAME constant, 0.990860:

    52.4934 x 0.99086 = 52.0134
  1102.3620 x 0.99086 = 1092.28
  1469.8163 x 0.99086 = 1456.38   (approx.)

A single uniform factor across three different recipes cannot come from a
rounding bug or from data entry. It is one wrong number used by all three —
and it is not in BOM generation, which computes exactly

    gross per portion = recipe_ingredients.qty_batch / recipe_ingredients.portions
    issued            = gross per portion x order_lines.required_portions

with no yield term of its own anywhere in the path (see
production_service.generate_bom, BOM_QTY_BASIS = "gross_prep",
BOM_APPLY_WASTAGE = False). Work the factor backwards and it says the stored
recipe line is grossed up at a yield of 76.903%, where every workbook you have
sent — FRSH_Master_Recipes_with_Inventory_Codes.xlsx and the SMC V2 workbook —
says 76.2% for this item. In other words: the arithmetic is right and the
RECIPE MASTER ROW IN THE DATABASE IS STALE, left behind by an earlier import.

That is a claim about your data, so this script proves or disproves it against
the live database instead of asking you to take it on trust. It does not
change anything.

WHAT IT PRINTS

  1. Per recipe line: qty_batch / portions / qty_per_portion as stored, the
     implied yield, the workbook's values, and the delta.
  2. A DRIFT list — every line where the stored gross differs from the
     workbook's by more than the tolerance, with the implied yield of each.
  3. Per order line: the full derivation from recipe row to issued quantity,
     so the number on the Butchery screen can be followed back to its source.

USAGE

    python scripts/verify_bom_math.py --workbook FRSH_Master_Recipes_with_Inventory_Codes.xlsx
    python scripts/verify_bom_math.py --workbook FRSH_...xlsx --order ORD-20260921-0001
    python scripts/verify_bom_math.py --workbook FRSH_...xlsx --item PLT2-3278

RESULT OF THE FIRST RUN (2026-09-22) — THE HYPOTHESIS WAS WRONG
--------------------------------------------------------------
    695 recipe/item pairs checked, DRIFT: None.

Every stored recipe line matches the workbook, so the recipe master is NOT
stale and Batch 235's explanation of the 0.99086 factor was incorrect. The
gap is downstream of the recipe master, between it and the Butchery screen.

There are exactly three stages it can enter at, and each leaves a different
fingerprint. --order now names which one, instead of leaving it to be guessed
a second time:

    BOM         recipe row -> bom_lines.total_required_with_waste_standard
                (wrong here means required_portions or the UOM conversion)
    ISSUE       bom_lines -> store_issuance_lines.issued_qty_standard
                (wrong here means the store issued a different quantity)
    KITCHEN     store issue -> kitchen_section_transactions.received_qty
                (wrong here means the section received less than was issued)

Run:

    python scripts/verify_bom_math.py --order ORD-20260921-0001 --item PLT2-3278

and read the STAGE line under each ingredient.

IF DRIFT IS CONFIRMED

Re-import the affected recipes from the current workbook
(scripts/import_frsh_master.py), then REGENERATE the BOM for any order that has
not yet been issued. Orders already in production keep their issued quantities
on purpose — silently rewriting a quantity a butcher has already cut against
would destroy the audit trail, exactly as documented in
production_service.finalize_store_issue.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402

from app.database.session import SessionLocal  # noqa: E402

TOL = 0.005  # 0.5% — below this is rounding, above it is a different number


def _f(v, d=0.0):
    try:
        if v is None or v == "":
            return d
        return float(v)
    except (TypeError, ValueError):
        return d


def load_workbook_lines(path: str) -> dict:
    """{(recipe_code, item_code): {net, gross, yield, portions}} from the sheet."""
    try:
        import openpyxl
    except ImportError:
        print("openpyxl is not installed — run: pip install openpyxl")
        return {}
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    if "Recipe Ingredients" not in wb.sheetnames:
        print(f"{path}: no 'Recipe Ingredients' sheet")
        return {}
    ws = wb["Recipe Ingredients"]
    rows = list(ws.iter_rows(values_only=True))

    # Find the header row rather than assuming row 4 — the SMC and FRSH
    # workbooks put it in different places, and a wrong guess here silently
    # produces zero-quantity comparisons.
    hrow, hdr = None, None
    for i, r in enumerate(rows[:25]):
        cells = [str(c).strip().lower() if c is not None else "" for c in (r or ())]
        if any(c.startswith("recipe code") or c.startswith("recipe ref") for c in cells):
            hrow, hdr = i, cells
            break
    if hrow is None:
        print(f"{path}: could not find the header row")
        return {}

    def col(*names):
        for n in names:
            for i, c in enumerate(hdr):
                if c.startswith(n):
                    return i
        return None

    c_rc = col("recipe code", "recipe ref")
    c_ic = col("item code")
    c_net = col("net qty req per batch", "qty req per batch")
    c_gr = col("gross qty req per batch", "gross qty req batch")
    c_y = col("yield %")
    c_p = col("no. of portions per batch")

    out = {}
    for r in rows[hrow + 1:]:
        if not r or c_rc is None or c_ic is None:
            continue
        rc = str(r[c_rc]).strip() if r[c_rc] else ""
        ic = str(r[c_ic]).strip() if r[c_ic] else ""
        if not rc or not ic:
            continue
        out.setdefault((rc, ic), []).append({
            "net": _f(r[c_net]) if c_net is not None else 0.0,
            "gross": _f(r[c_gr]) if c_gr is not None else 0.0,
            "yield": _f(r[c_y], 100.0) if c_y is not None else 100.0,
            "portions": _f(r[c_p], 1.0) if c_p is not None else 1.0,
        })
    return out


def _autodetect_workbook() -> str:
    """Batch 236 — find a recipe workbook without being told where it is.

    The first run of this script died on `FRSH_..._Codes.xlsx`, a placeholder
    copied out of a README. Any .xlsx sitting in the project root with a
    "Recipe Ingredients" sheet is a recipe workbook; the most recently modified
    one is the one being worked on.
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        import openpyxl
    except ImportError:
        return ""
    best, best_mtime = "", -1.0
    for name in os.listdir(root):
        if not name.lower().endswith(".xlsx") or name.startswith("~$"):
            continue
        path = os.path.join(root, name)
        try:
            wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
            names = wb.sheetnames
            wb.close()
        except Exception:
            continue
        if "Recipe Ingredients" not in names:
            continue
        m = os.path.getmtime(path)
        if m > best_mtime:
            best, best_mtime = path, m
    if best:
        print(f"(no --workbook given; using {os.path.basename(best)})")
    return best


def main() -> int:
    ap = argparse.ArgumentParser()
    # Batch 236: optional. The first run failed on a placeholder filename
    # ("FRSH_..._Codes.xlsx") copied out of the README, and the drift check is
    # the only part that needs a workbook at all — --order works without one.
    # With no --workbook, any .xlsx in the project root that has a
    # "Recipe Ingredients" sheet is used.
    ap.add_argument("--workbook", default="", help="recipe workbook to compare against "
                                                   "(default: auto-detect in the project root)")
    ap.add_argument("--order", default="", help="limit the derivation section to one order")
    ap.add_argument("--item", default="", help="limit everything to one inventory code")
    args = ap.parse_args()

    wb_path = args.workbook or _autodetect_workbook()
    book = {}
    if wb_path:
        book = load_workbook_lines(wb_path)
        if book:
            print(f"Workbook: {wb_path} — {len(book)} recipe/item pairs\n")
    if not book:
        if args.workbook:
            print(f"Could not read {args.workbook}.")
        if not (args.order or args.item):
            print("Nothing to do: no readable workbook, and no --order / --item to trace.\n"
                  "Pass --workbook <file.xlsx> for the drift check, or --order ORD-… to "
                  "trace where a quantity changes.")
            return 2
        print("No workbook — skipping the drift check, running the derivation only.\n")

    # Batch 236: a refused MySQL connection used to print forty lines of
    # SQLAlchemy traceback. It is an operational condition, not a crash.
    db = SessionLocal()
    try:
        db.execute(text("SELECT 1"))
    except Exception as exc:
        db.close()
        print(f"Cannot reach the database ({exc.__class__.__name__}). "
              f"Start MySQL/Laragon and run this again.")
        return 3
    try:
        where, params = ["1=1"], {}
        if args.item:
            where.append("ri.inventory_code = :ic")
            params["ic"] = args.item
        stored = db.execute(text(f"""
            SELECT r.recipe_code, r.recipe_name, ri.inventory_code, ri.item_name,
                   COALESCE(ri.qty_batch, 0)        AS qty_batch,
                   COALESCE(ri.portions, 0)         AS portions,
                   COALESCE(ri.qty_per_portion, 0)  AS qty_per_portion,
                   COALESCE(r.standard_portions, 0) AS std_portions
              FROM recipe_ingredients ri
              JOIN recipes r ON r.id = ri.recipe_id
             WHERE {' AND '.join(where)}
             ORDER BY r.recipe_code, ri.line_no
        """), params).mappings().all()

        drift = []
        for row in (stored if book else []):
            key = (row["recipe_code"], row["inventory_code"])
            wb_lines = book.get(key)
            if not wb_lines:
                continue
            portions = _f(row["portions"]) or _f(row["std_portions"]) or 1.0
            db_gross_pp = _f(row["qty_batch"]) / portions if portions else 0.0
            db_net_pp = _f(row["qty_per_portion"])
            db_yield = (db_net_pp / db_gross_pp * 100) if db_gross_pp > 0 else None

            # A recipe may legitimately repeat an item; compare against the
            # closest workbook line so a duplicate never reads as drift.
            best = min(wb_lines, key=lambda w: abs(
                (w["gross"] / (w["portions"] or 1.0)) - db_gross_pp))
            wb_gross_pp = best["gross"] / (best["portions"] or 1.0)
            if wb_gross_pp <= 0:
                continue
            ratio = db_gross_pp / wb_gross_pp
            if abs(ratio - 1.0) > TOL:
                drift.append({
                    "recipe": row["recipe_code"], "recipe_name": row["recipe_name"],
                    "item": row["inventory_code"], "item_name": row["item_name"],
                    "db_gross_pp": db_gross_pp, "wb_gross_pp": wb_gross_pp,
                    "ratio": ratio,
                    "db_yield": db_yield, "wb_yield": best["yield"],
                })

        if book:
            print("=" * 78)
            print(f"DRIFT: stored gross differs from the workbook by more than {TOL:.1%}")
            print("=" * 78)
        if not book:
            pass
        elif not drift:
            print("None. Every stored recipe line matches the workbook — the issued\n"
                  "quantities are what this workbook asks for, and the gap you saw\n"
                  "is somewhere other than the recipe master.\n")
        else:
            drift.sort(key=lambda d: abs(d["ratio"] - 1.0), reverse=True)
            print(f"{len(drift)} line(s).\n")
            hdr = (f"{'RECIPE':<18}{'ITEM':<12}{'DB g/port':>12}{'BOOK g/port':>13}"
                   f"{'FACTOR':>9}{'DB yld%':>9}{'BOOK yld%':>11}")
            print(hdr)
            print("-" * len(hdr))
            for d in drift[:200]:
                print(f"{d['recipe']:<18}{d['item']:<12}"
                      f"{d['db_gross_pp']:>12.4f}{d['wb_gross_pp']:>13.4f}"
                      f"{d['ratio']:>9.5f}"
                      f"{(d['db_yield'] if d['db_yield'] is not None else 0):>9.3f}"
                      f"{d['wb_yield']:>11.3f}   {d['item_name'][:28]}")
            if len(drift) > 200:
                print(f"... and {len(drift) - 200} more")
            factors = sorted({round(d["ratio"], 5) for d in drift})
            if len(factors) <= 5:
                print(f"\nAll drift shares {len(factors)} factor(s): {factors}")
                print("A single shared factor means one stale number, not many small "
                      "errors —\nre-import the recipes and regenerate unissued BOMs.")

        # ------------------------------------------------------------------
        # The derivation, for the orders actually on the floor.
        # ------------------------------------------------------------------
        if args.order or args.item:
            ow, op = ["1=1"], {}
            if args.order:
                ow.append("bl.order_no = :o")
                op["o"] = args.order
            if args.item:
                ow.append("bl.ingredient_code = :ic2")
                op["ic2"] = args.item
            print("\n" + "=" * 78)
            print("DERIVATION — recipe row → issued quantity → kitchen received")
            print("=" * 78)
            lines = db.execute(text(f"""
                SELECT bl.id AS bom_line_id, bl.order_no, bl.recipe_no, bl.recipe_name,
                       bl.ingredient_code, bl.ingredient_name,
                       COALESCE(ol.required_portions, 0)                AS req_portions,
                       COALESCE(bl.net_required_qty_standard, 0)        AS net_req,
                       COALESCE(bl.total_required_with_waste_standard,0) AS issued_req,
                       COALESCE(bl.standard_uom, '')                    AS uom,
                       (SELECT SUM(COALESCE(s.issued_qty_standard, 0))
                          FROM store_issuance_lines s
                         WHERE s.bom_line_id = bl.id)                   AS store_issued,
                       (SELECT SUM(COALESCE(k.received_qty_standard, 0))
                          FROM kitchen_section_transactions k
                         WHERE k.bom_line_id = bl.id)                   AS kitchen_received
                  FROM bom_lines bl
                  LEFT JOIN order_lines ol
                         ON ol.order_no = bl.order_no AND ol.recipe_no = bl.recipe_no
                 WHERE {' AND '.join(ow)}
                 ORDER BY bl.recipe_no, bl.ingredient_name, bl.id
                 LIMIT 300
            """), op).mappings().all()
            for ln in lines:
                key = (ln["recipe_no"], ln["ingredient_code"])
                wb_lines = book.get(key) or []
                rp = _f(ln["req_portions"])
                expected = ""
                if wb_lines:
                    w = wb_lines[0]
                    wb_pp = w["gross"] / (w["portions"] or 1.0)
                    expected = (f"  book: {wb_pp:.4f}/port x {rp:.2f} = "
                                f"{wb_pp * rp:.4f} (yield {w['yield']:.1f}%)")
                print(f"\n{ln['recipe_no']} {ln['recipe_name']} — {ln['ingredient_name']} "
                      f"[{ln['ingredient_code']}] bom_line {ln['bom_line_id']}")
                print(f"  portions {rp:.2f} · net {_f(ln['net_req']):.4f} · "
                      f"issued req {_f(ln['issued_req']):.4f} {ln['uom']} · "
                      f"store issued {_f(ln['store_issued']):.4f} · "
                      f"kitchen received {_f(ln['kitchen_received']):.4f}")
                if expected:
                    print(expected)

                # ----------------------------------------------------------
                # Batch 236 — WHICH STAGE MOVED THE NUMBER.
                #
                # The quantity passes through three hand-offs on its way to
                # the Butchery screen. Print each one's delta so the stage
                # that changed it is named, rather than inferred from a
                # factor a second time. A stage with no delta is silent; the
                # first one that moves is the answer.
                # ----------------------------------------------------------
                bom_q = _f(ln["issued_req"])
                iss_q = _f(ln["store_issued"])
                kit_q = _f(ln["kitchen_received"])
                stages = []
                if expected:
                    book_q = wb_pp * rp
                    stages.append(("BOM     recipe book -> bom_lines", book_q, bom_q))
                if iss_q:
                    stages.append(("ISSUE   bom_lines  -> store issue", bom_q, iss_q))
                if kit_q:
                    stages.append(("KITCHEN store issue -> received  ", iss_q or bom_q, kit_q))
                culprit = None
                for name, a, b in stages:
                    delta = b - a
                    factor = (b / a) if a else 0.0
                    flag = ""
                    if abs(delta) > 0.001:
                        flag = "   <-- CHANGED HERE"
                        if culprit is None:
                            culprit = name.split()[0]
                    print(f"  {name}: {a:12.4f} -> {b:12.4f}  "
                          f"delta {delta:+9.4f}  factor {factor:.5f}{flag}")
                if culprit:
                    print(f"  >>> The quantity first changes at the {culprit} stage.")
                elif stages:
                    print("  >>> Unchanged end to end — this line is not the one to chase.")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
