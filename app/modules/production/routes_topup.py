# app/modules/production/routes_topup.py
# =============================================================================
# Batch 94 — STORE ISSUANCE TOP-UP REQUESTS
# -----------------------------------------------------------------------------
# The gap: store issuance is calculated once from the BOM and finalized. When
# a kitchen section then burns more than it was issued — spillage, a failed
# batch, a yield that came in under standard, a portion count revised upward
# mid-shift — there was no route to ask for more. In practice that means
# someone walks to the store and takes it, and the ledger quietly stops
# matching the physical shelf. Unrecorded issue is the single fastest way to
# make inventory valuation meaningless.
#
# THE DESIGN DECISION (this was the open question — here is the answer taken,
# and it is all in this one file if you want it different):
#
#   A top-up is a NEW REQUEST against the same order, not an edit of the
#   original issuance line. The original line is left exactly as it was.
#
#   Why: the original line is the BOM's answer to "what should this order
#   have consumed". Editing it destroys the only baseline that makes yield
#   and wastage reporting possible — after three silent edits, nobody can
#   tell whether the kitchen used 12kg because the recipe says 12kg or
#   because someone raised it three times. Keeping the original intact and
#   recording top-ups separately means variance stays measurable, and the
#   pattern "Hot Kitchen tops up chicken on 40% of orders" becomes visible
#   instead of being absorbed into the baseline.
#
#   Flow:  Section requests  ->  Store approves/rejects  ->  on approve, a
#          real STORE_ISSUE stock movement posts and the top-up is Issued.
#
#   Stock is checked at approval time, not request time — the store keeper
#   is the one who can see the shelf, and stock moves between the two.
# =============================================================================
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.templates import render
from app.core.rbac import require_area, require_action
from app.core.notifications import notify_role
from app.core.stock_ledger import post_stock_movement
from app.database.session import get_db

router = APIRouter(prefix="/production", tags=["Production"])

SECTIONS = ["Cutting", "Butchery", "Hot Kitchen", "Cold Kitchen", "Bakery/Pastry", "Trayline/Packing"]

REASONS = [
    "Spillage / dropped",
    "Failed batch — remake",
    "Yield below standard",
    "Portion count revised up",
    "Quality rejection at section",
    "Other (see notes)",
]


def _cid(request: Request) -> int:
    return int(request.session.get("company_id") or 1)


def _user(request: Request) -> str:
    return request.session.get("username", "system")


def ensure_schema(db: Session) -> None:
    try:
        db.execute(text("""
            CREATE TABLE IF NOT EXISTS store_topup_requests (
                id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                company_id INT NULL,
                topup_no VARCHAR(40) NOT NULL UNIQUE,
                order_no VARCHAR(80) NOT NULL,
                section VARCHAR(100) NULL,
                ingredient_code VARCHAR(80) NOT NULL,
                ingredient_name VARCHAR(255) NULL,
                uom VARCHAR(50) NULL,
                originally_issued DECIMAL(18,6) NOT NULL DEFAULT 0,
                requested_qty DECIMAL(18,6) NOT NULL DEFAULT 0,
                approved_qty DECIMAL(18,6) NULL,
                reason VARCHAR(120) NULL,
                notes VARCHAR(500) NULL,
                status VARCHAR(20) NOT NULL DEFAULT 'Pending',
                requested_by VARCHAR(120) NULL,
                requested_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                decided_by VARCHAR(120) NULL,
                decided_at DATETIME NULL,
                decision_note VARCHAR(500) NULL,
                KEY idx_topup_order (order_no),
                KEY idx_topup_status (status),
                KEY idx_topup_section (section)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """))
        db.commit()
    except Exception:
        db.rollback()


def _next_no(db: Session) -> str:
    row = db.execute(text("SELECT topup_no FROM store_topup_requests ORDER BY id DESC LIMIT 1")).first()
    n = 0
    if row and row[0]:
        try:
            n = int(str(row[0]).split("-")[-1])
        except Exception:
            n = 0
    return f"TOP-{n + 1:06d}"


def _rows_safe(db: Session, sql: str, params: dict) -> list[dict]:
    """Query that degrades to an empty list rather than 500-ing the screen.

    These feed dropdowns. A missing table on an older schema should cost you
    the convenience of the picker, not the whole page.
    """
    try:
        return [dict(r) for r in db.execute(text(sql), params).mappings().all()]
    except Exception:
        return []


def _on_hand(db: Session, code: str, cid: int) -> float:
    """Ledger only, QC-cleared only — same rule as everywhere else."""
    return float(db.execute(text("""
        SELECT COALESCE(SUM(
                 CASE WHEN qc_status IN ('Pending','Failed') THEN 0 ELSE COALESCE(qty_in,0) END
               ), 0) - COALESCE(SUM(COALESCE(qty_out,0)), 0)
        FROM inventory_transactions
        WHERE inventory_code = :c AND (company_id = :cid OR company_id IS NULL)
    """), {"c": code, "cid": cid}).scalar() or 0)


# ---------------------------------------------------------------------------
# Queue
# ---------------------------------------------------------------------------
@router.get("/topups")
def topup_queue(request: Request, db: Session = Depends(get_db)):
    require_area(request, "store_issuance")
    ensure_schema(db)
    cid = _cid(request)
    status_f = (request.query_params.get("status") or "Pending").strip()
    order_f = (request.query_params.get("order") or "").strip()

    where = "(company_id = :cid OR company_id IS NULL)"
    params: dict = {"cid": cid}
    if status_f and status_f.lower() != "all":
        where += " AND status = :st"
        params["st"] = status_f
    if order_f:
        where += " AND order_no LIKE :o"
        params["o"] = f"%{order_f}%"

    rows = [dict(r) for r in db.execute(text(f"""
        SELECT * FROM store_topup_requests WHERE {where}
        ORDER BY (status = 'Pending') DESC, id DESC LIMIT 300
    """), params).mappings().all()]

    for r in rows:
        r["on_hand"] = round(_on_hand(db, r["ingredient_code"], cid), 3)
        r["can_cover"] = r["on_hand"] >= float(r["requested_qty"] or 0)

    counts = db.execute(text("""
        SELECT SUM(status = 'Pending')  AS pending,
               SUM(status = 'Issued')   AS issued,
               SUM(status = 'Rejected') AS rejected
        FROM store_topup_requests WHERE (company_id = :cid OR company_id IS NULL)
    """), {"cid": cid}).mappings().first() or {}

    # ------------------------------------------------------------------
    # Batch 105: real pickers instead of free-text boxes.
    #
    # The modal asked the section to TYPE an order number and an ingredient
    # code from memory. A typo silently creates a request against an order
    # that does not exist, or an ingredient that was never issued — and the
    # store keeper only finds out when the approve fails.
    #
    # Running orders = anything already issued from store but not yet
    # dispatched. That is exactly the window in which a top-up can be needed:
    # before issuance there is nothing to top up, after dispatch it is too
    # late.
    # ------------------------------------------------------------------
    running_orders = _rows_safe(db, """
        SELECT DISTINCT o.order_no,
               COALESCE(o.customer_name, '') AS customer_name,
               COALESCE(o.status, '')        AS status,
               o.required_delivery_date      AS delivery_date
        FROM customer_orders o
        JOIN store_issuance_lines s ON s.order_no = o.order_no
        WHERE (o.company_id = :cid OR o.company_id IS NULL)
          AND COALESCE(o.status, '') NOT IN ('Delivered', 'Cancelled', 'Rejected')
        ORDER BY o.required_delivery_date DESC, o.order_no DESC
        LIMIT 300
    """, {"cid": cid})

    # Ingredients actually issued to the selected orders — a section can only
    # top up something it was already given, so offering the whole 1,429-item
    # master would be noise.
    issued_items = _rows_safe(db, """
        SELECT DISTINCT s.order_no, s.ingredient_code,
               COALESCE(s.ingredient_name, s.ingredient_code) AS ingredient_name,
               COALESCE(s.standard_uom, '')    AS uom,
               COALESCE(s.issue_to_section, '') AS section
        FROM store_issuance_lines s
        JOIN customer_orders o ON o.order_no = s.order_no
        WHERE (o.company_id = :cid OR o.company_id IS NULL)
          AND COALESCE(o.status, '') NOT IN ('Delivered', 'Cancelled', 'Rejected')
        ORDER BY s.order_no, ingredient_name
        LIMIT 4000
    """, {"cid": cid})

    return render(request, "production/topups.html", {
        "running_orders": running_orders,
        "issued_items": issued_items,
        "rows": rows, "counts": counts, "sections": SECTIONS, "reasons": REASONS,
        "filters": {"status": status_f, "order": order_f},
        "status_options": ["Pending", "Issued", "Rejected", "All"],
        "page_title": "Store Top-Up Requests",
    })


# ---------------------------------------------------------------------------
# Raise
# ---------------------------------------------------------------------------
@router.post("/topups/request")
async def topup_request(request: Request, db: Session = Depends(get_db)):
    """Raised by a kitchen section. Deliberately does NOT move stock — asking
    for material and receiving it are two different events, and only the
    store can perform the second one."""
    require_action(request, "kitchen", "add")
    ensure_schema(db)
    form = await request.form()
    cid = _cid(request)

    order_no = (form.get("order_no") or "").strip()
    code = (form.get("ingredient_code") or "").strip()
    try:
        qty = float(form.get("requested_qty") or 0)
    except ValueError:
        qty = 0

    back = form.get("return_to") or "/production/topups"
    if not order_no or not code or qty <= 0:
        return RedirectResponse(
            f"{back}?toast=warning&title=Incomplete"
            "&msg=Order, ingredient and a quantity greater than zero are all required.",
            status_code=303)

    orig = db.execute(text("""
        SELECT ingredient_name, standard_uom, issue_to_section,
               COALESCE(SUM(issued_qty_standard), 0) AS issued
        FROM store_issuance_lines
        WHERE order_no = :o AND ingredient_code = :c
        GROUP BY ingredient_name, standard_uom, issue_to_section LIMIT 1
    """), {"o": order_no, "c": code}).mappings().first()

    topup_no = _next_no(db)
    db.execute(text("""
        INSERT INTO store_topup_requests
            (company_id, topup_no, order_no, section, ingredient_code, ingredient_name, uom,
             originally_issued, requested_qty, reason, notes, status, requested_by)
        VALUES (:cid, :no, :o, :sec, :code, :name, :uom, :orig, :qty, :reason, :notes, 'Pending', :by)
    """), {
        "cid": cid, "no": topup_no, "o": order_no,
        "sec": (form.get("section") or (orig or {}).get("issue_to_section") or "").strip() or None,
        "code": code, "name": (orig or {}).get("ingredient_name") or code,
        "uom": (orig or {}).get("standard_uom") or "Kg",
        "orig": float((orig or {}).get("issued") or 0), "qty": qty,
        "reason": (form.get("reason") or "").strip()[:120] or None,
        "notes": (form.get("notes") or "").strip()[:500] or None,
        "by": _user(request),
    })
    db.commit()

    notify_role(db, company_id=cid, role="STORE",
                title=f"Top-up requested — {order_no}",
                message=f"{(orig or {}).get('ingredient_name') or code}: {qty} requested by {_user(request)}",
                url="/production/topups", category="topup_requested")

    return RedirectResponse(
        f"{back}?toast=success&title=Requested"
        f"&msg={topup_no} sent to Store. Material moves only once Store approves it.",
        status_code=303)


# ---------------------------------------------------------------------------
# Decide
# ---------------------------------------------------------------------------
@router.post("/topups/{topup_no}/approve")
async def topup_approve(request: Request, topup_no: str, db: Session = Depends(get_db)):
    """Store approves and issues. THIS is where stock actually moves — one
    real STORE_ISSUE movement, so the ledger reflects what physically left
    the store rather than only what the BOM predicted would."""
    require_action(request, "store_issuance", "edit")
    ensure_schema(db)
    cid = _cid(request)
    form = await request.form()

    row = db.execute(text("SELECT * FROM store_topup_requests WHERE topup_no = :t"),
                     {"t": topup_no}).mappings().first()
    if not row:
        return RedirectResponse("/production/topups?toast=danger&title=Not found&msg=Request not found",
                                status_code=303)
    if row["status"] != "Pending":
        return RedirectResponse(
            f"/production/topups?toast=warning&title=Already decided"
            f"&msg={topup_no} is already {row['status']}.", status_code=303)

    raw = (form.get("approved_qty") or "").strip()
    try:
        qty = float(raw) if raw else float(row["requested_qty"] or 0)
    except ValueError:
        qty = float(row["requested_qty"] or 0)
    if qty <= 0:
        return RedirectResponse(
            f"/production/topups?toast=warning&title=Nothing to issue"
            "&msg=Approve a quantity greater than zero, or reject the request.", status_code=303)

    available = _on_hand(db, row["ingredient_code"], cid)
    if qty > available + 0.0001:
        # Hard block. Issuing stock that isn't there produces a negative
        # balance, which then silently corrupts valuation and every shortage
        # check that reads this ledger.
        return RedirectResponse(
            f"/production/topups?toast=danger&title=Not enough stock"
            f"&msg=Only {round(available, 3)} {row['uom']} of {row['ingredient_code']} on hand. "
            "Issue less, or raise a purchase requisition.", status_code=303)

    ok = post_stock_movement(
        db, company_id=cid, inventory_code=row["ingredient_code"],
        item_name=row["ingredient_name"], uom=row["uom"] or "", qty=qty,
        movement_type="STORE_ISSUE", reference_no=row["order_no"],
        remarks=f"Top-up {topup_no} to {row['section'] or 'kitchen'} — {row['reason'] or 'no reason given'}",
        created_by=_user(request),
    )
    if not ok:
        # Never mark it Issued when the ledger write failed — that is exactly
        # the Batch 23 failure mode where a PO showed RECEIVED with zero stock.
        return RedirectResponse(
            f"/production/topups?toast=danger&title=Ledger write failed"
            f"&msg=Stock movement for {topup_no} did not post. Nothing was issued; check the inventory ledger.",
            status_code=303)

    db.execute(text("""
        UPDATE store_topup_requests
        SET status = 'Issued', approved_qty = :q, decided_by = :by, decided_at = :at, decision_note = :n
        WHERE topup_no = :t
    """), {"q": qty, "by": _user(request), "at": datetime.utcnow(),
           "n": (form.get("decision_note") or "").strip()[:500] or None, "t": topup_no})
    db.commit()

    return RedirectResponse(
        f"/production/topups?toast=success&title=Issued"
        f"&msg={qty} {row['uom']} of {row['ingredient_code']} issued against {row['order_no']}.",
        status_code=303)


@router.post("/topups/{topup_no}/reject")
async def topup_reject(request: Request, topup_no: str, db: Session = Depends(get_db)):
    require_action(request, "store_issuance", "edit")
    ensure_schema(db)
    form = await request.form()
    note = (form.get("decision_note") or "").strip()
    if not note:
        return RedirectResponse(
            "/production/topups?toast=warning&title=Reason required"
            "&msg=Tell the section why, so they know what to do instead.", status_code=303)

    db.execute(text("""
        UPDATE store_topup_requests
        SET status = 'Rejected', decided_by = :by, decided_at = :at, decision_note = :n
        WHERE topup_no = :t AND status = 'Pending'
    """), {"by": _user(request), "at": datetime.utcnow(), "n": note[:500], "t": topup_no})
    db.commit()
    return RedirectResponse(
        f"/production/topups?toast=warning&title=Rejected&msg={topup_no} rejected — no material issued.",
        status_code=303)
