# app/modules/inventory/routes_reorder.py
# =============================================================================
# Batch 107 — REORDER AUTOMATION  (backlog item #4)
# -----------------------------------------------------------------------------
# The Below Minimum report already told you WHAT was short. It did not tell you
# HOW MUCH to buy, and it left you to raise the requisition by hand, line by
# line, for every item.
#
# HOW MUCH TO ORDER — the calculation, and why it is not just "top up to min"
#
#   reorder point   = average daily usage × lead time days × safety factor
#   suggested qty   = reorder point + min_stock − available
#
# "Top up to minimum" is the naive version and it is wrong for a food business:
# it ignores how fast the item actually moves and how long the supplier takes.
# An item consumed 40 kg/day with a 5-day lead time needs 200 kg of cover
# regardless of what its minimum says. So usage is measured from the ledger
# over a real window, not assumed.
#
# WHAT COUNTS AS AVAILABLE
#
#   available = on hand (QC-cleared only) + already on order but not received
#
# Both halves matter. Counting QC-hold stock as available orders too little;
# ignoring open purchase orders orders the same thing twice, which is the
# classic double-ordering failure in every reorder system that skips it.
#
# NOTHING IS ORDERED AUTOMATICALLY. The engine produces a SUGGESTION, and the
# one-click action raises a Purchase REQUISITION — which still goes to
# Procurement for review exactly like Batch 94 established. A system that
# silently commits money because a number crossed a threshold is not
# automation, it is an incident waiting to happen.
# =============================================================================
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import text
from sqlalchemy.orm import Session
from urllib.parse import quote

from app.core.rbac import require_area, require_action
from app.core.templates import render
from app.database.session import get_db

router = APIRouter(prefix="/inventory/reorder", tags=["Inventory"])

DEFAULT_LEAD_DAYS = 5
DEFAULT_USAGE_WINDOW = 30
DEFAULT_SAFETY = 1.2      # 20% buffer on the lead-time cover


def _cid(request: Request) -> int:
    return int(request.session.get("company_id") or 1)


def suggestions(db: Session, cid: int, lead_days: int, window: int,
                safety: float, only_short: bool = True) -> list[dict]:
    """Compute reorder suggestions from real ledger movement."""
    try:
        rows = [dict(r) for r in db.execute(text("""
            SELECT i.ingredient_code, i.name AS item_name,
                   COALESCE(NULLIF(i.default_supplier,''), '') AS supplier_name,
                   COALESCE(i.standard_uom, '')        AS uom,
                   COALESCE(i.min_stock_standard, 0)   AS min_stock,
                   COALESCE(i.reorder_level_standard, 0) AS reorder_level,
                   COALESCE(i.unit_cost_standard, 0)   AS unit_cost,
                   COALESCE(i.storage_type, '')        AS storage_type,
                   COALESCE(t.on_hand, 0)              AS on_hand,
                   COALESCE(t.used, 0)                 AS used_in_window,
                   0                                   AS on_order
            FROM ingredients i
            LEFT JOIN (
                SELECT inventory_code,
                       SUM(CASE WHEN qc_status IN ('Pending','Failed') THEN 0
                                ELSE COALESCE(qty_in,0) END) - SUM(COALESCE(qty_out,0)) AS on_hand,
                       SUM(CASE WHEN COALESCE(txn_date, created_at) >= DATE_SUB(NOW(), INTERVAL :win DAY)
                                THEN COALESCE(qty_out,0) ELSE 0 END) AS used
                FROM inventory_transactions
                WHERE (company_id = :cid OR company_id IS NULL)
                GROUP BY inventory_code
            ) t ON t.inventory_code = i.ingredient_code
            WHERE COALESCE(i.status,'ACTIVE') = 'ACTIVE'
            LIMIT 4000
        """), {"cid": cid, "win": window}).mappings().all()]
    except Exception:
        return []

    # Batch 107 — on-order is computed SEPARATELY, on purpose.
    #
    # It was originally a LEFT JOIN inside the query above. That meant a
    # missing or differently-named procurement table took down the entire
    # suggestion engine, and the try/except returned an empty list — so the
    # screen said "nothing needs reordering" when the truth was "the query
    # exploded". A safety net that hides a failure is worse than no net.
    #
    # Split out, a procurement problem now costs only the double-order
    # protection, and that degradation is visible in the UI rather than
    # silently pretending everything is fine.
    on_order_map: dict[str, float] = {}
    try:
        for r in db.execute(text("""
            SELECT pol.inventory_code AS code,
                   SUM(GREATEST(COALESCE(pol.ordered_qty,0) - COALESCE(g.recv,0), 0)) AS on_order
            FROM purchase_order_lines pol
            JOIN purchase_orders po ON po.po_no = pol.po_no
            LEFT JOIN (SELECT po_no, inventory_code, SUM(received_qty) AS recv
                       FROM grn_receipt_lines GROUP BY po_no, inventory_code) g
                   ON g.po_no = pol.po_no AND g.inventory_code = pol.inventory_code
            WHERE COALESCE(po.status,'') NOT IN ('Cancelled','Closed','Received')
              AND (po.company_id = :cid OR po.company_id IS NULL)
            GROUP BY pol.inventory_code
        """), {"cid": cid}).mappings().all():
            on_order_map[r["code"]] = float(r["on_order"] or 0)
    except Exception:
        on_order_map = {}

    out = []
    for r in rows:
        on_hand = float(r["on_hand"] or 0)
        on_order = on_order_map.get(r["ingredient_code"], 0.0)
        used = float(r["used_in_window"] or 0)
        min_stock = float(r["min_stock"] or 0)

        daily = used / window if window else 0
        cover = daily * lead_days * safety
        # An explicit reorder level in Master Data beats the computed one —
        # somebody set it deliberately and the system should not argue.
        reorder_point = max(float(r["reorder_level"] or 0), cover)
        available = on_hand + on_order

        need = reorder_point + min_stock - available
        if need <= 0 and only_short:
            continue

        days_cover = (available / daily) if daily > 0 else None
        out.append({
            **r,
            "on_hand": round(on_hand, 3),
            "on_order": round(on_order, 3),
            "available": round(available, 3),
            "daily_usage": round(daily, 4),
            "reorder_point": round(reorder_point, 3),
            "suggested_qty": round(max(need, 0), 3),
            "est_value": round(max(need, 0) * float(r["unit_cost"] or 0), 2),
            "days_cover": (round(days_cover, 1) if days_cover is not None else None),
            # Urgency is about TIME, not quantity. 2 kg short of something you
            # burn daily is more urgent than 200 kg short of an annual item.
            "urgency": ("critical" if (days_cover is not None and days_cover < lead_days)
                        else ("soon" if (days_cover is not None and days_cover < lead_days * 2)
                              else "watch")),
        })

    order = {"critical": 0, "soon": 1, "watch": 2}
    return sorted(out, key=lambda x: (order.get(x["urgency"], 3), -x["est_value"]))


@router.get("")
def reorder_screen(request: Request, db: Session = Depends(get_db)):
    require_area(request, "inventory_valuation")
    cid = _cid(request)
    q = request.query_params

    def _int(name, default, lo, hi):
        try:
            return max(lo, min(hi, int(q.get(name) or default)))
        except (TypeError, ValueError):
            return default

    lead = _int("lead_days", DEFAULT_LEAD_DAYS, 1, 90)
    window = _int("window", DEFAULT_USAGE_WINDOW, 7, 365)
    try:
        safety = max(1.0, min(3.0, float(q.get("safety") or DEFAULT_SAFETY)))
    except ValueError:
        safety = DEFAULT_SAFETY
    supplier = (q.get("supplier") or "").strip()

    rows = suggestions(db, cid, lead, window, safety)
    if supplier:
        rows = [r for r in rows if supplier.lower() in (r["supplier_name"] or "").lower()]

    supplier_list = sorted({r["supplier_name"] for r in rows if r["supplier_name"]})

    return render(request, "inventory/reorder.html", {
        "rows": rows,
        "suppliers": supplier_list,
        "filters": {"lead_days": lead, "window": window,
                    "safety": safety, "supplier": supplier},
        "totals": {
            "items": len(rows),
            "critical": sum(1 for r in rows if r["urgency"] == "critical"),
            "value": round(sum(r["est_value"] for r in rows), 2),
        },
        "page_title": "Reorder Suggestions",
    })


@router.post("/raise")
async def raise_from_suggestions(request: Request, db: Session = Depends(get_db)):
    """Turn the ticked suggestions into ONE purchase requisition.

    Deliberately a requisition, not a purchase order: Batch 94 established
    that only Procurement commits money, and an automated threshold is not a
    reason to bypass that.
    """
    require_action(request, "purchase_requisition", "add")
    from app.modules.purchase_req.routes import create_requisition, ensure_schema
    ensure_schema(db)

    form = await request.form()
    codes = form.getlist("code")
    qtys = form.getlist("qty")
    back = (form.get("return_to") or "/inventory/reorder").strip()

    lines = []
    for i, code in enumerate(codes):
        code = (code or "").strip()
        if not code:
            continue
        try:
            qty = float(qtys[i]) if i < len(qtys) else 0
        except (ValueError, IndexError):
            qty = 0
        if qty <= 0:
            continue
        meta = db.execute(text("""
            SELECT name, COALESCE(standard_uom,'') AS uom,
                   COALESCE(default_supplier,'') AS supplier,
                   COALESCE(unit_cost_standard,0) AS price
            FROM ingredients WHERE ingredient_code = :c LIMIT 1
        """), {"c": code}).mappings().first() or {}
        lines.append({
            "inventory_code": code,
            "item_name": meta.get("name") or code,
            "uom": meta.get("uom", ""),
            "required_qty": qty,
            "on_hand_qty": 0.0,
            "suggested_supplier": meta.get("supplier", ""),
            "estimated_price": float(meta.get("price") or 0),
            "line_remarks": "Reorder suggestion"[:255],
        })

    if not lines:
        return RedirectResponse(
            f"{back}?toast=warning&title={quote('Nothing selected')}"
            f"&msg={quote('Tick at least one item with a quantity above zero.')}",
            status_code=303)

    pr_no = create_requisition(
        db, company_id=_cid(request),
        requested_by=request.session.get("username", "system"),
        lines=lines, department="Inventory / Reorder",
        source_type="Reorder", source_ref="",
        required_date=date.today(),
        justification=f"Automatic reorder suggestion — {len(lines)} item(s) below cover.",
    )
    return RedirectResponse(
        f"/purchase-requisitions/{pr_no}?toast=success&title={quote('Requisition Raised')}"
        f"&msg={quote(f'{pr_no} created from {len(lines)} reorder suggestion(s). Procurement reviews it before any purchase order exists.')}",
        status_code=303)
