# app/modules/subscriptions/routes.py
# =============================================================================
# Batch 76 — Subscriptions / Recurring Orders
# -----------------------------------------------------------------------------
# A subscription is a template for repeating customer orders: a customer, a
# fixed set of recipes/portions, and a cadence (Weekly on chosen weekdays, or
# Monthly on chosen day-of-month). "Generate Due Orders" walks every Active
# subscription and creates REAL customer_orders — through the same
# create_order() service used by manual order entry — for every occurrence
# that:
#   - falls within the generation window (today .. today+GEN_WINDOW_DAYS)
#   - is at least 48 hours away (the same rule manual order entry enforces)
#   - has not already been generated (subscription_orders has a UNIQUE key on
#     (subscription_id, delivery_date), so re-running is always safe)
#
# There is no cron/worker in this stack (FastAPI + Laragon on Windows), so
# generation is a manual button by design — click "Generate Due Orders Now"
# from the dashboard, or point a Windows Task Scheduler job / cron entry at
#     POST /subscriptions/generate-due
# once a day. Because generation is idempotent, running it more or less often
# never creates duplicates or skips a due date within the window.
#
# Tables auto-create (ensure_schema), matching the pattern used by HR/Finance
# in this codebase — every statement is tolerant so a partially migrated DB
# never 500s.
#
# Registered in main.py:
#     from app.modules.subscriptions.routes import router as subscriptions_router
#     app.include_router(subscriptions_router)
# =============================================================================
from __future__ import annotations

from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.templates import render
from app.core.rbac import require_area, require_action
from app.database.session import get_db
from app.services.production_service import create_order
from app.schemas.production import CustomerOrderCreate, OrderLineIn

router = APIRouter(prefix="/subscriptions", tags=["Subscriptions"])

FREQUENCIES = ["Weekly", "Monthly"]
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
GEN_WINDOW_DAYS = 21     # how far ahead "Generate Due Orders" looks
MIN_LEAD_HOURS = 48      # same 48-hour rule enforced on manual order entry


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
def ensure_schema(db: Session) -> None:
    stmts = [
        """CREATE TABLE IF NOT EXISTS customer_subscriptions (
            id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
            company_id INT NULL,
            subscription_no VARCHAR(30) NOT NULL UNIQUE,
            customer_no VARCHAR(80) NULL,
            customer_name VARCHAR(255) NOT NULL,
            brand VARCHAR(100) NULL,
            channel VARCHAR(100) NULL,
            kitchen VARCHAR(100) NULL,
            plan_name VARCHAR(150) NULL,
            frequency VARCHAR(20) NOT NULL DEFAULT 'Weekly',
            delivery_days VARCHAR(100) NULL,
            delivery_time VARCHAR(20) NULL,
            start_date DATE NOT NULL,
            end_date DATE NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'Active',
            paused_at DATETIME NULL,
            paused_by VARCHAR(120) NULL,
            pause_reason VARCHAR(300) NULL,
            resume_date DATE NULL,
            last_generated_date DATE NULL,
            created_by VARCHAR(120) NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",
        """CREATE TABLE IF NOT EXISTS subscription_lines (
            id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
            subscription_id INT NOT NULL,
            recipe_no VARCHAR(50) NOT NULL,
            recipe_name VARCHAR(255) NULL,
            portions FLOAT NOT NULL DEFAULT 0,
            selling_price_per_portion FLOAT NOT NULL DEFAULT 0,
            KEY idx_sl_sub (subscription_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",
        """CREATE TABLE IF NOT EXISTS subscription_orders (
            id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
            subscription_id INT NOT NULL,
            order_no VARCHAR(80) NULL,
            delivery_date DATE NOT NULL,
            generated_date DATETIME DEFAULT CURRENT_TIMESTAMP,
            status VARCHAR(20) NOT NULL DEFAULT 'Generated',
            remarks VARCHAR(300) NULL,
            KEY idx_so_sub (subscription_id),
            UNIQUE KEY uq_sub_delivery (subscription_id, delivery_date)
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _cid(request: Request) -> int:
    try:
        return int(request.session.get("company_id") or 1)
    except Exception:
        return 1


def _user(request: Request) -> str:
    return request.session.get("username") or "system"


def _rows(db: Session, sql: str, params: dict | None = None) -> list[dict]:
    try:
        return [dict(r) for r in db.execute(text(sql), params or {}).mappings().all()]
    except Exception:
        return []


def _one(db: Session, sql: str, params: dict | None = None) -> dict | None:
    try:
        r = db.execute(text(sql), params or {}).mappings().first()
        return dict(r) if r else None
    except Exception:
        return None


def _next_sub_no(db: Session) -> str:
    n = 0
    try:
        n = int(db.execute(text("SELECT COUNT(*) FROM customer_subscriptions")).scalar() or 0)
    except Exception:
        pass
    return f"SUB-{n + 1:05d}"


def _due_dates(sub: dict, window_start: date, window_end: date) -> list[date]:
    """Every calendar date in [window_start, window_end] that matches this
    subscription's cadence, clipped to its own start/end_date."""
    freq = (sub.get("frequency") or "Weekly").strip()
    raw_days = (sub.get("delivery_days") or "").strip()
    sub_start = sub.get("start_date")
    sub_end = sub.get("end_date")
    if isinstance(sub_start, str):
        sub_start = datetime.strptime(sub_start, "%Y-%m-%d").date()
    if isinstance(sub_end, str) and sub_end:
        sub_end = datetime.strptime(sub_end, "%Y-%m-%d").date()

    lo = max(window_start, sub_start) if sub_start else window_start
    hi = min(window_end, sub_end) if sub_end else window_end
    if lo > hi:
        return []

    out: list[date] = []
    if freq == "Monthly":
        wanted_days = set()
        for part in raw_days.split(","):
            part = part.strip()
            if part.isdigit():
                wanted_days.add(int(part))
        d = lo
        while d <= hi:
            import calendar as _cal
            last_day = _cal.monthrange(d.year, d.month)[1]
            for wd in wanted_days:
                target = min(wd, last_day)
                if d.day == target:
                    out.append(d)
                    break
            d += timedelta(days=1)
    else:  # Weekly
        wanted = {w.strip()[:3] for w in raw_days.split(",") if w.strip()}
        d = lo
        while d <= hi:
            if WEEKDAYS[d.weekday()] in wanted:
                out.append(d)
            d += timedelta(days=1)
    return out


def _generate_for_subscription(db: Session, sub: dict, company_id: int, user: str) -> dict:
    """Create real customer_orders for every due-and-not-yet-generated date
    for ONE subscription. Returns a small result summary dict."""
    today = date.today()
    window_end = today + timedelta(days=GEN_WINDOW_DAYS)
    cutoff = datetime.now() + timedelta(hours=MIN_LEAD_HOURS)

    lines_raw = _rows(db, "SELECT recipe_no, recipe_name, portions, selling_price_per_portion "
                          "FROM subscription_lines WHERE subscription_id=:i", {"i": sub["id"]})
    if not lines_raw:
        return {"created": 0, "skipped": 0, "failed": 0}

    already = {r["delivery_date"] for r in _rows(
        db, "SELECT delivery_date FROM subscription_orders WHERE subscription_id=:i", {"i": sub["id"]})}

    created = skipped = failed = 0
    for d in _due_dates(sub, today, window_end):
        if d in already:
            continue
        deliv_dt = datetime(d.year, d.month, d.day, 9, 0)
        try:
            hh, mm = (int(x) for x in (sub.get("delivery_time") or "09:00")[:5].split(":")[:2])
            deliv_dt = datetime(d.year, d.month, d.day, hh, mm)
        except Exception:
            pass
        if deliv_dt < cutoff:
            skipped += 1
            continue

        payload = CustomerOrderCreate(
            customer_no=sub.get("customer_no"),
            customer_name=sub["customer_name"],
            brand=sub.get("brand"),
            channel=sub.get("channel"),
            kitchen=sub.get("kitchen"),
            order_type="Subscription",
            priority="Normal",
            required_delivery_date=d,
            required_delivery_time=sub.get("delivery_time") or None,
            notes=f"Auto-generated from subscription {sub['subscription_no']}",
            lines=[OrderLineIn(recipe_no=l["recipe_no"], recipe_name=l.get("recipe_name"),
                               required_portions=float(l["portions"] or 0),
                               selling_price_per_portion=float(l.get("selling_price_per_portion") or 0))
                   for l in lines_raw if float(l["portions"] or 0) > 0],
        )
        if not payload.lines:
            skipped += 1
            continue
        try:
            order = create_order(db, payload, created_by=f"{user} (subscription)", company_id=company_id)
            db.execute(text("""
                INSERT INTO subscription_orders (subscription_id, order_no, delivery_date, status)
                VALUES (:s, :o, :d, 'Generated')
            """), {"s": sub["id"], "o": order.order_no, "d": d})
            db.commit()
            created += 1
        except ValueError as exc:
            db.rollback()
            try:
                db.execute(text("""
                    INSERT INTO subscription_orders (subscription_id, order_no, delivery_date, status, remarks)
                    VALUES (:s, NULL, :d, 'Failed', :r)
                    ON DUPLICATE KEY UPDATE status='Failed', remarks=:r
                """), {"s": sub["id"], "d": d, "r": str(exc)[:290]})
                db.commit()
            except Exception:
                db.rollback()
            failed += 1

    if created:
        db.execute(text("UPDATE customer_subscriptions SET last_generated_date=:d WHERE id=:i"),
                  {"d": today, "i": sub["id"]})
        db.commit()
    return {"created": created, "skipped": skipped, "failed": failed}


def _next_due_date(db: Session, sub: dict) -> date | None:
    today = date.today()
    due = _due_dates(sub, today, today + timedelta(days=GEN_WINDOW_DAYS * 3))
    return due[0] if due else None


# ---------------------------------------------------------------------------
# Dashboard / list
# ---------------------------------------------------------------------------
@router.get("")
def subscriptions_dashboard(request: Request, db: Session = Depends(get_db)):
    require_area(request, "subscriptions")
    ensure_schema(db)
    cid = _cid(request)
    status_filter = (request.query_params.get("status") or "").strip()

    where = "(company_id=:c OR company_id IS NULL)"
    params = {"c": cid}
    if status_filter:
        where += " AND status=:st"
        params["st"] = status_filter

    subs = _rows(db, f"""
        SELECT * FROM customer_subscriptions WHERE {where}
        ORDER BY FIELD(status,'Active','Paused','Cancelled'), created_at DESC LIMIT 300
    """, params)

    for s in subs:
        s["line_count"] = int(_one(db, "SELECT COUNT(*) AS n FROM subscription_lines WHERE subscription_id=:i",
                                   {"i": s["id"]})["n"])
        s["orders_generated"] = int(_one(db, "SELECT COUNT(*) AS n FROM subscription_orders "
                                             "WHERE subscription_id=:i AND status='Generated'", {"i": s["id"]})["n"])
        s["next_due"] = _next_due_date(db, s) if s["status"] == "Active" else None

    kpis = {
        "active": len([s for s in subs if s["status"] == "Active"]),
        "paused": len([s for s in subs if s["status"] == "Paused"]),
        "cancelled": len([s for s in subs if s["status"] == "Cancelled"]),
        "orders_this_month": int(_one(db, """
            SELECT COUNT(*) AS n FROM subscription_orders
            WHERE status='Generated' AND YEAR(generated_date)=YEAR(CURDATE()) AND MONTH(generated_date)=MONTH(CURDATE())
        """)["n"]),
    }

    return render(request, "subscriptions/list.html", {
        "subs": subs, "kpis": kpis, "status_filter": status_filter,
        "page_title": "Subscriptions",
    })


@router.post("/generate-due")
def generate_due(request: Request, db: Session = Depends(get_db)):
    require_action(request, "subscriptions", "edit")
    ensure_schema(db)
    cid = _cid(request)
    user = _user(request)
    active = _rows(db, "SELECT * FROM customer_subscriptions WHERE status='Active' AND (company_id=:c OR company_id IS NULL)", {"c": cid})

    totals = {"created": 0, "skipped": 0, "failed": 0, "subscriptions": len(active)}
    for sub in active:
        r = _generate_for_subscription(db, sub, cid, user)
        totals["created"] += r["created"]
        totals["skipped"] += r["skipped"]
        totals["failed"] += r["failed"]

    msg = f"{totals['created']} order(s) generated across {totals['subscriptions']} active subscription(s)"
    if totals["failed"]:
        msg += f", {totals['failed']} failed (see subscription history)"
    kind = "success" if totals["created"] or not totals["failed"] else "warning"
    return RedirectResponse(f"/subscriptions?toast={kind}&title=Generate+Due+Orders&msg={msg}", status_code=303)


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------
@router.get("/new")
def new_subscription_form(request: Request, db: Session = Depends(get_db)):
    require_area(request, "subscriptions")
    ensure_schema(db)
    customers = _rows(db, "SELECT customer_code, customer_name, COALESCE(brand,'') AS brand "
                          "FROM customers ORDER BY customer_name LIMIT 1000")
    recipes = _rows(db, "SELECT recipe_code, recipe_name, COALESCE(sale_price_per_portion,0) AS price "
                        "FROM recipes WHERE UPPER(TRIM(status))='ACTIVE' AND is_active=1 "
                        "GROUP BY recipe_code ORDER BY recipe_name LIMIT 1000")
    return render(request, "subscriptions/form.html", {
        "customers": customers, "recipes": recipes,
        "frequencies": FREQUENCIES, "weekdays": WEEKDAYS,
        "page_title": "New Subscription", "error": request.query_params.get("error"),
    })


@router.post("/new")
async def create_subscription(request: Request, db: Session = Depends(get_db)):
    require_action(request, "subscriptions", "add")
    ensure_schema(db)
    form = await request.form()

    customer_name = (form.get("customer_name") or "").strip()
    if not customer_name:
        return RedirectResponse("/subscriptions/new?error=Customer is required", status_code=303)

    frequency = form.get("frequency") or "Weekly"
    if frequency not in FREQUENCIES:
        frequency = "Weekly"
    if frequency == "Weekly":
        chosen_days = form.getlist("weekday")
        delivery_days = ",".join(chosen_days)
    else:
        chosen_days = form.getlist("month_day")
        delivery_days = ",".join(chosen_days)
    if not delivery_days:
        return RedirectResponse("/subscriptions/new?error=Choose at least one delivery day", status_code=303)

    start_date = (form.get("start_date") or "").strip()
    if not start_date:
        return RedirectResponse("/subscriptions/new?error=Start date is required", status_code=303)
    end_date = (form.get("end_date") or "").strip() or None

    recipe_no = form.getlist("recipe_no")
    recipe_name = form.getlist("recipe_name")
    portions = form.getlist("portions")
    price = form.getlist("selling_price_per_portion")

    lines = []
    for i, rn in enumerate(recipe_no):
        if not rn:
            continue
        try:
            p = float(portions[i]) if i < len(portions) and portions[i] else 0
        except ValueError:
            p = 0
        if p <= 0:
            continue
        try:
            pr = float(price[i]) if i < len(price) and price[i] else 0
        except ValueError:
            pr = 0
        lines.append((rn, recipe_name[i] if i < len(recipe_name) else rn, p, pr))

    if not lines:
        return RedirectResponse("/subscriptions/new?error=Add at least one recipe with portions greater than zero", status_code=303)

    sub_no = _next_sub_no(db)
    cid = _cid(request)
    db.execute(text("""
        INSERT INTO customer_subscriptions
            (company_id, subscription_no, customer_no, customer_name, brand, channel, kitchen,
             plan_name, frequency, delivery_days, delivery_time, start_date, end_date, status, created_by)
        VALUES (:cid, :no, :cno, :cname, :brand, :channel, :kitchen,
                :plan, :freq, :days, :time, :start, :end, 'Active', :by)
    """), {
        "cid": cid, "no": sub_no,
        "cno": (form.get("customer_no") or "").strip() or None,
        "cname": customer_name, "brand": (form.get("brand") or "").strip() or None,
        "channel": (form.get("channel") or "").strip() or None,
        "kitchen": (form.get("kitchen") or "").strip() or None,
        "plan": (form.get("plan_name") or "").strip() or None,
        "freq": frequency, "days": delivery_days,
        "time": (form.get("delivery_time") or "09:00").strip(),
        "start": start_date, "end": end_date, "by": _user(request),
    })
    db.commit()
    sub = _one(db, "SELECT id FROM customer_subscriptions WHERE subscription_no=:n", {"n": sub_no})
    for rn, rname, p, pr in lines:
        db.execute(text("""
            INSERT INTO subscription_lines (subscription_id, recipe_no, recipe_name, portions, selling_price_per_portion)
            VALUES (:s, :rn, :rname, :p, :pr)
        """), {"s": sub["id"], "rn": rn, "rname": rname, "p": p, "pr": pr})
    db.commit()

    return RedirectResponse(f"/subscriptions/{sub['id']}?toast=success&title=Subscription+Created&msg={sub_no} created", status_code=303)


# ---------------------------------------------------------------------------
# Detail + lifecycle actions
# ---------------------------------------------------------------------------
@router.get("/{sub_id}")
def subscription_detail(request: Request, sub_id: int, db: Session = Depends(get_db)):
    require_area(request, "subscriptions")
    ensure_schema(db)
    sub = _one(db, "SELECT * FROM customer_subscriptions WHERE id=:i", {"i": sub_id})
    if not sub:
        return RedirectResponse("/subscriptions?toast=danger&title=Not+found&msg=Subscription not found", status_code=303)
    lines = _rows(db, "SELECT * FROM subscription_lines WHERE subscription_id=:i", {"i": sub_id})
    history = _rows(db, """
        SELECT so.*, COALESCE(co.status,'') AS order_status
        FROM subscription_orders so
        LEFT JOIN customer_orders co ON co.order_no = so.order_no
        WHERE so.subscription_id=:i ORDER BY so.delivery_date DESC LIMIT 100
    """, {"i": sub_id})
    next_due = _next_due_date(db, sub) if sub["status"] == "Active" else None
    return render(request, "subscriptions/detail.html", {
        "sub": sub, "lines": lines, "history": history, "next_due": next_due,
        "page_title": f"Subscription {sub['subscription_no']}",
    })


@router.post("/{sub_id}/pause")
async def pause_subscription(request: Request, sub_id: int, db: Session = Depends(get_db)):
    require_action(request, "subscriptions", "edit")
    ensure_schema(db)
    form = await request.form()
    reason = (form.get("pause_reason") or "").strip() or None
    resume_date = (form.get("resume_date") or "").strip() or None

    sub = _one(db, "SELECT * FROM customer_subscriptions WHERE id=:i", {"i": sub_id})
    if not sub:
        return RedirectResponse("/subscriptions?toast=danger&title=Not+found&msg=Subscription not found", status_code=303)

    # 48-hour rule: warn (don't silently cancel) if a delivery inside the
    # cutoff window has already been generated — that order is already in
    # the kitchen pipeline and must be cancelled separately if needed.
    warn = ""
    cutoff = date.today() + timedelta(days=2)
    upcoming = _one(db, """
        SELECT order_no, delivery_date FROM subscription_orders
        WHERE subscription_id=:i AND status='Generated' AND delivery_date <= :cut AND delivery_date >= CURDATE()
        ORDER BY delivery_date ASC LIMIT 1
    """, {"i": sub_id, "cut": cutoff})
    if upcoming:
        warn = f" Note: order {upcoming['order_no']} for {upcoming['delivery_date']} was already generated (inside the 48-hour cutoff) and will still be delivered — cancel it separately from Production Orders if you don't want it."

    db.execute(text("""
        UPDATE customer_subscriptions
        SET status='Paused', paused_at=:now, paused_by=:by, pause_reason=:r, resume_date=:rd
        WHERE id=:i
    """), {"now": datetime.utcnow(), "by": _user(request), "r": reason, "rd": resume_date, "i": sub_id})
    db.commit()
    return RedirectResponse(
        f"/subscriptions/{sub_id}?toast=warning&title=Paused&msg=Subscription paused.{warn}", status_code=303)


@router.post("/{sub_id}/resume")
async def resume_subscription(request: Request, sub_id: int, db: Session = Depends(get_db)):
    require_action(request, "subscriptions", "edit")
    ensure_schema(db)
    db.execute(text("""
        UPDATE customer_subscriptions
        SET status='Active', paused_at=NULL, paused_by=NULL, pause_reason=NULL, resume_date=NULL
        WHERE id=:i
    """), {"i": sub_id})
    db.commit()
    return RedirectResponse(f"/subscriptions/{sub_id}?toast=success&title=Resumed&msg=Subscription resumed", status_code=303)


@router.post("/{sub_id}/cancel")
async def cancel_subscription(request: Request, sub_id: int, db: Session = Depends(get_db)):
    require_action(request, "subscriptions", "delete")
    ensure_schema(db)
    db.execute(text("UPDATE customer_subscriptions SET status='Cancelled' WHERE id=:i"), {"i": sub_id})
    db.commit()
    return RedirectResponse(f"/subscriptions/{sub_id}?toast=success&title=Cancelled&msg=Subscription cancelled. No further orders will be generated.", status_code=303)
