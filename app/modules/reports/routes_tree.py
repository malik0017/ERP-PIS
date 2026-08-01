# app/modules/reports/routes_tree.py
# =============================================================================
# Batch 68 — SAP B1-style Relationship Tree (document-card data)
# -----------------------------------------------------------------------------
# The graph design is kept. What changes is the DATA each node carries and how
# expanded children render: instead of "label + status word", every document
# node now returns SAP-style meta —
#     doc_type   e.g. "Sales Order", "Purchase Order", "GRN", "AR Invoice"
#     doc_no     the document number
#     doc_date   the document date (dd.mm.yyyy, SAP style)
#     doc_value  the monetary value (formatted)
#     status     workflow status
# so the side "Document Explorer" can draw the exact stacked cards you see in
# SAP Business One (type header bar, number, date, value).
#
# This is a NEW endpoint (/reports/api/tree-node) so the old one keeps working;
# the tree page is repointed to it. It also FIXES a real bug in the old API:
# it queried `FROM grns` but the table is `grn_receipts`, so PO→GRN drill-down
# always came back empty.
#
# Registered in main.py:
#     from app.modules.reports.routes_tree import router as reports_tree_router
#     app.include_router(reports_tree_router)
# =============================================================================

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.templates import render
from app.core.rbac import require_area
from app.database.session import get_db

router = APIRouter(tags=["Reports"])


# ---------------------------------------------------------------------------
def _rows(db, sql, params=None):
    try:
        return [dict(r) for r in db.execute(text(sql), params or {}).mappings().all()]
    except Exception:
        return []


def _count(db, sql, params=None):
    try:
        return int(db.execute(text(sql), params or {}).scalar() or 0)
    except Exception:
        return 0


def _fmt_date(v):
    """SAP shows dd.mm.yyyy."""
    if not v:
        return ""
    s = str(v)[:10]
    if "-" in s and len(s) == 10:
        y, m, d = s.split("-")
        return f"{d}.{m}.{y}"
    return s


def _fmt_money(v):
    try:
        return f"{float(v or 0):,.2f}"
    except Exception:
        return "0.00"


def _doc(node_type, key, doc_type, doc_no, doc_date="", doc_value=None,
         status="", url=None, leaf=False, badge=None):
    """Build a document-card node payload."""
    d = {
        "type": node_type, "key": str(key or ""), "leaf": leaf,
        "doc_type": doc_type, "doc_no": str(doc_no or ""),
        "doc_date": _fmt_date(doc_date),
        "status": status or "",
        "label": f"{doc_type} · {doc_no}" if doc_no else doc_type,
        "badge": badge if badge is not None else (status or ""),
        "card": True,
    }
    if doc_value is not None:
        d["doc_value"] = _fmt_money(doc_value)
    if url:
        d["url"] = url
    return d


def _line(label, badge="", url=None, leaf=True, node_type="leaf", key=""):
    """A simple (non-card) line node — used for line items and folders."""
    d = {"type": node_type, "key": str(key or ""), "leaf": leaf,
         "label": label, "badge": str(badge), "card": False}
    if url:
        d["url"] = url
    return d


# ---------------------------------------------------------------------------
@router.get("/reports/relationship-tree")
def relationship_tree(request: Request, db: Session = Depends(get_db)):
    require_area(request, "reports")
    roots = [
        {"type": "root_sales", "label": "Sales & Orders", "icon": "shopping-cart",
         "badge": _count(db, "SELECT COUNT(*) FROM customer_orders")},
        {"type": "root_production", "label": "Production", "icon": "activity",
         "badge": _count(db, "SELECT COUNT(DISTINCT order_no) FROM bom_lines")},
        {"type": "root_inventory", "label": "Inventory", "icon": "box",
         "badge": _count(db, "SELECT COUNT(*) FROM ingredients")},
        {"type": "root_procurement", "label": "Procurement", "icon": "shopping-bag",
         "badge": _count(db, "SELECT COUNT(*) FROM purchase_orders")},
        {"type": "root_finance", "label": "Finance (AR)", "icon": "dollar-sign",
         "badge": _count(db, "SELECT COUNT(*) FROM ar_invoices")},
        {"type": "root_masters", "label": "Master Data", "icon": "database",
         "badge": _count(db, "SELECT COUNT(*) FROM customers")},
    ]
    return render(request, "reports/relationship_tree.html", {
        "roots": roots, "page_title": "Relationship Tree",
    })


@router.get("/reports/api/tree-node")
def tree_node(request: Request,
              node_type: str = Query(alias="type", default=""),
              key: str = Query(default=""),
              db: Session = Depends(get_db)):
    t_, k = node_type, key
    out: list[dict] = []

    # ============================ ROOTS ============================
    if t_ == "root_sales":
        for r in _rows(db, """
            SELECT order_no, customer_name, COALESCE(status,'') AS status,
                   order_date, required_delivery_date,
                   COALESCE(total_estimated_selling_value,0) AS val
            FROM customer_orders ORDER BY id DESC LIMIT 40"""):
            out.append(_doc("order", r["order_no"], "Sales Order", r["order_no"],
                            r["order_date"], r["val"], r["status"],
                            url=f"/production/orders/{r['order_no']}"))
    elif t_ == "root_production":
        for r in _rows(db, """
            SELECT b.order_no, MAX(o.customer_name) AS customer,
                   COUNT(*) AS lines, MAX(o.order_date) AS order_date
            FROM bom_lines b LEFT JOIN customer_orders o ON o.order_no = b.order_no
            GROUP BY b.order_no ORDER BY b.order_no DESC LIMIT 40"""):
            out.append(_doc("order_bom", r["order_no"], "Production BOM", r["order_no"],
                            r["order_date"], None, f"{r['lines']} lines",
                            url=f"/production/orders/{r['order_no']}",
                            badge=f"{r['lines']} lines"))
    elif t_ == "root_inventory":
        for r in _rows(db, """
            SELECT COALESCE(main_category,'Uncategorized') AS cat, COUNT(*) AS n
            FROM ingredients GROUP BY COALESCE(main_category,'Uncategorized')
            ORDER BY n DESC LIMIT 40"""):
            out.append(_line(r["cat"], f"{r['n']} items", leaf=False,
                             node_type="inv_category", key=r["cat"]))
    elif t_ == "root_procurement":
        for r in _rows(db, """
            SELECT po_no, COALESCE(supplier_name,'') AS supplier,
                   COALESCE(status,'') AS status, po_date,
                   COALESCE(total_value,0) AS val
            FROM purchase_orders ORDER BY id DESC LIMIT 40"""):
            out.append(_doc("po", r["po_no"], "Purchase Order", r["po_no"],
                            r["po_date"], r["val"], r["status"],
                            url=f"/procurement/po/{r['po_no']}"))
    elif t_ == "root_finance":
        for r in _rows(db, """
            SELECT invoice_no, COALESCE(customer_name,'') AS customer,
                   COALESCE(status,'Draft') AS status, invoice_date,
                   COALESCE(amount,0) AS amount, COALESCE(order_no,'') AS order_no
            FROM ar_invoices ORDER BY id DESC LIMIT 40"""):
            out.append(_doc("ar_invoice", r["invoice_no"], "A/R Invoice", r["invoice_no"],
                            r["invoice_date"], r["amount"], r["status"], url="/finance"))
        if not out:
            out.append(_line("No A/R invoices yet", "", leaf=True))
    elif t_ == "root_masters":
        for label, typ, sql, url in [
            ("Customers", "master_customers", "SELECT COUNT(*) FROM customers", "/customers"),
            ("Suppliers", "master_suppliers", "SELECT COUNT(*) FROM suppliers", "/suppliers"),
            ("Recipes", "master_recipes", "SELECT COUNT(*) FROM recipes", "/recipes"),
            ("Brands", "leaf", "SELECT COUNT(*) FROM brands", "/brands"),
        ]:
            out.append(_line(label, _count(db, sql), url=url,
                             leaf=(typ == "leaf"), node_type=typ, key=""))

    # ==================== SALES ORDER DOCUMENT FLOW ====================
    elif t_ == "order":
        o = {"o": k}
        # Each stage becomes a folder node; badge = linked count.
        stages = [
            ("order_lines", "Order Lines", "SELECT COUNT(*) FROM order_lines WHERE order_no=:o", f"/production/orders/{k}"),
            ("order_bom", "Production BOM", "SELECT COUNT(*) FROM bom_lines WHERE order_no=:o", f"/production/orders/{k}"),
            ("order_store", "Store Issues", "SELECT COUNT(*) FROM store_issuance_lines WHERE order_no=:o", "/production/store-issuance"),
            ("order_kitchen", "Kitchen Moves", "SELECT COUNT(*) FROM kitchen_section_transactions WHERE order_no=:o", "/production/orders"),
            ("order_qc", "QC Checks", "SELECT COUNT(*) FROM qc_checks WHERE order_no=:o", "/qc"),
            ("order_pack", "Packing / Dispatch", "SELECT COUNT(*) FROM packing_dispatch WHERE order_no=:o", "/packing"),
            ("order_ar", "A/R Invoice", "SELECT COUNT(*) FROM ar_invoices WHERE order_no=:o", "/finance"),
        ]
        for typ, label, sql, url in stages:
            n = _count(db, sql, o)
            out.append(_line(label, n, url=url, leaf=(n == 0), node_type=typ, key=k))
    elif t_ == "order_lines":
        for r in _rows(db, """
            SELECT COALESCE(recipe_name, recipe_code,'') AS recipe,
                   COALESCE(portions, quantity, 0) AS qty
            FROM order_lines WHERE order_no=:o ORDER BY id LIMIT 200""", {"o": k}):
            out.append(_line(r["recipe"], f"{r['qty']} portions"))
    elif t_ == "order_bom":
        for r in _rows(db, """
            SELECT COALESCE(inventory_code,'') AS code,
                   COALESCE(item_name, ingredient_name,'') AS item,
                   COALESCE(required_qty, quantity, 0) AS qty, COALESCE(uom,'') AS uom
            FROM bom_lines WHERE order_no=:o ORDER BY id LIMIT 300""", {"o": k}):
            out.append(_line(f"{r['item']} ({r['code']})" if r["code"] else r["item"],
                             f"{r['qty']} {r['uom']}".strip(),
                             leaf=not r["code"], node_type="inv_item", key=r["code"]))
    elif t_ == "order_store":
        for r in _rows(db, """
            SELECT COALESCE(inventory_code,'') AS code, COALESCE(item_name,'') AS item,
                   COALESCE(issued_qty, quantity, 0) AS qty
            FROM store_issuance_lines WHERE order_no=:o ORDER BY id LIMIT 300""", {"o": k}):
            out.append(_line(f"{r['item']} ({r['code']})" if r["code"] else r["item"],
                             r["qty"], leaf=not r["code"], node_type="inv_item", key=r["code"]))
    elif t_ in ("order_kitchen", "order_qc", "order_pack", "order_ar"):
        if t_ == "order_ar":
            for r in _rows(db, """
                SELECT invoice_no, COALESCE(status,'') AS status, invoice_date,
                       COALESCE(amount,0) AS amount
                FROM ar_invoices WHERE order_no=:o ORDER BY id DESC LIMIT 50""", {"o": k}):
                out.append(_doc("ar_invoice", r["invoice_no"], "A/R Invoice", r["invoice_no"],
                                r["invoice_date"], r["amount"], r["status"], url="/finance"))
        else:
            table = {"order_kitchen": ("kitchen_section_transactions", "section", "transaction_status"),
                     "order_qc": ("qc_checks", "check_point", "status"),
                     "order_pack": ("packing_dispatch", "dispatch_status", "vehicle_no")}[t_]
            tbl, c1, c2 = table
            for r in _rows(db, f"""
                SELECT COALESCE({c1},'') AS a, COALESCE({c2},'') AS b
                FROM {tbl} WHERE order_no=:o ORDER BY id DESC LIMIT 80""", {"o": k}):
                out.append(_line(r["a"] or "—", r["b"]))

    # ==================== INVENTORY DRILL-DOWN ====================
    elif t_ == "inv_category":
        for r in _rows(db, """
            SELECT inventory_code, COALESCE(item_name, ingredient_name,'') AS item,
                   COALESCE(current_stock,0) AS stock, COALESCE(uom,'') AS uom
            FROM ingredients WHERE COALESCE(main_category,'Uncategorized')=:c
            ORDER BY item LIMIT 150""", {"c": k}):
            out.append(_line(f"{r['item']} ({r['inventory_code']})",
                             f"{r['stock']} {r['uom']}".strip(),
                             leaf=False, node_type="inv_item", key=r["inventory_code"]))
    elif t_ == "inv_item":
        used = _count(db, "SELECT COUNT(*) FROM recipe_ingredients WHERE inventory_code=:c", {"c": k})
        moves = _count(db, "SELECT COUNT(*) FROM inventory_transactions WHERE inventory_code=:c", {"c": k})
        po = _count(db, "SELECT COUNT(*) FROM purchase_order_lines WHERE inventory_code=:c", {"c": k})
        out = [
            _line("Used in Recipes", used, leaf=(used == 0), node_type="inv_item_recipes", key=k),
            _line("Ledger Movements", moves, url=f"/inventory/ledger/{k}", leaf=True),
            _line("On Purchase Orders", po, url="/procurement", leaf=True),
        ]
    elif t_ == "inv_item_recipes":
        for r in _rows(db, """
            SELECT DISTINCT COALESCE(r.recipe_name, r.name,'') AS recipe
            FROM recipe_ingredients ri JOIN recipes r ON r.id = ri.recipe_id
            WHERE ri.inventory_code=:c LIMIT 150""", {"c": k}):
            out.append(_line(r["recipe"], ""))

    # ==================== PURCHASE ORDER FLOW ====================
    elif t_ == "po":
        lines = _count(db, "SELECT COUNT(*) FROM purchase_order_lines WHERE po_no=:p", {"p": k})
        grns = _count(db, "SELECT COUNT(*) FROM grn_receipts WHERE po_no=:p", {"p": k})  # FIX: grn_receipts
        ap = _count(db, "SELECT COUNT(*) FROM ap_invoices WHERE po_no=:p", {"p": k})
        out = [
            _line("PO Lines", lines, leaf=(lines == 0), node_type="po_lines", key=k),
            _line("Goods Receipts (GRN)", grns, leaf=(grns == 0), node_type="po_grns", key=k),
            _line("A/P Invoices", ap, leaf=(ap == 0), node_type="po_ap", key=k),
        ]
    elif t_ == "po_lines":
        for r in _rows(db, """
            SELECT COALESCE(inventory_code,'') AS code, COALESCE(item_name,'') AS item,
                   COALESCE(quantity,0) AS qty
            FROM purchase_order_lines WHERE po_no=:p ORDER BY id LIMIT 200""", {"p": k}):
            out.append(_line(f"{r['item']} ({r['code']})" if r["code"] else r["item"],
                             r["qty"], leaf=not r["code"], node_type="inv_item", key=r["code"]))
    elif t_ == "po_grns":
        for r in _rows(db, """
            SELECT grn_no, COALESCE(status,'') AS status, received_date
            FROM grn_receipts WHERE po_no=:p ORDER BY id DESC LIMIT 50""", {"p": k}):
            out.append(_doc("grn", r["grn_no"], "Goods Receipt", r["grn_no"],
                            r["received_date"], None, r["status"]))
    elif t_ == "po_ap":
        for r in _rows(db, """
            SELECT ap_no, COALESCE(status,'') AS status, invoice_date,
                   COALESCE(amount,0) AS amount
            FROM ap_invoices WHERE po_no=:p ORDER BY id DESC LIMIT 50""", {"p": k}):
            out.append(_doc("ap_invoice", r["ap_no"], "A/P Invoice", r["ap_no"],
                            r["invoice_date"], r["amount"], r["status"], url="/finance"))

    # ==================== MASTERS DRILL-DOWN ====================
    elif t_ == "master_customers":
        for r in _rows(db, """
            SELECT customer_code, customer_name FROM customers
            ORDER BY customer_name LIMIT 150"""):
            out.append(_line(r["customer_name"], r["customer_code"], leaf=False,
                             node_type="customer", key=r["customer_code"]))
    elif t_ == "customer":
        rs = _rows(db, """
            SELECT co.order_no, COALESCE(co.status,'') AS status, co.order_date,
                   COALESCE(co.total_estimated_selling_value,0) AS val
            FROM customer_orders co
            JOIN customers c ON (co.customer_name = c.customer_name OR co.customer_no = c.customer_code)
            WHERE c.customer_code=:c ORDER BY co.id DESC LIMIT 40""", {"c": k})
        for r in rs:
            out.append(_doc("order", r["order_no"], "Sales Order", r["order_no"],
                            r["order_date"], r["val"], r["status"],
                            url=f"/production/orders/{r['order_no']}"))
        if not rs:
            out.append(_line("No orders for this customer", "", leaf=True))
    elif t_ == "master_suppliers":
        for r in _rows(db, """
            SELECT COALESCE(supplier_code,'') AS code, supplier_name FROM suppliers
            ORDER BY supplier_name LIMIT 150"""):
            out.append(_line(r["supplier_name"], r["code"]))
    elif t_ == "master_recipes":
        for r in _rows(db, """
            SELECT id, COALESCE(recipe_name, name,'') AS recipe, COALESCE(status,'') AS status
            FROM recipes ORDER BY recipe LIMIT 150"""):
            out.append(_line(r["recipe"], r["status"], leaf=False,
                             node_type="recipe", key=str(r["id"])))
    elif t_ == "recipe":
        for r in _rows(db, """
            SELECT COALESCE(inventory_code,'') AS code,
                   COALESCE(ingredient_name, item_name,'') AS item,
                   COALESCE(quantity,0) AS qty, COALESCE(uom,'') AS uom
            FROM recipe_ingredients WHERE recipe_id=:r ORDER BY id LIMIT 150""", {"r": k}):
            out.append(_line(f"{r['item']} ({r['code']})" if r["code"] else r["item"],
                             f"{r['qty']} {r['uom']}".strip(),
                             leaf=not r["code"], node_type="inv_item", key=r["code"]))

    if not out:
        out = [_line("No linked documents", "", leaf=True)]
    return JSONResponse({"children": out})
