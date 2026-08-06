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

            # Prefer qty_per_portion. If missing, calculate from qty_batch / standard portions.
            qty_per_portion = _num(ri.qty_per_portion)
            if qty_per_portion <= 0 and _num(ri.qty_batch) > 0:
                qty_per_portion = _num(ri.qty_batch) / std_portions

            required_recipe_qty = qty_per_portion * order_portions
            required_std = convert_to_standard(required_recipe_qty, recipe_uom, standard_uom, conv)
            waste_pct = _num(recipe.target_wastage_pct) * 100 if _num(recipe.target_wastage_pct) < 1 else _num(recipe.target_wastage_pct)
            expected_waste = required_std * waste_pct / 100
            required_with_waste = required_std + expected_waste
            cost = required_with_waste * unit_cost
            route = default_route_for_ingredient(ingredient, ri.item_name)
            first_section = route[1] if len(route) > 1 else "Hot Kitchen"

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
        SELECT inventory_code, COALESCE(SUM(COALESCE(qty_in,0)) - SUM(COALESCE(qty_out,0)), 0) AS on_hand
        FROM inventory_transactions
        WHERE inventory_code IN ({placeholders}) AND (company_id = :cid OR company_id IS NULL)
        GROUP BY inventory_code
    """), params).mappings().all()
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
        first_section = bom.default_issue_section or (route[1] if len(route) > 1 else "Hot Kitchen")
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


def bakery_pastry_consolidated(db: Session, order_no: str | None = None) -> list[dict[str, Any]]:
    """Group Bakery/Pastry work at recipe level, not item level.

    Client requirement: Bakery/Pastry receives separate ingredients but processes them
    as one bulk recipe. Therefore this screen groups by order + recipe and sums all
    issued/received ingredient quantities for that recipe.
    """
    q = db.query(KitchenSectionTransaction).filter(
        KitchenSectionTransaction.current_section == "Bakery/Pastry"
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
                "total_waste_qty_standard": 0.0,
                "total_balance_qty_standard": 0.0,
                "details": [],
            }
        g = grouped[key]
        g["ingredients_count"] += 1
        g["total_issued_qty_standard"] += _num(tx.issued_qty_standard)
        g["total_received_qty_standard"] += _num(tx.received_qty_standard)
        g["total_processed_qty_standard"] += _num(tx.processed_qty_standard)
        g["total_waste_qty_standard"] += _num(tx.waste_qty_standard)
        g["total_balance_qty_standard"] += _num(tx.balance_qty_standard)
        g["details"].append(tx)

    return sorted(grouped.values(), key=lambda x: (x["order_no"], x["recipe_name"] or ""))


def process_bakery_pastry_recipe(
    db: Session,
    order_no: str,
    recipe_no: str,
    output_qty: float,
    output_uom: str,
    waste_qty: float,
    next_section: str,
    user: str,
    remarks: str | None = None,
) -> KitchenSectionTransaction:
    """Complete Bakery/Pastry bulk processing for one order+recipe.

    Original ingredient transactions are closed as one mixed batch. A new transaction
    is created for the finished/semi-finished recipe output and forwarded to the
    selected next kitchen stage, e.g. Trayline/Packing, Packing, Hot Kitchen, Cold
    Kitchen or QC.
    """
    txs = db.query(KitchenSectionTransaction).filter(
        KitchenSectionTransaction.order_no == order_no,
        KitchenSectionTransaction.recipe_no == recipe_no,
        KitchenSectionTransaction.current_section == "Bakery/Pastry",
    ).all()
    if not txs:
        raise ValueError("No Bakery/Pastry transactions found for this recipe/order")

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
        tx.transaction_status = "Completed - Mixed in Bakery/Pastry"
        tx.section_remarks = remarks

    # Keep recipe-level waste on the first ingredient transaction for audit history.
    txs[0].waste_qty_standard = waste
    txs[0].waste_reason = "Recipe-level Bakery/Pastry wastage"

    route = ["Bakery/Pastry", next_section, "QC", "Packing", "Dispatch"]
    recipe_name = txs[0].recipe_name or recipe_no
    next_tx = KitchenSectionTransaction(
        company_id=getattr(txs[0], "company_id", None),
        order_no=order_no,
        order_line_id=txs[0].order_line_id,
        recipe_no=recipe_no,
        recipe_name=recipe_name,
        ingredient_code=recipe_no,
        ingredient_name=recipe_name,
        standard_uom=output_uom or "Portions",
        from_section="Bakery/Pastry",
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
        section_remarks=f"Bakery/Pastry output. Input received total: {total_received:.4f}. Recipe waste: {waste:.4f}. {remarks or ''}".strip(),
    )
    db.add(next_tx)
    order = db.query(CustomerOrder).filter(CustomerOrder.order_no == order_no).first()
    if order and order.status in {"Store Pending", "In Production", "BOM Generated"}:
        order.status = "In Production"
    db.commit()
    db.refresh(next_tx)
    return next_tx
