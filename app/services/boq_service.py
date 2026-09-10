# app/services/boq_service.py
# =============================================================================
# Batch 200 — BILL OF QUANTITY: the chef's view.
# -----------------------------------------------------------------------------
# The BOQ screen (Batch 105/106) answered the STORE's question — "what do I pull
# off the shelf" — one line per ingredient. The kitchen's question is different:
# "for THIS order, what goes into THIS recipe, component by component, and how
# much". That is the paper sheet the chefs work from today (order → recipe →
# sub-recipe such as "Ranch sauce" / "Garnish / Tray line" → ingredient → qty).
#
# WHERE THE NUMBERS COME FROM — deliberately NOT a second explosion:
#   * Required quantity  = bom_lines.total_required_with_waste_standard, i.e.
#     the exact figure the store issues against. The chef sheet, the pick list
#     and the store issuance screen therefore can never disagree.
#   * Sub-recipe label   = recipe_ingredients.sub_recipe_code (the workbook's
#     "Sub Recipe Description", stored there since Batch 128).
#   * Kitchen section    = recipe_ingredients.kitchen_section (the workbook's
#     "Section" column), mapped through map_excel_section().
#
# bom_lines carries no link to recipe_ingredients.id, so each BOM line is
# PAIRED to its recipe line: BOM generation walks recipe lines in line_no
# order and inserts one BOM row per line, so the n-th BOM row for an
# (order_line, ingredient_code) matches the n-th recipe row with that code.
# This matters when one ingredient appears twice in a recipe (Fresh Mint in the
# main recipe AND in the sauce) — a plain JOIN on the code would double both.
# =============================================================================
from __future__ import annotations

from collections import OrderedDict
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from app.core.production_constants import map_excel_section

MAIN_COMPONENT = "Main recipe"


def _f(v: Any) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def order_where(f: dict, cid: int) -> tuple[str, dict]:
    """Order-level filter shared by every BOQ view (alias `o`)."""
    clauses = ["(o.company_id = :cid OR o.company_id IS NULL)",
               "COALESCE(o.status,'') NOT IN ('Cancelled','Rejected')"]
    params: dict = {"cid": cid}
    if f.get("date_from"):
        clauses.append("o.required_delivery_date >= :df"); params["df"] = f["date_from"]
    if f.get("date_to"):
        clauses.append("o.required_delivery_date <= :dt"); params["dt"] = f["date_to"]
    if f.get("customer"):
        clauses.append("o.customer_name LIKE :cu"); params["cu"] = f"%{f['customer']}%"
    if f.get("order_no"):
        clauses.append("o.order_no LIKE :on"); params["on"] = f"%{f['order_no']}%"
    if f.get("brand"):
        clauses.append("COALESCE(o.brand,'') LIKE :br"); params["br"] = f"%{f['brand']}%"
    if f.get("kitchen"):
        clauses.append("COALESCE(o.kitchen,'') LIKE :kt"); params["kt"] = f"%{f['kitchen']}%"
    if f.get("recipe"):
        clauses.append("b.recipe_no = :rc"); params["rc"] = f["recipe"]
    return " AND ".join(clauses), params


def _recipe_lines(db: Session, codes: list[str], cid: int) -> dict[str, dict]:
    """Latest active version of each recipe code -> header + ordered lines."""
    if not codes:
        return {}
    try:
        rows = db.execute(text("""
            SELECT r.id AS recipe_id, r.recipe_code, r.recipe_name, r.category,
                   r.standard_portions, r.version,
                   ri.id AS ri_id, ri.line_no, ri.inventory_code, ri.item_name,
                   ri.sub_recipe_code, ri.kitchen_section, ri.cutting_portion_size,
                   ri.qty_per_portion, ri.uom
            FROM recipes r
            JOIN recipe_ingredients ri ON ri.recipe_id = r.id
            WHERE r.recipe_code IN :codes
              AND (r.company_id = :cid OR r.company_id IS NULL)
              AND UPPER(TRIM(COALESCE(r.status,''))) = 'ACTIVE'
              AND COALESCE(r.is_active, 1) = 1
            ORDER BY r.recipe_code, r.version DESC, r.id DESC, ri.line_no
        """).bindparams(bindparam("codes", expanding=True)),
            {"codes": list(codes), "cid": cid}).mappings().all()
    except Exception:
        return {}

    out: dict[str, dict] = {}
    for r in rows:
        code = r["recipe_code"]
        head = out.get(code)
        if head is None:
            head = out[code] = {"recipe_id": r["recipe_id"], "category": r["category"] or "",
                                "standard_portions": _f(r["standard_portions"]), "lines": []}
        if r["recipe_id"] != head["recipe_id"]:
            continue  # an older version of the same recipe — ignore
        head["lines"].append(dict(r))
    return out


def recipe_sheet(db: Session, f: dict, cid: int) -> list[dict]:
    """Order → recipe → component (sub-recipe) → ingredient lines.

    Returns a list of order dicts, each with `recipes`, each recipe with
    `components` (ordered as they appear in the recipe master), each component
    with `lines`. Section filter (f['section']) keeps only matching lines and
    drops recipes/orders left empty.
    """
    where, params = order_where(f, cid)
    try:
        bom = db.execute(text(f"""
            SELECT b.id, b.order_no, b.order_line_id, b.recipe_no, b.recipe_name,
                   b.ingredient_code, b.ingredient_name,
                   COALESCE(b.ingredient_main_category, '') AS main_category,
                   COALESCE(b.recipe_uom, '')   AS recipe_uom,
                   COALESCE(b.required_qty_recipe_uom, 0) AS required_recipe_uom,
                   COALESCE(b.standard_uom, '') AS uom,
                   COALESCE(b.total_required_with_waste_standard, b.required_qty_standard, 0) AS required_qty,
                   COALESCE(b.default_issue_section, '') AS issue_section,
                   COALESCE(b.estimated_cost, 0) AS est_cost,
                   o.customer_name, COALESCE(o.brand,'') AS brand,
                   o.required_delivery_date AS delivery_date,
                   o.cooking_date, COALESCE(o.status,'') AS order_status,
                   COALESCE(ol.line_no, 0) AS order_line_no,
                   COALESCE(ol.required_portions, 0) AS portions
            FROM bom_lines b
            JOIN customer_orders o ON o.order_no = b.order_no
            LEFT JOIN order_lines ol ON ol.id = b.order_line_id
            WHERE {where}
            ORDER BY o.required_delivery_date, b.order_no, ol.line_no, b.id
            LIMIT 12000
        """), params).mappings().all()
    except Exception:
        return []
    if not bom:
        return []

    masters = _recipe_lines(db, sorted({r["recipe_no"] for r in bom if r["recipe_no"]}), cid)
    want_section = (f.get("section") or "").strip()

    # used[(order_line_id or order/recipe key, recipe line id)] — pairing state
    used: dict[tuple, set] = {}
    orders: "OrderedDict[str, dict]" = OrderedDict()

    for b in bom:
        o = orders.get(b["order_no"])
        if o is None:
            o = orders[b["order_no"]] = {
                "order_no": b["order_no"], "customer_name": b["customer_name"] or "",
                "brand": b["brand"], "delivery_date": b["delivery_date"],
                "cooking_date": b["cooking_date"], "status": b["order_status"],
                "recipes": OrderedDict(), "line_count": 0, "portions": 0.0,
            }
        rkey = (b["order_line_id"] or 0, b["recipe_no"] or "")
        rec = o["recipes"].get(rkey)
        master = masters.get(b["recipe_no"] or "", {})
        if rec is None:
            rec = o["recipes"][rkey] = {
                "recipe_no": b["recipe_no"] or "", "recipe_name": b["recipe_name"] or "",
                "category": master.get("category", ""), "portions": _f(b["portions"]),
                "order_line_no": b["order_line_no"], "components": OrderedDict(),
                "sections": set(), "est_cost": 0.0,
            }
            o["portions"] += _f(b["portions"])

        # --- pair this BOM row to its recipe line --------------------------
        pair_key = (b["order_no"],) + rkey
        taken = used.setdefault(pair_key, set())
        ri = None
        for cand in master.get("lines", []):
            cand_code = cand["inventory_code"] or f"NO-CODE-{cand['ri_id']}"
            if cand["ri_id"] in taken or cand_code != b["ingredient_code"]:
                continue
            ri = cand
            taken.add(cand["ri_id"])
            break

        component = ((ri or {}).get("sub_recipe_code") or "").strip() or MAIN_COMPONENT
        kitchen = map_excel_section((ri or {}).get("kitchen_section")) or b["issue_section"] or "—"
        if want_section and want_section not in (kitchen, b["issue_section"]):
            continue

        portions = _f(b["portions"])
        per_portion = (_f(b["required_recipe_uom"]) / portions) if portions else 0.0
        comp = rec["components"].setdefault(component, {
            "name": component, "sort": (ri or {}).get("line_no") or 10 ** 6, "lines": []})
        comp["lines"].append({
            "line_no": (ri or {}).get("line_no") or 10 ** 6,
            "ingredient_code": b["ingredient_code"],
            "ingredient_name": b["ingredient_name"],
            "main_category": b["main_category"],
            "kitchen_section": kitchen,
            "issue_section": b["issue_section"] or "—",
            "cutting": (ri or {}).get("cutting_portion_size") or "",
            "per_portion": per_portion,
            "recipe_uom": b["recipe_uom"],
            "required_qty": _f(b["required_qty"]),
            "uom": b["uom"],
        })
        rec["sections"].add(kitchen)
        rec["est_cost"] += _f(b["est_cost"])
        o["line_count"] += 1

    result = []
    for o in orders.values():
        recipes = []
        for rec in o["recipes"].values():
            comps = sorted(rec["components"].values(),
                           key=lambda c: (c["name"] != MAIN_COMPONENT, c["sort"]))
            for c in comps:
                c["lines"].sort(key=lambda ln: ln["line_no"])
            if not comps:
                continue
            rec["components"] = comps
            rec["sections"] = sorted(s for s in rec["sections"] if s)
            rec["line_count"] = sum(len(c["lines"]) for c in comps)
            recipes.append(rec)
        if recipes:
            o["recipes"] = recipes
            o["recipe_count"] = len(recipes)
            o["line_count"] = sum(r["line_count"] for r in recipes)
            result.append(o)
    return result


def by_section(sheet: list[dict]) -> list[dict]:
    """Kitchen section → recipe (with order) → ingredient lines.

    Built from the recipe sheet, so the quantities are identical by
    construction. A recipe whose lines are cooked in two sections (e.g. a salad
    whose chicken is prepped in Butchery) appears under both, each with only
    its own lines — that is what each section chef actually needs to see.
    """
    sections: "OrderedDict[str, dict]" = OrderedDict()
    for o in sheet:
        for rec in o["recipes"]:
            for comp in rec["components"]:
                for ln in comp["lines"]:
                    s = sections.setdefault(ln["kitchen_section"], {
                        "section": ln["kitchen_section"], "recipes": OrderedDict(),
                        "orders": set(), "lines": 0})
                    rk = (o["order_no"], rec["recipe_no"], rec["order_line_no"])
                    r = s["recipes"].setdefault(rk, {
                        "order_no": o["order_no"], "customer_name": o["customer_name"],
                        "delivery_date": o["delivery_date"], "recipe_no": rec["recipe_no"],
                        "recipe_name": rec["recipe_name"], "portions": rec["portions"],
                        "lines": []})
                    r["lines"].append({**ln, "component": comp["name"]})
                    s["orders"].add(o["order_no"])
                    s["lines"] += 1
    out = []
    for name in sorted(sections, key=lambda x: (x == "—", x)):
        s = sections[name]
        out.append({"section": name, "recipes": list(s["recipes"].values()),
                    "order_count": len(s["orders"]), "lines": s["lines"]})
    return out


def by_recipe(sheet: list[dict]) -> list[dict]:
    """Recipe across every filtered order → total portions → ingredient totals.

    For batch cooking: one recipe served to three customers is cooked once.
    Lines are aggregated per (component, ingredient, uom).
    """
    recipes: "OrderedDict[str, dict]" = OrderedDict()
    for o in sheet:
        for rec in o["recipes"]:
            r = recipes.setdefault(rec["recipe_no"], {
                "recipe_no": rec["recipe_no"], "recipe_name": rec["recipe_name"],
                "category": rec["category"], "portions": 0.0, "orders": [],
                "sections": set(), "lines": OrderedDict()})
            r["portions"] += rec["portions"]
            r["orders"].append({"order_no": o["order_no"], "customer_name": o["customer_name"],
                                "portions": rec["portions"], "delivery_date": o["delivery_date"]})
            r["sections"].update(rec["sections"])
            for comp in rec["components"]:
                for ln in comp["lines"]:
                    k = (comp["name"], ln["ingredient_code"], ln["uom"])
                    agg = r["lines"].setdefault(k, {**ln, "component": comp["name"],
                                                    "required_qty": 0.0})
                    agg["required_qty"] += ln["required_qty"]
    out = []
    for r in sorted(recipes.values(), key=lambda x: x["recipe_name"] or ""):
        lines = sorted(r["lines"].values(),
                       key=lambda ln: (ln["component"] != MAIN_COMPONENT, ln["component"], ln["line_no"]))
        out.append({**r, "sections": sorted(r["sections"]), "lines": lines})
    return out


def picker_options(db: Session, cid: int) -> dict:
    """Dropdown sources for the BOQ filter bar — only values that can return rows."""
    def _opts(sql: str) -> list:
        try:
            return [r[0] for r in db.execute(text(sql), {"cid": cid}).all() if r[0]]
        except Exception:
            return []

    open_clause = ("(o.company_id = :cid OR o.company_id IS NULL) "
                   "AND COALESCE(o.status,'') NOT IN ('Cancelled','Rejected')")
    return {
        "customers": _opts(f"SELECT DISTINCT o.customer_name FROM customer_orders o "
                           f"WHERE {open_clause} ORDER BY o.customer_name LIMIT 500"),
        "orders": _opts(f"SELECT DISTINCT o.order_no FROM customer_orders o "
                        f"WHERE {open_clause} ORDER BY o.order_no DESC LIMIT 500"),
        "brands": _opts(f"SELECT DISTINCT o.brand FROM customer_orders o "
                        f"WHERE {open_clause} AND COALESCE(o.brand,'') <> '' ORDER BY o.brand LIMIT 200"),
        "recipes": [
            {"code": r[0], "name": r[1]} for r in _rows(db, f"""
                SELECT b.recipe_no, MAX(b.recipe_name) AS recipe_name FROM bom_lines b
                JOIN customer_orders o ON o.order_no = b.order_no
                WHERE {open_clause} AND COALESCE(b.recipe_no,'') <> ''
                GROUP BY b.recipe_no ORDER BY recipe_name LIMIT 800""", cid)
        ],
        "sections": ["Cutting", "Butchery", "Hot Kitchen", "Cold Kitchen",
                     "Bakery/Pastry", "Trayline / Packing"],
    }


def _rows(db: Session, sql: str, cid: int) -> list:
    try:
        return db.execute(text(sql), {"cid": cid}).all()
    except Exception:
        return []
