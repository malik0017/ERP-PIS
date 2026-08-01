# app/modules/reports/routes_workflow.py
# =============================================================================
# Batch 66 — ERP WORKFLOW & DATA-MOVEMENT reference (in-app)
# -----------------------------------------------------------------------------
# Brings the shared "ERP Workflow & Data Movement" document INTO the ERP as a
# living page under Reports. It renders the five phases (Master data ▸
# Procurement ▸ Inventory ▸ Sales & delivery ▸ Finance) and, crucially, shows
# LIVE status: for each auto-posting source it reports how many GL journals of
# that type actually exist, so you can see the data flow is wired end-to-end.
#
# Registered in app/main.py:
#     from app.modules.reports.routes_workflow import router as reports_workflow_router
#     app.include_router(reports_workflow_router)
# =============================================================================

from fastapi import APIRouter, Depends, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.templates import render
from app.core.rbac import require_area
from app.database.session import get_db

router = APIRouter(tags=["Reports"])


def _count(db: Session, sql: str, params: dict | None = None) -> int:
    try:
        return int(db.execute(text(sql), params or {}).scalar() or 0)
    except Exception:
        return 0


# GL auto-posting sources described by the workflow, with their Dr/Cr mapping
# and the source_type used by post_journal. `live` is filled from gl_journals.
GL_SOURCES = [
    ("Stock receipt (GRN)", "1130 Inventory", "2200 GR accrual", "GRN"),
    ("Vendor invoice (AP)", "2200 GR accrual", "2100 Accounts payable", "AP_INVOICE"),
    ("Vendor payment", "2100 Accounts payable", "1110 Bank", "PAYMENT_OUT"),
    ("Stock issuance", "5100 WIP / COGS", "1130 Inventory", "STORE_ISSUE"),
    ("Delivery (COGS)", "5100 COGS", "1130 Inventory", "DISPATCH_COGS"),
    ("Sales invoice (AR)", "1120 AR", "4100 Revenue + 2300 VAT", "AR_INVOICE"),
    ("Customer payment", "1110 Bank", "1120 AR", "PAYMENT_IN"),
    ("Inventory adjustment", "1130 / 5100", "5100 / 1130", "INV_ADJUST"),
]


@router.get("/reports/workflow")
def erp_workflow(request: Request, db: Session = Depends(get_db)):
    require_area(request, "reports")

    # live GL source counts (0 = not yet triggered, not necessarily broken)
    sources = []
    for label, dr, cr, stype in GL_SOURCES:
        sources.append({
            "label": label, "debit": dr, "credit": cr, "source_type": stype,
            "live": _count(db, "SELECT COUNT(*) FROM gl_journals WHERE source_type=:s",
                           {"s": stype}),
        })

    # phase-level record counts so each stage shows real volume
    phases = {
        "customers": _count(db, "SELECT COUNT(*) FROM customers"),
        "suppliers": _count(db, "SELECT COUNT(*) FROM suppliers"),
        "items": _count(db, "SELECT COUNT(*) FROM ingredients"),
        "coa": _count(db, "SELECT COUNT(*) FROM gl_accounts"),
        "pos": _count(db, "SELECT COUNT(*) FROM purchase_orders"),
        "grns": _count(db, "SELECT COUNT(*) FROM grn_receipts"),
        "ap": _count(db, "SELECT COUNT(*) FROM ap_invoices"),
        "issues": _count(db, "SELECT COUNT(DISTINCT order_no) FROM store_issuance_lines WHERE finalized=1"),
        "orders": _count(db, "SELECT COUNT(*) FROM customer_orders"),
        "dispatch": _count(db, "SELECT COUNT(*) FROM packing_dispatch"),
        "ar": _count(db, "SELECT COUNT(*) FROM ar_invoices"),
        "journals": _count(db, "SELECT COUNT(*) FROM gl_journals"),
    }

    # trial-balance health: does the ledger balance?
    tb = {"debit": 0.0, "credit": 0.0, "balanced": True}
    try:
        row = db.execute(text("""
            SELECT ROUND(COALESCE(SUM(debit),0),2) AS d,
                   ROUND(COALESCE(SUM(credit),0),2) AS c
            FROM gl_journal_lines
        """)).mappings().first()
        if row:
            tb["debit"] = float(row["d"] or 0)
            tb["credit"] = float(row["c"] or 0)
            tb["balanced"] = abs(tb["debit"] - tb["credit"]) < 0.01
    except Exception:
        pass

    return render(request, "reports/workflow.html", {
        "page_title": "ERP Workflow & Data Movement",
        "sources": sources, "phases": phases, "tb": tb,
    })
