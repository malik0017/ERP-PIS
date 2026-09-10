# app/services/command_center.py
# =============================================================================
# Batch 203 — PRODUCTION COMMAND CENTER (Images 14–19)
# -----------------------------------------------------------------------------
# One place that turns the GLOBAL FILTER (timeline, date basis, customer, brand,
# recipe, section, status, channel, priority, kitchen, comparison) into:
#   * KPI cards   — value, prior-period value, delta, target, daily sparkline
#   * KPI drawer  — definition, formula, drill records (side panel on click)
#   * Charts      — pipeline funnel, status, queue time by section, store
#                   issuance, recipe/customer mix
#   * Batch table — one row per order with stage, input/output/waste, cycle,
#                   delay and QC status
#
# Rules this module follows (so every number can be defended):
#   1. Every query is scoped to the session company AND to the same filtered
#      order set (`_scope`). The old dashboard counted customer_orders across
#      ALL companies — a multi-company leak — and mixed filtered and unfiltered
#      counts on the same screen.
#   2. The comparison period is the same length immediately before the window.
#   3. A KPI with no data returns None, never 0 — "no deliveries yet" is not
#      "0% on time".
#   4. Filters are parsed once (`parse_filters`) and are the same field names
#      the rest of the system can reuse (partials/global_filters.html).
# =============================================================================
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

import logging
import re

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

TIMELINES = [
    ("rolling30", "Last 30 + next 30 days"),
    ("all", "All dates"),
    ("today", "Today"),
    ("next7", "Next 7 days"),
    ("last7", "Last 7 days"),
    ("last30", "Last 30 days"),
    ("this_month", "This month"),
    ("custom", "Custom range"),
]
DATE_BASIS = {
    "delivery": ("Delivery date", "o.required_delivery_date"),
    "cooking": ("Cooking date", "COALESCE(o.cooking_date, o.required_delivery_date)"),
    "order": ("Order date", "COALESCE(o.order_date, DATE(o.created_at))"),
}
CLOSED = ("Delivered", "Dispatched", "Closed", "Cancelled", "Rejected")
SECTIONS = ["Cutting", "Butchery", "Hot Kitchen", "Cold Kitchen", "Bakery/Pastry", "QC", "Trayline / Packing"]
DEFAULT_TARGETS = {"margin_pct": 30.0, "food_cost_pct": 35.0, "on_time_pct": 96.0,
                   "waste_pct": 4.0, "qc_pass_pct": 98.0, "delayed": 0.0}


def _f(v: Any) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _d(s: str) -> date | None:
    try:
        return date.fromisoformat(s) if s else None
    except ValueError:
        return None


# --------------------------------------------------------------------------- filters
GF_KEYS = ("timeline", "date_from", "date_to", "basis", "customer", "brand", "recipe",
           "section", "order_status", "channel", "priority", "kitchen", "compare")


def parse_filters(q, default_timeline: str = "rolling30") -> dict:
    """Read the global filter from query params. Same names everywhere.

    Batch 204: list screens (QC, Packing, Dispatch, Store Issuance) default to
    timeline "all" so adding the bar changes nothing until the user filters.
    `active` is True only when the request carries global-filter values.
    """
    g = lambda k, dflt="": (q.get(k) or dflt).strip()  # noqa: E731
    f = {
        "timeline": g("timeline", default_timeline),
        "date_from": g("date_from"), "date_to": g("date_to"),
        "basis": g("basis", "delivery") if g("basis", "delivery") in DATE_BASIS else "delivery",
        "customer": g("customer"), "brand": g("brand"), "recipe": g("recipe"),
        # Batch 204: the query key is `order_status` — list screens already use
        # `status` for their own (QC / packing / dispatch) status filter.
        "section": g("section"), "status": g("order_status"), "channel": g("channel"),
        "priority": g("priority"), "kitchen": g("kitchen"),
        "compare": g("compare", "prior"),
    }
    today = date.today()
    tl = f["timeline"]
    if tl == "custom" and (_d(f["date_from"]) or _d(f["date_to"])):
        start = _d(f["date_from"]) or (_d(f["date_to"]) - timedelta(days=30))
        end = _d(f["date_to"]) or (start + timedelta(days=30))
    elif tl == "today":
        start = end = today
    elif tl == "next7":
        start, end = today, today + timedelta(days=6)
    elif tl == "last7":
        start, end = today - timedelta(days=6), today
    elif tl == "last30":
        start, end = today - timedelta(days=29), today
    elif tl == "all":
        # No date restriction. start/end only bound sparklines on the Command Center.
        start, end = today - timedelta(days=364), today + timedelta(days=60)
    elif tl == "this_month":
        start = today.replace(day=1)
        end = (start + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    else:
        f["timeline"] = "rolling30" if tl != "custom" else tl
        start, end = today - timedelta(days=30), today + timedelta(days=30)
    if end < start:
        start, end = end, start
    span = (end - start).days + 1
    f.update({"start": start, "end": end, "span": span,
              "prior_start": start - timedelta(days=span), "prior_end": start - timedelta(days=1)})
    f["order_status"] = f["status"]
    if f["timeline"] == "all":
        f["date_from"] = f["date_to"] = ""
    else:
        f["date_from"], f["date_to"] = start.isoformat(), end.isoformat()
    f["active"] = q.get("gf") == "1" or any(
        (q.get(k) or "").strip() for k in GF_KEYS if k not in ("timeline", "basis", "compare", "date_from", "date_to")
    ) or (f["timeline"] != default_timeline and bool(q.get("timeline")))
    return f


def _scope(f: dict, cid: int, prior: bool = False) -> tuple[str, dict]:
    """WHERE clause over customer_orders `o` for the filtered window."""
    col = DATE_BASIS[f["basis"]][1]
    p = {"cid": cid, "s": f["prior_start"] if prior else f["start"], "e": f["prior_end"] if prior else f["end"]}
    w = ["(o.company_id = :cid OR o.company_id IS NULL)"]
    if f["timeline"] != "all":
        w.append(f"{col} BETWEEN :s AND :e")
    if f["status"]:
        w.append("COALESCE(o.status,'') = :st"); p["st"] = f["status"]
    else:
        w.append("COALESCE(o.status,'') NOT IN ('Cancelled','Rejected')")
    for key, sql in (("customer", "o.customer_name = :cu"), ("brand", "COALESCE(o.brand,'') = :br"),
                     ("channel", "COALESCE(o.channel,'') = :ch"), ("priority", "COALESCE(o.priority,'') = :pr"),
                     ("kitchen", "COALESCE(o.kitchen,'') = :kt")):
        if f[key]:
            w.append(sql); p[sql.split(":")[1]] = f[key]
    if f["recipe"]:
        w.append("EXISTS (SELECT 1 FROM order_lines ol WHERE ol.order_no = o.order_no AND ol.recipe_no = :rc)")
        p["rc"] = f["recipe"]
    if f["section"]:
        w.append("(EXISTS (SELECT 1 FROM bom_lines bs WHERE bs.order_no = o.order_no AND bs.default_issue_section = :sec)"
                 " OR EXISTS (SELECT 1 FROM kitchen_section_transactions ks WHERE ks.order_no = o.order_no AND ks.current_section = :sec))")
        p["sec"] = f["section"]
    return " AND ".join(w), p


class _Q:
    """Query helper. A failing query degrades that widget to "no data" but is
    LOGGED — a silent except is how a reserved-word alias (`delayed`) zeroed
    four KPI cards during development of this batch without any visible error."""

    def __init__(self, db: Session):
        self.db = db

    def rows(self, sql: str, p: dict) -> list[dict]:
        try:
            return [dict(r) for r in self.db.execute(text(sql), p).mappings().all()]
        except Exception as exc:
            logger.warning("command_center query failed: %s", str(exc).splitlines()[0][:300])
            self.db.rollback()
            return []

    def one(self, sql: str, p: dict) -> dict:
        r = self.rows(sql, p)
        return r[0] if r else {}


# --------------------------------------------------------------------------- KPIs
def _period_metrics(q: _Q, f: dict, cid: int, prior: bool) -> dict:
    where, p = _scope(f, cid, prior)
    col = DATE_BASIS[f["basis"]][1]
    o = q.one(f"""
        SELECT COUNT(*) AS orders,
               COALESCE(SUM(o.total_estimated_selling_value),0) AS sale,
               COALESCE(SUM(o.total_estimated_food_cost),0) AS cost,
               COALESCE(SUM(o.total_planned_portions),0) AS portions,
               SUM(CASE WHEN COALESCE(o.status,'') NOT IN {CLOSED} THEN 1 ELSE 0 END) AS open_orders,
               SUM(CASE WHEN COALESCE(o.status,'') NOT IN {CLOSED}
                         AND o.required_delivery_date < CURDATE() THEN 1 ELSE 0 END) AS delayed_cnt
        FROM customer_orders o WHERE {where}""", p)
    w = q.one(f"""
        SELECT COALESCE(SUM(k.waste_qty_standard),0) AS waste,
               COALESCE(SUM(k.received_qty_standard),0) AS received
        FROM kitchen_section_transactions k JOIN customer_orders o ON o.order_no = k.order_no
        WHERE {where} AND k.current_section NOT IN ('QC','Trayline / Packing','Dispatch')""", p)
    qc = q.one(f"""
        SELECT SUM(CASE WHEN c.qc_status='Passed' THEN 1 ELSE 0 END) AS passed,
               SUM(CASE WHEN c.qc_status IN ('Passed','Rejected','Hold') THEN 1 ELSE 0 END) AS decided
        FROM qc_checks c JOIN customer_orders o ON o.order_no = c.order_no WHERE {where}""", p)
    dl = q.one(f"""
        SELECT COUNT(*) AS delivered,
               SUM(CASE WHEN pd.dispatch_date IS NOT NULL AND pd.dispatch_date <= o.required_delivery_date
                        THEN 1 ELSE 0 END) AS on_time
        FROM packing_dispatch pd JOIN customer_orders o ON o.order_no = pd.order_no
        WHERE {where} AND pd.dispatch_status = 'Delivered'""", p)
    sale, cost = _f(o.get("sale")), _f(o.get("cost"))
    return {
        "orders": int(_f(o.get("orders"))), "sale": sale, "cost": cost, "portions": _f(o.get("portions")),
        "open_orders": int(_f(o.get("open_orders"))), "delayed": int(_f(o.get("delayed_cnt"))),
        "margin": sale - cost,
        "margin_pct": ((sale - cost) / sale * 100) if sale else None,
        "food_cost_pct": (cost / sale * 100) if sale else None,
        "waste_pct": (_f(w.get("waste")) / _f(w.get("received")) * 100) if _f(w.get("received")) else None,
        "qc_pass_pct": (_f(qc.get("passed")) / _f(qc.get("decided")) * 100) if _f(qc.get("decided")) else None,
        "on_time_pct": (_f(dl.get("on_time")) / _f(dl.get("delivered")) * 100) if _f(dl.get("delivered")) else None,
        "delivered": int(_f(dl.get("delivered"))), "qc_decided": int(_f(qc.get("decided"))),
        "waste_qty": _f(w.get("waste")), "received_qty": _f(w.get("received")),
    }


def _daily(q: _Q, f: dict, cid: int) -> dict:
    """Daily series over the window for sparklines (one query per source)."""
    where, p = _scope(f, cid)
    col = DATE_BASIS[f["basis"]][1]
    days = [(f["start"] + timedelta(days=i)) for i in range(min(f["span"], 120))]
    idx = {d.isoformat(): i for i, d in enumerate(days)}
    series = {k: [0.0] * len(days) for k in ("sale", "cost", "orders", "delayed", "waste", "received", "qc_pass", "qc_all", "deliv", "ontime")}

    def fill(rows, mapping):
        for r in rows:
            i = idx.get(str(r["d"])[:10])
            if i is None:
                continue
            for src, dst in mapping.items():
                series[dst][i] += _f(r.get(src))

    fill(q.rows(f"""SELECT {col} AS d, SUM(o.total_estimated_selling_value) sale, SUM(o.total_estimated_food_cost) cost,
                      COUNT(*) orders,
                      SUM(CASE WHEN COALESCE(o.status,'') NOT IN {CLOSED} AND o.required_delivery_date < CURDATE() THEN 1 ELSE 0 END) delayed_cnt
                    FROM customer_orders o WHERE {where} GROUP BY {col}""", p),
         {"sale": "sale", "cost": "cost", "orders": "orders", "delayed_cnt": "delayed"})
    fill(q.rows(f"""SELECT {col} AS d, SUM(k.waste_qty_standard) waste, SUM(k.received_qty_standard) received
                    FROM kitchen_section_transactions k JOIN customer_orders o ON o.order_no = k.order_no
                    WHERE {where} AND k.current_section NOT IN ('QC','Trayline / Packing','Dispatch') GROUP BY {col}""", p),
         {"waste": "waste", "received": "received"})
    fill(q.rows(f"""SELECT {col} AS d, SUM(c.qc_status='Passed') qp, SUM(c.qc_status IN ('Passed','Rejected','Hold')) qa
                    FROM qc_checks c JOIN customer_orders o ON o.order_no = c.order_no WHERE {where} GROUP BY {col}""", p),
         {"qp": "qc_pass", "qa": "qc_all"})
    fill(q.rows(f"""SELECT {col} AS d, COUNT(*) dv,
                      SUM(pd.dispatch_date IS NOT NULL AND pd.dispatch_date <= o.required_delivery_date) ot
                    FROM packing_dispatch pd JOIN customer_orders o ON o.order_no = pd.order_no
                    WHERE {where} AND pd.dispatch_status='Delivered' GROUP BY {col}""", p),
         {"dv": "deliv", "ot": "ontime"})

    def ratio(a, b, scale=100):
        return [round(x / y * scale, 2) if y else None for x, y in zip(series[a], series[b])]
    margin = [round((s - c) / s * 100, 2) if s else None for s, c in zip(series["sale"], series["cost"])]
    return {"labels": [d.isoformat() for d in days],
            "sale": series["sale"], "cost": series["cost"], "orders": series["orders"], "delayed": series["delayed"],
            "margin_pct": margin, "waste_pct": ratio("waste", "received"),
            "qc_pass_pct": ratio("qc_pass", "qc_all"), "on_time_pct": ratio("ontime", "deliv")}


def _targets(q: _Q, cid: int) -> dict:
    t = dict(DEFAULT_TARGETS)
    for r in q.rows("""SELECT metric_code, target_value FROM performance_targets
                       WHERE (company_id = :cid OR company_id IS NULL) AND status = 'Active'
                         AND COALESCE(customer_name,'') = ''""", {"cid": cid}):
        if r["metric_code"] == "ON_TIME_DELIVERY":
            t["on_time_pct"] = _f(r["target_value"])
    return t


def kpi_cards(db: Session, f: dict, cid: int) -> tuple[list[dict], dict]:
    q = _Q(db)
    cur, pri = _period_metrics(q, f, cid, False), _period_metrics(q, f, cid, True)
    daily, tg = _daily(q, f, cid), _targets(q, cid)

    def delta(key, pct_point=False):
        a, b = cur.get(key), pri.get(key)
        if a is None or b is None or f["compare"] == "none":
            return None
        if pct_point:
            return round(a - b, 1)
        return round((a - b) / b * 100, 1) if b else None

    def card(key, title, icon, color, value, fmt, unit, spark, target=None, good="up", pp=False, records=None,
             definition="", formula="", dims=""):
        d = delta(key, pp)
        tone = None
        if d is not None and d != 0:
            tone = "good" if (d > 0) == (good == "up") else "bad"
        return {"key": key, "title": title, "icon": icon, "color": color, "value": value, "fmt": fmt,
                "unit": unit, "delta": d, "delta_unit": "pp" if pp else "%", "tone": tone,
                "prior": pri.get(key), "target": target, "good": good, "spark": spark,
                "records": records, "definition": definition, "formula": formula, "dims": dims}

    cards = [
        card("sale", "Order value", "bi-cash-stack", "#1e5bb8", cur["sale"], "money", "SAR", daily["sale"],
             records=cur["orders"], definition="Estimated selling value of every order in the filter.",
             formula="SUM(customer_orders.total_estimated_selling_value)", dims="Customer, brand, recipe, period"),
        card("cost", "Food cost", "bi-basket2", "#12b8a6", cur["cost"], "money", "SAR", daily["cost"], good="down",
             records=cur["orders"], target=None,
             definition="Estimated material cost from the BOM of every order in the filter.",
             formula="SUM(customer_orders.total_estimated_food_cost)", dims="Customer, recipe, ingredient category"),
        card("margin_pct", "Gross margin %", "bi-graph-up-arrow", "#8c68f5", cur["margin_pct"], "pct", "%",
             daily["margin_pct"], target=tg["margin_pct"], pp=True, records=cur["orders"],
             definition="Selling value less food cost, as a share of selling value.",
             formula="(Σ selling − Σ food cost) ÷ Σ selling × 100", dims="Customer, recipe, period"),
        card("on_time_pct", "On-time delivery", "bi-truck", "#e59f0b", cur["on_time_pct"], "pct", "%",
             daily["on_time_pct"], target=tg["on_time_pct"], pp=True, records=cur["delivered"],
             definition="Delivered orders whose dispatch date is on or before the required delivery date.",
             formula="Delivered with dispatch_date ≤ required_delivery_date ÷ delivered × 100",
             dims="Customer, region, driver"),
        card("waste_pct", "Waste % of input", "bi-trash3", "#f0743e", cur["waste_pct"], "pct", "%",
             daily["waste_pct"], target=tg["waste_pct"], good="down", pp=True,
             records=None, definition="Waste recorded by kitchen sections as a share of what they received.",
             formula="Σ waste_qty ÷ Σ received_qty × 100 (Cutting, Butchery, Hot, Cold, Bakery)",
             dims="Section, ingredient, recipe"),
        card("delayed", "Delayed orders", "bi-alarm", "#dc3545", cur["delayed"], "int", "", daily["delayed"],
             target=tg["delayed"], good="down", records=cur["open_orders"],
             definition="Open orders whose required delivery date has already passed.",
             formula="status not delivered/closed AND required_delivery_date < today", dims="Stage, customer"),
        card("qc_pass_pct", "QC pass rate", "bi-patch-check", "#0ea5c6", cur["qc_pass_pct"], "pct", "%",
             daily["qc_pass_pct"], target=tg["qc_pass_pct"], pp=True, records=cur["qc_decided"],
             definition="QC checks passed out of all decided checks (passed, rejected, hold).",
             formula="Passed ÷ (Passed + Rejected + Hold) × 100", dims="Recipe, section, inspector"),
        card("open_orders", "Open orders", "bi-kanban", "#6b7a90", cur["open_orders"], "int", "", daily["orders"],
             records=cur["orders"], definition="Orders in the filter that are not yet delivered, closed or cancelled.",
             formula="COUNT(orders) WHERE status NOT IN (Delivered, Dispatched, Closed, Cancelled)",
             dims="Stage, customer, priority"),
    ]
    return cards, {"current": cur, "prior": pri, "daily_labels": daily["labels"]}


# --------------------------------------------------------------------------- drill
DRILL_SQL = {
    "sale": ("Highest-value orders", """
        SELECT o.order_no AS ref, o.customer_name AS customer, o.status AS stage,
               o.required_delivery_date AS due, o.total_estimated_selling_value AS metric
        FROM customer_orders o WHERE {w} ORDER BY metric DESC LIMIT 25""", "SAR"),
    "cost": ("Highest food-cost orders", """
        SELECT o.order_no AS ref, o.customer_name AS customer, o.status AS stage,
               o.required_delivery_date AS due, o.total_estimated_food_cost AS metric
        FROM customer_orders o WHERE {w} ORDER BY metric DESC LIMIT 25""", "SAR"),
    "margin_pct": ("Lowest-margin orders", """
        SELECT o.order_no AS ref, o.customer_name AS customer, o.status AS stage,
               o.required_delivery_date AS due,
               ROUND((o.total_estimated_selling_value - o.total_estimated_food_cost)
                     / NULLIF(o.total_estimated_selling_value,0) * 100, 1) AS metric
        FROM customer_orders o WHERE {w} AND COALESCE(o.total_estimated_selling_value,0) > 0
        ORDER BY metric ASC LIMIT 25""", "%"),
    "on_time_pct": ("Late deliveries", """
        SELECT o.order_no AS ref, o.customer_name AS customer, pd.dispatch_status AS stage,
               o.required_delivery_date AS due, DATEDIFF(pd.dispatch_date, o.required_delivery_date) AS metric
        FROM packing_dispatch pd JOIN customer_orders o ON o.order_no = pd.order_no
        WHERE {w} AND pd.dispatch_status = 'Delivered'
        ORDER BY metric DESC LIMIT 25""", "days late"),
    "waste_pct": ("Highest waste lines", """
        SELECT k.order_no AS ref, CONCAT(k.current_section, ' · ', k.ingredient_name) AS customer,
               k.recipe_name AS stage, o.required_delivery_date AS due, ROUND(k.waste_qty_standard,2) AS metric
        FROM kitchen_section_transactions k JOIN customer_orders o ON o.order_no = k.order_no
        WHERE {w} AND COALESCE(k.waste_qty_standard,0) > 0 ORDER BY metric DESC LIMIT 25""", "qty"),
    "delayed": ("Delayed open orders", """
        SELECT o.order_no AS ref, o.customer_name AS customer, o.status AS stage,
               o.required_delivery_date AS due, DATEDIFF(CURDATE(), o.required_delivery_date) AS metric
        FROM customer_orders o
        WHERE {w} AND COALESCE(o.status,'') NOT IN """ + str(CLOSED) + """ AND o.required_delivery_date < CURDATE()
        ORDER BY metric DESC LIMIT 25""", "days"),
    "qc_pass_pct": ("Rejected / on-hold QC checks", """
        SELECT c.order_no AS ref, COALESCE(c.recipe_name, o.customer_name) AS customer, c.qc_status AS stage,
               o.required_delivery_date AS due, c.overall_score AS metric
        FROM qc_checks c JOIN customer_orders o ON o.order_no = c.order_no
        WHERE {w} AND c.qc_status IN ('Rejected','Hold') ORDER BY c.id DESC LIMIT 25""", "score"),
    "open_orders": ("Open orders by due date", """
        SELECT o.order_no AS ref, o.customer_name AS customer, o.status AS stage,
               o.required_delivery_date AS due, o.total_planned_portions AS metric
        FROM customer_orders o WHERE {w} AND COALESCE(o.status,'') NOT IN """ + str(CLOSED) + """
        ORDER BY o.required_delivery_date ASC LIMIT 25""", "portions"),
}


def drill(db: Session, f: dict, cid: int, key: str) -> dict:
    if key not in DRILL_SQL:
        return {"title": "", "rows": [], "unit": ""}
    title, sql, unit = DRILL_SQL[key]
    where, p = _scope(f, cid)
    rows = _Q(db).rows(sql.format(w=where), p)
    for r in rows:
        r["due"] = str(r.get("due") or "")
        r["metric"] = None if r.get("metric") is None else round(_f(r["metric"]), 2)
    return {"title": title, "rows": rows, "unit": unit}


# --------------------------------------------------------------------------- charts
def charts(db: Session, f: dict, cid: int) -> dict:
    q = _Q(db)
    where, p = _scope(f, cid)
    stage = q.one(f"""
        SELECT COUNT(*) AS orders,
          SUM(EXISTS(SELECT 1 FROM bom_lines b WHERE b.order_no=o.order_no)) AS bom,
          SUM(EXISTS(SELECT 1 FROM store_issuance_lines s WHERE s.order_no=o.order_no AND s.issuance_status IN ('Issued','Short Issued'))) AS issued,
          SUM(EXISTS(SELECT 1 FROM kitchen_section_transactions k WHERE k.order_no=o.order_no)) AS kitchen,
          SUM(EXISTS(SELECT 1 FROM qc_checks c WHERE c.order_no=o.order_no AND c.qc_status='Passed')) AS qc,
          SUM(EXISTS(SELECT 1 FROM packing_dispatch pd WHERE pd.order_no=o.order_no AND pd.dispatch_status IN ('Packed','Assigned','Out for Delivery','Delivered'))) AS packed,
          SUM(EXISTS(SELECT 1 FROM packing_dispatch pd WHERE pd.order_no=o.order_no AND pd.dispatch_status IN ('Out for Delivery','Delivered'))) AS dispatched,
          SUM(EXISTS(SELECT 1 FROM packing_dispatch pd WHERE pd.order_no=o.order_no AND pd.dispatch_status='Delivered')) AS delivered
        FROM customer_orders o WHERE {where}""", p)
    funnel = [(lbl, int(_f(stage.get(k)))) for k, lbl in (
        ("orders", "Orders"), ("bom", "BOM generated"), ("issued", "Store issued"), ("kitchen", "In kitchen"),
        ("qc", "QC passed"), ("packed", "Packed"), ("dispatched", "Dispatched"), ("delivered", "Delivered"))]

    status = q.rows(f"""SELECT COALESCE(NULLIF(o.status,''),'Submitted') AS label, COUNT(*) AS total
                        FROM customer_orders o WHERE {where} GROUP BY label ORDER BY total DESC""", p)

    queue = q.rows(f"""
        SELECT k.current_section AS label,
               -- only well-ordered timestamps: a back-dated edit must not
               -- produce a negative average that hides real waiting time
               ROUND(AVG(CASE WHEN k.received_at >= k.created_at
                              THEN TIMESTAMPDIFF(MINUTE, k.created_at, k.received_at) END),1) AS queue_min,
               ROUND(AVG(CASE WHEN k.transferred_at >= k.received_at
                              THEN TIMESTAMPDIFF(MINUTE, k.received_at, k.transferred_at) END),1) AS cycle_min,
               COUNT(*) AS line_count,
               SUM(CASE WHEN UPPER(COALESCE(k.transaction_status,'')) IN ('TRANSFERRED') OR UPPER(COALESCE(k.transaction_status,'')) LIKE 'COMPLETED%' THEN 1 ELSE 0 END) AS done,
               ROUND(SUM(COALESCE(k.received_qty_standard,0)),2) AS received,
               ROUND(SUM(COALESCE(k.waste_qty_standard,0)),2) AS waste
        FROM kitchen_section_transactions k JOIN customer_orders o ON o.order_no = k.order_no
        WHERE {where} GROUP BY k.current_section ORDER BY queue_min DESC""", p)

    store = q.one(f"""
        SELECT SUM(s.issuance_status='Issued' AND COALESCE(s.input_material_issued,0) <= COALESCE(s.required_qty_with_waste_standard,0)+0.001) AS issued,
               SUM(s.issuance_status='Issued' AND COALESCE(s.input_material_issued,0) > COALESCE(s.required_qty_with_waste_standard,0)+0.001) AS excess,
               SUM(s.issuance_status='Short Issued') AS short_issued,
               SUM(COALESCE(s.issuance_status,'Pending') NOT IN ('Issued','Short Issued','Cancelled')) AS pending
        FROM store_issuance_lines s JOIN customer_orders o ON o.order_no = s.order_no WHERE {where}""", p)

    recipe_mix = q.rows(f"""
        SELECT COALESCE(NULLIF(r.category,''),'Unassigned') AS label, ROUND(SUM(ol.required_portions),0) AS total
        FROM order_lines ol JOIN customer_orders o ON o.order_no = ol.order_no
        LEFT JOIN recipes r ON r.recipe_code = ol.recipe_no AND (r.company_id = :cid OR r.company_id IS NULL)
        WHERE {where} GROUP BY label ORDER BY total DESC LIMIT 10""", p)
    customer_mix = q.rows(f"""
        SELECT o.customer_name AS label, ROUND(SUM(o.total_estimated_selling_value),2) AS total
        FROM customer_orders o WHERE {where} GROUP BY o.customer_name ORDER BY total DESC LIMIT 10""", p)

    return {"funnel": funnel, "status": status, "queue": queue,
            "store": {k: int(_f(store.get(k))) for k in ("issued", "excess", "short_issued", "pending")},
            "recipe_mix": recipe_mix, "customer_mix": customer_mix}


def attention(db: Session, f: dict, cid: int) -> list[dict]:
    """Exceptions as visual tiles (count, share of open orders, link)."""
    q = _Q(db)
    where, p = _scope(f, cid)
    r = q.one(f"""
        SELECT SUM(COALESCE(o.status,'') NOT IN {CLOSED}) AS open_orders,
          SUM(COALESCE(o.status,'') NOT IN {CLOSED} AND o.required_delivery_date < CURDATE()) AS delayed_cnt,
          SUM(COALESCE(o.status,'') NOT IN {CLOSED} AND o.required_delivery_date BETWEEN CURDATE() AND DATE_ADD(CURDATE(), INTERVAL 1 DAY)) AS due_soon,
          SUM(EXISTS(SELECT 1 FROM store_issuance_lines s WHERE s.order_no=o.order_no AND COALESCE(s.finalized,0)=0)) AS store_open,
          SUM(EXISTS(SELECT 1 FROM kitchen_section_transactions k WHERE k.order_no=o.order_no AND k.current_section='QC'
                     AND UPPER(COALESCE(k.transaction_status,'')) NOT IN ('QC PASSED','QC REJECTED','TRANSFERRED','COMPLETED'))) AS qc_waiting,
          SUM(EXISTS(SELECT 1 FROM packing_dispatch pd WHERE pd.order_no=o.order_no AND COALESCE(pd.dispatch_status,'Packing Pending') IN ('Packing Pending','Packing In Progress','Pending'))) AS packing,
          SUM(EXISTS(SELECT 1 FROM packing_dispatch pd WHERE pd.order_no=o.order_no AND pd.dispatch_status IN ('Packed','Assigned') AND COALESCE(pd.driver_name,'')='')) AS no_driver
        FROM customer_orders o WHERE {where}""", p)
    base = max(int(_f(r.get("open_orders"))), 1)
    items = [
        ("delayed", "Past due, not delivered", "bi-alarm", "#dc3545", "/production/orders"),
        ("due_soon", "Due today or tomorrow", "bi-hourglass-split", "#e59f0b", "/production/orders"),
        ("store_open", "Store issuance not finalized", "bi-box-seam", "#1e5bb8", "/production/store-issuance/by-section"),
        ("qc_waiting", "Waiting for QC", "bi-patch-question", "#0ea5c6", "/qc"),
        ("packing", "Waiting for trayline / packing", "bi-grid-3x3-gap", "#8c68f5", "/packing"),
        ("no_driver", "Packed, no driver assigned", "bi-person-x", "#f0743e", "/dispatch/logistics/board"),
    ]
    col = {"delayed": "delayed_cnt"}  # DELAYED is a reserved word in MySQL/MariaDB
    return [{"key": k, "title": t, "icon": i, "color": c, "url": u, "value": int(_f(r.get(col.get(k, k)))),
             "share": round(int(_f(r.get(col.get(k, k)))) / base * 100)} for k, t, i, c, u in items]


def batch_table(db: Session, f: dict, cid: int, limit: int = 300) -> list[dict]:
    """Image 19 — one row per order: stage, qty in/out/waste, cycle, delay, QC."""
    q = _Q(db)
    where, p = _scope(f, cid)
    rows = q.rows(f"""
        SELECT o.order_no, o.customer_name, COALESCE(o.brand,'') AS brand, COALESCE(o.priority,'Normal') AS priority,
               COALESCE(o.status,'') AS status, o.required_delivery_date AS delivery_date,
               COALESCE(o.total_planned_portions,0) AS portions,
               COALESCE(o.total_estimated_food_cost,0) AS food_cost,
               COALESCE(o.total_estimated_selling_value,0) AS sale,
               COALESCE(o.total_estimated_margin,0) AS margin, o.created_at,
               (SELECT COUNT(DISTINCT ol.recipe_no) FROM order_lines ol WHERE ol.order_no=o.order_no) AS recipes,
               (SELECT MAX(ol.recipe_name) FROM order_lines ol WHERE ol.order_no=o.order_no) AS first_recipe,
               (SELECT SUM(COALESCE(s.input_material_issued,0)) FROM store_issuance_lines s
                 WHERE s.order_no=o.order_no AND s.issuance_status IN ('Issued','Short Issued')) AS input_qty,
               (SELECT SUM(COALESCE(k.issued_qty_standard,0)) FROM kitchen_section_transactions k
                 WHERE k.order_no=o.order_no AND k.current_section='QC') AS output_qty,
               (SELECT SUM(COALESCE(k.waste_qty_standard,0)) FROM kitchen_section_transactions k
                 WHERE k.order_no=o.order_no) AS waste_qty,
               (SELECT k.current_section FROM kitchen_section_transactions k
                 WHERE k.order_no=o.order_no AND UPPER(COALESCE(k.transaction_status,'')) NOT IN ('TRANSFERRED')
                   AND UPPER(COALESCE(k.transaction_status,'')) NOT LIKE 'COMPLETED%'
                 ORDER BY k.route_step_no DESC, k.id DESC LIMIT 1) AS current_section,
               (SELECT ROUND(AVG(TIMESTAMPDIFF(MINUTE, k.created_at, k.received_at)),0) FROM kitchen_section_transactions k
                 WHERE k.order_no=o.order_no AND k.received_at >= k.created_at) AS queue_min,
               (SELECT c.qc_status FROM qc_checks c WHERE c.order_no=o.order_no ORDER BY c.id DESC LIMIT 1) AS qc_status,
               (SELECT pd.dispatch_date FROM packing_dispatch pd WHERE pd.order_no=o.order_no ORDER BY pd.id DESC LIMIT 1) AS dispatch_date
        FROM customer_orders o WHERE {where}
        ORDER BY o.required_delivery_date ASC, o.id DESC LIMIT {int(limit)}""", p)
    today = date.today()
    for r in rows:
        due = r.get("delivery_date")
        due_d = due if isinstance(due, date) else _d(str(due or "")[:10])
        closed = r["status"] in CLOSED
        r["delay_days"] = (today - due_d).days if (due_d and not closed and due_d < today) else 0
        created = r.get("created_at")
        end = datetime.now()
        r["cycle_hours"] = round((end - created).total_seconds() / 3600, 1) if isinstance(created, datetime) and not closed else None
        r["margin_pct"] = round(_f(r["margin"]) / _f(r["sale"]) * 100, 1) if _f(r["sale"]) else None
        r["delivery_date"] = str(due or "")
        r["health"] = ("Delayed" if r["delay_days"] > 0 else "On hold" if r["status"] in ("Hold", "On Hold")
                       else "Completed" if closed else "On track")
    return rows


def picker_options(db: Session, cid: int) -> dict:
    q = _Q(db)
    scope = "(company_id = :cid OR company_id IS NULL)"

    def col(sql):
        return [r["v"] for r in q.rows(sql, {"cid": cid}) if r.get("v")]
    return {
        "customers": col(f"SELECT DISTINCT customer_name v FROM customer_orders WHERE {scope} ORDER BY v LIMIT 400"),
        "brands": col(f"SELECT DISTINCT brand v FROM customer_orders WHERE {scope} ORDER BY v LIMIT 200"),
        "statuses": col(f"SELECT DISTINCT status v FROM customer_orders WHERE {scope} ORDER BY v"),
        "channels": col(f"SELECT DISTINCT channel v FROM customer_orders WHERE {scope} ORDER BY v"),
        "priorities": col(f"SELECT DISTINCT priority v FROM customer_orders WHERE {scope} ORDER BY v"),
        "kitchens": col(f"SELECT DISTINCT kitchen v FROM customer_orders WHERE {scope} ORDER BY v"),
        "recipes": q.rows(f"""SELECT recipe_code AS code, MAX(recipe_name) AS name FROM recipes
                               WHERE {scope} AND UPPER(TRIM(COALESCE(status,'')))='ACTIVE'
                               GROUP BY recipe_code ORDER BY name LIMIT 1000""", {"cid": cid}),
        "sections": SECTIONS, "timelines": TIMELINES,
        "basis": [(k, v[0]) for k, v in DATE_BASIS.items()],
    }


def list_scope(request, db: Session, alias: str, order_col: str = "order_no") -> dict:
    """Batch 204 — apply COMPANY scope + GLOBAL FILTER to a list screen.

    Returns {"gf", "gf_options", "sql", "params"} where `sql` is an AND-clause
    for a table aliased `alias` that has `company_id` and `order_no` columns:

      * company scope is ALWAYS applied — QC history, Packing and Dispatch lists
        previously showed every company's records (same leak as the old
        dashboard, fixed there in Batch 203);
      * the global filter is applied only when active, as
        order_no IN (SELECT o.order_no FROM customer_orders o WHERE <scope>),
        so page-specific filters keep working unchanged on top of it.
    """
    cid = int(request.session.get("company_id") or 1)
    f = parse_filters(request.query_params, default_timeline="all")
    sql = f" AND ({alias}.company_id = :gf_cid OR {alias}.company_id IS NULL)"
    params: dict = {"gf_cid": cid}
    if f["active"]:
        where, p = _scope(f, cid)
        # Prefix every bind name with gf_ (whole-name match: ":s" must not
        # touch ":st" or ":sec") so they cannot collide with the page's own.
        where = re.sub(r":([A-Za-z_]\w*)", lambda m: f":gf_{m.group(1)}" if m.group(1) in p else m.group(0), where)
        params.update({f"gf_{k}": v for k, v in p.items()})
        sql += f" AND {alias}.{order_col} IN (SELECT o.order_no FROM customer_orders o WHERE {where})"
    return {"gf": f, "gf_options": picker_options(db, cid), "sql": sql, "params": params}
