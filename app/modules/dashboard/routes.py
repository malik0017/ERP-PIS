from fastapi.responses import RedirectResponse
from fastapi import APIRouter, Depends, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.templates import render
from app.core.rbac import require_area
from app.database.session import get_db
from app.core.db_read import log_failure as db_read_log

router = APIRouter(tags=["dashboard"])


def _one(db: Session, sql: str, params: dict | None = None):
    try:
        return db.execute(text(sql), params or {}).scalar() or 0
    except Exception as _exc:
        db_read_log(_exc, sql, 'routes.py._one')
        return 0


def _rows(db: Session, sql: str, params: dict | None = None):
    try:
        return [dict(r) for r in db.execute(text(sql), params or {}).mappings().all()]
    except Exception as _exc:
        db_read_log(_exc, sql, 'routes.py._rows')
        return []


def _pct(value: float, base: float) -> float:
    try:
        return round((float(value or 0) / float(base or 0)) * 100, 2) if float(base or 0) else 0
    except Exception:
        return 0


@router.get("/dashboard", name="dashboard")
async def dashboard(request: Request, db: Session = Depends(get_db)):
  
    require_area(request, "dashboard")
    from datetime import datetime as _dt
    from app.services import command_center as cc

    cid = int(request.session.get("company_id") or 1)
    f = cc.parse_filters(request.query_params)
    cards, periods = cc.kpi_cards(db, f, cid)
    mode = (request.query_params.get("mode") or "").strip()
    company_name = request.session.get("company_name")
    if not company_name:
        try:
            company_name = db.execute(text("SELECT name FROM companies WHERE id = :i"), {"i": cid}).scalar()
        except Exception:
            db.rollback()
    return render(request, "dashboard/command_center.html", {
        "company_name": company_name or f"Company #{cid}",
        "username": request.session.get("username", "Guest"),
        "user_role": request.session.get("user_role", "UNKNOWN"),
        "page_title": "Production Command Center - ISFC PIMS",
        "filters": f,
        "options": cc.picker_options(db, cid),
        "cards": cards,
        "periods": periods,
        "charts": cc.charts(db, f, cid),
        "attention": cc.attention(db, f, cid),
        "batches": cc.batch_table(db, f, cid),
        "mode": mode if mode in ("presentation", "wallboard") else "",
        "generated_at": _dt.now().strftime("%Y-%m-%d %H:%M:%S"),
    })


@router.get("/dashboard/order/{order_no}/360")
async def order_360_json(request: Request, order_no: str, db: Session = Depends(get_db)):
   
    require_area(request, "dashboard")
    from fastapi.responses import JSONResponse

    from app.core.company import require_order_scope
    from app.services import order_360

    require_order_scope(db, request, order_no)
    cid = int(request.session.get("company_id") or 1)
    data = order_360.build(db, order_no, cid)
    if not data:
        return JSONResponse({"error": "Order not found"}, status_code=404)
    return JSONResponse(data)


@router.get("/dashboard/kpi/{key}")
async def dashboard_kpi_drill(request: Request, key: str, db: Session = Depends(get_db)):
    """Batch 203 (Image 16) — side-panel evidence for one KPI card, same filter."""
    require_area(request, "dashboard")
    from fastapi.responses import JSONResponse
    from app.services import command_center as cc

    cid = int(request.session.get("company_id") or 1)
    f = cc.parse_filters(request.query_params)
    return JSONResponse(cc.drill(db, f, cid, key))


@router.get("/production", name="production_home")
async def production(request: Request):
   
    require_area(request, "dashboard")
    return RedirectResponse("/production/head-chef", status_code=303)
