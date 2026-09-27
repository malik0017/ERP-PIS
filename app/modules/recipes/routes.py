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
from app.core.rbac import require_area, require_action
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
    require_area(request, "recipe_list")
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

    where_parts = [scope_sql]
    params: dict[str, object] = dict(scope_params)

    # Batch 81 fix: the 4 KPI cards (Total/Active/Pending/Inactive) used to
    # always reflect the FULL unfiltered recipe set, even while Category,
    # Customer, or Search were actively narrowing the table below them —
    # so the cards and the table told two different stories. They now
    # share the same Category/Customer/Search filters as the table. They
    # deliberately do NOT also apply the Status filter itself, since their
    # whole purpose is to show the Active/Pending/Inactive breakdown WITHIN
    # whatever's currently filtered — filtering them by status too would
    # collapse 3 of the 4 cards to zero.
    stats_where_parts = [scope_sql]
    stats_params: dict[str, object] = dict(scope_params)

    if category and category != "All Categories":
        stats_where_parts.append("COALESCE(category,'') = :category")
        stats_params["category"] = category
    if customer and customer != "All Customers":
        stats_where_parts.append("COALESCE(customer_name,'') = :customer")
        stats_params["customer"] = customer
    if search:
        stats_where_parts.append("(recipe_code LIKE :search OR recipe_name LIKE :search OR COALESCE(customer_name,'') LIKE :search OR COALESCE(category,'') LIKE :search)")
        stats_params["search"] = f"%{search}%"
    stats_where_sql = " AND ".join(stats_where_parts)

    stats_row = db.execute(
        text(f"""
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN UPPER(TRIM(COALESCE(status,''))) = 'ACTIVE' THEN 1 ELSE 0 END) AS active,
                SUM(CASE WHEN UPPER(TRIM(COALESCE(status,''))) = 'PENDING' THEN 1 ELSE 0 END) AS pending,
                SUM(CASE WHEN UPPER(TRIM(COALESCE(status,''))) = 'INACTIVE' THEN 1 ELSE 0 END) AS inactive
            FROM recipes
            WHERE {stats_where_sql}
        """),
        stats_params,
    ).mappings().first()

    stats = {
        "total": int(stats_row["total"] or 0) if stats_row else 0,
        "active": int(stats_row["active"] or 0) if stats_row else 0,
        "pending": int(stats_row["pending"] or 0) if stats_row else 0,
        "inactive": int(stats_row["inactive"] or 0) if stats_row else 0,
    }
    filters_active = bool(category and category != "All Categories") or bool(customer and customer != "All Customers") or bool(search)

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
    # Batch 247: FOOD COST / PORTION column. recipes imported without a recalc
    # carry food_cost but food_cost_per_portion = 0, so fall back to
    # batch cost ÷ standard portions — the same rule as order_costing.FCPP_SQL.
    for r in recipes:
        _portions = float(r.get("standard_portions") or 0)
        if not float(r.get("food_cost_per_portion") or 0) and _portions > 0:
            r["food_cost_per_portion"] = float(r.get("food_cost") or 0) / _portions
        if not float(r.get("sale_price_per_portion") or 0) and _portions > 0:
            r["sale_price_per_portion"] = float(r.get("sale_price") or 0) / _portions

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
            "filters_active": filters_active,
        },
    )


@router.post("/upload-excel")
async def upload_recipe_excel(
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    require_action(request, "recipe_list", "add")
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

    # Batch 176 — 176-C UI surfacing. import_recipe_excel() now flags a UOM
    # whose unit family (Mass/Volume/Count) disagrees with its ingredient
    # master's — the exact bug behind "why is chicken in Ml" (Section
    # Report). Only the count crosses the redirect; the full list (recipe,
    # ingredient, row UOM, expected family) is in result["uom_mismatches"]
    # if a dedicated review screen is wanted later — this is deliberately
    # the minimal surfacing (a count + a warning banner), not a new page.
    mismatch_count = len(result.get("uom_mismatches") or [])
    return RedirectResponse(
        url=(f"/recipes?upload=success&created={result['created']}&updated={result['updated']}"
             f"&lines={result['lines']}&uom_mismatches={mismatch_count}"),
        status_code=303,
    )


@router.get("/prepare", response_class=HTMLResponse)
def prepare_recipe_form(
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    require_area(request, "recipe_prepare")
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
    require_action(request, "recipe_prepare", "add")
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
    require_area(request, "recipe_missing")
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
    require_area(request, "recipe_approvals")
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
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Approve the newest pending version for every recipe code.

    This repair is intentionally defensive. It normalizes status text, approves
    the latest pending record per recipe code, and supersedes all older versions.
    """
    require_action(request, "recipe_approvals", "edit")
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
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Safety repair: approve latest pending versions when no active recipes exist.

    This handles the common beginner scenario where recipe master was uploaded,
    then ingredient upload created pending V2 versions, so the ACTIVE list becomes empty.
    """
    require_action(request, "recipe_approvals", "edit")
    return approve_all_pending_recipes(db=db, current_user=current_user)


@router.post("/{recipe_id}/approve")
def approve_recipe_version(
    recipe_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    require_action(request, "recipe_approvals", "edit")
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
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    require_action(request, "recipe_approvals", "edit")
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
    require_area(request, "recipe_list")
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
    require_area(request, "recipe_list")
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
    require_area(request, "recipe_list")
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
    require_action(request, "recipe_list", "edit")
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
    # Batch 128: persist the Menu Day (weekly-menu customers).
    recipe.day_of_week = str(form.get("day_of_week") or "").strip() or None
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
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    require_action(request, "recipe_list", "edit")
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




# ============================================================================
# Batch 17 — Activate recipe + Excel / PDF downloads (view.html buttons 404'd)
# ============================================================================
from fastapi.responses import StreamingResponse
import io


@router.post("/{recipe_id}/activate")
def activate_recipe(
    recipe_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Mirror of deactivate: bring an INACTIVE recipe back into use."""
    require_action(request, "recipe_list", "edit")
    recipe = (
        db.query(Recipe)
        .filter(Recipe.id == recipe_id, Recipe.company_id == _company_id(current_user))
        .first()
    )
    if not recipe:
        raise HTTPException(status_code=404, detail="Recipe not found.")
    recipe.status = "ACTIVE"
    recipe.is_active = True
    db.commit()
    return RedirectResponse(url=f"/recipes/{recipe_id}", status_code=303)


def _recipe_for_export(recipe_id: int, db: Session, current_user):
    recipe = (
        db.query(Recipe)
        .options(selectinload(Recipe.lines))
        .filter(Recipe.id == recipe_id, Recipe.company_id == _company_id(current_user))
        .first()
    )
    if not recipe:
        raise HTTPException(status_code=404, detail="Recipe not found.")
    return recipe


@router.get("/{recipe_id}/download-excel")
def download_recipe_excel(
    recipe_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    require_area(request, "recipe_list")
    recipe = _recipe_for_export(recipe_id, db, current_user)
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment

    wb = Workbook()
    ws = wb.active
    ws.title = "Recipe"
    head_fill = PatternFill("solid", fgColor="102542")
    head_font = Font(color="FFFFFF", bold=True)

    ws.append([f"Recipe {recipe.recipe_code} — {recipe.recipe_name}"])
    ws["A1"].font = Font(bold=True, size=14)
    ws.append([])
    for label, value in [
        ("Customer", recipe.customer_name or ""), ("Category", recipe.category or ""),
        ("Version", recipe.version or 1), ("Status", recipe.status or ""),
        ("Portions", recipe.standard_portions or 1), ("Wt/Portion (g)", recipe.weight_per_portion_g or 0),
        ("Food Cost", recipe.food_cost or 0),
        # Batch 247: per-portion figures, same fallback as the recipe list.
        ("Food Cost / Portion", float(recipe.food_cost_per_portion or 0)
            or float(recipe.food_cost or 0) / (float(recipe.standard_portions or 0) or 1)),
        ("Total Cost", recipe.total_cost or 0),
        ("Sale Price", recipe.sale_price or 0),
        ("Sale Price / Portion", float(recipe.sale_price_per_portion or 0)
            or float(recipe.sale_price or 0) / (float(recipe.standard_portions or 0) or 1)),
    ]:
        ws.append([label, value])
        ws.cell(row=ws.max_row, column=1).font = Font(bold=True)
    ws.append([])

    header = ["#", "Type", "Item Code", "Item Name", "UOM", "Qty/Batch", "Portions", "Qty/Portion", "Cost/UOM", "Line Cost", "Remark"]
    ws.append(header)
    for c in range(1, len(header) + 1):
        cell = ws.cell(row=ws.max_row, column=c)
        cell.fill, cell.font = head_fill, head_font
        cell.alignment = Alignment(horizontal="center")
    for i, l in enumerate(recipe.lines or [], start=1):
        ws.append([i, l.line_type or "Main Recipe", l.inventory_code or "", l.item_name or "",
                   l.uom or "", float(l.qty_batch or 0), float(l.portions or 0),
                   float(l.qty_per_portion or 0), float(l.cost_uom or 0),
                   float(l.line_cost or 0), l.remark or ""])
    widths = [5, 14, 14, 40, 8, 12, 10, 12, 12, 12, 24]
    for c, w in enumerate(widths, start=1):
        ws.column_dimensions[ws.cell(row=1, column=c).column_letter].width = w

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{recipe.recipe_code}_v{recipe.version or 1}.xlsx"'},
    )


@router.get("/{recipe_id}/download-pdf")
def download_recipe_pdf(
    recipe_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    require_area(request, "recipe_list")
    recipe = _recipe_for_export(recipe_id, db, current_user)
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib import colors
        from reportlab.lib.units import mm
        from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
        from reportlab.lib.styles import getSampleStyleSheet
    except Exception:
        # reportlab not installed -> print-ready HTML (browser: Ctrl+P -> Save as PDF)
        return templates.TemplateResponse("recipes/print.html", {"request": request, "recipe": recipe})

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=14 * mm, rightMargin=14 * mm,
                            topMargin=14 * mm, bottomMargin=14 * mm)
    styles = getSampleStyleSheet()
    # Batch 223: recipe names and ingredients are frequently Arabic in this
    # business, and the default styles are Helvetica — no Arabic glyphs, so they
    # printed as boxes. Font chosen from the UI language and the content; every
    # string drawn is shaped and bidi-reordered (app/core/pdf_arabic.py).
    from reportlab.lib.enums import TA_LEFT, TA_RIGHT
    from reportlab.lib.styles import ParagraphStyle

    from app.core.pdf_arabic import font_names, is_rtl, shape as _ar
    _lang = request.session.get("lang") or "en"
    _sample = " ".join(str(x or "") for x in
                       [recipe.recipe_name, recipe.customer_name] +
                       [l.item_name for l in (recipe.lines or [])[:40]])
    _REG, _BOLD = font_names(_lang, _sample)
    _align = TA_RIGHT if is_rtl(_lang) else TA_LEFT
    _title = ParagraphStyle("t", parent=styles["Title"], fontName=_BOLD, alignment=_align)
    _norm = ParagraphStyle("n", parent=styles["Normal"], fontName=_REG, alignment=_align)
    _cell = ParagraphStyle("c", parent=styles["Normal"], fontName=_REG, fontSize=7.5,
                           leading=9.5, alignment=_align)
    # ------------------------------------------------------------------
    # Batch 247 — HEADER ROW TEXT WAS INVISIBLE (navy on navy).
    #
    # ROOT CAUSE: the header cells are Paragraphs (so Arabic headers can be
    # shaped), and a Paragraph draws in ITS OWN style's textColor — black by
    # default. The table's ("TEXTCOLOR", row 0, white) rule only reaches plain
    # string cells, so it was silently ignored for every header cell and the
    # black text sat on the #102542 band. The header now has its own
    # paragraph style: white, bold, centred.
    #
    # Also in this pass: the column widths summed to 189 mm on a 182 mm frame
    # (A4 minus 2 × 14 mm), so the table ran past the right margin; the cost
    # line printed raw 4-dp floats; and per-portion cost was not on the sheet
    # at all. Replaced by a summary block and a totals row.
    # ------------------------------------------------------------------
    from reportlab.lib.enums import TA_CENTER
    _hdr = ParagraphStyle("h", parent=_cell, fontName=_BOLD, textColor=colors.white,
                          alignment=TA_CENTER, fontSize=7.5, leading=9)
    _k = ParagraphStyle("k", parent=_cell, fontName=_BOLD, fontSize=6.8, leading=8,
                        textColor=colors.HexColor("#5a6a82"))
    _v = ParagraphStyle("v", parent=_cell, fontName=_BOLD, fontSize=9.5, leading=11.5,
                        textColor=colors.HexColor("#102542"))

    portions = float(recipe.standard_portions or 0) or 1.0
    food = float(recipe.food_cost or 0)
    fcpp = float(recipe.food_cost_per_portion or 0) or food / portions
    total_cost = float(recipe.total_cost or 0)
    sale = float(recipe.sale_price or 0)
    sppp = float(recipe.sale_price_per_portion or 0) or sale / portions
    fc_pct = (food / sale * 100) if sale else 0.0

    def _kv(k, v):
        return [Paragraph(_ar(k), _k), Paragraph(_ar(str(v)), _v)]

    facts = [
        _kv("CUSTOMER", recipe.customer_name or "-") + _kv("CATEGORY", recipe.category or "-")
        + _kv("VERSION / STATUS", f"V{recipe.version or 1} · {recipe.status or '-'}")
        + _kv("PORTIONS", f"{portions:g}"),
        _kv("FOOD COST (BATCH)", f"{food:,.2f}") + _kv("FOOD COST / PORTION", f"{fcpp:,.4f}")
        + _kv("TOTAL COST", f"{total_cost:,.2f}") + _kv("SALE PRICE", f"{sale:,.2f}"),
        _kv("SALE PRICE / PORTION", f"{sppp:,.4f}") + _kv("FOOD COST %", f"{fc_pct:.1f}%")
        + _kv("MISSING COST LINES", int(recipe.missing_cost_lines or 0))
        + _kv("PRINTED", datetime.now().strftime("%Y-%m-%d %H:%M")),
    ]
    # Label/value pairs are laid out as 8 columns (4 facts per row).
    fact_tbl = Table(facts, colWidths=[20*mm, 25.5*mm] * 4)
    fact_tbl.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor("#c9d7e4")),
        ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#e3ebf5")),
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f6f9fd")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))

    story = [
        Paragraph(_ar(f"Recipe {recipe.recipe_code} — {recipe.recipe_name}"), _title),
        fact_tbl,
        Spacer(1, 8),
    ]
    # Fixed English labels, split on purpose so the narrow numeric columns
    # break between words ("Qty /" "Portion") instead of mid-word.
    data = [[Paragraph(h, _hdr) for h in
             ["#", "Item Code", "Item Name", "UOM", "Qty /<br/>Batch", "Qty /<br/>Portion",
              "Cost /<br/>UOM", "Line<br/>Cost", "Cost /<br/>Portion"]]]
    lines = list(recipe.lines or [])
    sum_line = sum_pp = 0.0
    for i, l in enumerate(lines, start=1):
        # Item names wrap as Paragraphs so Arabic can be shaped; the numeric
        # columns stay plain strings — nothing to shape and cheaper to draw.
        lc = float(l.line_cost or 0)
        lpp = float(getattr(l, "line_cost_per_portion", 0) or 0) or \
            float(l.qty_per_portion or 0) * float(l.cost_uom or 0)
        sum_line += lc
        sum_pp += lpp
        data.append([i, l.inventory_code or "",
                     Paragraph(_ar((l.item_name or "")[:70]), _cell), l.uom or "",
                     f"{float(l.qty_batch or 0):,.3f}", f"{float(l.qty_per_portion or 0):,.3f}",
                     f"{float(l.cost_uom or 0):.4f}", f"{lc:,.4f}", f"{lpp:,.4f}"])
    data.append(["", "", Paragraph(_ar("TOTAL"), _hdr), "", "", "", "",
                 f"{sum_line:,.4f}", f"{sum_pp:,.4f}"])
    # 8 + 21 + 55 + 11 + 17 + 17 + 17 + 18 + 18 = 182 mm = the frame width.
    tbl = Table(data, repeatRows=1,
                colWidths=[8*mm, 21*mm, 55*mm, 11*mm, 17*mm, 17*mm, 17*mm, 18*mm, 18*mm])
    last = len(data) - 1
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#102542")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("FONTSIZE", (0, 0), (-1, -1), 7.5),
        # Batch 223: the plain (non-Paragraph) cells need the font naming too,
        # or they silently fall back to Helvetica mid-table.
        ("FONTNAME", (0, 0), (-1, -1), _REG),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#c9d7e4")),
        ("ROWBACKGROUNDS", (0, 1), (-1, last - 1), [colors.white, colors.HexColor("#f4f8fc")]),
        ("ALIGN", (4, 1), (-1, -1), "RIGHT"),
        ("ALIGN", (0, 1), (0, -1), "CENTER"),
        # totals row — same navy band as the header, white bold figures
        ("BACKGROUND", (0, last), (-1, last), colors.HexColor("#1e3a5f")),
        ("TEXTCOLOR", (0, last), (-1, last), colors.white),
        ("FONTNAME", (0, last), (-1, last), _BOLD),
    ]))
    story.append(tbl)

    def _footer(canvas, _doc):
        canvas.saveState()
        canvas.setFont(_REG, 7)
        canvas.setFillColor(colors.HexColor("#8a97a8"))
        canvas.drawString(14 * mm, 8 * mm, f"{recipe.recipe_code} · V{recipe.version or 1}")
        canvas.drawRightString(A4[0] - 14 * mm, 8 * mm, f"Page {_doc.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    buf.seek(0)
    return StreamingResponse(buf, media_type="application/pdf",
                             headers={"Content-Disposition": f'attachment; filename="{recipe.recipe_code}_v{recipe.version or 1}.pdf"'})
