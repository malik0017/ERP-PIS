# app/modules/procurement/routes_print.py
# =============================================================================
# Batch 22 — Purchase Order PRINT view
# -----------------------------------------------------------------------------
# A clean, self-contained printable PO document (A4). Opens on its own page and
# calls window.print() automatically. Included after the base procurement router
# so it lives in the same /procurement namespace.
# =============================================================================

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.templates import render
from app.core.rbac import require_area
from app.database import get_db

# Batch 23 FIX: the base procurement router uses prefix="/procurement". Without
# the same prefix here this route registered as /po/{po_no}/print, so
# /procurement/po/PO-.../print returned 404. Adding the prefix fixes it.
router = APIRouter(prefix="/procurement", tags=["Procurement (Print)"])


@router.get("/po/{po_no}/print")
async def po_print(request: Request, po_no: str, db: Session = Depends(get_db)):
    require_area(request, "procurement")
    po = db.execute(
        text("SELECT * FROM purchase_orders WHERE po_no = :p"), {"p": po_no}
    ).mappings().first()
    if not po:
        return RedirectResponse("/procurement?error=PO not found", status_code=303)
    lines = db.execute(
        text("SELECT * FROM purchase_order_lines WHERE po_no = :p ORDER BY line_no"),
        {"p": po_no},
    ).mappings().all()

    supplier = None
    try:
        supplier = db.execute(text("""
            SELECT supplier_name, phone, email, city, vat_number
            FROM suppliers
            WHERE supplier_code = :c OR supplier_name = :n
            LIMIT 1
        """), {"c": po["supplier_code"] or "", "n": po["supplier_name"] or ""}).mappings().first()
    except Exception:
        pass

    company_name = "International Specialized Food Company"
    try:
        row = db.execute(text(
            "SELECT company_name FROM companies WHERE id = :i"
        ), {"i": po.get("company_id") or 1}).mappings().first()
        if row and row.get("company_name"):
            company_name = row["company_name"]
    except Exception:
        pass

    return render(request, "procurement/po_print.html", {
        "po": po, "lines": lines, "supplier": supplier,
        "company_name": company_name, "page_title": f"PO {po_no} — Print",
    })
