# app/core/module_visibility.py
# =============================================================================
# Batch 65 — MODULE VISIBILITY (sellable-module gating)
# -----------------------------------------------------------------------------
# The business goal: ISFC will sell the system module-by-module. A given
# customer company might buy ONLY Production first, then later switch on
# Procurement / Inventory / Finance, etc.
#
# This layer answers one question: "Is a top-level ERP module switched ON for
# this company?" It is completely SEPARATE from RBAC:
#
#   * RBAC (app/core/rbac.py)        -> "is THIS USER allowed to see area X?"
#   * Module visibility (this file)  -> "did the COMPANY buy / enable module X?"
#
# A screen is shown only when BOTH are true. An admin can never see a module
# that the company has switched off, because a disabled module is not part of
# the product for that company at all.
#
# Storage: one row per (company_id, module_key) in `module_visibility`.
#   enabled = 1 -> visible,  enabled = 0 -> hidden.
# A module with NO row defaults to its DEFAULT_ENABLED value below, so a fresh
# install already shows the "core" set and hides the not-yet-sold ones.
#
# The whole thing is framework-light and fail-open on infrastructure errors:
# if the DB/table is unreachable we return the default map so the app never
# hard-breaks because of a settings lookup.
# =============================================================================

from __future__ import annotations

from functools import lru_cache
from fastapi import Request
from sqlalchemy import text
from sqlalchemy.orm import Session

# -----------------------------------------------------------------------------
# Catalogue of sellable modules. `key` is stored in the DB and used everywhere.
# `areas` are the RBAC areas that belong to the module — when a module is OFF,
# every one of its areas is treated as hidden regardless of the RBAC matrix.
# `default` decides visibility before any admin ever touches the toggles.
# -----------------------------------------------------------------------------
MODULE_CATALOG: list[dict] = [
    {
        "key": "production",
        "label": "Production Intelligence",
        "icon": "activity",
        "color": "primary",
        "default": True,
        "areas": {
            "dashboard", "orders", "order_portal", "production_orders",
            "head_chef", "bom", "store", "store_issuance", "kitchen",
            "kitchen_summary", "qc", "packing", "dispatch",
            "section_cutting", "section_butchery", "section_hot_kitchen",
            "section_cold_kitchen", "section_bakery_pastry",
        },
    },
    {
        "key": "inventory",
        "label": "Inventory & Valuation",
        "icon": "box-seam",
        "color": "info",
        "default": True,
        "areas": {"inventory_valuation"},
    },
    {
        "key": "procurement",
        "label": "Procurement",
        "icon": "bag",
        "color": "warning",
        "default": True,
        "areas": {"procurement"},
    },
    {
        "key": "recipes",
        "label": "Recipes & BOM Library",
        "icon": "journal-text",
        "color": "primary",
        "default": True,
        "areas": {"recipes", "recipe_list", "recipe_prepare",
                  "recipe_missing", "recipe_approvals"},
    },
    {
        "key": "masters",
        "label": "Master Data",
        "icon": "database",
        "color": "danger",
        "default": True,
        "areas": {"masters", "master_data", "master_upload"},
    },
    {
        "key": "reports",
        "label": "Reports & BI",
        "icon": "bar-chart-line",
        "color": "secondary",
        "default": True,
        "areas": {"reports", "relationship"},
    },
    {
        "key": "projects",
        "label": "Project Management",
        "icon": "briefcase",
        "color": "info",
        "default": False,
        "areas": {"project_management", "project_list", "project_detail",
                  "project_team", "project_timeline"},
    },
    {
        "key": "finance",
        "label": "Finance (AR / AP)",
        "icon": "cash-coin",
        "color": "success",
        "default": True,   # Batch 69: Finance is complete (GL + statements) — show by default
        "areas": {"finance"},
    },
    {
        "key": "hcm",
        "label": "HCM (Human Capital)",
        "icon": "people",
        "color": "primary",
        "default": False,
        "areas": {"hr"},
    },
    {
        "key": "customer_portal",
        "label": "Customer Portal",
        "icon": "person-check",
        "color": "success",
        "default": True,
        "areas": {"customer_portal"},
    },
    {
        "key": "subscriptions",
        "label": "Subscriptions & Recurring Orders",
        "icon": "arrow-repeat",
        "color": "info",
        "default": True,   # Batch 76: new module, on by default so it's usable immediately
        "areas": {"subscriptions"},
    },
    {
        "key": "users",
        "label": "Users & Access",
        "icon": "shield-lock",
        "color": "dark",
        "default": True,     # kept on so admins never lock themselves out
        "areas": {"users", "audit"},
    },
    {
        # Batch 120: Sales was rendered by the launcher template but had NO
        # catalog entry, so module_enabled("sales") fell through to False and
        # the card never appeared. Sales is where order-to-cash begins, so it
        # defaults ON. Its areas are the sales/portal ordering surfaces.
        "key": "sales",
        "label": "Sales & Orders",
        "icon": "cart",
        "color": "success",
        "default": True,
        "areas": {
            "order_portal", "sales_review", "immediate_order",
            "my_orders", "account_statement", "sale_requisitions",
        },
    },
]

MODULE_KEYS = [m["key"] for m in MODULE_CATALOG]
_DEFAULT_MAP = {m["key"]: bool(m["default"]) for m in MODULE_CATALOG}

# area -> module key (reverse index)
_AREA_TO_MODULE: dict[str, str] = {}
for _m in MODULE_CATALOG:
    for _a in _m["areas"]:
        _AREA_TO_MODULE[_a] = _m["key"]


# -----------------------------------------------------------------------------
# Schema (idempotent). Called by settings save + lazily by the reader.
# -----------------------------------------------------------------------------
def ensure_schema(db: Session) -> None:
    try:
        db.execute(text("""
            CREATE TABLE IF NOT EXISTS module_visibility (
                id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                company_id INT NOT NULL DEFAULT 1,
                module_key VARCHAR(64) NOT NULL,
                enabled TINYINT(1) NOT NULL DEFAULT 1,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    ON UPDATE CURRENT_TIMESTAMP,
                UNIQUE KEY uq_company_module (company_id, module_key)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """))
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass


# -----------------------------------------------------------------------------
# DB readers. `get_map` returns {module_key: bool} for a company, merging saved
# rows over the defaults so unknown/new modules always resolve.
# -----------------------------------------------------------------------------
def get_map(db: Session, company_id: int = 1) -> dict[str, bool]:
    result = dict(_DEFAULT_MAP)
    try:
        rows = db.execute(text("""
            SELECT module_key, enabled FROM module_visibility
            WHERE company_id = :cid
        """), {"cid": company_id or 1}).mappings().all()
        for r in rows:
            if r["module_key"] in result:
                result[r["module_key"]] = bool(r["enabled"])
    except Exception:
        # table not created yet — create then return defaults
        ensure_schema(db)
    return result


def set_map(db: Session, enabled_keys: set[str], company_id: int = 1) -> None:
    """Persist the full on/off set. `enabled_keys` = keys that should be ON."""
    ensure_schema(db)
    for key in MODULE_KEYS:
        on = 1 if key in enabled_keys else 0
        db.execute(text("""
            INSERT INTO module_visibility (company_id, module_key, enabled)
            VALUES (:cid, :k, :en)
            ON DUPLICATE KEY UPDATE enabled = :en
        """), {"cid": company_id or 1, "k": key, "en": on})
    db.commit()


# -----------------------------------------------------------------------------
# Request-scoped cache so a single page render hits the DB once, not per-card.
# We stash the resolved map on request.state.
# -----------------------------------------------------------------------------
def _map_for_request(request: Request) -> dict[str, bool]:
    cached = getattr(getattr(request, "state", None), "_module_vis", None)
    if cached is not None:
        return cached

    company_id = 1
    try:
        company_id = int(request.session.get("company_id") or 1)
    except Exception:
        company_id = 1

    result = dict(_DEFAULT_MAP)
    try:
        from app.database.session import SessionLocal
        db = SessionLocal()
        try:
            result = get_map(db, company_id)
        finally:
            db.close()
    except Exception:
        result = dict(_DEFAULT_MAP)

    try:
        request.state._module_vis = result
    except Exception:
        pass
    return result


def module_enabled(request: Request, module_key: str) -> bool:
    """Is a top-level module switched ON for the current company?"""
    return bool(_map_for_request(request).get(module_key, False))


def area_enabled(request: Request, area: str) -> bool:
    """Is the module that owns this RBAC area switched ON?

    Areas that don't map to any sellable module (edge cases) are allowed, so
    this never over-blocks. `module_home` (the launcher itself) is always on.
    """
    if area in ("module_home", "settings"):
        return True
    mod = _AREA_TO_MODULE.get(area)
    if mod is None:
        return True
    return module_enabled(request, mod)


def enabled_modules(request: Request) -> list[dict]:
    """Catalogue entries that are currently ON — used to render the launcher."""
    m = _map_for_request(request)
    return [dict(x) for x in MODULE_CATALOG if m.get(x["key"], False)]
