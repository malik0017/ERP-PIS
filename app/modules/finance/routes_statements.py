# app/modules/finance/routes_statements.py
# ============================================================================
# BATCH 29 — FINANCIAL STATEMENTS MODULE
# ============================================================================
# P&L, Balance Sheet, Cash Flow, and AR/AP Aging Reports
# All routes use dynamic filters (date range, cost center, customer)
# ============================================================================

from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, Request
from sqlalchemy import text
from sqlalchemy.orm import Session
from app.core.templates import render
from app.core.rbac import require_action
from app.database.session import get_db
import logging

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/finance", tags=["Financial Statements"])


def _company_id(request: Request) -> int:
    """Get company ID from session"""
    return request.session.get("company_id", 1)


def _parse_date(date_str: str, default_offset_days: int = -365) -> str:
    """Parse date string, fallback to offset from today"""
    if not date_str:
        return (datetime.now() + timedelta(days=default_offset_days)).strftime("%Y-%m-%d")
    return date_str


# ============================================================================
# PROFIT & LOSS STATEMENT
# ============================================================================

@router.get("/statements/profit-loss")
async def profit_loss_statement(
    request: Request,
    from_date: str = None,
    to_date: str = None,
    cost_center: str = None,
    db: Session = Depends(get_db),
):
    """
    Profit & Loss Statement
    Revenue - COGS - Expenses = Net Income
    """
    require_action(request, "finance", "view")
    
    from_date = _parse_date(from_date, -365)
    to_date = _parse_date(to_date, 0)
    cid = _company_id(request)
    
    # Build WHERE clause
    where_parts = [
        "jl.debit_date >= :from_date",
        "jl.debit_date <= :to_date",
        "jl.company_id = :cid",
        "ca.account_code NOT IN ('1000','1100','1200','1300','2000','2100','3000','3100')",  # exclude B/S
    ]
    
    if cost_center:
        where_parts.append("j.cost_center = :cc")
    
    where_sql = " AND ".join(where_parts)
    
    sql = f"""
    SELECT 
        ca.account_code,
        ca.account_name,
        CASE 
            WHEN ca.account_code LIKE '4%' THEN 'Revenue'
            WHEN ca.account_code LIKE '5%' THEN 'COGS'
            WHEN ca.account_code LIKE '6%' THEN 'Expenses'
            ELSE 'Other'
        END AS category,
        ROUND(SUM(COALESCE(jl.debit_amount, 0)) - SUM(COALESCE(jl.credit_amount, 0)), 2) AS amount
    FROM gl_journal_lines jl
    JOIN gl_journals j ON j.id = jl.journal_id
    JOIN gl_accounts ca ON ca.code = jl.account_code
    WHERE {where_sql}
    GROUP BY ca.account_code, ca.account_name, category
    ORDER BY category, ca.account_code
    """
    
    params = {"from_date": from_date, "to_date": to_date, "cid": cid}
    if cost_center:
        params["cc"] = cost_center
    
    try:
        rows = [dict(r) for r in db.execute(text(sql), params).mappings().all()]
    except Exception as exc:
        logger.error(f"P&L query failed: {exc}")
        rows = []
    
    # Calculate totals
    revenue = [r for r in rows if r["category"] == "Revenue"]
    cogs = [r for r in rows if r["category"] == "COGS"]
    expenses = [r for r in rows if r["category"] == "Expenses"]
    
    total_revenue = sum(r["amount"] for r in revenue)
    total_cogs = sum(r["amount"] for r in cogs)
    total_expenses = sum(r["amount"] for r in expenses)
    
    gross_profit = total_revenue + total_cogs  # COGS is negative
    net_income = gross_profit + total_expenses
    
    return render(request, "finance/profit_loss.html", {
        "from_date": from_date,
        "to_date": to_date,
        "cost_center": cost_center,
        "revenue": revenue,
        "cogs": cogs,
        "expenses": expenses,
        "totals": {
            "total_revenue": round(total_revenue, 2),
            "total_cogs": round(total_cogs, 2),
            "gross_profit": round(gross_profit, 2),
            "total_expenses": round(total_expenses, 2),
            "net_income": round(net_income, 2),
        },
        "page_title": "Profit & Loss Statement",
    })


# ============================================================================
# BALANCE SHEET
# ============================================================================

@router.get("/statements/balance-sheet")
async def balance_sheet_statement(
    request: Request,
    as_of_date: str = None,
    db: Session = Depends(get_db),
):
    """
    Balance Sheet as of a specific date
    Assets = Liabilities + Equity
    """
    require_action(request, "finance", "view")
    
    as_of = _parse_date(as_of_date, 0)
    cid = _company_id(request)
    
    sql = """
    SELECT 
        ca.account_code,
        ca.account_name,
        CASE 
            WHEN ca.account_code LIKE '1%' THEN 'Assets'
            WHEN ca.account_code LIKE '2%' THEN 'Liabilities'
            WHEN ca.account_code LIKE '3%' THEN 'Equity'
            ELSE 'Other'
        END AS category,
        ROUND(SUM(COALESCE(jl.debit_amount, 0)) - SUM(COALESCE(jl.credit_amount, 0)), 2) AS balance
    FROM gl_journal_lines jl
    JOIN gl_journals j ON j.id = jl.journal_id
    JOIN gl_accounts ca ON ca.code = jl.account_code
    WHERE jl.debit_date <= :as_of AND jl.company_id = :cid
    GROUP BY ca.account_code, ca.account_name, category
    ORDER BY category, ca.account_code
    """
    
    try:
        rows = [dict(r) for r in db.execute(
            text(sql),
            {"as_of": as_of, "cid": cid}
        ).mappings().all()]
    except Exception as exc:
        logger.error(f"Balance Sheet query failed: {exc}")
        rows = []
    
    assets = [r for r in rows if r["category"] == "Assets"]
    liabilities = [r for r in rows if r["category"] == "Liabilities"]
    equity = [r for r in rows if r["category"] == "Equity"]
    
    total_assets = sum(r["balance"] for r in assets)
    total_liab = sum(r["balance"] for r in liabilities)
    total_equity = sum(r["balance"] for r in equity)
    
    difference = round(total_assets - (total_liab + total_equity), 2)
    
    return render(request, "finance/balance_sheet.html", {
        "as_of": as_of,
        "assets": assets,
        "liabilities": liabilities,
        "equity": equity,
        "totals": {
            "total_assets": round(total_assets, 2),
            "total_liabilities": round(total_liab, 2),
            "total_equity": round(total_equity, 2),
            "total_liab_equity": round(total_liab + total_equity, 2),
            "difference": difference,
            "balanced": abs(difference) < 0.01,
        },
        "page_title": "Balance Sheet",
    })


# ============================================================================
# CASH FLOW STATEMENT
# ============================================================================

@router.get("/statements/cash-flow")
async def cash_flow_statement(
    request: Request,
    from_date: str = None,
    to_date: str = None,
    db: Session = Depends(get_db),
):
    """
    Cash Flow Statement
    Operating + Investing + Financing Activities
    """
    require_action(request, "finance", "view")
    
    from_date = _parse_date(from_date, -365)
    to_date = _parse_date(to_date, 0)
    cid = _company_id(request)
    
    # Operating activities (AR, AP, inventory movements)
    operating_sql = """
    SELECT 
        CASE 
            WHEN ca.account_code IN ('1000', '1100') THEN 'Cash received from customers'
            WHEN ca.account_code = '1200' THEN 'Accounts Receivable'
            WHEN ca.account_code = '1300' THEN 'Inventory'
            WHEN ca.account_code IN ('2100', '2200') THEN 'Payments to suppliers'
            ELSE 'Other operating'
        END AS activity,
        ROUND(SUM(COALESCE(jl.debit_amount, 0)) - SUM(COALESCE(jl.credit_amount, 0)), 2) AS amount
    FROM gl_journal_lines jl
    JOIN gl_journals j ON j.id = jl.journal_id
    JOIN gl_accounts ca ON ca.code = jl.account_code
    WHERE jl.debit_date BETWEEN :from_date AND :to_date
      AND jl.company_id = :cid
      AND ca.account_code IN ('1000','1100','1200','1300','2100','2200')
    GROUP BY activity
    """
    
    try:
        operating = [dict(r) for r in db.execute(
            text(operating_sql),
            {"from_date": from_date, "to_date": to_date, "cid": cid}
        ).mappings().all()]
    except Exception:
        operating = []
    
    investing = []  # Can be expanded later
    financing = []  # Can be expanded later
    
    total_operating = sum(r["amount"] for r in operating)
    total_investing = sum(r["amount"] for r in investing)
    total_financing = sum(r["amount"] for r in financing)
    
    net_change = total_operating + total_investing + total_financing
    
    return render(request, "finance/cash_flow.html", {
        "from_date": from_date,
        "to_date": to_date,
        "operating": operating,
        "investing": investing,
        "financing": financing,
        "totals": {
            "total_operating": round(total_operating, 2),
            "total_investing": round(total_investing, 2),
            "total_financing": round(total_financing, 2),
            "net_change": round(net_change, 2),
        },
        "page_title": "Cash Flow Statement",
    })


# ============================================================================
# AR/AP AGING REPORTS
# ============================================================================

@router.get("/reports/aging")
async def aging_report(
    request: Request,
    report_type: str = "ar",  # ar or ap
    from_date: str = None,
    db: Session = Depends(get_db),
):
    """
    Accounts Receivable / Payable Aging Report
    Buckets: 0-30, 31-60, 61-90, 90+
    """
    require_action(request, "finance", "view")
    
    to_date = datetime.now().strftime("%Y-%m-%d")
    cid = _company_id(request)
    
    if report_type == "ar":
        sql = """
        SELECT 
            customer_name,
            invoice_no,
            invoice_date,
            amount,
            COALESCE(paid_amount, 0) AS paid_amount,
            ROUND(amount - COALESCE(paid_amount, 0), 2) AS outstanding,
            DATEDIFF(CURDATE(), invoice_date) AS days_old,
            CASE 
                WHEN DATEDIFF(CURDATE(), invoice_date) <= 30 THEN '0-30 days'
                WHEN DATEDIFF(CURDATE(), invoice_date) <= 60 THEN '31-60 days'
                WHEN DATEDIFF(CURDATE(), invoice_date) <= 90 THEN '61-90 days'
                ELSE '90+ days'
            END AS bucket
        FROM ar_invoices
        WHERE status NOT IN ('Paid', 'Cancelled')
          AND company_id = :cid
        ORDER BY invoice_date ASC
        """
        party_field = "customer_name"
        page_title = "Accounts Receivable Aging"
    else:
        sql = """
        SELECT 
            supplier_name,
            ap_no AS invoice_no,
            invoice_date,
            amount,
            COALESCE(paid_amount, 0) AS paid_amount,
            ROUND(amount - COALESCE(paid_amount, 0), 2) AS outstanding,
            DATEDIFF(CURDATE(), invoice_date) AS days_old,
            CASE 
                WHEN DATEDIFF(CURDATE(), invoice_date) <= 30 THEN '0-30 days'
                WHEN DATEDIFF(CURDATE(), invoice_date) <= 60 THEN '31-60 days'
                WHEN DATEDIFF(CURDATE(), invoice_date) <= 90 THEN '61-90 days'
                ELSE '90+ days'
            END AS bucket
        FROM ap_invoices
        WHERE status NOT IN ('Paid', 'Cancelled')
          AND company_id = :cid
        ORDER BY invoice_date ASC
        """
        party_field = "supplier_name"
        page_title = "Accounts Payable Aging"
    
    try:
        rows = [dict(r) for r in db.execute(
            text(sql),
            {"cid": cid}
        ).mappings().all()]
    except Exception as exc:
        logger.error(f"Aging query failed: {exc}")
        rows = []
    
    # Summarize by bucket
    buckets = {
        "0-30 days": {"count": 0, "total": 0},
        "31-60 days": {"count": 0, "total": 0},
        "61-90 days": {"count": 0, "total": 0},
        "90+ days": {"count": 0, "total": 0},
    }
    
    for row in rows:
        bucket = row["bucket"]
        if bucket in buckets:
            buckets[bucket]["count"] += 1
            buckets[bucket]["total"] += row["outstanding"]
    
    total_outstanding = sum(b["total"] for b in buckets.values())
    
    return render(request, "finance/aging_report.html", {
        "report_type": report_type,
        "rows": rows,
        "buckets": buckets,
        "total_outstanding": round(total_outstanding, 2),
        "party_field": party_field,
        "page_title": page_title,
    })
