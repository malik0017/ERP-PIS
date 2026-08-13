# app/core/approval_chain.py
# =============================================================================
# Batch 111 — APPROVAL HIERARCHY BY VALUE
# -----------------------------------------------------------------------------
# Until now a 500 SAR requisition and a 500,000 SAR requisition followed the
# identical path: one signature from anyone holding purchase_requisition/edit.
# That undercuts the whole separation of duties Batch 94 built — the gate
# exists, but it costs the same to pass regardless of what is at stake.
#
# HOW IT WORKS
#
# Tiers are ranges, not a ladder of individual approvers:
#
#     0 – 5,000        SUPERVISOR                    1 approval
#     5,001 – 50,000   SUPERVISOR → MANAGER          2 approvals, in order
#     50,001 +         SUPERVISOR → MANAGER → ADMIN  3 approvals, in order
#
# A requisition finds its tier by value, then needs every step in that tier,
# in sequence. Step 2 cannot sign before step 1.
#
# FOUR RULES THAT MATTER, AND WHY
#
# 1. NO SELF-APPROVAL AT A HIGHER STEP. The person who raised it may satisfy
#    step 1 if their role fits, but never a subsequent step. One person
#    walking a large requisition through three steps alone is exactly the
#    control this exists to prevent.
#
# 2. NO SAME-PERSON DOUBLE-SIGNING. Two steps, two people. A user with both
#    MANAGER and ADMIN cannot sign for both.
#
# 3. VALUE IS LOCKED AT FIRST APPROVAL. Otherwise a 60,000 requisition gets
#    step 1, is edited down to 4,000, and completes on one signature — then
#    is edited back up. The tier is decided once and recorded.
#
# 4. A SUPERIOR ROLE CAN SIGN A JUNIOR STEP, BUT IT STILL COUNTS AS ONE STEP.
#    An ADMIN may satisfy the SUPERVISOR step when nobody else is available;
#    that does not collapse the remaining steps. Three signatures are still
#    three signatures.
# =============================================================================
from __future__ import annotations

from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

# Role seniority. A role can satisfy a step requiring its own rank or lower.
ROLE_RANK = {
    "VIEWER": 0, "STAFF": 1, "USER": 1,
    "SUPERVISOR": 2, "HEAD_CHEF": 2, "HEAD_CHEF_PLANNING": 2,
    "PROCUREMENT": 2, "STORE": 2, "QC": 2,
    "MANAGER": 3,
    "ADMIN": 4, "SUPER_ADMIN": 5, "SUPERADMIN": 5,
}

DEFAULT_TIERS = [
    # Ranges are [min, max) — max is exclusive, so they tile with no overlap.
    {"min_value": 0,     "max_value": 5000,   "steps": "SUPERVISOR"},
    {"min_value": 5000,  "max_value": 50000,  "steps": "SUPERVISOR,MANAGER"},
    {"min_value": 50000, "max_value": None,   "steps": "SUPERVISOR,MANAGER,ADMIN"},
]


def rank(role: str) -> int:
    return ROLE_RANK.get((role or "").strip().upper().replace(" ", "_"), 1)


def ensure_schema(db: Session) -> None:
    try:
        db.execute(text("""
            CREATE TABLE IF NOT EXISTS approval_tiers (
                id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                company_id INT NULL,
                doc_type VARCHAR(40) NOT NULL DEFAULT 'purchase_requisition',
                min_value DECIMAL(18,4) NOT NULL DEFAULT 0,
                max_value DECIMAL(18,4) NULL,
                steps VARCHAR(255) NOT NULL,
                is_active TINYINT(1) NOT NULL DEFAULT 1,
                updated_by VARCHAR(120) NULL,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                KEY idx_tier_doc (doc_type, min_value)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """))
        db.execute(text("""
            CREATE TABLE IF NOT EXISTS approval_steps (
                id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                company_id INT NULL,
                doc_type VARCHAR(40) NOT NULL,
                doc_no VARCHAR(80) NOT NULL,
                step_no INT NOT NULL,
                required_role VARCHAR(60) NOT NULL,
                status VARCHAR(20) NOT NULL DEFAULT 'Pending',
                approved_by_id INT NULL,
                approved_by VARCHAR(120) NULL,
                approved_role VARCHAR(60) NULL,
                approved_at DATETIME NULL,
                note VARCHAR(500) NULL,
                doc_value DECIMAL(18,4) NOT NULL DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE KEY uq_step (doc_type, doc_no, step_no),
                KEY idx_step_doc (doc_type, doc_no)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """))
        db.commit()
        seed_defaults(db)
    except Exception:
        db.rollback()


def seed_defaults(db: Session, doc_type: str = "purchase_requisition") -> None:
    """Seed the default ladder once, only if nothing is configured.

    Seeding on every call would silently resurrect tiers an admin deliberately
    deleted.
    """
    try:
        n = db.execute(text("SELECT COUNT(*) FROM approval_tiers WHERE doc_type = :d"),
                       {"d": doc_type}).scalar() or 0
        if n:
            return
        for t in DEFAULT_TIERS:
            db.execute(text("""
                INSERT INTO approval_tiers (company_id, doc_type, min_value, max_value, steps, is_active)
                VALUES (NULL, :d, :mn, :mx, :s, 1)
            """), {"d": doc_type, "mn": t["min_value"], "mx": t["max_value"], "s": t["steps"]})
        db.commit()
    except Exception:
        db.rollback()


def tier_for(db: Session, value: float, cid: int,
             doc_type: str = "purchase_requisition") -> list[str]:
    """The ordered step list for a document of this value."""
    ensure_schema(db)
    try:
        row = db.execute(text("""
            SELECT steps FROM approval_tiers
            WHERE doc_type = :d AND is_active = 1
              AND (company_id = :cid OR company_id IS NULL)
              AND :v >= min_value
              -- Batch 111: max_value is EXCLUSIVE. With <= the ranges
              -- 0–5,000 and 5,000–50,000 both contained exactly 5,000, so a
              -- requisition of precisely 5,000 matched two tiers and the
              -- result depended on the ORDER BY. Boundary values are common
              -- (round-number budgets), so this had to be unambiguous.
              AND (max_value IS NULL OR :v < max_value)
            ORDER BY min_value DESC
            LIMIT 1
        """), {"d": doc_type, "cid": cid, "v": float(value or 0)}).scalar()
    except Exception:
        row = None
    if not row:
        return ["MANAGER"]
    return [s.strip().upper() for s in str(row).split(",") if s.strip()]


def build_chain(db: Session, doc_type: str, doc_no: str, value: float, cid: int) -> list[dict]:
    """Create the approval steps for a document, once.

    Idempotent: if steps already exist they are returned unchanged. This is
    rule 3 — the tier is decided at first approval and the value that decided
    it is recorded on every step, so editing the document afterwards cannot
    change how many signatures it needs.
    """
    ensure_schema(db)
    existing = get_chain(db, doc_type, doc_no)
    if existing:
        return existing

    steps = tier_for(db, value, cid, doc_type)
    for i, role in enumerate(steps, start=1):
        try:
            db.execute(text("""
                INSERT INTO approval_steps
                    (company_id, doc_type, doc_no, step_no, required_role, status, doc_value)
                VALUES (:cid, :d, :n, :s, :r, 'Pending', :v)
            """), {"cid": cid, "d": doc_type, "n": doc_no, "s": i, "r": role,
                   "v": float(value or 0)})
        except Exception:
            db.rollback()
    db.commit()
    return get_chain(db, doc_type, doc_no)


def get_chain(db: Session, doc_type: str, doc_no: str) -> list[dict]:
    try:
        return [dict(r) for r in db.execute(text("""
            SELECT * FROM approval_steps
            WHERE doc_type = :d AND doc_no = :n
            ORDER BY step_no
        """), {"d": doc_type, "n": doc_no}).mappings().all()]
    except Exception:
        return []


def next_step(chain: list[dict]) -> dict | None:
    for s in chain:
        if s["status"] != "Approved":
            return s
    return None


def is_complete(chain: list[dict]) -> bool:
    return bool(chain) and all(s["status"] == "Approved" for s in chain)


def can_approve(chain: list[dict], user_id, username: str, user_role: str,
                raised_by: str = "") -> tuple[bool, str]:
    """May THIS user sign the next outstanding step? Returns (ok, reason)."""
    step = next_step(chain)
    if step is None:
        return False, "Already fully approved."

    need = (step["required_role"] or "").upper()
    if rank(user_role) < rank(need):
        return False, (f"Step {step['step_no']} needs {need} or higher. "
                       f"Your role is {(user_role or 'unknown').upper()}.")

    # Rule 2 — two steps, two people.
    for s in chain:
        if s["status"] == "Approved":
            same_id = (user_id is not None and s["approved_by_id"] is not None
                       and int(s["approved_by_id"]) == int(user_id))
            same_name = (s["approved_by"] or "").lower() == (username or "").lower()
            if same_id or same_name:
                return False, (f"You already approved step {s['step_no']}. "
                               "A second signature must come from someone else.")

    # Rule 1 — the raiser never signs beyond the first step.
    if step["step_no"] > 1 and raised_by and (raised_by or "").lower() == (username or "").lower():
        return False, ("You raised this requisition, so you cannot approve it beyond "
                       "the first step.")

    return True, ""


def approve_step(db: Session, doc_type: str, doc_no: str, user_id, username: str,
                 user_role: str, note: str = "") -> tuple[bool, str, list[dict]]:
    """Sign the next outstanding step. Returns (ok, message, updated chain)."""
    chain = get_chain(db, doc_type, doc_no)
    step = next_step(chain)
    if step is None:
        return False, "This document is already fully approved.", chain

    db.execute(text("""
        UPDATE approval_steps
        SET status = 'Approved', approved_by_id = :uid, approved_by = :u,
            approved_role = :r, approved_at = :at, note = :n
        WHERE id = :i
    """), {"uid": user_id, "u": username, "r": (user_role or "").upper(),
           "at": datetime.utcnow(), "n": (note or "")[:500] or None, "i": step["id"]})
    db.commit()

    chain = get_chain(db, doc_type, doc_no)
    if is_complete(chain):
        return True, f"Step {step['step_no']} approved — the document is now fully approved.", chain
    nxt = next_step(chain)
    return True, (f"Step {step['step_no']} approved. "
                  f"Step {nxt['step_no']} still needs {nxt['required_role']}."), chain


def reset_chain(db: Session, doc_type: str, doc_no: str) -> None:
    """Clear the chain — used when a document is rejected, so a resubmission
    starts from step 1 rather than inheriting stale signatures."""
    try:
        db.execute(text("DELETE FROM approval_steps WHERE doc_type = :d AND doc_no = :n"),
                   {"d": doc_type, "n": doc_no})
        db.commit()
    except Exception:
        db.rollback()
