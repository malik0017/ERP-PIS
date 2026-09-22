# app/modules/production/routes_boq.py
# =============================================================================
# Batch 105 — BILL OF QUANTITY export for the kitchen
# -----------------------------------------------------------------------------
# What the Head Chef screen was missing: a way to get the consolidated material
# requirement OUT of the system and onto the pass, filtered the way the kitchen
# actually works.
#
# Two views, because they answer two different questions:
#
#   ORDER-WISE     "what does THIS order need"      -> one sheet per order
#   CONSOLIDATED   "what do I need to pull from the
#                   store this morning, in total"    -> one line per ingredient
#                                                      across every order in
#                                                      the filter
#
# The consolidated view is the one that matters at 5am: a store keeper does not
# want twelve separate lists that each ask for flour, they want one line saying
# 43 kg. This is the same explosion the BOM screen does — deliberately reusing
# bom_lines rather than re-imploding the recipes, so the printed sheet and the
# on-screen BOM can never disagree.
#
# SAP B1 calls this the Pick and Pack / Production Order component list; Odoo
# calls it the MO component report. Same document, same purpose.
# =============================================================================
from __future__ import annotations

import io
from datetime import date

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.rbac import require_area
from app.core.templates import render
from app.database.session import get_db
from app.services.boq_service import (
    MAIN_COMPONENT, by_recipe, by_section, order_where, picker_options, recipe_sheet,
)

router = APIRouter(prefix="/production/boq", tags=["Production"])


def _cid(request: Request) -> int:
    return int(request.session.get("company_id") or 1)


def _filters(request: Request) -> dict:
    q = request.query_params
    # Batch E (Image 1): customer / order / section are multi-select now.
    # getlist() returns every repeated param; a single value still arrives as a
    # one-item list, so old single-value bookmarks keep working.
    customers = [c.strip() for c in q.getlist("customer") if c.strip()]
    order_nos = [o.strip() for o in q.getlist("order_no") if o.strip()]
    sections = [s.strip() for s in q.getlist("section") if s.strip()]
    # Batch 236 (Image 1): brand is multi-value now, like customer and order —
    # the unified finder lets you tick two brands the same way. A single
    # ?brand= from an old bookmark still arrives here as a one-item list.
    brands = [b.strip() for b in q.getlist("brand") if b.strip()]
    return {
        "date_from": (q.get("date_from") or "").strip(),
        "date_to": (q.get("date_to") or "").strip(),
        "customer": customers[0] if customers else "",
        "order_no": order_nos[0] if order_nos else "",
        "customers": customers,
        "order_nos": order_nos,
        "sections": sections,
        "brand": brands[0] if brands else "",
        "brands": brands,
        "kitchen": (q.get("kitchen") or "").strip(),
        "recipe": (q.get("recipe") or "").strip(),
        "section": sections[0] if sections else "",
        "view": (q.get("view") or "recipe").strip(),
        # Batch 236 (Image 1): columns the user switched off on screen, so the
        # printed sheet matches what they are looking at. Empty = print
        # everything, which is what every existing link does.
        "hide": [h.strip() for h in q.getlist("hide") if h.strip()],
    }


def _where(f: dict, cid: int) -> tuple[str, dict]:
    # Batch 200: single definition lives in boq_service so the chef sheet,
    # pick list and export filter identically (it also adds the recipe filter).
    return order_where(f, cid)


def consolidated(db: Session, f: dict, cid: int) -> list[dict]:
    where, params = _where(f, cid)
    if f.get("sections"):
        # Batch E (Image 1): the pick list can target several sections at once.
        binds = []
        for i, s in enumerate(f["sections"]):
            k = f"sec{i}"; binds.append(f":{k}"); params[k] = s
        where += (" AND COALESCE(NULLIF(b.default_issue_section, ''), i.default_issue_section, '') "
                  f"IN ({','.join(binds)})")
    elif f.get("section"):
        where += " AND COALESCE(NULLIF(b.default_issue_section, ''), i.default_issue_section, '') = :sec"
        params["sec"] = f["section"]
    try:
        return [dict(r) for r in db.execute(text(f"""
            SELECT b.ingredient_code,
                   MAX(COALESCE(b.ingredient_name, b.ingredient_code)) AS item_name,
                   MAX(COALESCE(b.standard_uom, ''))            AS uom,
                   MAX(COALESCE(i.storage_type, ''))            AS storage_type,
                   MAX(COALESCE(NULLIF(b.default_issue_section, ''), i.default_issue_section, '')) AS section,
                   -- Batch 105: total_required_with_waste_standard, NOT a
                   -- "total_qty_standard" (which does not exist). Waste is
                   -- included deliberately: a pick list that ignores expected
                   -- wastage sends the kitchen short every single time.
                   SUM(COALESCE(b.total_required_with_waste_standard,
                                b.required_qty_standard, 0))  AS required_qty,
                   COUNT(DISTINCT b.order_no)                   AS order_count,
                   -- Batch 123: show WHO/WHICH orders this consolidated pull is
                   -- for, so the store isn't looking at anonymous totals.
                   GROUP_CONCAT(DISTINCT o.customer_name ORDER BY o.customer_name SEPARATOR ', ') AS customers,
                   GROUP_CONCAT(DISTINCT b.order_no ORDER BY b.order_no SEPARATOR ', ') AS orders,
                   MAX(COALESCE(b.unit_cost_standard, i.unit_cost_standard, 0)) AS unit_cost
            FROM bom_lines b
            JOIN customer_orders o ON o.order_no = b.order_no
            LEFT JOIN ingredients i ON i.ingredient_code = b.ingredient_code
            WHERE {where}
            GROUP BY b.ingredient_code
            ORDER BY section, item_name
            LIMIT 3000
        """), params).mappings().all()]
    except Exception:
        return []


def order_wise(db: Session, f: dict, cid: int) -> list[dict]:
    where, params = _where(f, cid)
    try:
        return [dict(r) for r in db.execute(text(f"""
            SELECT b.order_no,
                   COALESCE(o.customer_name, '') AS customer_name,
                   COALESCE(o.brand, '')         AS brand,
                   o.required_delivery_date      AS delivery_date,
                   COALESCE(b.recipe_no, '')     AS recipe_no,
                   COALESCE(b.recipe_name, '')   AS recipe_name,
                   COALESCE(b.ingredient_main_category, '') AS main_category,
                   COALESCE(b.ingredient_sub_category, '')  AS sub_category,
                   b.ingredient_code,
                   COALESCE(b.ingredient_name, b.ingredient_code) AS item_name,
                   COALESCE(b.standard_uom, '')  AS uom,
                   -- Batch 206: the BOQ reports NET; the store issues GROSS.
                   COALESCE(b.net_required_qty_standard,
                            b.total_required_with_waste_standard,
                            b.required_qty_standard, 0) AS required_qty,
                   COALESCE(b.total_required_with_waste_standard,
                            b.required_qty_standard, 0) AS issue_qty,
                   -- Batch 194-A (Img 4): "BOQ should include Recipe name,
                   -- Sub recipe Description, also include protein items,
                   -- vegetable, gram, etc." Recipe name and category
                   -- (protein/vegetable/dry/etc) already exist on bom_lines
                   -- directly, added above. Sub-recipe description does
                   -- NOT exist as its own imported field anywhere in this
                   -- system (checked the importer — your workbook's "Sub
                   -- Recipe Description" column isn't captured into its own
                   -- DB field on import). Same technique already proven in
                   -- Section Report (Batch 181): when this line's
                   -- ingredient_code is ITSELF another recipe's code (i.e.
                   -- something made in-house, like a stock or base, rather
                   -- than a purchased item), that recipe's own name serves
                   -- as its description. Scalar subquery, costs nothing on
                   -- the common case (a real ingredient, no match, shows "").
                   (SELECT MAX(rc2.recipe_name) FROM recipes rc2
                     WHERE rc2.recipe_code = b.ingredient_code
                       AND (rc2.company_id = :cid2 OR rc2.company_id IS NULL)) AS sub_recipe_description
            FROM bom_lines b
            JOIN customer_orders o ON o.order_no = b.order_no
            WHERE {where}
            ORDER BY o.required_delivery_date, b.order_no, b.ingredient_name
            LIMIT 8000
        """), {**params, "cid2": cid}).mappings().all()]
    except Exception:
        return []


def _stamp(f: dict) -> str:
    """The filter, written out for the top of a printed sheet.

    Batch 160-1 stopped this crashing — f holds LISTS (customers, order_nos,
    sections) alongside the single-value mirrors of the same data, and feeding
    a list to str.join raised "sequence item N: expected str instance, list
    found" on Print Recipe Sheet and Excel export.

    Batch 236 (Image 2) fixes what it then PRINTED. Flattening every key meant
    both mirrors were emitted, so a sheet filtered to two orders was stamped
    "Filter: ORD-20260906-0001, ORD-20260906-0001" — the first order twice and
    the second one missing, which is worse than no stamp at all because it
    looks authoritative. Each filter is now named once, by its label, from the
    LIST form only.
    """
    def _fv(v) -> str:
        if isinstance(v, (list, tuple, set)):
            return ", ".join(str(x) for x in v)
        return str(v)

    bits: list[str] = []
    if f.get("date_from") or f.get("date_to"):
        bits.append(f"Delivery {f.get('date_from') or '…'} → {f.get('date_to') or '…'}")
    for label, list_key, single_key in (
        ("Orders", "order_nos", "order_no"),
        ("Customers", "customers", "customer"),
        ("Brands", "brands", "brand"),
        ("Sections", "sections", "section"),
    ):
        val = f.get(list_key) or ([f[single_key]] if f.get(single_key) else [])
        if val:
            bits.append(f"{label}: {_fv(val)}")
    if f.get("recipe"):
        bits.append(f"Recipe: {f['recipe']}")
    if f.get("kitchen"):
        bits.append(f"Kitchen: {f['kitchen']}")
    return (f"Generated {date.today().isoformat()} · "
            f"Filter: {' · '.join(bits) or 'all open orders'}")


def _customers_summary(ow: list[dict]) -> list[dict]:
    agg: dict[str, dict] = {}
    for x in ow:
        e = agg.setdefault(x["customer_name"] or "—", {"orders": set(), "items": set(), "qty": 0.0})
        e["orders"].add(x["order_no"])
        e["items"].add(x["ingredient_code"])
        e["qty"] += float(x["required_qty"] or 0)
    # Batch 108: key is "item_count", NOT "items" — {{ x.items }} resolves to
    # the dict's built-in .items method in Jinja.
    return sorted([{"customer": k, "orders": len(v["orders"]), "item_count": len(v["items"]),
                    "qty": round(v["qty"], 3)} for k, v in agg.items()], key=lambda r: -r["qty"])


@router.get("/export")
def export_boq(request: Request, db: Session = Depends(get_db)):
    """Bill of Quantity workbook.

    Batch 200 sheet order: the chef's Recipe Sheet first (it is the document
    the kitchen prints), then By Section, By Recipe, the store's consolidated
    Pick List, the flat By Order table (kept for Excel filtering) and By Customer.
    """
    require_area(request, "bom")
    cid = _cid(request)
    f = _filters(request)

    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    wb = Workbook()
    head = PatternFill("solid", fgColor="132947")
    order_fill = PatternFill("solid", fgColor="1E5BB8")
    recipe_fill = PatternFill("solid", fgColor="D9E6F7")
    comp_fill = PatternFill("solid", fgColor="F2F5F9")
    sub = PatternFill("solid", fgColor="EAEFF5")
    thin = Side(style="thin", color="D0D7E2")
    box = Border(bottom=thin)
    stamp = _stamp(f)

    def header(ws, cols, title, subtitle, widths=None):
        ws["A1"] = title
        ws["A1"].font = Font(bold=True, size=14)
        ws["A2"] = subtitle
        ws["A2"].font = Font(italic=True, size=9)
        for i, h in enumerate(cols, start=1):
            c = ws.cell(row=4, column=i, value=h)
            c.font = Font(bold=True, color="FFFFFF")
            c.fill = head
            c.alignment = Alignment(wrap_text=True, vertical="center")
            ws.column_dimensions[c.column_letter].width = (
                widths[i - 1] if widths else max(14, min(34, len(h) + 8)))
        ws.freeze_panes = "A5"

    def band(ws, row, ncols, value, fill, color="000000", size=11):
        for col in range(1, ncols + 1):
            ws.cell(row=row, column=col).fill = fill
        c = ws.cell(row=row, column=1, value=value)
        c.font = Font(bold=True, color=color, size=size)

    sheet = recipe_sheet(db, f, cid)

    # --- Sheet 1: chef recipe sheet (order → recipe → component → ingredient)
    ws = wb.active
    ws.title = "Recipe Sheet (Chef)"
    cols = ["Component", "Item Code", "Ingredient", "Kitchen Section", "Issued To",
            "Per Portion", "Recipe UOM", "Net Qty", "Issue Qty", "Trim Loss", "UOM",
            "Cutting / Portion", "Done ✓"]
    header(ws, cols, "BILL OF QUANTITY — RECIPE SHEET", stamp,
           widths=[26, 14, 36, 16, 16, 12, 11, 13, 13, 11, 9, 28, 9])
    r = 5
    n = len(cols)
    for o in sheet:
        band(ws, r, n, f"{o['order_no']}  |  {o['customer_name']}  |  {o['brand']}  |  "
                       f"Delivery {o['delivery_date'] or '—'}  |  Cooking {o['cooking_date'] or '—'}",
             order_fill, "FFFFFF", 12)
        r += 1
        for rec in o["recipes"]:
            band(ws, r, n, f"{rec['recipe_name']}  ({rec['recipe_no']})  —  "
                           f"{rec['portions']:g} portions  ·  {rec['category'] or ''}", recipe_fill)
            r += 1
            for comp in rec["components"]:
                band(ws, r, n, f"   {comp['name']}", comp_fill, "132947", 10)
                r += 1
                for ln in comp["lines"]:
                    vals = ["", ln["ingredient_code"], ln["ingredient_name"], ln["kitchen_section"],
                            ln["issue_section"], round(ln.get("net_per_portion") or ln["per_portion"], 4),
                            ln["recipe_uom"], round(ln["required_qty"], 3), round(ln["issue_qty"], 3),
                            round(ln["yield_loss"], 3), ln["uom"], ln["cutting"], ""]
                    for ci, v in enumerate(vals, start=1):
                        cell = ws.cell(row=r, column=ci, value=v)
                        cell.border = box
                    r += 1
            r += 1
        r += 1

    # --- Sheet 2: by kitchen section
    ws_s = wb.create_sheet("By Section")
    header(ws_s, ["Section", "Order", "Customer", "Recipe", "Portions", "Component",
                  "Item Code", "Ingredient", "Net Qty", "Issue Qty", "UOM", "Issued To"],
           "BILL OF QUANTITY — BY SECTION", stamp,
           widths=[16, 20, 26, 30, 10, 22, 14, 34, 13, 13, 9, 16])
    r = 5
    for s in by_section(sheet):
        band(ws_s, r, 12, f"{s['section']}  —  {len(s['recipes'])} recipe(s), {s['lines']} line(s)",
             order_fill, "FFFFFF")
        r += 1
        for rec in s["recipes"]:
            for ln in rec["lines"]:
                for ci, v in enumerate([s["section"], rec["order_no"], rec["customer_name"],
                                        f"{rec['recipe_name']} ({rec['recipe_no']})", rec["portions"],
                                        ln["component"], ln["ingredient_code"], ln["ingredient_name"],
                                        round(ln["required_qty"], 3), round(ln["issue_qty"], 3),
                                        ln["uom"], ln["issue_section"]], 1):
                    ws_s.cell(row=r, column=ci, value=v)
                r += 1

    # --- Sheet 3: by recipe (consolidated across orders, for batch cooking)
    ws_r = wb.create_sheet("By Recipe")
    header(ws_r, ["Recipe", "Total Portions", "Orders", "Component", "Item Code", "Ingredient",
                  "Kitchen Section", "Net Qty", "Issue Qty", "UOM"],
           "BILL OF QUANTITY — BY RECIPE (all filtered orders)", stamp,
           widths=[34, 13, 40, 22, 14, 34, 16, 13, 13, 9])
    r = 5
    for rec in by_recipe(sheet):
        band(ws_r, r, 10, f"{rec['recipe_name']} ({rec['recipe_no']})  —  {rec['portions']:g} portions",
             recipe_fill)
        r += 1
        orders_txt = ", ".join(f"{x['order_no']} ({x['portions']:g})" for x in rec["orders"])
        for ln in rec["lines"]:
            for ci, v in enumerate([rec["recipe_name"], rec["portions"], orders_txt, ln["component"],
                                    ln["ingredient_code"], ln["ingredient_name"], ln["kitchen_section"],
                                    round(ln["required_qty"], 3), round(ln["issue_qty"], 3), ln["uom"]], 1):
                ws_r.cell(row=r, column=ci, value=v)
            r += 1

    # --- Sheet 4: consolidated pick list (store)
    rows = consolidated(db, f, cid)
    ws_p = wb.create_sheet("Pick List (Consolidated)")
    header(ws_p, ["Section", "Storage", "Item Code", "Ingredient", "UOM",
                  "Total Required", "Orders", "Est. Value", "Picked ✓"],
           "CONSOLIDATED PICK LIST", stamp)
    r = 5
    for x in rows:
        qty = float(x["required_qty"] or 0)
        for ci, v in enumerate([x["section"] or "—", x["storage_type"] or "", x["ingredient_code"],
                                x["item_name"], x["uom"], round(qty, 3), int(x["order_count"] or 0),
                                round(qty * float(x["unit_cost"] or 0), 2)], 1):
            ws_p.cell(row=r, column=ci, value=v)
        r += 1
    if rows:
        t = ws_p.cell(row=r, column=4, value="TOTAL")
        t.font = Font(bold=True)
        t.fill = sub
        tv = ws_p.cell(row=r, column=8, value=round(sum(
            float(x["required_qty"] or 0) * float(x["unit_cost"] or 0) for x in rows), 2))
        tv.font = Font(bold=True)
        tv.fill = sub

    # --- Sheet 5: flat by-order detail (kept — easiest to filter in Excel)
    ow = order_wise(db, f, cid)
    ws2 = wb.create_sheet("By Order (flat)")
    header(ws2, ["Delivery", "Order", "Customer", "Brand", "Recipe Name", "Recipe Code",
                 "Sub-Recipe", "Main Cat.", "Sub Cat.", "Item Code", "Ingredient", "UOM",
                 "Net Qty", "Issue Qty"],
           "BILL OF QUANTITY — BY ORDER", stamp)
    r = 5
    for x in ow:
        for ci, v in enumerate([str(x["delivery_date"] or ""), x["order_no"], x["customer_name"],
                                x["brand"], x.get("recipe_name") or "", x["recipe_no"],
                                x.get("sub_recipe_description") or "", x.get("main_category") or "",
                                x.get("sub_category") or "", x["ingredient_code"], x["item_name"],
                                x["uom"], round(float(x["required_qty"] or 0), 3),
                                round(float(x.get("issue_qty") or 0), 3)], 1):
            ws2.cell(row=r, column=ci, value=v)
        r += 1

    # --- Sheet 6: by customer
    ws3 = wb.create_sheet("By Customer")
    header(ws3, ["Customer", "Orders", "Ingredients", "Total Qty"], "BILL OF QUANTITY — BY CUSTOMER", stamp)
    r = 5
    for e in _customers_summary(ow):
        for ci, v in enumerate([e["customer"], e["orders"], e["item_count"], e["qty"]], 1):
            ws3.cell(row=r, column=ci, value=v)
        r += 1

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition":
                 f'attachment; filename="ISFC_BOQ_{date.today().isoformat()}.xlsx"'},
    )


@router.get("/preview")
def preview_boq(request: Request, db: Session = Depends(get_db)):
    """Batch 106 — see the BOQ on screen before downloading it.

    Batch 200 — five views over the same filtered BOM:
      Recipe Sheet  order → recipe → component → ingredient (the chef's sheet)
      By Section    kitchen section → recipe → ingredient
      By Recipe     one recipe consolidated across orders (batch cooking)
      Pick List     one line per ingredient (the store)
      By Customer   summary
    """
    require_area(request, "bom")
    cid = _cid(request)
    f = _filters(request)

    sheet = recipe_sheet(db, f, cid)
    rows = consolidated(db, f, cid)
    ow = order_wise(db, f, cid)

    total_value = sum(float(x["required_qty"] or 0) * float(x["unit_cost"] or 0) for x in rows)
    by_sec_counts: dict[str, int] = {}
    for x in rows:
        by_sec_counts[x["section"] or "—"] = by_sec_counts.get(x["section"] or "—", 0) + 1

    # Batch 159-5 (Image 6): the Bill of Quantity is often produced BEFORE the
    # BOM is generated. Until it is, "Issue Qty" (the store hand-over weight) is
    # not meaningful — only the required quantity is. Hide the Issue-Qty column
    # unless every order in scope has reached BOM generation.
    _PRE_BOM = {"", "submitted", "awaiting planning", "awaiting head chef",
                "draft", "new", "pending"}
    show_issue = True
    _ons = [o.get("order_no") for o in sheet if o.get("order_no")]
    if _ons:
        try:
            from sqlalchemy import bindparam
            _stmt = text("SELECT COALESCE(status,'') FROM customer_orders "
                         "WHERE order_no IN :ons").bindparams(bindparam("ons", expanding=True))
            _st = [str(r[0] or "").strip().lower() for r in db.execute(_stmt, {"ons": _ons}).all()]
            if _st:
                show_issue = all(s not in _PRE_BOM for s in _st)
        except Exception:
            show_issue = True

    return render(request, "production/boq_preview.html", {
        "rows": rows,
        "sheet": sheet,
        "show_issue": show_issue,
        "section_view": by_section(sheet),
        "recipe_view": by_recipe(sheet),
        "main_component": MAIN_COMPONENT,
        "customers_summary": _customers_summary(ow),
        "pick": picker_options(db, cid),
        "filters": f,
        "totals": {
            "ingredients": len(rows),
            "orders": len(sheet),
            "recipes": sum(o["recipe_count"] for o in sheet),
            "portions": round(sum(o["portions"] for o in sheet), 2),
            "value": round(total_value, 2),
            "sections": by_sec_counts,
        },
        "page_title": "Bill of Quantity",
    })


@router.get("/recipe-sheet/print")
def print_recipe_sheet(request: Request, db: Session = Depends(get_db)):
    """Batch 200 — printable chef sheet, one order per page, with tick boxes.

    Standalone (no sidebar/topbar) so Ctrl+P / Save as PDF gives a clean sheet
    to put on the pass. `mode=section` prints the By Section layout instead.
    """
    require_area(request, "bom")
    cid = _cid(request)
    f = _filters(request)
    sheet = recipe_sheet(db, f, cid)
    mode = (request.query_params.get("mode") or "order").strip()
    # ------------------------------------------------------------------
    # Batch 236 (Images 1, 2) — the sheet prints what you are looking at.
    #
    # `hide`  the columns switched off with the Columns button on screen.
    #         The print link is rewritten as those boxes are ticked, so the
    #         paper matches the screen instead of always printing all nine
    #         columns. Empty on every existing link, which prints everything.
    #
    # `up`    ingredients per printed row. Your note: *print recipes on page
    #         like top show the recipes and two ingredient show in one row
    #         like side by side, it will save the page.* A 27-line recipe at
    #         up=2 is 14 rows instead of 27, so Chicken creamy mint stops
    #         running onto a second sheet. 1 (the old layout) and 2 only —
    #         three across leaves no room for the ingredient name at A4.
    # ------------------------------------------------------------------
    try:
        up = int(request.query_params.get("up") or 1)
    except (TypeError, ValueError):
        up = 1
    up = 2 if up >= 2 else 1
    return render(request, "production/boq_recipe_print.html", {
        "sheet": sheet,
        "section_view": by_section(sheet) if mode == "section" else [],
        "recipe_view": by_recipe(sheet) if mode == "recipe" else [],
        "mode": mode,
        "up": up,
        "hide": set(f.get("hide") or []),
        "filters": f,
        "stamp": _stamp(f),
        "page_title": "Recipe Sheet",
    })
