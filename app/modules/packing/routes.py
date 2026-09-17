# app/modules/packing/routes.py
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import text
from sqlalchemy.orm import Session
from starlette.status import HTTP_303_SEE_OTHER

from app.core.templates import render
from app.core.rbac import require_area, require_action
from app.database.session import get_db
from app.models.production import CustomerOrder, PackingDispatch
from app.core.company import require_order_scope, require_record_scope

router = APIRouter(prefix="/packing", tags=["Trayline / Packing"])

PACK_REGIONS = ["Riyadh", "Eastern", "Dammam", "Jeddah", "Makkah", "Madinah", "Qassim", "Other"]


def _parse_date(value: Optional[str]):
    try:
        return date.fromisoformat(value) if value else None
    except Exception:
        return None


def _redirect_with_error(url: str, message: str) -> RedirectResponse:
    sep = "&" if "?" in url else "?"
    return RedirectResponse(f"{url}{sep}error={message}", status_code=HTTP_303_SEE_OTHER)


def ensure_schema(db: Session) -> None:
   
    cols = {
        "packed_bags": "INT NULL",
        "rejected_bags": "INT NULL",
        "rejected_bags_reason": "VARCHAR(255) NULL",
    }
    for col, ddl in cols.items():
        try:
            exists = db.execute(text("""
                SELECT COUNT(*) FROM information_schema.columns
                WHERE table_schema = DATABASE() AND table_name = 'packing_dispatch'
                  AND column_name = :c
            """), {"c": col}).scalar()
            if not exists:
                db.execute(text(f"ALTER TABLE packing_dispatch ADD COLUMN {col} {ddl}"))
                db.commit()
        except Exception:
            db.rollback()


def pack_reconciliation(db: Session, order_no: str) -> list[dict]:
  
    def _q(sql: str) -> dict:
        try:
            return {r["recipe_no"]: r for r in db.execute(text(sql), {"o": order_no}).mappings().all()}
        except Exception:
            db.rollback()
            return {}

    bom = _q("""
        SELECT recipe_no, MAX(recipe_name) AS recipe_name,
               SUM(COALESCE(total_required_with_waste_standard, required_qty_standard, 0)) AS required_qty
        FROM bom_lines WHERE order_no = :o GROUP BY recipe_no""")
    issued = _q("""
        SELECT recipe_no,
               SUM(CASE WHEN COALESCE(finalized,0)=1 OR issuance_status IN ('Issued','Short Issued')
                        THEN COALESCE(input_material_issued, issued_qty_standard, 0) ELSE 0 END) AS issued_qty
        FROM store_issuance_lines WHERE order_no = :o GROUP BY recipe_no""")
    qc = _q("""
        SELECT k.recipe_no, MAX(k.recipe_name) AS recipe_name,
               SUM(COALESCE(k.issued_qty_standard,0))   AS transferred_qty,
               SUM(COALESCE(k.received_qty_standard,0)) AS received_qty,
               GROUP_CONCAT(DISTINCT k.from_section ORDER BY k.from_section SEPARATOR ', ') AS from_section,
               MAX(k.standard_uom) AS uom,
               SUM(k.protein_g) AS recv_protein, SUM(k.carb_g) AS recv_carb,
               SUM(k.vegetable_g) AS recv_veg, MAX(k.portion_weight_g) AS portion_weight
        FROM kitchen_section_transactions k
        WHERE k.order_no = :o AND k.current_section = 'QC'
        GROUP BY k.recipe_no""")
    planned = _q("""
        SELECT recipe_no, SUM(COALESCE(required_portions,0)) AS planned
        FROM order_lines WHERE order_no = :o GROUP BY recipe_no""")
    packed = _q("""
        SELECT recipe_no, MAX(packed_portion) AS packed_portion,
               MAX(packed_protein_g) AS protein, MAX(packed_carb_g) AS carb, MAX(packed_veg_g) AS veg
        FROM packing_pack_lines WHERE order_no = :o GROUP BY recipe_no""")

    def f(v):
        return float(v) if v is not None else None

    rows = []
    for rc in sorted(set(bom) | set(qc), key=lambda k: ((qc.get(k) or bom.get(k) or {}).get("recipe_name") or "")):
        b, i, k, p, pk = bom.get(rc, {}), issued.get(rc, {}), qc.get(rc, {}), planned.get(rc, {}), packed.get(rc, {})
        row = {
            "recipe_no": rc, "recipe_name": k.get("recipe_name") or b.get("recipe_name") or rc,
            "from_section": k.get("from_section") or "", "uom": k.get("uom") or "",
            "planned": f(p.get("planned")) or 0.0,
            "required_qty": f(b.get("required_qty")) or 0.0,
            "issued_qty": f(i.get("issued_qty")) or 0.0,
            "transferred_qty": f(k.get("transferred_qty")) or 0.0,
            "received_qty": f(k.get("received_qty")) or 0.0,
            "recv_protein": f(k.get("recv_protein")), "recv_carb": f(k.get("recv_carb")),
            "recv_veg": f(k.get("recv_veg")), "portion_weight": f(k.get("portion_weight")),
            "packed_portion": f(pk.get("packed_portion")),
            "pk_protein": f(pk.get("protein")), "pk_carb": f(pk.get("carb")), "pk_veg": f(pk.get("veg")),
            "reached_qc": rc in qc,
        }
        row["issue_variance"] = row["issued_qty"] - row["required_qty"] if row["issued_qty"] else None
        for key, rv, pv in (("diff_protein", row["recv_protein"], row["pk_protein"]),
                            ("diff_carb", row["recv_carb"], row["pk_carb"]),
                            ("diff_veg", row["recv_veg"], row["pk_veg"])):
            row[key] = (rv - pv) if (rv is not None and pv is not None) else None
        rows.append(row)
    return rows


@router.get("", response_class=HTMLResponse)
def packing_dashboard(request: Request, db: Session = Depends(get_db)):
    require_area(request, "packing")
    q = request.query_params
    search = (q.get("search") or "").strip()
    from_date = (q.get("from_date") or "").strip()
    to_date = (q.get("to_date") or "").strip()
    status_f = (q.get("status") or "").strip()
    scope = (q.get("scope") or "current").strip().lower()
    from app.services.command_center import list_scope as _list_scope
    _ls = _list_scope(request, db, "pd")
    extra = _ls["sql"]
    params = dict(_ls["params"])
    if _ls["gf"]["active"]:
        scope = "all"  # Batch 204: the global timeline decides the dates
    if search:
        extra += " AND (pd.order_no LIKE :search OR COALESCE(pd.customer_name,'') LIKE :search OR COALESCE(co.brand,'') LIKE :search)"
        params["search"] = f"%{search}%"
    if from_date:
        extra += " AND COALESCE(co.required_delivery_date,'') >= :from_date"
        params["from_date"] = from_date
    if to_date:
        extra += " AND COALESCE(co.required_delivery_date,'') <= :to_date"
        params["to_date"] = to_date
    if status_f:
        extra += " AND COALESCE(pd.dispatch_status,'Packing Pending') = :status_f"
        params["status_f"] = status_f
   
    if scope != "all" and not from_date and not to_date:
        extra += " AND COALESCE(co.required_delivery_date, '9999-12-31') >= CURDATE()"
    rows = db.execute(text(f"""
        SELECT
            pd.id, pd.dispatch_no, pd.order_no, pd.customer_name,
            COALESCE(co.brand,'') AS brand,
            COALESCE(co.channel,'') AS channel,
            COALESCE(co.required_delivery_date,'') AS delivery_date,
            COALESCE(co.required_delivery_time,'') AS delivery_time,
            COALESCE(co.total_planned_portions, pd.packed_portions, 0) AS planned_portions,
            COALESCE(pd.packed_portions,0) AS packed_portions,
            COALESCE(pd.rejected_portions,0) AS rejected_portions,
            COALESCE(pd.dispatch_status,'Packing Pending') AS packing_status,
            COALESCE(pd.remarks,'') AS remarks,
            pd.created_at
        FROM packing_dispatch pd
        LEFT JOIN customer_orders co ON co.order_no = pd.order_no
        WHERE COALESCE(pd.dispatch_status,'Packing Pending') IN ('Packing Pending','Packing In Progress','Packed','Pending','Assigned')
        {extra}
        ORDER BY COALESCE(co.required_delivery_date, '9999-12-31') ASC, pd.id DESC
    """), params).mappings().all()
    # Batch 204: KPI tiles were unscoped (every company). Same scope as the list.
    _c = db.execute(text(f"""
        SELECT SUM(COALESCE(pd.dispatch_status,'Packing Pending') IN ('Packing Pending','Packing In Progress','Pending')) AS pending,
               SUM(pd.dispatch_status = 'Packed') AS packed,
               COALESCE(SUM(pd.rejected_portions),0) AS rejected,
               COALESCE(SUM(CASE WHEN pd.dispatch_status IN ('Packed','Assigned','Out for Delivery','Delivered') THEN pd.packed_portions END),0) AS portions
        FROM packing_dispatch pd WHERE 1=1 {_ls['sql']}"""), _ls["params"]).mappings().first() or {}
    summary = {k: (_c.get(k) or 0) for k in ("pending", "packed", "rejected", "portions")}
    return render(request, "packing/index.html", {"rows": rows, "summary": summary, "page_title": "Trayline / Packing",
                                                   "gf": _ls["gf"], "gf_options": _ls["gf_options"],
                                                   "filters": {"search": search, "from_date": from_date, "to_date": to_date, "status": status_f, "scope": scope},
                                                   "error": request.query_params.get("error")})


@router.get("/reports", response_class=HTMLResponse)
async def packing_reports(request: Request, db: Session = Depends(get_db)):
   
    require_area(request, "packing")
    from app.core.company import company_clause
    from app.core.db_read import rows as _rows

    cid = int(request.session.get("company_id") or 1)
    recent = _rows(db, """
        SELECT pd.id, pd.order_no, pd.customer_name, pd.dispatch_no,
               pd.dispatch_status, pd.packed_bags
        FROM packing_dispatch pd
        WHERE 1=1""" + company_clause("pd") + """
        ORDER BY pd.id DESC LIMIT 15""", {"scope_cid": cid}, label="packing reports hub")

    reports = [
        {"title": "Tray Line Report", "url": "/dispatch/logistics/tray-line",
         "icon": "bi-clipboard-check", "color": "#1e5bb8",
         "blurb": "The delivery-bags form — one row per receiver, per region, for signing at handover."},
        {"title": "Logistics / Region Report", "url": "/dispatch/logistics",
         "icon": "bi-geo-alt", "color": "#0ea5c6",
         "blurb": "Region-wise bag counts by customer, with totals and export."},
        {"title": "Logistics Board", "url": "/dispatch/logistics/board",
         "icon": "bi-truck", "color": "#8a5a00",
         "blurb": "Assign driver, vehicle and region bags to each dispatch."},
        {"title": "Dispatch / Delivery", "url": "/dispatch",
         "icon": "bi-box-seam", "color": "#15803d",
         "blurb": "Bag management, proof of delivery and the OTIF evidence."},
        {"title": "Trayline queue", "url": "/packing",
         "icon": "bi-grid-3x3-gap", "color": "#8c68f5",
         "blurb": "Orders waiting to be packed, and the packed weights per recipe."},
    ]
    return render(request, "packing/reports.html", {
        "page_title": "Packing Reports", "reports": reports, "recent": recent,
    })


@router.get("/{packing_id}", response_class=HTMLResponse)
def packing_order(request: Request, packing_id: int, db: Session = Depends(get_db)):
    require_record_scope(db, request, "packing_dispatch", packing_id)
    require_area(request, "packing")
    row = db.query(PackingDispatch).filter(PackingDispatch.id == packing_id).first()
    if not row:
        return _redirect_with_error("/packing", "Packing record not found.")
    order = db.query(CustomerOrder).filter(CustomerOrder.order_no == row.order_no).first()
    qc_rows = db.execute(text("""
        SELECT qc_no, qc_status, overall_score, checked_by, checked_at, issue_found, corrective_action
        FROM qc_checks
        WHERE order_no = :order_no
        ORDER BY id DESC
        LIMIT 5
    """), {"order_no": row.order_no}).mappings().all()

    pack_lines = pack_reconciliation(db, row.order_no)

    delivery_weekday = ""
    try:
        _dd = getattr(order, "required_delivery_date", None) if order else None
        if _dd:
            from datetime import datetime as _dt
            if hasattr(_dd, "strftime"):
                delivery_weekday = _dd.strftime("%A")
            else:
                delivery_weekday = _dt.strptime(str(_dd)[:10], "%Y-%m-%d").strftime("%A")
    except Exception:
        delivery_weekday = ""

    _PACK_REGIONS = PACK_REGIONS
    region_bags = []
    if getattr(row, "region_bags", None):
        try:
            import json as _json
            for name, cnt in _json.loads(row.region_bags).items():
                region_bags.append({"name": name, "bags": cnt})
        except Exception:
            region_bags = []

    if not region_bags and (getattr(row, "region", None) or getattr(row, "packed_bags", None)):
        region_bags = [{"name": row.region or _PACK_REGIONS[0], "bags": row.packed_bags or 0}]
    if not region_bags:
        region_bags = [{"name": _PACK_REGIONS[0], "bags": ""}]

    return render(request, "packing/order.html",
                  {"row": row, "order": order, "qc_rows": qc_rows,
                   "pack_lines": pack_lines, "delivery_weekday": delivery_weekday,
                   "region_bags": region_bags, "pack_regions": _PACK_REGIONS,
                   "packed_nutrition": {},
                   "page_title": f"Packing - {row.order_no}",
                   "error": request.query_params.get("error")})


@router.get("/{packing_id}/report", response_class=HTMLResponse)
def packing_report(request: Request, packing_id: int, db: Session = Depends(get_db)):
    require_record_scope(db, request, "packing_dispatch", packing_id)
    require_area(request, "packing")
    row = db.query(PackingDispatch).filter(PackingDispatch.id == packing_id).first()
    if not row:
        return _redirect_with_error("/packing", "Packing record not found.")
    order = db.query(CustomerOrder).filter(CustomerOrder.order_no == row.order_no).first()
    lines = pack_reconciliation(db, row.order_no)
    alloc = []
    if getattr(row, "region_bags", None):
        try:
            import json as _json
            alloc = [{"name": k, "bags": v} for k, v in _json.loads(row.region_bags).items()]
        except Exception:
            alloc = []
    extra = {}
    try:
        extra = dict(db.execute(text(
            "SELECT rejected_bags, rejected_bags_reason FROM packing_dispatch WHERE id = :i"),
            {"i": packing_id}).mappings().first() or {})
    except Exception:
        db.rollback()

    def _tot(key):
        vals = [x[key] for x in lines if x.get(key) is not None]
        return sum(vals) if vals else None

    totals = {k: _tot(k) for k in ("planned", "required_qty", "issued_qty", "transferred_qty", "received_qty",
                                   "recv_protein", "recv_carb", "recv_veg", "packed_portion",
                                   "pk_protein", "pk_carb", "pk_veg", "diff_protein", "diff_carb", "diff_veg")}
    return render(request, "packing/report.html", {
        "row": row, "order": order, "lines": lines, "totals": totals, "alloc": alloc,
        "rejected_bags": extra.get("rejected_bags"), "rejected_reason": extra.get("rejected_bags_reason"),
        "page_title": f"Pack Report - {row.order_no}",
    })


@router.post("/{packing_id}/pack-lines")
async def save_pack_lines(request: Request, packing_id: int, db: Session = Depends(get_db)):
    
    require_record_scope(db, request, "packing_dispatch", packing_id)
    require_action(request, "packing", "edit")
    row = db.execute(text("SELECT order_no FROM packing_dispatch WHERE id = :i"),
                     {"i": packing_id}).mappings().first()
    if not row:
        return _redirect_with_error("/packing", "Packing record not found.")
    order_no = row["order_no"]

    form = await request.form()
    recipes = form.getlist("pl_recipe_no")
    prot = form.getlist("pl_protein")
    carb = form.getlist("pl_carb")
    veg = form.getlist("pl_veg")
    portion = form.getlist("pl_portion")

    def _opt(seq, i):
        if i >= len(seq):
            return None
        v = (seq[i] or "").strip()
        if v == "":
            return None
        try:
            return float(v)
        except ValueError:
            return None

    saved = 0
    for i, rc in enumerate(recipes):
        rc = (rc or "").strip()
        if not rc:
            continue
        vals = {"o": order_no, "r": rc,
                "p": _opt(prot, i), "c": _opt(carb, i),
                "v": _opt(veg, i), "pp": _opt(portion, i)}

        if vals["pp"] is None:
            try:
                vals["pp"] = db.execute(text(
                    "SELECT packed_portion FROM packing_pack_lines WHERE order_no = :o AND recipe_no = :r"),
                    {"o": order_no, "r": rc}).scalar()
            except Exception:
                db.rollback()
        if all(vals[k] is None for k in ("p", "c", "v", "pp")):
            continue
        db.execute(text("""
            INSERT INTO packing_pack_lines
                (order_no, recipe_no, region, packed_portion,
                 packed_protein_g, packed_carb_g, packed_veg_g,
                 created_at, updated_at)
            VALUES (:o, :r, '', :pp, :p, :c, :v, NOW(), NOW())
            ON DUPLICATE KEY UPDATE
                packed_portion   = VALUES(packed_portion),
                packed_protein_g = VALUES(packed_protein_g),
                packed_carb_g    = VALUES(packed_carb_g),
                packed_veg_g     = VALUES(packed_veg_g),
                updated_at       = NOW()
        """), vals)
        saved += 1
    db.commit()

    from urllib.parse import quote as _q
    return RedirectResponse(
        f"/packing/{packing_id}?toast=success&title={_q('Packed Weights Saved')}"
        f"&msg={_q(f'{saved} recipe line(s) recorded.')}",
        status_code=HTTP_303_SEE_OTHER)


@router.post("/{packing_id}/update")
async def update_packing(
    request: Request,
    packing_id: int,
    packed_portions: float = Form(0),
    rejected_portions: float = Form(0),
    packed_bags: Optional[int] = Form(None),
    dispatch_date: Optional[str] = Form(None),
    packing_status: str = Form("Packed"),
    remarks: str = Form(""),
    region: str = Form(""),
    db: Session = Depends(get_db),
):

    require_record_scope(db, request, "packing_dispatch", packing_id)
    require_action(request, "packing", "edit")
    row = db.query(PackingDispatch).filter(PackingDispatch.id == packing_id).first()
    if not row:
        return _redirect_with_error("/packing", "Packing record not found.")

    # Batch 121: STEP-LOCK — packing is view-only once the order is dispatched.
    from app.core.stage_lock import is_stage_locked, lock_reason
    _order = db.query(CustomerOrder).filter(CustomerOrder.order_no == row.order_no).first()
    _status = getattr(_order, "status", "") if _order else ""
    if is_stage_locked(_status, "packing"):
        return _redirect_with_error("/packing", lock_reason(_status, "packing"))
    if packing_status not in {"Packing Pending", "Packing In Progress", "Packed"}:
        packing_status = "Packed"
    row.packed_portions = packed_portions
    row.rejected_portions = rejected_portions
    # Batch 146: region chosen at packing carries through to Dispatch/Logistics.
    if (region or "").strip():
        row.region = region.strip()

    _alloc_total = None
    try:
        _form = await request.form()
        _rn = _form.getlist("region_name") if hasattr(_form, "getlist") else []
        _rc = _form.getlist("region_bag_count") if hasattr(_form, "getlist") else []
        if _rn:
            import json as _json
            alloc: dict[str, int] = {}
            for name, cnt in zip(_rn, _rc):
                name = (name or "").strip()
                if not name:
                    continue
                try:
                    c = int(float(cnt or 0))
                except (TypeError, ValueError):
                    c = 0
                if c > 0:
                    alloc[name] = alloc.get(name, 0) + c
            if alloc:
                row.region_bags = _json.dumps(alloc)
                _alloc_total = sum(alloc.values())
                row.region = max(alloc, key=alloc.get)
            else:
                row.region_bags = None
    except Exception:
        pass
    try:
        db.execute(
            text("UPDATE packing_dispatch SET packed_bags = :b WHERE id = :i"),
            {"b": (_alloc_total if _alloc_total is not None
                   else (int(packed_bags) if packed_bags not in (None, "") else None)),
             "i": packing_id},
        )
    except Exception:
        db.rollback()
    row.dispatch_date = _parse_date(dispatch_date) or row.dispatch_date
    row.dispatch_status = packing_status
    row.remarks = remarks or row.remarks

    order = db.query(CustomerOrder).filter(CustomerOrder.order_no == row.order_no).first()
    if order:
        order.status = packing_status
    db.commit()
    from urllib.parse import quote as _q
    _bags = f" · {int(packed_bags)} bag(s)" if packed_bags not in (None, "") else ""
    return RedirectResponse(
        f"/packing?toast=success&title={_q('Packing Saved')}",
        status_code=HTTP_303_SEE_OTHER)
