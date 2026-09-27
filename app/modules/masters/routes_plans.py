# app/modules/masters/routes_plans.py
# =============================================================================
# Batch 248 — Master → Customer Plans.
#
# Plans come in with the customer's workbook (scripts/import_salus_master.py),
# but the protein / carb headline, the display name, the order they appear in
# and whether a plan is still sold are commercial facts that change without a
# new workbook. This screen edits exactly those, per customer key.
#
# Which dishes are in which plan, and the per-plan quantities on each recipe
# line, stay with the recipe import — they are recipe data, and editing them
# here would let the menu and the kitchen quantities drift apart.
# =============================================================================
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.rbac import require_action, require_area
from app.core.templates import render
from app.database.session import get_db
from app.services.customer_plans import ensure_plan_schema

router = APIRouter(tags=["Masters"])


def _cid(request: Request) -> int:
    return int(request.session.get("company_id") or 1)


def _num(v):
    try:
        v = str(v or "").strip()
        return float(v) if v else None
    except ValueError:
        return None


@router.get("/masters/customer-plans")
def customer_plans_page(request: Request, db: Session = Depends(get_db)):
    require_area(request, "master_data")
    ensure_plan_schema(db)
    cid = _cid(request)
    rows = db.execute(text("""
        SELECT p.id, p.customer_key, p.plan_code, p.plan_name, p.protein_g, p.carb_g,
               COALESCE(p.description,'') AS description, p.sort_order, p.is_active,
               (SELECT COUNT(*) FROM recipe_plans rp
                 WHERE rp.plan_code = p.plan_code
                   AND (rp.company_id = :cid OR rp.company_id IS NULL)) AS dishes,
               (SELECT COUNT(DISTINCT rip.recipe_ingredient_id) FROM recipe_ingredient_plans rip
                 WHERE rip.plan_code = p.plan_code) AS qty_lines
        FROM customer_plans p
        WHERE p.company_id = :cid OR p.company_id IS NULL
        ORDER BY p.customer_key, p.sort_order, p.plan_name
    """), {"cid": cid}).mappings().all()
    groups: dict[str, list] = {}
    for r in rows:
        groups.setdefault(r["customer_key"], []).append(dict(r))
    return render(request, "masters/customer_plans.html", {
        "groups": groups, "page_title": "Customer Plans"})


@router.post("/masters/customer-plans/save")
async def customer_plans_save(request: Request, db: Session = Depends(get_db)):
    require_action(request, "master_data", "edit")
    ensure_plan_schema(db)
    cid = _cid(request)
    form = await request.form()
    ids = [int(x) for x in form.getlist("id") if str(x).isdigit()]
    for pid in ids:
        db.execute(text("""
            UPDATE customer_plans
               SET plan_name = :name, protein_g = :p, carb_g = :c, description = :d,
                   sort_order = :s, is_active = :a, updated_at = NOW()
             WHERE id = :id AND (company_id = :cid OR company_id IS NULL)
        """), {"id": pid, "cid": cid,
               "name": (form.get(f"name_{pid}") or "").strip() or "Plan",
               "p": _num(form.get(f"protein_{pid}")), "c": _num(form.get(f"carb_{pid}")),
               "d": (form.get(f"desc_{pid}") or "").strip() or None,
               "s": int(_num(form.get(f"sort_{pid}")) or 0),
               "a": 1 if form.get(f"active_{pid}") else 0})
    # Optional new plan row.
    key = (form.get("new_customer_key") or "").strip()
    code = (form.get("new_plan_code") or "").strip().upper().replace(" ", "_")
    name = (form.get("new_plan_name") or "").strip()
    msg = f"{len(ids)} plan(s) saved."
    if key and code and name:
        db.execute(text("""
            INSERT INTO customer_plans (company_id, customer_key, plan_code, plan_name,
                protein_g, carb_g, sort_order, is_active, created_at, updated_at)
            VALUES (:cid, :key, :code, :name, :p, :c, :s, 1, NOW(), NOW())
            ON DUPLICATE KEY UPDATE plan_name = VALUES(plan_name), is_active = 1, updated_at = NOW()
        """), {"cid": cid, "key": key.upper(), "code": code, "name": name,
               "p": _num(form.get("new_protein")), "c": _num(form.get("new_carb")),
               "s": int(_num(form.get("new_sort")) or 99)})
        msg += f" Plan {name} added for {key.upper()}."
    db.commit()
    return RedirectResponse(f"/masters/customer-plans?toast=success&title=Plans&msg={msg}", status_code=303)
