# app/modules/settings/routes_modules.py
# =============================================================================
# Batch 65 — MODULE VISIBILITY admin (sell modules one-by-one)
# -----------------------------------------------------------------------------
# A dedicated admin screen with tick-boxes for each sellable module. Saving
# writes the on/off set to `module_visibility` for the active company. The
# gate is enforced in app/core/rbac.can_access() (module then RBAC), so
# switching a module OFF removes its cards from the launcher, its items from
# every navbar/sidebar, AND blocks its routes — for all users, admins included.
#
# Registered from app/main.py:
#     from app.modules.settings.routes_modules import router as settings_modules_router
#     app.include_router(settings_modules_router)
# =============================================================================

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.core.templates import render
from app.core.audit import write_audit
from app.core.rbac import is_admin
from app.core.module_visibility import (
    MODULE_CATALOG, MODULE_KEYS, ensure_schema, get_map, set_map,
)

router = APIRouter(prefix="/settings/modules", tags=["Settings"])


def _company_id(request: Request) -> int:
    try:
        return int(request.session.get("company_id") or 1)
    except Exception:
        return 1


def _guard(request: Request):
    """Only admins manage the sellable-module set."""
    if not is_admin(request):
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="Admins only")


@router.get("")
async def module_visibility_page(request: Request, db: Session = Depends(get_db)):
    _guard(request)
    ensure_schema(db)
    cid = _company_id(request)
    current = get_map(db, cid)

    modules = []
    for m in MODULE_CATALOG:
        modules.append({
            **m,
            "enabled": bool(current.get(m["key"], m["default"])),
            "locked": m["key"] == "users",  # never allow hiding Users & Access
        })

    return render(request, "settings/module_visibility.html", {
        "page_title": "Module Visibility",
        "modules": modules,
        "company_id": cid,
    })


@router.post("")
async def module_visibility_save(request: Request, db: Session = Depends(get_db)):
    _guard(request)
    cid = _company_id(request)
    form = await request.form()

    # Checkbox convention: a checked box submits "on"; unchecked submits nothing.
    enabled = {k for k in MODULE_KEYS if form.get(f"mod_{k}")}
    enabled.add("users")  # safety: Users & Access can never be switched off

    set_map(db, enabled, cid)

    try:
        write_audit(db, request.session.get("user_id"),
                    "MODULE_VISIBILITY_UPDATED", "module_visibility", cid)
        db.commit()
    except Exception:
        pass

    # Clear the per-request cache marker isn't needed (new request), but drop
    # any stale session hint if present.
    return RedirectResponse(
        url="/settings/modules?toast=success&title=Saved"
            "&msg=Module visibility updated. Users see changes on next page load.",
        status_code=303,
    )
