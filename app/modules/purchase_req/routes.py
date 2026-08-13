# app/modules/purchase_req/routes.py
# =============================================================================
# PURCHASE REQUISITIONS: the pre-stage before a Purchase Order.
# -----------------------------------------------------------------------------
# The pipeline this closes:
#
#   Need identified (shortage / manual)
#        -> Purchase Requisition  [Pending]
#        -> Procurement review    [Approved | Rejected]
#        -> Convert to PO         [Converted]  -> normal PO/GRN flow, unchanged
# =============================================================================
from __future__ import annotations

from datetime import date, datetime, timedelta
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.templates import render
from app.core.rbac import require_area, require_action
from app.core.notifications import notify_role
from app.database.session import get_db

router = APIRouter(prefix="/purchase-requisitions", tags=["Purchase Requisitions"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _cid(request: Request) -> int:
    return int(request.session.get("company_id") or 1)


def _user(request: Request) -> str:
    return request.session.get("username", "system")


def ensure_schema(db: Session) -> None:
    """Create the PR tables if they don't exist.

    NOTE on MySQL 8 (learned the hard way in earlier batches):
    - CREATE INDEX IF NOT EXISTS is PostgreSQL syntax and is NOT supported,
      so indexes are declared inline in CREATE TABLE via KEY.
    - ALTER TABLE ... ADD COLUMN IF NOT EXISTS is likewise unsupported, so
      any later column additions must go through an information_schema
      pre-check (see _ensure_pr_columns below).
    """
    try:
        db.execute(text("""
            CREATE TABLE IF NOT EXISTS purchase_requisitions (
                id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                company_id INT NULL,
                pr_no VARCHAR(40) NOT NULL UNIQUE,
                pr_date DATE NULL,
                requested_by VARCHAR(120) NULL,
                department VARCHAR(80) NULL,
                source_type VARCHAR(30) NULL,
                source_ref VARCHAR(80) NULL,
                required_date DATE NULL,
                justification VARCHAR(500) NULL,
                status VARCHAR(20) NOT NULL DEFAULT 'Pending',
                reviewed_by VARCHAR(120) NULL,
                reviewed_at DATETIME NULL,
                review_reason VARCHAR(500) NULL,
                converted_po_nos VARCHAR(500) NULL,
                converted_at DATETIME NULL,
                estimated_value DECIMAL(18,4) NOT NULL DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                KEY idx_pr_status (status),
                KEY idx_pr_company (company_id),
                KEY idx_pr_source (source_type, source_ref)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """))
        db.execute(text("""
            CREATE TABLE IF NOT EXISTS purchase_requisition_lines (
                id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                company_id INT NULL,
                pr_no VARCHAR(40) NOT NULL,
                line_no INT NOT NULL,
                inventory_code VARCHAR(80) NOT NULL,
                item_name VARCHAR(255) NULL,
                uom VARCHAR(50) NULL,
                required_qty DECIMAL(18,6) NOT NULL DEFAULT 0,
                on_hand_qty DECIMAL(18,6) NOT NULL DEFAULT 0,
                approved_qty DECIMAL(18,6) NULL,
                suggested_supplier VARCHAR(255) NULL,
                estimated_price DECIMAL(18,6) NOT NULL DEFAULT 0,
                line_remarks VARCHAR(255) NULL,
                KEY idx_prl_pr (pr_no),
                KEY idx_prl_item (inventory_code)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """))
        db.commit()
    except Exception:
        db.rollback()


def _next_no(db: Session) -> str:
    """PR-000001 style, same shape as Procurement's own PO numbering."""
    row = db.execute(text(
        "SELECT pr_no FROM purchase_requisitions ORDER BY id DESC LIMIT 1"
    )).first()
    n = 0
    if row and row[0]:
        try:
            n = int(str(row[0]).split("-")[-1])
        except Exception:
            n = 0
    return f"PR-{n + 1:06d}"


def _rows(db: Session, sql: str, params: dict | None = None) -> list[dict]:
    return [dict(r) for r in db.execute(text(sql), params or {}).mappings().all()]


def _one(db: Session, sql: str, params: dict | None = None) -> dict | None:
    r = db.execute(text(sql), params or {}).mappings().first()
    return dict(r) if r else None


def on_hand_map(db: Session, codes: list[str], cid: int) -> dict[str, float]:
    """Stock on hand from the ledger — never from ingredients.current_stock.

    Mirrors preview_bom_shortages() exactly, including the Batch 93 QC gate:
    quantity received but still sitting in QC Hold ('Pending') or rejected
    ('Failed') is NOT available to production, so it must not count here
    either. Legacy rows written before qc_status existed are NULL and pass
    through the CASE untouched.
    """
    if not codes:
        return {}
    placeholders = ",".join(f":c{i}" for i in range(len(codes)))
    params: dict = {f"c{i}": c for i, c in enumerate(codes)}
    params["cid"] = cid
    rows = db.execute(text(f"""
        SELECT inventory_code,
               COALESCE(SUM(
                 CASE WHEN qc_status IN ('Pending','Failed') THEN 0 ELSE COALESCE(qty_in,0) END
               ), 0) - COALESCE(SUM(COALESCE(qty_out,0)), 0) AS on_hand
        FROM inventory_transactions
        WHERE inventory_code IN ({placeholders})
          AND (company_id = :cid OR company_id IS NULL)
        GROUP BY inventory_code
    """), params).mappings().all()
    return {r["inventory_code"]: float(r["on_hand"] or 0) for r in rows}


def create_requisition(
    db: Session,
    *,
    company_id: int,
    requested_by: str,
    lines: list[dict],
    department: str = "",
    source_type: str = "Manual",
    source_ref: str = "",
    required_date=None,
    justification: str = "",
) -> str:
    """Shared PR creator.

    Called from three places — the manual PR form here, the Sales Request
    screen's shortage action, and the order-detail shortage action — so all
    three produce an identical document rather than three near-copies that
    drift apart. Returns the new PR number.

    Each line dict: inventory_code, item_name, uom, required_qty,
    on_hand_qty, suggested_supplier, estimated_price, line_remarks.
    """
    ensure_schema(db)
    pr_no = _next_no(db)
    est_total = sum(
        float(l.get("required_qty") or 0) * float(l.get("estimated_price") or 0)
        for l in lines
    )
    db.execute(text("""
        INSERT INTO purchase_requisitions
            (company_id, pr_no, pr_date, requested_by, department, source_type, source_ref,
             required_date, justification, status, estimated_value)
        VALUES (:cid, :pr, :d, :by, :dept, :st, :sr, :rd, :j, 'Pending', :ev)
    """), {
        "cid": company_id, "pr": pr_no, "d": date.today(), "by": requested_by,
        "dept": department or None, "st": source_type, "sr": source_ref or None,
        "rd": required_date, "j": (justification or "")[:500] or None,
        "ev": round(est_total, 4),
    })
    for i, l in enumerate(lines, start=1):
        db.execute(text("""
            INSERT INTO purchase_requisition_lines
                (company_id, pr_no, line_no, inventory_code, item_name, uom,
                 required_qty, on_hand_qty, suggested_supplier, estimated_price, line_remarks)
            VALUES (:cid, :pr, :ln, :code, :name, :uom, :qty, :oh, :sup, :price, :rem)
        """), {
            "cid": company_id, "pr": pr_no, "ln": i,
            "code": l.get("inventory_code"), "name": l.get("item_name"),
            "uom": l.get("uom") or "", "qty": float(l.get("required_qty") or 0),
            "oh": float(l.get("on_hand_qty") or 0),
            "sup": (l.get("suggested_supplier") or "") or None,
            "price": float(l.get("estimated_price") or 0),
            "rem": (l.get("line_remarks") or "")[:255] or None,
        })
    db.commit()

    notify_role(
        db, company_id=company_id, role="PROCUREMENT",
        title=f"Purchase Requisition {pr_no} awaiting review",
        message=f"{len(lines)} item(s) requested by {requested_by}"
                + (f" for {source_ref}" if source_ref else ""),
        url=f"/purchase-requisitions/{pr_no}", category="pr_pending",
    )
    return pr_no


# ---------------------------------------------------------------------------
# List / register
# ---------------------------------------------------------------------------
@router.get("")
def pr_list(request: Request, db: Session = Depends(get_db)):
    require_area(request, "purchase_requisition")
    ensure_schema(db)
    cid = _cid(request)
    # Batch 97: default changed from "Pending" to "All".
    #
    # Defaulting to Pending meant the screen opened showing an empty table
    # while the KPI cards above it said "1 Approved" — the register looked
    # broken. Worse, the one state a user most often wants after approving
    # something (did it convert? where did it go?) was hidden behind a filter
    # they had to notice and change. A register should show the register.
    status_f = (request.query_params.get("status") or "All").strip()
    search = (request.query_params.get("search") or "").strip()

    where = "(r.company_id = :cid OR r.company_id IS NULL)"
    params: dict = {"cid": cid}
    if status_f and status_f.lower() != "all":
        where += " AND r.status = :st"
        params["st"] = status_f
    if search:
        where += " AND (r.pr_no LIKE :s OR r.source_ref LIKE :s OR r.requested_by LIKE :s)"
        params["s"] = f"%{search}%"

    prs = _rows(db, f"""
        SELECT r.*,
               (SELECT COUNT(*) FROM purchase_requisition_lines l WHERE l.pr_no = r.pr_no) AS line_count
        FROM purchase_requisitions r
        WHERE {where}
        ORDER BY r.id DESC LIMIT 300
    """, params)

    counts = _one(db, """
        SELECT
          SUM(status = 'Pending')   AS pending,
          SUM(status = 'Approved')  AS approved,
          SUM(status = 'Rejected')  AS rejected,
          SUM(status = 'Converted') AS converted,
          COALESCE(SUM(CASE WHEN status = 'Pending' THEN estimated_value ELSE 0 END), 0) AS pending_value
        FROM purchase_requisitions
        WHERE (company_id = :cid OR company_id IS NULL)
    """, {"cid": cid}) or {}

    return render(request, "purchase_req/list.html", {
        "prs": prs,
        "counts": counts,
        "filters": {"status": status_f, "search": search},
        "status_options": ["All", "Approved", "Rejected", "Converted", "Pending"],
        "page_title": "Purchase Requisitions",
    })


# ---------------------------------------------------------------------------
# Manual creation
# ---------------------------------------------------------------------------
@router.get("/new")
def pr_new_form(request: Request, db: Session = Depends(get_db)):
    require_area(request, "purchase_requisition")
    ensure_schema(db)
    items = _rows(db, """
        SELECT ingredient_code AS inventory_code, name AS item_name,
               COALESCE(standard_uom, purchase_uom, recipe_uom, '') AS uom,
               COALESCE(default_supplier, '') AS default_supplier
        FROM ingredients ORDER BY ingredient_code LIMIT 3000
    """)
    return render(request, "purchase_req/form.html", {
        "items": items, "page_title": "New Purchase Requisition",
    })


@router.post("/new")
async def pr_create(request: Request, db: Session = Depends(get_db)):
    require_action(request, "purchase_requisition", "add")
    ensure_schema(db)
    form = await request.form()
    cid = _cid(request)

    codes = form.getlist("inventory_code")
    names = form.getlist("item_name")
    uoms = form.getlist("uom")
    qtys = form.getlist("required_qty")
    prices = form.getlist("estimated_price")
    sups = form.getlist("suggested_supplier")

    lines: list[dict] = []
    for i, code in enumerate(codes):
        code = (code or "").strip()
        try:
            qty = float(qtys[i] or 0) if i < len(qtys) else 0.0
        except (ValueError, IndexError):
            qty = 0.0
        if not code or qty <= 0:
            continue
        try:
            price = float(prices[i] or 0) if i < len(prices) else 0.0
        except (ValueError, IndexError):
            price = 0.0
        lines.append({
            "inventory_code": code,
            "item_name": (names[i] if i < len(names) else "") or code,
            "uom": uoms[i] if i < len(uoms) else "",
            "required_qty": qty,
            "on_hand_qty": 0.0,
            "suggested_supplier": sups[i] if i < len(sups) else "",
            "estimated_price": price,
        })

    if not lines:
        return RedirectResponse(
            "/purchase-requisitions/new?toast=warning&title=Nothing to request"
            "&msg=Add at least one item with a quantity greater than zero.",
            status_code=303)

    # Stamp real on-hand at the moment of raising, so the reviewer can see
    # what the requester was looking at rather than a number that has since
    # moved. Ledger only — never ingredients.current_stock.
    oh = on_hand_map(db, [l["inventory_code"] for l in lines], cid)
    for l in lines:
        l["on_hand_qty"] = oh.get(l["inventory_code"], 0.0)

    req_date = (form.get("required_date") or "").strip() or None
    pr_no = create_requisition(
        db, company_id=cid, requested_by=_user(request), lines=lines,
        department=(form.get("department") or "").strip(),
        source_type="Manual",
        required_date=req_date,
        justification=(form.get("justification") or "").strip(),
    )
    return RedirectResponse(
        f"/purchase-requisitions/{pr_no}?toast=success&title=Requisition Raised"
        f"&msg={pr_no} submitted to Procurement for review.", status_code=303)


# ---------------------------------------------------------------------------
# Detail
# ---------------------------------------------------------------------------
@router.get("/{pr_no}")
def pr_detail(request: Request, pr_no: str, db: Session = Depends(get_db)):
    require_area(request, "purchase_requisition")
    ensure_schema(db)
    cid = _cid(request)
    pr = _one(db, """
        SELECT * FROM purchase_requisitions
        WHERE pr_no = :p AND (company_id = :cid OR company_id IS NULL)
    """, {"p": pr_no, "cid": cid})
    if not pr:
        return RedirectResponse(
            "/purchase-requisitions?toast=danger&title=Not found"
            f"&msg=Requisition {pr_no} does not exist for this company.", status_code=303)

    lines = _rows(db, """
        SELECT * FROM purchase_requisition_lines WHERE pr_no = :p ORDER BY line_no
    """, {"p": pr_no})

    # Live on-hand alongside the on-hand captured when the PR was raised —
    # stock moves between raising and reviewing, and the reviewer should be
    # deciding on today's number, not last week's.
    live = on_hand_map(db, [l["inventory_code"] for l in lines], cid)
    for l in lines:
        l["live_on_hand"] = live.get(l["inventory_code"], 0.0)
        l["still_short"] = float(l["required_qty"] or 0) > l["live_on_hand"] + 0.0001

    suppliers = _rows(db, """
        SELECT supplier_code, supplier_name FROM suppliers
        ORDER BY supplier_name LIMIT 1000
    """)

    # ------------------------------------------------------------------
    # Batch 99 — suggest a supplier per line from actual purchase history.
    #
    # Before this, every line said "Assign supplier" and the buyer had to
    # remember, or go digging through old POs, who last supplied each of 19
    # ingredients. On a requisition this size that is the slowest part of the
    # whole job and the easiest place to pick the wrong vendor.
    #
    # For each item on the PR, this pulls the suppliers that have actually
    # delivered it before, ranked by how recently, with the last price paid.
    # Ranked by recency rather than frequency on purpose: a supplier used
    # twenty times two years ago is less useful than the one used last month,
    # and prices move.
    #
    # Only GRN-backed history counts — a PO that was raised and never received
    # is not evidence that the supplier can actually deliver the item.
    # ------------------------------------------------------------------
    codes = [l["inventory_code"] for l in lines]
    suggestions: dict[str, list[dict]] = {}
    if codes:
        ph = ",".join(f":s{i}" for i in range(len(codes)))
        params = {f"s{i}": c for i, c in enumerate(codes)}
        params["cid"] = cid
        try:
            for r in _rows(db, f"""
                SELECT pol.inventory_code,
                       po.supplier_name,
                       MAX(po.po_date)                AS last_ordered,
                       COUNT(DISTINCT po.po_no)       AS times_used,
                       SUBSTRING_INDEX(
                         GROUP_CONCAT(pol.unit_price ORDER BY po.po_date DESC), ',', 1
                       ) AS last_price
                FROM purchase_order_lines pol
                JOIN purchase_orders po ON po.po_no = pol.po_no
                JOIN grn_receipts g     ON g.po_no  = po.po_no
                WHERE pol.inventory_code IN ({ph})
                  AND (po.company_id = :cid OR po.company_id IS NULL)
                  AND COALESCE(po.supplier_name, '') <> ''
                GROUP BY pol.inventory_code, po.supplier_name
                ORDER BY pol.inventory_code, last_ordered DESC
            """, params):
                suggestions.setdefault(r["inventory_code"], []).append({
                    "supplier_name": r["supplier_name"],
                    "last_ordered": str(r["last_ordered"] or ""),
                    "times_used": int(r["times_used"] or 0),
                    "last_price": float(r["last_price"] or 0),
                })
        except Exception:
            # History is a convenience, never a dependency — if the query
            # fails (legacy schema, missing GRN table) the screen still works
            # exactly as it did before, just without the shortcuts.
            suggestions = {}

    # Keep the top 3 per item so the UI stays scannable.
    for k in suggestions:
        suggestions[k] = suggestions[k][:3]

    from app.core import approval_chain as _ac
    _ac.ensure_schema(db)
    chain = _ac.get_chain(db, "purchase_requisition", pr_no)
    if not chain and pr["status"] == "Pending":
        # Show the ladder BEFORE the first signature, so the reviewer knows how
        # many approvals this requisition will need rather than discovering it
        # after signing.
        steps = _ac.tier_for(db, float(pr.get("estimated_value") or 0), cid)
        chain = [{"step_no": i, "required_role": r, "status": "Pending",
                  "approved_by": None, "approved_at": None}
                 for i, r in enumerate(steps, start=1)]

    return render(request, "purchase_req/detail.html", {
        "pr": pr, "lines": lines, "suppliers": suppliers,
        "suggestions": suggestions,
        "approval_chain": chain,
        "page_title": f"Requisition {pr_no}",
    })


# ---------------------------------------------------------------------------
# Review actions
# ---------------------------------------------------------------------------
@router.post("/{pr_no}/approve")
async def pr_approve(request: Request, pr_no: str, db: Session = Depends(get_db)):
    """Procurement approves the REQUEST. This still does not buy anything —
    approval only means the requisition is legitimate and may be converted
    into a Purchase Order. Conversion is a separate, explicit action."""
    require_action(request, "purchase_requisition", "edit")
    ensure_schema(db)
    cid = _cid(request)
    form = await request.form()

    pr = _one(db, "SELECT * FROM purchase_requisitions WHERE pr_no = :p", {"p": pr_no})
    if not pr:
        return RedirectResponse("/purchase-requisitions?toast=danger&title=Not found&msg=Requisition not found",
                                status_code=303)
    if pr["status"] != "Pending":
        return RedirectResponse(
            f"/purchase-requisitions/{pr_no}?toast=warning&title=Already reviewed"
            f"&msg=This requisition is already {pr['status']}.", status_code=303)

    # ------------------------------------------------------------------
    # Batch 111 — approval hierarchy by value.
    #
    # Until now one signature approved a requisition of any size. The chain
    # is built from the requisition's own estimated value, so a small one
    # still clears in a single step and nothing slows down, while a large one
    # now needs every step in its tier, signed by different people, in order.
    # ------------------------------------------------------------------
    from app.core import approval_chain as _ac
    chain = _ac.build_chain(db, "purchase_requisition", pr_no,
                            float(pr.get("estimated_value") or 0), cid)
    ok, why = _ac.can_approve(chain,
                              request.session.get("user_id"),
                              _user(request),
                              request.session.get("user_role") or request.session.get("role") or "",
                              raised_by=pr.get("requested_by") or "")
    if not ok:
        return RedirectResponse(
            f"/purchase-requisitions/{pr_no}?toast=warning&title={quote('Cannot approve yet')}"
            f"&msg={quote(why)}", status_code=303)

    ok, msg, chain = _ac.approve_step(
        db, "purchase_requisition", pr_no,
        request.session.get("user_id"), _user(request),
        request.session.get("user_role") or request.session.get("role") or "",
        note=(form.get("reason") or "").strip())

    # Only the FINAL signature moves the requisition to Approved. An
    # intermediate one records the step and leaves it Pending, which is what
    # makes the tier meaningful rather than decorative.
    if not _ac.is_complete(chain):
        return RedirectResponse(
            f"/purchase-requisitions/{pr_no}?toast=success&title={quote('Step Approved')}"
            f"&msg={quote(msg)}", status_code=303)

    # Per-line approved quantity — Procurement can approve less than asked
    # (partial approval is standard practice: budget, MOQ, or a pending
    # delivery already covering part of it). Blank means "as requested".
    line_ids = form.getlist("line_id")
    appr_qtys = form.getlist("approved_qty")
    for i, lid in enumerate(line_ids):
        raw = appr_qtys[i] if i < len(appr_qtys) else ""
        try:
            val = float(raw) if str(raw).strip() != "" else None
        except ValueError:
            val = None
        db.execute(text(
            "UPDATE purchase_requisition_lines SET approved_qty = :q WHERE id = :i AND pr_no = :p"
        ), {"q": val, "i": int(lid), "p": pr_no})

    db.execute(text("""
        UPDATE purchase_requisitions
        SET status = 'Approved', reviewed_by = :by, reviewed_at = :at, review_reason = :r
        WHERE pr_no = :p
    """), {"by": _user(request), "at": datetime.utcnow(),
           "r": (form.get("reason") or "").strip()[:500] or None, "p": pr_no})
    db.commit()

    notify_role(db, company_id=cid, role="ADMIN",
                title=f"Requisition {pr_no} approved",
                message=f"Approved by {_user(request)} — ready to convert to a Purchase Order.",
                url=f"/purchase-requisitions/{pr_no}", category="pr_approved")

    return RedirectResponse(
        f"/purchase-requisitions/{pr_no}?toast=success&title=Approved"
        f"&msg={pr_no} approved. Convert it to a Purchase Order when supplier and pricing are settled.",
        status_code=303)


@router.post("/{pr_no}/reject")
async def pr_reject(request: Request, pr_no: str, db: Session = Depends(get_db)):
    require_action(request, "purchase_requisition", "edit")
    ensure_schema(db)
    form = await request.form()
    reason = (form.get("reason") or "").strip()
    if not reason:
        return RedirectResponse(
            f"/purchase-requisitions/{pr_no}?toast=warning&title=Reason required"
            "&msg=Give a reason so the requester knows what to do next.", status_code=303)

    pr = _one(db, "SELECT * FROM purchase_requisitions WHERE pr_no = :p", {"p": pr_no})
    if not pr or pr["status"] != "Pending":
        return RedirectResponse(
            f"/purchase-requisitions/{pr_no}?toast=warning&title=Not pending"
            "&msg=Only a pending requisition can be rejected.", status_code=303)

    db.execute(text("""
        UPDATE purchase_requisitions
        SET status = 'Rejected', reviewed_by = :by, reviewed_at = :at, review_reason = :r
        WHERE pr_no = :p
    """), {"by": _user(request), "at": datetime.utcnow(), "r": reason[:500], "p": pr_no})
    db.commit()

    # Batch 111: clear any partial approvals so a resubmission starts from
    # step 1 instead of inheriting signatures given to a version that was
    # then rejected.
    from app.core import approval_chain as _ac
    _ac.reset_chain(db, "purchase_requisition", pr_no)
    return RedirectResponse(
        f"/purchase-requisitions/{pr_no}?toast=warning&title=Rejected"
        f"&msg={pr_no} rejected — no Purchase Order will be raised from it.", status_code=303)


# ---------------------------------------------------------------------------
# Conversion to real Purchase Order(s)
# ---------------------------------------------------------------------------
@router.post("/{pr_no}/convert-to-po")
async def pr_convert(request: Request, pr_no: str, db: Session = Depends(get_db)):
    """Approved requisition -> real Purchase Order(s), grouped by supplier.

    Deliberately reuses Procurement's own schema helper and PO numbering
    rather than duplicating them, so a PO born from a requisition is
    indistinguishable from one typed in by hand: same tables, same numbering,
    same GRN / Incoming QC / AP / GL path afterwards.
    """
    require_action(request, "procurement", "add")
    ensure_schema(db)
    from app.modules.procurement.routes import _ensure_procurement_schema, _next_no as _po_next_no
    _ensure_procurement_schema(db)

    cid = _cid(request)
    form = await request.form()

    pr = _one(db, "SELECT * FROM purchase_requisitions WHERE pr_no = :p", {"p": pr_no})
    if not pr:
        return RedirectResponse("/purchase-requisitions?toast=danger&title=Not found&msg=Requisition not found",
                                status_code=303)
    if pr["status"] != "Approved":
        return RedirectResponse(
            f"/purchase-requisitions/{pr_no}?toast=warning&title=Not approved"
            "&msg=A requisition must be approved before it can become a Purchase Order.",
            status_code=303)

    lines = _rows(db, "SELECT * FROM purchase_requisition_lines WHERE pr_no = :p ORDER BY line_no",
                  {"p": pr_no})
    if not lines:
        return RedirectResponse(
            f"/purchase-requisitions/{pr_no}?toast=warning&title=No lines&msg=Nothing to convert.",
            status_code=303)

    # Supplier assignment happens HERE, at conversion — this is the point at
    # which the decision "who are we actually buying from" gets made, and it
    # belongs to Procurement, not to whoever spotted the shortage.
    override_sup = form.getlist("line_supplier")
    override_price = form.getlist("line_price")
    line_ids = form.getlist("line_id")
    sup_by_id: dict[int, str] = {}
    price_by_id: dict[int, float] = {}
    for i, lid in enumerate(line_ids):
        try:
            key = int(lid)
        except ValueError:
            continue
        if i < len(override_sup) and (override_sup[i] or "").strip():
            sup_by_id[key] = override_sup[i].strip()
        if i < len(override_price):
            try:
                price_by_id[key] = float(override_price[i] or 0)
            except ValueError:
                pass

    groups: dict[str, list[dict]] = {}
    for l in lines:
        qty = l["approved_qty"] if l["approved_qty"] is not None else l["required_qty"]
        qty = float(qty or 0)
        if qty <= 0:
            continue  # approved down to zero = deliberately excluded
        supplier = sup_by_id.get(l["id"]) or (l["suggested_supplier"] or "").strip() or "Unassigned Supplier"
        price = price_by_id.get(l["id"], float(l["estimated_price"] or 0))
        groups.setdefault(supplier, []).append({**l, "_qty": qty, "_price": price})

    if not groups:
        return RedirectResponse(
            f"/purchase-requisitions/{pr_no}?toast=warning&title=Nothing to order"
            "&msg=Every line was approved at zero quantity.", status_code=303)

    created: list[str] = []
    for supplier, items in groups.items():
        po_no = _po_next_no(db, "purchase_orders", "po_no", "PO")
        total = round(sum(i["_qty"] * i["_price"] for i in items), 4)
        db.execute(text("""
            INSERT INTO purchase_orders (company_id, po_no, po_date, supplier_code, supplier_name,
                                         expected_date, status, total_value, remarks, created_by)
            VALUES (:cid, :po, :d, '', :sn, :ed, 'Open', :tv, :rm, :by)
        """), {
            "cid": cid, "po": po_no, "d": date.today(), "sn": supplier,
            "ed": (pr["required_date"] or (date.today() + timedelta(days=3))),
            "tv": total, "rm": f"Converted from requisition {pr_no}", "by": _user(request),
        })
        for i, it in enumerate(items, start=1):
            db.execute(text("""
                INSERT INTO purchase_order_lines (company_id, po_no, line_no, inventory_code,
                                                  item_name, ordered_qty, uom, unit_price, line_value)
                VALUES (:cid, :po, :ln, :code, :name, :qty, :uom, :price, :val)
            """), {
                "cid": cid, "po": po_no, "ln": i, "code": it["inventory_code"],
                "name": it["item_name"], "qty": it["_qty"], "uom": it["uom"] or "",
                "price": it["_price"], "val": round(it["_qty"] * it["_price"], 4),
            })
        created.append(po_no)

    db.execute(text("""
        UPDATE purchase_requisitions
        SET status = 'Converted', converted_po_nos = :pos, converted_at = :at
        WHERE pr_no = :p
    """), {"pos": ", ".join(created)[:500], "at": datetime.utcnow(), "p": pr_no})
    db.commit()

    return RedirectResponse(
        f"/purchase-requisitions/{pr_no}?toast=success&title=Purchase Order(s) Created"
        f"&msg={pr_no} converted into {len(created)} PO(s): {', '.join(created)}.",
        status_code=303)
