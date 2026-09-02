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

router = APIRouter(prefix="/packing", tags=["Trayline / Packing"])


def _parse_date(value: Optional[str]):
    try:
        return date.fromisoformat(value) if value else None
    except Exception:
        return None


def _redirect_with_error(url: str, message: str) -> RedirectResponse:
    sep = "&" if "?" in url else "?"
    return RedirectResponse(f"{url}{sep}error={message}", status_code=HTTP_303_SEE_OTHER)


def ensure_schema(db: Session) -> None:
    """Batch 121 — add packed_bags to packing_dispatch so the packer can
    record how many physical bags/trays go out. Surfaced later on Dispatch.
    Verified via information_schema first (CREATE INDEX IF NOT EXISTS is not
    supported on MySQL/MariaDB; plain column add is fine and idempotent)."""
    try:
        exists = db.execute(text("""
            SELECT COUNT(*) FROM information_schema.columns
            WHERE table_schema = DATABASE() AND table_name = 'packing_dispatch'
              AND column_name = 'packed_bags'
        """)).scalar()
        if not exists:
            db.execute(text("ALTER TABLE packing_dispatch ADD COLUMN packed_bags INT NULL"))
            db.commit()
    except Exception:
        db.rollback()


@router.get("", response_class=HTMLResponse)
def packing_dashboard(request: Request, db: Session = Depends(get_db)):
    require_area(request, "packing")
    q = request.query_params
    search = (q.get("search") or "").strip()
    from_date = (q.get("from_date") or "").strip()
    to_date = (q.get("to_date") or "").strip()
    status_f = (q.get("status") or "").strip()
    scope = (q.get("scope") or "current").strip().lower()
    extra = ""
    params = {}
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
    # Batch 144: default to current work (delivery today onward) unless a date
    # range or scope=all is set. Priority sort = nearest delivery first.
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
        WHERE COALESCE(pd.dispatch_status,'Packing Pending') IN ('Packing Pending','Packing In Progress','Packed','Pending')
        {extra}
        ORDER BY COALESCE(co.required_delivery_date, '9999-12-31') ASC, pd.id DESC
    """), params).mappings().all()
    summary = {
        "pending": db.execute(text("SELECT COUNT(*) FROM packing_dispatch WHERE COALESCE(dispatch_status,'Packing Pending') IN ('Packing Pending','Packing In Progress','Pending')")).scalar() or 0,
        "packed": db.execute(text("SELECT COUNT(*) FROM packing_dispatch WHERE dispatch_status = 'Packed'")).scalar() or 0,
        "rejected": db.execute(text("SELECT COALESCE(SUM(rejected_portions),0) FROM packing_dispatch")).scalar() or 0,
        "portions": db.execute(text("SELECT COALESCE(SUM(packed_portions),0) FROM packing_dispatch WHERE dispatch_status IN ('Packed','Out for Delivery','Delivered')")).scalar() or 0,
    }
    return render(request, "packing/index.html", {"rows": rows, "summary": summary, "page_title": "Trayline / Packing",
                                                   "filters": {"search": search, "from_date": from_date, "to_date": to_date, "status": status_f, "scope": scope},
                                                   "error": request.query_params.get("error")})


@router.get("/{packing_id}", response_class=HTMLResponse)
def packing_order(request: Request, packing_id: int, db: Session = Depends(get_db)):
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

    # Batch 130: per-recipe packing detail. Pull the recipe outputs that reached
    # QC/packing, with planned portions (from order_lines) vs received, the lack
    # (planned − received), and the protein/carb captured by Hot Kitchen in the
    # [NUT w= p= c=] tag on the section remark.
    # Batch 146 (fixes image 11): the previous GROUP BY included per-ingredient
    # columns (received/remarks), so a recipe appeared once PER INGREDIENT — the
    # long duplicated list in the screenshot. Aggregate to ONE row per recipe:
    # planned from order_lines, received = SUM across the recipe's QC lines, and
    # the section it came from. Nutrition (protein/carb) is pulled from any line
    # of the recipe that carries the Hot Kitchen [NUT ...] tag.
    import re as _re
    tx = db.execute(text("""
        SELECT k.recipe_no,
               MAX(k.recipe_name) AS recipe_name,
               ROUND(SUM(COALESCE(k.received_qty_standard, 0)), 2) AS received,
               MAX(COALESCE(ol.required_portions, 0)) AS planned,
               MAX(COALESCE(k.from_section, '')) AS from_section,
               GROUP_CONCAT(COALESCE(k.section_remarks, '') SEPARATOR ' ') AS remarks,
               MAX(k.standard_uom) AS uom,
               -- Batch 153: read the real columns. The [NUT ...] remark parse
               -- below is kept as a fallback for rows written before this batch
               -- and not yet back-filled, so nothing goes blank mid-migration.
               MAX(k.protein_g) AS protein_col,
               MAX(k.carb_g) AS carb_col,
               MAX(k.vegetable_g) AS veg_col,
               MAX(k.portion_weight_g) AS weight_col
        FROM kitchen_section_transactions k
        LEFT JOIN order_lines ol
          ON ol.order_no = k.order_no AND ol.recipe_no = k.recipe_no
        WHERE k.order_no = :order_no AND k.current_section = 'QC'
        GROUP BY k.recipe_no
        ORDER BY MAX(k.recipe_name)
    """), {"order_no": row.order_no}).mappings().all()

    pack_lines = []
    for t in tx:
        rm = t["remarks"] or ""
        w = p = c = ""
        m = _re.search(r"\[NUT\s+([^\]]*)\]", rm)
        if m:
            for kv in m.group(1).split():
                if kv.startswith("w="):
                    w = kv[2:]
                elif kv.startswith("p="):
                    p = kv[2:]
                elif kv.startswith("c="):
                    c = kv[2:]
        planned = float(t["planned"] or 0)
        received = float(t["received"] or 0)

        # Batch 153 — column first, remark stamp second. Once the back-fill has
        # run the stamp path stops being used, but leaving it in means an
        # un-migrated database still shows figures instead of dashes.
        def _pick(col_val, parsed):
            if col_val is not None:
                return float(col_val)
            try:
                return float(parsed) if str(parsed).strip() != "" else None
            except (TypeError, ValueError):
                return None

        recv_protein = _pick(t["protein_col"], p)
        recv_carb = _pick(t["carb_col"], c)
        recv_veg = _pick(t["veg_col"], None)
        weight = _pick(t["weight_col"], w)

        pack_lines.append({
            "recipe_no": t["recipe_no"], "recipe_name": t["recipe_name"],
            "planned": planned, "received": received,
            "lack": max(planned - received, 0),
            # Received side — what Hot Kitchen sent through
            "recv_protein": recv_protein, "recv_carb": recv_carb, "recv_veg": recv_veg,
            "protein": p, "carb": c, "weight": weight,
            "from_section": t["from_section"], "uom": t["uom"],
        })

    # Batch 153 — packed weights per recipe, from packing_pack_lines when that
    # table exists. Trayline entry per recipe is not built yet, so today this
    # resolves to an empty map and the Packed / Excess columns show "—".
    # That is deliberate: showing 0.00 for a line nobody has weighed would make
    # an unreconciled recipe look reconciled. The columns and the arithmetic are
    # in place, so the entry form is a small follow-up rather than a rework.
    packed_nutrition: dict = {}
    try:
        _has = db.execute(text("""
            SELECT COUNT(*) FROM information_schema.tables
            WHERE table_schema = DATABASE() AND table_name = 'packing_pack_lines'
        """)).scalar()
        if _has:
            for _r in db.execute(text("""
                SELECT recipe_no,
                       MAX(packed_protein_g) AS protein,
                       MAX(packed_carb_g)    AS carb,
                       MAX(packed_veg_g)     AS veg
                FROM packing_pack_lines WHERE order_no = :o GROUP BY recipe_no
            """), {"o": row.order_no}).mappings().all():
                packed_nutrition[_r["recipe_no"]] = {
                    "protein": float(_r["protein"]) if _r["protein"] is not None else None,
                    "carb": float(_r["carb"]) if _r["carb"] is not None else None,
                    "veg": float(_r["veg"]) if _r["veg"] is not None else None,
                }
    except Exception:
        packed_nutrition = {}

    # Batch 152: weekday name for the delivery date (image 14).
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

    # Batch 148: existing region/bag split for the allocator. Passed explicitly
    # (an undefined name in Jinja is falsy, which would render an empty
    # allocator on an order that already has one and quietly wipe it on save).
    _PACK_REGIONS = ["Riyadh", "Eastern", "Jeddah", "Makkah", "Madinah", "Qassim", "Other"]
    region_bags = []
    if getattr(row, "region_bags", None):
        try:
            import json as _json
            for name, cnt in _json.loads(row.region_bags).items():
                region_bags.append({"name": name, "bags": cnt})
        except Exception:
            region_bags = []

    return render(request, "packing/order.html",
                  {"row": row, "order": order, "qc_rows": qc_rows,
                   "pack_lines": pack_lines, "delivery_weekday": delivery_weekday,
                   "region_bags": region_bags, "pack_regions": _PACK_REGIONS,
                   # Batch 153: packed weights recorded at Trayline, keyed by
                   # recipe. Passed explicitly — an undefined name in Jinja is
                   # falsy, so omitting it would render a silent column of
                   # dashes that looks like "nothing packed yet" rather than a
                   # missing variable.
                   "packed_nutrition": packed_nutrition,
                   "page_title": f"Packing - {row.order_no}",
                   "error": request.query_params.get("error")})


@router.post("/{packing_id}/pack-lines")
async def save_pack_lines(request: Request, packing_id: int, db: Session = Depends(get_db)):
    """Batch 157 — record packed weights per recipe at Trayline.

    Saves the whole grid in one submit rather than a Save button per row: the
    operator weighs the trays for an order as one pass, and a per-row save would
    mean 19 round trips and 19 chances to lose a value by navigating away.

    Upsert on (order_no, recipe_no, region) so re-weighing corrects the existing
    row instead of stacking duplicates. region is '' today — see the schema
    guard in main.py for why the column exists now rather than later.

    A blank box is stored as NULL, not 0. Nothing weighed is not the same as
    weighed-and-found-zero, and the pack sheet renders the two differently
    ("—" versus 0.00). Writing 0 for blanks would make an unweighed recipe look
    reconciled with a shortage equal to everything received.
    """
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

    # ------------------------------------------------------------------
    # BATCH 148 — REGION-WISE BAG ALLOCATION MOVES TO TRAYLINE
    #
    # The allocation lived on the Dispatch screen, which is the wrong place:
    # Trayline is where bags are physically filled and where the operator knows
    # that 10 bags are Riyadh and 8 are Dammam. Dispatch was being asked to
    # re-enter a fact that had already happened upstream, which is how the two
    # screens end up disagreeing.
    #
    # Same JSON shape and same column as the Dispatch form wrote, so the
    # logistics report and the existing per-region expansion keep working
    # untouched. Dispatch keeps its editor as a correction path.
    #
    # packed_bags is DERIVED from the allocation when one is supplied, so the
    # header count and the region rows can never disagree. Without an
    # allocation the manually entered packed_bags below still applies.
    # ------------------------------------------------------------------
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
                # Primary region = the one with the most bags, matching what the
                # Dispatch form already did, so single-region views are stable.
                row.region = max(alloc, key=alloc.get)
            else:
                # An explicitly emptied allocation clears it rather than leaving
                # a stale split behind a now-single-region order.
                row.region_bags = None
    except Exception:
        pass
    # Batch 121: persist bag count (column added via ensure_schema). Written
    # with raw SQL so it works even if the ORM model attribute isn't present.
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
