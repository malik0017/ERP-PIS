# app/modules/printforms/routes.py
"""Printable Forms - the six operational documents.

    /print/{order_no}                 -> hub page listing all six
    /print/{order_no}/order-sheet     -> Order Sheet
    /print/{order_no}/bom-sheet       -> BOM Sheet
    /print/{order_no}/store-issue     -> Store Issue Slip
    /print/{order_no}/qc-certificate  -> QC Certificate
    /print/{order_no}/packing-slip    -> Packing Slip
    /print/{order_no}/delivery-note   -> Delivery Note

Each page uses templates/print/layout.html (A4 print CSS + auto print button)
and reads ONLY existing tables - no schema changes required.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.rbac import require_area

from app.core.templates import render
from app.database.session import get_db

router = APIRouter(prefix="/print", tags=["Print Forms"])

FORMS = [
    {"key": "order-sheet",    "title": "Order Sheet",      "icon": "uil-file-alt",          "desc": "Customer order header + recipe lines for the kitchen wall."},
    {"key": "bom-sheet",      "title": "BOM Sheet",        "icon": "uil-list-ul",           "desc": "Consolidated material requirement for store picking."},
    {"key": "store-issue",    "title": "Store Issue Slip", "icon": "uil-store",             "desc": "Issued materials per section with lot and supplier."},
    {"key": "qc-certificate", "title": "QC Certificate",   "icon": "uil-clipboard-notes",   "desc": "Quality checks, score and result for the order."},
    {"key": "packing-slip",   "title": "Packing Slip",     "icon": "uil-box",               "desc": "Packed portions vs planned with rejection count."},
    {"key": "delivery-note",  "title": "Delivery Note",    "icon": "uil-truck",             "desc": "Dispatch document for driver and customer signature."},
]


def _order(db: Session, order_no: str):
    row = db.execute(text("SELECT * FROM customer_orders WHERE order_no = :o"), {"o": order_no}).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail=f"Order {order_no} not found")
    return row


def _rows(db: Session, sql: str, params: dict) -> list:
    try:
        return list(db.execute(text(sql), params).mappings().all())
    except Exception:
        return []


@router.get("/{order_no}")
async def print_hub(request: Request, order_no: str, db: Session = Depends(get_db)):
    require_area(request, "production_orders")
    order = _order(db, order_no)
    return render(request, "print/hub.html", {"order": order, "forms": FORMS, "order_no": order_no,
                                              "page_title": f"Print Forms {order_no}"})


@router.get("/{order_no}/order-sheet")
async def order_sheet(request: Request, order_no: str, db: Session = Depends(get_db)):
    require_area(request, "production_orders")
    order = _order(db, order_no)
    lines = _rows(db, """
        SELECT recipe_no, recipe_name, required_portions, standard_portions,
               batches, selling_price_per_portion, line_sales_value
        FROM order_lines WHERE order_no = :o ORDER BY id
    """, {"o": order_no})
    return render(request, "print/order_sheet.html", {"order": order, "lines": lines,
                                                      "doc_title": "Order Sheet", "order_no": order_no})


@router.get("/{order_no}/bom-sheet")
async def bom_sheet(request: Request, order_no: str, db: Session = Depends(get_db)):
    require_area(request, "production_orders")
    order = _order(db, order_no)
    lines = _rows(db, """
        SELECT COALESCE(ingredient_main_category, ingredient_category, '-') AS main_category,
               ingredient_code, ingredient_name,
               ROUND(SUM(COALESCE(total_required_with_waste_standard,0)),4) AS qty,
               MAX(standard_uom) AS uom,
               ROUND(SUM(COALESCE(total_estimated_cost,0)),2) AS cost
        FROM bom_lines WHERE order_no = :o
        GROUP BY main_category, ingredient_code, ingredient_name
        ORDER BY main_category, ingredient_code
    """, {"o": order_no})
    total_cost = round(sum(float(l["cost"] or 0) for l in lines), 2)
    return render(request, "print/bom_sheet.html", {"order": order, "lines": lines, "total_cost": total_cost,
                                                    "doc_title": "BOM Sheet", "order_no": order_no})


@router.get("/{order_no}/store-issue")
async def store_issue_slip(request: Request, order_no: str, db: Session = Depends(get_db)):
    require_area(request, "production_orders")
    order = _order(db, order_no)
    lines = _rows(db, """
        SELECT ingredient_code, ingredient_name, issue_to_section,
               COALESCE(input_material_issued, issued_qty, 0) AS issued_qty,
               issued_uom, COALESCE(lot_no,'') AS lot_no, COALESCE(supplier_name,'') AS supplier_name,
               COALESCE(issue_status, status, '') AS status
        FROM store_issuance_lines WHERE order_no = :o
        ORDER BY issue_to_section, ingredient_code
    """, {"o": order_no})
    return render(request, "print/store_issue.html", {"order": order, "lines": lines,
                                                      "doc_title": "Store Issue Slip", "order_no": order_no})


@router.get("/{order_no}/qc-certificate")
async def qc_certificate(request: Request, order_no: str, db: Session = Depends(get_db)):
    require_area(request, "production_orders")
    order = _order(db, order_no)
    checks = _rows(db, """
        SELECT qc_no, qc_type, qc_status, score, checked_by, created_at,
               COALESCE(issue_found,'') AS issue_found, COALESCE(corrective_action,'') AS corrective_action
        FROM qc_checks WHERE order_no = :o ORDER BY id
    """, {"o": order_no})
    return render(request, "print/qc_certificate.html", {"order": order, "checks": checks,
                                                         "doc_title": "QC Certificate", "order_no": order_no})


@router.get("/{order_no}/packing-slip")
async def packing_slip(request: Request, order_no: str, db: Session = Depends(get_db)):
    require_area(request, "production_orders")
    order = _order(db, order_no)
    pack = db.execute(text("SELECT * FROM packing_dispatch WHERE order_no = :o ORDER BY id DESC LIMIT 1"),
                      {"o": order_no}).mappings().first()
    lines = _rows(db, """
        SELECT recipe_no, recipe_name, required_portions FROM order_lines WHERE order_no = :o ORDER BY id
    """, {"o": order_no})
    return render(request, "print/packing_slip.html", {"order": order, "pack": pack, "lines": lines,
                                                       "doc_title": "Packing Slip", "order_no": order_no})


@router.get("/{order_no}/delivery-note")
async def delivery_note(request: Request, order_no: str, db: Session = Depends(get_db)):
    require_area(request, "production_orders")
    order = _order(db, order_no)
    pack = db.execute(text("SELECT * FROM packing_dispatch WHERE order_no = :o ORDER BY id DESC LIMIT 1"),
                      {"o": order_no}).mappings().first()
    lines = _rows(db, """
        SELECT recipe_no, recipe_name, required_portions FROM order_lines WHERE order_no = :o ORDER BY id
    """, {"o": order_no})
    return render(request, "print/delivery_note.html", {"order": order, "pack": pack, "lines": lines,
                                                        "doc_title": "Delivery Note", "order_no": order_no})
