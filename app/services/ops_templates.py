# app/services/ops_templates.py
# =============================================================================
# Batch 214 — STARTER SLA RULES AND PERFORMANCE TARGETS
# -----------------------------------------------------------------------------
# Both screens opened empty, and an empty measurement screen asks a question the
# software should already be able to answer: which SLAs does a catering company
# commit to, and what does a production kitchen measure? Batch 212 put the
# answer on screen as guidance. This module makes it installable.
#
# Design decisions worth recording:
#
#   * NOTHING is installed automatically. A target that appears without anyone
#     choosing it is a number nobody owns, and the first time it is missed the
#     answer is "that was not our target, the system invented it". Installation
#     is an explicit button, guarded by the same settings/edit permission as
#     creating a rule by hand.
#
#   * INSTALLATION IS IDEMPOTENT and never overwrites. Matching is on the
#     natural key — rule name for SLAs, metric+period for targets — so pressing
#     the button twice adds nothing, and a target you have edited keeps your
#     value. The result reports created/skipped separately so the screen can say
#     which happened.
#
#   * The numbers are INDUSTRY DEFAULTS, not promises. Each carries the reason
#     it exists and the range it came from, shown in the UI, so whoever presses
#     the button can argue with it before the kitchen is measured against it.
#
# Four SLAs, six targets. Deliberately small: a board with fifteen measures is
# a board nobody reads.
# =============================================================================
from __future__ import annotations

from datetime import date

from sqlalchemy import text
from sqlalchemy.orm import Session
from app.core.db_read import log_failure as db_read_log

# --------------------------------------------------------------------------- SLA
# `sla_minutes` is the commitment, measured from `starts_on` to `ends_on`.
# `at_risk_minutes` is how long before the deadline the board turns amber, and
# `grace_minutes` how long past it before a miss counts as breached.
SLA_TEMPLATES = [
    {
        "rule_name": "On-time delivery (OTIF)",
        "sla_minutes": 24 * 60, "at_risk_minutes": 180, "grace_minutes": 15,
        "starts_on": "CONFIRMED", "ends_on": "DELIVERED", "basis": "DEADLINE",
        "priority": 10,
        "why": "The commitment customers write into catering contracts. Measured "
               "from order confirmation to delivery; 96–98% is the usual contractual level.",
        "target_pct": 96.0,
    },
    {
        "rule_name": "Order confirmation within 4 hours",
        "sla_minutes": 4 * 60, "at_risk_minutes": 60, "grace_minutes": 15,
        "starts_on": "SUBMITTED", "ends_on": "CONFIRMED", "basis": "DEADLINE",
        "priority": 20,
        "why": "Protects the kitchen's planning window. An order confirmed late "
               "compresses purchasing and prep for everyone downstream.",
        "target_pct": 95.0,
    },
    {
        "rule_name": "QC completed before packing",
        "sla_minutes": 2 * 60, "at_risk_minutes": 30, "grace_minutes": 10,
        "starts_on": "IN_PRODUCTION", "ends_on": "QC_PASSED", "basis": "DEADLINE",
        "priority": 30,
        "why": "Food safety gate. Anything packed without a QC decision is "
               "unverified stock leaving the building.",
        "target_pct": 98.0,
    },
    {
        "rule_name": "Dispatch released same day as packing",
        "sla_minutes": 12 * 60, "at_risk_minutes": 120, "grace_minutes": 30,
        "starts_on": "PACKED", "ends_on": "DISPATCHED", "basis": "DEADLINE",
        "priority": 40,
        "why": "Cold-chain exposure grows with every hour packed food waits on "
               "the dock. Keeps packing and logistics on the same clock.",
        "target_pct": 97.0,
    },
]

# --------------------------------------------------------------------------- Targets
TARGET_TEMPLATES = [
    {"metric_code": "GROSS_MARGIN_PCT", "target_name": "Gross margin %",
     "target_value": 30.0, "unit": "%", "period": "MONTHLY",
     "why": "Contract catering typically runs 30–35%. Below 30 the recipe mix or "
            "the selling price needs review, not the kitchen."},
    {"metric_code": "FOOD_COST_PCT", "target_name": "Food cost % of sales",
     "target_value": 35.0, "unit": "%", "period": "MONTHLY",
     "why": "The same coin as margin, read the way purchasing thinks. 30–35% is "
            "normal; a rising figure points at prices or over-issue."},
    {"metric_code": "WASTE_PCT", "target_name": "Waste % of input",
     "target_value": 4.0, "unit": "%", "period": "WEEKLY",
     "why": "Waste recorded by kitchen sections against what they received. "
            "3–5% is achievable; above that is trim technique or over-production."},
    {"metric_code": "YIELD_PCT", "target_name": "Kitchen yield %",
     "target_value": 90.0, "unit": "%", "period": "WEEKLY",
     "why": "Transferred out ÷ received in, across the prep sections. 85–95% "
            "depending on protein and produce mix."},
    # Batch 224: real OTIF, now that delivery time and delivered quantity are
    # captured. Kept alongside its two halves so a miss can be attributed —
    # "we were late" and "we were short" need different fixes.
    {"metric_code": "OTIF_PCT", "target_name": "OTIF (On Time In Full) %",
     "target_value": 95.0, "unit": "%", "period": "MONTHLY",
     "why": "The number customers write into catering contracts: delivered by "
            "the agreed time AND complete. 95–98% is the usual commitment."},
    {"metric_code": "IN_FULL_PCT", "target_name": "Delivered in full %",
     "target_value": 98.0, "unit": "%", "period": "MONTHLY",
     "why": "The other half of OTIF. A shortfall here points at production or "
            "picking, not at logistics."},
    {"metric_code": "ON_TIME_DELIVERY", "target_name": "On-time delivery %",
     "target_value": 96.0, "unit": "%", "period": "MONTHLY",
     "why": "The customer-facing number. Needs at least one SLA rule installed "
            "before it can be measured."},
    {"metric_code": "QC_PASS_RATE", "target_name": "QC pass rate %",
     "target_value": 98.0, "unit": "%", "period": "WEEKLY",
     "why": "First-time pass rate. Every failure is rework, delay and cost."},
]


def _scalar(db: Session, sql: str, params: dict):
    try:
        return db.execute(text(sql), params).scalar()
    except Exception as _exc:
        # Batch 221: logged, not swallowed — a silent except here makes
        # a broken query look like an empty table (app/core/db_read.py).
        db_read_log(_exc, sql, 'ops_templates.py._scalar')
        db.rollback()
        return None


def install_sla_templates(db: Session, company_id: int) -> dict:
    """Create any missing starter SLA rule. Never edits an existing one."""
    created, skipped = [], []
    for t in SLA_TEMPLATES:
        exists = _scalar(db, """
            SELECT id FROM sla_rules
            WHERE rule_name = :n AND (company_id = :cid OR company_id IS NULL)
            LIMIT 1""", {"n": t["rule_name"], "cid": company_id})
        if exists:
            skipped.append(t["rule_name"])
            continue
        try:
            db.execute(text("""
                INSERT INTO sla_rules
                  (company_id, rule_name, customer_name, order_type, sla_minutes,
                   at_risk_minutes, grace_minutes, starts_on, ends_on, basis,
                   priority, notify_at_risk, notify_overdue, status, created_at, updated_at)
                VALUES (:cid, :n, NULL, NULL, :sla, :risk, :grace, :starts, :ends,
                        :basis, :prio, 1, 1, 'Active', NOW(), NOW())
            """), {"cid": company_id, "n": t["rule_name"], "sla": t["sla_minutes"],
                   "risk": t["at_risk_minutes"], "grace": t["grace_minutes"],
                   "starts": t["starts_on"], "ends": t["ends_on"],
                   "basis": t["basis"], "prio": t["priority"]})
            created.append(t["rule_name"])
        except Exception:
            db.rollback()
            skipped.append(t["rule_name"])
    if created:
        db.commit()
    return {"created": created, "skipped": skipped}


def install_target_templates(db: Session, company_id: int) -> dict:
    """Create any missing starter target. An edited target keeps its value."""
    created, skipped = [], []
    for t in TARGET_TEMPLATES:
        exists = _scalar(db, """
            SELECT id FROM performance_targets
            WHERE metric_code = :m AND period = :p
              AND COALESCE(customer_name,'') = ''
              AND (company_id = :cid OR company_id IS NULL)
            LIMIT 1""", {"m": t["metric_code"], "p": t["period"], "cid": company_id})
        if exists:
            skipped.append(t["target_name"])
            continue
        try:
            db.execute(text("""
                INSERT INTO performance_targets
                  (company_id, target_name, metric_code, customer_name, period,
                   target_value, unit, effective_from, status, created_at, updated_at)
                VALUES (:cid, :n, :m, NULL, :p, :v, :u, :ef, 'Active', NOW(), NOW())
            """), {"cid": company_id, "n": t["target_name"], "m": t["metric_code"],
                   "p": t["period"], "v": t["target_value"], "u": t["unit"],
                   "ef": date.today().isoformat()})
            created.append(t["target_name"])
        except Exception:
            db.rollback()
            skipped.append(t["target_name"])
    if created:
        db.commit()
    return {"created": created, "skipped": skipped}
