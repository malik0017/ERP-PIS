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
from app.core.db_read import log_failure as db_read_log

MAIN_COMPONENT = "Main recipe"

# ---------------------------------------------------------------------------
# Batch 247 — BUTCHERY ITEM FILTER.
#
# Your note: *one more filter for the butchery section, on item code with item
# name, same as order no & customer name — but only these items: Fish (SFD2),
# Meat (MET1 and MET2), Chicken (PLT1 and PLT2), except PLT1-37 eggs.*
#
# The inventory code prefix IS the protein family in this item master, so the
# list is defined by prefix rather than by a category column that may or may
# not be filled. A prefix matches "<prefix>-" only, so PLT1 never swallows a
# future PLT10. Change the families here; the screen, the print sheet and the
# Excel export all read this one definition.
# ---------------------------------------------------------------------------
BUTCHERY_GROUPS: "OrderedDict[str, list[str]]" = OrderedDict([
    ("Fish", ["SFD2"]),
    ("Meat", ["MET1", "MET2"]),
    ("Chicken", ["PLT1", "PLT2"]),
])
BUTCHERY_EXCLUDE = {"PLT1-37"}   # eggs sit under PLT1 but are not butchery work


def butchery_group_of(code: str) -> str:
    """'Fish' / 'Meat' / 'Chicken' for a butchery item code, '' otherwise."""
    c = (code or "").strip().upper()
    if not c or c in BUTCHERY_EXCLUDE:
        return ""
    for group, prefixes in BUTCHERY_GROUPS.items():
        if any(c.startswith(p + "-") for p in prefixes):
            return group
    return ""


def _item_clause(f: dict, params: dict) -> str:
    """SQL for the item filter (alias `b`), or '' when no item is picked.

    Picked ITEMS match exactly. Picked GROUPS match by prefix, minus the
    exclusions. The two are OR-ed: "all Chicken + MET2-1051" is one pick.
    """
    items = [i for i in (f.get("items") or []) if i]
    groups = [g for g in (f.get("item_groups") or []) if g in BUTCHERY_GROUPS]
    if not items and not groups:
        return ""
    ors = []
    if items:
        binds = []
        for i, code in enumerate(items):
            k = f"it{i}"; binds.append(f":{k}"); params[k] = code
        ors.append(f"b.ingredient_code IN ({','.join(binds)})")
    if groups:
        likes = []
        n = 0
        for g in groups:
            for pfx in BUTCHERY_GROUPS[g]:
                k = f"ip{n}"; n += 1
                likes.append(f"UPPER(b.ingredient_code) LIKE :{k}"); params[k] = f"{pfx}-%"
        ex = []
        for i, code in enumerate(sorted(BUTCHERY_EXCLUDE)):
            k = f"ix{i}"; ex.append(f":{k}"); params[k] = code
        ors.append(f"(({' OR '.join(likes)}) AND UPPER(b.ingredient_code) NOT IN ({','.join(ex)}))")
    return "(" + " OR ".join(ors) + ")"


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
    if f.get("customers"):
        # Batch E (Image 1): pick several customers at once.
        binds = []
        for i, c in enumerate(f["customers"]):
            k = f"cu{i}"; binds.append(f":{k}"); params[k] = c
        clauses.append(f"o.customer_name IN ({','.join(binds)})")
    elif f.get("customer"):
        clauses.append("o.customer_name LIKE :cu"); params["cu"] = f"%{f['customer']}%"
    if f.get("order_nos"):
        # Batch E (Image 1): several orders at once, alongside the customer filter.
        binds = []
        for i, o in enumerate(f["order_nos"]):
            k = f"on{i}"; binds.append(f":{k}"); params[k] = o
        clauses.append(f"o.order_no IN ({','.join(binds)})")
    elif f.get("order_no"):
        clauses.append("o.order_no LIKE :on"); params["on"] = f"%{f['order_no']}%"
    if f.get("brands"):
        # Batch 236 (Image 1): brand joins customer and order as a multi-value
        # filter, because the unified finder lets you tick two brands the same
        # way it lets you tick two customers. A single ?brand= still works — it
        # arrives as a one-item list from _filters().
        binds = []
        for i, b in enumerate(f["brands"]):
            k = f"br{i}"; binds.append(f":{k}"); params[k] = b
        clauses.append(f"COALESCE(o.brand,'') IN ({','.join(binds)})")
    elif f.get("brand"):
        clauses.append("COALESCE(o.brand,'') LIKE :br"); params["br"] = f"%{f['brand']}%"
    if f.get("kitchen"):
        clauses.append("COALESCE(o.kitchen,'') LIKE :kt"); params["kt"] = f"%{f['kitchen']}%"
    if f.get("recipe"):
        clauses.append("b.recipe_no = :rc"); params["rc"] = f["recipe"]
    # Batch 247: butchery item / protein-group filter (every BOQ view).
    item_sql = _item_clause(f, params)
    if item_sql:
        clauses.append(item_sql)
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
                   ri.qty_per_portion, ri.uom, ri.portions AS line_portions
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
                   b.net_required_qty_standard AS net_qty_col,
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

    # Batch 248: meal-plan lines show the PLAN's per-portion quantity, not the
    # recipe master's base (which for Salus is the sum of all four plans).
    line_plan: dict[int, str] = {}
    plan_qty: dict[tuple, float] = {}
    try:
        ol_ids = sorted({int(r["order_line_id"]) for r in bom if r["order_line_id"]})
        if ol_ids:
            for _id, _p in db.execute(text(
                    "SELECT id, COALESCE(plan_code,'') FROM order_lines WHERE id IN :ids"
            ).bindparams(bindparam("ids", expanding=True)), {"ids": ol_ids}).all():
                if _p:
                    line_plan[int(_id)] = _p
        if line_plan:
            ri_ids = sorted({ln["ri_id"] for m in masters.values() for ln in m["lines"]})
            for _ri, _p, _q in db.execute(text(
                    "SELECT recipe_ingredient_id, plan_code, qty_batch FROM recipe_ingredient_plans "
                    "WHERE recipe_ingredient_id IN :ids"
            ).bindparams(bindparam("ids", expanding=True)), {"ids": ri_ids}).all():
                plan_qty[(int(_ri), _p)] = _f(_q)
    except Exception:
        line_plan, plan_qty = {}, {}
    # Batch E (Image 1): one or several sections.
    want_sections = set(f.get("sections") or [])
    if not want_sections and (f.get("section") or "").strip():
        want_sections = {f["section"].strip()}

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
        if want_sections and not (want_sections & {kitchen, b["issue_section"]}):
            continue

        portions = _f(b["portions"])
        per_portion = (_f(b["required_recipe_uom"]) / portions) if portions else 0.0
        # --- Batch 205 (Image 1): NET vs GROSS -------------------------------
        # BOM_QTY_BASIS is "gross_prep", so bom_lines.required_qty holds the
        # PRE-TRIM (gross) weight on any line with a yield loss — right for the
        # store, wrong on the chef's sheet: the pot takes the NET weight from
        # the recipe. Net is recomputed from the paired recipe line
        # (qty_per_portion = the workbook's "NET Qty req per Batch" ÷ portions)
        # and converted with the same factor the BOM used, so no schema change
        # and historical orders are corrected too.
        gross_qty = _f(b["required_qty"])
        conv = (gross_qty / _f(b["required_recipe_uom"])) if _f(b["required_recipe_uom"]) else 1.0
        # Batch 206: prefer the stored net requirement; fall back to
        # recomputing from the recipe for BOMs generated before it existed.
        net_pp = _f((ri or {}).get("qty_per_portion"))
        _plan = line_plan.get(int(b["order_line_id"] or 0), "")
        if _plan and ri and (ri["ri_id"], _plan) in plan_qty:
            _mp = _f(ri.get("line_portions")) or \
                _f((masters.get(b["recipe_no"] or "", {}) or {}).get("standard_portions")) or 1.0
            net_pp = plan_qty[(ri["ri_id"], _plan)] / _mp
        if b.get("net_qty_col") is not None:
            net_qty = _f(b["net_qty_col"])
        else:
            net_qty = net_pp * portions * conv if net_pp > 0 else gross_qty
        if net_qty > gross_qty:
            net_qty = gross_qty  # never show a net above the issued weight
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
            "net_per_portion": net_pp,
            "recipe_uom": b["recipe_uom"],
            "required_qty": net_qty,              # Batch 205: NET is the chef figure
            "issue_qty": gross_qty,               # what the store hands over
            "yield_loss": round(gross_qty - net_qty, 3),
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
                                                    "required_qty": 0.0, "issue_qty": 0.0,
                                                    "yield_loss": 0.0})
                    agg["required_qty"] += ln["required_qty"]
                    agg["issue_qty"] += ln["issue_qty"]
                    agg["yield_loss"] += ln["yield_loss"]
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
        # Batch 159-4 (Image 7): order shown WITH its customer in one field.
        "orders_detailed": _rows(db, f"""
                SELECT o.order_no, COALESCE(o.customer_name,'') AS customer
                FROM customer_orders o WHERE {open_clause}
                ORDER BY o.order_no DESC LIMIT 500""", cid),
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
        # Batch 236 (Image 1): ONE list for ONE box. See finder_options().
        "finder": finder_options(db, cid),
        # Batch 247: the butchery item finder.
        "item_finder": butchery_item_options(db, cid),
    }


def butchery_item_options(db: Session, cid: int) -> list[dict]:
    """Batch 247 — entries for the Butchery Items finder.

    Same shape as finder_options(): the three protein groups first (one pick =
    the whole family), then every butchery item that appears on an open
    order's BOM — so, as with the order finder, every choice returns rows.
    """
    open_clause = ("(o.company_id = :cid OR o.company_id IS NULL) "
                   "AND COALESCE(o.status,'') NOT IN ('Cancelled','Rejected')")
    rows = _rows(db, f"""
        SELECT b.ingredient_code, MAX(COALESCE(b.ingredient_name, b.ingredient_code)) AS name,
               COUNT(DISTINCT b.order_no) AS orders
          FROM bom_lines b
          JOIN customer_orders o ON o.order_no = b.order_no
         WHERE {open_clause}
         GROUP BY b.ingredient_code
         ORDER BY name
         LIMIT 3000""", cid)
    items, per_group = [], {g: 0 for g in BUTCHERY_GROUPS}
    for code, name, orders in rows:
        group = butchery_group_of(code)
        if not group:
            continue
        per_group[group] += 1
        items.append({"kind": "item", "value": code, "label": f"{code} · {name}",
                      "sub": f"{group} · {orders} order(s)",
                      "search": f"{code} {name} {group}".lower()})
    head = [{"kind": "group", "value": g, "label": f"All {g}",
             "sub": f"{', '.join(BUTCHERY_GROUPS[g])} · {per_group[g]} item(s)",
             "search": f"{g} {' '.join(BUTCHERY_GROUPS[g])}".lower()}
            for g in BUTCHERY_GROUPS]
    return head + items


def finder_options(db: Session, cid: int) -> list[dict]:
    """Batch 236 (Image 1) — everything the one search box can find.

    The screen had three separate pickers stacked across the filter bar:
    Customer (multi), Order No (multi) and Brand (free text + datalist). They
    filter the same thing — which orders are in scope — so the chef had to
    decide WHICH BOX a thing lived in before typing it, and "Ma'una" belongs to
    two of them. Your note: *merge all three fields into one, user can search
    with customer name, order code and brand*.

    One list, three kinds of entry, each carrying the parameter it submits:

        {"kind": "order",    "value": "ORD-20260906-0001",
         "label": "ORD-20260906-0001", "sub": "Ma'una Foundation (FRSH)",
         "search": "ord-20260906-0001 ma'una foundation (frsh) gourmet 360"}

    `search` is everything about the entry lowercased and concatenated, so
    typing a customer name finds that customer's ORDERS as well as the
    customer itself — which is what someone means when they type it.

    The kinds map onto the existing request parameters (customer / order_no /
    brand), so nothing downstream changes, old bookmarks keep working, and the
    Excel export and print sheet read the same filter they always did.
    """
    open_clause = ("(o.company_id = :cid OR o.company_id IS NULL) "
                   "AND COALESCE(o.status,'') NOT IN ('Cancelled','Rejected')")
    rows = _rows(db, f"""
        SELECT o.order_no,
               COALESCE(o.customer_name,'') AS customer,
               COALESCE(o.brand,'')         AS brand,
               o.required_delivery_date     AS delivery_date
          FROM customer_orders o
         WHERE {open_clause}
         ORDER BY o.required_delivery_date DESC, o.order_no DESC
         LIMIT 800""", cid)

    out: list[dict] = []
    customers: "OrderedDict[str, dict]" = OrderedDict()
    brands: "OrderedDict[str, dict]" = OrderedDict()

    for r in rows:
        order_no, customer, brand, delivery = r[0], r[1] or "", r[2] or "", r[3]
        if not order_no:
            continue
        sub_bits = [x for x in (customer, brand, str(delivery) if delivery else "") if x]
        out.append({
            "kind": "order", "value": order_no, "label": order_no,
            "sub": " · ".join(sub_bits),
            "search": " ".join([order_no, customer, brand]).lower(),
        })
        if customer:
            c = customers.setdefault(customer, {"orders": 0, "brands": set()})
            c["orders"] += 1
            if brand:
                c["brands"].add(brand)
        if brand:
            b = brands.setdefault(brand, {"orders": 0})
            b["orders"] += 1

    # Customers and brands go FIRST: picking "every order for this customer" is
    # the broader, more common intent, and a long order list underneath would
    # otherwise bury them.
    head: list[dict] = []
    for name, c in customers.items():
        head.append({
            "kind": "customer", "value": name, "label": name,
            "sub": f"{c['orders']} order(s)" + (f" · {', '.join(sorted(c['brands']))}"
                                                if c["brands"] else ""),
            "search": (name + " " + " ".join(c["brands"])).lower(),
        })
    for name, b in brands.items():
        head.append({
            "kind": "brand", "value": name, "label": name,
            "sub": f"{b['orders']} order(s)",
            "search": name.lower(),
        })
    return head + out


def _rows(db: Session, sql: str, cid: int) -> list:
    try:
        return db.execute(text(sql), {"cid": cid}).all()
    except Exception as _exc:
        # Batch 221: logged, not swallowed — a silent except here makes
        # a broken query look like an empty table (app/core/db_read.py).
        db_read_log(_exc, sql, 'boq_service.py._rows')
        return []
