# app/modules/qc/sampling.py
# =============================================================================
# Batch 94 — RANDOM QC SAMPLING ON GRN (skip-lot / reduced inspection)
# -----------------------------------------------------------------------------
# The ask was "1-in-10 style check before stock posts". This implements that,
# but not as a blind 1-in-10 coin flip, because this is a food business and a
# blind coin flip on food receipts is not defensible.
#
# DESIGN DECISION (change it if you disagree — it is all in one place):
#
#   Sampling is OFF by default. Upgrading changes nothing until someone
#   explicitly turns it on per company. Every GRN keeps going to QC Hold
#   exactly as it does today.
#
#   When it IS on, a receipt is auto-released ONLY if every one of these
#   holds. Any single one forces a full inspection:
#
#     1. The interval hasn't come up      (every Nth receipt is always inspected)
#     2. No item on it is a critical item (ingredients.critical_item)
#     3. No item is temperature-controlled (Chilled / Frozen storage_type)
#     4. The supplier has no QC failure in the recent look-back window
#     5. The supplier has cleared a minimum number of receipts already
#        (no track record = no reduced inspection)
#
#   Rules 2-5 are the guardrails. Skipping inspection on frozen chicken from
#   a supplier who failed last week is not a sampling plan, it's an incident
#   waiting to be traced back to this file. Rule 4 in particular is the
#   standard "failure reverts to tightened inspection" behaviour from
#   sampling schemes like ISO 2859 / MIL-STD-105 — one failure and that
#   supplier goes back to 100% inspection until they earn their way out.
#
#   Auto-released receipts are NOT invisible. Each one writes a
#   qc_incoming_inspections row with decision='Auto-Released' recording which
#   rule let it through and where it sat in the sampling interval, so the
#   audit trail answers "why was this batch never inspected" years later.
# =============================================================================
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session


DEFAULTS = {
    "enabled": 0,
    "sample_every_n": 10,      # inspect 1 in every N receipts
    "min_clean_receipts": 5,   # supplier must clear this many before skipping starts
    "failure_lookback_days": 30,
    "always_inspect_critical": 1,
    "always_inspect_cold_chain": 1,
}

COLD_CHAIN = ("CHILLED", "FROZEN", "COLD", "REFRIGERATED")


def ensure_schema(db: Session) -> None:
    """One row per company. Column-per-setting, matching system_settings."""
    try:
        db.execute(text("""
            CREATE TABLE IF NOT EXISTS qc_sampling_config (
                id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                company_id INT NOT NULL DEFAULT 1,
                enabled TINYINT(1) NOT NULL DEFAULT 0,
                sample_every_n INT NOT NULL DEFAULT 10,
                min_clean_receipts INT NOT NULL DEFAULT 5,
                failure_lookback_days INT NOT NULL DEFAULT 30,
                always_inspect_critical TINYINT(1) NOT NULL DEFAULT 1,
                always_inspect_cold_chain TINYINT(1) NOT NULL DEFAULT 1,
                updated_by VARCHAR(120) NULL,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                UNIQUE KEY uq_qsc_company (company_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """))
        # decision column was VARCHAR(20) and now needs to hold 'Auto-Released'
        # (13 chars, fits) — no ALTER needed, noted so nobody widens it twice.
        db.commit()
    except Exception:
        db.rollback()


def get_config(db: Session, company_id: int) -> dict:
    ensure_schema(db)
    row = db.execute(text("""
        SELECT * FROM qc_sampling_config WHERE company_id = :cid LIMIT 1
    """), {"cid": company_id}).mappings().first()
    if not row:
        return {**DEFAULTS, "company_id": company_id}
    return dict(row)


def save_config(db: Session, company_id: int, values: dict, updated_by: str = "system") -> None:
    ensure_schema(db)
    payload = {**DEFAULTS, **{k: v for k, v in values.items() if k in DEFAULTS}}
    payload.update({"cid": company_id, "by": updated_by})
    db.execute(text("""
        INSERT INTO qc_sampling_config
            (company_id, enabled, sample_every_n, min_clean_receipts,
             failure_lookback_days, always_inspect_critical, always_inspect_cold_chain, updated_by)
        VALUES (:cid, :enabled, :sample_every_n, :min_clean_receipts,
                :failure_lookback_days, :always_inspect_critical, :always_inspect_cold_chain, :by)
        ON DUPLICATE KEY UPDATE
            enabled = VALUES(enabled),
            sample_every_n = VALUES(sample_every_n),
            min_clean_receipts = VALUES(min_clean_receipts),
            failure_lookback_days = VALUES(failure_lookback_days),
            always_inspect_critical = VALUES(always_inspect_critical),
            always_inspect_cold_chain = VALUES(always_inspect_cold_chain),
            updated_by = VALUES(updated_by)
    """), payload)
    db.commit()


def decide(db: Session, *, company_id: int, supplier_name: str,
           inventory_codes: list[str]) -> tuple[str, str]:
    """Return (qc_status, reason) for a GRN about to be posted.

    qc_status is 'Pending' (goes to QC Hold, inspector must clear it) or
    'Passed' (auto-released under the sampling plan). Reason is recorded
    either way, so nothing about this decision is silent.

    Fails CLOSED: any error anywhere in here returns Pending. A bug in the
    sampling logic must never be the reason uninspected food reached the
    kitchen.
    """
    try:
        # Same reasoning as the config screen: this runs during GRN posting,
        # which can easily happen on a database where Incoming QC has never
        # been opened and the inspections table doesn't exist yet.
        try:
            from app.modules.qc.routes import _ensure_incoming_qc_schema
            _ensure_incoming_qc_schema(db)
        except Exception:
            pass
        cfg = get_config(db, company_id)
        if not int(cfg.get("enabled") or 0):
            return "Pending", "Sampling disabled — full inspection"

        n = max(int(cfg.get("sample_every_n") or 10), 2)
        supplier = (supplier_name or "").strip()
        if not supplier:
            return "Pending", "No supplier on receipt — full inspection"

        # --- Guardrail: critical / cold-chain items are never skipped ---
        if inventory_codes:
            ph = ",".join(f":c{i}" for i in range(len(inventory_codes)))
            params = {f"c{i}": c for i, c in enumerate(inventory_codes)}
            rows = db.execute(text(f"""
                SELECT COALESCE(critical_item, 0) AS critical,
                       UPPER(COALESCE(storage_type, '')) AS storage
                FROM ingredients WHERE ingredient_code IN ({ph})
            """), params).mappings().all()

            if int(cfg.get("always_inspect_critical") or 0):
                if any(int(r["critical"] or 0) for r in rows):
                    return "Pending", "Contains a critical item — always inspected"

            if int(cfg.get("always_inspect_cold_chain") or 0):
                if any(any(k in (r["storage"] or "") for k in COLD_CHAIN) for r in rows):
                    return "Pending", "Temperature-controlled item — always inspected"

        # --- Guardrail: recent failure reverts this supplier to 100% ---
        lookback = int(cfg.get("failure_lookback_days") or 30)
        recent_fail = db.execute(text("""
            SELECT COUNT(*) FROM qc_incoming_inspections
            WHERE supplier_name = :s AND decision = 'Failed'
              AND inspected_at >= DATE_SUB(NOW(), INTERVAL :d DAY)
              AND (company_id = :cid OR company_id IS NULL)
        """), {"s": supplier, "d": lookback, "cid": company_id}).scalar() or 0
        if recent_fail:
            return "Pending", f"Supplier failed QC within {lookback} days — tightened inspection"

        # --- Guardrail: no track record, no reduced inspection ---
        cleared = db.execute(text("""
            SELECT COUNT(*) FROM qc_incoming_inspections
            WHERE supplier_name = :s AND decision IN ('Passed', 'Auto-Released')
              AND (company_id = :cid OR company_id IS NULL)
        """), {"s": supplier, "cid": company_id}).scalar() or 0
        min_clean = int(cfg.get("min_clean_receipts") or 5)
        if cleared < min_clean:
            return "Pending", f"Supplier has {cleared}/{min_clean} clean receipts — building track record"

        # --- The interval itself ---
        # Counted per supplier, from that supplier's own receipt history, so
        # one busy supplier doesn't consume another's sampling slots.
        total = db.execute(text("""
            SELECT COUNT(*) FROM grn_receipts
            WHERE supplier_name = :s AND (company_id = :cid OR company_id IS NULL)
        """), {"s": supplier, "cid": company_id}).scalar() or 0
        position = (total % n) + 1          # 1..n, this receipt's slot
        if position == 1:
            return "Pending", f"Sampling interval hit (receipt {position} of {n}) — inspected"

        return "Passed", f"Auto-released under sampling plan (receipt {position} of {n}, supplier clear)"

    except Exception as exc:
        return "Pending", f"Sampling check unavailable ({type(exc).__name__}) — defaulted to full inspection"


def record_auto_release(db: Session, *, company_id: int, grn_no: str, po_no: str,
                        supplier_name: str, reason: str, by: str) -> None:
    """Write the audit row for a receipt that skipped inspection.

    Without this, an auto-released GRN would simply never appear in the QC
    records at all — which reads identically to "the inspector forgot".
    """
    try:
        db.execute(text("""
            INSERT INTO qc_incoming_inspections
                (company_id, grn_no, po_no, supplier_name, decision, notes, inspected_by)
            VALUES (:cid, :grn, :po, :sup, 'Auto-Released', :notes, :by)
        """), {"cid": company_id, "grn": grn_no, "po": po_no, "sup": supplier_name,
               "notes": reason[:500], "by": f"{by} (sampling plan)"})
        db.commit()
    except Exception:
        db.rollback()
