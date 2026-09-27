# app/services/customer_plans.py
# =============================================================================
# Batch 248 — CUSTOMER MEAL PLANS (Salus: Comfy / Low Carb / Grit / Salus Fit)
# -----------------------------------------------------------------------------
# A plan is a package the customer sells to ITS customers, with a different
# protein and carb weight per plate. What the Salus workbook actually says:
#
#   Menu sheet            which main dishes are offered in which plans
#   Recipe Ingredients    per-plan BATCH QUANTITIES on the plan-sensitive lines
#                         (Butter Chicken, chicken breast, per 10-portion batch:
#                          Fit 1300 · Low Carb 1800 · Comfy 1800 · Grit 2300)
#   Packages table        the headline protein / carb per plan (display only)
#
# So a plan changes what is cooked, issued and costed — not just which dishes
# appear. Three small tables hold it; nothing in the existing recipe tables
# changes shape:
#
#   customer_plans              the plans a customer offers (+ protein/carb)
#   recipe_plans                which recipe codes are offered in which plan
#   recipe_ingredient_plans     per-plan batch qty for a recipe_ingredients row
#   order_lines.plan_code       which plan an order line was ordered for
#
# Nothing is Salus-specific. The next plan-based customer is data: import their
# workbook (scripts/import_salus_master.py handles the same layout) or add the
# plans in Master → Customer Plans.
#
# `customer_key` is the value recipes.customer_name holds for that customer
# ("SALUS"), the same key the weekly-menu lookup already matches on, so plans
# are found by exactly the rule that finds the menu.
# =============================================================================
from __future__ import annotations

from typing import Iterable

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from app.core.db_read import log_failure as db_read_log

# Colour per plan for chips and matrix headers. Unknown plans cycle the rest.
PLAN_COLORS = ["#1e5bb8", "#0f766e", "#b45309", "#7c3aed", "#be123c", "#334155"]


def _f(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _has_column(db: Session, table: str, column: str) -> bool:
    return bool(db.execute(text("""
        SELECT COUNT(*) FROM information_schema.columns
        WHERE table_schema = DATABASE() AND table_name = :t AND column_name = :c
    """), {"t": table, "c": column}).scalar())


def _has_table(db: Session, table: str) -> bool:
    return bool(db.execute(text("""
        SELECT COUNT(*) FROM information_schema.tables
        WHERE table_schema = DATABASE() AND table_name = :t
    """), {"t": table}).scalar())


_SCHEMA_OK = False


def ensure_plan_schema(db: Session) -> None:
    """Create the plan tables / order_lines.plan_code if missing.

    information_schema guards — ADD COLUMN IF NOT EXISTS is not available on
    the target MySQL. Run at startup (main.py) and before every plan read, so
    a database that has not been restarted since this batch still works.
    Checks run once per process; after that this is a no-op.
    """
    global _SCHEMA_OK
    if _SCHEMA_OK:
        return
    try:
        if not _has_table(db, "customer_plans"):
            db.execute(text("""
                CREATE TABLE customer_plans (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    company_id INT NULL,
                    customer_key VARCHAR(120) NOT NULL,
                    plan_code VARCHAR(40) NOT NULL,
                    plan_name VARCHAR(120) NOT NULL,
                    protein_g DECIMAL(10,2) NULL,
                    carb_g DECIMAL(10,2) NULL,
                    description VARCHAR(255) NULL,
                    sort_order INT NOT NULL DEFAULT 0,
                    is_active TINYINT(1) NOT NULL DEFAULT 1,
                    created_at DATETIME NULL,
                    updated_at DATETIME NULL,
                    UNIQUE KEY uq_customer_plan (company_id, customer_key, plan_code)
                )"""))
        if not _has_table(db, "recipe_plans"):
            db.execute(text("""
                CREATE TABLE recipe_plans (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    company_id INT NULL,
                    recipe_code VARCHAR(50) NOT NULL,
                    plan_code VARCHAR(40) NOT NULL,
                    UNIQUE KEY uq_recipe_plan (company_id, recipe_code, plan_code),
                    KEY ix_recipe_plans_code (recipe_code)
                )"""))
        if not _has_table(db, "recipe_ingredient_plans"):
            db.execute(text("""
                CREATE TABLE recipe_ingredient_plans (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    recipe_ingredient_id INT NOT NULL,
                    plan_code VARCHAR(40) NOT NULL,
                    qty_batch DECIMAL(18,6) NOT NULL DEFAULT 0,
                    UNIQUE KEY uq_ri_plan (recipe_ingredient_id, plan_code)
                )"""))
        if not _has_column(db, "order_lines", "plan_code"):
            db.execute(text("ALTER TABLE order_lines ADD COLUMN plan_code VARCHAR(40) NULL"))
        db.commit()
        _SCHEMA_OK = True
    except Exception as exc:  # pragma: no cover - logged, never fatal
        db.rollback()
        db_read_log(exc, "ensure_plan_schema", "customer_plans.ensure_plan_schema")


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------
def plans_for_keys(db: Session, cid: int, keys: Iterable[str]) -> list[dict]:
    """Active plans for whichever customer key matches (case-insensitive)."""
    keys = [k.lower() for k in keys if k]
    if not keys:
        return []
    try:
        rows = db.execute(text("""
            SELECT plan_code, plan_name, protein_g, carb_g, COALESCE(description,'') AS description,
                   customer_key, sort_order
            FROM customer_plans
            WHERE LOWER(customer_key) IN :keys
              AND (company_id = :cid OR company_id IS NULL)
              AND is_active = 1
            ORDER BY sort_order, plan_name
        """).bindparams(bindparam("keys", expanding=True)),
            {"keys": keys, "cid": cid}).mappings().all()
    except Exception as exc:
        db_read_log(exc, "plans_for_keys", "customer_plans.plans_for_keys")
        return []
    out = []
    for i, r in enumerate(rows):
        out.append({
            "code": r["plan_code"], "name": r["plan_name"],
            "protein_g": _f(r["protein_g"]) or None, "carb_g": _f(r["carb_g"]) or None,
            "description": r["description"], "customer_key": r["customer_key"],
            "color": PLAN_COLORS[i % len(PLAN_COLORS)],
        })
    return out


def recipe_plan_map(db: Session, cid: int, codes: Iterable[str]) -> dict[str, list[str]]:
    """recipe_code -> [plan_code, ...] (empty/missing = not a plan dish)."""
    codes = sorted({c for c in codes if c})
    if not codes:
        return {}
    try:
        rows = db.execute(text("""
            SELECT recipe_code, plan_code FROM recipe_plans
            WHERE recipe_code IN :codes AND (company_id = :cid OR company_id IS NULL)
        """).bindparams(bindparam("codes", expanding=True)),
            {"codes": codes, "cid": cid}).all()
    except Exception as exc:
        db_read_log(exc, "recipe_plan_map", "customer_plans.recipe_plan_map")
        return {}
    out: dict[str, list[str]] = {}
    for code, plan in rows:
        out.setdefault(code, []).append(plan)
    return out


def plan_qty_map(db: Session, recipe_ingredient_ids: Iterable[int], plan_code: str) -> dict[int, float]:
    """recipe_ingredient_id -> per-plan batch qty, for ONE plan."""
    ids = sorted({int(i) for i in recipe_ingredient_ids if i})
    if not ids or not plan_code:
        return {}
    try:
        rows = db.execute(text("""
            SELECT recipe_ingredient_id, qty_batch FROM recipe_ingredient_plans
            WHERE recipe_ingredient_id IN :ids AND plan_code = :p
        """).bindparams(bindparam("ids", expanding=True)),
            {"ids": ids, "p": plan_code}).all()
    except Exception as exc:
        db_read_log(exc, "plan_qty_map", "customer_plans.plan_qty_map")
        return {}
    return {int(r[0]): _f(r[1]) for r in rows}


def plan_cost_adjustments(db: Session, cid: int, codes: Iterable[str]) -> dict[tuple, float]:
    """(recipe_code, plan_code) -> food cost per portion for that plan.

    Recipe food cost per portion is the sum of its lines' cost per portion.
    For a plan, the plan-sensitive lines are re-costed at the plan quantity:

        plan_fcpp = recipe_fcpp
                    - Σ base line cost/portion   (plan-sensitive lines)
                    + Σ plan qty/portion × cost/UOM

    Uses the latest active version of each recipe. Returns only pairs that
    actually have plan quantities; everything else costs as the base recipe.
    """
    codes = sorted({c for c in codes if c})
    if not codes:
        return {}
    try:
        rows = db.execute(text("""
            SELECT r.recipe_code, r.id AS recipe_id, r.version, rip.plan_code,
                   ri.id AS ri_id,
                   COALESCE(ri.qty_per_portion, 0) AS net_pp,
                   COALESCE(ri.qty_batch, 0) AS qty_batch,
                   COALESCE(NULLIF(ri.portions, 0), NULLIF(r.standard_portions, 0), 1) AS portions,
                   COALESCE(ri.cost_uom, 0) AS cost_uom,
                   COALESCE(ri.line_cost_per_portion, 0) AS base_lcpp,
                   rip.qty_batch AS plan_batch
            FROM recipes r
            JOIN recipe_ingredients ri ON ri.recipe_id = r.id
            JOIN recipe_ingredient_plans rip ON rip.recipe_ingredient_id = ri.id
            WHERE r.recipe_code IN :codes
              AND (r.company_id = :cid OR r.company_id IS NULL)
              AND UPPER(TRIM(COALESCE(r.status,''))) = 'ACTIVE'
              AND COALESCE(r.is_active, 1) = 1
            ORDER BY r.recipe_code, r.version DESC, r.id DESC
        """).bindparams(bindparam("codes", expanding=True)),
            {"codes": codes, "cid": cid}).mappings().all()
    except Exception as exc:
        db_read_log(exc, "plan_cost_adjustments", "customer_plans.plan_cost_adjustments")
        return {}
    latest: dict[str, int] = {}
    delta: dict[tuple, float] = {}
    for r in rows:
        code = r["recipe_code"]
        latest.setdefault(code, r["recipe_id"])
        if r["recipe_id"] != latest[code]:
            continue  # older version
        base = _f(r["base_lcpp"]) or _f(r["net_pp"]) * _f(r["cost_uom"])
        plan = _f(r["plan_batch"]) / (_f(r["portions"]) or 1) * _f(r["cost_uom"])
        key = (code, r["plan_code"])
        delta[key] = delta.get(key, 0.0) + (plan - base)
    return delta


def plan_label(plans: list[dict], code: str) -> str:
    for p in plans:
        if p["code"] == code:
            return p["name"]
    return code
