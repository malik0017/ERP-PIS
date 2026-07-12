# app/modules/search/routes.py
"""ISFC Global ERP Search.

Search is no longer only a quick-link filter. The navbar search calls /search/api
for live results and /search?q=... for the full results page. It searches real
ERP documents using defensive SQL, so a missing future table will never break
navigation.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.templates import render
from app.database.session import get_db

router = APIRouter(prefix="/search", tags=["Search"])


def _rows(db: Session, sql: str, params: dict | None = None) -> list[dict]:
    try:
        return [dict(r) for r in db.execute(text(sql), params or {}).mappings().all()]
    except Exception:
        return []


def _count_table(db: Session, table: str) -> int:
    try:
        return int(db.execute(text("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=DATABASE() AND table_name=:t"), {"t": table}).scalar() or 0)
    except Exception:
        return 0


def _page_results(q: str) -> list[dict]:
    pages = [
        ("Module Launcher", "/modules", "Open ERP module cards"),
        ("Production Dashboard", "/dashboard", "Production command center"),
        ("Customer / Internal Order Portal", "/orders/portal", "Create customer or internal order"),
        ("Production Orders", "/production/orders", "Order register and workflow"),
        ("Head Chef Planning", "/production/head-chef", "Approve cooking and material schedule"),
        ("Store Issuance", "/production/store-issuance", "Issue material to kitchen sections"),
        ("Kitchen Summary", "/production/kitchen-summary", "Section workload and movement"),
        ("Quality Control", "/qc", "QC queue and history"),
        ("Trayline / Packing", "/packing", "Packing readiness"),
        ("Dispatch / Delivery", "/dispatch", "Delivery closure"),
        ("Procurement", "/procurement", "Purchase orders and GRN"),
        ("Inventory Valuation", "/inventory", "Stock ledger and valuation"),
        ("Finance", "/finance", "AR, AP, GL, payments and statements"),
        ("Reports Center", "/reports", "Management and drill-down reports"),
        ("Relationship Map", "/reports/relationship-map", "Document flow map"),
        ("Master Upload", "/masters/upload", "Admin master data upload"),
        ("Chefs", "/chefs", "Chef master"),
        ("Brands", "/brands", "Brand master"),
        ("Suppliers", "/suppliers", "Supplier master"),
        ("Customers", "/customers", "Customer master"),
        ("Inventory Master", "/inventory-master", "Item master"),
        ("Recipes & Costing", "/recipes", "Recipe master and costing"),
        ("Prepare Recipe", "/recipes/prepare", "Manual recipe entry"),
        ("Users & Access", "/admin/users", "User and permission administration"),
        ("Audit Logs", "/admin/audit-logs", "Security and activity history"),
        ("Settings", "/settings", "Company and system settings"),
    ]
    ql = q.lower()
    return [{"type":"Page", "title":p[0], "subtitle":p[2], "url":p[1], "meta":"Screen"} for p in pages if ql in p[0].lower() or ql in p[2].lower()]


def build_results(db: Session, q: str, limit: int = 8) -> list[dict]:
    q = (q or "").strip()
    if not q:
        return []
    like = f"%{q}%"
    results: list[dict] = []

    # Pages / reports / dashboards
    results.extend(_page_results(q)[:limit])

    # Orders: customer_orders and production_orders both appear in different builds.
    for r in _rows(db, """
        SELECT order_no, customer_name, COALESCE(brand,'') AS brand, COALESCE(status,'') AS status,
               COALESCE(required_delivery_date, delivery_date, '') AS delivery_date
        FROM customer_orders
        WHERE order_no LIKE :like OR customer_name LIKE :like OR COALESCE(brand,'') LIKE :like OR COALESCE(status,'') LIKE :like
        ORDER BY id DESC LIMIT :lim
    """, {"like": like, "lim": limit}):
        results.append({"type":"Order", "title":r["order_no"], "subtitle":f"{r.get('customer_name','')} · {r.get('brand','')} · {r.get('status','')}", "url":f"/production/orders/{r['order_no']}", "meta":str(r.get("delivery_date") or "")})

    for r in _rows(db, """
        SELECT order_no, customer_name, COALESCE(brand,'') AS brand, COALESCE(status,'') AS status
        FROM production_orders
        WHERE order_no LIKE :like OR customer_name LIKE :like OR COALESCE(brand,'') LIKE :like OR COALESCE(status,'') LIKE :like
        ORDER BY id DESC LIMIT :lim
    """, {"like": like, "lim": limit}):
        results.append({"type":"Production", "title":r["order_no"], "subtitle":f"{r.get('customer_name','')} · {r.get('brand','')} · {r.get('status','')}", "url":f"/production/orders/{r['order_no']}", "meta":"Order"})

    # Recipes
    for r in _rows(db, """
        SELECT id, recipe_code, recipe_name, COALESCE(category,'') AS category, COALESCE(customer_name,'') AS customer_name, COALESCE(status,'') AS status
        FROM recipes
        WHERE recipe_code LIKE :like OR recipe_name LIKE :like OR COALESCE(category,'') LIKE :like OR COALESCE(customer_name,'') LIKE :like
        ORDER BY id DESC LIMIT :lim
    """, {"like": like, "lim": limit}):
        url = f"/recipes/{r['id']}" if r.get("id") else "/recipes"
        results.append({"type":"Recipe", "title":f"{r.get('recipe_code','')} - {r.get('recipe_name','')}", "subtitle":f"{r.get('category','')} · {r.get('customer_name','')}", "url":url, "meta":r.get("status") or "Recipe"})

    # Masters
    for table, code_col, name_col, url_prefix, label in [
        ("customers", "customer_code", "customer_name", "/masters/customers", "Customer"),
        ("suppliers", "supplier_code", "supplier_name", "/masters/suppliers", "Supplier"),
        ("chefs", "chef_code", "chef_name", "/masters/chefs", "Chef"),
        ("brands", "brand_code", "brand_name", "/masters/brands", "Brand"),
    ]:
        if _count_table(db, table):
            for r in _rows(db, f"""
                SELECT id, {code_col} AS code, {name_col} AS name, COALESCE(status,'') AS status
                FROM {table}
                WHERE {code_col} LIKE :like OR {name_col} LIKE :like OR COALESCE(status,'') LIKE :like
                ORDER BY id DESC LIMIT :lim
            """, {"like": like, "lim": limit}):
                results.append({"type":label, "title":f"{r.get('code','')} - {r.get('name','')}", "subtitle":label, "url":f"{url_prefix}/{r.get('id')}", "meta":r.get("status") or "Master"})

    # Inventory item master (ingredients is current stable table)
    for r in _rows(db, """
        SELECT ingredient_code AS code, name AS name, COALESCE(main_category, category, '') AS category,
               COALESCE(standard_uom, purchase_uom, recipe_uom, '') AS uom
        FROM ingredients
        WHERE ingredient_code LIKE :like OR name LIKE :like OR COALESCE(category,'') LIKE :like OR COALESCE(main_category,'') LIKE :like
        ORDER BY ingredient_code LIMIT :lim
    """, {"like": like, "lim": limit}):
        results.append({"type":"Inventory", "title":f"{r.get('code','')} - {r.get('name','')}", "subtitle":r.get("category") or "Inventory item", "url":f"/inventory/ledger/{r.get('code')}", "meta":r.get("uom") or ""})

    # Procurement documents
    for r in _rows(db, """
        SELECT po_no, supplier_name, COALESCE(status,'') AS status, COALESCE(total_value,0) AS total_value
        FROM purchase_orders
        WHERE po_no LIKE :like OR supplier_name LIKE :like OR COALESCE(status,'') LIKE :like
        ORDER BY id DESC LIMIT :lim
    """, {"like": like, "lim": limit}):
        results.append({"type":"PO", "title":r["po_no"], "subtitle":r.get("supplier_name") or "Supplier", "url":f"/procurement/po/{r['po_no']}", "meta":r.get("status") or "PO"})
    for r in _rows(db, """
        SELECT grn_no, po_no, supplier_name, COALESCE(status,'') AS status
        FROM grn_receipts
        WHERE grn_no LIKE :like OR po_no LIKE :like OR supplier_name LIKE :like
        ORDER BY id DESC LIMIT :lim
    """, {"like": like, "lim": limit}):
        results.append({"type":"GRN", "title":r["grn_no"], "subtitle":f"PO {r.get('po_no','')} · {r.get('supplier_name','')}", "url":f"/procurement/po/{r.get('po_no')}", "meta":r.get("status") or "GRN"})

    # Finance documents
    for r in _rows(db, """
        SELECT invoice_no, order_no, customer_name, COALESCE(status,'') AS status, COALESCE(amount,0) AS amount
        FROM ar_invoices
        WHERE invoice_no LIKE :like OR order_no LIKE :like OR customer_name LIKE :like OR COALESCE(status,'') LIKE :like
        ORDER BY id DESC LIMIT :lim
    """, {"like": like, "lim": limit}):
        results.append({"type":"AR", "title":r["invoice_no"], "subtitle":f"{r.get('customer_name','')} · {r.get('order_no','')}", "url":"/finance#ar", "meta":r.get("status") or "AR"})
    for r in _rows(db, """
        SELECT ap_no, supplier_name, po_no, grn_no, COALESCE(status,'') AS status
        FROM ap_invoices
        WHERE ap_no LIKE :like OR supplier_name LIKE :like OR po_no LIKE :like OR grn_no LIKE :like OR COALESCE(status,'') LIKE :like
        ORDER BY id DESC LIMIT :lim
    """, {"like": like, "lim": limit}):
        results.append({"type":"AP", "title":r["ap_no"], "subtitle":f"{r.get('supplier_name','')} · PO {r.get('po_no','')}", "url":"/finance#ap", "meta":r.get("status") or "AP"})
    for r in _rows(db, """
        SELECT payment_no, party_type, party_name, reference_no, COALESCE(amount,0) AS amount
        FROM finance_payments
        WHERE payment_no LIKE :like OR party_name LIKE :like OR reference_no LIKE :like OR party_type LIKE :like
        ORDER BY id DESC LIMIT :lim
    """, {"like": like, "lim": limit}):
        results.append({"type":"Payment", "title":r["payment_no"], "subtitle":f"{r.get('party_type','')} · {r.get('party_name','')} · {r.get('reference_no','')}", "url":"/finance#payments", "meta":str(r.get("amount") or "")})

    # De-duplicate by type/title/url
    seen=set(); clean=[]
    for x in results:
        key=(x.get("type"), x.get("title"), x.get("url"))
        if key not in seen:
            seen.add(key); clean.append(x)
    return clean[:50]


@router.get("/api")
def global_search_api(q: str = "", db: Session = Depends(get_db)):
    results = build_results(db, q, limit=6)
    return JSONResponse({"q": q, "total": len(results), "results": results[:20]})


@router.get("")
async def global_search(request: Request, db: Session = Depends(get_db)):
    q = (request.query_params.get("q") or "").strip()
    results = build_results(db, q, limit=12)
    groups = []
    by_type: dict[str, list[dict]] = {}
    for r in results:
        by_type.setdefault(r["type"], []).append(r)
    icon = {"Order":"uil-shopping-cart-alt", "Production":"uil-clipboard-notes", "Recipe":"uil-book-open", "Inventory":"uil-box", "Supplier":"uil-truck", "Customer":"uil-users-alt", "Page":"uil-window-grid", "PO":"uil-file-contract", "GRN":"uil-archive", "AR":"uil-receipt", "AP":"uil-invoice", "Payment":"uil-money-withdraw"}
    for k, rows in by_type.items():
        groups.append({"key": k.lower(), "title": k, "icon": icon.get(k, "uil-search"), "rows": rows})
    return render(request, "search/results.html", {
        "q": q,
        "groups": groups,
        "total": len(results),
        "page_title": f"Search: {q}" if q else "Search",
    })
