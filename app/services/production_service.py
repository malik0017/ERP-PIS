from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, datetime
from typing import Any

from sqlalchemy.orm import Session
from sqlalchemy import func, text

from app.models.ingredient import Ingredient
from app.models.inventory import InventoryTransaction, StockLot
from app.models.production import (
    BOMLine,
    CustomerOrder,
    HeadChefPlan,
    KitchenSectionTransaction,
    OrderLine,
    StoreIssuanceLine,
)
from app.models.recipe import Recipe, RecipeIngredient
from app.schemas.production import CustomerOrderCreate
from app.core.notifications import notify_role
from app.core.production_constants import (
    resolve_issue_section,
    DEFAULT_ISSUE_SECTION,
)


# =============================================================================
# Batch 142 — BOM QUANTITY POLICY
#
# Two switches, deliberately module-level constants rather than settings rows,
# because changing either one changes what the store physically hands over and
# that should be a reviewed code change, not a checkbox someone toggles.
#
#   BOM_APPLY_WASTAGE = False
#       The BOM quantity is exactly  qty_per_portion x ordered_portions.
#       No 5% uplift, so the printed number reconciles against the recipe by
#       hand. recipes.target_wastage_pct still exists for planning; it is just
#       no longer folded into the issue quantity.
#
#   BOM_QTY_BASIS = "per_portion"
#       Reads recipe_ingredients.qty_per_portion (the workbook's NET, i.e.
#       post-trim, weight). "gross" instead divides gross-qty-per-batch by
#       portions-per-batch, which is the weight the store must actually issue
#       once yield loss is accounted for. See the note at the call site.
# =============================================================================
BOM_APPLY_WASTAGE = False
BOM_QTY_BASIS = "per_portion"


def _num(value: Any) -> float:
    try:
        if value is None or value == "":
            return 0.0
        return float(value)
    except Exception:
        return 0.0


def _now() -> datetime:
    return datetime.utcnow()


def _json_list(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        data = json.loads(value)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _dump_route(route: list[str]) -> str:
    return json.dumps(route, ensure_ascii=False)


def _normalize_section(name: str) -> str:
    """Batch 19: retired sections collapse to their successor; 'Packing' and
    'Trayline / Packing' are ONE stage (standardized on 'Trayline / Packing')."""
    n = (name or "").strip()
    if n == "Packing":
        return "Trayline / Packing"
    # Batch 20: both activities live inside Butchery now.
    if n in ("Thawing", "Marination"):
        return "Butchery"
    return n


def generate_order_no(db: Session) -> str:
    today = date.today().strftime("%Y%m%d")
    count = db.query(CustomerOrder).filter(CustomerOrder.order_no.like(f"ORD-{today}-%")).count() + 1
    return f"ORD-{today}-{count:04d}"


def convert_to_standard(qty: float, from_uom: str, standard_uom: str, conversion_to_standard: float | None = None) -> float:
    """Convert any recipe/purchase issue UOM into the ingredient standard UOM.

    Mass: g -> Kg, Kg -> Kg
    Volume: mL -> L, L -> L
    Count: Each -> Each
    Pack/Box/Tray: qty * ingredient conversion_to_standard
    """
    qty = _num(qty)
    from_uom = (from_uom or "").strip()
    standard_uom = (standard_uom or "").strip()
    conv = _num(conversion_to_standard) or 1.0

    if from_uom == standard_uom:
        return qty
    if from_uom == "g" and standard_uom == "Kg":
        return qty / 1000
    if from_uom == "Kg" and standard_uom == "Kg":
        return qty
    if from_uom == "mL" and standard_uom == "L":
        return qty / 1000
    if from_uom == "L" and standard_uom == "L":
        return qty
    if from_uom == "Each" and standard_uom == "Each":
        return qty
    if from_uom in {"Pack", "Box", "Tray"}:
        return qty * conv
    # Last fallback for configured conversion.
    return qty * conv


def default_route_for_ingredient(ingredient: Ingredient | None, ingredient_name: str = "") -> list[str]:
    name = (ingredient_name or "").lower()
    if ingredient:
        if ingredient.is_bakery_item or ingredient.default_issue_section == "Bakery/Pastry":
            return ["Store", "Bakery/Pastry", "Trayline / Packing"]
        # Batch 20: thawing/marination flags now route through Butchery,
        # which owns thawing + marination internally.
        if ingredient.requires_thawing or ingredient.requires_marination:
            return ["Store", "Butchery", "Hot Kitchen", "QC", "Trayline / Packing"]
        if ingredient.requires_butchery:
            return ["Store", "Butchery", "Hot Kitchen", "QC", "Trayline / Packing"]
        if ingredient.requires_cutting and ingredient.is_cold_kitchen_item:
            return ["Store", "Cutting", "Cold Kitchen", "QC", "Trayline / Packing"]
        if ingredient.requires_cutting:
            return ["Store", "Cutting", "Hot Kitchen", "QC", "Trayline / Packing"]
        if ingredient.is_cold_kitchen_item:
            return ["Store", "Cold Kitchen", "QC", "Trayline / Packing"]

    if any(x in name for x in ["flour", "sugar", "butter", "yeast", "chocolate", "cream", "milk"]):
        return ["Store", "Bakery/Pastry", "Trayline / Packing"]
    if any(x in name for x in ["chicken", "beef", "meat", "mutton", "fish"]):
        return ["Store", "Butchery", "Hot Kitchen", "QC", "Trayline / Packing"]
    if any(x in name for x in ["onion", "tomato", "lettuce", "cucumber", "vegetable", "salad"]):
        return ["Store", "Cutting", "Hot Kitchen", "QC", "Trayline / Packing"]
    return ["Store", "Hot Kitchen", "QC", "Trayline / Packing"]


def create_order(db: Session, payload: CustomerOrderCreate, created_by: str = "system", company_id: int | None = None) -> CustomerOrder:
    """Create customer/internal production order using the active recipe master.

    This version matches app.models.recipe.Recipe fields:
    recipe_code, recipe_name, standard_portions, weight_per_portion_g,
    sale_price_per_portion, food_cost_per_portion.
    """
    order_no = payload.order_no or generate_order_no(db)
    if db.query(CustomerOrder).filter(CustomerOrder.order_no == order_no).first():
        raise ValueError(f"Order number already exists: {order_no}")

    order = CustomerOrder(
        company_id=company_id,
        order_no=order_no,
        order_date=payload.order_date or date.today(),
        required_delivery_date=payload.required_delivery_date,
        required_delivery_time=getattr(payload, "required_delivery_time", None),
        cooking_date=getattr(payload, "cooking_date", None),
        cooking_time=getattr(payload, "cooking_time", None),
        material_receiving_date=getattr(payload, "material_receiving_date", None),
        material_receiving_time=getattr(payload, "material_receiving_time", None),
        customer_no=payload.customer_no,
        customer_name=payload.customer_name,
        brand=payload.brand,
        channel=payload.channel,
        kitchen=payload.kitchen,
        order_type=payload.order_type,
        priority=payload.priority,
        created_by=created_by,
        status="Submitted",
        notes=payload.notes,
    )
    db.add(order)
    db.flush()

    total_portions = 0.0
    total_sales = 0.0
    total_food = 0.0

    for idx, line in enumerate(payload.lines, start=1):
        required_portions = _num(line.required_portions)
        if required_portions <= 0:
            continue

        recipe = (
            db.query(Recipe)
            .filter(
                Recipe.recipe_code == line.recipe_no,
                func.upper(func.trim(Recipe.status)) == "ACTIVE",
                Recipe.is_active == True,
            )
            .order_by(Recipe.version.desc(), Recipe.id.desc())
            .first()
        )

        recipe_name = line.recipe_name or (recipe.recipe_name if recipe else line.recipe_no)
        selling_price = line.selling_price_per_portion or _num(recipe.sale_price_per_portion if recipe else 0)
        food_cost_per_portion = _num(recipe.food_cost_per_portion if recipe else 0)
        standard_portions = max(_num(recipe.standard_portions if recipe else 1), 1)
        planned_batches = required_portions / standard_portions

        ol = OrderLine(
            company_id=company_id,
            order_no=order_no,
            line_no=idx,
            recipe_no=line.recipe_no,
            recipe_name=recipe_name,
            required_portions=required_portions,
            planned_batches=planned_batches,
            portion_size_g=_num(recipe.weight_per_portion_g if recipe else 0),
            selling_price_per_portion=selling_price,
            customer_notes=line.customer_notes,
            status="Open",
        )
        db.add(ol)
        total_portions += required_portions
        total_sales += required_portions * selling_price
        total_food += required_portions * food_cost_per_portion

    if total_portions <= 0:
        raise ValueError("Please enter at least one recipe with portions greater than zero.")

    order.total_planned_portions = total_portions
    order.total_estimated_selling_value = total_sales
    order.total_estimated_food_cost = total_food
    order.total_estimated_margin = total_sales - total_food
    db.commit()
    db.refresh(order)

    # Batch 78: real notification — every order creation path (manual entry,
    # customer portal, subscriptions) funnels through this one function, so
    # hooking it here covers all of them at once. Best-effort: never blocks
    # or fails the order itself if the notification write has a problem.
    notify_role(
        db, company_id=company_id, role="HEAD_CHEF",
        title=f"New order {order_no} submitted",
        message=f"{payload.customer_name} · {total_portions:.0f} portions — awaiting Head Chef approval.",
        url=f"/production/orders/{order_no}",
        category="order_submitted",
    )
    return order


def generate_bom_for_order(db: Session, order_no: str, approved_by: str | None = None) -> list[BOMLine]:
    """Generate BOM from active recipe_ingredients lines.

    This version matches app.models.recipe.RecipeIngredient fields:
    recipe_id, inventory_code, item_name, uom, qty_batch, qty_per_portion,
    cost_uom, line_cost.
    """
    order = db.query(CustomerOrder).filter(CustomerOrder.order_no == order_no).first()
    if not order:
        raise ValueError("Order not found")

    existing = db.query(BOMLine).filter(BOMLine.order_no == order_no).all()
    if existing:
        return existing

    lines = db.query(OrderLine).filter(OrderLine.order_no == order_no).order_by(OrderLine.line_no).all()
    created: list[BOMLine] = []
    total_cost = 0.0

    for ol in lines:
        recipe = (
            db.query(Recipe)
            .filter(
                Recipe.recipe_code == ol.recipe_no,
                func.upper(func.trim(Recipe.status)) == "ACTIVE",
                Recipe.is_active == True,
            )
            .order_by(Recipe.version.desc(), Recipe.id.desc())
            .first()
        )
        if not recipe:
            continue

        std_portions = max(_num(recipe.standard_portions), 1)
        order_portions = _num(ol.required_portions)
        multiplier = order_portions / std_portions if std_portions else 0

        recipe_items = (
            db.query(RecipeIngredient)
            .filter(RecipeIngredient.recipe_id == recipe.id)
            .order_by(RecipeIngredient.line_no)
            .all()
        )

        for ri in recipe_items:
            ingredient_code = ri.inventory_code or f"NO-CODE-{ri.id}"
            ingredient = db.query(Ingredient).filter(Ingredient.ingredient_code == ingredient_code).first()

            recipe_uom = ri.uom or (ingredient.recipe_uom if ingredient else "Kg")
            standard_uom = ingredient.standard_uom if ingredient else recipe_uom
            conv = ingredient.conversion_to_standard if ingredient else 1
            unit_cost = _num(ri.cost_uom) or _num(ingredient.unit_cost_standard if ingredient else 0)

            # -----------------------------------------------------------------
            # Batch 142 — WHICH COLUMN, and Batch 142b — NO WASTAGE UPLIFT.
            #
            # BOM_QTY_BASIS controls the source column:
            #   "per_portion"  (default) recipe_ingredients.qty_per_portion,
            #                  i.e. the workbook's "Qty req per portion".
            #   "gross"        gross qty per batch / portions per batch.
            #
            # Default is "per_portion" because that is the figure you asked the
            # BOM to show. Keep in mind what it means: that column is the NET
            # weight, after peeling and trimming. Beetroot is NET 160 g per
            # 10-portion batch at 83.67% yield, so the gross weight the store
            # has to hand over is 19.12 g/portion, not 16. On the "per_portion"
            # basis the BOM under-issues fresh produce by the yield loss on
            # every order. Dry goods are identical either way (100% yield).
            # Flip the constant to "gross" when you want issuance quantities
            # instead of plate quantities — nothing else needs to change.
            # -----------------------------------------------------------------
            line_portions = _num(getattr(ri, "portions", 0)) or std_portions
            qty_per_portion = 0.0
            if BOM_QTY_BASIS == "gross" and _num(ri.qty_batch) > 0 and line_portions > 0:
                qty_per_portion = _num(ri.qty_batch) / line_portions
            if qty_per_portion <= 0:
                qty_per_portion = _num(ri.qty_per_portion)
            if qty_per_portion <= 0 and _num(ri.qty_batch) > 0:
                qty_per_portion = _num(ri.qty_batch) / (line_portions or std_portions)

            required_recipe_qty = qty_per_portion * order_portions
            required_std = convert_to_standard(required_recipe_qty, recipe_uom, standard_uom, conv)

            # Batch 142b — the BOM quantity is now EXACTLY
            #     qty_per_portion x ordered_portions
            # with no wastage multiplier. The 5% uplift from
            # recipes.target_wastage_pct was silently inflating every line
            # (16 x 8 = 128 was being stored and printed as 134.4), so the
            # number on the BOM could not be reconciled against the recipe by
            # hand — which is the whole reason the column is there.
            #
            # target_wastage_pct is NOT deleted; it is still on the recipe and
            # still available for planning. It is simply no longer folded into
            # the quantity the store issues against. Set BOM_APPLY_WASTAGE to
            # True to restore the old behaviour.
            waste_pct = _num(recipe.target_wastage_pct) * 100 if _num(recipe.target_wastage_pct) < 1 else _num(recipe.target_wastage_pct)
            if BOM_APPLY_WASTAGE:
                expected_waste = required_std * waste_pct / 100
            else:
                waste_pct = 0.0
                expected_waste = 0.0
            required_with_waste = required_std + expected_waste
            cost = required_with_waste * unit_cost
            route = default_route_for_ingredient(ingredient, ri.item_name)
            # Batch 131 — store-issuance destination. PRD1-* fresh produce goes
            # to Cutting; every other line follows the kitchen section written
            # on the recipe ingredient (mapped from the workbook's "Section"
            # column). The route's step[1] is only the last-resort fallback.
            route_first = route[1] if len(route) > 1 else DEFAULT_ISSUE_SECTION
            first_section = resolve_issue_section(
                ingredient_code,
                getattr(ri, "kitchen_section", None),
                fallback=route_first,
            )

            bom = BOMLine(
                company_id=getattr(order, "company_id", None),
                order_no=order_no,
                order_line_id=ol.id,
                recipe_no=recipe.recipe_code,
                recipe_name=recipe.recipe_name,
                ingredient_code=ingredient_code,
                ingredient_name=ri.item_name,
                ingredient_category=(getattr(ingredient, "category", None) if ingredient else ri.line_type),
                ingredient_main_category=(getattr(ingredient, "main_category", None) or getattr(ingredient, "category", None) if ingredient else ri.line_type),
                ingredient_sub_category=(getattr(ingredient, "sub_category", None) if ingredient else None),
                original_recipe_qty=_num(ri.qty_batch),
                recipe_uom=recipe_uom,
                required_qty_recipe_uom=required_recipe_qty,
                standard_uom=standard_uom,
                required_qty_standard=required_std,
                wastage_pct=waste_pct,
                expected_waste_qty_standard=expected_waste,
                total_required_with_waste_standard=required_with_waste,
                unit_cost_standard=unit_cost,
                estimated_cost=cost,
                default_issue_section=first_section,
                route_template=_dump_route(route),
                bom_status="Generated",
            )
            db.add(bom)
            created.append(bom)
            total_cost += cost

    order.status = "BOM Generated"
    order.total_estimated_food_cost = total_cost
    order.total_estimated_margin = _num(order.total_estimated_selling_value) - total_cost
    db.commit()
    return db.query(BOMLine).filter(BOMLine.order_no == order_no).all()


def preview_bom_shortages(db: Session, order_no: str) -> list[dict[str, Any]]:
    """Batch 80 — read-only BOM preview for the Head Chef approval screen.

    Runs the exact same recipe -> ingredient explosion as
    generate_bom_for_order() (portions-adjusted quantity + waste %), but
    never writes a BOMLine row — this is meant to be safe to call on every
    page load of the order detail screen, before the Head Chef has approved
    anything. For each resulting ingredient, compares the required quantity
    against current stock on hand (computed the same way Inventory does:
    SUM(qty_in) - SUM(qty_out) from the ledger, company-scoped) and returns
    only the ones that would come up short.

    This is advisory, not a hard block — the Head Chef sees exactly which
    ingredients and by how much, and still decides whether to proceed
    (e.g. a purchase is already in transit), same as they would today,
    just no longer flying blind until Store discovers the shortage later.
    """
    order = db.query(CustomerOrder).filter(CustomerOrder.order_no == order_no).first()
    if not order:
        return []

    cid = getattr(order, "company_id", None)
    lines = db.query(OrderLine).filter(OrderLine.order_no == order_no).order_by(OrderLine.line_no).all()
    required: dict[str, dict[str, Any]] = {}

    for ol in lines:
        recipe = (
            db.query(Recipe)
            .filter(Recipe.recipe_code == ol.recipe_no,
                    func.upper(func.trim(Recipe.status)) == "ACTIVE",
                    Recipe.is_active == True)
            .order_by(Recipe.version.desc(), Recipe.id.desc())
            .first()
        )
        if not recipe:
            continue
        std_portions = max(_num(recipe.standard_portions), 1)
        order_portions = _num(ol.required_portions)
        waste_pct_raw = _num(recipe.target_wastage_pct)
        waste_pct = waste_pct_raw * 100 if waste_pct_raw < 1 else waste_pct_raw

        recipe_items = (
            db.query(RecipeIngredient)
            .filter(RecipeIngredient.recipe_id == recipe.id)
            .order_by(RecipeIngredient.line_no)
            .all()
        )
        for ri in recipe_items:
            ingredient_code = ri.inventory_code or f"NO-CODE-{ri.id}"
            ingredient = db.query(Ingredient).filter(Ingredient.ingredient_code == ingredient_code).first()
            recipe_uom = ri.uom or (ingredient.recipe_uom if ingredient else "Kg")
            standard_uom = ingredient.standard_uom if ingredient else recipe_uom
            conv = ingredient.conversion_to_standard if ingredient else 1

            qty_per_portion = _num(ri.qty_per_portion)
            if qty_per_portion <= 0 and _num(ri.qty_batch) > 0:
                qty_per_portion = _num(ri.qty_batch) / std_portions
            required_recipe_qty = qty_per_portion * order_portions
            required_std = convert_to_standard(required_recipe_qty, recipe_uom, standard_uom, conv)
            required_std += required_std * waste_pct / 100

            key = ingredient_code
            if key not in required:
                required[key] = {
                    "ingredient_code": ingredient_code, "ingredient_name": ri.item_name,
                    "standard_uom": standard_uom, "required_qty": 0.0, "recipes": set(),
                }
            required[key]["required_qty"] += required_std
            required[key]["recipes"].add(recipe.recipe_name)

    if not required:
        return []

    codes = list(required.keys())
    placeholders = ",".join(f":c{i}" for i in range(len(codes)))
    params = {f"c{i}": c for i, c in enumerate(codes)}
    params["cid"] = cid
    stock_rows = db.execute(text(f"""
        SELECT inventory_code,
               COALESCE(SUM(
                 CASE WHEN qc_status IN ('Pending','Failed') THEN 0 ELSE COALESCE(qty_in,0) END
               ), 0) - COALESCE(SUM(COALESCE(qty_out,0)), 0) AS on_hand
        FROM inventory_transactions
        WHERE inventory_code IN ({placeholders}) AND (company_id = :cid OR company_id IS NULL)
        GROUP BY inventory_code
    """), params).mappings().all()
    # Batch 93: only counts qty_in that QC has actually cleared (or
    # legacy rows from before this gate existed, which have no
    # qc_status at all — those pass through the CASE unaffected since
    # NULL never matches 'Pending'/'Failed'). A receipt still sitting in
    # QC Hold contributes its value to the ledger elsewhere (so
    # financials stay correct) but not to what production can actually
    # consume — the gate that makes /qc/inspection real rather than
    # decorative, per the blueprint's own stated principle.
    on_hand = {r["inventory_code"]: float(r["on_hand"] or 0) for r in stock_rows}

    shortages = []
    for key, r in required.items():
        available = on_hand.get(key, 0.0)
        if r["required_qty"] > available + 0.0001:
            shortages.append({
                "ingredient_code": r["ingredient_code"], "ingredient_name": r["ingredient_name"],
                "standard_uom": r["standard_uom"], "required_qty": round(r["required_qty"], 3),
                "available_qty": round(available, 3), "shortfall": round(r["required_qty"] - available, 3),
                "recipes": sorted(r["recipes"]),
            })
    return sorted(shortages, key=lambda x: -x["shortfall"])


def consolidated_bom(db: Session, order_no: str | None = None, order_nos: list[str] | None = None) -> list[dict[str, Any]]:
    q = db.query(BOMLine)
    if order_no:
        q = q.filter(BOMLine.order_no == order_no)
    if order_nos:
        q = q.filter(BOMLine.order_no.in_(order_nos))
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for line in q.all():
        key = (line.ingredient_code, line.standard_uom)
        if key not in grouped:
            grouped[key] = {
                "ingredient_code": line.ingredient_code,
                "ingredient_name": line.ingredient_name,
                "ingredient_category": getattr(line, "ingredient_category", None),
                "ingredient_main_category": getattr(line, "ingredient_main_category", None),
                "ingredient_sub_category": getattr(line, "ingredient_sub_category", None),
                "default_issue_section": getattr(line, "default_issue_section", None),
                "standard_uom": line.standard_uom,
                "total_required_qty_standard": 0.0,
                "total_required_with_waste_standard": 0.0,
                "total_estimated_cost": 0.0,
                "recipes_used": set(),
                "orders_used": set(),
            }
        g = grouped[key]
        g["total_required_qty_standard"] += _num(line.required_qty_standard)
        g["total_required_with_waste_standard"] += _num(line.total_required_with_waste_standard)
        g["total_estimated_cost"] += _num(line.estimated_cost)
        if line.recipe_name:
            g["recipes_used"].add(line.recipe_name)
        g["orders_used"].add(line.order_no)
    results = []
    for g in grouped.values():
        g["recipes_used"] = sorted(g["recipes_used"])
        g["orders_used"] = sorted(g["orders_used"])
        results.append(g)
    return sorted(results, key=lambda x: x["ingredient_name"])


def approve_order_before_bom(db: Session, order_no: str, approved_by: str) -> list[HeadChefPlan]:
    """Head Chef approval happens before BOM release.

    SAP-style process:
    1. Customer/Internal order is submitted.
    2. Head Chef approves the order/portion plan.
    3. BOM is generated after approval.
    4. BOM is released to Store Issuance.
    """
    order = db.query(CustomerOrder).filter(CustomerOrder.order_no == order_no).first()
    if not order:
        raise ValueError("Order not found")

    order_lines = db.query(OrderLine).filter(OrderLine.order_no == order_no).order_by(OrderLine.line_no).all()
    if not order_lines:
        raise ValueError("Order has no recipe lines")

    now = _now()
    order.status = "Head Chef Approved"
    order.approved_by = approved_by

    plans: list[HeadChefPlan] = []
    for line in order_lines:
        plan = db.query(HeadChefPlan).filter(
            HeadChefPlan.order_no == order_no,
            HeadChefPlan.order_line_id == line.id,
        ).first()
        if not plan:
            plan = HeadChefPlan(
                order_no=order_no,
                order_line_id=line.id,
                recipe_no=line.recipe_no,
                recipe_name=line.recipe_name,
                planned_date=order.required_delivery_date or date.today(),
                kitchen=order.kitchen,
                planned_section=order.kitchen or "Central Kitchen",
                planned_portions=line.required_portions,
                planned_batches=line.planned_batches,
            )
            db.add(plan)
        plan.planning_status = "Approved"
        plan.approved_by = approved_by
        plan.approved_at = now
        plans.append(plan)

    db.commit()
    return db.query(HeadChefPlan).filter(HeadChefPlan.order_no == order_no).all()


def approve_head_chef_plan(db: Session, order_no: str, approved_by: str) -> list[StoreIssuanceLine]:
    bom_lines = db.query(BOMLine).filter(BOMLine.order_no == order_no).all()
    if not bom_lines:
        raise ValueError("Generate BOM before releasing to Store Issuance")

    now = _now()
    for bom in bom_lines:
        bom.approved_by_head_chef = True
        bom.approved_by = approved_by
        bom.approved_at = now
        bom.bom_status = "Approved by Head Chef"

    order = db.query(CustomerOrder).filter(CustomerOrder.order_no == order_no).first()
    if order:
        order.status = "Store Pending"

    db.commit()
    return create_store_issuance_from_bom(db, order_no)


def create_store_issuance_from_bom(db: Session, order_no: str) -> list[StoreIssuanceLine]:
    created: list[StoreIssuanceLine] = []
    bom_lines = db.query(BOMLine).filter(BOMLine.order_no == order_no).all()
    for bom in bom_lines:
        exists = db.query(StoreIssuanceLine).filter(StoreIssuanceLine.bom_line_id == bom.id).first()
        if exists:
            continue
        route = _json_list(bom.route_template)
        route_first = route[1] if len(route) > 1 else DEFAULT_ISSUE_SECTION
        # Batch 131 — single source of truth for the issue-to section. PRD1-*
        # fresh produce → Cutting; otherwise the section the BOM inherited from
        # the recipe workbook (bom.default_issue_section). This SUPERSEDES the
        # Batch-124 PRD1-only rule: non-PRD1 lines now honour the recipe's own
        # kitchen section instead of collapsing to a generic default. The store
        # keeper can still change it on screen; this only sets the default.
        first_section = resolve_issue_section(
            bom.ingredient_code,
            None,  # sheet section already baked into default_issue_section at BOM time
            fallback=bom.default_issue_section or route_first,
        )
        available = (
            db.query(StockLot)
            .filter(StockLot.ingredient_code == bom.ingredient_code, StockLot.status == "Available")
            .all()
        )
        available_qty = sum(_num(x.available_qty_standard) for x in available)
        issue = StoreIssuanceLine(
            company_id=getattr(bom, "company_id", None),
            order_no=bom.order_no,
            order_line_id=bom.order_line_id,
            bom_line_id=bom.id,
            recipe_no=bom.recipe_no,
            recipe_name=bom.recipe_name,
            ingredient_code=bom.ingredient_code,
            ingredient_name=bom.ingredient_name,
            ingredient_main_category=getattr(bom, "ingredient_main_category", None),
            ingredient_sub_category=getattr(bom, "ingredient_sub_category", None),
            required_qty_standard=bom.required_qty_standard,
            required_qty_with_waste_standard=bom.total_required_with_waste_standard,
            standard_uom=bom.standard_uom,
            requested_qty=bom.total_required_with_waste_standard,
            requested_uom=bom.standard_uom,
            input_material_issued=bom.total_required_with_waste_standard,
            issued_uom=bom.standard_uom,
            issued_qty_standard=bom.total_required_with_waste_standard,
            variance_qty_standard=0,
            variance_pct=0,
            issue_to_section=first_section,
            route_template=bom.route_template,
            available_stock_standard=available_qty,
            issuance_status="Pending",
        )
        db.add(issue)
        created.append(issue)

    order = db.query(CustomerOrder).filter(CustomerOrder.order_no == order_no).first()
    if order and order.status not in {"Store Issued", "In Production"}:
        order.status = "Store Pending"
    db.commit()
    return db.query(StoreIssuanceLine).filter(StoreIssuanceLine.order_no == order_no).all()


def backfill_issue_sections(db: Session, company_id: int | None = None) -> dict[str, int]:
    """Batch 131 — re-apply the section routing rule to data created BEFORE this
    batch, without regenerating anything.

    Three passes, all idempotent:
      1. BOM lines whose default_issue_section doesn't match the rule → corrected
         using the matching recipe ingredient's kitchen_section (looked up by
         order recipe + ingredient code).
      2. Store-issuance lines that are still PENDING (not yet physically issued)
         → their issue_to_section is re-pointed to match their BOM line. Lines
         already issued/in production are LEFT ALONE: the pipeline lock owns them
         and re-pointing a completed issue would strand kitchen transactions.

    Returns a small counter dict for the README / admin toast.
    """
    counts = {"bom_updated": 0, "issue_updated": 0, "skipped_locked": 0}

    # --- Pass 1: BOM lines ------------------------------------------------
    bom_q = db.query(BOMLine)
    if company_id is not None:
        bom_q = bom_q.filter(BOMLine.company_id == company_id)
    for bom in bom_q.all():
        # Resolve the section this line SHOULD have. Prefer a matching recipe
        # ingredient's stored kitchen_section; fall back to the BOM's own value.
        sheet_section = None
        recipe = (
            db.query(Recipe)
            .filter(Recipe.recipe_code == bom.recipe_no)
            .order_by(Recipe.version.desc(), Recipe.id.desc())
            .first()
        )
        if recipe:
            ri = (
                db.query(RecipeIngredient)
                .filter(
                    RecipeIngredient.recipe_id == recipe.id,
                    RecipeIngredient.inventory_code == bom.ingredient_code,
                )
                .first()
            )
            sheet_section = getattr(ri, "kitchen_section", None) if ri else None

        target = resolve_issue_section(
            bom.ingredient_code, sheet_section,
            fallback=bom.default_issue_section or DEFAULT_ISSUE_SECTION,
        )
        if (bom.default_issue_section or "") != target:
            bom.default_issue_section = target
            counts["bom_updated"] += 1

    db.flush()

    # --- Pass 2: pending store-issuance lines -----------------------------
    iss_q = db.query(StoreIssuanceLine)
    if company_id is not None:
        iss_q = iss_q.filter(StoreIssuanceLine.company_id == company_id)
    for line in iss_q.all():
        if (line.issuance_status or "").strip().lower() not in {"pending", ""}:
            counts["skipped_locked"] += 1
            continue
        bom = db.query(BOMLine).filter(BOMLine.id == line.bom_line_id).first()
        target = resolve_issue_section(
            line.ingredient_code,
            None,
            fallback=(bom.default_issue_section if bom else None)
            or line.issue_to_section or DEFAULT_ISSUE_SECTION,
        )
        if (line.issue_to_section or "") != target:
            line.issue_to_section = target
            counts["issue_updated"] += 1

    db.commit()
    return counts


def update_store_issuance_line(
    db: Session,
    line_id: int,
    input_material_issued: float,
    issued_uom: str,
    issue_to_section: str,
    lot_no: str | None = None,
    supplier_name: str | None = None,
    remarks: str | None = None,
) -> StoreIssuanceLine:
    line = db.query(StoreIssuanceLine).filter(StoreIssuanceLine.id == line_id).first()
    if not line:
        raise ValueError("Store issuance line not found")
    if getattr(line, "finalized", False):
        raise ValueError("This store issuance is locked because it was already finalized. Use Re-issue/Edit first, then update quantities.")
    line.input_material_issued = _num(input_material_issued)
    line.issued_uom = issued_uom or line.standard_uom
    line.issued_qty_standard = convert_to_standard(
        line.input_material_issued,
        line.issued_uom,
        line.standard_uom,
        1,
    )
    line.issue_to_section = issue_to_section
    line.lot_no = lot_no
    line.supplier_name = supplier_name
    line.store_remarks = remarks
    line.variance_qty_standard = _num(line.issued_qty_standard) - _num(line.required_qty_with_waste_standard)
    required = max(_num(line.required_qty_with_waste_standard), 0.000001)
    line.variance_pct = line.variance_qty_standard / required * 100
    if _num(line.issued_qty_standard) <= 0:
        line.issuance_status = "Pending"
    elif _num(line.issued_qty_standard) < _num(line.required_qty_with_waste_standard):
        line.issuance_status = "Short Issued"
    else:
        line.issuance_status = "Issued"
    db.commit()
    db.refresh(line)
    return line


def finalize_store_issuance(db: Session, order_no: str, issued_by: str) -> list[KitchenSectionTransaction]:
    lines = db.query(StoreIssuanceLine).filter(StoreIssuanceLine.order_no == order_no).all()
    if not lines:
        raise ValueError("No store issuance lines found")
    created: list[KitchenSectionTransaction] = []
    now = _now()

    # ---- STOCK LEDGER: post ISSUE_OUT movements (Batch 4) ----
    # Every finalized issuance consumes stock. Defensive: ledger schema
    # differences must never block production.
    from sqlalchemy import text as _text
    for _l in lines:
        try:
            db.execute(_text("""
                INSERT INTO inventory_transactions
                    (company_id, txn_date, inventory_code, item_name, uom,
                     qty_in, qty_out, txn_type, ref_no, unit_cost, remarks, created_by)
                VALUES (:cid, NOW(), :code, :name, :uom, 0, :qty, 'STORE_ISSUE', :ref, 0, :rm, :by)
            """), {
                "cid": getattr(_l, "company_id", None),
                "code": _l.ingredient_code, "name": _l.ingredient_name,
                "uom": getattr(_l, "issued_uom", None) or getattr(_l, "standard_uom", ""),
                "qty": float(getattr(_l, "input_material_issued", 0) or 0),
                "ref": order_no, "rm": f"Store issue to {getattr(_l, 'issue_to_section', '')}",
                "by": issued_by,
            })
        except Exception:
            try:
                db.execute(_text("""
                    INSERT INTO inventory_transactions
                        (company_id, transaction_date, inventory_code, item_name, uom,
                         qty_in, qty_out, movement_type, reference_no, unit_cost, remarks, created_by)
                    VALUES (:cid, NOW(), :code, :name, :uom, 0, :qty, 'STORE_ISSUE', :ref, 0, :rm, :by)
                """), {
                    "cid": getattr(_l, "company_id", None),
                    "code": _l.ingredient_code, "name": _l.ingredient_name,
                    "uom": getattr(_l, "issued_uom", None) or "",
                    "qty": float(getattr(_l, "input_material_issued", 0) or 0),
                    "ref": order_no, "rm": f"Store issue to {getattr(_l, 'issue_to_section', '')}",
                    "by": issued_by,
                })
            except Exception:
                pass

    for line in lines:
        if line.finalized:
            continue
        issued_qty = _num(line.issued_qty_standard)
        if issued_qty <= 0:
            line.issuance_status = "Cancelled"
            continue
        route = _json_list(line.route_template) or ["Store", line.issue_to_section, "QC", "Packing"]
        if line.issue_to_section not in route:
            route.insert(1, line.issue_to_section)
        step_no = route.index(line.issue_to_section) if line.issue_to_section in route else 1
        next_section = route[step_no + 1] if len(route) > step_no + 1 else None

        line.finalized = True
        line.issued_by = issued_by
        line.issued_at = now
        line.issuance_status = "Issued" if issued_qty >= _num(line.required_qty_with_waste_standard) else "Short Issued"

        if line.lot_no:
            lot = (
                db.query(StockLot)
                .filter(StockLot.ingredient_code == line.ingredient_code, StockLot.lot_no == line.lot_no)
                .first()
            )
            if lot:
                lot.available_qty_standard = max(0, _num(lot.available_qty_standard) - issued_qty)
                inv = InventoryTransaction(
                    transaction_no=f"ISS-{order_no}-{line.id}",
                    ingredient_code=line.ingredient_code,
                    ingredient_name=line.ingredient_name,
                    lot_no=line.lot_no,
                    transaction_type="Issue",
                    qty_standard=issued_qty,
                    standard_uom=line.standard_uom,
                    from_location="Store",
                    to_location=line.issue_to_section,
                    reference_type="CustomerOrder",
                    reference_no=order_no,
                    performed_by=issued_by,
                    remarks=line.store_remarks,
                )
                db.add(inv)

        # ------------------------------------------------------------------
        # Batch 120: UPSERT the section transaction instead of blindly
        # inserting a new one.
        #
        # The old code always did db.add(tx). Re-issue clears line.finalized,
        # so finalize re-runs and created a SECOND kitchen_section_transactions
        # row for the same line — the "ingredient shown twice" bug — and could
        # also strand a row under the wrong section (e.g. Cold Kitchen showing
        # nothing). We now match an existing transaction for this exact line and
        # update it in place; only genuinely new lines are inserted.
        #
        # Match key: (order_no, order_line_id, ingredient_code). order_line_id is
        # the strongest key; ingredient_code disambiguates when line ids are null.
        # We only re-target section fields when the transaction has NOT yet been
        # received/processed downstream, so we never rewrite work already done in
        # a kitchen section.
        # ------------------------------------------------------------------
        existing = (
            db.query(KitchenSectionTransaction)
            .filter(
                KitchenSectionTransaction.order_no == line.order_no,
                KitchenSectionTransaction.ingredient_code == line.ingredient_code,
            )
        )
        if line.order_line_id is not None:
            existing = existing.filter(
                KitchenSectionTransaction.order_line_id == line.order_line_id
            )
        existing = existing.first()

        if existing:
            # ------------------------------------------------------------------
            # BATCH 144 — RE-ISSUE AFTER THE KITCHEN HAS RECEIVED
            #
            # This is why the store showed 6200 g issued while Butchery still
            # showed 6142.50 g received.
            #
            # `downstream_touched` treated "received" as work already done, so a
            # corrected issue updated issued_qty_standard and nothing else. But
            # the kitchen screen renders
            #     received_qty_standard OR issued_qty_standard
            # so once a line had been received even once, it displayed the OLD
            # received figure for ever. The store's correction was recorded and
            # then had no effect on the section that has to act on it — the
            # worst possible outcome, because both screens look authoritative.
            #
            # Receiving is not consumption. Nothing has been cut, cooked or
            # moved; the section simply acknowledged the delivery. So a line
            # that has been RECEIVED but not processed / transferred / wasted is
            # re-synced to the corrected quantity, with the change written into
            # the remark so the section can see it moved and why.
            #
            # A line that HAS been worked is deliberately left alone: silently
            # rewriting a quantity someone has already cut against would destroy
            # the audit trail. Those surface as a variance instead (see below).
            # ------------------------------------------------------------------
            _worked = (
                _num(getattr(existing, "processed_qty_standard", 0)) > 0
                or _num(getattr(existing, "transferred_qty_standard", 0)) > 0
                or _num(getattr(existing, "waste_qty_standard", 0)) > 0
            )
            _prev_recv = _num(getattr(existing, "received_qty_standard", 0))
            downstream_touched = _prev_recv > 0 or _worked

            existing.recipe_no = line.recipe_no
            existing.recipe_name = line.recipe_name
            existing.ingredient_name = line.ingredient_name
            existing.standard_uom = line.standard_uom
            existing.issued_qty_standard = issued_qty

            if _prev_recv > 0 and not _worked and abs(_prev_recv - issued_qty) > 0.0001:
                existing.received_qty_standard = issued_qty
                _note = (f"Store re-issued: {_prev_recv:.4f} -> {issued_qty:.4f} "
                         f"{line.standard_uom or ''}".strip())
                existing.section_remarks = (
                    f"{existing.section_remarks} | {_note}"
                    if getattr(existing, "section_remarks", None) else _note
                )
            elif _worked and abs(_num(existing.received_qty_standard) - issued_qty) > 0.0001:
                # Worked already — do not touch the quantities, but make the
                # mismatch visible instead of leaving it silent.
                _note = (f"VARIANCE: store issue corrected to {issued_qty:.4f} "
                         f"after processing began (received "
                         f"{_num(existing.received_qty_standard):.4f})")
                existing.section_remarks = (
                    f"{existing.section_remarks} | {_note}"
                    if getattr(existing, "section_remarks", None) else _note
                )
            # Balance = issued minus whatever has already moved downstream, so a
            # corrected (higher/lower) issue reflects correctly without dropping
            # progress the kitchen already recorded.
            _consumed = (
                _num(getattr(existing, "transferred_qty_standard", 0))
                + _num(getattr(existing, "waste_qty_standard", 0))
                + _num(getattr(existing, "returned_qty_standard", 0))
            )
            existing.balance_qty_standard = max(issued_qty - _consumed, 0)
            if not downstream_touched:
                # safe to re-target the section the store now chose
                existing.from_section = "Store"
                existing.current_section = line.issue_to_section
                existing.to_section = next_section
                existing.route_step_no = step_no
                existing.route_template = _dump_route(route)
                existing.transaction_status = "Pending Receive"
            created.append(existing)
        else:
            tx = KitchenSectionTransaction(
                company_id=getattr(line, "company_id", None),
                order_no=line.order_no,
                order_line_id=line.order_line_id,
                recipe_no=line.recipe_no,
                recipe_name=line.recipe_name,
                ingredient_code=line.ingredient_code,
                ingredient_name=line.ingredient_name,
                standard_uom=line.standard_uom,
                from_section="Store",
                current_section=line.issue_to_section,
                to_section=next_section,
                route_step_no=step_no,
                route_template=_dump_route(route),
                issued_qty_standard=issued_qty,
                received_qty_standard=0,
                processed_qty_standard=0,
                waste_qty_standard=0,
                returned_qty_standard=0,
                transferred_qty_standard=0,
                balance_qty_standard=issued_qty,
                transaction_status="Pending Receive",
            )
            db.add(tx)
            created.append(tx)

    order = db.query(CustomerOrder).filter(CustomerOrder.order_no == order_no).first()
    if order:
        order.status = "In Production"
    db.commit()
    return db.query(KitchenSectionTransaction).filter(KitchenSectionTransaction.order_no == order_no).all()


def receive_transaction(db: Session, tx_id: int, received_qty: float, received_by: str) -> KitchenSectionTransaction:
    tx = db.query(KitchenSectionTransaction).filter(KitchenSectionTransaction.id == tx_id).first()
    if not tx:
        raise ValueError("Transaction not found")
    # Batch 20: transferred/completed lines are locked for receiving too.
    _st = str(tx.transaction_status or "").upper()
    if _st == "TRANSFERRED" or _st.startswith("COMPLETED"):
        raise ValueError(f"Line {tx.ingredient_name} is already {tx.transaction_status} and locked.")
    tx.received_qty_standard = _num(received_qty)
    tx.balance_qty_standard = _num(received_qty)
    tx.received_by = received_by
    tx.received_at = _now()
    tx.transaction_status = "Received"
    db.commit()
    db.refresh(tx)
    return tx


def transfer_transaction(
    db: Session,
    tx_id: int,
    processed_qty: float,
    waste_qty: float,
    returned_qty: float,
    transferred_qty: float,
    user: str,
    waste_reason: str | None = None,
    remarks: str | None = None,
    next_section_override: str | None = None,
) -> KitchenSectionTransaction:
    tx = db.query(KitchenSectionTransaction).filter(KitchenSectionTransaction.id == tx_id).first()
    if not tx:
        raise ValueError("Transaction not found")
    # Batch 20: a transferred/completed line is LOCKED — it cannot be
    # transferred again from this section. The next section owns it now.
    cur_status = str(tx.transaction_status or "").upper()
    if cur_status == "TRANSFERRED" or cur_status.startswith("COMPLETED"):
        raise ValueError(f"Line {tx.ingredient_name} is already {tx.transaction_status} and locked.")
    received = _num(tx.received_qty_standard) or _num(tx.issued_qty_standard)
    processed = _num(processed_qty)
    waste = _num(waste_qty)
    returned = _num(returned_qty)
    transfer = _num(transferred_qty)
    if transfer > received - waste - returned + 0.0001:
        raise ValueError("Transferred quantity cannot exceed available quantity")

    tx.processed_qty_standard = processed
    tx.waste_qty_standard = waste
    tx.returned_qty_standard = returned
    tx.transferred_qty_standard = transfer
    tx.balance_qty_standard = max(0, received - waste - returned - transfer)
    tx.processed_by = user
    tx.transferred_by = user
    tx.processed_at = _now()
    tx.transferred_at = _now()
    tx.waste_reason = waste_reason
    tx.section_remarks = remarks

    route = _json_list(tx.route_template)
    next_step = _num(tx.route_step_no) + 1
    next_section = route[int(next_step)] if len(route) > int(next_step) else None

    # Client requirement: each section workstation can decide the next section
    # when production needs to branch (Thawing -> Butchery/Hot Kitchen, Cutting -> Cold Kitchen, etc.).
    # We keep route_template for audit but allow an operational override.
    if next_section_override:
        next_section = _normalize_section(next_section_override)
        if tx.current_section not in route:
            route.insert(0, tx.current_section)
        cur_idx = route.index(tx.current_section) if tx.current_section in route else int(_num(tx.route_step_no))
        if next_section not in route[cur_idx + 1:cur_idx + 2]:
            route = route[:cur_idx + 1] + [next_section] + [x for x in route[cur_idx + 1:] if x != next_section]
            tx.route_template = _dump_route(route)
        next_step = cur_idx + 1

    next_section = _normalize_section(next_section) if next_section else next_section
    after_next = route[int(next_step) + 1] if len(route) > int(next_step) + 1 else None

    if next_section and transfer > 0:
        tx.transaction_status = "Transferred"
        # Batch 122 FIX: the locked row must record where it ACTUALLY went, not
        # the stale route-derived value. Previously tx.to_section kept its old
        # value, so a line transferred to QC / Cold Kitchen / Trayline still
        # displayed "sent to Hot Kitchen". Set it to the real destination.
        tx.to_section = next_section
        new_tx = KitchenSectionTransaction(
            company_id=getattr(tx, "company_id", None),
            order_no=tx.order_no,
            order_line_id=tx.order_line_id,
            recipe_no=tx.recipe_no,
            recipe_name=tx.recipe_name,
            ingredient_code=tx.ingredient_code,
            ingredient_name=tx.ingredient_name,
            standard_uom=tx.standard_uom,
            from_section=tx.current_section,
            current_section=next_section,
            to_section=after_next,
            route_step_no=int(next_step),
            route_template=tx.route_template,
            issued_qty_standard=transfer,
            balance_qty_standard=transfer,
            transaction_status="Pending Receive",
        )
        db.add(new_tx)
    else:
        tx.transaction_status = "Completed"

    db.commit()
    db.refresh(tx)
    return tx


def ingredient_wise_consolidated(db: Session, order_no: str | None = None,
                                 section: str = "Cutting") -> list[dict[str, Any]]:
    """Group a section's work at INGREDIENT level, across all recipes.

    Batch 133 — the mirror image of bakery_pastry_consolidated. One ingredient
    (e.g. Fresh Onion) is issued separately for many recipes, but a prep cook
    wants to receive/wash/cut ALL of it in one motion. This groups by
    order + ingredient_code and sums quantities across every recipe that uses it,
    keeping the underlying line txs in `details` so a bulk action can fan out to
    each one. Works for any section; used by Cutting, Butchery, Hot, Cold and
    Bakery/Pastry.
    """
    q = db.query(KitchenSectionTransaction).filter(
        KitchenSectionTransaction.current_section == section
    )
    if order_no:
        q = q.filter(KitchenSectionTransaction.order_no == order_no)

    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for tx in q.order_by(KitchenSectionTransaction.order_no,
                         KitchenSectionTransaction.ingredient_name,
                         KitchenSectionTransaction.recipe_no).all():
        key = (tx.order_no, tx.ingredient_code or tx.ingredient_name or "")
        if key not in grouped:
            grouped[key] = {
                "order_no": tx.order_no,
                "ingredient_code": tx.ingredient_code,
                "ingredient_name": tx.ingredient_name,
                "uom": tx.standard_uom,
                "recipes_count": 0,
                "total_issued_qty_standard": 0.0,
                "total_received_qty_standard": 0.0,
                "total_processed_qty_standard": 0.0,
                # Batch 149: a real TRANSFERRED total. Both group headers were
                # labelled "Transferred" but summed processed_qty_standard —
                # the same wrong-field bug Batch 143 fixed on the By Line
                # column, repeated at group level in two more places.
                "total_transferred_qty_standard": 0.0,
                "total_waste_qty_standard": 0.0,
                "total_balance_qty_standard": 0.0,
                "pending_receive": 0,
                "received_ready": 0,
                "locked": 0,
                "details": [],
            }
        g = grouped[key]
        g["recipes_count"] += 1
        g["total_issued_qty_standard"] += _num(tx.issued_qty_standard)
        g["total_received_qty_standard"] += _num(tx.received_qty_standard)
        g["total_processed_qty_standard"] += _num(tx.processed_qty_standard)
        g["total_transferred_qty_standard"] += _num(tx.transferred_qty_standard)
        g["total_waste_qty_standard"] += _num(tx.waste_qty_standard)
        g["total_balance_qty_standard"] += _num(tx.balance_qty_standard)
        status = str(tx.transaction_status or "").upper()
        if status == "TRANSFERRED" or status.startswith("COMPLETED"):
            g["locked"] += 1
        elif _num(tx.received_qty_standard) > 0:
            g["received_ready"] += 1
        else:
            g["pending_receive"] += 1
        g["details"].append(tx)

    return sorted(grouped.values(),
                  key=lambda x: (x["order_no"], x["ingredient_name"] or ""))


def bulk_receive_ingredient(db: Session, order_no: str, section: str,
                            ingredient_code: str, user: str) -> tuple[int, int]:
    """Receive every not-yet-received line for one ingredient in one section.

    Batch 133 — powers ingredient-wise bulk receiving. Each pending line is
    received at its full issued quantity. Returns (received, skipped)."""
    txs = db.query(KitchenSectionTransaction).filter(
        KitchenSectionTransaction.order_no == order_no,
        KitchenSectionTransaction.current_section == section,
        KitchenSectionTransaction.ingredient_code == ingredient_code,
    ).all()
    ok = skipped = 0
    for tx in txs:
        status = str(tx.transaction_status or "").upper()
        if status == "TRANSFERRED" or status.startswith("COMPLETED"):
            skipped += 1
            continue
        if _num(tx.received_qty_standard) > 0:
            skipped += 1
            continue
        try:
            receive_transaction(db, tx.id, _num(tx.issued_qty_standard), user)
            ok += 1
        except ValueError:
            skipped += 1
    return ok, skipped


def bulk_receive_recipe(db: Session, order_no: str, section: str,
                        recipe_no: str, user: str) -> tuple[int, int]:
    """Receive every not-yet-received line for one RECIPE in one section.

    Batch 135 — recipe-wise receive, so By-Recipe has the same one-click
    "Receive All" that By-Ingredient already offers. Returns (received, skipped)."""
    txs = db.query(KitchenSectionTransaction).filter(
        KitchenSectionTransaction.order_no == order_no,
        KitchenSectionTransaction.current_section == section,
        KitchenSectionTransaction.recipe_no == recipe_no,
    ).all()
    ok = skipped = 0
    for tx in txs:
        status = str(tx.transaction_status or "").upper()
        if status == "TRANSFERRED" or status.startswith("COMPLETED"):
            skipped += 1
            continue
        if _num(tx.received_qty_standard) > 0:
            skipped += 1
            continue
        try:
            receive_transaction(db, tx.id, _num(tx.issued_qty_standard), user)
            ok += 1
        except ValueError:
            skipped += 1
    return ok, skipped


def _prorata_process_transfer(
    db: Session, eligible: list, total_txs: int,
    processed_qty: float, waste_qty: float, returned_qty: float,
    transferred_qty: float, next_section: str | None,
    waste_reason: str | None, remarks: str | None, user: str,
) -> tuple[int, int]:
    """Shared core: split entered totals PRO-RATA across already-filtered
    eligible (received, unlocked) lines by received qty, last line absorbing the
    rounding remainder so section totals reconcile exactly. Used by both the
    ingredient-wise and recipe-wise bulk process/transfer endpoints so their
    maths is guaranteed identical. `eligible` is a list of (tx, received_qty)."""
    if not eligible:
        return 0, total_txs

    total_recv = sum(r for _, r in eligible) or 1.0
    P, W, R, T = (_num(processed_qty), _num(waste_qty),
                  _num(returned_qty), _num(transferred_qty))

    ok = skipped = 0
    n = len(eligible)
    acc = {"p": 0.0, "w": 0.0, "r": 0.0, "t": 0.0}
    for idx, (tx, recv) in enumerate(eligible):
        share = recv / total_recv
        if idx < n - 1:
            p = round(P * share, 4); w = round(W * share, 4)
            r = round(R * share, 4); t = round(T * share, 4)
            acc["p"] += p; acc["w"] += w; acc["r"] += r; acc["t"] += t
        else:
            p = round(P - acc["p"], 4); w = round(W - acc["w"], 4)
            r = round(R - acc["r"], 4); t = round(T - acc["t"], 4)
        try:
            transfer_transaction(db, tx.id, p, w, r, t, user,
                                 waste_reason, remarks, next_section or None)
            ok += 1
        except ValueError:
            skipped += 1
    return ok, skipped


def _eligible_lines(txs: list) -> list:
    """Filter kitchen txs to (tx, received_qty) pairs that are received and not
    yet locked — the only lines a bulk process/transfer may touch."""
    out = []
    for tx in txs:
        status = str(tx.transaction_status or "").upper()
        if status == "TRANSFERRED" or status.startswith("COMPLETED"):
            continue
        recv = _num(tx.received_qty_standard) or _num(tx.issued_qty_standard)
        if recv <= 0:
            continue
        out.append((tx, recv))
    return out


def bulk_process_transfer_ingredient(
    db: Session, order_no: str, section: str, ingredient_code: str,
    processed_qty: float, waste_qty: float, returned_qty: float,
    transferred_qty: float, next_section: str | None,
    waste_reason: str | None, remarks: str | None, user: str,
) -> tuple[int, int]:
    """Process + transfer every received line for one ingredient, distributing
    the entered totals PRO-RATA across the ingredient's lines by received qty.

    Batch 133 — the ingredient-wise twin of the single-line Process & Transfer.
    Lines already locked or not yet received are skipped."""
    txs = db.query(KitchenSectionTransaction).filter(
        KitchenSectionTransaction.order_no == order_no,
        KitchenSectionTransaction.current_section == section,
        KitchenSectionTransaction.ingredient_code == ingredient_code,
    ).all()
    return _prorata_process_transfer(
        db, _eligible_lines(txs), len(txs),
        processed_qty, waste_qty, returned_qty, transferred_qty,
        next_section, waste_reason, remarks, user)


def bulk_process_transfer_recipe(
    db: Session, order_no: str, section: str, recipe_no: str,
    processed_qty: float, waste_qty: float, returned_qty: float,
    transferred_qty: float, next_section: str | None,
    waste_reason: str | None, remarks: str | None, user: str,
) -> tuple[int, int]:
    """Batch 134 — recipe-wise twin of the above. Process + transfer every
    received line of ONE recipe in this prep section (Cutting/Butchery), with the
    full process/waste/return/transfer field set split pro-rata across the
    recipe's ingredient lines. Same maths as the single-line and ingredient-wise
    panels. Cook sections keep the cooked-output form (process_bakery_pastry_recipe)."""
    txs = db.query(KitchenSectionTransaction).filter(
        KitchenSectionTransaction.order_no == order_no,
        KitchenSectionTransaction.current_section == section,
        KitchenSectionTransaction.recipe_no == recipe_no,
    ).all()
    return _prorata_process_transfer(
        db, _eligible_lines(txs), len(txs),
        processed_qty, waste_qty, returned_qty, transferred_qty,
        next_section, waste_reason, remarks, user)


def bakery_pastry_consolidated(db: Session, order_no: str | None = None,
                               section: str = "Bakery/Pastry") -> list[dict[str, Any]]:
    """Group a section's work at recipe level, not item level.

    Client requirement: Bakery/Pastry (and now Cold Kitchen + Hot Kitchen too)
    receive separate ingredients but process them as one bulk recipe. This
    groups by order + recipe and sums all issued/received ingredient quantities
    for that recipe. `section` defaults to Bakery/Pastry for backward-compat.
    """
    q = db.query(KitchenSectionTransaction).filter(
        KitchenSectionTransaction.current_section == section
    )
    if order_no:
        q = q.filter(KitchenSectionTransaction.order_no == order_no)

    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for tx in q.order_by(KitchenSectionTransaction.order_no, KitchenSectionTransaction.recipe_no, KitchenSectionTransaction.ingredient_name).all():
        key = (tx.order_no, tx.recipe_no or "")
        if key not in grouped:
            grouped[key] = {
                "order_no": tx.order_no,
                "recipe_no": tx.recipe_no,
                "recipe_name": tx.recipe_name,
                "ingredients_count": 0,
                "total_issued_qty_standard": 0.0,
                "total_received_qty_standard": 0.0,
                "total_processed_qty_standard": 0.0,
                # Batch 149: a real TRANSFERRED total. Both group headers were
                # labelled "Transferred" but summed processed_qty_standard —
                # the same wrong-field bug Batch 143 fixed on the By Line
                # column, repeated at group level in two more places.
                "total_transferred_qty_standard": 0.0,
                "total_waste_qty_standard": 0.0,
                "total_balance_qty_standard": 0.0,
                # Batch 135: per-status counts so By-Recipe can offer a recipe-wise
                # "Receive All (n)" button like By-Ingredient does.
                "pending_receive": 0,
                "received_ready": 0,
                "locked": 0,
                "details": [],
            }
        g = grouped[key]
        g["ingredients_count"] += 1
        g["total_issued_qty_standard"] += _num(tx.issued_qty_standard)
        g["total_received_qty_standard"] += _num(tx.received_qty_standard)
        g["total_processed_qty_standard"] += _num(tx.processed_qty_standard)
        g["total_transferred_qty_standard"] += _num(tx.transferred_qty_standard)
        g["total_waste_qty_standard"] += _num(tx.waste_qty_standard)
        g["total_balance_qty_standard"] += _num(tx.balance_qty_standard)
        _st = str(tx.transaction_status or "").upper()
        if _st == "TRANSFERRED" or _st.startswith("COMPLETED"):
            g["locked"] += 1
        elif _num(tx.received_qty_standard) > 0:
            g["received_ready"] += 1
        else:
            g["pending_receive"] += 1
        g["details"].append(tx)

    return sorted(grouped.values(), key=lambda x: (x["order_no"], x["recipe_name"] or ""))


def process_bakery_pastry_recipe(
    db: Session,
    order_no: str,
    recipe_no: str,
    output_qty: float,
    output_uom: str,
    next_section: str,
    user: str,
    waste_qty: float = 0,
    remarks: str | None = None,
    source_section: str = "Bakery/Pastry",
) -> KitchenSectionTransaction:
    """Complete bulk recipe processing for one order+recipe in a bulk-cook
    section (Bakery/Pastry, Cold Kitchen or Hot Kitchen — `source_section`).

    Original ingredient transactions are closed as one mixed batch. A new
    transaction is created for the finished/semi-finished recipe output and
    forwarded to the selected next kitchen stage.
    """
    txs = db.query(KitchenSectionTransaction).filter(
        KitchenSectionTransaction.order_no == order_no,
        KitchenSectionTransaction.recipe_no == recipe_no,
        KitchenSectionTransaction.current_section == source_section,
    ).all()
    if not txs:
        raise ValueError(f"No {source_section} transactions found for this recipe/order")

    output = _num(output_qty)
    waste = _num(waste_qty)
    if output <= 0:
        raise ValueError("Output quantity must be greater than zero")

    now = _now()
    total_received = 0.0
    for tx in txs:
        received = _num(tx.received_qty_standard) or _num(tx.issued_qty_standard)
        total_received += received
        tx.received_qty_standard = received
        tx.processed_qty_standard = received
        tx.waste_qty_standard = 0
        tx.returned_qty_standard = 0
        tx.transferred_qty_standard = 0
        tx.balance_qty_standard = 0
        tx.received_by = tx.received_by or user
        tx.processed_by = user
        tx.transferred_by = user
        tx.received_at = tx.received_at or now
        tx.processed_at = now
        tx.transferred_at = now
        tx.transaction_status = f"Completed - Mixed in {source_section}"
        # BATCH 143 FIX — the Action column on a locked line renders tx.to_section.
        # These rows kept whatever the ORIGINAL route said (often "Bakery/Pastry"),
        # so a line mixed in Hot Kitchen and sent to QC still displayed
        # "Bakery/Pastry" as its destination. Record where it actually went.
        tx.to_section = next_section
        tx.section_remarks = remarks

    # Keep recipe-level waste on the first ingredient transaction for audit history.
    txs[0].waste_qty_standard = waste
    txs[0].waste_reason = f"Recipe-level {source_section} wastage"

    route = [source_section, next_section, "QC", "Packing", "Dispatch"]
    recipe_name = txs[0].recipe_name or recipe_no

    # ======================================================================
    # BATCH 158 — RE-PROCESSING A RECIPE MUST CORRECT, NOT DUPLICATE
    #
    # Processing the same recipe three times (output 1, then 200, then 200)
    # created THREE separate QC rows, and QC's Input Qty summed them to 401 for
    # a single 1-portion order. Every re-run was an unconditional db.add().
    #
    # A chef correcting an output quantity is stating "the output is actually
    # 200", not "produce 200 more". Treating a correction as an addition
    # inflates QC input, inflates what Packing expects, and there is no way for
    # QC to tell which of the three rows is the real one.
    #
    # So: if an output row for this (order, recipe, from_section) already exists
    # and the downstream section has NOT started work on it, overwrite it.
    #
    # If QC HAS already received or processed it, a new row is still created —
    # at that point the earlier output is a real event that was really handed
    # over, and silently rewriting it would erase something QC has physically
    # inspected. That case is rare and now carries an explicit remark.
    # ======================================================================
    existing_out = (
        db.query(KitchenSectionTransaction)
        .filter(
            KitchenSectionTransaction.order_no == order_no,
            KitchenSectionTransaction.recipe_no == recipe_no,
            KitchenSectionTransaction.ingredient_code == recipe_no,
            KitchenSectionTransaction.from_section == source_section,
            KitchenSectionTransaction.current_section == next_section,
        )
        .order_by(KitchenSectionTransaction.id.desc())
        .first()
    )
    _downstream_started = bool(existing_out) and (
        _num(existing_out.received_qty_standard) > 0
        or _num(existing_out.processed_qty_standard) > 0
        or _num(existing_out.transferred_qty_standard) > 0
    )

    if existing_out is not None and not _downstream_started:
        existing_out.standard_uom = output_uom or "Portions"
        existing_out.issued_qty_standard = output
        existing_out.balance_qty_standard = output
        existing_out.transaction_status = "Pending Receive"
        existing_out.section_remarks = (
            f"{source_section} output (revised). Input received total: "
            f"{total_received:.4f}. Recipe waste: {waste:.4f}. {remarks or ''}"
        ).strip()
        existing_out.updated_at = _now()
        order = db.query(CustomerOrder).filter(CustomerOrder.order_no == order_no).first()
        if order and order.status in {"Store Pending", "In Production", "BOM Generated"}:
            order.status = "In Production"
        db.commit()
        db.refresh(existing_out)
        return existing_out

    _revision_note = ""
    if _downstream_started:
        _revision_note = (f" ADDITIONAL OUTPUT — a previous output of "
                          f"{_num(existing_out.issued_qty_standard):.4f} was already "
                          f"received downstream and was left untouched.")

    next_tx = KitchenSectionTransaction(
        company_id=getattr(txs[0], "company_id", None),
        order_no=order_no,
        order_line_id=txs[0].order_line_id,
        recipe_no=recipe_no,
        recipe_name=recipe_name,
        ingredient_code=recipe_no,
        ingredient_name=recipe_name,
        standard_uom=output_uom or "Portions",
        from_section=source_section,
        current_section=next_section,
        to_section="QC" if next_section not in {"QC", "Packing", "Dispatch"} else ("Packing" if next_section == "QC" else "Dispatch"),
        route_step_no=1,
        route_template=_dump_route(route),
        issued_qty_standard=output,
        received_qty_standard=0,
        processed_qty_standard=0,
        waste_qty_standard=0,
        returned_qty_standard=0,
        transferred_qty_standard=0,
        balance_qty_standard=output,
        transaction_status="Pending Receive",
        # BATCH 143 FIX — "Bakery/Pastry" was hardcoded. This function serves
        # every section, so a Hot Kitchen mix was writing "Bakery/Pastry output"
        # into the remark that QC then reads on screen. Use the real section.
        section_remarks=(f"{source_section} output. Input received total: {total_received:.4f}. "
                         f"Recipe waste: {waste:.4f}. {remarks or ''}{_revision_note}").strip(),
    )
    db.add(next_tx)
    order = db.query(CustomerOrder).filter(CustomerOrder.order_no == order_no).first()
    if order and order.status in {"Store Pending", "In Production", "BOM Generated"}:
        order.status = "In Production"
    db.commit()
    db.refresh(next_tx)
    return next_tx
