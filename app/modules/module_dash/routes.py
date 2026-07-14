# app/modules/module_dash/routes.py — Batch 10
"""
Per-Module Dashboards (SAP-style module cockpits)

Every ERP module now has its own dashboard page, exactly like the Production
Intelligence Command Center, reached from the Module Launcher:

    /module/{key}/dashboard

The dashboards are CONFIG-DRIVEN: one route + one template
(templates/modules/module_dashboard.html) serve all modules. Adding a new
module dashboard = adding one entry to MODULE_DASHBOARDS below. Each entry
defines:
  - rbac area      (gate; admins always pass)
  - KPI tiles      (label + safe SQL, fails to 0 if table missing)
  - quick links    (filtered again by rbac in the template)
  - charts         (label/value SQL rendered as ISFC multi-charts with the
                    H-Bar/Bar/Line/Pie/Donut switcher from isfc-charts.js)

All SQL is read-only COUNT/SUM with COALESCE fallbacks, so a stub module
(e.g. HCM) still renders a professional empty cockpit rather than a 500.
"""

from fastapi import APIRouter, Depends, Request, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.templates import render
from app.core.rbac import require_area, can_access
from app.database import get_db

router = APIRouter(tags=["Module Dashboards"])


# ---------------------------------------------------------------------------
# Safe scalar helper — any failure (missing table/column) returns 0.
# ---------------------------------------------------------------------------
def _n(db: Session, sql: str, params: dict | None = None) -> float:
    try:
        v = db.execute(text(sql), params or {}).scalar()
        return float(v or 0)
    except Exception:
        return 0.0


def _rows(db: Session, sql: str, params: dict | None = None) -> list:
    try:
        return list(db.execute(text(sql), params or {}).mappings().all())
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Dashboard configuration — ADD NEW MODULES HERE (no other code changes).
# ---------------------------------------------------------------------------
MODULE_DASHBOARDS: dict[str, dict] = {
    "inventory": {
        "area": "inventory_valuation",
        "title": "Inventory & Valuation Dashboard",
        "subtitle": "Stock on hand, GRN movements, issue movements, item ledger and valuation.",
        "icon": "box",
        "kpis": [
            ("Inventory Items", "SELECT COUNT(*) FROM ingredients", "materials in inventory master"),
            ("Stock Value", "SELECT COALESCE(SUM(COALESCE(current_stock,0)*COALESCE(unit_cost,0)),0) FROM ingredients", "on-hand valuation"),
            ("GRN Movements", "SELECT COUNT(*) FROM grn_lines", "goods receipt lines"),
            ("Issue Movements", "SELECT COUNT(*) FROM store_issue_lines", "store issue lines"),
        ],
        "links": [
            ("Inventory Valuation", "/inventory", "inventory_valuation", "box"),
            ("Ledger Verification", "/inventory/verification", "inventory_valuation", "check-circle"),
            ("Master Upload", "/masters/upload", "master_upload", "upload-cloud"),
            ("Procurement (GRN)", "/procurement", "procurement", "shopping-bag"),
            ("Reports Center", "/reports", "reports", "bar-chart-2"),
        ],
        "charts": [
            {"title": "Stock Value by Main Category",
             "sql": "SELECT COALESCE(main_category,'Uncategorized') AS label, "
                    "SUM(COALESCE(current_stock,0)*COALESCE(unit_cost,0)) AS value "
                    "FROM ingredients GROUP BY COALESCE(main_category,'Uncategorized') "
                    "ORDER BY value DESC LIMIT 10", "default": "donut"},
            {"title": "Top 10 Items by Stock Value",
             "sql": "SELECT COALESCE(item_name, ingredient_name, inventory_code) AS label, "
                    "COALESCE(current_stock,0)*COALESCE(unit_cost,0) AS value FROM ingredients "
                    "ORDER BY value DESC LIMIT 10", "default": "hbar"},
            {"title": "Ledger Movements by Type",
             "sql": "SELECT COALESCE(txn_type,'Other') AS label, COUNT(*) AS value "
                    "FROM inventory_transactions WHERE 1=1 {range} "
                    "GROUP BY COALESCE(txn_type,'Other') ORDER BY value DESC",
             "range_col": "COALESCE(txn_date, transaction_date, created_at)", "default": "bar"},
            {"title": "Negative / Zero / Positive Stock",
             "sql": "SELECT CASE WHEN COALESCE(current_stock,0) < 0 THEN 'Negative' "
                    "WHEN COALESCE(current_stock,0) = 0 THEN 'Zero' ELSE 'Positive' END AS label, "
                    "COUNT(*) AS value FROM ingredients GROUP BY 1", "default": "pie"},
        ],
    },
    "procurement": {
        "area": "procurement",
        "title": "Procurement Dashboard",
        "subtitle": "Purchase orders, GRN receiving, supplier purchases and inventory entry.",
        "icon": "shopping-bag",
        "kpis": [
            ("Open POs", "SELECT COUNT(*) FROM purchase_orders WHERE COALESCE(status,'') NOT IN ('Closed','Cancelled')", "not closed / cancelled"),
            ("Total POs", "SELECT COUNT(*) FROM purchase_orders", "all purchase orders"),
            ("GRNs", "SELECT COUNT(*) FROM grns", "goods receipts"),
            ("Suppliers", "SELECT COUNT(*) FROM suppliers", "supplier master"),
        ],
        "links": [
            ("Purchase Orders", "/procurement", "procurement", "file-text"),
            ("Inventory", "/inventory", "inventory_valuation", "box"),
            ("Reports Center", "/reports", "reports", "bar-chart-2"),
        ],
        "charts": [
            {"title": "PO Pipeline by Status",
             "sql": "SELECT COALESCE(status,'Unknown') AS label, COUNT(*) AS value "
                    "FROM purchase_orders GROUP BY COALESCE(status,'Unknown') ORDER BY value DESC",
             "default": "donut"},
            {"title": "Top Suppliers by PO Count",
             "sql": "SELECT COALESCE(supplier_name,'Unknown') AS label, COUNT(*) AS value "
                    "FROM purchase_orders WHERE 1=1 {range} GROUP BY COALESCE(supplier_name,'Unknown') "
                    "ORDER BY value DESC LIMIT 10", "range_col": "created_at", "default": "hbar"},
            {"title": "GRN Receipts Trend",
             "sql": "SELECT DATE(created_at) AS label, COUNT(*) AS value FROM grn_receipts "
                    "WHERE 1=1 {range} GROUP BY DATE(created_at) ORDER BY label DESC LIMIT 30",
             "range_col": "created_at", "default": "area"},
        ],
    },
    "finance": {
        "area": "finance",
        "title": "Finance Dashboard",
        "subtitle": "AR invoice drafts, AP supplier invoices, payments and statement foundation.",
        "icon": "dollar-sign",
        "kpis": [
            ("Open AR", "SELECT COUNT(*) FROM ar_invoices WHERE COALESCE(status,'') <> 'Paid'", "unpaid AR invoices"),
            ("AR Value", "SELECT COALESCE(SUM(amount-paid_amount),0) FROM ar_invoices WHERE COALESCE(status,'') <> 'Paid'", "outstanding receivable"),
            ("AP Invoices", "SELECT COUNT(*) FROM ap_invoices", "supplier invoices"),
            ("Payments", "SELECT COUNT(*) FROM finance_payments", "recorded payments"),
        ],
        "links": [
            ("Finance Command Center", "/finance", "finance", "dollar-sign"),
            ("General Ledger", "/finance/gl", "finance", "book"),
            ("Customer Orders", "/orders", "orders", "shopping-cart"),
            ("Reports Center", "/reports", "reports", "bar-chart-2"),
        ],
        "charts": [
            {"title": "AR by Status",
             "sql": "SELECT COALESCE(status,'Unknown') AS label, COUNT(*) AS value "
                    "FROM ar_invoices GROUP BY COALESCE(status,'Unknown') ORDER BY value DESC",
             "default": "donut"},
            {"title": "Open AR by Customer (Top 10)",
             "sql": "SELECT COALESCE(customer_name,'Unknown') AS label, "
                    "SUM(COALESCE(amount,0)-COALESCE(paid_amount,0)) AS value FROM ar_invoices "
                    "WHERE COALESCE(status,'') <> 'Paid' GROUP BY COALESCE(customer_name,'Unknown') "
                    "ORDER BY value DESC LIMIT 10", "default": "hbar"},
            {"title": "Payments Received",
             "sql": "SELECT DATE(payment_date) AS label, SUM(COALESCE(amount,0)) AS value "
                    "FROM finance_payments WHERE 1=1 {range} GROUP BY DATE(payment_date) "
                    "ORDER BY label DESC LIMIT 30", "range_col": "payment_date", "default": "line"},
            {"title": "AR vs AP Open Value",
             "sql": "SELECT 'Receivable (AR)' AS label, COALESCE(SUM(amount-paid_amount),0) AS value "
                    "FROM ar_invoices WHERE COALESCE(status,'') <> 'Paid' "
                    "UNION ALL SELECT 'Payable (AP)', COALESCE(SUM(amount-paid_amount),0) "
                    "FROM ap_invoices WHERE COALESCE(status,'') <> 'Paid'", "default": "bar"},
        ],
    },
    "masters": {
        "area": "masters",
        "title": "Master Data Dashboard",
        "subtitle": "Customers, suppliers, chefs, brands, kitchen sections, revenue streams and items.",
        "icon": "database",
        "kpis": [
            ("Customers", "SELECT COUNT(*) FROM customers", "customer master"),
            ("Suppliers", "SELECT COUNT(*) FROM suppliers", "supplier master"),
            ("Chefs", "SELECT COUNT(*) FROM chefs", "chef master"),
            ("Inventory Items", "SELECT COUNT(*) FROM ingredients", "inventory master"),
        ],
        "links": [
            ("Master Data", "/masters", "master_data", "database"),
            ("Upload Master Data", "/masters/upload", "master_upload", "upload-cloud"),
            ("Recipes & Costing", "/recipes", "recipes", "book-open"),
        ],
        "charts": [
            {"title": "Master Data Coverage",
             "sql": "SELECT 'Customers' AS label, COUNT(*) AS value FROM customers "
                    "UNION ALL SELECT 'Suppliers', COUNT(*) FROM suppliers "
                    "UNION ALL SELECT 'Chefs', COUNT(*) FROM chefs "
                    "UNION ALL SELECT 'Brands', COUNT(*) FROM brands "
                    "UNION ALL SELECT 'Recipes', COUNT(*) FROM recipes", "default": "bar"},
            {"title": "Inventory Items by Category",
             "sql": "SELECT COALESCE(main_category,'Uncategorized') AS label, COUNT(*) AS value "
                    "FROM ingredients GROUP BY 1 ORDER BY value DESC LIMIT 10", "default": "donut"},
        ],
    },
    "projects": {
        "area": "project_management",
        "title": "Project Management Dashboard",
        "subtitle": "Create, plan and manage projects with team assignments, timelines and budgets.",
        "icon": "briefcase",
        "kpis": [
            ("Projects", "SELECT COUNT(*) FROM projects", "all projects"),
            ("Active", "SELECT COUNT(*) FROM projects WHERE COALESCE(status,'') IN ('Active','In Progress')", "in progress"),
            ("Tasks", "SELECT COUNT(*) FROM project_tasks", "all tasks"),
            ("Open Tasks", "SELECT COUNT(*) FROM project_tasks WHERE COALESCE(status,'') NOT IN ('Done','Completed','Closed')", "not completed"),
        ],
        "links": [
            ("Projects", "/projects", "project_management", "briefcase"),
            ("Reports Center", "/reports", "reports", "bar-chart-2"),
        ],
        "charts": [
            {"title": "Projects by Status",
             "sql": "SELECT COALESCE(status,'Unknown') AS label, COUNT(*) AS value "
                    "FROM projects GROUP BY COALESCE(status,'Unknown') ORDER BY value DESC",
             "default": "donut"},
            {"title": "Open Tasks by Project (Top 10)",
             "sql": "SELECT COALESCE(p.name, p.project_name, CONCAT('Project ', t.project_id)) AS label, "
                    "COUNT(*) AS value FROM project_tasks t LEFT JOIN projects p ON p.id = t.project_id "
                    "WHERE COALESCE(t.status,'') NOT IN ('Done','Completed','Closed') "
                    "GROUP BY 1 ORDER BY value DESC LIMIT 10", "default": "hbar"},
        ],
    },
    "reports": {
        "area": "reports",
        "title": "Reports & BI Dashboard",
        "subtitle": "Management reports, printable forms, drill-downs and export packs.",
        "icon": "bar-chart-2",
        "kpis": [
            ("Open Documents", "SELECT COUNT(*) FROM customer_orders WHERE COALESCE(status,'') NOT IN ('Delivered','Closed','Cancelled')", "open order docs"),
            ("BOM Lines", "SELECT COUNT(*) FROM production_bom_lines", "production BOM lines"),
            ("QC Checks", "SELECT COUNT(*) FROM qc_checks", "quality checks"),
            ("Dispatches", "SELECT COUNT(*) FROM dispatches", "delivery documents"),
        ],
        "links": [
            ("Reports Center", "/reports", "reports", "bar-chart-2"),
            ("Relationship Map", "/reports/relationship-map", "relationship", "share-2"),
            ("Relationship Tree", "/reports/relationship-tree", "relationship", "git-branch"),
        ],
        "charts": [
            {"title": "Document Volume",
             "sql": "SELECT 'Orders' AS label, COUNT(*) AS value FROM customer_orders "
                    "UNION ALL SELECT 'BOM Lines', COUNT(*) FROM bom_lines "
                    "UNION ALL SELECT 'QC Checks', COUNT(*) FROM qc_checks "
                    "UNION ALL SELECT 'Dispatches', COUNT(*) FROM packing_dispatch", "default": "bar"},
            {"title": "Orders by Status",
             "sql": "SELECT COALESCE(NULLIF(status,''),'Submitted') AS label, COUNT(*) AS value "
                    "FROM customer_orders GROUP BY 1 ORDER BY value DESC", "default": "donut"},
            {"title": "Order Intake Trend",
             "sql": "SELECT DATE(order_date) AS label, COUNT(*) AS value FROM customer_orders "
                    "WHERE 1=1 {range} GROUP BY DATE(order_date) ORDER BY label DESC LIMIT 30",
             "range_col": "order_date", "default": "area"},
        ],
    },
    "hr": {
        "area": "hr",
        "title": "HCM Dashboard",
        "subtitle": "Human capital management — employees, attendance and payroll (next phase).",
        "icon": "users",
        "kpis": [
            ("Employees", "SELECT COUNT(*) FROM employees", "employee master"),
            ("Chefs", "SELECT COUNT(*) FROM chefs", "kitchen staff"),
            ("Users", "SELECT COUNT(*) FROM users", "system users"),
            ("Active Users", "SELECT COUNT(*) FROM users WHERE is_active = 1", "enabled accounts"),
        ],
        "links": [
            ("Users & Access", "/admin/users", "users", "users"),
        ],
        "charts": [
            {"title": "Users by Role",
             "sql": "SELECT COALESCE(r.name,'No Role') AS label, COUNT(*) AS value "
                    "FROM users u LEFT JOIN roles r ON r.id = u.role_id "
                    "GROUP BY COALESCE(r.name,'No Role') ORDER BY value DESC", "default": "donut"},
            {"title": "Active vs Inactive Users",
             "sql": "SELECT CASE WHEN is_active = 1 THEN 'Active' ELSE 'Inactive' END AS label, "
                    "COUNT(*) AS value FROM users GROUP BY 1", "default": "pie"},
        ],
    },
    "production": {
        "area": "dashboard",
        "title": "Production Intelligence Dashboard",
        "subtitle": "Orders, head chef planning, BOM, store issuance, kitchen, QC, packing and dispatch.",
        "icon": "activity",
        "kpis": [
            ("Open Orders", "SELECT COUNT(*) FROM customer_orders WHERE COALESCE(status,'') NOT IN ('Delivered','Closed','Cancelled')", "in the pipeline"),
            ("BOM Lines", "SELECT COUNT(*) FROM bom_lines", "material demand lines"),
            ("QC Checks", "SELECT COUNT(*) FROM qc_checks", "quality checkpoints"),
            ("Dispatched", "SELECT COUNT(*) FROM packing_dispatch WHERE dispatch_status IN ('Out for Delivery','Delivered','Dispatched','Closed')", "delivery documents"),
        ],
        "links": [
            ("Command Center (Classic)", "/dashboard", "dashboard", "monitor"),
            ("Production Orders", "/production/orders", "production_orders", "clipboard"),
            ("Store Issuance", "/production/store-issuance", "store_issuance", "box"),
            ("Relationship Tree", "/reports/relationship-tree", "relationship", "git-branch"),
        ],
        "charts": [
            {"title": "Order Pipeline by Status",
             "sql": "SELECT COALESCE(NULLIF(status,''),'Submitted') AS label, COUNT(*) AS value "
                    "FROM customer_orders GROUP BY 1 ORDER BY value DESC", "default": "hbar"},
            {"title": "BOM Cost by Issue Section",
             "sql": "SELECT COALESCE(issue_section, section,'Unassigned') AS label, "
                    "SUM(COALESCE(estimated_cost,0)) AS value FROM bom_lines "
                    "GROUP BY 1 ORDER BY value DESC LIMIT 10", "default": "donut"},
            {"title": "Orders Intake Trend",
             "sql": "SELECT DATE(order_date) AS label, COUNT(*) AS value FROM customer_orders "
                    "WHERE 1=1 {range} GROUP BY DATE(order_date) ORDER BY label DESC LIMIT 30",
             "range_col": "order_date", "default": "area"},
            {"title": "QC Result Mix",
             "sql": "SELECT COALESCE(status,'Pending') AS label, COUNT(*) AS value "
                    "FROM qc_checks GROUP BY 1", "default": "pie"},
        ],
    },
}


RANGES = {"7d": 7, "1m": 30, "3m": 90, "1y": 365, "all": None}


@router.get("/module/{key}/dashboard", name="module_dashboard")
async def module_dashboard(request: Request, key: str, db: Session = Depends(get_db)):
    cfg = MODULE_DASHBOARDS.get(key)
    if not cfg:
        raise HTTPException(status_code=404, detail="Unknown module dashboard")

    # RBAC gate — admins bypass inside require_area/can_access.
    require_area(request, cfg["area"])

    # ---- Time slicer (applies to charts that declare range_col) ----
    range_key = (request.query_params.get("range") or "3m").lower()
    if range_key not in RANGES:
        range_key = "3m"
    days = RANGES[range_key]

    kpis = [
        {"label": label, "value": _n(db, sql), "hint": hint}
        for (label, sql, hint) in cfg["kpis"]
    ]
    for k in kpis:
        k["value"] = int(k["value"]) if float(k["value"]).is_integer() else round(k["value"], 2)

    # ---- Charts: new multi-chart list, with legacy single "chart" fallback ----
    chart_cfgs = cfg.get("charts") or ([cfg["chart"]] if cfg.get("chart") else [])
    charts = []
    for cc in chart_cfgs:
        sql = cc["sql"]
        if "{range}" in sql:
            col = cc.get("range_col", "created_at")
            cond = f" AND {col} >= DATE_SUB(CURDATE(), INTERVAL {days} DAY)" if days else ""
            sql = sql.replace("{range}", cond)
        rows = _rows(db, sql)
        labels = [str(r.get("label", "")) for r in rows]
        values = [float(r.get("value") or 0) for r in rows]
        # trend charts come back DESC for LIMIT; flip to chronological
        if cc.get("range_col") and labels:
            labels, values = labels[::-1], values[::-1]
        if labels:
            charts.append({"title": cc["title"], "labels": labels, "values": values,
                           "default": cc.get("default", "hbar"),
                           "has_range": "{range}" in cc["sql"]})

    links = [
        {"title": t, "url": u, "area": a, "icon": i}
        for (t, u, a, i) in cfg["links"]
        if can_access(request, a)
    ]

    has_range = any(c.get("has_range") for c in charts) or any("{range}" in c["sql"] for c in chart_cfgs)

    return render(request, "modules/module_dashboard.html", {
        "page_title": cfg["title"],
        "dash": cfg,
        "kpis": kpis,
        "charts": charts,
        "chart": (charts[0] if charts else None),  # legacy template safety
        "links": links,
        "module_key": key,
        "range_key": range_key,
        "has_range": has_range,
        "ranges": [("7d", "7 Days"), ("1m", "1 Month"), ("3m", "3 Months"), ("1y", "1 Year"), ("all", "Max")],
    })
