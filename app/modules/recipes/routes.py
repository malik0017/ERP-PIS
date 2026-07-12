# app/modules/recipes/routes.py
import os
import tempfile
from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session, selectinload
from sqlalchemy import func, text

from app.database.session import get_db
from app.models.recipe import Recipe, RecipeIngredient
from app.models.customer import Customer
from app.models.ingredient import Ingredient
from app.models.master_data import Brand
from app.services.recipe_service import import_recipe_excel, recalc_recipe
from app.core.auth import get_current_user
from app.core.templates import templates


router = APIRouter(prefix="/recipes", tags=["Recipes"])


def _company_id(user) -> int:
    return getattr(user, "company_id", None) or 1


def _d(value, default="0") -> Decimal:
    if value in (None, "", "-", "—"):
        return Decimal(default)

    try:
        return Decimal(str(value))
    except Exception:
        return Decimal(default)


def _recipe_form_context(request: Request, db: Session, current_user, recipe=None, mode: str = "create"):
    company_id = _company_id(current_user)
    customers = (
        db.query(Customer)
        .filter(Customer.company_id == company_id, Customer.is_active == True)
        .order_by(Customer.customer_name.asc())
        .all()
    )
    brands = (
        db.query(Brand)
        .filter(Brand.company_id == company_id, Brand.is_active == True)
        .order_by(Brand.brand_name_en.asc())
        .all()
    )
    items = (
        db.query(Ingredient)
        .filter(Ingredient.status.in_(["ACTIVE", "Active"]))
        .order_by(Ingredient.name.asc())
        .limit(3000)
        .all()
    )
    inventory_items = [
        {
            "code": i.ingredient_code,
            "name": i.name,
            "inventory_uom": i.purchase_uom or i.standard_uom or "Each",
            "recipe_uom": i.recipe_uom or i.purchase_uom or i.standard_uom or "Each",
            "cost": float(i.unit_cost_standard or 0),
            "category": i.category or "",
        }
        for i in items
    ]
    return {
        "request": request,
        "recipe": recipe,
        "mode": mode,
        "customers": customers,
        "brands": brands,
        "inventory_items": inventory_items,
    }


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def recipe_list(
    request: Request,
    search: str | None = None,
    status: str | None = "ACTIVE",
    category: str | None = None,
    customer: str | None = None,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Professional SAP-style recipe master list.

    This list uses direct SQL and does not depend on ORM relationship loading.
    The project is currently single-company, so it also contains a defensive
    fallback that displays the records present in MySQL even if the session
    company value is not available.
    """
    company_id = _company_id(current_user)
    selected_status = (status or "ACTIVE").strip().upper()

    # First try the logged-in company. If no rows are found, fall back to all
    # recipe rows so the screen always reflects what phpMyAdmin shows.
    company_rows = db.execute(
        text("SELECT COUNT(*) FROM recipes WHERE company_id = :company_id"),
        {"company_id": company_id},
    ).scalar() or 0

    scope_sql = "company_id = :company_id" if company_rows else "1 = 1"
    scope_params = {"company_id": company_id}

    stats_row = db.execute(
        text(f"""
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN UPPER(TRIM(COALESCE(status,''))) = 'ACTIVE' THEN 1 ELSE 0 END) AS active,
                SUM(CASE WHEN UPPER(TRIM(COALESCE(status,''))) = 'PENDING' THEN 1 ELSE 0 END) AS pending,
                SUM(CASE WHEN UPPER(TRIM(COALESCE(status,''))) = 'INACTIVE' THEN 1 ELSE 0 END) AS inactive
            FROM recipes
            WHERE {scope_sql}
        """),
        scope_params,
    ).mappings().first()

    stats = {
        "total": int(stats_row["total"] or 0) if stats_row else 0,
        "active": int(stats_row["active"] or 0) if stats_row else 0,
        "pending": int(stats_row["pending"] or 0) if stats_row else 0,
        "inactive": int(stats_row["inactive"] or 0) if stats_row else 0,
    }

    where_parts = [scope_sql]
    params: dict[str, object] = dict(scope_params)

    if selected_status and selected_status != "ALL":
        where_parts.append("UPPER(TRIM(COALESCE(status,''))) = :status")
        params["status"] = selected_status

    if category and category != "All Categories":
        where_parts.append("COALESCE(category,'') = :category")
        params["category"] = category

    if customer and customer != "All Customers":
        where_parts.append("COALESCE(customer_name,'') = :customer")
        params["customer"] = customer

    if search:
        where_parts.append("(recipe_code LIKE :search OR recipe_name LIKE :search OR COALESCE(customer_name,'') LIKE :search OR COALESCE(category,'') LIKE :search)")
        params["search"] = f"%{search}%"

    where_sql = " AND ".join(where_parts)

    rows = db.execute(
        text(f"""
            SELECT
                id,
                company_id,
                recipe_code,
                recipe_name,
                COALESCE(brand_name,'') AS brand_name,
                COALESCE(customer_name,'') AS customer_name,
                COALESCE(category,'') AS category,
                COALESCE(version,1) AS version,
                UPPER(TRIM(COALESCE(status,''))) AS status,
                COALESCE(is_active,0) AS is_active,
                COALESCE(approval_status,'') AS approval_status,
                COALESCE(is_sub_recipe,0) AS is_sub_recipe,
                COALESCE(standard_portions,0) AS standard_portions,
                COALESCE(weight_per_portion_g,0) AS weight_per_portion_g,
                COALESCE(food_cost,0) AS food_cost,
                COALESCE(food_cost_per_portion,0) AS food_cost_per_portion,
                COALESCE(total_cost,0) AS total_cost,
                COALESCE(total_cost_per_portion,0) AS total_cost_per_portion,
                COALESCE(sale_price,0) AS sale_price,
                COALESCE(sale_price_per_portion,0) AS sale_price_per_portion,
                COALESCE(missing_cost_lines,0) AS missing_cost_lines,
                created_at,
                updated_at
            FROM recipes
            WHERE {where_sql}
            ORDER BY recipe_code ASC, version DESC, id DESC
        """),
        params,
    ).mappings().all()

    recipes = [dict(row) for row in rows]

    # Category filter comes from recipe master category values. Customer filter is
    # linked to customer master, with recipe customer names added as a fallback
    # for old uploads where customer_name was stored as plain text.
    categories = [
        row["category"] for row in db.execute(
            text(f"""
                SELECT DISTINCT category
                FROM recipes
                WHERE {scope_sql}
                  AND category IS NOT NULL
                  AND TRIM(category) <> ''
                ORDER BY category
            """),
            scope_params,
        ).mappings().all()
    ]

    customer_rows = db.execute(
        text("""
            SELECT DISTINCT customer_name AS customer_name
            FROM customers
            WHERE company_id = :company_id
              AND customer_name IS NOT NULL
              AND TRIM(customer_name) <> ''
            UNION
            SELECT DISTINCT customer_name AS customer_name
            FROM recipes
            WHERE company_id = :company_id
              AND customer_name IS NOT NULL
              AND TRIM(customer_name) <> ''
            ORDER BY customer_name
        """),
        {"company_id": company_id},
    ).mappings().all()
    customers = [row["customer_name"] for row in customer_rows]

    return templates.TemplateResponse(
        "recipes/index.html",
        {
            "request": request,
            "recipes": recipes,
            "stats": stats,
            "categories": categories,
            "customers": customers,
            "search": search or "",
            "status": selected_status,
            "selected_category": category or "All Categories",
            "selected_customer": customer or "All Customers",
            "company_id": company_id,
            "company_scope_rows": company_rows,
        },
    )


@router.post("/upload-excel")
async def upload_recipe_excel(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    filename = file.filename or ""

    if not filename.lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(status_code=400, detail="Please upload a valid Excel .xlsx file.")

    suffix = os.path.splitext(filename)[1]

    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(await file.read())
        tmp_path = tmp.name

    try:
        result = import_recipe_excel(
            db=db,
            file_path=tmp_path,
            company_id=_company_id(current_user),
        )
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

    return RedirectResponse(
        url=f"/recipes?upload=success&created={result['created']}&updated={result['updated']}&lines={result['lines']}",
        status_code=303,
    )


@router.get("/prepare", response_class=HTMLResponse)
def prepare_recipe_form(
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    return templates.TemplateResponse(
        "recipes/form.html",
        _recipe_form_context(request, db, current_user, recipe=None, mode="create"),
    )


@router.post("/prepare")
async def save_manual_recipe(
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    form = await request.form()

    recipe = Recipe(
        company_id=_company_id(current_user),
        recipe_code=str(form.get("recipe_code") or "").strip(),
        recipe_name=str(form.get("recipe_name") or "").strip(),
        brand_name=str(form.get("brand_name") or "").strip(),
        customer_name=str(form.get("customer_name") or "").strip(),
        category=str(form.get("category") or "").strip(),
        version=1,
        status="ACTIVE",
        is_sub_recipe=str(form.get("is_sub_recipe") or "No").lower() == "yes",
        standard_portions=_d(form.get("standard_portions"), "1"),
        weight_per_portion_g=_d(form.get("weight_per_portion_g")),
        std_yield_pct=_d(form.get("std_yield_pct"), "0.95"),
        packaging_cost=_d(form.get("packaging_cost")),
        labor_cost=_d(form.get("labor_cost")),
        delivery_cost=_d(form.get("delivery_cost")),
        overheads=_d(form.get("overheads")),
        other_costs=_d(form.get("other_costs")),
        margin_pct=_d(form.get("margin_pct"), "0.30"),
        notes=str(form.get("notes") or "").strip(),
    )

    if not recipe.recipe_code or not recipe.recipe_name:
        raise HTTPException(status_code=400, detail="Recipe code and recipe name are required.")

    line_types = form.getlist("line_type[]")
    inventory_codes = form.getlist("inventory_code[]")
    item_names = form.getlist("item_name[]")
    uoms = form.getlist("uom[]")
    qty_batches = form.getlist("qty_batch[]")
    portions_list = form.getlist("line_portions[]")
    qty_per_portions = form.getlist("qty_per_portion[]")
    cost_uoms = form.getlist("cost_uom[]")
    remarks = form.getlist("remark[]")

    for index, line_type in enumerate(line_types):
        item_name = item_names[index].strip() if index < len(item_names) else ""

        if not item_name:
            continue

        recipe.lines.append(
            RecipeIngredient(
                line_no=index + 1,
                line_type=line_type or "Main Recipe",
                inventory_code=inventory_codes[index].strip() if index < len(inventory_codes) else None,
                item_name=item_name,
                uom=uoms[index].strip() if index < len(uoms) else None,
                qty_batch=_d(qty_batches[index]) if index < len(qty_batches) else Decimal("0"),
                portions=_d(portions_list[index], "1") if index < len(portions_list) else Decimal("1"),
                qty_per_portion=_d(qty_per_portions[index]) if index < len(qty_per_portions) else Decimal("0"),
                cost_uom=_d(cost_uoms[index]) if index < len(cost_uoms) else Decimal("0"),
                remark=remarks[index].strip() if index < len(remarks) else None,
            )
        )

    recalc_recipe(recipe)

    db.add(recipe)
    db.commit()
    db.refresh(recipe)

    return RedirectResponse(url=f"/recipes/{recipe.id}", status_code=303)

@router.get("/missing-data", response_class=HTMLResponse)
def missing_recipe_data(
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    lines = (
        db.query(RecipeIngredient)
        .join(Recipe)
        .filter(
            Recipe.company_id == _company_id(current_user),
            RecipeIngredient.missing_cost == True,
        )
        .order_by(Recipe.recipe_code, RecipeIngredient.line_no)
        .all()
    )

    return templates.TemplateResponse(
        "recipes/missing_data.html",
        {
            "request": request,
            "lines": lines,
        },
    )


@router.get("/pending", response_class=HTMLResponse)
def pending_recipes(
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    company_id = _company_id(current_user)
    recipes = (
        db.query(Recipe)
        .filter(
            Recipe.company_id == company_id,
            func.upper(func.trim(Recipe.status)) == "PENDING",
        )
        .order_by(Recipe.recipe_code.asc(), Recipe.version.desc(), Recipe.id.desc())
        .all()
    )
    stats_q = db.query(Recipe).filter(Recipe.company_id == company_id)
    stats = {
        "total": stats_q.count(),
        "active": stats_q.filter(func.upper(func.trim(Recipe.status)) == "ACTIVE").count(),
        "pending": stats_q.filter(func.upper(func.trim(Recipe.status)) == "PENDING").count(),
        "inactive": stats_q.filter(func.upper(func.trim(Recipe.status)) == "INACTIVE").count(),
    }
    return templates.TemplateResponse(
        "recipes/pending.html",
        {"request": request, "recipes": recipes, "stats": stats},
    )


@router.post("/approve-all-pending")
def approve_all_pending_recipes(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Approve the newest pending version for every recipe code.

    This repair is intentionally defensive. It normalizes status text, approves
    the latest pending record per recipe code, and supersedes all older versions.
    """
    company_id = _company_id(current_user)
    user_id = getattr(current_user, "id", None)
    now = datetime.utcnow()

    pending_codes = [
        row[0]
        for row in db.query(Recipe.recipe_code)
        .filter(
            Recipe.company_id == company_id,
            func.upper(func.trim(Recipe.status)) == "PENDING",
        )
        .distinct()
        .all()
    ]

    approved_count = 0
    for recipe_code in pending_codes:
        latest = (
            db.query(Recipe)
            .filter(
                Recipe.company_id == company_id,
                Recipe.recipe_code == recipe_code,
                func.upper(func.trim(Recipe.status)) == "PENDING",
            )
            .order_by(Recipe.version.desc(), Recipe.id.desc())
            .first()
        )
        if not latest:
            continue

        db.query(Recipe).filter(
            Recipe.company_id == company_id,
            Recipe.recipe_code == recipe_code,
            Recipe.id != latest.id,
        ).update(
            {
                "status": "INACTIVE",
                "is_active": False,
                "approval_status": "SUPERSEDED",
            },
            synchronize_session=False,
        )

        latest.status = "ACTIVE"
        latest.is_active = True
        latest.approval_status = "APPROVED"
        latest.approved_by = user_id
        latest.approved_at = now
        approved_count += 1

    db.commit()

    return RedirectResponse(
        url=f"/recipes?status=ACTIVE&toast=success&msg={approved_count}%20pending%20recipes%20approved",
        status_code=303,
    )

@router.post("/repair-active-status")
def repair_active_recipe_status(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Safety repair: approve latest pending versions when no active recipes exist.

    This handles the common beginner scenario where recipe master was uploaded,
    then ingredient upload created pending V2 versions, so the ACTIVE list becomes empty.
    """
    return approve_all_pending_recipes(db=db, current_user=current_user)


@router.post("/{recipe_id}/approve")
def approve_recipe_version(
    recipe_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    recipe = (
        db.query(Recipe)
        .filter(Recipe.id == recipe_id, Recipe.company_id == _company_id(current_user))
        .first()
    )
    if not recipe:
        raise HTTPException(status_code=404, detail="Recipe not found.")

    db.query(Recipe).filter(
        Recipe.company_id == recipe.company_id,
        Recipe.recipe_code == recipe.recipe_code,
        Recipe.id != recipe.id,
    ).update(
        {"status": "INACTIVE", "is_active": False, "approval_status": "SUPERSEDED"},
        synchronize_session=False,
    )

    recipe.status = "ACTIVE"
    recipe.is_active = True
    recipe.approval_status = "APPROVED"
    recipe.approved_by = getattr(current_user, "id", None)
    recipe.approved_at = datetime.utcnow()
    db.add(recipe)
    db.commit()
    db.expire_all()
    return RedirectResponse(url="/recipes?status=ACTIVE&toast=success&msg=Recipe version approved", status_code=303)


@router.post("/{recipe_id}/reject")
def reject_recipe_version(
    recipe_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    recipe = (
        db.query(Recipe)
        .filter(Recipe.id == recipe_id, Recipe.company_id == _company_id(current_user))
        .first()
    )
    if not recipe:
        raise HTTPException(status_code=404, detail="Recipe not found.")
    recipe.status = "INACTIVE"
    recipe.is_active = False
    recipe.approval_status = "REJECTED"
    db.commit()
    return RedirectResponse(url="/recipes/pending?toast=warning&msg=Recipe version rejected", status_code=303)



@router.get("/ingredients", response_class=HTMLResponse)
def recipe_ingredients_master(
    request: Request,
    search: str | None = None,
    status: str | None = "ACTIVE",
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Recipe Ingredient / BOM master list."""
    company_id = _company_id(current_user)
    selected_status = (status or "ACTIVE").strip().upper()

    row_count = db.execute(
        text("SELECT COUNT(*) FROM recipes WHERE company_id = :company_id"),
        {"company_id": company_id},
    ).scalar() or 0
    company_filter_sql = "r.company_id = :company_id" if row_count else "1 = 1"

    where_parts = [company_filter_sql]
    params: dict[str, object] = {"company_id": company_id}

    if selected_status and selected_status != "ALL":
        where_parts.append("UPPER(TRIM(COALESCE(r.status,''))) = :status")
        params["status"] = selected_status

    if search:
        where_parts.append("(r.recipe_code LIKE :search OR r.recipe_name LIKE :search OR ri.inventory_code LIKE :search OR ri.item_name LIKE :search)")
        params["search"] = f"%{search}%"

    where_sql = " AND ".join(where_parts)

    lines = db.execute(
        text(f"""
            SELECT
                r.id AS recipe_id,
                r.recipe_code,
                r.recipe_name,
                UPPER(TRIM(COALESCE(r.status,''))) AS recipe_status,
                r.version,
                ri.id AS line_id,
                ri.line_no,
                ri.line_type,
                ri.inventory_code,
                ri.item_name,
                ri.uom,
                ri.qty_batch,
                ri.qty_per_portion,
                ri.cost_uom,
                ri.line_cost,
                ri.missing_cost
            FROM recipe_ingredients ri
            JOIN recipes r ON r.id = ri.recipe_id
            WHERE {where_sql}
            ORDER BY r.recipe_code ASC, ri.line_no ASC, ri.id ASC
        """),
        params,
    ).mappings().all()

    stats_row = db.execute(
        text(f"""
            SELECT
                COUNT(*) AS total_lines,
                COUNT(DISTINCT r.id) AS total_recipes,
                SUM(CASE WHEN ri.missing_cost = 1 THEN 1 ELSE 0 END) AS missing_lines
            FROM recipe_ingredients ri
            JOIN recipes r ON r.id = ri.recipe_id
            WHERE {company_filter_sql}
        """),
        {"company_id": company_id},
    ).mappings().first()

    stats = {
        "total_lines": int(stats_row["total_lines"] or 0) if stats_row else 0,
        "total_recipes": int(stats_row["total_recipes"] or 0) if stats_row else 0,
        "missing_lines": int(stats_row["missing_lines"] or 0) if stats_row else 0,
    }

    return templates.TemplateResponse(
        "recipes/ingredients.html",
        {
            "request": request,
            "lines": lines,
            "stats": stats,
            "search": search or "",
            "status": selected_status,
        },
    )

@router.get("/{recipe_id}", response_class=HTMLResponse)
def view_recipe(
    recipe_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    recipe = (
        db.query(Recipe)
        .options(selectinload(Recipe.lines))
        .filter(
            Recipe.id == recipe_id,
            Recipe.company_id == _company_id(current_user),
        )
        .first()
    )

    if not recipe:
        raise HTTPException(status_code=404, detail="Recipe not found.")

    return templates.TemplateResponse(
        "recipes/view.html",
        {
            "request": request,
            "recipe": recipe,
        },
    )


@router.get("/{recipe_id}/edit", response_class=HTMLResponse)
def edit_recipe_form(
    recipe_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    recipe = (
        db.query(Recipe)
        .options(selectinload(Recipe.lines))
        .filter(
            Recipe.id == recipe_id,
            Recipe.company_id == _company_id(current_user),
        )
        .first()
    )

    if not recipe:
        raise HTTPException(status_code=404, detail="Recipe not found.")

    return templates.TemplateResponse(
        "recipes/form.html",
        _recipe_form_context(request, db, current_user, recipe=recipe, mode="edit"),
    )

@router.post("/{recipe_id}/edit")
async def update_recipe(
    recipe_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    form = await request.form()

    recipe = (
        db.query(Recipe)
        .options(selectinload(Recipe.lines))
        .filter(
            Recipe.id == recipe_id,
            Recipe.company_id == _company_id(current_user),
        )
        .first()
    )

    if not recipe:
        raise HTTPException(status_code=404, detail="Recipe not found.")

    recipe.recipe_code = str(form.get("recipe_code") or "").strip()
    recipe.recipe_name = str(form.get("recipe_name") or "").strip()
    recipe.brand_name = str(form.get("brand_name") or "").strip()
    recipe.customer_name = str(form.get("customer_name") or "").strip()
    recipe.category = str(form.get("category") or "").strip()
    recipe.is_sub_recipe = str(form.get("is_sub_recipe") or "No").lower() == "yes"
    recipe.standard_portions = _d(form.get("standard_portions"), "1")
    recipe.weight_per_portion_g = _d(form.get("weight_per_portion_g"))
    recipe.std_yield_pct = _d(form.get("std_yield_pct"), "0.95")
    recipe.packaging_cost = _d(form.get("packaging_cost"))
    recipe.labor_cost = _d(form.get("labor_cost"))
    recipe.delivery_cost = _d(form.get("delivery_cost"))
    recipe.overheads = _d(form.get("overheads"))
    recipe.other_costs = _d(form.get("other_costs"))
    recipe.margin_pct = _d(form.get("margin_pct"), "0.30")
    recipe.notes = str(form.get("notes") or "").strip()

    if not recipe.recipe_code or not recipe.recipe_name:
        raise HTTPException(status_code=400, detail="Recipe code and recipe name are required.")

    # Replace old lines with new lines from form.
    recipe.lines.clear()
    db.flush()

    line_types = form.getlist("line_type[]")
    inventory_codes = form.getlist("inventory_code[]")
    item_names = form.getlist("item_name[]")
    uoms = form.getlist("uom[]")
    qty_batches = form.getlist("qty_batch[]")
    portions_list = form.getlist("line_portions[]")
    qty_per_portions = form.getlist("qty_per_portion[]")
    cost_uoms = form.getlist("cost_uom[]")
    remarks = form.getlist("remark[]")

    line_no = 1

    for index, line_type in enumerate(line_types):
        item_name = item_names[index].strip() if index < len(item_names) else ""

        if not item_name:
            continue

        recipe.lines.append(
            RecipeIngredient(
                line_no=line_no,
                line_type=line_type or "Main Recipe",
                inventory_code=inventory_codes[index].strip() if index < len(inventory_codes) else None,
                item_name=item_name,
                uom=uoms[index].strip() if index < len(uoms) else None,
                qty_batch=_d(qty_batches[index]) if index < len(qty_batches) else Decimal("0"),
                portions=_d(portions_list[index], "1") if index < len(portions_list) else Decimal("1"),
                qty_per_portion=_d(qty_per_portions[index]) if index < len(qty_per_portions) else Decimal("0"),
                cost_uom=_d(cost_uoms[index]) if index < len(cost_uoms) else Decimal("0"),
                remark=remarks[index].strip() if index < len(remarks) else None,
            )
        )

        line_no += 1

    recalc_recipe(recipe)

    db.commit()
    db.refresh(recipe)

    return RedirectResponse(url=f"/recipes/{recipe.id}", status_code=303)

@router.post("/{recipe_id}/deactivate")
def deactivate_recipe(
    recipe_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    recipe = (
        db.query(Recipe)
        .filter(
            Recipe.id == recipe_id,
            Recipe.company_id == _company_id(current_user),
        )
        .first()
    )

    if not recipe:
        raise HTTPException(status_code=404, detail="Recipe not found.")

    recipe.status = "INACTIVE"
    db.commit()

    return RedirectResponse(url="/recipes", status_code=303)


