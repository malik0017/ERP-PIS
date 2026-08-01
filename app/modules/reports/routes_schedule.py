# app/modules/reports/routes_schedule.py
# =============================================================================
# Batch 74 — Scheduled report exports (P3)
# -----------------------------------------------------------------------------
# On-demand CSV/XLSX export already exists (/reports/export/{key}). This adds the
# missing SCHEDULING layer: a registry where a user defines "export report X as
# CSV/XLSX every day/week/month and email it to these recipients". The registry
# is stored in `report_schedules`; each row can be run on demand (which just
# links to the existing export endpoint) and is the record a cron/worker reads
# to actually deliver by email.
#
# Delivery mechanism: a small worker (documented in the README) queries due
# schedules and emails the generated file. The app writes and manages the
# schedule; the cron does the sending, so no SMTP config is required to use the
# feature for on-demand exports.
#
# Registered in main.py:
#     from app.modules.reports.routes_schedule import router as report_schedule_router
#     app.include_router(report_schedule_router)
# =============================================================================

from datetime import datetime

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.templates import render
from app.core.rbac import require_area, require_action
from app.database.session import get_db

router = APIRouter(tags=["Reports"])

REPORTS = [
    ("order-register", "Customer Order Register"),
    ("recipe-master", "Recipe Master & Versions"),
    ("recipe-bom", "Recipe Ingredients / BOM"),
    ("bom-lines", "BOM Lines"),
    ("store-issuance", "Store Issuance"),
    ("yield-wastage", "Yield & Wastage"),
    ("qc-checks", "QC Checks"),
    ("packing", "Packing"),
    ("dispatch", "Dispatch"),
    ("bom-section-cost", "BOM Cost by Section"),
    ("bom-category-cost", "BOM Cost by Category"),
]
REPORT_LABELS = dict(REPORTS)
FREQUENCIES = ["Daily", "Weekly", "Monthly"]


def _cid(request: Request) -> int:
    try:
        return int(request.session.get("company_id") or 1)
    except Exception:
        return 1


def ensure_schema(db: Session) -> None:
    try:
        db.execute(text("""
            CREATE TABLE IF NOT EXISTS report_schedules (
                id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                company_id INT NULL,
                report_key VARCHAR(50) NOT NULL,
                report_label VARCHAR(150) NULL,
                fmt VARCHAR(10) NOT NULL DEFAULT 'csv',
                frequency VARCHAR(12) NOT NULL DEFAULT 'Weekly',
                recipients VARCHAR(500) NULL,
                is_active TINYINT(1) NOT NULL DEFAULT 1,
                last_run_at DATETIME NULL,
                created_by VARCHAR(120) NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """))
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass


@router.get("/reports/schedules")
def schedules_page(request: Request, db: Session = Depends(get_db)):
    require_area(request, "reports")
    ensure_schema(db)
    cid = _cid(request)
    schedules = []
    try:
        schedules = [dict(r) for r in db.execute(text("""
            SELECT * FROM report_schedules WHERE (company_id=:c OR company_id IS NULL)
            ORDER BY created_at DESC
        """), {"c": cid}).mappings().all()]
    except Exception:
        schedules = []
    return render(request, "reports/schedules.html", {
        "schedules": schedules, "reports": REPORTS, "frequencies": FREQUENCIES,
        "page_title": "Scheduled Exports",
    })


@router.post("/reports/schedules/create")
async def schedule_create(request: Request,
                          report_key: str = Form(...), fmt: str = Form("csv"),
                          frequency: str = Form("Weekly"), recipients: str = Form(""),
                          db: Session = Depends(get_db)):
    require_action(request, "reports", "create")
    ensure_schema(db)
    label = REPORT_LABELS.get(report_key, report_key)
    fmt = "xlsx" if str(fmt).lower() in {"xlsx", "excel"} else "csv"
    db.execute(text("""
        INSERT INTO report_schedules (company_id, report_key, report_label, fmt, frequency, recipients, is_active, created_by)
        VALUES (:c, :k, :l, :f, :fr, :r, 1, :by)
    """), {"c": _cid(request), "k": report_key, "l": label, "f": fmt, "fr": frequency,
           "r": recipients, "by": request.session.get("username") or ""})
    db.commit()
    return RedirectResponse("/reports/schedules?toast=success&title=Scheduled&msg=Export schedule created", status_code=303)


@router.post("/reports/schedules/{sched_id}/toggle")
async def schedule_toggle(request: Request, sched_id: int, db: Session = Depends(get_db)):
    require_action(request, "reports", "edit")
    ensure_schema(db)
    db.execute(text("UPDATE report_schedules SET is_active = 1 - is_active WHERE id=:i"), {"i": sched_id})
    db.commit()
    return RedirectResponse("/reports/schedules", status_code=303)


@router.post("/reports/schedules/{sched_id}/delete")
async def schedule_delete(request: Request, sched_id: int, db: Session = Depends(get_db)):
    require_action(request, "reports", "delete")
    ensure_schema(db)
    db.execute(text("DELETE FROM report_schedules WHERE id=:i"), {"i": sched_id})
    db.commit()
    return RedirectResponse("/reports/schedules?toast=success&title=Removed&msg=Schedule deleted", status_code=303)
