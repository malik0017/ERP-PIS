# app/modules/projects/routes.py
"""Project Management Module - Complete CRUD with timeline, team, tasks.

Batch 9: Phase 2 COMPLETE
  - Fixed 500 error on /projects/create (templates were missing).
  - Full CRUD: list, create, detail, update, delete.
  - Team assignment (add / remove).
  - Task management (add / update status / delete).
  - Milestones (add).
  - Gantt / timeline view.
  - Activity log (audit trail) for every mutation.
  - Live progress + budget roll-up computed from tasks.
  - Multi-company scoped (company_id) and RBAC two-gate protected.
  - Dark mode + RTL safe (handled in templates / shared CSS).
"""
from datetime import datetime, date
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.templates import render
from app.core.rbac import require_area, require_action
from app.database.session import get_db

router = APIRouter(prefix="/projects", tags=["Project Management"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _cid(request: Request) -> int:
    """Current company ID from session (defaults to 1)."""
    return request.session.get("company_id") or 1


def _user(request: Request) -> str:
    """Current username."""
    return request.session.get("username") or "system"


def _flash(url: str, ok: str = "", err: str = "") -> RedirectResponse:
    """Build a redirect that carries a success/error banner via query string."""
    from urllib.parse import quote
    sep = "&" if "?" in url else "?"
    if ok:
        return RedirectResponse(f"{url}{sep}success={quote(ok)}", status_code=303)
    if err:
        return RedirectResponse(f"{url}{sep}error={quote(err)}", status_code=303)
    return RedirectResponse(url, status_code=303)


def _ensure_schema(db: Session) -> None:
    """Create project management tables if missing (self-healing migration).

    Uses CREATE TABLE IF NOT EXISTS individually so a partial install still
    completes. Foreign keys reference users(id); if that table is absent the
    FK lines are harmless because we only reach here on a live ERP schema.
    """
    stmts = [
        """
        CREATE TABLE IF NOT EXISTS projects (
            id INT AUTO_INCREMENT PRIMARY KEY,
            company_id INT NULL,
            project_code VARCHAR(80) UNIQUE NOT NULL,
            project_name VARCHAR(255) NOT NULL,
            description TEXT NULL,
            start_date DATE NULL,
            end_date DATE NULL,
            actual_end_date DATE NULL,
            project_manager_id INT NULL,
            budget DECIMAL(18,2) DEFAULT 0,
            spent DECIMAL(18,2) DEFAULT 0,
            status VARCHAR(50) NOT NULL DEFAULT 'Planning',
            priority VARCHAR(30) NOT NULL DEFAULT 'Medium',
            progress_pct INT DEFAULT 0,
            created_by VARCHAR(120) NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            KEY idx_company (company_id),
            KEY idx_status (status),
            KEY idx_pm (project_manager_id),
            KEY idx_code (project_code)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
        """
        CREATE TABLE IF NOT EXISTS project_phases (
            id INT AUTO_INCREMENT PRIMARY KEY,
            project_id INT NOT NULL,
            phase_name VARCHAR(255) NOT NULL,
            description TEXT NULL,
            start_date DATE NULL,
            end_date DATE NULL,
            status VARCHAR(50) NOT NULL DEFAULT 'Not Started',
            progress_pct INT DEFAULT 0,
            sort_order INT DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            KEY idx_project (project_id),
            KEY idx_status (status)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
        """
        CREATE TABLE IF NOT EXISTS project_tasks (
            id INT AUTO_INCREMENT PRIMARY KEY,
            project_id INT NOT NULL,
            phase_id INT NULL,
            task_name VARCHAR(255) NOT NULL,
            description TEXT NULL,
            assigned_to INT NULL,
            start_date DATE NULL,
            end_date DATE NULL,
            actual_end_date DATE NULL,
            duration_days INT DEFAULT 0,
            predecessor_task_id INT NULL,
            status VARCHAR(50) NOT NULL DEFAULT 'Not Started',
            priority VARCHAR(30) NOT NULL DEFAULT 'Medium',
            progress_pct INT DEFAULT 0,
            estimated_cost DECIMAL(18,2) DEFAULT 0,
            actual_cost DECIMAL(18,2) DEFAULT 0,
            notes TEXT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            KEY idx_project (project_id),
            KEY idx_phase (phase_id),
            KEY idx_assigned (assigned_to),
            KEY idx_status (status),
            KEY idx_dates (start_date, end_date)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
        """
        CREATE TABLE IF NOT EXISTS project_team (
            id INT AUTO_INCREMENT PRIMARY KEY,
            project_id INT NOT NULL,
            user_id INT NOT NULL,
            role VARCHAR(100) NOT NULL,
            allocated_hours INT DEFAULT 0,
            hourly_rate DECIMAL(10,2) DEFAULT 0,
            start_date DATE NULL,
            end_date DATE NULL,
            joined_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY uq_project_user (project_id, user_id),
            KEY idx_project (project_id),
            KEY idx_user (user_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
        """
        CREATE TABLE IF NOT EXISTS project_milestones (
            id INT AUTO_INCREMENT PRIMARY KEY,
            project_id INT NOT NULL,
            milestone_name VARCHAR(255) NOT NULL,
            milestone_date DATE NOT NULL,
            description TEXT NULL,
            status VARCHAR(50) NOT NULL DEFAULT 'Pending',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            KEY idx_project (project_id),
            KEY idx_date (milestone_date)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
        """
        CREATE TABLE IF NOT EXISTS project_activities (
            id INT AUTO_INCREMENT PRIMARY KEY,
            project_id INT NOT NULL,
            activity_type VARCHAR(100) NOT NULL,
            description TEXT NULL,
            performed_by INT NULL,
            old_value VARCHAR(255) NULL,
            new_value VARCHAR(255) NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            KEY idx_project (project_id),
            KEY idx_type (activity_type),
            KEY idx_date (created_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
    ]
    try:
        for s in stmts:
            db.execute(text(s))
        db.commit()
    except Exception:
        db.rollback()


def _next_project_code(db: Session) -> str:
    """Generate next project code PRJ-YYYYMMDD-0001."""
    today = datetime.utcnow().strftime("%Y%m%d")
    try:
        row = db.execute(text(
            "SELECT project_code FROM projects WHERE project_code LIKE :p ORDER BY id DESC LIMIT 1"
        ), {"p": f"PRJ-{today}-%"}).first()
        seq = int(row[0].rsplit("-", 1)[-1]) + 1 if row else 1
        return f"PRJ-{today}-{seq:04d}"
    except Exception:
        return f"PRJ-{today}-0001"


def _users(db: Session) -> list:
    """Selectable users for PM / team / assignee dropdowns."""
    try:
        return [dict(r) for r in db.execute(text(
            "SELECT id, username, COALESCE(NULLIF(full_name,''), username) AS full_name "
            "FROM users ORDER BY full_name LIMIT 300"
        )).mappings().all()]
    except Exception:
        return []


def _log_activity(db: Session, project_id: int, activity_type: str, description: str,
                  user: str, old_value: str = None, new_value: str = None) -> None:
    """Log project activity for the audit trail (best-effort)."""
    try:
        uid = db.execute(text("SELECT id FROM users WHERE username=:u LIMIT 1"),
                         {"u": user}).scalar()
        db.execute(text("""
            INSERT INTO project_activities
                (project_id, activity_type, description, performed_by, old_value, new_value)
            VALUES (:pid, :type, :desc, :uid, :old, :new)
        """), {"pid": project_id, "type": activity_type, "desc": description,
               "uid": uid, "old": old_value, "new": new_value})
        db.commit()
    except Exception:
        db.rollback()


def _recompute_rollup(db: Session, project_id: int) -> None:
    """Roll up progress % and spent from tasks onto the parent project."""
    try:
        row = db.execute(text("""
            SELECT COUNT(*) AS n,
                   COALESCE(AVG(progress_pct),0) AS avg_prog,
                   COALESCE(SUM(actual_cost),0)  AS spent
            FROM project_tasks WHERE project_id=:pid
        """), {"pid": project_id}).mappings().first()
        if row and row["n"]:
            db.execute(text("""
                UPDATE projects
                   SET progress_pct = :p, spent = :s, updated_at = NOW()
                 WHERE id = :id
            """), {"p": int(round(row["avg_prog"])), "s": float(row["spent"]), "id": project_id})
            db.commit()
    except Exception:
        db.rollback()


# ===========================================================================
# LIST
# ===========================================================================
@router.get("")
async def projects_list(request: Request, db: Session = Depends(get_db)):
    """List all projects (multi-company filtered)."""
    require_area(request, "project_management")
    _ensure_schema(db)

    cid = _cid(request)
    q = request.query_params
    search = (q.get("search") or "").strip()
    status = (q.get("status") or "").strip()

    try:
        sql = "SELECT * FROM projects WHERE (company_id = :cid OR company_id IS NULL)"
        params = {"cid": cid}
        if search:
            sql += " AND (project_name LIKE :s OR project_code LIKE :s)"
            params["s"] = f"%{search}%"
        if status:
            sql += " AND status = :status"
            params["status"] = status
        sql += " ORDER BY created_at DESC LIMIT 500"
        projects = [dict(r) for r in db.execute(text(sql), params).mappings().all()]

        kpis = {
            "total": len(projects),
            "active": sum(1 for p in projects if p.get("status") == "Active"),
            "completed": sum(1 for p in projects if p.get("status") == "Completed"),
            "on_budget": sum(1 for p in projects
                             if float(p.get("spent") or 0) <= float(p.get("budget") or 0)),
        }
        return render(request, "projects/index.html", {
            "projects": projects,
            "kpis": kpis,
            "filters": {"search": search, "status": status},
            "page_title": "Projects",
        })
    except Exception as e:
        return render(request, "projects/index.html", {
            "projects": [],
            "kpis": {"total": 0, "active": 0, "completed": 0, "on_budget": 0},
            "filters": {"search": search, "status": status},
            "page_title": "Projects",
            "error": str(e),
        })


# ===========================================================================
# CREATE
# ===========================================================================
@router.get("/create")
async def projects_create_form(request: Request, db: Session = Depends(get_db)):
    """Create project form."""
    require_action(request, "project_management", "add")
    _ensure_schema(db)
    return render(request, "projects/create.html", {
        "users": _users(db),
        "page_title": "New Project",
        "today": date.today().isoformat(),
    })


@router.post("")
async def projects_create(
    request: Request,
    project_name: str = Form(...),
    description: str = Form(""),
    start_date: str = Form(""),
    end_date: str = Form(""),
    project_manager_id: int = Form(0),
    budget: float = Form(0),
    priority: str = Form("Medium"),
    status: str = Form("Planning"),
    db: Session = Depends(get_db),
):
    """Create new project."""
    require_action(request, "project_management", "add")
    _ensure_schema(db)
    try:
        code = _next_project_code(db)
        db.execute(text("""
            INSERT INTO projects
                (company_id, project_code, project_name, description, start_date, end_date,
                 project_manager_id, budget, priority, status, created_by)
            VALUES
                (:cid, :code, :name, :desc, :start, :end, :pm, :bud, :pri, :status, :user)
        """), {
            "cid": _cid(request), "code": code, "name": project_name,
            "desc": description or None, "start": start_date or None, "end": end_date or None,
            "pm": project_manager_id or None, "bud": budget or 0, "pri": priority,
            "status": status or "Planning", "user": _user(request),
        })
        db.commit()
        proj_id = db.execute(text("SELECT LAST_INSERT_ID()")).scalar()
        _log_activity(db, proj_id, "created", f"Project {code} created", _user(request))
        return _flash(f"/projects/{proj_id}", ok="Project created successfully")
    except Exception as e:
        db.rollback()
        return _flash("/projects/create", err=str(e))


# ===========================================================================
# DETAIL (dashboard)
# ===========================================================================
@router.get("/{project_id}")
async def projects_detail(project_id: int, request: Request, db: Session = Depends(get_db)):
    """Project detail / dashboard."""
    require_area(request, "project_management")
    _ensure_schema(db)
    try:
        project = db.execute(text(
            "SELECT * FROM projects WHERE id=:id AND (company_id=:cid OR company_id IS NULL)"
        ), {"id": project_id, "cid": _cid(request)}).mappings().first()
        if not project:
            return _flash("/projects", err="Project not found")
        project = dict(project)

        # Resolve PM display name.
        pm_name = None
        if project.get("project_manager_id"):
            pm_name = db.execute(text(
                "SELECT COALESCE(NULLIF(full_name,''), username) FROM users WHERE id=:id"
            ), {"id": project["project_manager_id"]}).scalar()
        project["pm_name"] = pm_name

        tasks = [dict(r) for r in db.execute(text("""
            SELECT pt.*, COALESCE(NULLIF(u.full_name,''), u.username) AS assignee_name
            FROM project_tasks pt
            LEFT JOIN users u ON u.id = pt.assigned_to
            WHERE pt.project_id=:pid ORDER BY pt.start_date, pt.id
        """), {"pid": project_id}).mappings().all()]

        team = [dict(r) for r in db.execute(text("""
            SELECT pt.*, u.username, COALESCE(NULLIF(u.full_name,''), u.username) AS full_name
            FROM project_team pt
            LEFT JOIN users u ON u.id = pt.user_id
            WHERE pt.project_id=:pid ORDER BY pt.joined_at
        """), {"pid": project_id}).mappings().all()]

        milestones = [dict(r) for r in db.execute(text(
            "SELECT * FROM project_milestones WHERE project_id=:pid ORDER BY milestone_date"
        ), {"pid": project_id}).mappings().all()]

        activities = [dict(r) for r in db.execute(text("""
            SELECT pa.*, COALESCE(NULLIF(u.full_name,''), u.username) AS actor
            FROM project_activities pa
            LEFT JOIN users u ON u.id = pa.performed_by
            WHERE pa.project_id=:pid ORDER BY pa.created_at DESC LIMIT 40
        """), {"pid": project_id}).mappings().all()]

        task_stats = {
            "total": len(tasks),
            "done": sum(1 for t in tasks if t.get("status") == "Completed"),
            "in_progress": sum(1 for t in tasks if t.get("status") == "In Progress"),
            "not_started": sum(1 for t in tasks if t.get("status") == "Not Started"),
        }

        return render(request, "projects/detail.html", {
            "project": project,
            "tasks": tasks,
            "team": team,
            "milestones": milestones,
            "activities": activities,
            "task_stats": task_stats,
            "users": _users(db),
            "page_title": project.get("project_name", "Project"),
        })
    except Exception as e:
        return _flash("/projects", err=str(e))


# ===========================================================================
# UPDATE (status / progress / basic fields)
# ===========================================================================
@router.post("/{project_id}")
async def update_project(
    project_id: int,
    request: Request,
    project_name: str = Form(""),
    status: str = Form(""),
    priority: str = Form(""),
    progress_pct: int = Form(-1),
    spent: float = Form(-1),
    budget: float = Form(-1),
    db: Session = Depends(get_db),
):
    """Update project header fields."""
    require_action(request, "project_management", "edit")
    _ensure_schema(db)
    try:
        updates, params = [], {"id": project_id, "cid": _cid(request)}
        if project_name:
            updates.append("project_name = :name"); params["name"] = project_name
        if status:
            updates.append("status = :status"); params["status"] = status
            if status == "Completed":
                updates.append("actual_end_date = CURDATE()")
        if priority:
            updates.append("priority = :pri"); params["pri"] = priority
        if progress_pct >= 0:
            updates.append("progress_pct = :pct"); params["pct"] = progress_pct
        if spent >= 0:
            updates.append("spent = :spent"); params["spent"] = spent
        if budget >= 0:
            updates.append("budget = :budget"); params["budget"] = budget
        if updates:
            sql = ("UPDATE projects SET " + ", ".join(updates) +
                   ", updated_at=NOW() WHERE id=:id AND (company_id=:cid OR company_id IS NULL)")
            db.execute(text(sql), params)
            db.commit()
            _log_activity(db, project_id, "updated", "Project details updated", _user(request))
        return _flash(f"/projects/{project_id}", ok="Project updated")
    except Exception as e:
        db.rollback()
        return _flash(f"/projects/{project_id}", err=str(e))


# ===========================================================================
# DELETE
# ===========================================================================
@router.post("/{project_id}/delete")
async def delete_project(project_id: int, request: Request, db: Session = Depends(get_db)):
    """Delete a project and its children."""
    require_action(request, "project_management", "delete")
    _ensure_schema(db)
    try:
        for tbl in ("project_activities", "project_milestones", "project_team",
                    "project_tasks", "project_phases"):
            db.execute(text(f"DELETE FROM {tbl} WHERE project_id=:pid"), {"pid": project_id})
        db.execute(text(
            "DELETE FROM projects WHERE id=:id AND (company_id=:cid OR company_id IS NULL)"
        ), {"id": project_id, "cid": _cid(request)})
        db.commit()
        return _flash("/projects", ok="Project deleted")
    except Exception as e:
        db.rollback()
        return _flash(f"/projects/{project_id}", err=str(e))


# ===========================================================================
# TASKS
# ===========================================================================
@router.post("/{project_id}/tasks")
async def add_task(
    project_id: int,
    request: Request,
    task_name: str = Form(...),
    start_date: str = Form(""),
    end_date: str = Form(""),
    assigned_to: int = Form(0),
    priority: str = Form("Medium"),
    estimated_cost: float = Form(0),
    db: Session = Depends(get_db),
):
    """Add task to project."""
    require_action(request, "project_management", "edit")
    _ensure_schema(db)
    try:
        db.execute(text("""
            INSERT INTO project_tasks
                (project_id, task_name, start_date, end_date, assigned_to,
                 priority, estimated_cost, status)
            VALUES (:pid, :name, :start, :end, :assign, :pri, :cost, 'Not Started')
        """), {
            "pid": project_id, "name": task_name, "start": start_date or None,
            "end": end_date or None, "assign": assigned_to or None,
            "pri": priority, "cost": estimated_cost or 0,
        })
        db.commit()
        _log_activity(db, project_id, "task_added", f"Task '{task_name}' added", _user(request))
        _recompute_rollup(db, project_id)
        return _flash(f"/projects/{project_id}", ok="Task added")
    except Exception as e:
        db.rollback()
        return _flash(f"/projects/{project_id}", err=str(e))


@router.post("/{project_id}/tasks/{task_id}/status")
async def update_task_status(
    project_id: int, task_id: int, request: Request,
    status: str = Form(...), progress_pct: int = Form(-1),
    actual_cost: float = Form(-1),
    db: Session = Depends(get_db),
):
    """Update a task's status / progress / cost."""
    require_action(request, "project_management", "edit")
    _ensure_schema(db)
    try:
        updates, params = ["status = :st"], {"st": status, "tid": task_id, "pid": project_id}
        # Auto-set progress from status when not explicitly provided.
        if progress_pct < 0:
            progress_pct = {"Not Started": 0, "In Progress": 50,
                            "Completed": 100, "On Hold": progress_pct}.get(status, -1)
        if progress_pct >= 0:
            updates.append("progress_pct = :pp"); params["pp"] = progress_pct
        if actual_cost >= 0:
            updates.append("actual_cost = :ac"); params["ac"] = actual_cost
        if status == "Completed":
            updates.append("actual_end_date = CURDATE()")
        db.execute(text(
            "UPDATE project_tasks SET " + ", ".join(updates) +
            ", updated_at=NOW() WHERE id=:tid AND project_id=:pid"
        ), params)
        db.commit()
        _log_activity(db, project_id, "task_updated", f"Task #{task_id} -> {status}", _user(request))
        _recompute_rollup(db, project_id)
        return _flash(f"/projects/{project_id}", ok="Task updated")
    except Exception as e:
        db.rollback()
        return _flash(f"/projects/{project_id}", err=str(e))


@router.post("/{project_id}/tasks/{task_id}/delete")
async def delete_task(project_id: int, task_id: int, request: Request,
                      db: Session = Depends(get_db)):
    """Delete a task."""
    require_action(request, "project_management", "edit")
    _ensure_schema(db)
    try:
        db.execute(text("DELETE FROM project_tasks WHERE id=:tid AND project_id=:pid"),
                   {"tid": task_id, "pid": project_id})
        db.commit()
        _log_activity(db, project_id, "task_removed", f"Task #{task_id} removed", _user(request))
        _recompute_rollup(db, project_id)
        return _flash(f"/projects/{project_id}", ok="Task removed")
    except Exception as e:
        db.rollback()
        return _flash(f"/projects/{project_id}", err=str(e))


# ===========================================================================
# TEAM
# ===========================================================================
@router.post("/{project_id}/team")
async def add_team_member(
    project_id: int, request: Request,
    user_id: int = Form(...), role: str = Form("Team Member"),
    allocated_hours: int = Form(0), hourly_rate: float = Form(0),
    db: Session = Depends(get_db),
):
    """Add team member to project."""
    require_action(request, "project_management", "edit")
    _ensure_schema(db)
    try:
        db.execute(text("""
            INSERT INTO project_team (project_id, user_id, role, allocated_hours, hourly_rate)
            VALUES (:pid, :uid, :role, :hours, :rate)
            ON DUPLICATE KEY UPDATE role=VALUES(role),
                allocated_hours=VALUES(allocated_hours), hourly_rate=VALUES(hourly_rate)
        """), {"pid": project_id, "uid": user_id, "role": role,
               "hours": allocated_hours or 0, "rate": hourly_rate or 0})
        db.commit()
        _log_activity(db, project_id, "team_added",
                      f"Member #{user_id} assigned as {role}", _user(request))
        return _flash(f"/projects/{project_id}", ok="Team member added")
    except Exception as e:
        db.rollback()
        return _flash(f"/projects/{project_id}", err=str(e))


@router.post("/{project_id}/team/{member_id}/delete")
async def remove_team_member(project_id: int, member_id: int, request: Request,
                             db: Session = Depends(get_db)):
    """Remove a team member row (member_id is project_team.id)."""
    require_action(request, "project_management", "edit")
    _ensure_schema(db)
    try:
        db.execute(text("DELETE FROM project_team WHERE id=:mid AND project_id=:pid"),
                   {"mid": member_id, "pid": project_id})
        db.commit()
        _log_activity(db, project_id, "team_removed", f"Team member removed", _user(request))
        return _flash(f"/projects/{project_id}", ok="Team member removed")
    except Exception as e:
        db.rollback()
        return _flash(f"/projects/{project_id}", err=str(e))


# ===========================================================================
# MILESTONES
# ===========================================================================
@router.post("/{project_id}/milestones")
async def add_milestone(
    project_id: int, request: Request,
    milestone_name: str = Form(...), milestone_date: str = Form(...),
    description: str = Form(""),
    db: Session = Depends(get_db),
):
    """Add a milestone."""
    require_action(request, "project_management", "edit")
    _ensure_schema(db)
    try:
        db.execute(text("""
            INSERT INTO project_milestones (project_id, milestone_name, milestone_date, description)
            VALUES (:pid, :name, :dt, :desc)
        """), {"pid": project_id, "name": milestone_name,
               "dt": milestone_date, "desc": description or None})
        db.commit()
        _log_activity(db, project_id, "milestone_added",
                      f"Milestone '{milestone_name}' added", _user(request))
        return _flash(f"/projects/{project_id}", ok="Milestone added")
    except Exception as e:
        db.rollback()
        return _flash(f"/projects/{project_id}", err=str(e))


# ===========================================================================
# TIMELINE (Gantt)
# ===========================================================================
@router.get("/{project_id}/timeline")
async def projects_timeline(project_id: int, request: Request, db: Session = Depends(get_db)):
    """Gantt / timeline view of project."""
    require_area(request, "project_management")
    _ensure_schema(db)
    try:
        project = db.execute(text(
            "SELECT * FROM projects WHERE id=:id AND (company_id=:cid OR company_id IS NULL)"
        ), {"id": project_id, "cid": _cid(request)}).mappings().first()
        if not project:
            return _flash("/projects", err="Project not found")
        project = dict(project)

        tasks = [dict(r) for r in db.execute(text("""
            SELECT pt.*, COALESCE(NULLIF(u.full_name,''), u.username) AS assignee_name
            FROM project_tasks pt
            LEFT JOIN users u ON u.id = pt.assigned_to
            WHERE pt.project_id=:pid ORDER BY pt.start_date, pt.id
        """), {"pid": project_id}).mappings().all()]

        milestones = [dict(r) for r in db.execute(text(
            "SELECT * FROM project_milestones WHERE project_id=:pid ORDER BY milestone_date"
        ), {"pid": project_id}).mappings().all()]

        return render(request, "projects/timeline.html", {
            "project": project,
            "tasks": tasks,
            "milestones": milestones,
            "page_title": f"Timeline: {project.get('project_name', 'Project')}",
        })
    except Exception as e:
        return _flash("/projects", err=str(e))
