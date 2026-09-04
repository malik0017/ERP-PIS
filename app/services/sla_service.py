"""Batch 170 — SLA and Performance Target engines.

Business users configure the rules; this module calculates status; the dashboard
displays the result. Nothing here writes UI text or reads request state, so the
same functions serve the config screens, the dashboard and any future report.

THE TWO DESIGN DECISIONS WORTH KNOWING

1. RULE RESOLUTION IS ORDERED, NOT "FIRST MATCH".
   Several rules can match one order — a global default, a customer rule, a
   customer + order-type rule. Specificity wins, then `priority`, and ties break
   on the newest rule. Without that ordering the result depends on table order,
   which is a bug that only shows up once someone adds a second rule.

2. AN SLA INSTANCE IS A SNAPSHOT.
   `compute_status()` reads the instance, never the rule. An order judged under
   a 4-hour SLA stays judged under it when the rule later becomes 6 hours.
   Recomputing from the live rule would silently rewrite history — and SLA
   history is precisely what gets disputed.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

# Simplified for the dashboard; the instance stores the full set.
ON_TRACK, AT_RISK, OVERDUE, BREACHED, COMPLETED = (
    "ON_TRACK", "AT_RISK", "OVERDUE", "BREACHED", "COMPLETED")

# Predefined metric codes. Free-text metric names were explicitly ruled out —
# the engine has to know how to CALCULATE each one, so the list is closed and
# each entry maps to a query below.
METRIC_CODES = [
    ("ORDERS_RECEIVED", "Orders Received", "orders"),
    ("ORDERS_COMPLETED", "Orders Completed", "orders"),
    ("PORTIONS_PRODUCED", "Portions Produced", "portions"),
    ("BOM_GENERATED", "BOM Generated", "orders"),
    ("QC_COMPLETED", "QC Completed", "checks"),
    ("PACKING_COMPLETED", "Packing Completed", "orders"),
    ("DISPATCH_COMPLETED", "Dispatch Completed", "orders"),
    ("DELIVERED", "Delivered", "orders"),
    ("SLA_COMPLIANCE", "SLA Compliance", "%"),
    ("ON_TIME_DELIVERY", "On-Time Delivery", "%"),
]
METRIC_LABELS = {c: l for c, l, _ in METRIC_CODES}


# ---------------------------------------------------------------------------
# SLA
# ---------------------------------------------------------------------------
def resolve_rule(db: Session, customer_name: str | None,
                 order_type: str | None, company_id: int | None = None) -> dict | None:
    """Pick the rule that governs an order.

    Specificity order, most specific first:
        customer + order type  ->  customer  ->  order type  ->  global default

    Expressed as a CASE in ORDER BY rather than four separate queries, so one
    round trip decides it and the ordering is visible in one place.
    """
    rows = db.execute(text("""
        SELECT * FROM sla_rules
        WHERE UPPER(TRIM(COALESCE(status,''))) = 'ACTIVE'
          AND (company_id = :cid OR company_id IS NULL OR :cid IS NULL)
          AND (customer_name IS NULL OR customer_name = '' OR customer_name = :cust)
          AND (order_type    IS NULL OR order_type    = '' OR order_type    = :otype)
        ORDER BY
            CASE
              WHEN COALESCE(customer_name,'') <> '' AND COALESCE(order_type,'') <> '' THEN 0
              WHEN COALESCE(customer_name,'') <> ''                                   THEN 1
              WHEN COALESCE(order_type,'')    <> ''                                   THEN 2
              ELSE 3
            END,
            priority ASC,
            id DESC
        LIMIT 1
    """), {"cust": customer_name or "", "otype": order_type or "",
           "cid": company_id}).mappings().first()
    return dict(rows) if rows else None


def build_instance(rule: dict, started_at: datetime,
                   delivery_at: datetime | None) -> dict:
    """Turn a rule plus an order's dates into concrete timestamps.

    Two SLA models, per the spec:
      DEADLINE  the order's own required delivery time is the deadline
      DURATION  deadline = start + sla_minutes

    DEADLINE falls back to DURATION when an order has no delivery time, rather
    than producing an instance with no due date — an SLA that can never be
    breached is worse than a slightly wrong one, because it looks healthy.
    """
    sla_min = int(rule.get("sla_minutes") or 0)
    at_risk_min = int(rule.get("at_risk_minutes") or 0)
    grace_min = int(rule.get("grace_minutes") or 0)
    basis = (rule.get("basis") or "DEADLINE").upper()

    if basis == "DEADLINE" and delivery_at:
        due = delivery_at
    else:
        due = started_at + timedelta(minutes=sla_min)

    return {
        "sla_rule_id": rule.get("id"),
        "rule_name": rule.get("rule_name"),
        "started_at": started_at,
        "due_at": due,
        "at_risk_at": due - timedelta(minutes=at_risk_min),
        "grace_until": due + timedelta(minutes=grace_min),
    }


def compute_status(inst: dict, now: datetime | None = None,
                   extend_minutes: int = 0) -> str:
    """Status from a stored instance. No rule lookup, no database.

    Order of the checks matters:
      completed first  — a delivered order is COMPLETED even if it was late;
                         lateness is answered by comparing completed_at to
                         due_at, not by leaving it flagged OVERDUE forever.
      then breached    — past the grace period
      then overdue     — past due, still inside grace
      then at risk
      else on track
    """
    now = now or datetime.utcnow()
    due = inst.get("due_at")
    if extend_minutes:
        due = due + timedelta(minutes=extend_minutes) if due else due

    if inst.get("completed_at"):
        return COMPLETED
    if not due:
        return ON_TRACK

    grace = inst.get("grace_until")
    if extend_minutes and grace:
        grace = grace + timedelta(minutes=extend_minutes)

    if grace and now > grace:
        return BREACHED
    if now > due:
        return OVERDUE
    at_risk = inst.get("at_risk_at")
    if extend_minutes and at_risk:
        at_risk = at_risk + timedelta(minutes=extend_minutes)
    if at_risk and now >= at_risk:
        return AT_RISK
    return ON_TRACK


DASHBOARD_STATUS = {
    ON_TRACK: ("On Track", "success"),
    AT_RISK: ("At Risk", "warning"),
    OVERDUE: ("Overdue", "danger"),
    BREACHED: ("Breached", "danger"),
    COMPLETED: ("Completed", "info"),
}


# ---------------------------------------------------------------------------
# Performance targets
# ---------------------------------------------------------------------------
_METRIC_SQL = {
    "ORDERS_RECEIVED": ("SELECT COUNT(*) FROM customer_orders co WHERE 1=1 {d} {c}", "co.order_date"),
    "ORDERS_COMPLETED": ("SELECT COUNT(*) FROM customer_orders co "
                         "WHERE COALESCE(co.status,'') IN ('Delivered','Closed') {d} {c}", "co.order_date"),
    "PORTIONS_PRODUCED": ("SELECT COALESCE(SUM(ol.required_portions),0) FROM order_lines ol "
                          "JOIN customer_orders co ON co.order_no = ol.order_no WHERE 1=1 {d} {c}",
                          "co.order_date"),
    "BOM_GENERATED": ("SELECT COUNT(DISTINCT bl.order_no) FROM bom_lines bl "
                      "JOIN customer_orders co ON co.order_no = bl.order_no WHERE 1=1 {d} {c}",
                      "co.order_date"),
    "QC_COMPLETED": ("SELECT COUNT(*) FROM qc_checks q "
                     "JOIN customer_orders co ON co.order_no = q.order_no WHERE 1=1 {d} {c}",
                     "co.order_date"),
    "PACKING_COMPLETED": ("SELECT COUNT(*) FROM packing_dispatch pd "
                          "JOIN customer_orders co ON co.order_no = pd.order_no "
                          "WHERE COALESCE(pd.packing_status,'') = 'Packed' {d} {c}", "co.order_date"),
    "DISPATCH_COMPLETED": ("SELECT COUNT(*) FROM packing_dispatch pd "
                           "JOIN customer_orders co ON co.order_no = pd.order_no "
                           "WHERE COALESCE(pd.dispatch_status,'') IN "
                           "('Out for Delivery','Delivered','Dispatched','Closed') {d} {c}",
                           "co.order_date"),
    "DELIVERED": ("SELECT COUNT(*) FROM customer_orders co "
                  "WHERE COALESCE(co.status,'') = 'Delivered' {d} {c}", "co.order_date"),
}


def measure_metric(db: Session, metric_code: str, date_from: str | None,
                   date_to: str | None, customer: str | None = None) -> float | None:
    """Actual value for a metric over a window.

    Returns None — not 0 — when the metric cannot be measured. A target showing
    0/500 reads as "we produced nothing today"; None renders as an em dash and
    reads as "not measured", which is the truth. Same reasoning as the cockpit
    KPI guard in Batch 165.
    """
    if metric_code in ("SLA_COMPLIANCE", "ON_TIME_DELIVERY"):
        return _measure_sla_percent(db, metric_code, date_from, date_to, customer)

    spec = _METRIC_SQL.get(metric_code)
    if not spec:
        return None
    sql, datecol = spec
    params: dict = {}
    dclause = ""
    if date_from:
        dclause += f" AND {datecol} >= :df"
        params["df"] = date_from
    if date_to:
        dclause += f" AND {datecol} <= :dt"
        params["dt"] = date_to
    cclause = ""
    if customer:
        cclause = " AND co.customer_name = :cust"
        params["cust"] = customer
    try:
        v = db.execute(text(sql.format(d=dclause, c=cclause)), params).scalar()
        return float(v or 0)
    except Exception:
        return None


def _measure_sla_percent(db: Session, code: str, date_from, date_to, customer):
    """SLA compliance / on-time delivery as a percentage.

    Only orders with an SLA instance count. Orders with no instance are excluded
    rather than assumed compliant — counting unmeasured orders as successes is
    how a compliance number becomes flattering and useless.
    """
    params: dict = {}
    where = ["1=1"]
    if date_from:
        where.append("os.due_at >= :df"); params["df"] = date_from
    if date_to:
        where.append("os.due_at <= :dt"); params["dt"] = date_to
    if customer:
        where.append("co.customer_name = :cust"); params["cust"] = customer
    w = " AND ".join(where)
    try:
        row = db.execute(text(f"""
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN os.completed_at IS NOT NULL
                             AND os.completed_at <= os.due_at THEN 1 ELSE 0 END) AS ok
            FROM order_sla os
            JOIN customer_orders co ON co.order_no = os.order_no
            WHERE {w}
        """), params).mappings().first()
        total = int(row["total"] or 0)
        if total == 0:
            return None
        return round(float(row["ok"] or 0) / total * 100, 1)
    except Exception:
        return None


def evaluate_targets(db: Session, date_from: str | None, date_to: str | None,
                     customer: str | None = None,
                     company_id: int | None = None) -> list[dict]:
    """Every active target with its actual, achievement % and status."""
    try:
        targets = db.execute(text("""
            SELECT * FROM performance_targets
            WHERE UPPER(TRIM(COALESCE(status,''))) = 'ACTIVE'
              AND (company_id = :cid OR company_id IS NULL OR :cid IS NULL)
            ORDER BY metric_code
        """), {"cid": company_id}).mappings().all()
    except Exception:
        return []

    out = []
    for t in targets:
        t = dict(t)
        cust = t.get("customer_name") or customer
        actual = measure_metric(db, t["metric_code"], date_from, date_to, cust)
        target = float(t.get("target_value") or 0)
        pct = None
        if actual is not None and target > 0:
            pct = round(actual / target * 100, 1)
        # Bands: below 90% is a miss, 90-100 is close, over 100 exceeded.
        if pct is None:
            state = "unknown"
        elif pct >= 100:
            state = "exceeded"
        elif pct >= 90:
            state = "close"
        else:
            state = "below"
        out.append({**t, "actual": actual, "achievement": pct, "state": state,
                    "metric_label": METRIC_LABELS.get(t["metric_code"], t["metric_code"])})
    return out


# ---------------------------------------------------------------------------
# Instance lifecycle
# ---------------------------------------------------------------------------
# Batch 171. Instances are created LAZILY — sync_open_orders() is called when a
# dashboard is rendered, rather than hooking every route that can change an
# order's status.
#
# Why lazily: hooking order confirm, BOM release, dispatch and delivery means
# five call sites that must all stay correct, and a missed one produces an order
# with no SLA that looks perfectly healthy. One reconciliation pass that reads
# the order's CURRENT status cannot miss anything, and it is cheap because it
# only touches orders that are open or recently closed.
#
# The snapshot rule from Batch 170 still holds: once an instance exists its
# due_at is never recomputed. Only completion is written afterwards.
_OPEN_EXCLUDE = ("Delivered", "Closed", "Cancelled", "Rejected")
_DONE_STATUSES = ("Delivered", "Closed")


def sync_open_orders(db: Session, company_id: int | None = None,
                     limit: int = 500) -> dict:
    """Create missing SLA instances and close completed ones.

    Returns counts so a caller can log or display them. Never raises: a
    dashboard must render even if SLA tables are missing.
    """
    created = completed = 0
    try:
        orders = db.execute(text("""
            SELECT co.order_no, co.customer_name, co.sales_channel AS order_type,
                   co.status, co.order_date, co.required_delivery_date,
                   co.company_id
            FROM customer_orders co
            LEFT JOIN order_sla os ON os.order_no = co.order_no
            WHERE (co.company_id = :cid OR co.company_id IS NULL OR :cid IS NULL)
              AND os.id IS NULL
            ORDER BY co.order_date DESC
            LIMIT :lim
        """), {"cid": company_id, "lim": limit}).mappings().all()

        for o in orders:
            rule = resolve_rule(db, o["customer_name"], o["order_type"], company_id)
            if not rule:
                # No rule configured yet — skip rather than invent a deadline.
                # An order with a made-up SLA is worse than one with none.
                continue
            started = o["order_date"] or datetime.utcnow()
            if not isinstance(started, datetime):
                started = datetime.combine(started, datetime.min.time())
            delivery = o["required_delivery_date"]
            if delivery is not None and not isinstance(delivery, datetime):
                delivery = datetime.combine(delivery, datetime.min.time())
            inst = build_instance(rule, started, delivery)
            db.execute(text("""
                INSERT INTO order_sla(company_id, order_no, sla_rule_id, rule_name,
                    started_at, due_at, at_risk_at, grace_until, status,
                    created_at, updated_at)
                VALUES(:cid,:ono,:rid,:rname,:st,:due,:risk,:grace,'ON_TRACK',NOW(),NOW())
                ON DUPLICATE KEY UPDATE updated_at = NOW()
            """), {"cid": o["company_id"], "ono": o["order_no"],
                   "rid": inst["sla_rule_id"], "rname": inst["rule_name"],
                   "st": inst["started_at"], "due": inst["due_at"],
                   "risk": inst["at_risk_at"], "grace": inst["grace_until"]})
            created += 1

        # Close instances whose order has since been delivered. completed_at is
        # stamped once and never moved.
        res = db.execute(text(f"""
            UPDATE order_sla os
            JOIN customer_orders co ON co.order_no = os.order_no
            SET os.completed_at = NOW(), os.status = 'COMPLETED', os.updated_at = NOW()
            WHERE os.completed_at IS NULL
              AND COALESCE(co.status,'') IN {_DONE_STATUSES}
        """))
        completed = res.rowcount or 0
        db.commit()
    except Exception:
        db.rollback()
    return {"created": created, "completed": completed}


def delivery_health(db: Session, company_id: int | None = None) -> dict:
    """On-time / at-risk / overdue across OPEN orders, for the dashboard."""
    out = {"on_track": 0, "at_risk": 0, "overdue": 0, "breached": 0,
           "completed": 0, "total": 0, "unmeasured": 0}
    try:
        rows = db.execute(text("""
            SELECT os.*, co.status AS order_status FROM order_sla os
            JOIN customer_orders co ON co.order_no = os.order_no
            WHERE (os.company_id = :cid OR os.company_id IS NULL OR :cid IS NULL)
        """), {"cid": company_id}).mappings().all()
        now = datetime.utcnow()
        for r in rows:
            st = compute_status(dict(r), now=now)
            out["total"] += 1
            key = {"ON_TRACK": "on_track", "AT_RISK": "at_risk",
                   "OVERDUE": "overdue", "BREACHED": "breached",
                   "COMPLETED": "completed"}[st]
            out[key] += 1
        # Open orders with no instance at all — reported, not hidden, so a
        # healthy-looking board cannot be the result of missing rules.
        out["unmeasured"] = int(db.execute(text(f"""
            SELECT COUNT(*) FROM customer_orders co
            LEFT JOIN order_sla os ON os.order_no = co.order_no
            WHERE os.id IS NULL AND COALESCE(co.status,'') NOT IN {_OPEN_EXCLUDE}
              AND (co.company_id = :cid OR co.company_id IS NULL OR :cid IS NULL)
        """), {"cid": company_id}).scalar() or 0)
    except Exception:
        pass
    return out
