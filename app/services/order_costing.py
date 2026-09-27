# app/services/order_costing.py
# =============================================================================
# Batch 247 — ONE place that answers "what does this order cost?"
# -----------------------------------------------------------------------------
# Before this file the same question was answered four different ways:
#
#   * Sale Requisition form   carried food_cost_per_portion in a datalist and
#                             never showed it
#   * Sales Request approval  printed order.total_sales_value — an attribute
#                             that does not exist on CustomerOrder (the column
#                             is total_estimated_selling_value), so Jinja
#                             rendered it as 0.00 on every request
#   * Relationship Map        showed master-data row counts instead of cost
#   * Order creation          multiplied recipes.food_cost_per_portion, which
#                             is 0 on recipes that were imported without a
#                             recalc, even though recipes.food_cost is filled
#
# Every screen now reads the per-portion figures through FCPP_SQL / SPPP_SQL
# below, which fall back to batch cost ÷ standard portions when the stored
# per-portion value is empty. Same numbers, same rounding, every screen.
# =============================================================================
from __future__ import annotations

from typing import Any, Iterable

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from app.core.db_read import log_failure as db_read_log


def FCPP_SQL(a: str = "r") -> str:
    """Food cost per portion, with the batch-cost fallback."""
    return (f"COALESCE(NULLIF({a}.food_cost_per_portion, 0), "
            f"{a}.food_cost / NULLIF({a}.standard_portions, 0), 0)")


def SPPP_SQL(a: str = "r") -> str:
    """Sale price per portion, with the batch-price fallback."""
    return (f"COALESCE(NULLIF({a}.sale_price_per_portion, 0), "
            f"{a}.sale_price / NULLIF({a}.standard_portions, 0), 0)")


def _f(v: Any) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _pct(part: float, whole: float) -> float:
    return round(part / whole * 100, 2) if whole else 0.0


# ---------------------------------------------------------------------------
# Recipe prices
# ---------------------------------------------------------------------------
def recipe_costs(db: Session, cid: int, codes: Iterable[str]) -> dict[str, dict]:
    """recipe_code -> {name, category, fcpp, sppp} for the latest ACTIVE version."""
    codes = sorted({(c or "").strip() for c in codes if (c or "").strip()})
    if not codes:
        return {}
    try:
        rows = db.execute(text(f"""
            SELECT r.recipe_code, r.recipe_name, COALESCE(r.category,'') AS category,
                   {FCPP_SQL('r')} AS fcpp, {SPPP_SQL('r')} AS sppp
            FROM recipes r
            WHERE r.recipe_code IN :codes
              AND (r.company_id = :cid OR r.company_id IS NULL)
              AND UPPER(TRIM(COALESCE(r.status,''))) = 'ACTIVE'
              AND COALESCE(r.is_active, 1) = 1
            ORDER BY r.recipe_code, r.version DESC, r.id DESC
        """).bindparams(bindparam("codes", expanding=True)),
            {"codes": codes, "cid": cid}).mappings().all()
    except Exception as exc:  # pragma: no cover - logged, never fatal
        db_read_log(exc, "recipe_costs", "order_costing.recipe_costs")
        return {}
    out: dict[str, dict] = {}
    for r in rows:
        if r["recipe_code"] in out:
            continue  # older version of a recipe already taken
        out[r["recipe_code"]] = {
            "name": r["recipe_name"], "category": r["category"],
            "fcpp": round(_f(r["fcpp"]), 4), "sppp": round(_f(r["sppp"]), 4),
        }
    return out


def _totals(lines: list[dict]) -> dict:
    portions = sum(l["portions"] for l in lines)
    food = sum(l["food_cost"] for l in lines)
    sale = sum(l["sale_value"] for l in lines)
    return {
        "lines": len(lines),
        "portions": round(portions, 2),
        "food_cost": round(food, 2),
        "sale_value": round(sale, 2),
        "margin": round(sale - food, 2),
        "margin_pct": _pct(sale - food, sale),
        "food_cost_pct": _pct(food, sale),
        "cost_per_portion": round(food / portions, 4) if portions else 0.0,
    }


def plan_fcpp(prices: dict, deltas: dict, code: str, plan: str) -> float:
    """Batch 248: food cost per portion for a recipe in a meal plan.

    Base recipe cost, re-costed on the plan-sensitive lines at the plan
    quantity (services/customer_plans.plan_cost_adjustments). No plan, or a
    recipe with no plan quantities, costs exactly as before.
    """
    base = _f((prices.get(code) or {}).get("fcpp"))
    if not plan:
        return base
    return max(0.0, base + deltas.get((code, plan), 0.0))


def _plan_deltas(db: Session, cid: int, codes) -> dict:
    try:
        from app.services.customer_plans import plan_cost_adjustments
        return plan_cost_adjustments(db, cid, codes)
    except Exception as exc:  # pragma: no cover
        db_read_log(exc, "plan deltas", "order_costing._plan_deltas")
        return {}


def quote(db: Session, cid: int, items: list[dict]) -> dict:
    """Cost a DRAFT order — the Sale Requisition "Check Order Cost" button.

    items: [{"code": "RCP-SMC-000162", "qty": 12, "plan": "COMFY"}, ...].
    `plan` is optional (Batch 248). Lines with qty <= 0 are ignored, exactly
    like order creation ignores them.
    """
    wanted = [(str(i.get("code") or "").strip(), _f(i.get("qty")),
               str(i.get("plan") or "").strip()) for i in items or []]
    wanted = [(c, q, p) for c, q, p in wanted if c and q > 0]
    prices = recipe_costs(db, cid, [c for c, _, _ in wanted])
    deltas = _plan_deltas(db, cid, [c for c, _, p in wanted if p])
    lines = []
    for code, qty, plan in wanted:
        p = prices.get(code) or {}
        fcpp, sppp = plan_fcpp(prices, deltas, code, plan), _f(p.get("sppp"))
        lines.append({
            "code": code, "name": p.get("name") or code, "category": p.get("category") or "",
            "plan": plan,
            "portions": qty, "fcpp": fcpp, "sppp": sppp,
            "food_cost": round(fcpp * qty, 4), "sale_value": round(sppp * qty, 4),
            "margin": round((sppp - fcpp) * qty, 4),
            "food_cost_pct": _pct(fcpp, sppp),
            "missing": not p,
        })
    return {"lines": lines, "totals": _totals(lines)}


# ---------------------------------------------------------------------------
# Saved orders
# ---------------------------------------------------------------------------
def order_line_costing(db: Session, cid: int, order_no: str) -> dict:
    """Per-line food cost and sale value for a SAVED order.

    Basis per line:
      BOM     once the BOM exists, the line's cost is the sum of its BOM lines'
              estimated_cost (what the store will actually issue, wastage in)
      Recipe  before that, recipe food cost per portion × portions
    Sale price is the price frozen on the order line at creation, falling back
    to the recipe's current price for lines saved without one.
    """
    try:
        from app.services.customer_plans import ensure_plan_schema
        ensure_plan_schema(db)
        ol = db.execute(text("""
            SELECT id, line_no, recipe_no, recipe_name,
                   COALESCE(plan_code, '') AS plan_code,
                   COALESCE(required_portions, 0) AS portions,
                   COALESCE(selling_price_per_portion, 0) AS sppp
            FROM order_lines
            WHERE order_no = :o AND (company_id = :cid OR company_id IS NULL)
            ORDER BY line_no, id
        """), {"o": order_no, "cid": cid}).mappings().all()
    except Exception as exc:  # pragma: no cover
        db_read_log(exc, "order_lines", "order_costing.order_line_costing")
        ol = []
    prices = recipe_costs(db, cid, [r["recipe_no"] for r in ol])
    deltas = _plan_deltas(db, cid, [r["recipe_no"] for r in ol if r["plan_code"]])

    bom_cost: dict[int, float] = {}
    try:
        for r in db.execute(text("""
            SELECT order_line_id, SUM(COALESCE(estimated_cost, 0)) AS cost
            FROM bom_lines WHERE order_no = :o AND order_line_id IS NOT NULL
            GROUP BY order_line_id
        """), {"o": order_no}).mappings().all():
            bom_cost[int(r["order_line_id"])] = _f(r["cost"])
    except Exception as exc:  # pragma: no cover
        db_read_log(exc, "bom_lines", "order_costing.order_line_costing")

    lines = []
    for r in ol:
        p = prices.get(r["recipe_no"]) or {}
        qty = _f(r["portions"])
        sppp = _f(r["sppp"]) or _f(p.get("sppp"))
        recipe_fc = plan_fcpp(prices, deltas, r["recipe_no"], r["plan_code"]) * qty
        if int(r["id"]) in bom_cost and qty > 0:
            food, basis = bom_cost[int(r["id"])], "BOM"
        else:
            food, basis = recipe_fc, "Recipe"
        fcpp = food / qty if qty else _f(p.get("fcpp"))
        lines.append({
            "id": r["id"], "line_no": r["line_no"], "code": r["recipe_no"],
            "plan": r["plan_code"],
            "name": r["recipe_name"] or p.get("name") or r["recipe_no"],
            "category": p.get("category") or "", "portions": qty,
            "fcpp": round(fcpp, 4), "sppp": round(sppp, 4),
            "food_cost": round(food, 4), "sale_value": round(sppp * qty, 4),
            "margin": round(sppp * qty - food, 4),
            "food_cost_pct": _pct(food, sppp * qty), "basis": basis,
            "missing": not p,
        })
    return {"lines": lines, "totals": _totals(lines)}


def refresh_order_totals(db: Session, cid: int, order) -> None:
    """Re-write the order header's cost/sale/margin totals from its lines.

    Used after the reviewer edits portions on a pending request — before this
    the header kept the ORIGINAL totals, so the approval screen, the order
    register and the map all disagreed with the corrected quantities.
    """
    t = order_line_costing(db, cid, order.order_no)["totals"]
    order.total_planned_portions = t["portions"]
    order.total_estimated_food_cost = t["food_cost"]
    order.total_estimated_selling_value = t["sale_value"]
    order.total_estimated_margin = t["margin"]


def order_intelligence(db: Session, cid: int, order_no: str) -> dict:
    """Everything the Relationship Map shows for one order: cost, waste, yield."""
    costing = order_line_costing(db, cid, order_no)

    def _rows(sql: str) -> list[dict]:
        try:
            return [dict(r) for r in db.execute(text(sql), {"o": order_no}).mappings().all()]
        except Exception as exc:  # pragma: no cover
            db_read_log(exc, sql, "order_costing.order_intelligence")
            return []

    # Planned material + planned waste, from the BOM.
    bom = _rows("""
        SELECT b.ingredient_code AS code,
               MAX(COALESCE(b.ingredient_name, b.ingredient_code)) AS name,
               MAX(COALESCE(b.standard_uom, '')) AS uom,
               SUM(COALESCE(b.total_required_with_waste_standard, b.required_qty_standard, 0)) AS qty,
               SUM(COALESCE(b.expected_waste_qty_standard, 0)) AS waste_qty,
               SUM(COALESCE(b.expected_waste_qty_standard, 0) * COALESCE(b.unit_cost_standard, 0)) AS waste_value,
               SUM(COALESCE(b.estimated_cost, 0)) AS cost
        FROM bom_lines b WHERE b.order_no = :o
        GROUP BY b.ingredient_code
        ORDER BY cost DESC
    """)
    bom_cost = sum(_f(r["cost"]) for r in bom)
    planned_waste_value = sum(_f(r["waste_value"]) for r in bom)
    top = []
    for r in bom[:10]:
        top.append({**r, "qty": round(_f(r["qty"]), 3), "waste_qty": round(_f(r["waste_qty"]), 3),
                    "cost": round(_f(r["cost"]), 2), "share": _pct(_f(r["cost"]), bom_cost)})

    # Actual yield + waste, from the kitchen section movements.
    sections = _rows("""
        SELECT COALESCE(NULLIF(k.current_section, ''), '—') AS section,
               SUM(COALESCE(k.issued_qty_standard, 0))    AS input_qty,
               SUM(COALESCE(k.processed_qty_standard, 0)) AS output_qty,
               SUM(COALESCE(k.waste_qty_standard, 0))     AS waste_qty,
               SUM(COALESCE(k.returned_qty_standard, 0))  AS return_qty,
               SUM(COALESCE(k.waste_qty_standard, 0) * COALESCE(b.unit_cost_standard, 0)) AS waste_value,
               COUNT(*) AS moves
        FROM kitchen_section_transactions k
        LEFT JOIN bom_lines b ON b.id = k.bom_line_id
        WHERE k.order_no = :o
        GROUP BY COALESCE(NULLIF(k.current_section, ''), '—')
        ORDER BY section
    """)
    for s in sections:
        for k in ("input_qty", "output_qty", "waste_qty", "return_qty", "waste_value"):
            s[k] = round(_f(s[k]), 3)
        s["yield_pct"] = _pct(s["output_qty"], s["input_qty"])
        s["waste_pct"] = _pct(s["waste_qty"], s["input_qty"])
    k_in = sum(s["input_qty"] for s in sections)
    k_out = sum(s["output_qty"] for s in sections)
    k_waste = sum(s["waste_qty"] for s in sections)
    actual_waste_value = sum(s["waste_value"] for s in sections)

    t = costing["totals"]
    return {
        "lines": costing["lines"],
        "totals": t,
        "bom_cost": round(bom_cost, 2),
        "top_items": top,
        "sections": sections,
        "waste": {
            "planned_value": round(planned_waste_value, 2),
            "planned_pct": _pct(planned_waste_value, bom_cost),
            "actual_qty": round(k_waste, 3),
            "actual_value": round(actual_waste_value, 2),
            "actual_pct": _pct(k_waste, k_in),
        },
        "yield": {
            "input": round(k_in, 3), "output": round(k_out, 3),
            "pct": _pct(k_out, k_in), "has_data": bool(sections),
        },
    }
