# app/modules/orders/routes_menu.py
# =============================================================================
# Batch 102 — MENU-DRIVEN CUSTOMERS (the "Frsh weekly plan" flow)
# -----------------------------------------------------------------------------
# The requirement, in your words: pick Frsh as the customer, a Day field
# appears, choosing "Sunday" limits the delivery dates to upcoming Sundays and
# limits the recipe picker to Frsh's Sunday recipes.
#
# WHY IT IS NOT HARD-CODED TO "FRSH"
#
# You said you have more customers like this and want them added one at a time.
# If "Frsh" were written into the code, adding the second one is a code change,
# a batch, and a deployment. Instead a customer is flagged **menu-driven** in
# Master Data, and everything below keys off that flag:
#
#     customers.is_menu_driven = 1
#
# Adding your next weekly-plan customer is then a tickbox, and their recipes
# just need the Day column filled on import. No code, no release.
#
# HOW A CUSTOMER'S MENU IS DISCOVERED
#
# Not from a separate menu table. The recipes themselves already carry both the
# customer and the day (recipes.customer_name + recipes.day_of_week, populated
# from the Day column your Excel already has). So the menu IS the recipe master
# — one source of truth, and uploading a new week's recipes updates the menu
# automatically with nothing else to maintain.
# =============================================================================
from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

import logging

from app.core.rbac import require_area
from app.database.session import get_db

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/menu", tags=["Menu Ordering"])

DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]

# Python's weekday(): Monday=0 .. Sunday=6
_DAY_INDEX = {"Monday": 0, "Tuesday": 1, "Wednesday": 2, "Thursday": 3,
              "Friday": 4, "Saturday": 5, "Sunday": 6}


# =============================================================================
# Batch 117 — MATCHING A WEEKDAY AGAINST REAL MENU DATA
#
# Frsh stores one weekday per recipe: "Sunday". SMC does not. Its 157 recipes
# use six different shapes in the same column:
#
#     Sunday                  a single day
#     Daily / Daily Course    available every day
#     Mon & Fri               two days
#     Sunday & Wed & Thu      three days, mixed full names and abbreviations
#     As per Order            made only on request
#     (blank)                 no menu day
#
# An equality test (day_of_week = 'Monday') matches only the first shape, which
# is why picking a date showed nothing for SMC: 41 of the 157 recipes are
# multi-day or daily, and every one of them was invisible.
#
# So the match is by CONTAINMENT of the three-letter stem, plus an explicit
# rule for "Daily". The stems Sun/Mon/Tue/Wed/Thu/Fri/Sat are mutually
# distinct, so containment cannot produce a false positive between weekdays —
# "Thursday" contains "Thu" and nothing else.
#
# "As per Order" and blanks are deliberately EXCLUDED. A recipe made only on
# request is not on the day's standard menu, and offering it as though it were
# would have the kitchen planning production for something nobody scheduled.
# =============================================================================
DAY_STEM = {
    "Monday": "Mon", "Tuesday": "Tue", "Wednesday": "Wed", "Thursday": "Thu",
    "Friday": "Fri", "Saturday": "Sat", "Sunday": "Sun",
}


def _day_clause(day: str, params: dict, col: str = "r.day_of_week") -> str:
    """SQL that is true when `col` covers the given weekday."""
    stem = DAY_STEM.get((day or "").strip().title())
    if not stem:
        return "1=0"
    params["daystem"] = f"%{stem}%"
    return (f"(COALESCE({col}, '') LIKE :daystem "
            f"OR UPPER(COALESCE({col}, '')) LIKE 'DAILY%')")


def expand_days(raw: str) -> list[str]:
    """Which weekdays a stored value covers — used for the 'days available' hint."""
    v = (raw or "").strip()
    if not v:
        return []
    if v.upper().startswith("DAILY"):
        return list(DAY_STEM)
    return [full for full, stem in DAY_STEM.items() if stem.lower() in v.lower()]


def ensure_schema(db: Session) -> None:
    """Add customers.is_menu_driven if missing.

    information_schema pre-check — ADD COLUMN IF NOT EXISTS is unsupported on
    the target MySQL version. Called from every route here rather than only at
    startup, for the same reason Batch 102 moved the recipes.day_of_week guard
    to import time: a schema assumption that only holds after a successful
    startup is a schema assumption that will eventually 500.
    """
    try:
        has = db.execute(text("""
            SELECT COUNT(*) FROM information_schema.columns
            WHERE table_schema = DATABASE() AND table_name = 'customers'
              AND column_name = 'is_menu_driven'
        """)).scalar()
        if not has:
            # NULLABLE with no default, on purpose. Three states are needed:
            #   NULL -> nobody has decided; auto-detect from the recipes
            #   1    -> explicitly a menu customer
            #   0    -> explicitly NOT one, even if their recipes carry days
            # Creating it NOT NULL DEFAULT 0 (as the first cut did) collapses
            # "undecided" into "explicitly off", so the auto-detect fallback
            # never ran and Frsh still showed as non-menu.
            db.execute(text(
                "ALTER TABLE customers ADD COLUMN is_menu_driven TINYINT(1) NULL DEFAULT NULL"))
            db.commit()
        else:
            # Repair a column created NOT NULL DEFAULT 0 by the first version.
            nn = db.execute(text("""
                SELECT IS_NULLABLE FROM information_schema.columns
                WHERE table_schema = DATABASE() AND table_name = 'customers'
                  AND column_name = 'is_menu_driven'
            """)).scalar()
            if str(nn).upper() == "NO":
                db.execute(text(
                    "ALTER TABLE customers MODIFY COLUMN is_menu_driven TINYINT(1) NULL DEFAULT NULL"))
                db.execute(text(
                    "UPDATE customers SET is_menu_driven = NULL WHERE is_menu_driven = 0"))
                db.commit()
    except Exception:
        db.rollback()


def _cid(request: Request) -> int:
    return int(request.session.get("company_id") or 1)


def menu_keys(db: Session, customer_name: str, brand: str, cid: int) -> list[str]:
    """Every value that recipes.customer_name might hold for this customer.

    BATCH 103 FIX — this is why the Day field never appeared for Frsh.

    Batch 102 matched recipes.customer_name against the CUSTOMER NAME. But in
    the real data those are different things:

        customers.customer_name = "Ma'una Foundation"
        customers.brand         = "FRSH"
        recipes.customer_name   = "Frsh"      <-- actually the BRAND

    So the lookup compared "Ma'una Foundation" to "Frsh", found nothing, and
    the customer was reported as not menu-driven. The feature was correct; it
    was keyed on the wrong column.

    Rather than pick one column and be wrong again, this collects every
    plausible key — customer name, brand, and the customer's own brand from
    the master — and the queries below match on any of them. Comparison is
    case-insensitive because the data has "FRSH" and "Frsh" in different
    places.
    """
    keys: list[str] = []

    # Batch 104: pull the brand out of a parenthetical in the customer name.
    # The real record is  customer_name = "Ma'una Foundation (FRSH)"  with the
    # brand column EMPTY — so neither the name nor the brand column on its own
    # matches recipes.customer_name = "Frsh". The bracketed token is the only
    # place the link exists in the data as it stands.
    import re as _re
    for m in _re.findall(r"\(([^)]{2,40})\)", customer_name or ""):
        m = m.strip()
        if m and m.lower() not in [k.lower() for k in keys]:
            keys.append(m)

    for v in (customer_name, brand):
        v = (v or "").strip()
        if v and v.lower() not in [k.lower() for k in keys]:
            keys.append(v)
    # Batch 116 — CUSTOMER GROUPS.
    #
    # Frsh worked because the brand sat in the customer name as "(FRSH)".
    # SMC does not follow that pattern: six separate customers
    # ("SMC 1 -(Olaya) Cafeteria", "Cafeteria Smc2", "Accounting Department
    # Smc1"...) all order from ONE recipe set stored as customer_name = "SMC".
    #
    # So the group has to be derived from the words in the customer name, not
    # from a bracketed token. Every word is offered as a candidate key, and a
    # trailing digit is stripped — "Smc2" and "SMC1" both become "SMC", which
    # is what the recipes are actually filed under.
    #
    # This is deliberately generous: menu_keys feeds an IN() lookup against
    # recipes.customer_name, so an extra candidate that matches nothing simply
    # does not appear. A missing candidate, by contrast, means the Day field
    # never shows and the whole flow silently degrades to manual typing —
    # which is exactly the Frsh bug from Batch 103, repeated.
    # Generic words are excluded. "Cafeteria", "Company", "Department" appear
    # in many unrelated customer names, so allowing them as group keys would
    # make two customers share a menu purely because both are cafeterias —
    # a silent cross-contamination that is much worse than the Day field not
    # appearing. Brand-like tokens (SMC, FRSH, G360) are short and distinctive;
    # that is what this is meant to catch.
    _GENERIC = {
        "cafeteria", "company", "department", "foundation", "restaurant",
        "kitchen", "catering", "services", "service", "trading", "group",
        "center", "centre", "branch", "main", "office", "hospital", "school",
        "camp", "site", "the", "and", "for", "ltd", "llc", "est", "co",
    }
    for _word in _re.findall(r"[A-Za-z][A-Za-z0-9]{1,}", customer_name or ""):
        if _word.lower() in _GENERIC:
            continue
        for _cand in (_word, _re.sub(r"\d+$", "", _word)):
            _cand = _cand.strip()
            if len(_cand) >= 3 and _cand.lower() not in [k.lower() for k in keys]:
                keys.append(_cand)

    bare = _re.sub(r"\s*\([^)]*\)\s*", " ", customer_name or "").strip()
    if bare and bare.lower() not in [k.lower() for k in keys]:
        keys.append(bare)

    if customer_name:
        try:
            row = db.execute(text("""
                SELECT COALESCE(brand, '') AS brand FROM customers
                WHERE customer_name = :n AND (company_id = :cid OR company_id IS NULL)
                LIMIT 1
            """), {"n": customer_name, "cid": cid}).mappings().first()
            b = (row or {}).get("brand", "")
            if b and b.lower() not in [k.lower() for k in keys]:
                keys.append(b)
        except Exception:
            pass
    return keys


def _key_clause(keys: list[str], params: dict, col: str = "customer_name") -> str:
    """Case-insensitive IN () over the candidate keys."""
    if not keys:
        return "1=0"
    parts = []
    for i, k in enumerate(keys):
        params[f"k{i}"] = k.lower()
        parts.append(f"LOWER(COALESCE({col}, '')) = :k{i}")
    return "(" + " OR ".join(parts) + ")"


def is_menu_driven(db: Session, customer_name: str, cid: int, brand: str = "") -> bool:
    """True when this customer orders from a weekly menu.

    Falls back to 'does this customer have any recipes with a day set?'. That
    fallback matters: it means the flow works for Frsh the moment their Day
    column is imported, without anyone remembering to tick the box first — and
    the explicit flag then lets you turn it OFF for a customer whose recipes
    happen to carry days but who orders ad hoc.
    """
    ensure_schema(db)
    if not customer_name:
        return False
    try:
        flag = db.execute(text("""
            SELECT is_menu_driven FROM customers
            WHERE customer_name = :n AND (company_id = :cid OR company_id IS NULL)
            LIMIT 1
        """), {"n": customer_name, "cid": cid}).scalar()
        # Only an EXPLICIT value decides. NULL means undecided, so fall through
        # to auto-detection below rather than reporting "not a menu customer".
        if flag is not None:
            return bool(int(flag))
    except Exception:
        pass
    try:
        keys = menu_keys(db, customer_name, brand, cid)
        params: dict = {}
        clause = _key_clause(keys, params)
        return bool(db.execute(text(f"""
            SELECT COUNT(*) FROM recipes
            WHERE {clause} AND COALESCE(day_of_week, '') <> ''
              AND COALESCE(is_active, 1) = 1
        """), params).scalar())
    except Exception:
        return False


@router.get("/customer-mode")
def customer_mode(request: Request, customer: str = "", brand: str = "",
                  db: Session = Depends(get_db)):
    """Does this customer order from a weekly menu, and which days do they have?

    Called when the customer field changes. Returns the days that actually have
    recipes — showing all seven and letting someone pick a day with an empty
    menu is a dead end the user only discovers after two more clicks.
    """
    require_area(request, "order_portal")
    cid = _cid(request)
    menu = is_menu_driven(db, customer, cid, brand=brand)
    days: list[dict] = []
    if menu:
        try:
            keys = menu_keys(db, customer, brand, cid)
            params: dict = {}
            clause = _key_clause(keys, params)
            rows = db.execute(text(f"""
                SELECT day_of_week AS d, COUNT(*) AS n
                FROM recipes
                WHERE {clause} AND COALESCE(day_of_week, '') <> ''
                  AND COALESCE(is_active, 1) = 1
                  AND COALESCE(approval_status, 'Approved') = 'Approved'
                GROUP BY day_of_week
            """), params).mappings().all()
            # Batch 117: "Mon & Fri" and "Daily" each cover several weekdays,
            # so a raw GROUP BY would list them as if they were days in their
            # own right. Expand them into the weekdays they actually cover.
            counts: dict = {}
            for r in rows:
                for d in expand_days(str(r["d"] or "")):
                    counts[d] = counts.get(d, 0) + int(r["n"])
            days = [{"day": d, "recipe_count": counts.get(d, 0)}
                    for d in DAYS if counts.get(d, 0) > 0]
        except Exception:
            days = []
    return {"menu_driven": menu, "customer": customer, "days": days}


@router.get("/dates")
def upcoming_dates(request: Request, day: str = "", weeks: int = 6,
                   lead_hours: int = 48, db: Session = Depends(get_db)):
    """The next N dates falling on `day`, honouring the 48-hour lead time.

    The lead time is applied HERE rather than left to the date picker, so the
    dropdown can never offer a date the server would reject on submit. Pass
    lead_hours=0 from the Immediate Order screen.
    """
    require_area(request, "order_portal")
    day = (day or "").strip().title()
    if day not in _DAY_INDEX:
        return {"day": day, "dates": []}

    from datetime import datetime
    earliest = (datetime.now() + timedelta(hours=max(0, int(lead_hours)))).date()

    target = _DAY_INDEX[day]
    cursor = earliest
    # Advance to the first matching weekday on or after the earliest date.
    while cursor.weekday() != target:
        cursor += timedelta(days=1)

    out = []
    for i in range(max(1, min(int(weeks), 26))):
        d = cursor + timedelta(weeks=i)
        out.append({
            "value": d.isoformat(),
            "label": d.strftime("%A, %d %B %Y"),
            "week": i + 1,
        })
    return {"day": day, "earliest": earliest.isoformat(), "dates": out}


@router.get("/recipes")
def menu_recipes(request: Request, customer: str = "", day: str = "",
                 brand: str = "", q: str = "", db: Session = Depends(get_db)):
    """Recipes available to this customer, optionally narrowed to one day.

    Only Active and Approved recipes are offered. Ordering something that is
    still pending approval creates a production order for a recipe whose
    costing nobody has signed off — the shortage check and the margin on that
    order would both be built on unapproved numbers.
    """
    require_area(request, "order_portal")
    cid = _cid(request)
    day = (day or "").strip().title()

    # Batch 140: company scope added — audit finding routes_menu.py "SELECT
    # ['recipes'] with no company_id". :cid was already bound here but never
    # used in the WHERE, which is the easiest kind of leak to miss on review.
    where = ["(r.company_id = :cid OR r.company_id IS NULL)",
             "COALESCE(r.is_active, 1) = 1",
             "COALESCE(r.approval_status, 'Approved') = 'Approved'"]
    params: dict = {"cid": cid}

    keys = menu_keys(db, customer, brand, cid)
    if keys:
        where.append(_key_clause(keys, params, "r.customer_name"))
    if day:
        # Batch 117: containment, not equality — see _day_clause.
        where.append(_day_clause(day, params))
    if q:
        where.append("(r.recipe_code LIKE :q OR r.recipe_name LIKE :q)")
        params["q"] = f"%{q}%"

    try:
        rows = db.execute(text(f"""
            SELECT r.recipe_code, r.recipe_name,
                   COALESCE(r.category, '') AS category,
                   COALESCE(r.day_of_week, '') AS day_of_week,
                   COALESCE(r.meal_order, '') AS meal_order,
                   COALESCE(r.standard_portions, 1) AS standard_portions,
                   COALESCE(r.sale_price_per_portion, 0) AS price,
                   -- Batch 103: the detail the order screen shows once a
                   -- recipe is picked, so the person ordering can see what
                   -- they are committing to without opening the recipe.
                   COALESCE(r.weight_per_portion_g, 0) AS weight_per_portion_g,
                   COALESCE(r.food_cost_per_portion, 0) AS food_cost,
                   COALESCE(r.std_yield_pct, 0) AS yield_pct
            FROM recipes r
            WHERE {' AND '.join(where)}
            ORDER BY r.category, r.recipe_name
            LIMIT 500
        """), params).mappings().all()
    except Exception:
        rows = []

    return {
        "customer": customer, "day": day, "count": len(rows),
        "recipes": [{
            "code": r["recipe_code"], "name": r["recipe_name"],
            "category": r["category"], "day": r["day_of_week"],
            "meal_order": (r["meal_order"] or "").strip().upper(),
            "standard_portions": float(r["standard_portions"] or 1),
            "price": float(r["price"] or 0),
            "weight_per_portion_g": float(r["weight_per_portion_g"] or 0),
            "food_cost": float(r["food_cost"] or 0),
            "yield_pct": float(r["yield_pct"] or 0),
        } for r in rows],
    }


# =============================================================================
# Batch 104 — CUSTOMER DEFAULTS + DATE-DRIVEN MENU
#
# The flow you asked for, which is simpler than Batch 102's day-dropdown and
# closer to how someone actually places the order:
#
#   pick customer  -> brand + sales channel fill themselves
#   pick date      -> the weekday is DERIVED from the date, and every recipe
#                     on that day's menu is listed with a quantity box
#   type quantities -> submit
#
# Deriving the weekday from the date rather than asking for it removes a whole
# field AND a class of mistake: you can no longer pick "Sunday" and then a date
# that is a Monday.
# =============================================================================
def ensure_default_columns(db: Session) -> None:
    """default_brand_code / default_channel_code on customers.

    Stored on the customer rather than hard-coded, so the next customer with a
    standing brand and channel is a Master Data edit, not a code change.
    """
    for col, ddl in (("default_brand_code", "VARCHAR(80) NULL"),
                     ("default_channel_code", "VARCHAR(80) NULL")):
        try:
            has = db.execute(text("""
                SELECT COUNT(*) FROM information_schema.columns
                WHERE table_schema = DATABASE() AND table_name = 'customers'
                  AND column_name = :c
            """), {"c": col}).scalar()
            if not has:
                db.execute(text(f"ALTER TABLE customers ADD COLUMN {col} {ddl}"))
                db.commit()
        except Exception:
            db.rollback()


def _learned_defaults(db: Session, customer_name: str, cid: int) -> dict:
    """Fall back to what this customer was last actually ordered with.

    If nobody has set the defaults in Master Data yet, the most recent order is
    a better guess than an empty field — and it means the feature does
    something useful on day one instead of waiting to be configured.
    """
    try:
        row = db.execute(text("""
            SELECT COALESCE(brand, '') AS brand, COALESCE(channel, '') AS channel
            FROM customer_orders
            WHERE customer_name = :n AND (company_id = :cid OR company_id IS NULL)
              AND (COALESCE(brand,'') <> '' OR COALESCE(channel,'') <> '')
            ORDER BY id DESC LIMIT 1
        """), {"n": customer_name, "cid": cid}).mappings().first()
        return dict(row) if row else {}
    except Exception:
        return {}


@router.get("/customer-defaults")
def customer_defaults(request: Request, customer: str = "", code: str = "",
                      db: Session = Depends(get_db)):
    """Everything the order form needs the moment a customer is chosen."""
    require_area(request, "order_portal")
    cid = _cid(request)
    ensure_schema(db)
    ensure_default_columns(db)

    brand_code = channel_code = ""
    brand_name = channel_name = ""
    try:
        row = db.execute(text("""
            SELECT COALESCE(default_brand_code, '')   AS b,
                   COALESCE(default_channel_code, '') AS c,
                   COALESCE(brand, '')                AS brand
            FROM customers
            WHERE (customer_code = :code OR customer_name = :n)
              AND (company_id = :cid OR company_id IS NULL)
            LIMIT 1
        """), {"code": code, "n": customer, "cid": cid}).mappings().first()
        if row:
            brand_code = row["b"] or ""
            channel_code = row["c"] or ""
    except Exception:
        pass

    # Batch 116 — learn the brand/channel from SIBLING customers in the same
    # group, not just from this customer's own history.
    #
    # A brand-new SMC cafeteria has no orders of its own, so the previous
    # lookup found nothing and the buyer had to type "Gourmet 360" and
    # "Corporate" by hand every time. But its five siblings have ordered
    # dozens of times, and they all use the same brand and channel — that is
    # what makes them a group. So the group's most common values are used.
    if not brand_code or not channel_code:
        try:
            keys = menu_keys(db, customer, "", cid)
            if keys:
                params: dict = {"cid": cid}
                clause = _key_clause(keys, params, "c.customer_name")
                sib = db.execute(text(f"""
                    SELECT COALESCE(o.brand,'') AS brand, COALESCE(o.channel,'') AS channel,
                           COUNT(*) AS n
                    FROM customer_orders o
                    JOIN customers c ON c.customer_name = o.customer_name
                    WHERE {clause}
                      AND (o.company_id = :cid OR o.company_id IS NULL)
                      AND (COALESCE(o.brand,'') <> '' OR COALESCE(o.channel,'') <> '')
                    GROUP BY o.brand, o.channel
                    ORDER BY n DESC LIMIT 1
                """), params).mappings().first()
                if sib:
                    brand_name = brand_name or sib.get("brand", "")
                    channel_name = channel_name or sib.get("channel", "")
        except Exception:
            pass

    if not brand_code or not channel_code:
        learned = _learned_defaults(db, customer, cid)
        brand_name = brand_name or learned.get("brand", "")
        channel_name = channel_name or learned.get("channel", "")

    # Resolve codes to display values so the form can fill the visible field.
    def _lookup(table, code_col, name_col, value):
        if not value:
            return "", ""
        try:
            r = db.execute(text(f"""
                SELECT {code_col} AS c, {name_col} AS n FROM {table}
                WHERE ({code_col} = :v OR {name_col} = :v)
                  AND (company_id = :cid OR company_id IS NULL) LIMIT 1
            """), {"v": value, "cid": cid}).mappings().first()
            return (r["c"], r["n"]) if r else ("", value)
        except Exception:
            return "", value

    bc, bn = _lookup("brands", "brand_code", "brand_name", brand_code or brand_name)
    cc, cn = _lookup("revenue_streams", "channel_code", "channel_name",
                     channel_code or channel_name)

    menu = is_menu_driven(db, customer, cid, brand=brand_name)
    days: list[str] = []
    if menu:
        try:
            keys = menu_keys(db, customer, brand_name, cid)
            params: dict = {}
            clause = _key_clause(keys, params)
            days = [d for r in db.execute(text(f"""
                SELECT DISTINCT day_of_week FROM recipes
                WHERE {clause} AND COALESCE(day_of_week,'') <> ''
                  AND COALESCE(is_active,1) = 1
                  AND COALESCE(approval_status,'Approved') = 'Approved'
            """), params).all() for d in expand_days(str(r[0] or ""))]
        except Exception:
            days = []

    return {
        "menu_driven": menu,
        "brand_code": bc, "brand_name": bn,
        "channel_code": cc, "channel_name": cn,
        "brand_display": (f"{bc} - {bn}" if bc and bn else (bn or "")),
        "channel_display": (f"{cc} - {cn}" if cc and cn else (cn or "")),
        "days_available": [d for d in DAYS if d in days],
    }


@router.get("/for-date")
def recipes_for_date(request: Request, customer: str = "", brand: str = "",
                     order_date: str = "", db: Session = Depends(get_db)):
    """Every menu recipe for the weekday that `order_date` falls on.

    The whole point: the person ordering picks a date, and the day's menu
    appears with a quantity box against each line. No recipe typing, no day
    field, and no way to mismatch the two.
    """
    require_area(request, "order_portal")
    cid = _cid(request)

    from datetime import datetime as _dt
    try:
        d = _dt.fromisoformat((order_date or "").strip()).date()
    except ValueError:
        return {"day": "", "date": order_date, "count": 0, "recipes": [],
                "error": "Pick a delivery date first."}

    day = d.strftime("%A")           # Monday .. Sunday
    keys = menu_keys(db, customer, brand, cid)
    params: dict = {"cid": cid}
    clause = _key_clause(keys, params, "r.customer_name")
    day_sql = _day_clause(day, params)
    # Batch 140 — company scope. This route was one of the seven HIGH RISK
    # findings from multicompany_scope_audit.py: it read `recipes` with no
    # company_id predicate at all, so company 2's order screen would have
    # listed company 1's menu the moment a second company existed. Legacy rows
    # carry company_id NULL, hence the OR — same pattern as everywhere else.
    scope_sql = "(r.company_id = :cid OR r.company_id IS NULL)"

    # meal_order only exists after the Batch 158 startup guard has run. Ask
    # information_schema rather than assuming, and fall back to a blank column
    # so an older database degrades to "no meal grouping" instead of a 500.
    try:
        has_meal = bool(db.execute(text("""
            SELECT COUNT(*) FROM information_schema.columns
            WHERE table_schema = DATABASE() AND table_name = 'recipes'
              AND column_name = 'meal_order'
        """)).scalar())
    except Exception:
        has_meal = False
    meal_col = "COALESCE(r.meal_order, '')" if has_meal else "''"

    sql = f"""
        SELECT r.recipe_code, r.recipe_name,
               COALESCE(r.category, '') AS category,
               {meal_col} AS meal_order,
               COALESCE(r.standard_portions, 1) AS standard_portions,
               COALESCE(r.sale_price_per_portion, 0) AS price,
               COALESCE(r.food_cost_per_portion, 0) AS food_cost,
               COALESCE(r.weight_per_portion_g, 0) AS weight_per_portion_g
        FROM recipes r
        WHERE {scope_sql}
          AND {clause}
          AND {day_sql}
          AND COALESCE(r.is_active, 1) = 1
          AND COALESCE(r.approval_status, 'Approved') = 'Approved'
        ORDER BY r.category, r.recipe_name
        LIMIT 300
    """
    err = ""
    try:
        rows = db.execute(text(sql), params).mappings().all()
    except Exception as exc:                      # pragma: no cover
        # Batch 140: this used to be a bare `rows = []`. A broken query and an
        # empty menu were indistinguishable on screen — the user saw "0 recipes"
        # and had no way to tell whether the data was missing or the SQL was.
        log.exception("for-date menu query failed")
        rows, err = [], f"Menu query failed: {exc.__class__.__name__}"

    out = {
        "day": day, "date": d.isoformat(), "count": len(rows),
        "recipes": [{
            "code": r["recipe_code"], "name": r["recipe_name"],
            "category": r["category"],
            "meal_order": (r["meal_order"] or "").strip().upper(),
            "standard_portions": float(r["standard_portions"] or 1),
            "price": float(r["price"] or 0),
            "food_cost": float(r["food_cost"] or 0),
            "weight_per_portion_g": float(r["weight_per_portion_g"] or 0),
        } for r in rows],
    }
    if err:
        out["error"] = err

    # ?debug=1 — stage-by-stage funnel. Add it to the URL when a menu comes back
    # short and it tells you WHICH predicate removed the rows, instead of
    # leaving you to guess between customer keys, day matching, active flag and
    # approval flag. Read-only; costs four COUNT(*) queries.
    if (request.query_params.get("debug") or "").strip() in ("1", "true", "yes"):
        out["diagnostics"] = _menu_funnel(db, cid, keys, clause, day_sql, params, day, has_meal)
    return out


def _menu_funnel(db: Session, cid: int, keys: list[str], clause: str,
                 day_sql: str, params: dict, day: str, has_meal: bool) -> dict:
    """Count survivors after each filter, one predicate at a time."""
    stages = [
        ("1_company_scope", "(r.company_id = :cid OR r.company_id IS NULL)"),
        ("2_customer_keys", f"(r.company_id = :cid OR r.company_id IS NULL) AND {clause}"),
        ("3_day_matches", f"(r.company_id = :cid OR r.company_id IS NULL) AND {clause} AND {day_sql}"),
        ("4_is_active", f"(r.company_id = :cid OR r.company_id IS NULL) AND {clause} AND {day_sql} "
                        "AND COALESCE(r.is_active, 1) = 1"),
        ("5_approved", f"(r.company_id = :cid OR r.company_id IS NULL) AND {clause} AND {day_sql} "
                       "AND COALESCE(r.is_active, 1) = 1 "
                       "AND COALESCE(r.approval_status, 'Approved') = 'Approved'"),
    ]
    funnel: dict = {"weekday": day, "customer_keys": keys,
                    "recipes_meal_order_column": has_meal}
    for label, where in stages:
        try:
            funnel[label] = int(db.execute(
                text(f"SELECT COUNT(*) FROM recipes r WHERE {where}"), params).scalar() or 0)
        except Exception as exc:
            funnel[label] = f"ERROR: {exc.__class__.__name__}"
    # What the day filter is actually being compared against.
    try:
        funnel["distinct_day_values"] = [
            r[0] for r in db.execute(text(f"""
                SELECT DISTINCT COALESCE(r.day_of_week, '(null)')
                FROM recipes r
                WHERE (r.company_id = :cid OR r.company_id IS NULL) AND {clause}
                LIMIT 40
            """), params).all()]
    except Exception:
        funnel["distinct_day_values"] = []
    return funnel
