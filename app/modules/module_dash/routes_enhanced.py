# app/modules/module_dash/routes_enhanced.py
# ============================================================================
# BATCH 29 — ENHANCED LAUNCHER DASHBOARD
# ============================================================================
# Live KPI cards with auto-refresh every 60 seconds
# ============================================================================

from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, Request
from sqlalchemy import text
from sqlalchemy.orm import Session
from app.core.templates import render
from app.database.session import get_db
import logging

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/modules", tags=["Dashboard"])


def _company_id(request: Request) -> int:
    """Get company ID from session"""
    return request.session.get("company_id", 1)


@router.get("")
async def enhanced_launcher(request: Request, db: Session = Depends(get_db)):
    """
    Enhanced Module Launcher Dashboard
    Live KPIs with mini-charts and auto-refresh
    """
    
    cid = _company_id(request)
    
    # ========== KPI 1: OPEN ORDERS ==========
    open_orders_count = db.execute(
        text("""
        SELECT COUNT(*) FROM customer_orders 
        WHERE status NOT IN ('Delivered', 'Cancelled') 
          AND company_id = :cid
        """),
        {"cid": cid}
    ).scalar() or 0
    
    # 7-day trend for open orders
    open_orders_trend = db.execute(
        text("""
        SELECT DATE(order_date) as dt, COUNT(*) as cnt
        FROM customer_orders
        WHERE company_id = :cid
          AND order_date >= DATE_SUB(NOW(), INTERVAL 7 DAY)
        GROUP BY DATE(order_date)
        ORDER BY dt ASC
        """),
        {"cid": cid}
    ).mappings().all()
    
    open_orders_trend_data = [r["cnt"] for r in open_orders_trend]
    
    # ========== KPI 2: INVENTORY VALUE ==========
    inventory_value = db.execute(
        text("""
        SELECT ROUND(SUM(COALESCE(qty_in, 0) * COALESCE(unit_cost, 0)), 2) as val
        FROM inventory_transactions
        WHERE company_id = :cid
        """),
        {"cid": cid}
    ).scalar() or 0
    
    # ========== KPI 3: AR OUTSTANDING ==========
    ar_outstanding = db.execute(
        text("""
        SELECT ROUND(SUM(COALESCE(amount, 0) - COALESCE(paid_amount, 0)), 2) as val
        FROM ar_invoices
        WHERE status NOT IN ('Paid', 'Cancelled')
          AND company_id = :cid
        """),
        {"cid": cid}
    ).scalar() or 0
    
    ar_aging = db.execute(
        text("""
        SELECT 
            CASE 
                WHEN DATEDIFF(CURDATE(), invoice_date) <= 30 THEN '0-30'
                WHEN DATEDIFF(CURDATE(), invoice_date) <= 60 THEN '31-60'
                WHEN DATEDIFF(CURDATE(), invoice_date) <= 90 THEN '61-90'
                ELSE '90+'
            END AS bucket,
            COUNT(*) as cnt
        FROM ar_invoices
        WHERE status NOT IN ('Paid', 'Cancelled')
          AND company_id = :cid
        GROUP BY bucket
        ORDER BY bucket
        """),
        {"cid": cid}
    ).mappings().all()
    
    ar_aging_data = {r["bucket"]: r["cnt"] for r in ar_aging}
    
    # ========== KPI 4: AP OUTSTANDING ==========
    ap_outstanding = db.execute(
        text("""
        SELECT ROUND(SUM(COALESCE(amount, 0) - COALESCE(paid_amount, 0)), 2) as val
        FROM ap_invoices
        WHERE status NOT IN ('Paid', 'Cancelled')
          AND company_id = :cid
        """),
        {"cid": cid}
    ).scalar() or 0
    
    # ========== KPI 5: TOTAL CUSTOMERS ==========
    total_customers = db.execute(
        text("""
        SELECT COUNT(*) FROM customers 
        WHERE company_id = :cid
        """),
        {"cid": cid}
    ).scalar() or 0
    
    # ========== CHART 1: ORDERS BY STATUS (LAST 30 DAYS) ==========
    orders_by_status = db.execute(
        text("""
        SELECT status, COUNT(*) as count
        FROM customer_orders
        WHERE company_id = :cid 
          AND order_date >= DATE_SUB(NOW(), INTERVAL 30 DAY)
        GROUP BY status
        """),
        {"cid": cid}
    ).mappings().all()
    
    # ========== CHART 2: PRODUCTION BY STAGE ==========
    production_by_stage = db.execute(
        text("""
        SELECT stage, COUNT(*) as count
        FROM production_plan_lines
        WHERE company_id = :cid
        GROUP BY stage
        """),
        {"cid": cid}
    ).mappings().all()
    
    # ========== CHART 3: INVENTORY BY CATEGORY ==========
    inventory_by_category = db.execute(
        text("""
        SELECT 
            i.category,
            COUNT(*) as count,
            ROUND(SUM(COALESCE(it.qty_in, 0)), 2) as total_qty
        FROM inventory_masters i
        LEFT JOIN inventory_transactions it ON i.code = it.inventory_code
        WHERE i.company_id = :cid
        GROUP BY i.category
        LIMIT 10
        """),
        {"cid": cid}
    ).mappings().all()
    
    # ========== CHART 4: TOP 5 SUPPLIERS BY PO VALUE ==========
    top_suppliers = db.execute(
        text("""
        SELECT 
            supplier_name,
            COUNT(*) as po_count,
            ROUND(SUM(total_value), 2) as total_value
        FROM purchase_orders
        WHERE company_id = :cid
        GROUP BY supplier_name
        ORDER BY total_value DESC
        LIMIT 5
        """),
        {"cid": cid}
    ).mappings().all()
    
    # ========== CHART 5: TOP 5 CUSTOMERS BY ORDER VALUE ==========
    top_customers = db.execute(
        text("""
        SELECT 
            customer_name,
            COUNT(*) as order_count,
            ROUND(SUM(COALESCE(order_total, 0)), 2) as total_value
        FROM customer_orders
        WHERE company_id = :cid
        GROUP BY customer_name
        ORDER BY total_value DESC
        LIMIT 5
        """),
        {"cid": cid}
    ).mappings().all()
    
    # Build KPI array
    kpis = [
        {
            "label": "Open Orders",
            "value": open_orders_count,
            "icon": "cart-fill",
            "color": "primary",
            "trend_data": open_orders_trend_data,
            "link": "/modules/orders",
        },
        {
            "label": "Inventory Value",
            "value": f"${inventory_value:,.2f}",
            "icon": "box",
            "color": "info",
            "link": "/inventory",
        },
        {
            "label": "AR Outstanding",
            "value": f"${ar_outstanding:,.2f}",
            "icon": "cash-coin",
            "color": "warning",
            "link": "/finance/reports/aging?report_type=ar",
        },
        {
            "label": "AP Outstanding",
            "value": f"${ap_outstanding:,.2f}",
            "icon": "credit-card",
            "color": "danger",
            "link": "/finance/reports/aging?report_type=ap",
        },
        {
            "label": "Total Customers",
            "value": total_customers,
            "icon": "people",
            "color": "success",
            "link": "/masters/customers",
        },
    ]
    
    charts = [
        {
            "id": "orders-by-status",
            "title": "Orders by Status (Last 30 Days)",
            "type": "donut",
            "labels": [r["status"] for r in orders_by_status] if orders_by_status else [],
            "values": [r["count"] for r in orders_by_status] if orders_by_status else [],
        },
        {
            "id": "production-by-stage",
            "title": "Production by Stage",
            "type": "bar",
            "labels": [r["stage"] for r in production_by_stage] if production_by_stage else [],
            "values": [r["count"] for r in production_by_stage] if production_by_stage else [],
        },
        {
            "id": "inventory-by-category",
            "title": "Inventory by Category (Top 10)",
            "type": "bar",
            "labels": [r["category"] for r in inventory_by_category] if inventory_by_category else [],
            "values": [r["total_qty"] for r in inventory_by_category] if inventory_by_category else [],
        },
        {
            "id": "top-suppliers",
            "title": "Top 5 Suppliers by Order Value",
            "type": "bar",
            "labels": [r["supplier_name"] for r in top_suppliers] if top_suppliers else [],
            "values": [r["total_value"] for r in top_suppliers] if top_suppliers else [],
        },
        {
            "id": "top-customers",
            "title": "Top 5 Customers by Order Value",
            "type": "bar",
            "labels": [r["customer_name"] for r in top_customers] if top_customers else [],
            "values": [r["total_value"] for r in top_customers] if top_customers else [],
        },
    ]
    
    return render(request, "modules/launcher_enhanced.html", {
        "kpis": kpis,
        "charts": charts,
        "ar_aging": ar_aging_data,
        "auto_refresh_seconds": 60,
        "page_title": "ISFC ERP Module Launcher",
    })


@router.get("/kpi-refresh")
async def kpi_refresh(request: Request, db: Session = Depends(get_db)):
    """
    AJAX endpoint for KPI refresh (called every 60 seconds by JS)
    Returns JSON of current KPI values
    """
    cid = _company_id(request)
    
    open_orders = db.execute(
        text("SELECT COUNT(*) FROM customer_orders WHERE status NOT IN ('Delivered','Cancelled') AND company_id = :cid"),
        {"cid": cid}
    ).scalar() or 0
    
    inventory_val = db.execute(
        text("SELECT ROUND(SUM(COALESCE(qty_in, 0) * COALESCE(unit_cost, 0)), 2) FROM inventory_transactions WHERE company_id = :cid"),
        {"cid": cid}
    ).scalar() or 0
    
    ar_val = db.execute(
        text("SELECT ROUND(SUM(COALESCE(amount, 0) - COALESCE(paid_amount, 0)), 2) FROM ar_invoices WHERE status NOT IN ('Paid','Cancelled') AND company_id = :cid"),
        {"cid": cid}
    ).scalar() or 0
    
    ap_val = db.execute(
        text("SELECT ROUND(SUM(COALESCE(amount, 0) - COALESCE(paid_amount, 0)), 2) FROM ap_invoices WHERE status NOT IN ('Paid','Cancelled') AND company_id = :cid"),
        {"cid": cid}
    ).scalar() or 0
    
    return {
        "open_orders": open_orders,
        "inventory_value": float(inventory_val or 0),
        "ar_outstanding": float(ar_val or 0),
        "ap_outstanding": float(ap_val or 0),
        "timestamp": datetime.now().isoformat(),
    }
