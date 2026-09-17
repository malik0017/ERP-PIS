"""Batch 170 — SLA Management and Performance Targets configuration.

Kept in its own module rather than bolted into System routes: these are two
config domains with their own tables, and burying them in a general settings
file is how they become hard to find and harder to change.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import text
from sqlalchemy.orm import Session
from starlette.status import HTTP_303_SEE_OTHER

from app.core.rbac import require_area, require_action
from app.core.templates import render
from app.database.session import get_db
import logging
from urllib.parse import quote_plus

from app.services.ops_templates import SLA_TEMPLATES, TARGET_TEMPLATES

from app.core.db_read import log_failure as db_read_log
from app.services.sla_service import (
    DASHBOARD_STATUS, METRIC_CODES, compute_status, evaluate_targets,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/system", tags=["SLA & Targets"])


def _cid(request: Request) -> int:
    try:
        return int(request.session.get("company_id") or 1)
    except Exception:
        return 1


def _rows(db: Session, sql: str, params: dict | None = None) -> list:
    """Read helper that reports failure instead of returning an empty list.

    A config screen that shows "no rules" when the table is missing looks
    identical to one with no rules configured — and the user would happily
    create a duplicate rule on top of the ones they cannot see.
    """
    try:
        return [dict(r) for r in db.execute(text(sql), params or {}).mappings().all()], None
    except Exception as exc:
        # Batch 221: this helper already returns the error to the caller (the
        # right pattern — see the docstring). It now logs as well, so a failure
        # is visible even where a caller discards the second value, which is
        # exactly how the Recent Orders bug survived (Batch 212).
        db_read_log(exc, sql, "sla/routes.py._rows")
        db.rollback()
        return [], f"{exc.__class__.__name__}: {exc}"


# ---------------------------------------------------------------------------
# SLA rules
# ---------------------------------------------------------------------------
@router.get("/sla")
async def sla_rules_page(request: Request, db: Session = Depends(get_db)):
    require_area(request, "settings")
    cid = _cid(request)
    rules, err = _rows(db, """
        SELECT * FROM sla_rules
        WHERE company_id = :cid OR company_id IS NULL
        ORDER BY
            CASE WHEN COALESCE(customer_name,'') <> '' AND COALESCE(order_type,'') <> '' THEN 0
                 WHEN COALESCE(customer_name,'') <> '' THEN 1
                 WHEN COALESCE(order_type,'') <> '' THEN 2 ELSE 3 END,
            priority ASC, id DESC
    """, {"cid": cid})

    # Live counts across open orders, so the page shows the effect of the rules
    # rather than only their definition.
    live = {"at_risk": 0, "overdue": 0, "total": 0, "breached": 0}
    inst, _ = _rows(db, """
        SELECT os.* FROM order_sla os
        WHERE os.completed_at IS NULL
          AND (os.company_id = :cid OR os.company_id IS NULL)
    """, {"cid": cid})
    now = datetime.utcnow()
    for i in inst:
        st = compute_status(i, now=now)
        live["total"] += 1
        if st == "AT_RISK":
            live["at_risk"] += 1
        elif st == "OVERDUE":
            live["overdue"] += 1
        elif st == "BREACHED":
            live["breached"] += 1

    customers, _ = _rows(db, """
        SELECT DISTINCT customer_name FROM customers
        WHERE COALESCE(customer_name,'') <> ''
          AND UPPER(TRIM(COALESCE(status,''))) = 'ACTIVE'
        ORDER BY customer_name LIMIT 200
    """)
    return render(request, "sla/rules.html", {
        # Batch 214: the starter set, shown while nothing is configured.
        "sla_templates": SLA_TEMPLATES,
        "rules": rules, "error": err, "live": live,
        "customers": [c["customer_name"] for c in customers],
        "page_title": "SLA Management"})


@router.post("/sla/save")
async def sla_rule_save(request: Request, db: Session = Depends(get_db)):
    require_action(request, "settings", "edit")
    f = await request.form()

    def _i(k, d=0):
        try:
            return int(float((f.get(k) or "").strip() or d))
        except ValueError:
            return d

    rule_id = (f.get("id") or "").strip()
    name = (f.get("rule_name") or "").strip()
    if not name:
        return RedirectResponse("/system/sla?error=Rule+name+is+required",
                                status_code=HTTP_303_SEE_OTHER)

    vals = {
        "cid": _cid(request), "name": name,
        # Empty string is stored as NULL, not "". NULL means "applies to all"
        # in the resolver; an empty string would match no customer at all and
        # the rule would silently never fire.
        "cust": (f.get("customer_name") or "").strip() or None,
        "otype": (f.get("order_type") or "").strip() or None,
        "sla": _i("sla_minutes", 480), "risk": _i("at_risk_minutes", 120),
        "grace": _i("grace_minutes", 15),
        "starts": (f.get("starts_on") or "CONFIRMED").strip(),
        "ends": (f.get("ends_on") or "DELIVERED").strip(),
        "basis": (f.get("basis") or "DEADLINE").strip(),
        "prio": _i("priority", 100),
        "nrisk": 1 if f.get("notify_at_risk") else 0,
        "nover": 1 if f.get("notify_overdue") else 0,
        "status": (f.get("status") or "Active").strip(),
    }
    try:
        if rule_id:
            vals["id"] = int(rule_id)
            db.execute(text("""
                UPDATE sla_rules SET rule_name=:name, customer_name=:cust,
                  order_type=:otype, sla_minutes=:sla, at_risk_minutes=:risk,
                  grace_minutes=:grace, starts_on=:starts, ends_on=:ends,
                  basis=:basis, priority=:prio, notify_at_risk=:nrisk,
                  notify_overdue=:nover, status=:status, updated_at=NOW()
                WHERE id=:id
            """), vals)
        else:
            db.execute(text("""
                INSERT INTO sla_rules(company_id, rule_name, customer_name, order_type,
                  sla_minutes, at_risk_minutes, grace_minutes, starts_on, ends_on,
                  basis, priority, notify_at_risk, notify_overdue, status,
                  created_at, updated_at)
                VALUES(:cid,:name,:cust,:otype,:sla,:risk,:grace,:starts,:ends,
                       :basis,:prio,:nrisk,:nover,:status,NOW(),NOW())
            """), vals)
        db.commit()
    except Exception as exc:
        return RedirectResponse(f"/system/sla?error={exc.__class__.__name__}",
                                status_code=HTTP_303_SEE_OTHER)
    return RedirectResponse("/system/sla?toast=success&title=Saved&msg=SLA+rule+saved.",
                            status_code=HTTP_303_SEE_OTHER)


# ---------------------------------------------------------------------------
# Performance targets
# ---------------------------------------------------------------------------
@router.post("/sla/install-templates")
async def sla_install_templates(request: Request, db: Session = Depends(get_db)):
    """Batch 214 — install the four starter SLA rules.

    Same permission as creating a rule by hand. Idempotent: an existing rule of
    the same name is left exactly as it is, so pressing this twice — or after
    editing one — changes nothing.
    """
    require_action(request, "settings", "edit")
    from app.services.ops_templates import install_sla_templates

    r = install_sla_templates(db, _cid(request))
    if r["created"]:
        msg = f"Added {len(r['created'])} SLA rule(s): " + ", ".join(r["created"])
        return RedirectResponse(f"/system/sla?msg={quote_plus(msg)}", status_code=HTTP_303_SEE_OTHER)
    return RedirectResponse(
        "/system/sla?msg=" + quote_plus("All starter rules already exist — nothing changed."),
        status_code=HTTP_303_SEE_OTHER)


@router.post("/targets/install-templates")
async def targets_install_templates(request: Request, db: Session = Depends(get_db)):
    """Batch 214 — install the six starter performance targets."""
    require_action(request, "settings", "edit")
    from app.services.ops_templates import install_target_templates

    r = install_target_templates(db, _cid(request))
    if r["created"]:
        msg = f"Added {len(r['created'])} target(s): " + ", ".join(r["created"])
        return RedirectResponse(f"/system/targets?msg={quote_plus(msg)}", status_code=HTTP_303_SEE_OTHER)
    return RedirectResponse(
        "/system/targets?msg=" + quote_plus("All starter targets already exist — nothing changed."),
        status_code=HTTP_303_SEE_OTHER)


@router.get("/targets")
async def targets_page(request: Request, db: Session = Depends(get_db)):
    require_area(request, "settings")
    cid = _cid(request)
    date_from = (request.query_params.get("date_from") or "").strip()
    date_to = (request.query_params.get("date_to") or "").strip()

    results = evaluate_targets(db, date_from or None, date_to or None,
                               company_id=cid)
    summary = {
        "active": len(results),
        "exceeded": sum(1 for r in results if r["state"] == "exceeded"),
        "below": sum(1 for r in results if r["state"] == "below"),
        "unknown": sum(1 for r in results if r["state"] == "unknown"),
    }
    measured = [r["achievement"] for r in results if r["achievement"] is not None]
    summary["achievement"] = round(sum(measured) / len(measured), 1) if measured else None

    customers, _ = _rows(db, """
        SELECT DISTINCT customer_name FROM customers
        WHERE COALESCE(customer_name,'') <> ''
        ORDER BY customer_name LIMIT 200
    """)
    return render(request, "sla/targets.html", {
        "target_templates": TARGET_TEMPLATES,
        "results": results, "summary": summary, "metrics": METRIC_CODES,
        "customers": [c["customer_name"] for c in customers],
        "filters": {"date_from": date_from, "date_to": date_to},
        "page_title": "Performance Targets"})


@router.post("/targets/save")
async def target_save(request: Request, db: Session = Depends(get_db)):
    require_action(request, "settings", "edit")
    f = await request.form()
    name = (f.get("target_name") or "").strip()
    metric = (f.get("metric_code") or "").strip()
    valid = {c for c, _, _ in METRIC_CODES}
    if not name or metric not in valid:
        # Metric codes are a closed list on purpose — the engine has to know how
        # to calculate each one, so an unknown code is rejected rather than
        # stored and silently measured as nothing.
        return RedirectResponse("/system/targets?error=Name+and+a+valid+metric+are+required",
                                status_code=HTTP_303_SEE_OTHER)
    try:
        tv = float((f.get("target_value") or "0").strip() or 0)
    except ValueError:
        tv = 0.0
    vals = {
        "cid": _cid(request), "name": name, "metric": metric,
        "cust": (f.get("customer_name") or "").strip() or None,
        "period": (f.get("period") or "DAILY").strip(),
        "tv": tv, "unit": (f.get("unit") or "").strip() or None,
        "efrom": (f.get("effective_from") or "").strip() or None,
        "eto": (f.get("effective_to") or "").strip() or None,
        "status": (f.get("status") or "Active").strip(),
    }
    tid = (f.get("id") or "").strip()
    try:
        if tid:
            vals["id"] = int(tid)
            db.execute(text("""
                UPDATE performance_targets SET target_name=:name, metric_code=:metric,
                  customer_name=:cust, period=:period, target_value=:tv, unit=:unit,
                  effective_from=:efrom, effective_to=:eto, status=:status,
                  updated_at=NOW() WHERE id=:id
            """), vals)
        else:
            db.execute(text("""
                INSERT INTO performance_targets(company_id, target_name, metric_code,
                  customer_name, period, target_value, unit, effective_from,
                  effective_to, status, created_at, updated_at)
                VALUES(:cid,:name,:metric,:cust,:period,:tv,:unit,:efrom,:eto,
                       :status,NOW(),NOW())
            """), vals)
        db.commit()
    except Exception as exc:
        return RedirectResponse(f"/system/targets?error={exc.__class__.__name__}",
                                status_code=HTTP_303_SEE_OTHER)
    return RedirectResponse("/system/targets?toast=success&title=Saved&msg=Target+saved.",
                            status_code=HTTP_303_SEE_OTHER)


# ===========================================================================
# Batch 171 — OPERATIONS OVERVIEW
#
# A separate router with no /system prefix: this is a dashboard, not config.
#
# Shipped as a NEW page rather than a rewrite of Command Center. The existing
# dashboard is in daily use and holds a lot of working detail; replacing it in
# one step would mean judging every panel on it sight-unseen. This is the
# "answer in 5-10 seconds" layer from your brief, side by side with the old one
# so you can compare before anything is retired.
# ===========================================================================
ops_router = APIRouter(tags=["Operations Overview"])


def _section_board(db: Session, cid: int) -> list:
    try:
        from app.services.section_board import build as _build
        return _build(db, cid)
    except Exception:
        db.rollback()
        return []


# ---------------------------------------------------------------------------
# Batch 172 — ROLE-BASED VIEWS
#
# The same panels, arranged for who is looking. A head chef does not need
# procurement's material picture and an executive does not need the pipeline
# stage counts.
#
# Two deliberate constraints:
#
#   * A view HIDES panels, it never grants access. RBAC still decides what a
#     user may open — a view that could widen access would be a security hole
#     dressed as a preference.
#
#   * Roles map to the ones that actually exist in rbac.py (HEAD_CHEF,
#     HEAD_CHEF_PLANNING, MANAGER, SUPERVISOR, ADMIN...). Inventing role names
#     to match a design document would give every real user the fallback view.
#
# ?view= overrides the default, so anyone can look at another arrangement
# without changing their role — useful when a manager covers a shift.
# ---------------------------------------------------------------------------
OPS_VIEWS = {
    "operations": {
        "label": "Operations",
        # Batch 217: the section live board is the panel a production floor
        # actually watches, so it sits directly under the KPI strip.
        "panels": ["kpis", "board", "attention", "today", "pipeline", "health",
                   "targets", "recent"],
    },
    "head_chef": {
        "label": "Head Chef",
        # Production load and quality; no margin, no dispatch documents.
        "panels": ["kpis", "board", "attention", "today", "pipeline", "recent"],
        "kpis": ["open_orders", "in_production", "qc_pending", "store_pending"],
    },
    "production": {
        "label": "Production Manager",
        "panels": ["kpis", "board", "attention", "pipeline", "health", "today", "targets"],
        "kpis": ["open_orders", "in_production", "qc_pending",
                 "pending_dispatch", "store_pending"],
    },
    "procurement": {
        "label": "Procurement",
        # Material demand only — the pipeline and QC are somebody else's problem.
        "panels": ["kpis", "attention", "recent"],
        "kpis": ["open_orders", "store_pending"],
    },
    "quality": {
        "label": "Quality",
        "panels": ["kpis", "board", "attention", "today", "recent"],
        "kpis": ["qc_pending", "in_production", "open_orders"],
    },
    "executive": {
        "label": "Executive",
        # Money and delivery performance; no operational queues.
        "panels": ["kpis", "health", "targets", "recent"],
        "kpis": ["open_orders", "in_production", "pending_dispatch", "margin"],
    },
}

# Real rbac.py roles -> default view.
ROLE_DEFAULT_VIEW = {
    "HEAD_CHEF": "head_chef",
    "HEAD_CHEF_PLANNING": "head_chef",
    "SUPERVISOR": "production",
    "MANAGER": "production",
    "ADMIN": "operations",
    "ADMINISTRATOR": "operations",
    "SUPER_ADMIN": "executive",
    "SUPERADMIN": "executive",
}


@ops_router.get("/dashboard/attention/{key}")
def attention_drill(request: Request, key: str, db: Session = Depends(get_db)):
    """Batch 232 — the orders behind one Needs Attention tile.

    The tiles used to navigate to a whole screen, which loses the board on a
    wall display and is a heavy answer to "which orders?". This returns just
    the list; the tile opens it in a side panel, and the full screen is still
    one click further.
    """
    require_area(request, "dashboard")
    from fastapi.responses import JSONResponse

    from app.core.company import company_clause
    from app.core.db_read import rows as _rows

    cid = int(request.session.get("company_id") or 1)
    scope = company_clause("co", param="scope_cid")
    SQL = {
        "qc": ("Orders waiting for QC", f"""
            SELECT co.order_no, co.customer_name, COALESCE(co.status,'') AS status,
                   co.required_delivery_date AS due,
                   COALESCE(co.total_planned_portions,0) AS portions
            FROM customer_orders co
            WHERE COALESCE(co.status,'') = 'In Production'
              AND NOT EXISTS (SELECT 1 FROM qc_checks q WHERE q.order_no = co.order_no
                              AND UPPER(COALESCE(q.qc_status,'')) = 'PASSED') {scope}
            ORDER BY co.required_delivery_date LIMIT 50"""),
        "store": ("Store lines not finalized", f"""
            SELECT co.order_no, co.customer_name, COALESCE(co.status,'') AS status,
                   co.required_delivery_date AS due,
                   COUNT(s.id) AS portions
            FROM customer_orders co
            JOIN store_issuance_lines s ON s.order_no = co.order_no
            WHERE COALESCE(s.finalized,0) = 0 {scope}
            GROUP BY co.order_no, co.customer_name, co.status, co.required_delivery_date
            ORDER BY co.required_delivery_date LIMIT 50"""),
        "sla": ("Orders past their SLA deadline", f"""
            SELECT co.order_no, co.customer_name, COALESCE(co.status,'') AS status,
                   co.required_delivery_date AS due,
                   COALESCE(co.total_planned_portions,0) AS portions
            FROM customer_orders co
            WHERE COALESCE(co.status,'') NOT IN ('Delivered','Closed','Cancelled','Rejected')
              AND co.required_delivery_date < CURDATE() {scope}
            ORDER BY co.required_delivery_date LIMIT 50"""),
        "dispatch": ("Orders pending dispatch", f"""
            SELECT co.order_no, co.customer_name, COALESCE(pd.dispatch_status,'') AS status,
                   co.required_delivery_date AS due,
                   COALESCE(pd.packed_bags,0) AS portions
            FROM packing_dispatch pd
            JOIN customer_orders co ON co.order_no = pd.order_no
            WHERE COALESCE(pd.dispatch_status,'') IN ('Packed','Assigned','Out for Delivery') {scope}
            ORDER BY co.required_delivery_date LIMIT 50"""),
    }
    if key not in SQL:
        return JSONResponse({"title": "", "rows": []}, status_code=404)
    title, sql = SQL[key]
    rows = _rows(db, sql, {"scope_cid": cid}, label=f"attention drill {key}")
    for r in rows:
        r["due"] = str(r.get("due") or "")
        r["portions"] = float(r.get("portions") or 0)
    return JSONResponse({"title": title, "rows": rows})


@ops_router.get("/dashboard/operations")
async def operations_overview(request: Request, db: Session = Depends(get_db)):
    require_area(request, "dashboard")
    cid = _cid(request)

    # Batch 172: resolve the view. An unknown ?view= falls back to the role
    # default rather than erroring — a bad bookmark should not break a
    # dashboard.
    from app.core.rbac import normalized_role
    role = normalized_role(request)
    requested = (request.query_params.get("view") or "").strip().lower()
    view_key = requested if requested in OPS_VIEWS else ROLE_DEFAULT_VIEW.get(role, "operations")
    view = OPS_VIEWS[view_key]
    panels = set(view["panels"])
    kpi_keys = view.get("kpis")

    # Reconcile SLA instances on load — see sync_open_orders() for why this is
    # a reconciliation pass rather than hooks on five status transitions.
    from app.services.sla_service import (
        delivery_health as _health, sync_open_orders as _sync,
    )
    _sync(db, company_id=cid)
    health = _health(db, company_id=cid)

    def _one(sql: str, params: dict | None = None):
        # Batch 220: log the failure. A silent except here is how the Recent
        # Orders bug (Batch 212) hid — a panel that reads 0 looks identical to
        # a panel whose query was rejected.
        try:
            return db.execute(text(sql), params or {}).scalar()
        except Exception as exc:
            logger.warning("operations panel query failed: %s", str(exc).splitlines()[0][:200])
            db.rollback()
            return None

    cp = {"cid": cid}
    scope = "(co.company_id = :cid OR co.company_id IS NULL)"

    # ---- Level 1: what is happening ----------------------------------------
    kpis = {
        "open_orders": _one(f"SELECT COUNT(*) FROM customer_orders co WHERE {scope} "
                            "AND COALESCE(co.status,'') NOT IN "
                            "('Delivered','Closed','Cancelled','Rejected')", cp),
        "in_production": _one(f"SELECT COUNT(*) FROM customer_orders co WHERE {scope} "
                              "AND COALESCE(co.status,'') = 'In Production'", cp),
        "qc_pending": _one(f"SELECT COUNT(*) FROM customer_orders co WHERE {scope} "
                           "AND COALESCE(co.status,'') IN ('QC Pending','Packing Pending')", cp),
        "pending_dispatch": _one("SELECT COUNT(*) FROM packing_dispatch "
                                 "WHERE COALESCE(dispatch_status,'') NOT IN "
                                 "('Delivered','Closed','Out for Delivery','Dispatched')"),
        "store_pending": _one("SELECT COUNT(*) FROM store_issuance_lines "
                              "WHERE COALESCE(finalized,0) = 0"),
        "margin": _one(f"""SELECT COALESCE(SUM(COALESCE(ol.required_portions,0) *
                              (COALESCE(r.sale_price_per_portion,0) -
                               COALESCE(r.food_cost_per_portion,0))),0)
                           FROM order_lines ol
                           JOIN customer_orders co ON co.order_no = ol.order_no
                           LEFT JOIN recipes r ON r.recipe_code = ol.recipe_no
                           WHERE {scope}
                             AND COALESCE(co.status,'') NOT IN
                                 ('Delivered','Closed','Cancelled','Rejected')""", cp),
    }

    # ---- Level 2: what needs action ----------------------------------------
    # Every item carries a link. An alert you cannot act on from where you read
    # it is just a number with a colour.
    # Batch 212: each item carries an icon so the tiles read as a dashboard
    # rather than a list of sentences.
    attention = [
        # Batch 232: `key` drives the side panel; `url` is still the full screen
        # behind "Open the full list", so nothing that worked before stops working.
        {"key": "qc", "tone": "danger", "icon": "bi-patch-question", "count": kpis["qc_pending"],
         "label": "Orders waiting for QC",
         "detail": "QC checks have not been completed.", "url": "/qc"},
        {"key": "store", "tone": "warning", "icon": "bi-box-seam", "count": kpis["store_pending"], "label": "Store lines not finalized",
         "detail": "Material demand is waiting for store confirmation.",
         "url": "/production/store-issuance"},
        {"key": "sla", "tone": "warning", "icon": "bi-alarm", "count": health.get("overdue", 0) + health.get("breached", 0),
         "label": "Orders past their SLA deadline",
         "detail": "Delivery commitment has been missed or is at grace.",
         "url": "/sales-requests?scope=all"},
        {"key": "dispatch", "tone": "info", "icon": "bi-truck", "count": kpis["pending_dispatch"], "label": "Orders pending dispatch",
         "detail": "Dispatch documents are not completed.", "url": "/dispatch"},
    ]
    attention = [a for a in attention if (a["count"] or 0) > 0]

    # ---- Level 3: where the work is ----------------------------------------
    # Each stage links to its queue, per the brief.
    stages = [
        ("Submitted", "Submitted", "/sales-requests?status=Pending"),
        ("BOM Generated", "BOM Generated", "/production/orders"),
        ("In Production", "In Production", "/production/orders"),
        ("Packing Pending", "Packing Pending", "/packing"),
        ("Out for Delivery", "Out for Delivery", "/dispatch"),
        ("Delivered", "Delivered", "/dispatch"),
    ]
    pipeline = []
    for label, status, url in stages:
        pipeline.append({
            "label": label, "url": url,
            "count": _one(f"SELECT COUNT(*) FROM customer_orders co WHERE {scope} "
                          "AND COALESCE(co.status,'') = :st", {**cp, "st": status}) or 0,
        })
    pipeline_max = max([p["count"] for p in pipeline] or [0]) or 1

    # ---- Today ------------------------------------------------------------
    today = {
        "received": _one(f"SELECT COUNT(*) FROM customer_orders co WHERE {scope} "
                         "AND DATE(co.order_date) = CURDATE()", cp),
        "in_production": kpis["in_production"],
        # Batch 220: these three counted EVERY company's QC checks, packing and
        # dispatch rows — the only unscoped queries left on this screen, and
        # the reason "Today's Production" could show activity to a company that
        # had none (flagged in Batch 212).
        "qc_done": _one("SELECT COUNT(*) FROM qc_checks q "
                        "WHERE DATE(q.created_at) = CURDATE() "
                        "AND (q.company_id = :cid OR q.company_id IS NULL)", cp),
        "packed": _one("SELECT COUNT(*) FROM packing_dispatch pd "
                       "WHERE COALESCE(pd.packing_status,'') = 'Packed' "
                       "AND DATE(pd.updated_at) = CURDATE() "
                       "AND (pd.company_id = :cid OR pd.company_id IS NULL)", cp),
        "dispatched": _one("SELECT COUNT(*) FROM packing_dispatch pd "
                           "WHERE COALESCE(pd.dispatch_status,'') IN "
                           "('Out for Delivery','Delivered','Dispatched') "
                           "AND DATE(pd.updated_at) = CURDATE() "
                           "AND (pd.company_id = :cid OR pd.company_id IS NULL)", cp),
    }
    _rec = today.get("received") or 0
    _disp = today.get("dispatched") or 0
    # Completion is only meaningful with a denominator; 0/0 is not 0%.
    today["completion"] = round(_disp / _rec * 100) if _rec else None

    targets = evaluate_targets(db, None, None, company_id=cid)

    # ------------------------------------------------------------------
    # Batch 212 (Image 3) — "Recent Orders shows nothing though we have a lot
    # of orders". ROOT CAUSE: this SELECT asked for `co.total_portions`. The
    # column is `total_planned_portions`; `total_portions` has never existed.
    # MySQL rejected the whole statement, _rows() swallowed it, and the panel
    # rendered its empty state — which looks exactly like "no orders yet".
    #
    # The second half of the bug is why nobody caught it: _rows() returns an
    # error alongside the rows precisely so a failure can be told apart from an
    # empty table (see its docstring), and this call discarded it with `_`.
    # The error is now kept and shown, so a broken panel says it is broken.
    #
    # ORDER BY also moved to COALESCE(order_date, created_at): orders imported
    # without an order_date sorted last regardless of age.
    # ------------------------------------------------------------------
    recent, recent_err = _rows(db, f"""
        SELECT co.order_no, co.customer_name, COALESCE(co.status,'') AS status,
               co.required_delivery_date,
               COALESCE(co.total_planned_portions, 0) AS total_portions,
               COALESCE(co.brand,'') AS brand,
               COALESCE(co.total_estimated_selling_value, 0) AS order_value
        FROM customer_orders co WHERE {scope}
        ORDER BY COALESCE(co.order_date, DATE(co.created_at)) DESC, co.id DESC
        LIMIT 10
    """, cp)

    return render(request, "sla/operations.html", {
        "panels": panels, "kpi_keys": kpi_keys, "view_key": view_key,
        "views": OPS_VIEWS, "view_label": view["label"], "role": role,
        "kpis": kpis, "attention": attention, "pipeline": pipeline,
        "pipeline_max": pipeline_max, "today": today, "health": health,
        "targets": targets, "recent": recent, "recent_err": recent_err,
        # Batch 217 — built only when the view asks for it; on the Executive
        # view it is neither shown nor queried.
        "board": (_section_board(db, cid) if "board" in panels else []),
        "page_title": "Operations Overview"})
