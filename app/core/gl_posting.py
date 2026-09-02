# app/core/gl_posting.py
from __future__ import annotations
import logging
from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

ACCT = {
    "bank":          "1110",   
    "ar":            "1120",   
    "inventory":     "1130",   
    "ap":            "2100",   
    "gr_accrual":    "2200",   
    "vat_payable":   "2300",   
    "revenue":       "4100",   
    "cogs":          "5100",   
    "wastage":       "5900",   
    "inv_adj":       "5100",   
}

# Full chart of accounts required by the workflow (seeded idempotently).
WORKFLOW_COA = [
    ("1000", "Assets", "Asset"),
    ("1100", "Current Assets", "Asset"),
    ("1110", "Cash & Bank", "Asset"),
    ("1120", "Accounts Receivable", "Asset"),
    ("1130", "Inventory", "Asset"),
    ("1200", "Fixed Assets", "Asset"),
    ("2000", "Liabilities", "Liability"),
    ("2100", "Accounts Payable", "Liability"),
    ("2200", "GR Accrual", "Liability"),
    ("2300", "VAT Payable", "Liability"),
    ("3000", "Equity", "Equity"),
    ("4000", "Revenue", "Income"),
    ("4100", "Sales Revenue", "Income"),
    ("5000", "Expenses", "Expense"),
    ("5100", "Cost of Goods Sold / WIP", "Expense"),
    ("5200", "Operating Expenses", "Expense"),
    ("5900", "Production Wastage", "Expense"),
]

DEFAULT_VAT_RATE = 0.15


def ensure_workflow_coa(db: Session) -> None:
    """Seed the workflow chart of accounts (idempotent, company-agnostic rows)."""
    try:
        db.execute(text("""
            CREATE TABLE IF NOT EXISTS gl_accounts (
                id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                company_id INT NULL,
                account_code VARCHAR(20) NOT NULL,
                account_name VARCHAR(255) NOT NULL,
                account_type VARCHAR(30) NOT NULL,
                is_active TINYINT(1) NOT NULL DEFAULT 1,
                UNIQUE KEY uq_gl_acct (company_id, account_code)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """))
        for code, name, typ in WORKFLOW_COA:
            n = db.execute(text(
                "SELECT COUNT(*) FROM gl_accounts WHERE account_code=:c AND COALESCE(company_id,0)=0"
            ), {"c": code}).scalar()
            if not n:
                db.execute(text("""
                    INSERT INTO gl_accounts (company_id, account_code, account_name, account_type)
                    VALUES (NULL, :c, :n, :t)
                """), {"c": code, "n": name, "t": typ})
        db.commit()
    except Exception as exc:
        logger.warning("ensure_workflow_coa failed: %s", exc)
        try:
            db.rollback()
        except Exception:
            pass


def _post(db: Session, request, source_type: str, source_no: str,
          memo: str, lines: list[tuple[str, float, float, str]]) -> str | None:
    """Delegate to finance.post_journal (idempotent + balanced). Never raises."""
    try:
        from app.modules.finance.routes import post_journal
        ensure_workflow_coa(db)
        return post_journal(db, request, source_type, source_no, memo, lines)
    except Exception as exc:
        logger.warning("gl_posting._post %s %s failed: %s", source_type, source_no, exc)
        return None

def post_grn_journal(db, request, grn_no: str, value: float,
                     supplier: str = "") -> str | None:
    """GRN confirmed:  Dr 1130 Inventory  /  Cr 2200 GR accrual."""
    value = round(float(value or 0), 4)
    if value <= 0:
        return None
    return _post(db, request, "GRN", grn_no,
                 f"Goods received {grn_no}",
                 [(ACCT["inventory"], value, 0.0, supplier),
                  (ACCT["gr_accrual"], 0.0, value, supplier)])


def post_issuance_journal(db, request, order_no: str, value: float,
                          section: str = "") -> str | None:
    """Store issue posted:  Dr 5100 WIP/COGS  /  Cr 1130 Inventory."""
    value = round(float(value or 0), 4)
    if value <= 0:
        return None
    return _post(db, request, "STORE_ISSUE", order_no,
                 f"Store issuance {order_no}",
                 [(ACCT["cogs"], value, 0.0, section),
                  (ACCT["inventory"], 0.0, value, section)])


def post_dispatch_cogs_journal(db, request, order_no: str, value: float,
                               customer: str = "") -> str | None:
    """Delivery confirmed:  Dr 5100 COGS  /  Cr 1130 Inventory."""
    value = round(float(value or 0), 4)
    if value <= 0:
        return None
    return _post(db, request, "DISPATCH_COGS", order_no,
                 f"Delivery COGS {order_no}",
                 [(ACCT["cogs"], value, 0.0, customer),
                  (ACCT["inventory"], 0.0, value, customer)])


def post_adjustment_journal(db, request, ref_no: str, value: float,
                            write_up: bool, note: str = "") -> str | None:
    """Inventory count posted. write_up=True -> stock increased.
       Up:   Dr 1130 Inventory  /  Cr 5100 Adj expense
       Down: Dr 5100 Adj expense /  Cr 1130 Inventory."""
    value = round(abs(float(value or 0)), 4)
    if value <= 0:
        return None
    if write_up:
        lines = [(ACCT["inventory"], value, 0.0, note),
                 (ACCT["inv_adj"], 0.0, value, note)]
    else:
        lines = [(ACCT["inv_adj"], value, 0.0, note),
                 (ACCT["inventory"], 0.0, value, note)]
    return _post(db, request, "INV_ADJUST", ref_no,
                 f"Inventory adjustment {ref_no}", lines)
