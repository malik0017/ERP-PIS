# app/services/section_stock.py
# =============================================================================
# Batch D — STORE ISSUANCE CARRY-FORWARD (Section Standing Stock)
# -----------------------------------------------------------------------------
# The store often issues MORE than an order needs. That surplus used to be
# invisible, so the next order re-issued the same material. This ledger tracks
# what is already standing at each section so a new order can draw it down
# first.
#
# Model — one signed movement per finalized issue line:
#     delta = issued_qty_standard - required_qty_with_waste_standard
# Balance(section, ingredient) = SUM(delta). Over-issuing adds (+); issuing less
# than required (because standing stock covered it) draws down (-). The ledger
# therefore nets out automatically:
#     Order 1  required 100, issued 120  ->  +20   (balance 20)
#     Order 2  required 100, available 20 -> issue 80  ->  80-100 = -20 (balance 0)
# so the store issues 200 for 200 required — never twice.
#
# Append-only + idempotent per order (re-finalizing replaces that order's rows),
# so history is preserved and month-end review is exact. Every call is defensive:
# a ledger problem must never block a store issue or a kitchen transfer.
# =============================================================================

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

_DDL = """
CREATE TABLE IF NOT EXISTS section_ingredient_ledger (
  id INT AUTO_INCREMENT PRIMARY KEY,
  company_id INT NULL,
  section VARCHAR(100) NOT NULL,
  ingredient_code VARCHAR(80) NOT NULL,
  ingredient_name VARCHAR(255) NULL,
  order_no VARCHAR(80) NULL,
  movement VARCHAR(30) NOT NULL,
  qty_standard DOUBLE NOT NULL DEFAULT 0,
  standard_uom VARCHAR(20) NULL,
  note VARCHAR(255) NULL,
  created_by VARCHAR(255) NULL,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  INDEX ix_sil_key (company_id, section, ingredient_code),
  INDEX ix_sil_order (order_no)
)
"""


def ensure_schema(db: Session) -> None:
    """Create the ledger table if it does not exist. Cheap and idempotent."""
    try:
        db.execute(text(_DDL))
        db.commit()
    except Exception:
        db.rollback()


def post_order_issue(db: Session, order_no: str, company_id: int | None, by: str | None = None) -> int:
    """Record this order's issue deltas. Idempotent: any prior 'issue_delta' rows
    for the order are removed first, so re-finalizing never double-posts.
    Returns the number of ledger rows written."""
    ensure_schema(db)
    try:
        db.execute(text("DELETE FROM section_ingredient_ledger "
                        "WHERE order_no = :o AND movement = 'issue_delta'"),
                   {"o": order_no})
        rows = db.execute(text("""
            SELECT issue_to_section AS section, ingredient_code, MAX(ingredient_name) AS ingredient_name,
                   MAX(standard_uom) AS uom,
                   SUM(COALESCE(issued_qty_standard, input_material_issued, 0)
                       - COALESCE(required_qty_with_waste_standard, required_qty_standard, 0)) AS delta
            FROM store_issuance_lines
            WHERE order_no = :o AND COALESCE(finalized, 0) = 1
            GROUP BY issue_to_section, ingredient_code
        """), {"o": order_no}).mappings().all()
        n = 0
        for r in rows:
            delta = float(r["delta"] or 0)
            if abs(delta) < 1e-6 or not r["section"] or not r["ingredient_code"]:
                continue
            db.execute(text("""
                INSERT INTO section_ingredient_ledger
                    (company_id, section, ingredient_code, ingredient_name, order_no,
                     movement, qty_standard, standard_uom, note, created_by)
                VALUES (:cid, :sec, :code, :name, :o, 'issue_delta', :qty, :uom, :note, :by)
            """), {"cid": company_id, "sec": r["section"], "code": r["ingredient_code"],
                   "name": r["ingredient_name"], "o": order_no, "qty": delta,
                   "uom": r["uom"], "by": by,
                   "note": ("over-issued surplus" if delta > 0 else "drew down standing stock")})
            n += 1
        db.commit()
        return n
    except Exception:
        db.rollback()
        return 0


def available(db: Session, company_id: int | None, section: str, ingredient_code: str) -> float:
    """Current standing balance for one (section, ingredient)."""
    try:
        v = db.execute(text("""
            SELECT COALESCE(SUM(qty_standard), 0)
            FROM section_ingredient_ledger
            WHERE section = :sec AND ingredient_code = :code
              AND (company_id = :cid OR company_id IS NULL OR :cid IS NULL)
        """), {"sec": section, "code": ingredient_code, "cid": company_id}).scalar()
        return float(v or 0)
    except Exception:
        return 0.0


def available_map(db: Session, company_id: int | None, pairs: list[tuple[str, str]]) -> dict[str, float]:
    """Balances for a set of (section, ingredient_code) pairs, keyed 'section||code'."""
    ensure_schema(db)
    out: dict[str, float] = {}
    want = {f"{s}||{c}" for s, c in pairs if s and c}
    if not want:
        return out
    try:
        rows = db.execute(text("""
            SELECT section, ingredient_code, COALESCE(SUM(qty_standard), 0) AS bal
            FROM section_ingredient_ledger
            WHERE (company_id = :cid OR company_id IS NULL OR :cid IS NULL)
            GROUP BY section, ingredient_code
        """), {"cid": company_id}).mappings().all()
        for r in rows:
            k = f"{r['section']}||{r['ingredient_code']}"
            if k in want:
                out[k] = float(r["bal"] or 0)
    except Exception:
        pass
    return out


def standing(db: Session, company_id: int | None, section: str | None = None,
             date_from: str | None = None, date_to: str | None = None,
             nonzero_only: bool = True) -> list[dict]:
    """Section Standing Stock report: current balance per (section, ingredient),
    with the age of the oldest movement and how many orders contributed."""
    ensure_schema(db)
    where = ["(company_id = :cid OR company_id IS NULL OR :cid IS NULL)"]
    params: dict = {"cid": company_id}
    if section:
        where.append("section = :sec")
        params["sec"] = section
    if date_from:
        where.append("created_at >= :df")
        params["df"] = date_from
    if date_to:
        where.append("created_at <= :dt")
        params["dt"] = date_to
    w = " AND ".join(where)
    having = "HAVING ABS(SUM(qty_standard)) > 0.000001" if nonzero_only else ""
    try:
        rows = db.execute(text(f"""
            SELECT section, ingredient_code, MAX(ingredient_name) AS ingredient_name,
                   MAX(standard_uom) AS uom,
                   SUM(qty_standard) AS balance,
                   MIN(created_at) AS oldest,
                   MAX(created_at) AS latest,
                   COUNT(DISTINCT order_no) AS orders
            FROM section_ingredient_ledger
            WHERE {w}
            GROUP BY section, ingredient_code
            {having}
            ORDER BY section, balance DESC
        """), params).mappings().all()
        return [dict(r) for r in rows]
    except Exception:
        return []
