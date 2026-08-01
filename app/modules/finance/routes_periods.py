# app/modules/finance/routes_periods.py
# =============================================================================
# Batch 73 — Finance: Period Close + Cost Centers (P3)
# -----------------------------------------------------------------------------
# Two enterprise-accounting features on top of the existing GL:
#
#  A) PERIOD CLOSE — lock a fiscal period (year+month) so no new journals can be
#     dated inside it. `gl_periods(year, month, status)`; a screen lists periods
#     with their posted totals and Open/Close/Reopen actions. `is_period_open()`
#     is exposed for the posting engine to consult (soft-guard: if the table is
#     missing everything is open, so nothing breaks on older installs).
#
#  B) COST CENTERS — a `cost_centers` master (code, name, active) plus a
#     cost-center dimension already available on journal lines via the `party`
#     tag convention. A screen manages centers and shows a per-center P&L
#     summary from the GL.
#
# Registered in main.py:
#     from app.modules.finance.routes_periods import router as finance_periods_router
#     app.include_router(finance_periods_router)
# =============================================================================

from datetime import datetime, date

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.templates import render
from app.core.rbac import require_area, require_action
from app.database.session import get_db

router = APIRouter(prefix="/finance", tags=["Finance"])

MONTHS = ["", "January", "February", "March", "April", "May", "June",
          "July", "August", "September", "October", "November", "December"]


def _cid(request: Request) -> int:
    try:
        return int(request.session.get("company_id") or 1)
    except Exception:
        return 1


def ensure_schema(db: Session) -> None:
    try:
        db.execute(text("""
            CREATE TABLE IF NOT EXISTS gl_periods (
                id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                company_id INT NOT NULL DEFAULT 1,
                fy_year INT NOT NULL,
                fy_month INT NOT NULL,
                status VARCHAR(12) NOT NULL DEFAULT 'OPEN',
                closed_by VARCHAR(120) NULL,
                closed_at DATETIME NULL,
                UNIQUE KEY uq_period (company_id, fy_year, fy_month)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """))
        db.execute(text("""
            CREATE TABLE IF NOT EXISTS cost_centers (
                id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                company_id INT NOT NULL DEFAULT 1,
                cc_code VARCHAR(30) NOT NULL,
                cc_name VARCHAR(150) NOT NULL,
                is_active TINYINT(1) NOT NULL DEFAULT 1,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                UNIQUE KEY uq_cc (company_id, cc_code)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """))
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass


def is_period_open(db: Session, company_id: int, d: date) -> bool:
    """Soft guard used by the posting engine. Missing table → always open."""
    try:
        row = db.execute(text("""
            SELECT status FROM gl_periods
            WHERE company_id=:c AND fy_year=:y AND fy_month=:m
        """), {"c": company_id or 1, "y": d.year, "m": d.month}).scalar()
        return (row or "OPEN") == "OPEN"
    except Exception:
        return True


# ---------------------------------------------------------------------------
# PERIOD CLOSE
# ---------------------------------------------------------------------------
@router.get("/periods")
def periods_page(request: Request, db: Session = Depends(get_db)):
    require_area(request, "finance")
    ensure_schema(db)
    cid = _cid(request)

    # posted totals per year+month, joined to saved period status
    rows = []
    try:
        rows = [dict(r) for r in db.execute(text("""
            SELECT YEAR(j.journal_date) AS fy_year, MONTH(j.journal_date) AS fy_month,
                   COUNT(DISTINCT j.journal_no) AS journals,
                   ROUND(SUM(l.debit),2) AS total_debit,
                   ROUND(SUM(l.credit),2) AS total_credit,
                   COALESCE(p.status,'OPEN') AS status
            FROM gl_journals j
            JOIN gl_journal_lines l ON l.journal_no = j.journal_no
            LEFT JOIN gl_periods p
              ON p.fy_year = YEAR(j.journal_date) AND p.fy_month = MONTH(j.journal_date)
             AND (p.company_id = :c OR p.company_id IS NULL)
            WHERE j.journal_date IS NOT NULL AND (j.company_id = :c OR j.company_id IS NULL)
            GROUP BY YEAR(j.journal_date), MONTH(j.journal_date), COALESCE(p.status,'OPEN')
            ORDER BY fy_year DESC, fy_month DESC
        """), {"c": cid}).mappings().all()]
    except Exception:
        rows = []
    for r in rows:
        r["month_name"] = MONTHS[int(r["fy_month"])] if r.get("fy_month") else ""

    return render(request, "finance/periods.html", {
        "rows": rows, "page_title": "Period Close",
    })


@router.post("/periods/set")
async def periods_set(request: Request,
                      fy_year: int = Form(...), fy_month: int = Form(...),
                      status: str = Form("CLOSED"),
                      db: Session = Depends(get_db)):
    require_action(request, "finance", "edit")
    ensure_schema(db)
    cid = _cid(request)
    status = "CLOSED" if str(status).upper() == "CLOSED" else "OPEN"
    user = request.session.get("username") or ""
    db.execute(text("""
        INSERT INTO gl_periods (company_id, fy_year, fy_month, status, closed_by, closed_at)
        VALUES (:c, :y, :m, :s, :u, :now)
        ON DUPLICATE KEY UPDATE status=:s, closed_by=:u, closed_at=:now
    """), {"c": cid, "y": fy_year, "m": fy_month, "s": status,
           "u": user if status == "CLOSED" else None,
           "now": datetime.utcnow() if status == "CLOSED" else None})
    db.commit()
    verb = "closed" if status == "CLOSED" else "reopened"
    return RedirectResponse(
        f"/finance/periods?toast=success&title=Period {verb}&msg={MONTHS[fy_month]} {fy_year} is now {status}",
        status_code=303)


# ---------------------------------------------------------------------------
# COST CENTERS
# ---------------------------------------------------------------------------
@router.get("/cost-centers")
def cost_centers_page(request: Request, db: Session = Depends(get_db)):
    require_area(request, "finance")
    ensure_schema(db)
    cid = _cid(request)
    centers = [dict(r) for r in db.execute(text("""
        SELECT cc_code, cc_name, is_active FROM cost_centers
        WHERE company_id=:c ORDER BY cc_code
    """), {"c": cid}).mappings().all()]

    # per-center P&L from the GL (party tag carries the center on ops journals)
    pnl = []
    try:
        pnl = [dict(r) for r in db.execute(text("""
            SELECT COALESCE(NULLIF(l.party,''),'(unassigned)') AS center,
                   ROUND(SUM(CASE WHEN a.account_code LIKE '4%' THEN l.credit - l.debit ELSE 0 END),2) AS revenue,
                   ROUND(SUM(CASE WHEN a.account_code LIKE '5%' THEN l.debit - l.credit ELSE 0 END),2) AS cost
            FROM gl_journal_lines l
            JOIN gl_journals j ON j.journal_no = l.journal_no
            JOIN gl_accounts a ON a.account_code = l.account_code
            WHERE (a.account_code LIKE '4%' OR a.account_code LIKE '5%')
              AND (j.company_id=:c OR j.company_id IS NULL)
            GROUP BY COALESCE(NULLIF(l.party,''),'(unassigned)')
            HAVING revenue <> 0 OR cost <> 0
            ORDER BY revenue DESC
            LIMIT 100
        """), {"c": cid}).mappings().all()]
        for r in pnl:
            r["margin"] = round(float(r["revenue"] or 0) - float(r["cost"] or 0), 2)
    except Exception:
        pnl = []

    return render(request, "finance/cost_centers.html", {
        "centers": centers, "pnl": pnl, "page_title": "Cost Centers",
    })


@router.post("/cost-centers/create")
async def cost_center_create(request: Request,
                             cc_code: str = Form(...), cc_name: str = Form(...),
                             db: Session = Depends(get_db)):
    require_action(request, "finance", "edit")
    ensure_schema(db)
    cid = _cid(request)
    code = (cc_code or "").strip().upper()
    name = (cc_name or "").strip()
    if code and name:
        try:
            db.execute(text("""
                INSERT INTO cost_centers (company_id, cc_code, cc_name, is_active)
                VALUES (:c, :code, :name, 1)
                ON DUPLICATE KEY UPDATE cc_name=:name, is_active=1
            """), {"c": cid, "code": code, "name": name})
            db.commit()
        except Exception:
            db.rollback()
    return RedirectResponse("/finance/cost-centers?toast=success&title=Saved&msg=Cost center saved", status_code=303)


@router.post("/cost-centers/{cc_code}/toggle")
async def cost_center_toggle(request: Request, cc_code: str, db: Session = Depends(get_db)):
    require_action(request, "finance", "edit")
    ensure_schema(db)
    cid = _cid(request)
    db.execute(text("UPDATE cost_centers SET is_active = 1 - is_active WHERE company_id=:c AND cc_code=:code"),
               {"c": cid, "code": cc_code})
    db.commit()
    return RedirectResponse("/finance/cost-centers", status_code=303)
