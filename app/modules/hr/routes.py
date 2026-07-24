# app/modules/hr/routes.py — Batch 18: HCM (employees + attendance)
"""Human Capital Management.

Phase 1 (this batch): employee master (code, EN/AR names, section, position,
optional linked system user) and daily attendance (Present / Absent / Leave /
Sick, with check-in/out). Payroll feeding Finance GL is the next HCM phase.
Tables auto-create on first visit; every statement is tolerant so a partially
migrated DB never 500s.
"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.templates import render
from app.core.rbac import require_area, require_action
from app.database.session import get_db

router = APIRouter(prefix="/hr", tags=["HCM"])

SECTIONS = ["Cutting", "Butchery", "Hot Kitchen", "Cold Kitchen", "Bakery/Pastry",
            "QC", "Trayline / Packing", "Store", "Dispatch", "Admin/Office"]
ATT_STATUSES = ["Present", "Absent", "Leave", "Sick"]


def _ensure_hr_schema(db: Session) -> None:
    def _try(sql):
        try:
            db.execute(text(sql))
        except Exception:
            db.rollback()
    _try("""
        CREATE TABLE IF NOT EXISTS hr_employees (
            id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
            company_id INT NULL,
            employee_code VARCHAR(30) NOT NULL UNIQUE,
            full_name VARCHAR(255) NOT NULL,
            full_name_ar VARCHAR(255) NULL,
            section VARCHAR(80) NULL,
            position VARCHAR(120) NULL,
            phone VARCHAR(40) NULL,
            hire_date DATE NULL,
            linked_username VARCHAR(120) NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'Active',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)
    _try("""
        CREATE TABLE IF NOT EXISTS hr_attendance (
            id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
            company_id INT NULL,
            employee_code VARCHAR(30) NOT NULL,
            att_date DATE NOT NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'Present',
            check_in VARCHAR(8) NULL,
            check_out VARCHAR(8) NULL,
            remarks VARCHAR(300) NULL,
            marked_by VARCHAR(120) NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY uq_att (employee_code, att_date)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)
    try:
        db.commit()
    except Exception:
        db.rollback()


def _rows(db: Session, sql: str, params: dict | None = None) -> list[dict]:
    try:
        return [dict(r) for r in db.execute(text(sql), params or {}).mappings().all()]
    except Exception:
        return []


def _next_emp_code(db: Session) -> str:
    n = 0
    try:
        n = int(db.execute(text("SELECT COUNT(*) FROM hr_employees")).scalar() or 0)
    except Exception:
        pass
    return f"EMP-{n + 1:04d}"


# ---------------------------------------------------------------------------
# Employees
# ---------------------------------------------------------------------------
@router.get("/employees")
def employees(request: Request, db: Session = Depends(get_db)):
    require_area(request, "hr")
    _ensure_hr_schema(db)
    q = (request.query_params.get("q") or "").strip()
    section = (request.query_params.get("section") or "").strip()
    where, params = ["1=1"], {}
    if q:
        where.append("(employee_code LIKE :q OR full_name LIKE :q OR COALESCE(full_name_ar,'') LIKE :q)")
        params["q"] = f"%{q}%"
    if section:
        where.append("COALESCE(section,'') = :sec")
        params["sec"] = section

    rows = _rows(db, f"""
        SELECT id, employee_code, full_name, COALESCE(full_name_ar,'') AS full_name_ar,
               COALESCE(section,'') AS section, COALESCE(position,'') AS position,
               COALESCE(phone,'') AS phone, COALESCE(hire_date,'') AS hire_date,
               COALESCE(linked_username,'') AS linked_username, status
        FROM hr_employees WHERE {' AND '.join(where)}
        ORDER BY employee_code LIMIT 500
    """, params)
    today = date.today().isoformat()
    kpis = {
        "total": len(_rows(db, "SELECT id FROM hr_employees")),
        "active": len(_rows(db, "SELECT id FROM hr_employees WHERE status='Active'")),
        "present_today": len(_rows(db, "SELECT id FROM hr_attendance WHERE att_date=:d AND status='Present'", {"d": today})),
        "absent_today": len(_rows(db, "SELECT id FROM hr_attendance WHERE att_date=:d AND status IN ('Absent','Sick','Leave')", {"d": today})),
    }
    users = _rows(db, "SELECT username FROM users ORDER BY username LIMIT 500")
    return render(request, "hr/employees.html", {
        "rows": rows, "kpis": kpis, "sections": SECTIONS, "users": users,
        "filters": {"q": q, "section": section},
        "next_code": _next_emp_code(db),
        "page_title": "Employees",
    })


@router.post("/employees/create")
def create_employee(
    request: Request,
    employee_code: str = Form(""),
    full_name: str = Form(...),
    full_name_ar: str = Form(""),
    section: str = Form(""),
    position: str = Form(""),
    phone: str = Form(""),
    hire_date: str = Form(""),
    linked_username: str = Form(""),
    db: Session = Depends(get_db),
):
    require_action(request, "hr", "create")
    _ensure_hr_schema(db)
    code = (employee_code or "").strip() or _next_emp_code(db)
    try:
        db.execute(text("""
            INSERT INTO hr_employees (company_id, employee_code, full_name, full_name_ar,
                                      section, position, phone, hire_date, linked_username, status)
            VALUES (:cid, :code, :fn, :fa, :sec, :pos, :ph, :hd, :lu, 'Active')
        """), {"cid": request.session.get("company_id") or 1, "code": code,
               "fn": full_name.strip(), "fa": full_name_ar.strip() or None,
               "sec": section or None, "pos": position or None, "ph": phone or None,
               "hd": hire_date or None, "lu": linked_username or None})
        db.commit()
    except Exception:
        db.rollback()
        return RedirectResponse("/hr/employees?toast=danger&title=Duplicate&msg=Employee code already exists", status_code=303)
    return RedirectResponse(f"/hr/employees?toast=success&title=Employee&msg={code} created", status_code=303)


@router.post("/employees/{emp_id}/update")
def update_employee(
    request: Request, emp_id: int,
    full_name: str = Form(...), full_name_ar: str = Form(""),
    section: str = Form(""), position: str = Form(""),
    phone: str = Form(""), hire_date: str = Form(""),
    linked_username: str = Form(""), status: str = Form("Active"),
    db: Session = Depends(get_db),
):
    require_action(request, "hr", "edit")
    _ensure_hr_schema(db)
    try:
        db.execute(text("""
            UPDATE hr_employees SET full_name=:fn, full_name_ar=:fa, section=:sec,
                   position=:pos, phone=:ph, hire_date=:hd, linked_username=:lu, status=:st
            WHERE id=:id
        """), {"fn": full_name.strip(), "fa": full_name_ar.strip() or None,
               "sec": section or None, "pos": position or None, "ph": phone or None,
               "hd": hire_date or None, "lu": linked_username or None,
               "st": status if status in ("Active", "Inactive") else "Active", "id": emp_id})
        db.commit()
    except Exception:
        db.rollback()
    return RedirectResponse("/hr/employees?toast=success&title=Employee&msg=Saved", status_code=303)


# ---------------------------------------------------------------------------
# Attendance
# ---------------------------------------------------------------------------
@router.get("/attendance")
def attendance(request: Request, db: Session = Depends(get_db)):
    require_area(request, "hr")
    _ensure_hr_schema(db)
    att_date = (request.query_params.get("att_date") or date.today().isoformat()).strip()
    section = (request.query_params.get("section") or "").strip()

    where, params = ["e.status = 'Active'"], {"d": att_date}
    if section:
        where.append("COALESCE(e.section,'') = :sec")
        params["sec"] = section

    rows = _rows(db, f"""
        SELECT e.employee_code, e.full_name, COALESCE(e.section,'') AS section,
               COALESCE(e.position,'') AS position,
               COALESCE(a.status,'') AS att_status,
               COALESCE(a.check_in,'') AS check_in,
               COALESCE(a.check_out,'') AS check_out,
               COALESCE(a.remarks,'') AS remarks
        FROM hr_employees e
        LEFT JOIN hr_attendance a ON a.employee_code = e.employee_code AND a.att_date = :d
        WHERE {' AND '.join(where)}
        ORDER BY e.section, e.employee_code
        LIMIT 500
    """, params)
    marked = [r for r in rows if r["att_status"]]
    kpis = {
        "employees": len(rows),
        "marked": len(marked),
        "present": len([r for r in marked if r["att_status"] == "Present"]),
        "absent": len([r for r in marked if r["att_status"] in ("Absent", "Sick", "Leave")]),
    }
    return render(request, "hr/attendance.html", {
        "rows": rows, "kpis": kpis, "att_date": att_date, "section": section,
        "sections": SECTIONS, "statuses": ATT_STATUSES,
        "page_title": "Attendance",
    })


@router.post("/attendance/mark")
async def mark_attendance(request: Request, db: Session = Depends(get_db)):
    """Upsert one day's attendance for every submitted employee row."""
    require_action(request, "hr", "edit")
    _ensure_hr_schema(db)
    form = await request.form()
    att_date = (form.get("att_date") or date.today().isoformat()).strip()
    codes = form.getlist("employee_code")
    statuses = form.getlist("att_status")
    ins = form.getlist("check_in")
    outs = form.getlist("check_out")
    remarks = form.getlist("remarks")
    saved = 0
    for i, code in enumerate(codes):
        st = statuses[i] if i < len(statuses) else ""
        if not code or st not in ATT_STATUSES:
            continue
        params = {"cid": request.session.get("company_id") or 1, "code": code, "d": att_date,
                  "st": st, "ci": (ins[i] if i < len(ins) else "") or None,
                  "co": (outs[i] if i < len(outs) else "") or None,
                  "rm": (remarks[i] if i < len(remarks) else "") or None,
                  "by": request.session.get("username") or "system"}
        try:
            db.execute(text("""
                UPDATE hr_attendance SET status=:st, check_in=:ci, check_out=:co,
                       remarks=:rm, marked_by=:by
                WHERE employee_code=:code AND att_date=:d
            """), params)
            n = db.execute(text("""
                SELECT COUNT(*) FROM hr_attendance WHERE employee_code=:code AND att_date=:d
            """), params).scalar()
            if not n:
                db.execute(text("""
                    INSERT INTO hr_attendance (company_id, employee_code, att_date, status,
                                               check_in, check_out, remarks, marked_by)
                    VALUES (:cid, :code, :d, :st, :ci, :co, :rm, :by)
                """), params)
            saved += 1
        except Exception:
            db.rollback()
    try:
        db.commit()
    except Exception:
        db.rollback()
    return RedirectResponse(f"/hr/attendance?att_date={att_date}&toast=success&title=Attendance&msg={saved} employees saved", status_code=303)
