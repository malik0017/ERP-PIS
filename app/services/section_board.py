# app/services/section_board.py
# =============================================================================
# Batch 217 — KITCHEN SECTION LIVE BOARD
# -----------------------------------------------------------------------------
# The board from your Images 7–8: one column per kitchen section, one card per
# order inside it, each card carrying the customer, the portions, how long is
# left before it is due, a progress bar and a priority flag. It is the screen a
# production manager watches all shift, so everything on it has to be a fact
# about work in progress — not a count of rows.
#
# WHAT A CARD MEANS
#   * An order appears in a section while that section still holds work for it:
#     at least one transaction whose status is not TRANSFERRED/COMPLETED.
#   * progress  = lines finished ÷ lines held, so the bar answers "how far
#     through is this section with this order".
#   * due_min   = minutes until the order's cooking time (negative = overdue).
#     Cooking date, not delivery date: the kitchen's deadline is when the food
#     has to be cooked, and using delivery would make every card look relaxed
#     until the morning of dispatch.
#   * urgency   = critical (overdue) / high (< 60 min) / normal, and it is
#     derived from due_min, NOT from the order's priority field. A "Normal"
#     order that is forty minutes late is the urgent one on the floor.
#
# The board is company-scoped and read-only; it is built from
# kitchen_section_transactions, which every workstation already writes to.
# =============================================================================
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# Column order on the board = the order material physically moves through.
SECTION_ORDER = ["Store", "Cutting", "Butchery", "Hot Kitchen", "Cold Kitchen",
                 "Bakery/Pastry", "QC", "Trayline / Packing"]
SECTION_COLOR = {
    "Store": "#6b7a90", "Cutting": "#1f7a3f", "Butchery": "#b42343",
    "Hot Kitchen": "#c2410c", "Cold Kitchen": "#0b7285", "Bakery/Pastry": "#7048a8",
    "QC": "#0ea5c6", "Trayline / Packing": "#8a5a00",
}
DONE = ("TRANSFERRED", "COMPLETED")


def _f(v: Any) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def build(db: Session, cid: int, limit_per_section: int = 8) -> list[dict]:
    """One entry per section, each with its open order cards."""
    try:
        rows = db.execute(text("""
            SELECT k.current_section AS section, k.order_no,
                   MAX(o.customer_name) AS customer_name,
                   MAX(COALESCE(o.priority,'Normal')) AS priority,
                   MAX(COALESCE(o.total_planned_portions,0)) AS portions,
                   MAX(COALESCE(o.cooking_date, o.required_delivery_date)) AS due_date,
                   MAX(o.required_delivery_date) AS delivery_date,
                   COUNT(*) AS line_count,
                   SUM(CASE WHEN UPPER(COALESCE(k.transaction_status,'')) = 'TRANSFERRED'
                             OR UPPER(COALESCE(k.transaction_status,'')) LIKE 'COMPLETED%'
                            THEN 1 ELSE 0 END) AS done_lines,
                   COALESCE(SUM(k.received_qty_standard),0) AS received,
                   COALESCE(SUM(k.waste_qty_standard),0) AS waste,
                   MAX(k.received_at) AS last_touch
            FROM kitchen_section_transactions k
            JOIN customer_orders o ON o.order_no = k.order_no
            WHERE (k.company_id = :cid OR k.company_id IS NULL)
              AND COALESCE(o.status,'') NOT IN ('Cancelled','Rejected','Delivered','Closed')
            GROUP BY k.current_section, k.order_no
            HAVING done_lines < line_count
            ORDER BY due_date, k.order_no
        """), {"cid": cid}).mappings().all()
    except Exception as exc:
        logger.warning("section_board query failed: %s", str(exc).splitlines()[0][:300])
        db.rollback()
        return []

    now = datetime.now()
    by_section: dict[str, list] = {}
    for r in rows:
        line_count = int(_f(r["line_count"])) or 1
        done = int(_f(r["done_lines"]))
        due = r["due_date"]
        due_min = None
        if due is not None:
            due_dt = due if isinstance(due, datetime) else datetime.combine(due, datetime.min.time())
            due_min = int((due_dt - now).total_seconds() // 60)
        # Urgency comes from the clock, not the priority field — see header.
        if due_min is None:
            urgency = "normal"
        elif due_min < 0:
            urgency = "critical"
        elif due_min < 60:
            urgency = "high"
        else:
            urgency = "normal"
        by_section.setdefault(r["section"] or "—", []).append({
            "order_no": r["order_no"],
            "customer": r["customer_name"] or "—",
            "priority": r["priority"] or "Normal",
            "portions": round(_f(r["portions"])),
            "lines": line_count, "done": done,
            "progress": round(done / line_count * 100),
            "received": round(_f(r["received"]), 1),
            "waste": round(_f(r["waste"]), 1),
            "due_min": due_min, "urgency": urgency,
            "due_text": _due_text(due_min),
        })

    board = []
    known = [s for s in SECTION_ORDER if s in by_section]
    for name in known + sorted(s for s in by_section if s not in SECTION_ORDER):
        cards = by_section[name]
        # Most urgent first: overdue, then soonest due, then largest.
        cards.sort(key=lambda c: (c["due_min"] if c["due_min"] is not None else 10 ** 6,
                                  -c["portions"]))
        board.append({
            "section": name,
            "color": SECTION_COLOR.get(name, "#1e5bb8"),
            "total": len(cards),
            "critical": sum(1 for c in cards if c["urgency"] == "critical"),
            "high": sum(1 for c in cards if c["urgency"] == "high"),
            "portions": round(sum(c["portions"] for c in cards)),
            "cards": cards[:limit_per_section],
            "more": max(0, len(cards) - limit_per_section),
        })
    return board


def _due_text(due_min: int | None) -> str:
    if due_min is None:
        return "no date"
    if due_min < 0:
        m = -due_min
        return f"{m // 60}h {m % 60}m ago" if m >= 60 else f"{m}m ago"
    return f"in {due_min // 60}h {due_min % 60}m" if due_min >= 60 else f"in {due_min}m"
