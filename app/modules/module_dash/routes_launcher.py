# app/modules/module_dash/routes_launcher.py
# =============================================================================
# Batch 65 — MODULE LAUNCHER data (professional cards + per-card sparklines)
# -----------------------------------------------------------------------------
# The launcher landing page shows one card per ENABLED module (module_visibility
# + RBAC gated in the template). Each card carries:
#   * a headline metric + a small delta,
#   * a 12-point sparkline series (rendered as an inline SVG polyline, exactly
#     like the "stock dashboard" reference), so the grid feels alive without
#     heavy chart libs on the landing page.
#
# build_launcher_context(db) returns:
#   stats  : the KPI-tile numbers (top strip)
#   cards  : { module_key: {value, delta, trend[], color} }
#
# Every query has a safe fallback so a fresh / partial DB never breaks the page.
# =============================================================================

import json
from datetime import date, timedelta
import logging

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.sql_scope import scoped

logger = logging.getLogger(__name__)


def _n(db: Session, sql: str, params: dict | None = None, cid: int | None = None) -> float:
    """Batch 220: pass cid= and the company condition is added to the outer
    query (app/core/sql_scope). Every launcher tile now does."""
    if cid is not None:
        sql, params = scoped(sql, params, cid)
    try:
        return float(db.execute(text(sql), params or {}).scalar() or 0)
    except Exception as exc:
        logger.warning("launcher tile query failed: %s", str(exc).splitlines()[0][:200])
        db.rollback()
        return 0.0


def _daily_series(db: Session, sql_template: str, days: int = 12,
                  cid: int | None = None) -> list[float]:
    """Run a per-day COUNT/SUM for the last `days` days. `sql_template` must
    accept a :d param and return a single scalar for that day. Returns a list
    oldest->newest.

    Batch 220 — TWO changes:

    1. Company scope. Pass cid= and each day's query is scoped like every
       other launcher query.
    2. NO MORE INVENTED DATA. This used to return a gentle upward curve
       whenever the query failed or every day was zero, "so the sparkline
       still draws". The result was tiles reading a confident +85.7% on a
       database with nothing in it (flagged in Batch 213). A sparkline that
       lies is worse than no sparkline: it is read as a trend. A genuinely
       empty period now returns zeros, the tile shows no delta, and a FAILED
       query is logged rather than dressed up as growth.
    """
    out: list[float] = []
    failed = False
    today = date.today()
    for i in range(days - 1, -1, -1):
        d = today - timedelta(days=i)
        sql, params = (scoped(sql_template, {"d": d.isoformat()}, cid)
                       if cid is not None else (sql_template, {"d": d.isoformat()}))
        try:
            out.append(round(float(db.execute(text(sql), params).scalar() or 0), 2))
        except Exception as exc:
            if not failed:
                logger.warning("launcher sparkline query failed: %s",
                               str(exc).splitlines()[0][:200])
            db.rollback()
            failed = True
            out.append(0.0)
    return out


def _delta(series: list[float]) -> float:
    """Percentage change first-half vs second-half of the series.

    Batch 220: an all-zero series returns 0.0 rather than a number, so a tile
    with no activity shows no arrow instead of a fictional trend.
    """
    if not series or len(series) < 4 or sum(series) == 0:
        return 0.0
    half = len(series) // 2
    a = sum(series[:half]) or 1.0
    b = sum(series[half:])
    return round((b - a) / a * 100.0, 1)


def _synthetic_warning(series: list[float]) -> bool:
    """Kept for compatibility. Batch 220 removed the synthetic curve, so this
    is always False now — no series is invented any more."""
    return False


def command_centre_card(db: Session, cid: int) -> dict:
    """Batch 213 (Image 1) — the launcher's headline card.

    Eight module tiles told you the system was busy but not how the business is
    doing. This card answers that in one line, and links to the Command Center
    where the same numbers can be filtered and drilled.

    It deliberately reuses app/services/command_center.py rather than writing
    its own SQL: the launcher and the Command Center must never disagree about
    the same figure, and the service is already company-scoped (Batch 203).
    """
    from app.services import command_center as cc

    f = cc.parse_filters({})                      # default: last 30 + next 30 days
    cards, periods = cc.kpi_cards(db, f, cid)
    by_key = {c["key"]: c for c in cards}
    cur = periods["current"]

    def pick(key, fmt="int"):
        c = by_key.get(key) or {}
        v = c.get("value")
        if v is None:
            disp = "—"
        elif fmt == "pct":
            disp = f"{v:.1f}%"
        elif fmt == "money":
            disp = f"{v/1000000:.2f}M" if abs(v) >= 1e6 else (f"{v/1000:.1f}K" if abs(v) >= 1000 else f"{v:.0f}")
        else:
            disp = f"{int(v):,}"
        # The Command Center sparkline carries None for days with no
        # denominator (a rate with nothing to divide). The launcher's inline
        # SVG does min()/max() over the list, which throws on None, so carry
        # the previous value forward for drawing only — the card's own value
        # is unaffected.
        spark, last = [], 0.0
        for pt in (c.get("spark") or []):
            if pt is None:
                spark.append(last)
            else:
                last = float(pt)
                spark.append(last)
        return {"key": key, "title": c.get("title", key), "value": disp, "spark_draw": spark,
                "delta": c.get("delta"), "tone": c.get("tone"), "unit": c.get("unit", ""),
                "target": c.get("target"), "color": c.get("color", "#1e5bb8")}

    return {
        "period": f"{f['date_from']} → {f['date_to']}",
        "orders": cur.get("orders", 0),
        "portions": round(cur.get("portions", 0) or 0),
        "delayed": cur.get("delayed", 0),
        "open_orders": cur.get("open_orders", 0),
        "metrics": [pick("sale", "money"), pick("margin_pct", "pct"),
                    pick("on_time_pct", "pct"), pick("waste_pct", "pct"),
                    pick("qc_pass_pct", "pct"), pick("delayed")],
        "labels": periods.get("daily_labels", []),
    }


def build_launcher_context(db: Session, cid: int = 1) -> dict:
    # Batch 220 — every tile below is company-scoped. They used to count every
    # company's orders, POs, recipes and users, which meant the launcher and
    # the Command Center card above it disagreed on a multi-company install
    # (flagged in Batch 213). scope_sql() adds the condition to each query;
    # tables with no company_id column are returned unchanged.
    # ---- top KPI strip -------------------------------------------------------
    stats = {
        "open_orders": int(_n(db, "SELECT COUNT(*) FROM customer_orders "
                                  "WHERE COALESCE(status,'') NOT IN ('Delivered','Closed','Cancelled')", cid=cid)),
        "inventory_items": int(_n(db, "SELECT COUNT(*) FROM ingredients", cid=cid)),
        "stock_value": round(_n(db, "SELECT COALESCE(SUM(COALESCE(current_stock,0)*COALESCE(unit_cost,0)),0) FROM ingredients", cid=cid), 0),
        "open_pos": int(_n(db, "SELECT COUNT(*) FROM purchase_orders "
                               "WHERE COALESCE(status,'') NOT IN ('Closed','Cancelled')", cid=cid)),
        "ar_open": round(_n(db, "SELECT COALESCE(SUM(amount-COALESCE(paid_amount,0)),0) FROM ar_invoices WHERE COALESCE(status,'') <> 'Paid'", cid=cid), 0),
        "customers": int(_n(db, "SELECT COUNT(*) FROM customers", cid=cid)),
    }

    # ---- per-card metric + sparkline ----------------------------------------
    prod_series = _daily_series(db, "SELECT COUNT(*) FROM customer_orders WHERE DATE(created_at)=:d", cid=cid)
    inv_series = _daily_series(db, "SELECT COALESCE(SUM(COALESCE(qty,quantity,0)),0) FROM inventory_transactions WHERE DATE(created_at)=:d", cid=cid)
    proc_series = _daily_series(db, "SELECT COUNT(*) FROM purchase_orders WHERE DATE(created_at)=:d", cid=cid)
    fin_series = _daily_series(db, "SELECT COALESCE(SUM(amount),0) FROM ar_invoices WHERE DATE(created_at)=:d", cid=cid)

    def card(value, series, color, fmt="int"):
        if fmt == "money":
            disp = f"{value:,.0f}"
        else:
            disp = f"{int(value):,}"
        return {
            "value": disp,
            "delta": _delta(series),
            "trend": json.dumps(series),
            "color": color,
        }

    cards = {
        "sales": card(int(_n(db, "SELECT COUNT(*) FROM customer_orders "
                                 "WHERE COALESCE(status,'') NOT IN ('Delivered','Closed','Cancelled')", cid=cid)),
                      prod_series, "success"),
        "production": card(stats["open_orders"], prod_series, "primary"),
        "inventory": card(stats["inventory_items"], inv_series, "info"),
        "procurement": card(stats["open_pos"], proc_series, "warning"),
        "recipes": card(int(_n(db, "SELECT COUNT(*) FROM recipes", cid=cid)),
                        _daily_series(db, "SELECT COUNT(*) FROM recipes WHERE DATE(created_at)=:d", cid=cid), "primary"),
        "masters": card(stats["customers"],
                        _daily_series(db, "SELECT COUNT(*) FROM customers WHERE DATE(created_at)=:d", cid=cid), "danger"),
        "reports": card(int(_n(db, "SELECT COUNT(*) FROM customer_orders", cid=cid)),
                        prod_series, "secondary"),
        "projects": card(int(_n(db, "SELECT COUNT(*) FROM projects", cid=cid)),
                         _daily_series(db, "SELECT COUNT(*) FROM projects WHERE DATE(created_at)=:d", cid=cid), "info"),
        "finance": card(stats["ar_open"], fin_series, "success", fmt="money"),
        "hcm": card(int(_n(db, "SELECT COUNT(*) FROM hr_employees", cid=cid)),
                    _daily_series(db, "SELECT COUNT(*) FROM hr_employees WHERE DATE(created_at)=:d", cid=cid), "primary"),
        "customer_portal": card(stats["customers"],
                                _daily_series(db, "SELECT COUNT(*) FROM customers WHERE DATE(created_at)=:d", cid=cid), "success"),
        "users": card(int(_n(db, "SELECT COUNT(*) FROM users", cid=cid)),
                      _daily_series(db, "SELECT COUNT(*) FROM users WHERE DATE(created_at)=:d", cid=cid), "dark"),
    }

    return {"stats": stats, "cards": cards}
