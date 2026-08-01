# app/modules/hr/routes_payroll.py
# =============================================================================
# Batch 74 — HCM: Payroll + Leave + Shifts (P2)
# -----------------------------------------------------------------------------
# Moves HCM from stub (employee master + attendance only) to usable. Adds three
# self-contained sub-modules on top of hr_employees / hr_attendance:
#
#   LEAVE   — hr_leave_requests: employees apply, admin approves/rejects; a
#             per-employee balance (annual allowance minus approved days).
#   SHIFTS  — hr_shifts (define named shifts with start/end) and
#             hr_shift_assignments (assign an employee to a shift for a date).
#   PAYROLL — salary structure on the employee (basic + allowances - deductions),
#             plus a monthly payroll RUN that generates a payslip per active
#             employee, prorated by attendance (present + paid-leave days) over
#             the month's working days. Net pay = earnings - deductions.
#
# All tables auto-create (ensure_schema). Registered in main.py:
#     from app.modules.hr.routes_payroll import router as hr_payroll_router
#     app.include_router(hr_payroll_router)
# =============================================================================

import calendar
from datetime import datetime, date

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.templates import render
from app.core.rbac import require_area, require_action
from app.database.session import get_db

router = APIRouter(prefix="/hr", tags=["HCM"])

MONTHS = ["", "January", "February", "March", "April", "May", "June",
          "July", "August", "September", "October", "November", "December"]


def _cid(request: Request) -> int:
    try:
        return int(request.session.get("company_id") or 1)
    except Exception:
        return 1


def _user(request: Request) -> str:
    return request.session.get("username") or ""


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


def ensure_schema(db: Session) -> None:
    stmts = [
        # salary columns on the employee master
        "ALTER TABLE hr_employees ADD COLUMN IF NOT EXISTS basic_salary DECIMAL(18,2) NOT NULL DEFAULT 0",
        "ALTER TABLE hr_employees ADD COLUMN IF NOT EXISTS allowances DECIMAL(18,2) NOT NULL DEFAULT 0",
        "ALTER TABLE hr_employees ADD COLUMN IF NOT EXISTS deductions DECIMAL(18,2) NOT NULL DEFAULT 0",
        "ALTER TABLE hr_employees ADD COLUMN IF NOT EXISTS annual_leave_days INT NOT NULL DEFAULT 21",
        """CREATE TABLE IF NOT EXISTS hr_leave_requests (
            id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
            company_id INT NULL,
            employee_code VARCHAR(30) NOT NULL,
            leave_type VARCHAR(30) NOT NULL DEFAULT 'Annual',
            date_from DATE NOT NULL,
            date_to DATE NOT NULL,
            days INT NOT NULL DEFAULT 1,
            reason VARCHAR(300) NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'Pending',
            decided_by VARCHAR(120) NULL,
            decided_at DATETIME NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",
        """CREATE TABLE IF NOT EXISTS hr_shifts (
            id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
            company_id INT NULL,
            shift_code VARCHAR(20) NOT NULL,
            shift_name VARCHAR(80) NOT NULL,
            start_time VARCHAR(8) NULL,
            end_time VARCHAR(8) NULL,
            is_active TINYINT(1) NOT NULL DEFAULT 1,
            UNIQUE KEY uq_shift (company_id, shift_code)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",
        """CREATE TABLE IF NOT EXISTS hr_shift_assignments (
            id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
            company_id INT NULL,
            employee_code VARCHAR(30) NOT NULL,
            shift_code VARCHAR(20) NOT NULL,
            work_date DATE NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY uq_assign (employee_code, work_date)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",
        """CREATE TABLE IF NOT EXISTS hr_payroll_runs (
            id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
            company_id INT NULL,
            fy_year INT NOT NULL,
            fy_month INT NOT NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'Draft',
            run_by VARCHAR(120) NULL,
            run_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY uq_run (company_id, fy_year, fy_month)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",
        """CREATE TABLE IF NOT EXISTS hr_payslips (
            id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
            run_id INT NOT NULL,
            employee_code VARCHAR(30) NOT NULL,
            employee_name VARCHAR(255) NULL,
            basic DECIMAL(18,2) NOT NULL DEFAULT 0,
            allowances DECIMAL(18,2) NOT NULL DEFAULT 0,
            deductions DECIMAL(18,2) NOT NULL DEFAULT 0,
            working_days INT NOT NULL DEFAULT 0,
            paid_days INT NOT NULL DEFAULT 0,
            gross DECIMAL(18,2) NOT NULL DEFAULT 0,
            net DECIMAL(18,2) NOT NULL DEFAULT 0,
            KEY idx_run (run_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",
    ]
    for s in stmts:
        try:
            db.execute(text(s))
            db.commit()
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass


# ===========================================================================
# LEAVE
# ===========================================================================
@router.get("/leave")
def leave_page(request: Request, db: Session = Depends(get_db)):
    require_area(request, "hr")
    ensure_schema(db)
    cid = _cid(request)
    requests = _rows(db, """
        SELECT l.*, e.full_name FROM hr_leave_requests l
        LEFT JOIN hr_employees e ON e.employee_code = l.employee_code
        WHERE (l.company_id=:c OR l.company_id IS NULL)
        ORDER BY l.created_at DESC LIMIT 300
    """, {"c": cid})
    employees = _rows(db, "SELECT employee_code, full_name, annual_leave_days FROM hr_employees WHERE status='Active' ORDER BY full_name")

    # per-employee balance = allowance - approved days this year
    taken = {r["employee_code"]: r["d"] for r in _rows(db, """
        SELECT employee_code, COALESCE(SUM(days),0) AS d FROM hr_leave_requests
        WHERE status='Approved' AND YEAR(date_from)=YEAR(CURDATE())
        GROUP BY employee_code
    """)}
    for e in employees:
        e["taken"] = int(taken.get(e["employee_code"], 0))
        e["balance"] = int(e["annual_leave_days"] or 0) - e["taken"]

    return render(request, "hr/leave.html", {
        "requests": requests, "employees": employees, "page_title": "Leave",
    })


@router.post("/leave/apply")
async def leave_apply(request: Request,
                      employee_code: str = Form(...), leave_type: str = Form("Annual"),
                      date_from: str = Form(...), date_to: str = Form(...),
                      reason: str = Form(""), db: Session = Depends(get_db)):
    require_action(request, "hr", "create")
    ensure_schema(db)
    try:
        d1 = datetime.strptime(date_from, "%Y-%m-%d").date()
        d2 = datetime.strptime(date_to, "%Y-%m-%d").date()
        days = max(1, (d2 - d1).days + 1)
    except Exception:
        return RedirectResponse("/hr/leave?toast=danger&title=Invalid dates&msg=Check the leave dates", status_code=303)
    db.execute(text("""
        INSERT INTO hr_leave_requests (company_id, employee_code, leave_type, date_from, date_to, days, reason, status)
        VALUES (:c, :e, :t, :d1, :d2, :days, :r, 'Pending')
    """), {"c": _cid(request), "e": employee_code, "t": leave_type,
           "d1": d1, "d2": d2, "days": days, "r": reason})
    db.commit()
    return RedirectResponse("/hr/leave?toast=success&title=Applied&msg=Leave request submitted", status_code=303)


@router.post("/leave/{leave_id}/decide")
async def leave_decide(request: Request, leave_id: int,
                       decision: str = Form(...), db: Session = Depends(get_db)):
    require_action(request, "hr", "edit")
    ensure_schema(db)
    status = "Approved" if decision.lower() == "approve" else "Rejected"
    db.execute(text("""
        UPDATE hr_leave_requests SET status=:s, decided_by=:u, decided_at=:now WHERE id=:i
    """), {"s": status, "u": _user(request), "now": datetime.utcnow(), "i": leave_id})
    db.commit()
    return RedirectResponse(f"/hr/leave?toast=success&title={status}&msg=Leave request {status.lower()}", status_code=303)


# ===========================================================================
# SHIFTS
# ===========================================================================
@router.get("/shifts")
def shifts_page(request: Request, db: Session = Depends(get_db)):
    require_area(request, "hr")
    ensure_schema(db)
    cid = _cid(request)
    shifts = _rows(db, "SELECT * FROM hr_shifts WHERE (company_id=:c OR company_id IS NULL) ORDER BY shift_code", {"c": cid})
    assignments = _rows(db, """
        SELECT a.*, e.full_name, s.shift_name, s.start_time, s.end_time
        FROM hr_shift_assignments a
        LEFT JOIN hr_employees e ON e.employee_code = a.employee_code
        LEFT JOIN hr_shifts s ON s.shift_code = a.shift_code
        WHERE (a.company_id=:c OR a.company_id IS NULL)
        ORDER BY a.work_date DESC LIMIT 200
    """, {"c": cid})
    employees = _rows(db, "SELECT employee_code, full_name FROM hr_employees WHERE status='Active' ORDER BY full_name")
    return render(request, "hr/shifts.html", {
        "shifts": shifts, "assignments": assignments, "employees": employees,
        "page_title": "Shifts",
    })


@router.post("/shifts/create")
async def shift_create(request: Request, shift_code: str = Form(...), shift_name: str = Form(...),
                       start_time: str = Form(""), end_time: str = Form(""),
                       db: Session = Depends(get_db)):
    require_action(request, "hr", "create")
    ensure_schema(db)
    code = (shift_code or "").strip().upper()
    if code and shift_name.strip():
        db.execute(text("""
            INSERT INTO hr_shifts (company_id, shift_code, shift_name, start_time, end_time, is_active)
            VALUES (:c, :code, :name, :st, :et, 1)
            ON DUPLICATE KEY UPDATE shift_name=:name, start_time=:st, end_time=:et, is_active=1
        """), {"c": _cid(request), "code": code, "name": shift_name.strip(),
               "st": start_time, "et": end_time})
        db.commit()
    return RedirectResponse("/hr/shifts?toast=success&title=Saved&msg=Shift saved", status_code=303)


@router.post("/shifts/assign")
async def shift_assign(request: Request, employee_code: str = Form(...),
                       shift_code: str = Form(...), work_date: str = Form(...),
                       db: Session = Depends(get_db)):
    require_action(request, "hr", "edit")
    ensure_schema(db)
    try:
        wd = datetime.strptime(work_date, "%Y-%m-%d").date()
    except Exception:
        return RedirectResponse("/hr/shifts?toast=danger&title=Invalid date&msg=Check the work date", status_code=303)
    db.execute(text("""
        INSERT INTO hr_shift_assignments (company_id, employee_code, shift_code, work_date)
        VALUES (:c, :e, :s, :d)
        ON DUPLICATE KEY UPDATE shift_code=:s
    """), {"c": _cid(request), "e": employee_code, "s": shift_code, "d": wd})
    db.commit()
    return RedirectResponse("/hr/shifts?toast=success&title=Assigned&msg=Shift assigned", status_code=303)


# ===========================================================================
# PAYROLL
# ===========================================================================
@router.get("/payroll")
def payroll_page(request: Request, db: Session = Depends(get_db)):
    require_area(request, "hr")
    ensure_schema(db)
    cid = _cid(request)
    runs = _rows(db, """
        SELECT r.*, COUNT(p.id) AS payslips, COALESCE(SUM(p.net),0) AS total_net
        FROM hr_payroll_runs r LEFT JOIN hr_payslips p ON p.run_id = r.id
        WHERE (r.company_id=:c OR r.company_id IS NULL)
        GROUP BY r.id ORDER BY r.fy_year DESC, r.fy_month DESC LIMIT 60
    """, {"c": cid})
    for r in runs:
        r["month_name"] = MONTHS[int(r["fy_month"])] if r.get("fy_month") else ""

    # selected run detail (payslips)
    run_id = request.query_params.get("run_id")
    payslips, sel_run = [], None
    if run_id:
        sel_run = _one(db, "SELECT * FROM hr_payroll_runs WHERE id=:i", {"i": run_id})
        if sel_run:
            sel_run["month_name"] = MONTHS[int(sel_run["fy_month"])]
            payslips = _rows(db, "SELECT * FROM hr_payslips WHERE run_id=:i ORDER BY employee_name", {"i": run_id})

    now = date.today()
    return render(request, "hr/payroll.html", {
        "runs": runs, "payslips": payslips, "sel_run": sel_run,
        "cur_year": now.year, "cur_month": now.month, "months": MONTHS,
        "page_title": "Payroll",
    })


@router.post("/payroll/run")
async def payroll_run(request: Request, fy_year: int = Form(...), fy_month: int = Form(...),
                      db: Session = Depends(get_db)):
    require_action(request, "hr", "create")
    ensure_schema(db)
    cid = _cid(request)
    y, m = int(fy_year), int(fy_month)

    # working days in the month (Mon-Sat = working; Fri off is common in region —
    # keep it simple: exclude Fridays as the weekly off).
    days_in_month = calendar.monthrange(y, m)[1]
    working_days = 0
    for d in range(1, days_in_month + 1):
        # weekday(): Mon=0 .. Sun=6 ; treat Friday(4) as the weekly off
        if date(y, m, d).weekday() != 4:
            working_days += 1

    # create/replace the run
    db.execute(text("""
        INSERT INTO hr_payroll_runs (company_id, fy_year, fy_month, status, run_by, run_at)
        VALUES (:c, :y, :m, 'Draft', :u, :now)
        ON DUPLICATE KEY UPDATE run_by=:u, run_at=:now, status='Draft'
    """), {"c": cid, "y": y, "m": m, "u": _user(request), "now": datetime.utcnow()})
    db.commit()
    run = _one(db, "SELECT id FROM hr_payroll_runs WHERE company_id=:c AND fy_year=:y AND fy_month=:m",
               {"c": cid, "y": y, "m": m})
    run_id = run["id"]
    db.execute(text("DELETE FROM hr_payslips WHERE run_id=:i"), {"i": run_id})

    emps = _rows(db, """
        SELECT employee_code, full_name, COALESCE(basic_salary,0) AS basic,
               COALESCE(allowances,0) AS allowances, COALESCE(deductions,0) AS deductions
        FROM hr_employees WHERE status='Active' AND (company_id=:c OR company_id IS NULL)
    """, {"c": cid})

    mm = f"{m:02d}"
    for e in emps:
        # paid days = present + paid leave (approved Annual/Sick), capped at working days
        present = db.execute(text("""
            SELECT COUNT(*) FROM hr_attendance
            WHERE employee_code=:e AND status='Present'
              AND att_date >= :d1 AND att_date <= :d2
        """), {"e": e["employee_code"], "d1": f"{y}-{mm}-01", "d2": f"{y}-{mm}-{days_in_month:02d}"}).scalar() or 0
        paid_leave = db.execute(text("""
            SELECT COALESCE(SUM(days),0) FROM hr_leave_requests
            WHERE employee_code=:e AND status='Approved' AND leave_type IN ('Annual','Sick')
              AND YEAR(date_from)=:y AND MONTH(date_from)=:m
        """), {"e": e["employee_code"], "y": y, "m": m}).scalar() or 0
        paid_days = min(working_days, int(present) + int(paid_leave))
        # if no attendance captured at all, assume full month (common fallback)
        if int(present) == 0 and int(paid_leave) == 0:
            paid_days = working_days

        ratio = (paid_days / working_days) if working_days else 0
        basic = round(float(e["basic"]) * ratio, 2)
        allow = round(float(e["allowances"]) * ratio, 2)
        ded = float(e["deductions"])
        gross = round(basic + allow, 2)
        net = round(gross - ded, 2)

        db.execute(text("""
            INSERT INTO hr_payslips (run_id, employee_code, employee_name, basic, allowances,
                                     deductions, working_days, paid_days, gross, net)
            VALUES (:r, :e, :n, :b, :a, :d, :wd, :pd, :g, :net)
        """), {"r": run_id, "e": e["employee_code"], "n": e["full_name"], "b": basic,
               "a": allow, "d": ded, "wd": working_days, "pd": paid_days, "g": gross, "net": net})
    db.commit()
    return RedirectResponse(f"/hr/payroll?run_id={run_id}&toast=success&title=Payroll generated&msg={MONTHS[m]} {y}: {len(emps)} payslips",
                            status_code=303)


@router.post("/payroll/{run_id}/finalize")
async def payroll_finalize(request: Request, run_id: int, db: Session = Depends(get_db)):
    require_action(request, "hr", "edit")
    ensure_schema(db)
    db.execute(text("UPDATE hr_payroll_runs SET status='Finalized' WHERE id=:i"), {"i": run_id})
    db.commit()
    return RedirectResponse(f"/hr/payroll?run_id={run_id}&toast=success&title=Finalized&msg=Payroll run finalized", status_code=303)


@router.post("/employees/{emp_code}/salary")
async def set_salary(request: Request, emp_code: str,
                     basic_salary: float = Form(0), allowances: float = Form(0),
                     deductions: float = Form(0), annual_leave_days: int = Form(21),
                     db: Session = Depends(get_db)):
    require_action(request, "hr", "edit")
    ensure_schema(db)
    db.execute(text("""
        UPDATE hr_employees SET basic_salary=:b, allowances=:a, deductions=:d, annual_leave_days=:l
        WHERE employee_code=:e
    """), {"b": basic_salary, "a": allowances, "d": deductions, "l": annual_leave_days, "e": emp_code})
    db.commit()
    return RedirectResponse("/hr/payroll?toast=success&title=Saved&msg=Salary updated for " + emp_code, status_code=303)
