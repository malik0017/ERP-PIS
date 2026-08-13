# app/modules/finance/routes_coa.py
# =============================================================================
# Batch 110 — CHART OF ACCOUNTS: hierarchy browser, creation, demo purge
# -----------------------------------------------------------------------------
# Your CoA is four levels deep:
#
#     Class (1 Assets) → Group (1 Current Assets) → Subgroup (1 Banks)
#                      → Account (09 Cards Under Process) → GL 11209
#
# Batch 109 imported it as a flat list, which lost the two things the
# hierarchy is for: grouped financial statements, and knowing where a new
# account belongs.
#
# A FINDING THAT SHAPED THIS DESIGN
#
# I checked whether the GL code can be derived from its parts. It cannot:
# 194 of your 203 accounts follow class+group+subgroup+account, but 9 do not.
# Subgroup "1" (Banks) produces GL digit 2, and subgroup "3" produces both
# 3 and 8 in different places.
#
# So the GL code is treated as AUTHORITATIVE, never computed. When you add an
# account the system SUGGESTS the next code within the subgroup — by taking
# the highest existing sibling and adding one, which respects whatever pattern
# that subgroup actually uses — and lets you override it. A system that
# silently "corrects" a GL code to match a formula would renumber your ledger.
# =============================================================================
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import text
from sqlalchemy.orm import Session
from urllib.parse import quote

from app.core.rbac import require_area, require_action
from app.core.templates import render
from app.database.session import get_db

router = APIRouter(prefix="/finance/coa", tags=["Finance"])

TYPES = ["ASSET", "LIABILITY", "EQUITY", "REVENUE", "EXPENSE"]


def _cid(request: Request) -> int:
    return int(request.session.get("company_id") or 1)


def _rows(db: Session, cid: int, include_inactive: bool = False,
          search: str = "") -> list[dict]:
    from app.modules.setup.routes_import import ensure_coa_hierarchy
    ensure_coa_hierarchy(db)

    where = ["(a.company_id = :cid OR a.company_id IS NULL)"]
    params: dict = {"cid": cid}
    if not include_inactive:
        where.append("COALESCE(a.is_active, 1) = 1")
    if search:
        where.append("(a.account_code LIKE :q OR a.account_name LIKE :q "
                     "OR a.class_name LIKE :q OR a.group_name LIKE :q OR a.subgroup_name LIKE :q)")
        params["q"] = f"%{search}%"
    try:
        return [dict(r) for r in db.execute(text(f"""
            SELECT a.id, a.account_code, a.account_name, a.account_type,
                   COALESCE(a.is_active, 1)      AS is_active,
                   COALESCE(a.is_demo, 0)        AS is_demo,
                   COALESCE(a.class_code, '')    AS class_code,
                   COALESCE(a.class_name, '(unclassified)')    AS class_name,
                   COALESCE(a.group_code, '')    AS group_code,
                   COALESCE(a.group_name, '(no group)')        AS group_name,
                   COALESCE(a.subgroup_code, '') AS subgroup_code,
                   COALESCE(a.subgroup_name, '(no subgroup)')  AS subgroup_name,
                   COALESCE(a.account_seq, '')   AS account_seq,
                   COALESCE(j.debit, 0)          AS debit,
                   COALESCE(j.credit, 0)         AS credit,
                   COALESCE(j.line_count, 0)     AS posting_lines
            FROM gl_accounts a
            LEFT JOIN (
                -- Batch 110: aliased `line_count`, NOT `lines`. LINES is a
                -- reserved word in MariaDB (LOAD DATA ... LINES TERMINATED BY)
                -- and using it unquoted is a syntax error. The try/except
                -- around this query turned that into an empty page rather
                -- than an error, so the screen rendered with no accounts and
                -- nothing said why — the same "safety net hides the failure"
                -- trap as the reorder engine in Batch 107.
                SELECT account_code, SUM(COALESCE(debit,0)) AS debit,
                       SUM(COALESCE(credit,0)) AS credit, COUNT(*) AS line_count
                FROM gl_journal_lines GROUP BY account_code
            ) j ON j.account_code = a.account_code
            WHERE {' AND '.join(where)}
            ORDER BY a.class_code, a.group_code, a.subgroup_code, a.account_code
        """), params).mappings().all()]
    except Exception:
        return []


def _tree(rows: list[dict]) -> list[dict]:
    """Group the flat rows into Class → Group → Subgroup for display."""
    tree: dict = {}
    for r in rows:
        ck = (r["class_code"], r["class_name"])
        gk = (r["group_code"], r["group_name"])
        sk = (r["subgroup_code"], r["subgroup_name"])
        c = tree.setdefault(ck, {"code": r["class_code"], "name": r["class_name"],
                                 "groups": {}, "count": 0, "balance": 0.0})
        g = c["groups"].setdefault(gk, {"code": r["group_code"], "name": r["group_name"],
                                        "subgroups": {}, "count": 0, "balance": 0.0})
        sgr = g["subgroups"].setdefault(sk, {"code": r["subgroup_code"], "name": r["subgroup_name"],
                                             "accounts": [], "count": 0, "balance": 0.0})
        bal = float(r["debit"] or 0) - float(r["credit"] or 0)
        r["balance"] = round(bal, 2)
        sgr["accounts"].append(r)
        for node in (c, g, sgr):
            node["count"] += 1
            node["balance"] = round(node["balance"] + bal, 2)

    out = []
    for c in sorted(tree.values(), key=lambda x: (x["code"] or "z", x["name"])):
        c["groups"] = sorted(c["groups"].values(), key=lambda x: (x["code"] or "z", x["name"]))
        for g in c["groups"]:
            g["subgroups"] = sorted(g["subgroups"].values(),
                                    key=lambda x: (x["code"] or "z", x["name"]))
        out.append(c)
    return out


@router.get("/tree")
def coa_tree(request: Request, db: Session = Depends(get_db)):
    """The hierarchy view — Class → Group → Subgroup → Account."""
    require_area(request, "finance")
    cid = _cid(request)
    q = request.query_params
    search = (q.get("search") or "").strip()
    include_inactive = q.get("inactive") == "1"

    rows = _rows(db, cid, include_inactive, search)
    demo = [r for r in rows if r["is_demo"] or (not r["class_code"] and r["posting_lines"] == 0)]

    by_type: dict[str, int] = {}
    for r in rows:
        by_type[r["account_type"]] = by_type.get(r["account_type"], 0) + 1

    return render(request, "finance/coa_tree.html", {
        "tree": _tree(rows),
        "totals": {
            "accounts": len(rows),
            "posted": sum(1 for r in rows if r["posting_lines"]),
            "demo": len(demo),
            "by_type": sorted(by_type.items()),
        },
        "demo_accounts": demo,
        "filters": {"search": search, "inactive": include_inactive},
        "page_title": "Chart of Accounts",
    })


@router.get("/new")
def coa_new(request: Request, db: Session = Depends(get_db)):
    """Add an account inside the existing hierarchy."""
    require_area(request, "finance")
    cid = _cid(request)
    rows = _rows(db, cid, include_inactive=True)

    # Distinct hierarchy nodes, so the form offers only paths that exist —
    # inventing a new Class from a free-text box is how a chart of accounts
    # ends up with "Assets", "assets" and "Asset " as three classes.
    nodes: dict = {}
    for r in rows:
        key = (r["class_code"], r["class_name"], r["group_code"], r["group_name"],
               r["subgroup_code"], r["subgroup_name"])
        e = nodes.setdefault(key, {"class_code": r["class_code"], "class_name": r["class_name"],
                                   "group_code": r["group_code"], "group_name": r["group_name"],
                                   "subgroup_code": r["subgroup_code"],
                                   "subgroup_name": r["subgroup_name"],
                                   "account_type": r["account_type"], "codes": []})
        e["codes"].append(r["account_code"])

    paths = []
    for e in nodes.values():
        # Suggest the next GL code from the highest existing SIBLING, not from
        # a formula — see the module docstring for why.
        numeric = [c for c in e["codes"] if c.isdigit()]
        e["suggested"] = str(max(int(c) for c in numeric) + 1) if numeric else ""
        e["label"] = (f"{e['class_code']} {e['class_name']} › "
                      f"{e['group_code']} {e['group_name']} › "
                      f"{e['subgroup_code']} {e['subgroup_name']}")
        paths.append(e)
    paths.sort(key=lambda x: (x["class_code"] or "z", x["group_code"] or "z",
                              x["subgroup_code"] or "z"))

    return render(request, "finance/coa_new.html", {
        "paths": paths, "types": TYPES, "page_title": "New GL Account",
    })


@router.post("/new")
async def coa_create(request: Request, db: Session = Depends(get_db)):
    require_action(request, "finance", "add")
    from app.modules.setup.routes_import import ensure_coa_hierarchy
    ensure_coa_hierarchy(db)

    cid = _cid(request)
    form = await request.form()
    code = (form.get("account_code") or "").strip()
    name = (form.get("account_name") or "").strip()
    if not code or not name:
        return RedirectResponse(
            f"/finance/coa/new?toast=warning&title={quote('Missing detail')}"
            f"&msg={quote('A GL code and an account name are both required.')}", status_code=303)

    exists = db.execute(text("""
        SELECT account_name FROM gl_accounts
        WHERE account_code = :c AND (company_id = :cid OR company_id IS NULL) LIMIT 1
    """), {"c": code, "cid": cid}).scalar()
    if exists:
        return RedirectResponse(
            f"/finance/coa/new?toast=danger&title={quote('Code already used')}"
            f"&msg={quote(f'GL {code} already exists as \"{exists}\". Pick another code.')}",
            status_code=303)

    db.execute(text("""
        INSERT INTO gl_accounts
            (company_id, account_code, account_name, account_type, is_active,
             class_code, class_name, group_code, group_name,
             subgroup_code, subgroup_name, account_seq, is_demo)
        VALUES (:cid, :code, :name, :type, 1,
                :ccode, :cname, :gcode, :gname, :scode, :sname, :aseq, 0)
    """), {
        "cid": cid, "code": code, "name": name[:255],
        "type": (form.get("account_type") or "ASSET").upper(),
        "ccode": (form.get("class_code") or "").strip() or None,
        "cname": (form.get("class_name") or "").strip() or None,
        "gcode": (form.get("group_code") or "").strip() or None,
        "gname": (form.get("group_name") or "").strip() or None,
        "scode": (form.get("subgroup_code") or "").strip() or None,
        "sname": (form.get("subgroup_name") or "").strip() or None,
        "aseq": (form.get("account_seq") or "").strip() or None,
    })
    db.commit()
    return RedirectResponse(
        f"/finance/coa/tree?toast=success&title={quote('Account Created')}"
        f"&msg={quote(f'GL {code} — {name} added.')}", status_code=303)


@router.post("/purge-demo")
async def purge_demo(request: Request, db: Session = Depends(get_db)):
    """Remove the seeded demo accounts.

    DELETES only accounts with no journal postings and no hierarchy — that is
    the signature of a seeded demo row. Anything carrying a posting is
    deactivated instead, never deleted: a deleted account orphans its journal
    lines and breaks every historical statement that referenced it.
    """
    require_action(request, "finance", "delete")
    from app.modules.setup.routes_import import ensure_coa_hierarchy
    ensure_coa_hierarchy(db)
    cid = _cid(request)

    posted = set()
    try:
        for r in db.execute(text("SELECT DISTINCT account_code FROM gl_journal_lines")).all():
            posted.add(r[0])
    except Exception:
        pass

    candidates = [dict(r) for r in db.execute(text("""
        SELECT id, account_code, account_name FROM gl_accounts
        WHERE (company_id = :cid OR company_id IS NULL)
          AND COALESCE(class_code, '') = ''
    """), {"cid": cid}).mappings().all()]

    deleted = deactivated = 0
    for a in candidates:
        if a["account_code"] in posted:
            db.execute(text("UPDATE gl_accounts SET is_active = 0 WHERE id = :i"), {"i": a["id"]})
            deactivated += 1
        else:
            db.execute(text("DELETE FROM gl_accounts WHERE id = :i"), {"i": a["id"]})
            deleted += 1
    db.commit()

    msg = f"{deleted} demo account(s) removed"
    if deactivated:
        msg += (f"; {deactivated} kept but deactivated because they carry journal "
                "postings — deleting those would orphan history")
    return RedirectResponse(
        f"/finance/coa/tree?toast=success&title={quote('Demo Accounts Cleared')}&msg={quote(msg + '.')}",
        status_code=303)
