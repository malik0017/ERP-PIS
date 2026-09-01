# app/modules/recipes/routes_bulk.py
# =============================================================================
# Batch 103 — BULK RECIPE OPERATIONS
# -----------------------------------------------------------------------------
# The situation your screenshots show: re-uploading the Excel created V2 of
# every recipe, leaving 110 rows — 54 active, 48 pending approval, 8 inactive.
# The only tools were "Approve All Pending" (all-or-nothing) and per-row
# buttons (49 clicks, each one a page reload that bounced you back to the
# full list).
#
# Neither is usable at this volume. This adds tick-box selection with four
# bulk actions, and — importantly — the page you were on is preserved after
# the action so you can work down a list instead of being thrown back to the
# top every time.
#
# WHY DEACTIVATE IS THE DEFAULT AND DELETE IS NOT
#
# A recipe that has ever been ordered is referenced by order_lines, bom_lines
# and kitchen_production. Deleting it orphans those rows: historical orders
# lose their recipe name, BOM explosions break, and yield reporting silently
# loses its denominator. So delete is refused for any recipe with history and
# offered only for versions that were never used — which is exactly the case
# for a duplicate V1 created by a re-upload.
# =============================================================================
from __future__ import annotations

from datetime import datetime
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.rbac import require_action
from app.database.session import get_db

router = APIRouter(prefix="/recipes", tags=["Recipes"])


def _ids(form) -> list[int]:
    out = []
    for raw in form.getlist("recipe_ids"):
        try:
            out.append(int(raw))
        except (TypeError, ValueError):
            continue
    return out


def _cid(request: Request) -> int:
    """Batch 154/160: active company. Every bulk UPDATE is scoped to it so a
    request that (accidentally or maliciously) carries recipe ids from ANOTHER
    company can never modify those rows — the id IN (...) is ANDed with
    company_id = :cid. Falls back to 1 for the single-company default."""
    try:
        return int(request.session.get("company_id") or 1)
    except (TypeError, ValueError):
        return 1


def _user(request: Request):
    """recipes.approved_by is an INT user id, not a username.

    Storing the display name there raises
        Incorrect integer value: 'admin' for column 'approved_by'
    under MySQL strict mode — which is exactly what strict mode is for. The
    per-row approve route already stored the id; the bulk version has to match
    or the two paths would write different things into the same column.
    """
    uid = request.session.get("user_id")
    try:
        return int(uid) if uid is not None else None
    except (TypeError, ValueError):
        return None


def _back(request: Request, form, msg: str, variant: str = "success",
          title: str = "Recipes") -> RedirectResponse:
    """Return to the screen the action was launched from.

    Every per-row action previously redirected to /recipes, so approving 49
    versions meant 49 trips back to the top of the full list. The form posts
    its own origin and we honour it.
    """
    target = (form.get("return_to") or "").strip() or "/recipes/pending"
    sep = "&" if "?" in target else "?"
    return RedirectResponse(
        f"{target}{sep}toast={variant}&title={quote(title)}&msg={quote(msg)}",
        status_code=303)


def _has_history(db: Session, recipe_code: str) -> bool:
    """Has this recipe ever been used? Checked before allowing a delete."""
    for sql in (
        "SELECT COUNT(*) FROM order_lines WHERE recipe_no = :c",
        "SELECT COUNT(*) FROM bom_lines WHERE recipe_no = :c",
        "SELECT COUNT(*) FROM kitchen_production WHERE recipe_no = :c",
    ):
        try:
            if (db.execute(text(sql), {"c": recipe_code}).scalar() or 0) > 0:
                return True
        except Exception:
            # Missing table on an older schema must not be read as "no history"
            # — that would let a delete through on unknown ground.
            return True
    return False


@router.post("/bulk/approve")
async def bulk_approve(request: Request, db: Session = Depends(get_db)):
    require_action(request, "recipe_approvals", "edit")
    form = await request.form()
    ids = _ids(form)
    if not ids:
        return _back(request, form, "Nothing was selected.", "warning")

    ph = ",".join(f":i{n}" for n in range(len(ids)))
    params = {f"i{n}": v for n, v in enumerate(ids)}
    params.update({"by": _user(request), "at": datetime.utcnow(), "cid": _cid(request)})
    db.execute(text(f"""
        UPDATE recipes
        SET approval_status = 'Approved', approved_by = :by, approved_at = :at,
            is_active = 1, status = 'Active'
        WHERE id IN ({ph}) AND (company_id = :cid OR company_id IS NULL)
    """), params)

    # Approving a version supersedes the older ones for the same code, so they
    # are retired in the same statement. Leaving both active is what produced
    # the duplicated rows in the list.
    db.execute(text(f"""
        UPDATE recipes r
        JOIN (SELECT recipe_code, MAX(version) AS v FROM recipes
              WHERE id IN ({ph}) GROUP BY recipe_code) x
          ON x.recipe_code = r.recipe_code
        SET r.is_active = 0, r.status = 'Inactive'
        WHERE r.version < x.v AND (r.company_id = :cid OR r.company_id IS NULL)
    """), {**{f"i{n}": v for n, v in enumerate(ids)}, "cid": _cid(request)})
    db.commit()
    return _back(request, form,
                 f"{len(ids)} recipe version(s) approved. Older versions were retired.")


@router.post("/bulk/reject")
async def bulk_reject(request: Request, db: Session = Depends(get_db)):
    require_action(request, "recipe_approvals", "edit")
    form = await request.form()
    ids = _ids(form)
    if not ids:
        return _back(request, form, "Nothing was selected.", "warning")
    ph = ",".join(f":i{n}" for n in range(len(ids)))
    params = {f"i{n}": v for n, v in enumerate(ids)}
    params.update({"by": _user(request), "at": datetime.utcnow(), "cid": _cid(request)})
    db.execute(text(f"""
        UPDATE recipes
        SET approval_status = 'Rejected', approved_by = :by, approved_at = :at,
            is_active = 0, status = 'Inactive'
        WHERE id IN ({ph}) AND (company_id = :cid OR company_id IS NULL)
    """), params)
    db.commit()
    return _back(request, form, f"{len(ids)} recipe version(s) rejected.", "warning")


@router.post("/bulk/deactivate")
async def bulk_deactivate(request: Request, db: Session = Depends(get_db)):
    """Retire versions without destroying them — the safe way to clear the
    duplicate V1 rows left behind by a re-upload."""
    require_action(request, "recipe_list", "edit")
    form = await request.form()
    ids = _ids(form)
    if not ids:
        return _back(request, form, "Nothing was selected.", "warning")
    ph = ",".join(f":i{n}" for n in range(len(ids)))
    db.execute(text(f"""
        UPDATE recipes SET is_active = 0, status = 'Inactive'
        WHERE id IN ({ph}) AND (company_id = :cid OR company_id IS NULL)
    """), {**{f"i{n}": v for n, v in enumerate(ids)}, "cid": _cid(request)})
    db.commit()
    return _back(request, form,
                 f"{len(ids)} recipe(s) deactivated. They stay in history and can be reactivated.")


@router.post("/bulk/activate")
async def bulk_activate(request: Request, db: Session = Depends(get_db)):
    require_action(request, "recipe_list", "edit")
    form = await request.form()
    ids = _ids(form)
    if not ids:
        return _back(request, form, "Nothing was selected.", "warning")
    ph = ",".join(f":i{n}" for n in range(len(ids)))
    db.execute(text(f"""
        UPDATE recipes SET is_active = 1, status = 'Active'
        WHERE id IN ({ph}) AND (company_id = :cid OR company_id IS NULL)
    """), {**{f"i{n}": v for n, v in enumerate(ids)}, "cid": _cid(request)})
    db.commit()
    return _back(request, form, f"{len(ids)} recipe(s) reactivated.")


@router.post("/bulk/delete")
async def bulk_delete(request: Request, db: Session = Depends(get_db)):
    """Permanent delete — refused for anything with production history.

    See the module docstring: deleting a recipe that has been ordered orphans
    order_lines, bom_lines and kitchen_production. Those rows are what your
    historical costing and yield reports are built on.
    """
    require_action(request, "recipe_list", "delete")
    form = await request.form()
    ids = _ids(form)
    if not ids:
        return _back(request, form, "Nothing was selected.", "warning")

    ph = ",".join(f":i{n}" for n in range(len(ids)))
    rows = db.execute(text(f"""
        SELECT id, recipe_code, version FROM recipes WHERE id IN ({ph})
    """), {f"i{n}": v for n, v in enumerate(ids)}).mappings().all()

    deletable, blocked = [], []
    for r in rows:
        if _has_history(db, r["recipe_code"]):
            blocked.append(f"{r['recipe_code']} v{r['version']}")
        else:
            deletable.append(r["id"])

    if deletable:
        dph = ",".join(f":d{n}" for n in range(len(deletable)))
        dparams = {f"d{n}": v for n, v in enumerate(deletable)}
        # Ingredient lines go first — orphaned recipe_ingredients rows would
        # otherwise linger and quietly inflate the Missing Data report.
        try:
            db.execute(text(f"DELETE FROM recipe_ingredients WHERE recipe_id IN ({dph})"), dparams)
        except Exception:
            db.rollback()
        db.execute(text(f"DELETE FROM recipes WHERE id IN ({dph})"), dparams)
        db.commit()

    if blocked and deletable:
        return _back(request, form,
                     f"{len(deletable)} deleted. {len(blocked)} kept because they have "
                     f"production history: {', '.join(blocked[:5])}"
                     + ("…" if len(blocked) > 5 else "")
                     + ". Deactivate those instead.", "warning")
    if blocked:
        return _back(request, form,
                     f"Nothing deleted — all {len(blocked)} selected recipe(s) have production "
                     "history and are referenced by existing orders. Deactivate them instead.",
                     "danger")
    return _back(request, form, f"{len(deletable)} recipe(s) deleted permanently.")


@router.post("/bulk/retire-superseded")
async def retire_superseded(request: Request, db: Session = Depends(get_db)):
    """One click for the exact situation in your screenshot.

    For every recipe_code that has more than one version, keep the highest
    version active and deactivate the rest. This is what a re-upload leaves
    behind, and doing it by hand is 50+ clicks.
    """
    require_action(request, "recipe_list", "edit")
    form = await request.form()
    # Batch 160: scope the version-cleanup to the active company. The inner
    # MAX(version) per recipe_code is also company-scoped, so cross-company codes
    # can't influence which version is kept.
    res = db.execute(text("""
        UPDATE recipes r
        JOIN (SELECT recipe_code, MAX(version) AS v FROM recipes
              WHERE (company_id = :cid OR company_id IS NULL)
              GROUP BY recipe_code) x
          ON x.recipe_code = r.recipe_code
        SET r.is_active = 0, r.status = 'Inactive'
        WHERE r.version < x.v AND COALESCE(r.is_active, 1) = 1
          AND (r.company_id = :cid OR r.company_id IS NULL)
    """), {"cid": _cid(request)})
    db.commit()
    n = getattr(res, "rowcount", 0) or 0
    return _back(request, form,
                 f"{n} superseded version(s) retired. Only the latest version of each recipe "
                 "is active now.")
