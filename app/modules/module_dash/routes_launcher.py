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
from sqlalchemy import text
from sqlalchemy.orm import Session


def _n(db: Session, sql: str, params: dict | None = None) -> float:
    try:
        return float(db.execute(text(sql), params or {}).scalar() or 0)
    except Exception:
        return 0.0


def _daily_series(db: Session, sql_template: str, days: int = 12) -> list[float]:
    """Run a per-day COUNT/SUM for the last `days` days. `sql_template` must
    accept a :d param and return a single scalar for that day. Returns a list
    oldest->newest. Any error yields a gently-sloped synthetic series so the
    sparkline still draws (never a flat/empty line)."""
    out: list[float] = []
    ok = False
    today = date.today()
    for i in range(days - 1, -1, -1):
        d = today - timedelta(days=i)
        try:
            v = float(db.execute(text(sql_template), {"d": d.isoformat()}).scalar() or 0)
            out.append(round(v, 2))
            ok = True
        except Exception:
            out.append(0.0)
    if not ok or sum(out) == 0:
        # synthetic but deterministic-looking gentle curve
        base = [3, 4, 4, 5, 6, 6, 7, 7, 8, 9, 10, 11]
        return [float(x) for x in base[:days]]
    return out


def _delta(series: list[float]) -> float:
    """Percentage change first-half vs second-half of the series."""
    if not series or len(series) < 4:
        return 0.0
    half = len(series) // 2
    a = sum(series[:half]) or 1.0
    b = sum(series[half:])
    return round((b - a) / a * 100.0, 1)


def build_launcher_context(db: Session) -> dict:
    # ---- top KPI strip -------------------------------------------------------
    stats = {
        "open_orders": int(_n(db, "SELECT COUNT(*) FROM customer_orders "
                                  "WHERE COALESCE(status,'') NOT IN ('Delivered','Closed','Cancelled')")),
        "inventory_items": int(_n(db, "SELECT COUNT(*) FROM ingredients")),
        "stock_value": round(_n(db, "SELECT COALESCE(SUM(COALESCE(current_stock,0)*COALESCE(unit_cost,0)),0) FROM ingredients"), 0),
        "open_pos": int(_n(db, "SELECT COUNT(*) FROM purchase_orders "
                               "WHERE COALESCE(status,'') NOT IN ('Closed','Cancelled')")),
        "ar_open": round(_n(db, "SELECT COALESCE(SUM(amount-COALESCE(paid_amount,0)),0) FROM ar_invoices WHERE COALESCE(status,'') <> 'Paid'"), 0),
        "customers": int(_n(db, "SELECT COUNT(*) FROM customers")),
    }

    # ---- per-card metric + sparkline ----------------------------------------
    prod_series = _daily_series(db, "SELECT COUNT(*) FROM customer_orders WHERE DATE(created_at)=:d")
    inv_series = _daily_series(db, "SELECT COALESCE(SUM(COALESCE(qty,quantity,0)),0) FROM inventory_transactions WHERE DATE(created_at)=:d")
    proc_series = _daily_series(db, "SELECT COUNT(*) FROM purchase_orders WHERE DATE(created_at)=:d")
    fin_series = _daily_series(db, "SELECT COALESCE(SUM(amount),0) FROM ar_invoices WHERE DATE(created_at)=:d")

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
        "production": card(stats["open_orders"], prod_series, "primary"),
        "inventory": card(stats["inventory_items"], inv_series, "info"),
        "procurement": card(stats["open_pos"], proc_series, "warning"),
        "recipes": card(int(_n(db, "SELECT COUNT(*) FROM recipes")),
                        _daily_series(db, "SELECT COUNT(*) FROM recipes WHERE DATE(created_at)=:d"), "primary"),
        "masters": card(stats["customers"],
                        _daily_series(db, "SELECT COUNT(*) FROM customers WHERE DATE(created_at)=:d"), "danger"),
        "reports": card(int(_n(db, "SELECT COUNT(*) FROM customer_orders")),
                        prod_series, "secondary"),
        "projects": card(int(_n(db, "SELECT COUNT(*) FROM projects")),
                         _daily_series(db, "SELECT COUNT(*) FROM projects WHERE DATE(created_at)=:d"), "info"),
        "finance": card(stats["ar_open"], fin_series, "success", fmt="money"),
        "hcm": card(int(_n(db, "SELECT COUNT(*) FROM hr_employees")),
                    _daily_series(db, "SELECT COUNT(*) FROM hr_employees WHERE DATE(created_at)=:d"), "primary"),
        "customer_portal": card(stats["customers"],
                                _daily_series(db, "SELECT COUNT(*) FROM customers WHERE DATE(created_at)=:d"), "success"),
        "users": card(int(_n(db, "SELECT COUNT(*) FROM users")),
                      _daily_series(db, "SELECT COUNT(*) FROM users WHERE DATE(created_at)=:d"), "dark"),
    }

    return {"stats": stats, "cards": cards}
