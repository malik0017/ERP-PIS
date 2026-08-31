# app/modules/production/routes_docs.py
# =============================================================================
# Batch 70 — Printable operational documents (QC Certificate + Delivery Note)
# -----------------------------------------------------------------------------
# Two gaps that kept QC and Packing/Dispatch below "complete":
#   * QC had no CERTIFICATE — food ERPs need a printable, per-order QC/HACCP
#     certificate showing temperatures, scores, pass/fail, checker and any
#     corrective action.
#   * Dispatch had no DELIVERY NOTE / proof-of-delivery — the document the
#     driver carries and the customer signs.
#
# Both are read-only, print-friendly (window.print()) pages built from data
# already captured. Registered in main.py:
#     from app.modules.production.routes_docs import router as prod_docs_router
#     app.include_router(prod_docs_router)
# =============================================================================

from fastapi import APIRouter, Depends, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.templates import render
from app.core.rbac import require_area
from app.database.session import get_db

router = APIRouter(tags=["Documents"])


def _rows(db, sql, params=None):
    try:
        return [dict(r) for r in db.execute(text(sql), params or {}).mappings().all()]
    except Exception:
        return []


def _one(db, sql, params=None):
    try:
        r = db.execute(text(sql), params or {}).mappings().first()
        return dict(r) if r else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# QC CERTIFICATE  —  /qc/orders/{order_no}/certificate
# ---------------------------------------------------------------------------
@router.get("/qc/orders/{order_no}/certificate")
def qc_certificate(request: Request, order_no: str, db: Session = Depends(get_db)):
    require_area(request, "qc")
    order = _one(db, """
        SELECT order_no, customer_name, COALESCE(brand,'') AS brand,
               COALESCE(status,'') AS status,
               COALESCE(required_delivery_date,'') AS delivery_date,
               COALESCE(required_delivery_time,'') AS delivery_time
        FROM customer_orders WHERE order_no = :o
    """, {"o": order_no})

    checks = _rows(db, """
        SELECT qc_no, COALESCE(recipe_name,'') AS recipe, COALESCE(section,'') AS section,
               COALESCE(check_type,'') AS check_type, temperature_c,
               COALESCE(appearance_score,0) AS appearance, COALESCE(taste_score,0) AS taste,
               COALESCE(portion_weight_score,0) AS portion_weight,
               COALESCE(packaging_score,0) AS packaging, COALESCE(hygiene_score,0) AS hygiene,
               COALESCE(overall_score,0) AS overall, COALESCE(qc_status,'') AS qc_status,
               COALESCE(checked_by,'') AS checked_by, checked_at,
               COALESCE(issue_found,'') AS issue_found,
               COALESCE(corrective_action,'') AS corrective_action
        FROM qc_checks WHERE order_no = :o ORDER BY recipe_name, qc_no
    """, {"o": order_no})

    passed = sum(1 for c in checks if (c["qc_status"] or "").lower() == "passed")
    rejected = sum(1 for c in checks if (c["qc_status"] or "").lower() == "rejected")
    hold = sum(1 for c in checks if (c["qc_status"] or "").lower() == "hold")
    verdict = "PASSED" if checks and rejected == 0 and hold == 0 else ("REJECTED" if rejected else "ON HOLD")
    avg_score = round(sum(float(c["overall"] or 0) for c in checks) / len(checks), 1) if checks else 0

    return render(request, "documents/qc_certificate.html", {
        "order": order, "order_no": order_no, "checks": checks,
        "summary": {"total": len(checks), "passed": passed, "rejected": rejected,
                    "hold": hold, "verdict": verdict, "avg_score": avg_score},
        "page_title": f"QC Certificate — {order_no}",
    })


# ---------------------------------------------------------------------------
# DELIVERY NOTE / POD  —  /dispatch/{dispatch_id}/delivery-note
# ---------------------------------------------------------------------------
@router.get("/dispatch/{dispatch_id}/delivery-note")
def delivery_note(request: Request, dispatch_id: int, db: Session = Depends(get_db)):
    require_area(request, "dispatch")
    d = _one(db, """
        SELECT dispatch_no, order_no, COALESCE(customer_name,'') AS customer_name,
               COALESCE(packed_portions,0) AS packed_portions,
               COALESCE(rejected_portions,0) AS rejected_portions,
               COALESCE(packed_bags,0) AS packed_bags,
               COALESCE(region,'') AS region,
               dispatch_date, COALESCE(vehicle_no,'') AS vehicle_no,
               COALESCE(driver_name,'') AS driver_name,
               delivery_temperature_c, COALESCE(dispatch_status,'') AS dispatch_status,
               COALESCE(remarks,'') AS remarks
        FROM packing_dispatch WHERE id = :i
    """, {"i": dispatch_id})

    order = None
    lines = []
    if d:
        order = _one(db, """
            SELECT order_no, customer_name, COALESCE(brand,'') AS brand,
                   COALESCE(required_delivery_date,'') AS delivery_date,
                   COALESCE(required_delivery_time,'') AS delivery_time,
                   COALESCE(total_planned_portions,0) AS total_portions,
                   COALESCE(total_estimated_selling_value,0) AS total_value
            FROM customer_orders WHERE order_no = :o
        """, {"o": d["order_no"]})
        lines = _rows(db, """
            SELECT COALESCE(recipe_name, recipe_no,'') AS recipe,
                   COALESCE(required_portions, 0) AS portions
            FROM order_lines WHERE order_no = :o ORDER BY id
        """, {"o": d["order_no"]})

    return render(request, "documents/delivery_note.html", {
        "d": d, "order": order, "lines": lines,
        "page_title": f"Delivery Note — {d['dispatch_no'] if d else dispatch_id}",
    })
