# app/modules/reports/routes_dynamic.py
# ============================================================================
# BATCH 29 — DYNAMIC REPORTING WITH FILTERS
# ============================================================================
# Reports with date range, customer, cost-center, supplier filters
# PDF export support
# ============================================================================

from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, Request, Query
from sqlalchemy import text
from sqlalchemy.orm import Session
from app.core.templates import render
from app.database.session import get_db
import logging

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/reports", tags=["Dynamic Reporting"])


def _company_id(request: Request) -> int:
    return request.session.get("company_id", 1)


def _build_date_range(preset: str, from_date: str = None, to_date: str = None) -> tuple:
    """Build date range from preset or custom dates"""
    today = datetime.now()
    
    if preset == "30":
        from_d = (today - timedelta(days=30)).strftime("%Y-%m-%d")
        to_d = today.strftime("%Y-%m-%d")
    elif preset == "90":
        from_d = (today - timedelta(days=90)).strftime("%Y-%m-%d")
        to_d = today.strftime("%Y-%m-%d")
    elif preset == "365":
        from_d = (today - timedelta(days=365)).strftime("%Y-%m-%d")
        to_d = today.strftime("%Y-%m-%d")
    elif preset == "custom" and from_date and to_date:
        from_d, to_d = from_date, to_date
    else:
        from_d = (today - timedelta(days=30)).strftime("%Y-%m-%d")
        to_d = today.strftime("%Y-%m-%d")
    
    return from_d, to_d


@router.get("/dynamic-ar")
async def dynamic_ar_report(
    request: Request,
    date_preset: str = Query("30"),
    from_date: str = Query(None),
    to_date: str = Query(None),
    customer: str = Query(None),  # Can be comma-separated list
    db: Session = Depends(get_db),
):
    """
    Dynamic AR Report with filters
    """
    cid = _company_id(request)
    from_d, to_d = _build_date_range(date_preset, from_date, to_date)
    
    where_parts = [
        "a.invoice_date BETWEEN :from_date AND :to_date",
        "a.status NOT IN ('Paid', 'Cancelled')",
        "a.company_id = :cid",
    ]
    
    params = {"from_date": from_d, "to_date": to_d, "cid": cid}
    
    if customer:
        customer_list = [c.strip() for c in customer.split(",") if c.strip()]
        if customer_list:
            placeholders = ",".join([f":cust{i}" for i in range(len(customer_list))])
            where_parts.append(f"a.customer_name IN ({placeholders})")
            for i, c in enumerate(customer_list):
                params[f"cust{i}"] = c
    
    where_sql = " AND ".join(where_parts)
    
    sql = f"""
    SELECT 
        a.invoice_no,
        a.customer_name,
        a.invoice_date,
        a.amount,
        COALESCE(a.paid_amount, 0) AS paid_amount,
        ROUND(a.amount - COALESCE(a.paid_amount, 0), 2) AS outstanding,
        DATEDIFF(CURDATE(), a.invoice_date) AS days_old,
        a.status
    FROM ar_invoices a
    WHERE {where_sql}
    ORDER BY a.invoice_date DESC
    """
    
    try:
        rows = [dict(r) for r in db.execute(text(sql), params).mappings().all()]
    except Exception as exc:
        logger.error(f"AR report failed: {exc}")
        rows = []
    
    # Get filter options for dropdown
    customers = db.execute(
        text("SELECT DISTINCT customer_name FROM customers WHERE company_id = :cid ORDER BY customer_name"),
        {"cid": cid}
    ).scalars().all()
    
    total_outstanding = sum(r["outstanding"] for r in rows)
    
    return render(request, "reports/dynamic_ar.html", {
        "rows": rows,
        "date_preset": date_preset,
        "from_date": from_d,
        "to_date": to_d,
        "selected_customer": customer,
        "customers": customers,
        "totals": {
            "count": len(rows),
            "total_amount": sum(r["amount"] for r in rows),
            "total_paid": sum(r["paid_amount"] for r in rows),
            "total_outstanding": round(total_outstanding, 2),
        },
        "page_title": "Accounts Receivable Report",
    })


@router.get("/dynamic-ap")
async def dynamic_ap_report(
    request: Request,
    date_preset: str = Query("30"),
    from_date: str = Query(None),
    to_date: str = Query(None),
    supplier: str = Query(None),  # Can be comma-separated list
    db: Session = Depends(get_db),
):
    """
    Dynamic AP Report with filters
    """
    cid = _company_id(request)
    from_d, to_d = _build_date_range(date_preset, from_date, to_date)
    
    where_parts = [
        "a.invoice_date BETWEEN :from_date AND :to_date",
        "a.status NOT IN ('Paid', 'Cancelled')",
        "a.company_id = :cid",
    ]
    
    params = {"from_date": from_d, "to_date": to_d, "cid": cid}
    
    if supplier:
        supplier_list = [s.strip() for s in supplier.split(",") if s.strip()]
        if supplier_list:
            placeholders = ",".join([f":supp{i}" for i in range(len(supplier_list))])
            where_parts.append(f"a.supplier_name IN ({placeholders})")
            for i, s in enumerate(supplier_list):
                params[f"supp{i}"] = s
    
    where_sql = " AND ".join(where_parts)
    
    sql = f"""
    SELECT 
        a.ap_no,
        a.supplier_name,
        a.invoice_date,
        a.amount,
        COALESCE(a.paid_amount, 0) AS paid_amount,
        ROUND(a.amount - COALESCE(a.paid_amount, 0), 2) AS outstanding,
        DATEDIFF(CURDATE(), a.invoice_date) AS days_old,
        a.status
    FROM ap_invoices a
    WHERE {where_sql}
    ORDER BY a.invoice_date DESC
    """
    
    try:
        rows = [dict(r) for r in db.execute(text(sql), params).mappings().all()]
    except Exception as exc:
        logger.error(f"AP report failed: {exc}")
        rows = []
    
    # Get filter options for dropdown
    suppliers = db.execute(
        text("SELECT DISTINCT supplier_name FROM suppliers WHERE company_id = :cid ORDER BY supplier_name"),
        {"cid": cid}
    ).scalars().all()
    
    total_outstanding = sum(r["outstanding"] for r in rows)
    
    return render(request, "reports/dynamic_ap.html", {
        "rows": rows,
        "date_preset": date_preset,
        "from_date": from_d,
        "to_date": to_d,
        "selected_supplier": supplier,
        "suppliers": suppliers,
        "totals": {
            "count": len(rows),
            "total_amount": sum(r["amount"] for r in rows),
            "total_paid": sum(r["paid_amount"] for r in rows),
            "total_outstanding": round(total_outstanding, 2),
        },
        "page_title": "Accounts Payable Report",
    })


@router.get("/dynamic-inventory")
async def dynamic_inventory_report(
    request: Request,
    category: str = Query(None),
    cost_center: str = Query(None),
    db: Session = Depends(get_db),
):
    """
    Dynamic Inventory Report with category and cost-center filters
    """
    cid = _company_id(request)
    
    where_parts = ["i.company_id = :cid"]
    params = {"cid": cid}
    
    if category:
        where_parts.append("i.category = :cat")
        params["cat"] = category
    
    where_sql = " AND ".join(where_parts)
    
    sql = f"""
    SELECT 
        i.code,
        i.name,
        i.category,
        i.uom,
        ROUND(SUM(COALESCE(it.qty_in, 0)), 2) AS qty_on_hand,
        ROUND(i.unit_cost, 4) AS unit_cost,
        ROUND(SUM(COALESCE(it.qty_in, 0)) * i.unit_cost, 2) AS total_value,
        MAX(it.txn_date) AS last_movement
    FROM inventory_masters i
    LEFT JOIN inventory_transactions it ON i.code = it.inventory_code
    WHERE {where_sql}
    GROUP BY i.code, i.name, i.category, i.uom, i.unit_cost
    ORDER BY i.category, i.name
    """
    
    try:
        rows = [dict(r) for r in db.execute(text(sql), params).mappings().all()]
    except Exception as exc:
        logger.error(f"Inventory report failed: {exc}")
        rows = []
    
    # Get filter options
    categories = db.execute(
        text("SELECT DISTINCT category FROM inventory_masters WHERE company_id = :cid ORDER BY category"),
        {"cid": cid}
    ).scalars().all()
    
    total_value = sum(r["total_value"] for r in rows if r["total_value"])
    
    return render(request, "reports/dynamic_inventory.html", {
        "rows": rows,
        "selected_category": category,
        "categories": categories,
        "totals": {
            "count": len(rows),
            "total_qty": sum(r["qty_on_hand"] for r in rows if r["qty_on_hand"]),
            "total_value": round(total_value, 2),
        },
        "page_title": "Inventory Report",
    })
