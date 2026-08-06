# app/modules/finance/routes_statements.py
# ============================================================================
# Batch 67 — FINANCIAL STATEMENTS (rewritten to match the real GL schema)
# ----------------------------------------------------------------------------
# WHY THIS WAS REWRITTEN
# ----------------------
# The Batch 29 version was never registered in main.py AND every query used
# column/table names that don't exist in this project's ledger:
#     it used  jl.debit_amount / jl.credit_amount / jl.debit_date /
#              jl.journal_id / jl.company_id / ca.code
#     reality  gl_journal_lines(journal_no, account_code, debit, credit, party)
#              gl_journals(journal_no, journal_date, company_id, source_type,…)
#              gl_accounts(account_code, account_name, account_type)
# So even if registered, all statements returned empty. It also excluded the
# OLD inventory code 1300 instead of the workflow code 1130.
#
# This rewrite:
#   * joins lines -> journals for company + date filters (lines carry neither),
#   * uses the real column names,
#   * classifies by account_type first (falling back to code prefix), so it
#     works whether accounts are typed Asset/Liability/Equity/Income/Expense
#     or only carry workflow codes (1xxx/2xxx/3xxx/4xxx/5xxx),
#   * produces P&L, Balance Sheet, Cash Flow and AR/AP Aging that read the GL
#     now fed by every operational source (Batch 66).
#
# Registered in main.py:
#     from app.modules.finance.routes_statements import router as finance_statements_router
#     app.include_router(finance_statements_router)
# ============================================================================

from datetime import datetime, timedelta
import logging

from fastapi import APIRouter, Depends, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.templates import render
from app.core.rbac import require_action
from app.database.session import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/finance", tags=["Financial Statements"])


def _company_id(request: Request) -> int:
    try:
        return int(request.session.get("company_id") or 1)
    except Exception:
        return 1


def _parse_date(date_str, default_offset_days: int = -365) -> str:
    if not date_str:
        return (datetime.now() + timedelta(days=default_offset_days)).strftime("%Y-%m-%d")
    return str(date_str)


def _company_filter(cid: int) -> str:
    """GL journals may carry company_id NULL (shared) or the real id. Match both
    so a single-company install (everything NULL) still returns rows."""
    return "(j.company_id = :cid OR j.company_id IS NULL)"


# ============================================================================
# PROFIT & LOSS  —  Revenue − COGS − Expenses = Net income
# ============================================================================
@router.get("/statements/profit-loss")
async def profit_loss_statement(
    request: Request,
    from_date: str = None,
    to_date: str = None,
    db: Session = Depends(get_db),
):
    require_action(request, "finance", "view")
    from_date = _parse_date(from_date, -365)
    to_date = _parse_date(to_date, 0)
    cid = _company_id(request)

    # Income accounts: type Income OR code 4xxx. Expense/COGS: type Expense OR 5xxx/6xxx.
    sql = f"""
        SELECT a.account_code, a.account_name,
               COALESCE(a.account_type,'') AS account_type,
               CASE
                 WHEN a.account_type = 'Income'  OR a.account_code LIKE '4%' THEN 'Revenue'
                 WHEN a.account_code LIKE '5%'                               THEN 'COGS'
                 WHEN a.account_type = 'Expense' OR a.account_code LIKE '6%' THEN 'Expenses'
                 ELSE 'Other'
               END AS category,
               ROUND(SUM(COALESCE(l.credit,0) - COALESCE(l.debit,0)), 2) AS amount
        FROM gl_journal_lines l
        JOIN gl_journals j ON j.journal_no = l.journal_no
        JOIN gl_accounts a ON a.account_code = l.account_code
        WHERE j.journal_date BETWEEN :from_date AND :to_date
          AND {_company_filter(cid)}
          AND ( a.account_type IN ('Income','Expense')
                OR a.account_code LIKE '4%' OR a.account_code LIKE '5%' OR a.account_code LIKE '6%' )
        GROUP BY a.account_code, a.account_name, a.account_type, category
        HAVING amount <> 0
        ORDER BY category, a.account_code
    """
    try:
        rows = [dict(r) for r in db.execute(
            text(sql), {"from_date": from_date, "to_date": to_date, "cid": cid}
        ).mappings().all()]
    except Exception as exc:
        logger.error("P&L query failed: %s", exc)
        rows = []

    # For revenue (credit-natural) amount is positive income; for expense/COGS
    # (debit-natural) credit-debit is negative -> flip sign so they read as costs.
    revenue = [r for r in rows if r["category"] == "Revenue"]
    cogs = [{**r, "amount": round(-r["amount"], 2)} for r in rows if r["category"] == "COGS"]
    expenses = [{**r, "amount": round(-r["amount"], 2)} for r in rows if r["category"] == "Expenses"]

    total_revenue = round(sum(r["amount"] for r in revenue), 2)
    total_cogs = round(sum(r["amount"] for r in cogs), 2)
    total_expenses = round(sum(r["amount"] for r in expenses), 2)
    gross_profit = round(total_revenue - total_cogs, 2)
    net_income = round(gross_profit - total_expenses, 2)

    return render(request, "finance/profit_loss.html", {
        "from_date": from_date, "to_date": to_date, "cost_center": None,
        "revenue": revenue, "cogs": cogs, "expenses": expenses,
        "totals": {
            "total_revenue": total_revenue, "total_cogs": total_cogs,
            "gross_profit": gross_profit, "total_expenses": total_expenses,
            "net_income": net_income,
        },
        "page_title": "Profit & Loss Statement",
    })


# ============================================================================
# BALANCE SHEET  —  Assets = Liabilities + Equity (+ retained earnings)
# ============================================================================
@router.get("/statements/balance-sheet")
async def balance_sheet_statement(
    request: Request,
    as_of_date: str = None,
    db: Session = Depends(get_db),
):
    require_action(request, "finance", "view")
    as_of = _parse_date(as_of_date, 0)
    cid = _company_id(request)

    sql = f"""
        SELECT a.account_code, a.account_name,
               CASE
                 WHEN a.account_type = 'Asset'     OR a.account_code LIKE '1%' THEN 'Assets'
                 WHEN a.account_type = 'Liability'  OR a.account_code LIKE '2%' THEN 'Liabilities'
                 WHEN a.account_type = 'Equity'     OR a.account_code LIKE '3%' THEN 'Equity'
                 ELSE 'Other'
               END AS category,
               ROUND(SUM(COALESCE(l.debit,0) - COALESCE(l.credit,0)), 2) AS balance
        FROM gl_journal_lines l
        JOIN gl_journals j ON j.journal_no = l.journal_no
        JOIN gl_accounts a ON a.account_code = l.account_code
        WHERE j.journal_date <= :as_of
          AND {_company_filter(cid)}
          AND ( a.account_type IN ('Asset','Liability','Equity')
                OR a.account_code LIKE '1%' OR a.account_code LIKE '2%' OR a.account_code LIKE '3%' )
        GROUP BY a.account_code, a.account_name, category
        HAVING balance <> 0
        ORDER BY category, a.account_code
    """
    try:
        rows = [dict(r) for r in db.execute(
            text(sql), {"as_of": as_of, "cid": cid}
        ).mappings().all()]
    except Exception as exc:
        logger.error("Balance Sheet query failed: %s", exc)
        rows = []

    # ------------------------------------------------------------------
    # Batch 79 fix — 500 error: "unsupported operand type(s) for -:
    # 'decimal.Decimal' and 'float'".
    #
    # MySQL's SUM() comes back through raw text() queries as
    # decimal.Decimal (via mappings()), not float. Meanwhile `retained`
    # a few lines below was explicitly cast with float(...). Python's
    # Decimal deliberately refuses to auto-mix with float (to avoid
    # silent precision loss), so the moment one side of a calculation
    # was a Decimal and the other a float, this raised instead of adding.
    # It only ever surfaced once a company had posted to Asset accounts
    # but not yet to Liability/Equity accounts (an empty list sums to
    # plain int 0, not Decimal) — a normal state for a system this early
    # in real use, which is exactly why it hadn't shown up before now.
    #
    # Fix: normalize every balance to float the moment it leaves the DB
    # layer, so every downstream sum/round/subtract operates on one
    # consistent type. This removes the whole class of bug, not just the
    # one combination that happened to trigger here.
    # ------------------------------------------------------------------
    for r in rows:
        r["balance"] = float(r["balance"] or 0)

    # Assets are debit-natural (positive as computed). Liabilities & equity are
    # credit-natural, so debit-credit is negative -> flip so they read positive.
    assets = [r for r in rows if r["category"] == "Assets"]
    liabilities = [{**r, "balance": round(-r["balance"], 2)} for r in rows if r["category"] == "Liabilities"]
    equity = [{**r, "balance": round(-r["balance"], 2)} for r in rows if r["category"] == "Equity"]

    total_assets = round(sum(r["balance"] for r in assets), 2)
    total_liab = round(sum(r["balance"] for r in liabilities), 2)
    total_equity_posted = round(sum(r["balance"] for r in equity), 2)

    # Retained earnings = net income to date (income − expense across all time up
    # to as_of). This makes the sheet balance without a period-close posting.
    re_row = 0.0
    try:
        re_row = float(db.execute(text(f"""
            SELECT ROUND(SUM(COALESCE(l.credit,0) - COALESCE(l.debit,0)),2)
            FROM gl_journal_lines l
            JOIN gl_journals j ON j.journal_no = l.journal_no
            JOIN gl_accounts a ON a.account_code = l.account_code
            WHERE j.journal_date <= :as_of AND {_company_filter(cid)}
              AND ( a.account_type IN ('Income','Expense')
                    OR a.account_code LIKE '4%' OR a.account_code LIKE '5%' OR a.account_code LIKE '6%' )
        """), {"as_of": as_of, "cid": cid}).scalar() or 0)
    except Exception:
        re_row = 0.0

    retained = round(re_row, 2)
    if abs(retained) > 0.001:
        equity = equity + [{
            "account_code": "3900", "account_name": "Retained Earnings (current)",
            "category": "Equity", "balance": retained,
        }]
    total_equity = round(total_equity_posted + retained, 2)

    difference = round(total_assets - (total_liab + total_equity), 2)

    return render(request, "finance/balance_sheet.html", {
        "as_of": as_of, "assets": assets, "liabilities": liabilities, "equity": equity,
        "totals": {
            "total_assets": total_assets, "total_liabilities": total_liab,
            "total_equity": total_equity, "total_liab_equity": round(total_liab + total_equity, 2),
            "difference": difference, "balanced": abs(difference) < 0.01,
        },
        "page_title": "Balance Sheet",
    })


# ============================================================================
# CASH FLOW  —  simplified direct method from cash/AR/AP/inventory movements
# ============================================================================
@router.get("/statements/cash-flow")
async def cash_flow_statement(
    request: Request,
    from_date: str = None,
    to_date: str = None,
    db: Session = Depends(get_db),
):
    require_action(request, "finance", "view")
    from_date = _parse_date(from_date, -365)
    to_date = _parse_date(to_date, 0)
    cid = _company_id(request)

    # Net movement on the bank account (1110, legacy 1000) over the period is the
    # actual cash change; the rest is shown as operating drivers (AR, AP, inv).
    def _move(codes: list[str]) -> float:
        placeholders = ",".join(f"'{c}'" for c in codes)
        try:
            return float(db.execute(text(f"""
                SELECT ROUND(SUM(COALESCE(l.debit,0) - COALESCE(l.credit,0)),2)
                FROM gl_journal_lines l
                JOIN gl_journals j ON j.journal_no = l.journal_no
                WHERE j.journal_date BETWEEN :f AND :t AND {_company_filter(cid)}
                  AND l.account_code IN ({placeholders})
            """), {"f": from_date, "t": to_date, "cid": cid}).scalar() or 0)
        except Exception:
            return 0.0

    cash_change = _move(["1110", "1000"])
    ar_change = _move(["1120", "1200"])
    ap_change = _move(["2100", "2200"])
    inv_change = _move(["1130", "1300"])

    operating = [
        {"activity": "Net cash (bank) movement", "amount": cash_change},
        {"activity": "Change in Accounts Receivable", "amount": round(-ar_change, 2)},
        {"activity": "Change in Inventory", "amount": round(-inv_change, 2)},
        {"activity": "Change in Payables / accruals", "amount": round(-ap_change, 2)},
    ]
    total_operating = round(cash_change, 2)  # bank movement is the realised cash

    return render(request, "finance/cash_flow.html", {
        "from_date": from_date, "to_date": to_date,
        "operating": operating, "investing": [], "financing": [],
        "totals": {
            "total_operating": total_operating, "total_investing": 0.0,
            "total_financing": 0.0, "net_change": total_operating,
        },
        "page_title": "Cash Flow Statement",
    })


# ============================================================================
# AR / AP AGING  —  buckets 0-30 / 31-60 / 61-90 / 90+
# ============================================================================
@router.get("/reports/aging")
async def aging_report(
    request: Request,
    report_type: str = "ar",
    db: Session = Depends(get_db),
):
    require_action(request, "finance", "view")
    cid = _company_id(request)

    if report_type == "ap":
        sql = """
            SELECT supplier_name AS party, ap_no AS invoice_no, invoice_date,
                   amount, COALESCE(paid_amount,0) AS paid_amount,
                   ROUND(amount - COALESCE(paid_amount,0),2) AS outstanding,
                   DATEDIFF(CURDATE(), invoice_date) AS days_old
            FROM ap_invoices
            WHERE COALESCE(status,'') NOT IN ('Paid','Cancelled')
              AND (company_id = :cid OR company_id IS NULL)
            ORDER BY invoice_date ASC
        """
        party_label = "Supplier"
        page_title = "Accounts Payable Aging"
    else:
        report_type = "ar"
        sql = """
            SELECT customer_name AS party, invoice_no, invoice_date,
                   amount, COALESCE(paid_amount,0) AS paid_amount,
                   ROUND(amount - COALESCE(paid_amount,0),2) AS outstanding,
                   DATEDIFF(CURDATE(), invoice_date) AS days_old
            FROM ar_invoices
            WHERE COALESCE(status,'') NOT IN ('Paid','Cancelled')
              AND (company_id = :cid OR company_id IS NULL)
            ORDER BY invoice_date ASC
        """
        party_label = "Customer"
        page_title = "Accounts Receivable Aging"

    try:
        rows = [dict(r) for r in db.execute(text(sql), {"cid": cid}).mappings().all()]
    except Exception as exc:
        logger.error("Aging query failed: %s", exc)
        rows = []

    def _bucket(days):
        d = int(days or 0)
        if d <= 30:
            return "0-30 days"
        if d <= 60:
            return "31-60 days"
        if d <= 90:
            return "61-90 days"
        return "90+ days"

    buckets = {b: {"count": 0, "total": 0.0} for b in
               ["0-30 days", "31-60 days", "61-90 days", "90+ days"]}
    for r in rows:
        r["bucket"] = _bucket(r.get("days_old"))
        buckets[r["bucket"]]["count"] += 1
        buckets[r["bucket"]]["total"] = round(buckets[r["bucket"]]["total"] + float(r["outstanding"] or 0), 2)

    total_outstanding = round(sum(b["total"] for b in buckets.values()), 2)

    return render(request, "finance/aging_report.html", {
        "report_type": report_type, "rows": rows, "buckets": buckets,
        "total_outstanding": total_outstanding, "party_label": party_label,
        "page_title": page_title,
    })
