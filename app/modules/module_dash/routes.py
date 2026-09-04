# app/modules/module_dash/routes.py — Batch 10

from fastapi import APIRouter, Depends, Request, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.templates import render
from app.core.rbac import require_area, can_access
from app.database import get_db

router = APIRouter(tags=["Module Dashboards"])

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


MODULE_DASHBOARDS: dict[str, dict] = {
    "inventory": {
        "area": "inventory_valuation",
        "title": "Inventory & Valuation Dashboard",
        "subtitle": "Stock on hand, GRN movements, issue movements, item ledger and valuation.",
        "icon": "box",
        "kpis": [
            ("Inventory Items", "SELECT COUNT(*) FROM ingredients", "materials in inventory master"),
            ("Stock Value", """
                SELECT COALESCE(SUM(bal * avg_cost), 0) FROM (
                  SELECT inventory_code,
                         SUM(COALESCE(qty_in,0)) - SUM(COALESCE(qty_out,0)) AS bal,
                         CASE WHEN SUM(COALESCE(qty_in,0)) > 0
                              THEN SUM(COALESCE(qty_in,0)*COALESCE(unit_cost,0)) / SUM(COALESCE(qty_in,0))
                              ELSE 0 END AS avg_cost
                  FROM inventory_transactions GROUP BY inventory_code
                ) x
             """, "SAR · on-hand valuation"),
            ("GRN Movements", "SELECT COUNT(*) FROM grn_lines WHERE 1=1 {range}", "goods receipt lines"),
            ("Issue Movements", "SELECT COUNT(*) FROM store_issuance_lines WHERE 1=1 {range}", "store issue lines"),
        ],
        "links": [
            ("Inventory Valuation", "/inventory", "inventory_valuation", "box"),
            ("Ledger Verification", "/inventory/verification", "inventory_valuation", "check-circle"),
            # ("Master Upload", "/masters/upload", "master_upload", "upload-cloud"),
            # ("Procurement (GRN)", "/procurement", "procurement", "shopping-bag"),
            # ("Reports Center", "/reports", "reports", "bar-chart-2"),
        ],
        "charts": [
            {"title": "Stock Value by Main Category",
             "sql": """
                SELECT COALESCE(i.main_category,'Uncategorized') AS label, SUM(x.bal * x.avg_cost) AS value
                FROM (
                  SELECT inventory_code,
                         SUM(COALESCE(qty_in,0)) - SUM(COALESCE(qty_out,0)) AS bal,
                         CASE WHEN SUM(COALESCE(qty_in,0)) > 0
                              THEN SUM(COALESCE(qty_in,0)*COALESCE(unit_cost,0)) / SUM(COALESCE(qty_in,0))
                              ELSE 0 END AS avg_cost
                  FROM inventory_transactions GROUP BY inventory_code
                ) x
                LEFT JOIN ingredients i ON i.ingredient_code = x.inventory_code
                GROUP BY COALESCE(i.main_category,'Uncategorized') ORDER BY value DESC LIMIT 10
             """, "default": "donut"},
            {"title": "Top 10 Items by Stock Value",
             "sql": """
                SELECT COALESCE(i.name, x.inventory_code) AS label, (x.bal * x.avg_cost) AS value
                FROM (
                  SELECT inventory_code,
                         SUM(COALESCE(qty_in,0)) - SUM(COALESCE(qty_out,0)) AS bal,
                         CASE WHEN SUM(COALESCE(qty_in,0)) > 0
                              THEN SUM(COALESCE(qty_in,0)*COALESCE(unit_cost,0)) / SUM(COALESCE(qty_in,0))
                              ELSE 0 END AS avg_cost
                  FROM inventory_transactions GROUP BY inventory_code
                ) x
                LEFT JOIN ingredients i ON i.ingredient_code = x.inventory_code
                ORDER BY value DESC LIMIT 10
             """, "default": "hbar"},
            {"title": "Ledger Movements by Type",
             "sql": "SELECT COALESCE(movement_type,'Other') AS label, COUNT(*) AS value "
                    "FROM inventory_transactions WHERE 1=1 {range} "
                    "GROUP BY COALESCE(movement_type,'Other') ORDER BY value DESC",
             "range_col": "COALESCE(txn_date, transaction_date, created_at)", "default": "bar"},
            {"title": "Negative / Zero / Positive Stock",
             "sql": """
                SELECT CASE WHEN bal < 0 THEN 'Negative' WHEN bal = 0 THEN 'Zero' ELSE 'Positive' END AS label,
                       COUNT(*) AS value
                FROM (
                  SELECT inventory_code, SUM(COALESCE(qty_in,0)) - SUM(COALESCE(qty_out,0)) AS bal
                  FROM inventory_transactions GROUP BY inventory_code
                ) x GROUP BY 1
             """, "default": "pie"},
        ],
    },
    "procurement": {
        "area": "procurement",
        "title": "Procurement Dashboard",
        "subtitle": "",
        "icon": "shopping-bag",
        "kpis": [
            ("Open POs", "SELECT COUNT(*) FROM purchase_orders WHERE COALESCE(status,'') NOT IN ('Closed','Cancelled')", ""),
            ("Total POs", "SELECT COUNT(*) FROM purchase_orders WHERE 1=1 {range}", ""),
            # Batch 87 fix: "grns" isn't a real table (the real one is
            # grn_receipts) — this silently returned 0 the same way the
            # Inventory cockpit's broken queries did.
            ("GRNs", "SELECT COUNT(*) FROM grn_receipts WHERE 1=1 {range}", ""),
            ("Suppliers", "SELECT COUNT(*) FROM suppliers", ""),
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
            ("New A/R Invoice", "/finance/ar/new", "finance", "file-plus"),
            ("General Ledger", "/finance/gl", "finance", "book"),
            ("New Journal Entry", "/finance/journal/new", "finance", "edit-3"),
            ("Chart of Accounts", "/finance/coa", "finance", "list"),
            ("AR Aging", "/finance/reports/aging?report_type=ar", "finance", "trending-up"),
            ("AP Aging", "/finance/reports/aging?report_type=ap", "finance", "trending-down"),
            ("Profit & Loss", "/finance/statements/profit-loss", "finance", "bar-chart-2"),
            ("Balance Sheet", "/finance/statements/balance-sheet", "finance", "layers"),
            ("Cash Flow", "/finance/statements/cash-flow", "finance", "activity"),
            ("Period Close", "/finance/periods", "finance", "calendar"),
            ("Cost Centers", "/finance/cost-centers", "finance", "grid"),
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
        "subtitle": "",
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
        "subtitle": "Employees, attendance, leave, shifts and payroll.",
        "icon": "users",
        "kpis": [
            ("Employees", "SELECT COUNT(*) FROM hr_employees", "in the employee master"),
            ("Active Employees", "SELECT COUNT(*) FROM hr_employees WHERE status='Active'", "currently employed"),
            ("Present Today", "SELECT COUNT(*) FROM hr_attendance WHERE att_date=CURDATE() AND status='Present'", "marked present"),
            ("Pending Leave", "SELECT COUNT(*) FROM hr_leave_requests WHERE status='Pending'", "awaiting decision"),
        ],
        "links": [
            ("Employees", "/hr/employees", "hr", "users"),
            ("Attendance", "/hr/attendance", "hr", "calendar"),
            ("Leave", "/hr/leave", "hr", "calendar"),
            ("Shifts", "/hr/shifts", "hr", "clock"),
            ("Payroll", "/hr/payroll", "hr", "credit-card"),
            ("Users & Access", "/admin/users", "users", "shield"),
        ],
        "charts": [
            {"title": "Employees by Section",
             "sql": "SELECT COALESCE(section,'Unassigned') AS label, COUNT(*) AS value "
                    "FROM hr_employees GROUP BY 1 ORDER BY value DESC", "default": "donut"},
            {"title": "Attendance Mix (period)",
             "sql": "SELECT COALESCE(status,'') AS label, COUNT(*) AS value FROM hr_attendance "
                    "WHERE 1=1 {range} GROUP BY 1", "range_col": "att_date", "default": "pie"},
            {"title": "Users by Role",
             "sql": "SELECT COALESCE(r.name,'No Role') AS label, COUNT(*) AS value "
                    "FROM users u LEFT JOIN roles r ON r.id = u.role_id "
                    "GROUP BY COALESCE(r.name,'No Role') ORDER BY value DESC", "default": "bar"},
        ],
    },
   
    "sales": {
        "area": "sales_review",
        "title": "Sales Cockpit",
        "subtitle": "Order intake, customer mix, request approvals and order value.",
        "icon": "shopping-cart",
        "kpi_range_col": "order_date",
        "kpis": [
            ("Orders Raised", "SELECT COUNT(*) FROM customer_orders WHERE 1=1 {range}",
             "sales requests in period"),
            ("Portions Ordered",
             "SELECT COALESCE(SUM(ol.required_portions),0) FROM order_lines ol "
             "JOIN customer_orders co ON co.order_no = ol.order_no WHERE 1=1 {range}",
             "total portions"),
            ("Awaiting Review",
             "SELECT COUNT(*) FROM customer_orders "
             "WHERE COALESCE(sales_review_status,'Pending') = 'Pending' {range}",
             "requests pending approval"),
            ("Active Customers",
             "SELECT COUNT(*) FROM customers WHERE UPPER(TRIM(COALESCE(status,''))) = 'ACTIVE'",
             "on the customer master"),
        ],
        "links": [
            ("Sale Requisitions", "/orders/portal", "order_portal", "shopping-cart"),
            ("Sales Requests", "/sales-requests", "sales_review", "check-square"),
        ],
        "charts": [
            {"title": "Orders by Customer",
             "sql": "SELECT COALESCE(NULLIF(customer_name,''),'Unassigned') AS label, "
                    "COUNT(*) AS value FROM customer_orders WHERE 1=1 {range} "
                    "GROUP BY 1 ORDER BY value DESC LIMIT 10",
             "range_col": "order_date", "default": "hbar"},
            {"title": "Order Intake Trend",
             "sql": "SELECT DATE(order_date) AS label, COUNT(*) AS value FROM customer_orders "
                    "WHERE 1=1 {range} GROUP BY DATE(order_date) ORDER BY label DESC LIMIT 30",
             "range_col": "order_date", "default": "area"},
            {"title": "Request Review Status",
             "sql": "SELECT COALESCE(NULLIF(sales_review_status,''),'Pending') AS label, "
                    "COUNT(*) AS value FROM customer_orders WHERE 1=1 {range} GROUP BY 1",
             "range_col": "order_date", "default": "donut"},
            {"title": "Portions by Brand",
             "sql": "SELECT COALESCE(NULLIF(co.brand,''),'Unassigned') AS label, "
                    "COALESCE(SUM(ol.required_portions),0) AS value "
                    "FROM order_lines ol JOIN customer_orders co ON co.order_no = ol.order_no "
                    "WHERE 1=1 {range} GROUP BY 1 ORDER BY value DESC LIMIT 10",
             "range_col": "co.order_date", "default": "bar"},
        ],
    },
    "production": {
        "area": "dashboard",
        "title": "Head Chef Dashboard",
        "subtitle": "",
        "icon": "activity",
        "kpis": [
            ("Open Orders", "SELECT COUNT(*) FROM customer_orders WHERE COALESCE(status,'') NOT IN ('Delivered','Closed','Cancelled') {range}", "in the pipeline"),
            ("BOM Lines", "SELECT COUNT(*) FROM bom_lines WHERE 1=1 {range}", "material demand lines"),
            ("QC Checks", "SELECT COUNT(*) FROM qc_checks WHERE 1=1 {range}", "quality checkpoints"),
            ("Dispatched", "SELECT COUNT(*) FROM packing_dispatch WHERE dispatch_status IN ('Out for Delivery','Delivered','Dispatched','Closed') {range}", "delivery documents"),
        ],
        "links": [
            # ("Command Center (Classic)", "/dashboard", "dashboard", "monitor"),
            ("Production Orders", "/production/orders", "production_orders", "clipboard"),
            ("Store Issuance", "/production/store-issuance", "store_issuance", "box"),
            # ("Relationship Tree", "/reports/relationship-tree", "relationship", "git-branch"),
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

    import re as _re
    def _valid_date(s):
        s = (s or "").strip()
        return s if _re.fullmatch(r"\d{4}-\d{2}-\d{2}", s) else ""
    date_from = _valid_date(request.query_params.get("date_from"))
    date_to = _valid_date(request.query_params.get("date_to"))
    use_abs = bool(date_from or date_to)

    def _range_cond(col):
        """Return the SQL AND-condition for the active window on `col`."""
        if use_abs:
            parts = []
            if date_from:
                parts.append(f" AND {col} >= '{date_from} 00:00:00'")
            if date_to:
                parts.append(f" AND {col} <= '{date_to} 23:59:59'")
            return "".join(parts)
        return f" AND {col} >= DATE_SUB(CURDATE(), INTERVAL {days} DAY)" if days else ""

   
    def _n_probe(sql_text: str):
        try:
            v = db.execute(text(sql_text)).scalar()
            return (float(v or 0), True)
        except Exception:
            return (0.0, False)

    kpis = []
    for (label, sql, hint) in cfg["kpis"]:
        ranged = "{range}" in sql
        col = cfg.get("kpi_range_col", "created_at")
        if ranged:
            value, ok = _n_probe(sql.replace("{range}", _range_cond(col)))
            if not ok:                      # date column missing / bad SQL
                value, ok = _n_probe(sql.replace("{range}", ""))
                ranged = False              # be honest: this one is all-time
        else:
            value, ok = _n_probe(sql)
        kpis.append({"label": label, "value": value, "hint": hint,
                     "ranged": ranged, "error": not ok})
    for k in kpis:
        k["value"] = int(k["value"]) if float(k["value"]).is_integer() else round(k["value"], 2)

    # ---- Charts: new multi-chart list, with legacy single "chart" fallback ----
    chart_cfgs = cfg.get("charts") or ([cfg["chart"]] if cfg.get("chart") else [])
    charts = []
    for cc in chart_cfgs:
        sql = cc["sql"]
        if "{range}" in sql:
            col = cc.get("range_col", "created_at")
            sql = sql.replace("{range}", _range_cond(col))
        rows = _rows(db, sql)
        labels = [str(r.get("label", "")) for r in rows]
        values = [float(r.get("value") or 0) for r in rows]
        # trend charts come back DESC for LIMIT; flip to chronological
        if cc.get("range_col") and labels:
            labels, values = labels[::-1], values[::-1]
        if labels:
            charts.append({"title": cc["title"], "labels": labels, "values": values,
                           "default": cc.get("default", "hbar"),
                           "has_range": "{range}" in cc["sql"],
                           # Batch 111: chart-level unit/note feed the richer
                           # tooltips added in Batch 107.
                           "unit": cc.get("unit", ""),
                           "note": cc.get("note", "")})

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
        # Batch 111: human label for the badge on each KPI card.
        "range_label": {"7d": "7 days", "1m": "1 month", "3m": "3 months",
                        "1y": "1 year", "all": "all time"}.get(range_key, range_key),
        "has_range": has_range,
        "ranges": [("7d", "7 Days"), ("1m", "1 Month"), ("3m", "3 Months"), ("1y", "1 Year"), ("all", "Max")],
        # Batch 121: absolute date filter state
        "date_from": date_from,
        "date_to": date_to,
        "use_abs": use_abs,
    })
