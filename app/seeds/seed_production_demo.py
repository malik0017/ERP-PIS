#app/seeds/seed_production_demo.py
from datetime import date, timedelta

from app.database.session import SessionLocal
from app.models.ingredient import Ingredient
from app.models.inventory import StockLot
from app.models.recipe import Recipe, RecipeIngredient


def upsert(db, model, lookup: dict, values: dict):
    obj = db.query(model).filter_by(**lookup).first()
    if not obj:
        obj = model(**lookup)
        db.add(obj)
    for k, v in values.items():
        setattr(obj, k, v)
    return obj


def main():
    db = SessionLocal()
    try:
        ingredients = [
            ("ING-001", "Chicken Breast", "Poultry", "Kg", "Kg", "Kg", 1, 24, "Thawing", dict(requires_thawing=True, requires_butchery=True, requires_marination=True)),
            ("ING-002", "Rice", "Grains", "Kg", "Kg", "Kg", 1, 6, "Hot Kitchen", dict()),
            ("ING-003", "Onion", "Produce", "Kg", "Kg", "Kg", 1, 3, "Cutting", dict(requires_cutting=True)),
            ("ING-004", "Tomato", "Produce", "Kg", "Kg", "Kg", 1, 4, "Cutting", dict(requires_cutting=True)),
            ("ING-005", "Flour", "Bakery", "Kg", "Kg", "Kg", 1, 5, "Bakery/Pastry", dict(is_bakery_item=True)),
            ("ING-006", "Sugar", "Bakery", "Kg", "Kg", "Kg", 1, 4, "Bakery/Pastry", dict(is_bakery_item=True)),
            ("ING-007", "Butter", "Dairy", "Kg", "Kg", "Kg", 1, 18, "Bakery/Pastry", dict(is_bakery_item=True)),
            ("ING-008", "Milk", "Dairy", "L", "mL", "L", 0.001, 7, "Bakery/Pastry", dict(is_bakery_item=True)),
            ("ING-009", "Lettuce", "Produce", "Kg", "Kg", "Kg", 1, 8, "Cutting", dict(requires_cutting=True, is_cold_kitchen_item=True)),
            ("ING-010", "Packaging Tray", "Packaging", "Tray", "Each", "Each", 1, 0.8, "Packing", dict()),
        ]
        for code, name, cat, purchase, recipe_uom, standard, conv, cost, section, flags in ingredients:
            data = dict(
                name=name,
                category=cat,
                purchase_uom=purchase,
                recipe_uom=recipe_uom,
                standard_uom=standard,
                conversion_to_standard=conv,
                unit_cost_standard=cost,
                default_issue_section=section,
                status="Active",
            )
            data.update(flags)
            upsert(db, Ingredient, {"ingredient_code": code}, data)

        recipes = [
            ("RCP-001", "Chicken Kabsa", "Hot Meals", 100, 28, [("ING-001", "Chicken Breast", 40, "Kg"), ("ING-002", "Rice", 25, "Kg"), ("ING-003", "Onion", 6, "Kg")]),
            ("RCP-002", "Green Salad", "Salads", 100, 18, [("ING-009", "Lettuce", 12, "Kg"), ("ING-004", "Tomato", 8, "Kg")]),
            ("RCP-003", "Bread Roll", "Bakery", 100, 5, [("ING-005", "Flour", 12, "Kg"), ("ING-006", "Sugar", 1.5, "Kg"), ("ING-008", "Milk", 3000, "mL")]),
            ("RCP-004", "Chocolate Cake", "Desserts", 50, 25, [("ING-005", "Flour", 5, "Kg"), ("ING-006", "Sugar", 4, "Kg"), ("ING-007", "Butter", 3, "Kg"), ("ING-008", "Milk", 2000, "mL")]),
        ]
        for rcp_no, name, cat, portions, price, items in recipes:
            upsert(
                db,
                Recipe,
                {"recipe_no": rcp_no},
                {
                    "name": name,
                    "brand": "Gourmet 360",
                    "kitchen": "Central Kitchen - Ishbiliyah",
                    "category": cat,
                    "std_portions_per_batch": portions,
                    "selling_price_per_portion": price,
                    "status": "Active",
                },
            )
            for ing_code, ing_name, qty, recipe_uom in items:
                ing = db.query(Ingredient).filter(Ingredient.ingredient_code == ing_code).first()
                existing = db.query(RecipeIngredient).filter_by(recipe_no=rcp_no, ingredient_code=ing_code).first()
                if not existing:
                    existing = RecipeIngredient(recipe_no=rcp_no, ingredient_code=ing_code)
                    db.add(existing)
                existing.ingredient_name = ing_name
                existing.gross_qty = qty
                existing.net_qty = qty * 0.95
                existing.wastage_pct = 5
                existing.recipe_uom = recipe_uom
                existing.standard_uom = ing.standard_uom if ing else recipe_uom
                existing.conversion_to_standard = ing.conversion_to_standard if ing else 1
                existing.cost_per_standard_unit = ing.unit_cost_standard if ing else 0
                existing.default_issue_section = ing.default_issue_section if ing else "Hot Kitchen"

        for ing in db.query(Ingredient).all():
            lot_no = f"LOT-{ing.ingredient_code}-001"
            upsert(
                db,
                StockLot,
                {"ingredient_code": ing.ingredient_code, "lot_no": lot_no},
                {
                    "ingredient_name": ing.name,
                    "supplier_name": ing.default_supplier or "Demo Supplier",
                    "received_date": date.today(),
                    "expiry_date": date.today() + timedelta(days=30),
                    "standard_uom": ing.standard_uom,
                    "received_qty_standard": 1000,
                    "available_qty_standard": 1000,
                    "unit_cost_standard": ing.unit_cost_standard,
                    "storage_type": ing.storage_type,
                    "status": "Available",
                },
            )

        db.commit()
        print("Demo production master data inserted/updated.")
    finally:
        db.close()


if __name__ == "__main__":
    main()