# app/modules/module_dash/routes_launcher.py
# =============================================================================
# Batch 22 — MODULE LAUNCHER live data
# -----------------------------------------------------------------------------
# The old /modules page showed a hard-coded strip of KPI badges and a red
# annotation. This provides a real, dynamic launcher context: live KPI tiles
# and three chart datasets (orders trend, inventory value by category, AR vs AP)
# so the landing page shows charts with real data rather than static text.
#
# The launcher itself is still rendered by /modules in app/main.py, but that
# route now imports build_launcher_context() from here (see the patched main.py
# provided in this batch) to obtain the numbers + chart JSON.
# =============================================================================

import json
from sqlalchemy import text
from sqlalchemy.orm import Session


def _n(db: Session, sql: str) -> float:
    try:
        return float(db.execute(text(sql)).scalar() or 0)
    except Exception:
        return 0.0


def _series(db: Session, sql: str) -> tuple[list, list]:
    """Return (labels, values) for a label/value SELECT. Empty on any error."""
    try:
        rows = db.execute(text(sql)).mappings().all()
        labels = [str(r["label"]) for r in rows]
        values = [round(float(r["value"] or 0), 2) for r in rows]
        return labels, values
    except Exception:
        return [], []


def build_launcher_context(db: Session) -> dict:
    """KPIs + chart JSON strings ready for the template's data-* attributes."""
    stats = {
        "open_orders": int(_n(db, "SELECT COUNT(*) FROM customer_orders "
                                  "WHERE COALESCE(status,'') NOT IN ('Delivered','Closed','Cancelled')")),
        "inventory_items": int(_n(db, "SELECT COUNT(*) FROM ingredients")),
        "stock_value": round(_n(db, "SELECT COALESCE(SUM(COALESCE(current_stock,0)*COALESCE(unit_cost,0)),0) FROM ingredients"), 2),
        "open_pos": int(_n(db, "SELECT COUNT(*) FROM purchase_orders "
                               "WHERE COALESCE(status,'') NOT IN ('Closed','Cancelled')")),
        "ar_open": round(_n(db, "SELECT COALESCE(SUM(amount-COALESCE(paid_amount,0)),0) FROM ar_invoices WHERE COALESCE(status,'') <> 'Paid'"), 2),
        "ap_open": round(_n(db, "SELECT COALESCE(SUM(amount-COALESCE(paid_amount,0)),0) FROM ap_invoices WHERE COALESCE(status,'') <> 'Paid'"), 2),
        "customers": int(_n(db, "SELECT COUNT(*) FROM customers")),
        "suppliers": int(_n(db, "SELECT COUNT(*) FROM suppliers")),
    }

    # Chart 1 — order status distribution
    ol, ov = _series(db, """
        SELECT COALESCE(status,'Unknown') AS label, COUNT(*) AS value
        FROM customer_orders GROUP BY COALESCE(status,'Unknown')
        ORDER BY value DESC LIMIT 8
    """)
    # Chart 2 — inventory value by category
    il, iv = _series(db, """
        SELECT COALESCE(main_category,'Uncategorized') AS label,
               ROUND(SUM(COALESCE(current_stock,0)*COALESCE(unit_cost,0)),2) AS value
        FROM ingredients GROUP BY COALESCE(main_category,'Uncategorized')
        ORDER BY value DESC LIMIT 8
    """)
    # Chart 3 — receivable vs payable
    fl = ["Receivable (AR)", "Payable (AP)"]
    fv = [stats["ar_open"], stats["ap_open"]]

    # Fallbacks so charts never render empty on a fresh DB.
    if not ol:
        ol, ov = ["No orders yet"], [0]
    if not il:
        il, iv = ["No inventory"], [0]

    charts = {
        "orders": {"labels": json.dumps(ol), "values": json.dumps(ov)},
        "inventory": {"labels": json.dumps(il), "values": json.dumps(iv)},
        "finance": {"labels": json.dumps(fl), "values": json.dumps(fv)},
    }
    return {"stats": stats, "charts": charts}
