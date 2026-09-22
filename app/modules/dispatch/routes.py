# app/modules/dispatch/routes.py
import os
import secrets
import logging
from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, Depends, Form, Request, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import text
from sqlalchemy.orm import Session
from starlette.status import HTTP_303_SEE_OTHER

from app.core.templates import render
from app.core.rbac import require_area, require_action
from app.database.session import get_db
from app.models.production import CustomerOrder, PackingDispatch
from app.modules.packing.routes import PACK_REGIONS, ensure_schema as _ensure_packing_schema
from app.core.company import require_order_scope, require_record_scope

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/dispatch", tags=["Dispatch"])

_POD_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "static", "uploads", "delivery_proof")


def _parse_date(value: Optional[str]):
    try:
        return date.fromisoformat(value) if value else None
    except Exception:
        return None


def _redirect_with_error(url: str, message: str) -> RedirectResponse:
    sep = "&" if "?" in url else "?"
    return RedirectResponse(f"{url}{sep}error={message}", status_code=HTTP_303_SEE_OTHER)


def _column_exists(db: Session, table: str, column: str) -> bool:
    return bool(db.execute(text("""
        SELECT COUNT(*) FROM information_schema.columns
        WHERE table_schema = DATABASE() AND table_name = :table AND column_name = :column
    """), {"table": table, "column": column}).scalar())


def _ensure_delivery_confirmation_schema(db: Session) -> None:
    """Batch 80 — proof-of-delivery columns on packing_dispatch, added
    defensively (MySQL 8 on this project's version doesn't reliably support
    ADD COLUMN IF NOT EXISTS, and phpMyAdmin has bitten this project on
    similar syntax before), so this checks information_schema first."""
    cols = {
        "delivery_otp": "VARCHAR(10) NULL",
        "delivery_otp_generated_at": "DATETIME NULL",
        "delivery_confirmed_by": "VARCHAR(20) NULL",
        "pod_photo_path": "VARCHAR(300) NULL",
        # Batch 224 — the two fields OTIF needs and the system never had.
        # delivered_at: when the customer RECEIVED it. Until now the only
        #   timestamp was dispatch_date, so an order that left on the right day
        #   but arrived at 15:00 for a noon service scored as on time.
        # delivered_portions: what actually arrived. Without it the "in full"
        #   half of OTIF cannot be computed — a short delivery counted as
        #   perfect. Both are nullable: history has neither, and a NULL keeps
        #   those orders OUT of the OTIF calculation rather than scoring them
        #   as failures.
        "delivered_at": "DATETIME NULL",
        "delivered_portions": "DECIMAL(14,2) NULL",
        "delivery_shortfall_reason": "VARCHAR(255) NULL",
    }
    for col, ddl in cols.items():
        if not _column_exists(db, "packing_dispatch", col):
            try:
                db.execute(text(f"ALTER TABLE packing_dispatch ADD COLUMN {col} {ddl}"))
                db.commit()
            except Exception:
                db.rollback()


def _rejected_info(db: Session, dispatch_id: int) -> tuple:
    """Batch 201 — (rejected_bags, rejected_bags_reason); raw SQL so the ORM
    model does not need the new columns (see packing.routes.ensure_schema)."""
    try:
        _ensure_packing_schema(db)
        r = db.execute(text("SELECT rejected_bags, rejected_bags_reason FROM packing_dispatch WHERE id = :i"),
                       {"i": dispatch_id}).mappings().first()
        return (r["rejected_bags"], r["rejected_bags_reason"]) if r else (None, None)
    except Exception:
        db.rollback()
        return (None, None)


def _ensure_tray_line_schema(db: Session) -> None:
    """Batch 176 — 176-E4. One row per (region, customer, delivery date) —
    matches exactly how the Logistics report already aggregates bags, so a
    confirmation always lines up with the row it was entered against, even
    though the underlying bags may have come from several packing_dispatch
    records split across regions."""
    try:
        db.execute(text("""
            CREATE TABLE IF NOT EXISTS delivery_confirmations (
                id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                company_id INT NULL,
                dispatch_date DATE NOT NULL,
                region VARCHAR(100) NOT NULL,
                customer_name VARCHAR(255) NOT NULL,
                expected_boxes INT NOT NULL DEFAULT 0,
                received_boxes INT NULL,
                confirmed_time VARCHAR(20) NULL,
                comments VARCHAR(500) NULL,
                confirmed_by VARCHAR(255) NULL,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                UNIQUE KEY uq_delivery_confirmation (dispatch_date, region, customer_name)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """))
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass


def _logistics_region_rows(db: Session, from_date: str, to_date: str,
                           extra: dict | None = None, delivery_fallback: bool = False) -> dict:
    """Batch 176 — extracted from logistics_report() so the Tray Line report
    (176-E4) can build on the exact same region+customer aggregation instead
    of a second, subtly different one.

    Batch 201 — `extra` filters (customer / order_no / brand / status /
    region) and `delivery_fallback`. The Tray Line report matched ONLY
    packing_dispatch.dispatch_date, which Trayline often leaves empty (Image 11
    shows mm/dd/yyyy) — those orders never appeared ("No packed/dispatched bags
    found"). With delivery_fallback the date falls back to the order's delivery
    date. The Logistics report calls this unchanged (extra=None, no fallback).
    """
    extra = extra or {}
    date_expr = ("COALESCE(pd.dispatch_date, co.required_delivery_date)"
                 if delivery_fallback else "pd.dispatch_date")
    where = "1=1"
    params: dict = {}
    if from_date:
        where += f" AND {date_expr} >= :fd"; params["fd"] = from_date
    if to_date:
        where += f" AND {date_expr} <= :td"; params["td"] = to_date
    if extra.get("customer"):
        where += " AND pd.customer_name = :cu"; params["cu"] = extra["customer"]
    if extra.get("order_no"):
        where += " AND pd.order_no = :on"; params["on"] = extra["order_no"]
    if extra.get("brand"):
        where += " AND COALESCE(co.brand,'') = :br"; params["br"] = extra["brand"]
    if extra.get("status"):
        where += " AND COALESCE(pd.dispatch_status,'') = :st"; params["st"] = extra["status"]
    if delivery_fallback:
        # Only bags that physically exist: packed or beyond.
        where += " AND COALESCE(pd.dispatch_status,'') IN ('Packed','Assigned','Out for Delivery','Delivered')"
    rows = db.execute(text(f"""
        SELECT pd.id, COALESCE(NULLIF(pd.region,''),'Unassigned') AS region,
               COALESCE(pd.customer_name,'—') AS customer_name,
               COALESCE(pd.packed_bags,0) AS bags,
               COALESCE(pd.packed_portions,0) AS portions,
               pd.region_bags
        FROM packing_dispatch pd
        LEFT JOIN customer_orders co ON co.order_no = pd.order_no
        WHERE {where}
        ORDER BY region, customer_name
    """), params).mappings().all()

    import json as _json
    expanded = []
    for r in rows:
        alloc = None
        if r.get("region_bags"):
            try:
                alloc = _json.loads(r["region_bags"])
            except Exception:
                alloc = None
        if alloc:
            _first = True
            for rname, rbags in alloc.items():
                expanded.append({"region": rname or "Unassigned", "customer_name": r["customer_name"],
                                 "orders": 1 if _first else 0, "bags": int(rbags or 0),
                                 "portions": float(r["portions"] or 0) if _first else 0.0})
                _first = False
        else:
            expanded.append({"region": r["region"], "customer_name": r["customer_name"],
                             "orders": 1, "bags": int(r["bags"] or 0), "portions": float(r["portions"] or 0)})

    if extra.get("region"):
        expanded = [e for e in expanded if e["region"] == extra["region"]]
    _agg = {}
    for e in expanded:
        key = (e["region"], e["customer_name"])
        if key not in _agg:
            _agg[key] = {"region": e["region"], "customer_name": e["customer_name"], "orders": 0, "bags": 0, "portions": 0.0}
        _agg[key]["orders"] += e["orders"]; _agg[key]["bags"] += e["bags"]; _agg[key]["portions"] += e["portions"]
    flat_rows = sorted(_agg.values(), key=lambda x: (x["region"], x["customer_name"]))

    regions: dict = {}
    for r in flat_rows:
        regions.setdefault(r["region"], {"rows": [], "bags": 0, "orders": 0, "portions": 0})
        g = regions[r["region"]]
        g["rows"].append(r)
        g["bags"] += int(r["bags"] or 0)
        g["orders"] += int(r["orders"] or 0)
        g["portions"] += float(r["portions"] or 0)
    return regions, flat_rows


@router.get("/report", response_class=HTMLResponse)
def dispatch_report(request: Request, db: Session = Depends(get_db)):
    """Batch 228 — the dispatch stage's own report.

    Built on the SAME delivery evidence as the OTIF metric (Batch 224) — the
    delivered_at timestamp and delivered_portions — so the report and the
    dashboard can never disagree about whether an order was late or short.

    Declared before /logistics only for readability; both are literal paths.
    """
    require_area(request, "dispatch")
    from app.core.company import company_clause
    from app.core.db_read import rows as _rows

    cid = int(request.session.get("company_id") or 1)
    f = {
        "from": (request.query_params.get("from") or "").strip(),
        "to": (request.query_params.get("to") or "").strip(),
        "customer": (request.query_params.get("customer") or "").strip(),
        "region": (request.query_params.get("region") or "").strip(),
        "status": (request.query_params.get("status") or "").strip(),
    }
    where, params = ["1=1"], {"scope_cid": cid}
    where.append(company_clause("pd").replace(" AND ", "", 1))
    # Filter on the DISPATCH date, falling back to the required delivery date —
    # an order packed but not yet dispatched has no dispatch date and would
    # otherwise vanish from every dated report.
    if f["from"]:
        where.append("COALESCE(pd.dispatch_date, o.required_delivery_date) >= :df"); params["df"] = f["from"]
    if f["to"]:
        where.append("COALESCE(pd.dispatch_date, o.required_delivery_date) <= :dt"); params["dt"] = f["to"]
    if f["customer"]:
        where.append("pd.customer_name = :cust"); params["cust"] = f["customer"]
    if f["region"]:
        where.append("COALESCE(pd.region,'') = :reg"); params["reg"] = f["region"]
    if f["status"]:
        where.append("COALESCE(pd.dispatch_status,'') = :st"); params["st"] = f["status"]

    rows = _rows(db, f"""
        SELECT pd.id, pd.order_no, pd.customer_name, pd.dispatch_no, pd.region,
               pd.driver_name, pd.vehicle_no, pd.dispatch_date, pd.delivered_at,
               pd.delivered_portions, pd.packed_bags, pd.dispatch_status,
               o.required_delivery_date,
               COALESCE(o.total_planned_portions, 0) AS ordered_portions
        FROM packing_dispatch pd
        JOIN customer_orders o ON o.order_no = pd.order_no
        WHERE {' AND '.join(where)}
        ORDER BY COALESCE(pd.dispatch_date, o.required_delivery_date) DESC, pd.id DESC
        LIMIT 500""", params, label="dispatch report")

    totals = {"dispatches": len(rows), "delivered": 0, "bags": 0,
              "portions": 0, "late": 0, "short": 0}
    for r in rows:
        totals["bags"] += float(r.get("packed_bags") or 0)
        totals["portions"] += float(r.get("ordered_portions") or 0)
        if (r.get("dispatch_status") or "") == "Delivered":
            totals["delivered"] += 1
        # None, not False, where there is no evidence — same rule as OTIF.
        r["on_time"] = None
        r["in_full"] = None
        if r.get("delivered_at") and r.get("required_delivery_date"):
            r["on_time"] = str(r["delivered_at"])[:10] <= str(r["required_delivery_date"])[:10]
            if not r["on_time"]:
                totals["late"] += 1
        if r.get("delivered_portions") is not None and float(r.get("ordered_portions") or 0) > 0:
            r["in_full"] = float(r["delivered_portions"]) >= float(r["ordered_portions"]) * 0.99
            if not r["in_full"]:
                totals["short"] += 1
        r["delivered_at"] = str(r["delivered_at"])[:16] if r.get("delivered_at") else ""
        r["dispatch_date"] = str(r["dispatch_date"])[:10] if r.get("dispatch_date") else ""
        r["required_delivery_date"] = str(r["required_delivery_date"])[:10] if r.get("required_delivery_date") else ""

    opts_rows = _rows(db, """
        SELECT DISTINCT pd.customer_name AS customer, COALESCE(pd.region,'') AS region,
               COALESCE(pd.dispatch_status,'') AS status
        FROM packing_dispatch pd WHERE 1=1""" + company_clause("pd"),
        {"scope_cid": cid}, label="dispatch report options")
    options = {
        "customers": sorted({r["customer"] for r in opts_rows if r.get("customer")}),
        "regions": sorted({r["region"] for r in opts_rows if r.get("region")}),
        "statuses": sorted({r["status"] for r in opts_rows if r.get("status")}),
    }
    return render(request, "dispatch/report.html", {
        "page_title": "Dispatch Report", "rows": rows, "totals": totals,
        "filters": f, "options": options,
    })


@router.get("/logistics", response_class=HTMLResponse)
def logistics_report(request: Request, db: Session = Depends(get_db)):
    """Batch 129 — Logistics report: region-wise bag counts by customer
    (image 13). Groups packing_dispatch by region + customer, summing bags and
    portions. CSV export supported via ?export=csv."""
    require_area(request, "dispatch")
    _ensure_delivery_confirmation_schema(db)
    q = request.query_params
    from_date = (q.get("from_date") or "").strip()
    to_date = (q.get("to_date") or "").strip()
    # Batch 243 (Image 6): customer- and order-wise filters, on top of the
    # existing date range. _logistics_region_rows already accepts these via
    # `extra`; the report route just never passed them through.
    customer = (q.get("customer") or "").strip()
    order_no = (q.get("order_no") or "").strip()
    extra = {}
    if customer:
        extra["customer"] = customer
    if order_no:
        extra["order_no"] = order_no
    regions, rows = _logistics_region_rows(db, from_date, to_date, extra=extra or None)

    # Dropdown option lists for the filter bar.
    try:
        customer_opts = [c for c in db.execute(text(
            "SELECT DISTINCT customer_name FROM packing_dispatch "
            "WHERE COALESCE(customer_name,'') <> '' ORDER BY customer_name"
        )).scalars().all()]
        order_opts = [o for o in db.execute(text(
            "SELECT DISTINCT order_no FROM packing_dispatch "
            "WHERE COALESCE(order_no,'') <> '' ORDER BY order_no DESC LIMIT 300"
        )).scalars().all()]
    except Exception:
        customer_opts, order_opts = [], []

    if q.get("export") == "csv":
        import csv, io
        out = io.StringIO(); w = csv.writer(out)
        w.writerow(["Region", "Customer", "Orders", "Bags", "Portions"])
        for region, g in regions.items():
            for r in g["rows"]:
                w.writerow([region, r["customer_name"], r["orders"], r["bags"], f'{float(r["portions"]):.2f}'])
            w.writerow([f"{region} TOTAL", "", g["orders"], g["bags"], f'{g["portions"]:.2f}'])
        out.seek(0)
        from fastapi.responses import StreamingResponse
        return StreamingResponse(iter(['\ufeff' + out.getvalue()]), media_type="text/csv",
                                 headers={"Content-Disposition": "attachment; filename=logistics_report.csv"})

    grand = {"bags": sum(g["bags"] for g in regions.values()),
             "orders": sum(g["orders"] for g in regions.values()),
             "portions": sum(g["portions"] for g in regions.values())}
    return render(request, "dispatch/logistics.html",
                  {"regions": regions, "grand": grand, "flat_rows": rows,
                   "filters": {"from_date": from_date, "to_date": to_date,
                               "customer": customer, "order_no": order_no},
                   "customer_opts": customer_opts, "order_opts": order_opts,
                   "page_title": "Logistics Report"})


@router.get("/logistics/tray-line", response_class=HTMLResponse)
def tray_line_report(request: Request, db: Session = Depends(get_db)):
   
    require_area(request, "dispatch")
    _ensure_tray_line_schema(db)
    # Batch 230: needed by the option and detail queries below.
    from app.core.db_read import rows as _rows

    cid = int(request.session.get("company_id") or 1)
    q = request.query_params
    d = (q.get("date") or date.today().isoformat()).strip()
    # Batch 201 (Image 13): customer / order / brand / region / status filters.
    filters = {k: (q.get(k) or "").strip() for k in ("customer", "order_no", "brand", "region", "status")}
    regions, _ = _logistics_region_rows(db, d, d, extra=filters, delivery_fallback=True)

    # ------------------------------------------------------------------
    # Batch 230 — the dropdowns used to list ONLY what was already on the
    # chosen date. On a day with nothing dispatched they came back empty, so
    # the filters looked broken exactly when you needed them to find the order
    # you were looking for.
    #
    # They now offer the day's dispatches AND every RUNNING order (packed,
    # assigned or out for delivery, whatever its date), so you can pick an
    # order first and let the date follow. Each list is de-duplicated and
    # sorted; the day's own entries come first because that is the common case.
    # ------------------------------------------------------------------
    def _opts(col, extra_where="", params=None):
        base = ("FROM packing_dispatch pd "
                "LEFT JOIN customer_orders co ON co.order_no = pd.order_no "
                "WHERE (pd.company_id = :cid OR pd.company_id IS NULL) ")
        try:
            return [r[0] for r in db.execute(
                text(f"SELECT DISTINCT {col} {base} {extra_where} ORDER BY 1"),
                {"d": d, "cid": cid, **(params or {})}).all() if r[0]]
        except Exception as exc:
            logger.warning("tray line options query failed: %s", str(exc).splitlines()[0][:200])
            db.rollback()
            return []

    _DAY = ("AND COALESCE(pd.dispatch_date, co.required_delivery_date) = :d "
            "AND COALESCE(pd.dispatch_status,'') IN ('Packed','Assigned','Out for Delivery','Delivered')")
    _RUNNING = "AND COALESCE(pd.dispatch_status,'') IN ('Packed','Assigned','Out for Delivery')"

    def _merge(col):
        day = _opts(col, _DAY)
        running = [x for x in _opts(col, _RUNNING) if x not in day]
        return day + running

    options = {
        "customers": _merge("pd.customer_name"),
        "orders": _merge("pd.order_no"),
        "brands": _merge("co.brand"),
        "regions": PACK_REGIONS,
        "statuses": ["Packed", "Assigned", "Out for Delivery", "Delivered"],
        # So the template can mark which entries belong to the selected day.
        "day_orders": _opts("pd.order_no", _DAY),
    }

    confirmations = {
        (c["region"], c["customer_name"]): c
        for c in db.execute(text("""
            SELECT region, customer_name, received_boxes, confirmed_time, comments, confirmed_by
            FROM delivery_confirmations WHERE dispatch_date = :d
        """), {"d": d}).mappings().all()
    }
    # Batch 230: the order detail behind each receiver line. The paper form has
    # a box count and nothing else, so nobody could tell WHICH orders made up a
    # receiver's bags without opening dispatch. One query for the whole day,
    # grouped in Python — a per-row query would be one round trip per receiver.
    detail: dict = {}
    for r in _rows(db, """
        SELECT COALESCE(pd.region,'Unassigned') AS region, pd.customer_name,
               pd.order_no, pd.dispatch_no, COALESCE(pd.packed_bags,0) AS bags,
               COALESCE(pd.dispatch_status,'') AS status,
               COALESCE(o.total_planned_portions,0) AS portions,
               COALESCE(o.brand,'') AS brand
        FROM packing_dispatch pd
        LEFT JOIN customer_orders o ON o.order_no = pd.order_no
        WHERE COALESCE(pd.dispatch_date, o.required_delivery_date) = :d
          AND COALESCE(pd.dispatch_status,'') IN ('Packed','Assigned','Out for Delivery','Delivered')
          AND (pd.company_id = :cid OR pd.company_id IS NULL)
        ORDER BY pd.order_no""", {"d": d, "cid": cid}, label="tray line order detail"):
        detail.setdefault((r["region"], r["customer_name"]), []).append(r)

    for region, g in regions.items():
        for r in g["rows"]:
            r["orders"] = detail.get((region, r["customer_name"]), [])
            c = confirmations.get((region, r["customer_name"]))
            r["received_boxes"] = c["received_boxes"] if c else None
            r["confirmed_time"] = c["confirmed_time"] if c else None
            r["comments"] = c["comments"] if c else None

    return render(request, "dispatch/tray_line_report.html", {
        "regions": regions, "date": d, "page_title": "Tray Line Report",
        "filters": filters, "options": options,
        # Batch 159-3b (Image 13): one merged order picker (order + customer +
        # status), running & previous orders, newest first.
        "order_options": [dict(r) for r in (db.execute(text("""
            SELECT DISTINCT pd.order_no,
                   COALESCE(pd.customer_name,'') AS customer,
                   COALESCE(pd.dispatch_status,'') AS status
            FROM packing_dispatch pd
            WHERE (pd.company_id = :cid OR pd.company_id IS NULL)
            ORDER BY pd.order_no DESC LIMIT 400
        """), {"cid": cid}).mappings().all() if True else [])],
        "totals": {"bags": sum(g["bags"] for g in regions.values()),
                   "receivers": sum(len(g["rows"]) for g in regions.values()),
                   "regions": len(regions)},
    })


@router.post("/logistics/tray-line/save")
async def tray_line_save(request: Request, db: Session = Depends(get_db)):
  
    require_action(request, "dispatch", "edit")
    _ensure_tray_line_schema(db)
    form = await request.form()
    dispatch_date = (form.get("dispatch_date") or "").strip()
    if not dispatch_date:
        return _redirect_with_error("/dispatch/logistics/tray-line", "Date is required.")

    regions_f = form.getlist("dc_region")
    customers_f = form.getlist("dc_customer")
    expected_f = form.getlist("dc_expected")
    received_f = form.getlist("dc_received")
    time_f = form.getlist("dc_time")
    comments_f = form.getlist("dc_comments")
    user = request.session.get("username") or request.session.get("user_name") or ""

    def _opt_int(seq, i):
        if i >= len(seq):
            return None
        v = (seq[i] or "").strip()
        if v == "":
            return None
        try:
            return int(float(v))
        except ValueError:
            return None

    saved = 0
    for i, region in enumerate(regions_f):
        region = (region or "").strip()
        customer = (customers_f[i] or "").strip() if i < len(customers_f) else ""
        if not region or not customer:
            continue
        expected = _opt_int(expected_f, i) or 0
        received = _opt_int(received_f, i)
        confirmed_time = (time_f[i] or "").strip() if i < len(time_f) else ""
        comments = (comments_f[i] or "").strip() if i < len(comments_f) else ""
        # Skip rows nobody touched — an untouched row should stay absent from
        # delivery_confirmations, not create a row of blanks that looks like
        # "confirmed with nothing filled in".
        if received is None and not confirmed_time and not comments:
            continue
        db.execute(text("""
            INSERT INTO delivery_confirmations
                (dispatch_date, region, customer_name, expected_boxes,
                 received_boxes, confirmed_time, comments, confirmed_by, updated_at)
            VALUES (:d, :rg, :cu, :exp, :rc, :ti, :co, :by, NOW())
            ON DUPLICATE KEY UPDATE
                expected_boxes = VALUES(expected_boxes),
                received_boxes = VALUES(received_boxes),
                confirmed_time = VALUES(confirmed_time),
                comments = VALUES(comments),
                confirmed_by = VALUES(confirmed_by),
                updated_at = NOW()
        """), {"d": dispatch_date, "rg": region, "cu": customer, "exp": expected,
               "rc": received, "ti": confirmed_time, "co": comments, "by": user})
        saved += 1
    db.commit()

    from urllib.parse import quote as _q
    return RedirectResponse(
        f"/dispatch/logistics/tray-line?date={dispatch_date}"
        f"&toast=success&title={_q('Saved')}&msg={_q(f'{saved} receiver(s) confirmed.')}",
        status_code=HTTP_303_SEE_OTHER)


@router.get("/logistics/board", response_class=HTMLResponse)
def logistics_board(request: Request, db: Session = Depends(get_db)):
    """Batch 152b — Logistics work board. The logistics user sees all dispatch
    orders (filters + table), opens one to assign driver / vehicle / region / bags
    (reusing the dispatch detail form), and can jump to the region report. Gated on
    the new `logistics` area, so a dedicated logistics role can be granted this
    without full dispatch rights."""
    require_area(request, "logistics")
    q = request.query_params
    search = (q.get("search") or "").strip()
    from_date = (q.get("from_date") or "").strip()
    to_date = (q.get("to_date") or "").strip()
    status_f = (q.get("status") or "").strip()
    scope = (q.get("scope") or "current").strip().lower()
    query = db.query(PackingDispatch).filter(
        PackingDispatch.dispatch_status.in_(["Packed", "Assigned", "Out for Delivery", "Delivered"]))
    if status_f:
        query = query.filter(PackingDispatch.dispatch_status == status_f)
    if search:
        query = query.filter((PackingDispatch.order_no.like(f"%{search}%")) |
                             (PackingDispatch.customer_name.like(f"%{search}%")))
    if from_date:
        query = query.filter(PackingDispatch.dispatch_date >= from_date)
    if to_date:
        query = query.filter(PackingDispatch.dispatch_date <= to_date)
    from datetime import date as _d
    from sqlalchemy import func as _func
    if scope != "all" and not from_date and not to_date:
        query = query.filter(_func.coalesce(PackingDispatch.dispatch_date, _d(9999, 12, 31)) >= _d.today())
    rows = query.order_by(_func.coalesce(PackingDispatch.dispatch_date, _d(9999, 12, 31)).asc(),
                          PackingDispatch.id.desc()).limit(200).all()
    summary = {
        "pending": db.query(PackingDispatch).filter(PackingDispatch.dispatch_status.in_(["Packed", "Assigned", "Out for Delivery"])).count(),
        "no_driver": db.query(PackingDispatch).filter(
            PackingDispatch.dispatch_status.in_(["Packed", "Assigned", "Out for Delivery"]),
            (PackingDispatch.driver_name.is_(None)) | (PackingDispatch.driver_name == "")).count(),
        "delivered": db.query(PackingDispatch).filter(PackingDispatch.dispatch_status == "Delivered").count(),
    }
    return render(request, "dispatch/logistics_board.html",
                  {"rows": rows, "summary": summary,
                   "filters": {"search": search, "from_date": from_date, "to_date": to_date, "status": status_f, "scope": scope},
                   "page_title": "Logistics Board"})


@router.get("/logistics/order/{dispatch_id}", response_class=HTMLResponse)
def logistics_detail(request: Request, dispatch_id: int, db: Session = Depends(get_db)):
    """Batch 157 — Logistics assignment detail. Owned by the LOGISTICS user: set
    driver / vehicle / region-wise bags for one order. Dispatch keeps portions /
    bags / status; driver+vehicle are read-only there. This fixes the board's
    Assign action pointing at the dispatch page (image 10)."""
    # Batch 207: an order number in the URL is not authorisation — 404 if it belongs to another company.
    require_record_scope(db, request, "packing_dispatch", dispatch_id)
    require_area(request, "logistics")
    row = db.query(PackingDispatch).filter(PackingDispatch.id == dispatch_id).first()
    if not row:
        return _redirect_with_error("/dispatch/logistics/board", "Dispatch record not found.")
    order = db.query(CustomerOrder).filter(CustomerOrder.order_no == row.order_no).first()
    region_bags = []
    if getattr(row, "region_bags", None):
        try:
            import json as _json
            for name, cnt in _json.loads(row.region_bags).items():
                region_bags.append({"name": name, "bags": cnt})
        except Exception:
            region_bags = []
    return render(request, "dispatch/logistics_detail.html",
                  {"r": row, "order": order, "region_bags": region_bags,
                   "regions": PACK_REGIONS,
                   "rejected_bags": _rejected_info(db, dispatch_id)[0],
                   "page_title": f"Logistics - {row.order_no}",
                   "error": request.query_params.get("error")})


@router.post("/logistics/order/{dispatch_id}/assign")
async def logistics_assign(request: Request, dispatch_id: int, db: Session = Depends(get_db)):
    """Batch 157 — save the logistics assignment (driver / vehicle / region-wise
    bags). Only the logistics-owned fields are written here."""
    # Batch 207: an order number in the URL is not authorisation — 404 if it belongs to another company.
    require_record_scope(db, request, "packing_dispatch", dispatch_id)
    require_area(request, "logistics")
    row = db.query(PackingDispatch).filter(PackingDispatch.id == dispatch_id).first()
    if not row:
        return _redirect_with_error("/dispatch/logistics/board", "Dispatch record not found.")
    form = await request.form()
    row.driver_name = (form.get("driver_name") or "").strip() or None
    row.vehicle_no = (form.get("vehicle_no") or "").strip() or None
    # ------------------------------------------------------------------
    # BATCH 195 (194-B) — Img 11, 12: "I have assigned 2 orders to a
    # driver... why does it still show Pending / Packed?"
    #
    # dispatch_status only ever had three real values: Packed, Out for
    # Delivery, Delivered. Assigning a driver/vehicle here wrote those two
    # fields but never touched dispatch_status — so a fully-assigned,
    # ready-to-depart order looked structurally identical to one nobody
    # had touched since packing. The dashboards' own "Awaiting Driver" KPI
    # (which DOES correctly check driver_name) implied a real state
    # existed for "assigned, not yet departed" — it just never had a
    # dispatch_status value of its own.
    #
    # New status: "Assigned". Set automatically, here, the moment both
    # driver and vehicle become non-empty — no new manual action for
    # anyone to remember. Only moves FORWARD from "Packed" (never
    # downgrades "Out for Delivery" or "Delivered" back to "Assigned" if
    # someone re-edits driver info after departure) and only applies when
    # both fields are actually present (clearing one afterward doesn't
    # auto-revert the status — that would be a surprising side effect of
    # what looks like a minor edit).
    # ------------------------------------------------------------------
    if row.driver_name and row.vehicle_no and (row.dispatch_status or "") == "Packed":
        row.dispatch_status = "Assigned"
    try:
        row.delivery_temperature_c = float(form.get("delivery_temperature_c") or 0)
    except (TypeError, ValueError):
        pass
    # region-wise bag allocation (parallel arrays)
    _rn = form.getlist("region_name") if hasattr(form, "getlist") else []
    _rc = form.getlist("region_bag_count") if hasattr(form, "getlist") else []
    if _rn:
        import json as _json
        alloc = {}
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
            row.packed_bags = sum(alloc.values())
            row.region = max(alloc, key=alloc.get)
    db.commit()
    return RedirectResponse("/dispatch/logistics/board?toast=success&title=Assigned&msg=Logistics assignment saved.",
                            status_code=HTTP_303_SEE_OTHER)


@router.get("", response_class=HTMLResponse)
def dispatch_dashboard(request: Request, db: Session = Depends(get_db)):
    require_area(request, "dispatch")
    _ensure_delivery_confirmation_schema(db)
    q = request.query_params
    search = (q.get("search") or "").strip()
    from_date = (q.get("from_date") or "").strip()
    to_date = (q.get("to_date") or "").strip()
    status_f = (q.get("status") or "").strip()
    scope = (q.get("scope") or "current").strip().lower()
    query = db.query(PackingDispatch).filter(PackingDispatch.dispatch_status.in_(["Packed", "Assigned", "Out for Delivery", "Delivered"]))
    # Batch 204: company scope (the list showed every company's dispatches) +
    # the global filter. list_scope() returns an AND-clause for the table alias.
    from app.services.command_center import list_scope as _list_scope
    _ls = _list_scope(request, db, "packing_dispatch")
    _scope_sql = _ls["sql"].strip()[4:]  # drop the leading "AND "
    query = query.filter(text(_scope_sql)).params(**_ls["params"])
    if _ls["gf"]["active"]:
        scope = "all"
    if status_f:
        query = query.filter(PackingDispatch.dispatch_status == status_f)
    if search:
        query = query.filter((PackingDispatch.order_no.like(f"%{search}%")) | (PackingDispatch.customer_name.like(f"%{search}%")))
    if from_date:
        query = query.filter(PackingDispatch.dispatch_date >= from_date)
    if to_date:
        query = query.filter(PackingDispatch.dispatch_date <= to_date)
    # Batch 144: default to current work (dispatch date today onward) unless a
    # date range or scope=all is set. Priority = nearest dispatch date first.
    from datetime import date as _d
    from sqlalchemy import func as _func
    if scope != "all" and not from_date and not to_date:
        query = query.filter(_func.coalesce(PackingDispatch.dispatch_date, _d(9999, 12, 31)) >= _d.today())
    rows = query.order_by(_func.coalesce(PackingDispatch.dispatch_date, _d(9999, 12, 31)).asc(), PackingDispatch.id.desc()).limit(200).all()
    _c = db.execute(text(f"""
        SELECT SUM(dispatch_status IN ('Packed','Assigned','Out for Delivery')) AS pending,
               SUM(dispatch_status = 'Delivered') AS delivered,
               SUM(COALESCE(rejected_portions,0) > 0) AS rejected,
               COALESCE(SUM(packed_portions),0) AS portions
        FROM packing_dispatch WHERE 1=1 {_ls['sql']}"""), _ls["params"]).mappings().first() or {}
    summary = {k: (_c.get(k) or 0) for k in ("pending", "delivered", "rejected", "portions")}
    return render(request, "dispatch/index.html", {"rows": rows, "summary": summary, "page_title": "Dispatch / Delivery",
                                                    "gf": _ls["gf"], "gf_options": _ls["gf_options"],
                                                    "filters": {"search": search, "from_date": from_date, "to_date": to_date, "status": status_f, "scope": scope},
                                                    "error": request.query_params.get("error")})


@router.post("/{dispatch_id}/generate-otp")
def generate_delivery_otp(request: Request, dispatch_id: int, db: Session = Depends(get_db)):
    """Batch 80 — generates a one-time code for delivery confirmation.

    This project has no SMS/email gateway configured yet (confirmed absent
    in the last security/architecture review), so this can't text the
    customer directly today. The intended flow: dispatch staff calls the
    customer, reads out this code, the customer reads it back to the
    driver at the door, and the driver enters it here to confirm delivery.
    Once SMS/WhatsApp integration exists, this same field is what a real
    "text the customer" step would populate automatically — no workflow
    change needed on this end, just wiring in the sender.
    """
    # Batch 207: an order number in the URL is not authorisation — 404 if it belongs to another company.
    require_record_scope(db, request, "packing_dispatch", dispatch_id)
    require_action(request, "dispatch", "edit")
    _ensure_delivery_confirmation_schema(db)
    row = db.query(PackingDispatch).filter(PackingDispatch.id == dispatch_id).first()
    if not row:
        return _redirect_with_error("/dispatch", "Dispatch record not found.")
    otp = f"{secrets.randbelow(10000):04d}"
    db.execute(text("UPDATE packing_dispatch SET delivery_otp=:o, delivery_otp_generated_at=:t WHERE id=:i"),
              {"o": otp, "t": datetime.utcnow(), "i": dispatch_id})
    db.commit()
    return RedirectResponse(
        f"/dispatch?toast=success&title=Delivery+Code+Generated&msg=Code {otp} for {row.order_no} — share it with the customer by phone, then have the driver enter it to confirm delivery.",
        status_code=HTTP_303_SEE_OTHER)


@router.get("/{dispatch_id}", response_class=HTMLResponse)
def dispatch_detail(request: Request, dispatch_id: int, db: Session = Depends(get_db)):
    """Batch 146b — single-order dispatch detail. The dashboard is now a compact
    index (cards + filters + table); opening an order brings you here to edit the
    driver / vehicle / region / status for just that order (image 12)."""
    # Batch 207: an order number in the URL is not authorisation — 404 if it belongs to another company.
    require_record_scope(db, request, "packing_dispatch", dispatch_id)
    require_area(request, "dispatch")
    _ensure_delivery_confirmation_schema(db)
    row = db.query(PackingDispatch).filter(PackingDispatch.id == dispatch_id).first()
    if not row:
        return _redirect_with_error("/dispatch", "Dispatch record not found.")
    order = db.query(CustomerOrder).filter(CustomerOrder.order_no == row.order_no).first()
    # Batch 152a: decode the region-bag allocation for the editor.
    region_bags = []
    if getattr(row, "region_bags", None):
        try:
            import json as _json
            for name, cnt in _json.loads(row.region_bags).items():
                region_bags.append({"name": name, "bags": cnt})
        except Exception:
            region_bags = []
    _rej, _rej_reason = _rejected_info(db, dispatch_id)
    try:
        order_lines = db.execute(text("""
            SELECT recipe_no, MAX(recipe_name) AS recipe_name, SUM(COALESCE(required_portions,0)) AS portions
            FROM order_lines WHERE order_no = :o GROUP BY recipe_no ORDER BY MAX(line_no)
        """), {"o": row.order_no}).mappings().all()
    except Exception:
        db.rollback()
        order_lines = []
    return render(request, "dispatch/detail.html",
                  {"r": row, "order": order, "region_bags": region_bags,
                   "rejected_bags": _rej, "rejected_reason": _rej_reason, "order_lines": order_lines,
                   "regions": PACK_REGIONS,
                   "page_title": f"Dispatch - {row.order_no}",
                   "error": request.query_params.get("error")})


@router.post("/{dispatch_id}/update")
async def update_dispatch(
    request: Request,
    dispatch_id: int,
    packed_portions: float = Form(0),
    rejected_portions: float = Form(0),
    dispatch_date: Optional[str] = Form(None),
    vehicle_no: str = Form(""),
    driver_name: str = Form(""),
    delivery_temperature_c: float = Form(0),
    dispatch_status: str = Form("Packed"),
    remarks: str = Form(""),
    region: str = Form(""),
    packed_bags: str = Form(""),
    delivery_otp_input: str = Form(""),
    pod_photo: Optional[UploadFile] = File(None),
    db: Session = Depends(get_db),
):
    # Batch 207: an order number in the URL is not authorisation — 404 if it belongs to another company.
    require_record_scope(db, request, "packing_dispatch", dispatch_id)
    require_action(request, "dispatch", "edit")
    _ensure_delivery_confirmation_schema(db)
    row = db.query(PackingDispatch).filter(PackingDispatch.id == dispatch_id).first()
    if not row:
        return _redirect_with_error("/dispatch", "Dispatch record not found.")

    # Batch 129: region + editable bag count. Persisted regardless of delivery
    # status transition (they're logistics attributes, not proof-of-delivery).
    _region = (region or "").strip()
    if _region:
        row.region = _region
    if (packed_bags or "").strip():
        try:
            row.packed_bags = int(float(packed_bags))
        except (TypeError, ValueError):
            pass

    # Batch 152a: region-wise bag allocation. The form posts parallel arrays
    # region_name[] + region_bag_count[]; we build {region: bags} and store JSON.
    # The total is also written to packed_bags so the delivery note / logistics
    # roll-up stay consistent.
    try:
        _form = await request.form()
        _rn = _form.getlist("region_name") if hasattr(_form, "getlist") else []
        _rc = _form.getlist("region_bag_count") if hasattr(_form, "getlist") else []
        if _rn:
            import json as _json
            alloc = {}
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
                row.packed_bags = sum(alloc.values())
                # primary region = the one with the most bags (for existing
                # single-region views and the logistics group-by).
                row.region = max(alloc, key=alloc.get)
    except Exception:
        pass

    # ------------------------------------------------------------------
    # Batch 100 — DELIVERED IS FINAL. Lock the record once delivery closes.
    #
    # Before this, a Delivered dispatch stayed fully editable: the packed
    # quantity, the driver, the vehicle, even the status could be changed
    # back afterwards. That breaks the whole point of the Batch 80
    # proof-of-delivery gate — you can satisfy it with a photo, save, and
    # then quietly rewrite the numbers the proof was attached to. It also
    # makes the delivery note and the AR invoice raised from it
    # unreconcilable with the record they came from.
    #
    # A delivered dispatch is a closed financial and legal document. If
    # something genuinely needs correcting, that is a credit note or a
    # returns process, not a silent edit — and neither exists yet, so the
    # honest behaviour is to refuse rather than to pretend.
    #
    # Deliberately checked BEFORE the proof gate below: a locked record must
    # not even reach the validation that could change it.
    # ------------------------------------------------------------------
    if (row.dispatch_status or "") == "Delivered":
        return _redirect_with_error(
            "/dispatch",
            f"{row.order_no} is already Delivered and is locked. "
            "A completed delivery cannot be edited — raise a customer complaint "
            "or a credit note if something needs correcting.")

    # Batch 80: proof-of-delivery gate. A delivery can't be marked Delivered
    # on trust alone anymore — the driver needs to provide EITHER a photo
    # (uploaded here) OR the OTP code the customer read back to them
    # (generated via "Generate Delivery Code" and confirmed against what's
    # stored). Neither present/matching -> the status change is rejected
    # and everything else on the form is left exactly as it was.
    # Batch 129: the delivery-code / photo proof-of-delivery UI was removed at
    # the client's request, so "Delivered" no longer requires proof here. The
    # record is still locked once Delivered (checked above), preserving the
    # "no silent edits after delivery" guarantee. If proof-of-delivery is
    # reinstated later, restore the gate that previously lived here.
    if dispatch_status == "Delivered":
        db.execute(text("UPDATE packing_dispatch SET delivery_confirmed_by='Manual' WHERE id=:i"),
                   {"i": dispatch_id})
        # ------------------------------------------------------------------
        # Batch 224 — capture OTIF evidence at the moment of delivery.
        #
        # delivered_at defaults to NOW() when the form does not supply a time:
        # marking an order Delivered IS the receipt event, and a blank
        # timestamp would silently drop the order out of OTIF. A back-dated
        # delivery can still be typed in.
        #
        # delivered_portions is left NULL when nothing is entered, NOT set to
        # the packed figure. Assuming "in full" because nobody typed a number
        # is how an on-time-only metric gets mistaken for OTIF, which is the
        # exact gap this batch closes.
        # ------------------------------------------------------------------
        _f_all = await request.form()
        _dlv_at = (_f_all.get("delivered_at") or "").strip()
        _dlv_qty = (_f_all.get("delivered_portions") or "").strip()
        _short = (_f_all.get("delivery_shortfall_reason") or "").strip() or None
        try:
            _qty = float(_dlv_qty) if _dlv_qty else None
        except (TypeError, ValueError):
            _qty = None
        db.execute(text("""
            UPDATE packing_dispatch
               SET delivered_at = COALESCE(:at, delivered_at, NOW()),
                   delivered_portions = COALESCE(:qty, delivered_portions),
                   delivery_shortfall_reason = COALESCE(:why, delivery_shortfall_reason)
             WHERE id = :i"""),
            {"at": _dlv_at.replace("T", " ") if _dlv_at else None,
             "qty": _qty, "why": _short, "i": dispatch_id})
        db.commit()

    row.packed_portions = packed_portions
    row.rejected_portions = rejected_portions
    row.dispatch_date = _parse_date(dispatch_date) or row.dispatch_date
    # Batch 157: driver/vehicle are logistics-owned and no longer on the dispatch
    # form. Only overwrite if a non-empty value was actually submitted, so a
    # dispatch save never wipes what logistics assigned.
    if vehicle_no:
        row.vehicle_no = vehicle_no
    if driver_name:
        row.driver_name = driver_name
    # Batch 201 ROOT CAUSE: this line ran on every dispatch save and the
    # dispatch form stopped posting temperature in Batch 157, so Form(0) →
    # `0 or None` → every dispatch save ERASED the temperature Logistics had
    # recorded. Temperature is logistics-owned; only write it if posted.
    _form_all = await request.form()
    if "delivery_temperature_c" in _form_all:
        row.delivery_temperature_c = delivery_temperature_c or None
    row.dispatch_status = dispatch_status
    row.remarks = remarks or None
    # Batch 201 (Image 12): rejected bags + reason — recorded, not hidden by
    # lowering the bag count, so the delivery note and tray line still show
    # what was packed and what was held back.
    if "rejected_bags" in _form_all:
        _ensure_packing_schema(db)
        try:
            _rb = (_form_all.get("rejected_bags") or "").strip()
            _rb_val = max(int(float(_rb)), 0) if _rb else None
        except (TypeError, ValueError):
            _rb_val = None
        _reason = (_form_all.get("rejected_bags_reason") or "").strip() or None
        if _rb_val and _rb_val > int(row.packed_bags or 0):
            return _redirect_with_error(f"/dispatch/{dispatch_id}",
                                        "Rejected bags cannot exceed the bags packed.")
        if _rb_val and not _reason:
            return _redirect_with_error(f"/dispatch/{dispatch_id}",
                                        "Enter a reason for the rejected bags.")
        db.execute(text("UPDATE packing_dispatch SET rejected_bags = :b, rejected_bags_reason = :r WHERE id = :i"),
                   {"b": _rb_val, "r": _reason, "i": dispatch_id})

    order = db.query(CustomerOrder).filter(CustomerOrder.order_no == row.order_no).first()
    if order:
        if dispatch_status == "Delivered":
            order.status = "Dispatched"
        elif dispatch_status == "Out for Delivery":
            order.status = "Out for Delivery"
        elif dispatch_status in ("Packed", "Assigned"):
            # Batch 201: "Assigned" (Batch 195) fell into the else-branch and
            # pushed the ORDER back to "Packing Pending" whenever Dispatch was
            # saved after Logistics assigned a driver.
            order.status = "Packed"
        else:
            order.status = "Packing Pending"
    db.commit()

    # Batch 69: on delivery, auto-post COGS — Dr 5100 COGS / Cr 1130 Inventory,
    # valued at the order's estimated food cost. Idempotent per order.
    if order and dispatch_status == "Delivered":
        try:
            from app.core.gl_posting import post_dispatch_cogs_journal
            post_dispatch_cogs_journal(
                db, request, row.order_no,
                float(getattr(order, "total_estimated_food_cost", 0) or 0),
                customer=getattr(order, "customer_name", "") or "")
        except Exception:
            pass

    return RedirectResponse("/dispatch", status_code=HTTP_303_SEE_OTHER)
