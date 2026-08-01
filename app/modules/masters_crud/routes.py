# app/modules/masters_crud/routes.py
# =============================================================================
# Batch 22 — MANUAL MASTER-DATA ENTRY (Add / Edit) for every master type
# -----------------------------------------------------------------------------
# Until now master data could only be loaded via the Excel upload flow. This
# module adds a SAP-B1-style "+ New ..." button on every master list that opens
# an inline form (hidden by default) so users can add a single Customer,
# Supplier, Chef, Brand, Kitchen Section, Revenue Stream or Inventory item by
# hand, and edit any existing row.
#
# DESIGN
#   * CONFIG-DRIVEN: one route set + one template serves ALL master types. To
#     add a new master type you add ONE entry to MASTER_FORMS below — no new
#     routes, no new templates.
#   * Multi-company: every INSERT stamps company_id from the session (scope()).
#   * RBAC: gated by require_area/require_action("master_data", ...). Admins pass
#     automatically via the existing rbac layer.
#   * Fail-safe: auto-codes (CUST-0001 ...) are generated when the user leaves
#     the code blank, so a row is never rejected for a missing code.
#
# URLS
#   GET  /masters-crud/{mtype}/new         -> blank form (inline card)
#   POST /masters-crud/{mtype}/new         -> insert, redirect back to list
#   GET  /masters-crud/{mtype}/{row_id}/edit -> pre-filled form
#   POST /masters-crud/{mtype}/{row_id}/edit -> update, redirect back to list
# =============================================================================

from datetime import datetime

from fastapi import APIRouter, Depends, Request, Form, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.templates import render
from app.core.rbac import require_area, require_action
from app.database import get_db

router = APIRouter(tags=["Masters (Manual Entry)"])


# ---------------------------------------------------------------------------
# Session helpers (multi-company scope / stamp)
# ---------------------------------------------------------------------------
def _cid(request: Request) -> int:
    try:
        return int(request.session.get("company_id") or 1)
    except Exception:
        return 1


def _user(request: Request) -> str:
    return request.session.get("username") or "system"


# ---------------------------------------------------------------------------
# FORM DEFINITIONS — the single source of truth for every master type.
#   table    : DB table
#   title    : human label (singular)
#   list_url : where to redirect after save
#   code_col : the "code" column (auto-generated if left blank)
#   code_pfx : prefix used when auto-generating a code
#   name_col : the primary name column (required)
#   fields   : ordered list of (column, label, type, required)
#              type in {text, textarea, number, email, select:OPT|OPT|OPT}
# ---------------------------------------------------------------------------
MASTER_FORMS: dict[str, dict] = {
    "customers": {
        "table": "customers", "title": "Customer", "list_url": "/customers",
        "code_col": "customer_code", "code_pfx": "CUST", "name_col": "customer_name",
        "fields": [
            ("customer_code", "Customer Code", "text", False),
            ("customer_name", "Customer Name", "text", True),
            ("customer_name_ar", "Name (Arabic)", "text", False),
            ("customer_type", "Customer Type", "text", False),
            ("brand", "Brand", "text", False),
            ("sales_man", "Salesman", "text", False),
            ("contact_person", "Contact Person", "text", False),
            ("phone", "Phone", "text", False),
            ("email", "Email", "email", False),
            ("city", "City", "text", False),
            ("vat_number", "VAT Number", "text", False),
            ("payment_terms", "Payment Terms", "text", False),
            ("status", "Status", "select:ACTIVE|INACTIVE", False),
        ],
    },
    "suppliers": {
        "table": "suppliers", "title": "Supplier", "list_url": "/suppliers",
        "code_col": "supplier_code", "code_pfx": "SUPP", "name_col": "supplier_name",
        "fields": [
            ("supplier_code", "Supplier Code", "text", False),
            ("supplier_name", "Supplier Name", "text", True),
            ("supplier_name_ar", "Name (Arabic)", "text", False),
            ("category", "Category", "text", False),
            ("supplier_type", "Supplier Type", "text", False),
            ("phone", "Phone", "text", False),
            ("email", "Email", "email", False),
            ("city", "City", "text", False),
            ("country", "Country", "text", False),
            ("vat_number", "VAT Number", "text", False),
            ("payment_terms", "Payment Terms", "text", False),
            ("status", "Status", "select:ACTIVE|INACTIVE", False),
        ],
    },
    "chefs": {
        "table": "chefs", "title": "Chef", "list_url": "/chefs",
        "code_col": "chef_code", "code_pfx": "CHEF", "name_col": "chef_name",
        "fields": [
            ("chef_code", "Chef Code", "text", False),
            ("chef_name", "Chef Name", "text", True),
            ("job_title", "Job Title", "text", False),
            ("kitchen_section", "Kitchen Section", "text", False),
            ("brand_assign", "Brand Assigned", "text", False),
            ("tasks", "Tasks", "text", False),
            ("remarks", "Remarks", "textarea", False),
            ("status", "Status", "select:ACTIVE|INACTIVE", False),
        ],
    },
    "brands": {
        "table": "brands", "title": "Brand", "list_url": "/brands",
        "code_col": "brand_code", "code_pfx": "BRND", "name_col": "brand_name_en",
        "fields": [
            ("brand_code", "Brand Code", "text", False),
            ("brand_name_en", "Brand Name", "text", True),
            ("brand_name_ar", "Name (Arabic)", "text", False),
            ("short_code", "Short Code", "text", False),
            ("revenue_stream_name", "Revenue Stream", "text", False),
            ("default_kitchen_code", "Default Kitchen Code", "text", False),
            ("remarks", "Remarks", "textarea", False),
            ("status", "Status", "select:ACTIVE|INACTIVE", False),
        ],
    },
    "revenue_streams": {
        "table": "revenue_streams", "title": "Revenue Stream", "list_url": "/revenue-streams",
        "code_col": "stream_code", "code_pfx": "REV", "name_col": "stream_name",
        "fields": [
            ("stream_code", "Stream Code", "text", False),
            ("stream_name", "Stream Name", "text", True),
            ("revenue_category", "Category", "text", False),
            ("description", "Description", "textarea", False),
            ("status", "Status", "select:ACTIVE|INACTIVE", False),
        ],
    },
    "kitchen_sections": {
        "table": "kitchen_sections", "title": "Kitchen Section", "list_url": "/kitchen-sections",
        "code_col": "section_code", "code_pfx": "KS", "name_col": "section_name",
        "fields": [
            ("section_code", "Section Code", "text", False),
            ("section_name", "Section Name", "text", True),
            ("section_name_ar", "Name (Arabic)", "text", False),
            ("kitchen_code", "Kitchen Code", "text", False),
            ("sequence_no", "Sequence No", "number", False),
            ("remarks", "Remarks", "textarea", False),
            ("status", "Status", "select:ACTIVE|INACTIVE", False),
        ],
    },
    "kitchen_locations": {
        "table": "kitchen_locations", "title": "Kitchen Location", "list_url": "/kitchen-locations",
        "code_col": "kitchen_code", "code_pfx": "KL", "name_col": "kitchen_name",
        "fields": [
            ("kitchen_code", "Kitchen Code", "text", False),
            ("kitchen_name", "Kitchen Name", "text", True),
            ("kitchen_type", "Kitchen Type", "text", False),
            ("location", "Location", "text", False),
            ("city", "City", "text", False),
            ("manager", "Manager", "text", False),
            ("capacity", "Capacity", "text", False),
            ("status", "Status", "select:ACTIVE|INACTIVE", False),
        ],
    },
    "inventory": {
        # NOTE: real table is `ingredients` (ingredient_code / name / ...).
        "table": "ingredients", "title": "Inventory Item", "list_url": "/inventory",
        "code_col": "ingredient_code", "code_pfx": "ITEM", "name_col": "name",
        "fields": [
            ("ingredient_code", "Inventory Code", "text", False),
            ("name", "Item Name", "text", True),
            ("main_category", "Main Category", "text", False),
            ("sub_category", "Sub Category", "text", False),
            ("purchase_uom", "Purchase UoM", "text", False),
            ("recipe_uom", "Recipe UoM", "text", False),
            ("unit_cost_standard", "Unit Cost", "number", False),
            ("reorder_level_standard", "Reorder Level", "number", False),
            ("default_supplier", "Default Supplier", "text", False),
            ("status", "Status", "select:Active|Inactive", False),
        ],
    },
}


def _cfg(mtype: str) -> dict:
    cfg = MASTER_FORMS.get(mtype)
    if not cfg:
        raise HTTPException(status_code=404, detail=f"Unknown master type '{mtype}'")
    return cfg


def _table_columns(db: Session, table: str) -> set[str]:
    """Return the real column set so we never INSERT a column the DB lacks."""
    try:
        rows = db.execute(text(f"SHOW COLUMNS FROM {table}")).mappings().all()
        return {r["Field"] for r in rows}
    except Exception:
        return set()


def _auto_code(db: Session, cfg: dict) -> str:
    prefix = cfg["code_pfx"]
    try:
        n = db.execute(text(f"SELECT COUNT(*) FROM {cfg['table']}")).scalar() or 0
    except Exception:
        n = 0
    return f"{prefix}-{int(n) + 1:04d}"


# ---------------------------------------------------------------------------
# NEW — blank form
# ---------------------------------------------------------------------------
@router.get("/masters-crud/{mtype}/new")
def new_master_form(mtype: str, request: Request, db: Session = Depends(get_db)):
    require_area(request, "master_data")
    cfg = _cfg(mtype)
    return render(request, "masters_crud/form.html", {
        "mtype": mtype, "cfg": cfg, "row": {}, "mode": "new",
        "page_title": f"New {cfg['title']}",
    })


@router.post("/masters-crud/{mtype}/new")
async def create_master(mtype: str, request: Request, db: Session = Depends(get_db)):
    require_action(request, "master_data", "add")
    cfg = _cfg(mtype)
    form = await request.form()

    cols_available = _table_columns(db, cfg["table"])
    data: dict[str, object] = {}
    for col, label, ftype, required in cfg["fields"]:
        val = (form.get(col) or "").strip()
        if required and not val:
            return RedirectResponse(
                f"/masters-crud/{mtype}/new?error={label} is required", status_code=303)
        if ftype == "number":
            data[col] = float(val) if val else 0
        else:
            data[col] = val or None

    # auto-code if blank
    code_col = cfg["code_col"]
    if not data.get(code_col):
        data[code_col] = _auto_code(db, cfg)

    # default status ACTIVE
    if "status" in cols_available and not data.get("status"):
        data["status"] = "ACTIVE"

    # multi-company stamp + common columns (only if the table has them)
    if "company_id" in cols_available:
        data["company_id"] = _cid(request)
    if "is_active" in cols_available:
        data["is_active"] = 1
    if "created_by" in cols_available:
        data["created_by"] = _user(request)

    # keep only columns that actually exist on the table
    data = {k: v for k, v in data.items() if k in cols_available}
    if cfg["name_col"] not in data or not data.get(cfg["name_col"]):
        return RedirectResponse(
            f"/masters-crud/{mtype}/new?error=Name is required", status_code=303)

    collist = ", ".join(data.keys())
    binds = ", ".join(f":{k}" for k in data.keys())
    try:
        db.execute(text(f"INSERT INTO {cfg['table']} ({collist}) VALUES ({binds})"), data)
        db.commit()
    except Exception as e:
        db.rollback()
        return RedirectResponse(
            f"/masters-crud/{mtype}/new?error=Could not save: {str(e)[:120]}",
            status_code=303)
    return RedirectResponse(
        f"{cfg['list_url']}?success={cfg['title']} added successfully", status_code=303)


# ---------------------------------------------------------------------------
# EDIT — pre-filled form
# ---------------------------------------------------------------------------
@router.get("/masters-crud/{mtype}/{row_id}/edit")
def edit_master_form(mtype: str, row_id: int, request: Request, db: Session = Depends(get_db)):
    require_area(request, "master_data")
    cfg = _cfg(mtype)
    row = db.execute(
        text(f"SELECT * FROM {cfg['table']} WHERE id = :i"), {"i": row_id}
    ).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail="Record not found")
    return render(request, "masters_crud/form.html", {
        "mtype": mtype, "cfg": cfg, "row": dict(row), "mode": "edit",
        "page_title": f"Edit {cfg['title']}",
    })


@router.post("/masters-crud/{mtype}/{row_id}/edit")
async def update_master(mtype: str, row_id: int, request: Request, db: Session = Depends(get_db)):
    require_action(request, "master_data", "edit")
    cfg = _cfg(mtype)
    form = await request.form()
    cols_available = _table_columns(db, cfg["table"])

    sets: dict[str, object] = {}
    for col, label, ftype, required in cfg["fields"]:
        if col not in cols_available:
            continue
        val = (form.get(col) or "").strip()
        if ftype == "number":
            sets[col] = float(val) if val else 0
        else:
            sets[col] = val or None

    if not sets:
        return RedirectResponse(cfg["list_url"], status_code=303)

    if "updated_at" in cols_available:
        sets["updated_at"] = datetime.utcnow()

    assignments = ", ".join(f"{k} = :{k}" for k in sets.keys())
    sets["_id"] = row_id
    try:
        db.execute(text(f"UPDATE {cfg['table']} SET {assignments} WHERE id = :_id"), sets)
        db.commit()
    except Exception as e:
        db.rollback()
        return RedirectResponse(
            f"/masters-crud/{mtype}/{row_id}/edit?error=Could not update: {str(e)[:120]}",
            status_code=303)
    return RedirectResponse(
        f"{cfg['list_url']}?success={cfg['title']} updated", status_code=303)
