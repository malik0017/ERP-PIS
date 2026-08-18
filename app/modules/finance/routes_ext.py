# app/modules/finance/routes_ext.py
# =============================================================================
# Batch 22 — FINANCE EXTENSIONS (SAP B1-style)
# -----------------------------------------------------------------------------
# Adds the three pieces the base finance module was missing, WITHOUT touching
# the working finance/routes.py:
#
#   1. Chart of Accounts management   GET/POST  /finance/coa
#   2. Manual Journal Entry (GL)      GET/POST  /finance/journal/new
#      - Enforces double-entry: total debit must equal total credit or the
#        entry is REJECTED and the imbalance (difference) is shown.
#   3. Manual A/R Invoice creation    GET/POST  /finance/ar/new
#      - Posts the receivable journal (Dr A/R 1200 / Cr Sales 4000) so the
#        general ledger, trial balance and dashboards update in real time.
#
# This router is included AFTER the base finance router in app/main.py, so its
# routes extend the same /finance namespace.
#
# WORKFLOW / ACCOUNTING (how it works)
# -----------------------------------------------------------------------------
#   Sales      : Customer order delivered -> A/R invoice -> Journal
#                Dr 1200 Accounts Receivable   Cr 4000 Sales Revenue
#   Receipt    : Payment in                -> Journal
#                Dr 1000 Cash & Bank          Cr 1200 Accounts Receivable
#   Purchase   : PO -> GRN -> A/P invoice   -> Journal
#                Dr 1300 Inventory            Cr 2100 Accounts Payable
#   Payment out: Payment to supplier        -> Journal
#                Dr 2100 Accounts Payable     Cr 1000 Cash & Bank
#   Expense    : Manual journal             -> Journal (any Dr expense / Cr cash)
#
# Every journal is balanced (sum debit == sum credit) before it is allowed to
# post; the trial balance therefore always nets to zero. The GL page already
# shows the live trial balance and journal browser.
# =============================================================================

from fastapi import APIRouter, Depends, Request, Form
from fastapi.responses import RedirectResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.templates import render
from app.core.rbac import require_area, require_action
from app.database import get_db

# Re-use the balanced-journal engine + schema helpers from the base module.
from app.modules.finance.routes import (
    _ensure_gl_schema,
    _ensure_finance_schema,
    post_journal,
    next_document_no,
    _cid,
    _user,
)

router = APIRouter(tags=["Finance (Extended)"])


# ---------------------------------------------------------------------------
# 1. CHART OF ACCOUNTS
# ---------------------------------------------------------------------------
@router.get("/finance/coa")
def chart_of_accounts(request: Request, db: Session = Depends(get_db)):
    require_area(request, "finance")
    _ensure_gl_schema(db)
    # Batch 86 fix: this had zero company scoping on either side of the
    # join — gl_accounts AND gl_journal_lines. Standard chart-of-accounts
    # codes (e.g. "1000 Cash", "4000 Revenue") are typically reused
    # identically across companies, so without scoping, one company's
    # account balance here was actually the SUM of every company sharing
    # that code — a real cross-company financial data leak, not just a
    # visibility gap. Both sides of the join now filter by company.
    cid = _cid(request)
    accounts = db.execute(text("""
        SELECT a.id, a.account_code, a.account_name, a.account_type,
               ROUND(COALESCE(SUM(l.debit),0),2)  AS total_debit,
               ROUND(COALESCE(SUM(l.credit),0),2) AS total_credit,
               ROUND(COALESCE(SUM(l.debit),0) - COALESCE(SUM(l.credit),0),2) AS balance
        FROM gl_accounts a
        LEFT JOIN gl_journal_lines l
               ON l.account_code = a.account_code
              AND (l.company_id = :cid OR l.company_id IS NULL)
        WHERE (a.company_id = :cid OR a.company_id IS NULL)
        GROUP BY a.id, a.account_code, a.account_name, a.account_type
        ORDER BY a.account_code
    """), {"cid": cid}).mappings().all()
    # Batch 116 — send /finance/coa to the HIERARCHY view.
    #
    # Batch 110 built the Class > Group > Subgroup > Account browser and the
    # guided New Account form, but the menu still pointed here — so the flat
    # 200-row list and the two-field "new account" box were what people
    # actually saw, and the hierarchy work was invisible.
    #
    # The flat renderer is kept, reachable at /finance/coa?flat=1, because a
    # single sortable list is genuinely better when you are hunting one code.
    if request.query_params.get("flat") != "1":
        return RedirectResponse("/finance/coa/tree", status_code=302)

    return render(request, "finance/coa.html", {
        "accounts": accounts, "page_title": "Chart of Accounts (flat)",
    })


@router.post("/finance/coa/create")
def create_account(
    request: Request,
    account_code: str = Form(...),
    account_name: str = Form(...),
    account_type: str = Form("Asset"),
    db: Session = Depends(get_db),
):
    require_action(request, "finance", "add")
    _ensure_gl_schema(db)
    code = account_code.strip()
    if not code:
        return RedirectResponse("/finance/coa?error=Account code is required", status_code=303)
    cid = _cid(request)
    try:
        # Batch 86 fix: this used to check for a duplicate account_code
        # across ALL companies, so Company B could never create "1000 Cash"
        # if Company A already had it — a false collision on completely
        # normal, commonly-reused chart-of-accounts codes.
        exists = db.execute(
            text("SELECT COUNT(*) FROM gl_accounts WHERE account_code = :c AND (company_id = :cid OR company_id IS NULL)"),
            {"c": code, "cid": cid}
        ).scalar()
        if exists:
            return RedirectResponse(
                f"/finance/coa?error=Account {code} already exists", status_code=303)
        db.execute(text("""
            INSERT INTO gl_accounts (company_id, account_code, account_name, account_type)
            VALUES (:cid, :c, :n, :t)
        """), {"cid": cid, "c": code, "n": account_name.strip(), "t": account_type})
        db.commit()
    except Exception as e:
        db.rollback()
        return RedirectResponse(f"/finance/coa?error={str(e)[:120]}", status_code=303)
    return RedirectResponse(f"/finance/coa?success=Account {code} created", status_code=303)


# ---------------------------------------------------------------------------
# 2. MANUAL JOURNAL ENTRY (double-entry, balanced or rejected)
# ---------------------------------------------------------------------------
@router.get("/finance/journal/new")
def journal_form(request: Request, db: Session = Depends(get_db)):
    require_area(request, "finance")
    _ensure_gl_schema(db)
    # Batch 86 fix: was showing every company's chart of accounts mixed
    # together in this dropdown, risking a journal entry posted against
    # the wrong company's account code.
    accounts = db.execute(text("""
        SELECT account_code, account_name, account_type
        FROM gl_accounts WHERE (company_id = :cid OR company_id IS NULL) ORDER BY account_code
    """), {"cid": _cid(request)}).mappings().all()
    return render(request, "finance/journal_form.html", {
        "accounts": accounts, "page_title": "New Journal Entry",
    })


@router.post("/finance/journal/new")
async def create_journal(request: Request, db: Session = Depends(get_db)):
    require_action(request, "finance", "add")
    _ensure_gl_schema(db)
    form = await request.form()

    memo = (form.get("memo") or "Manual Journal").strip()
    source_type = (form.get("source_type") or "MANUAL_JV").strip()

    codes = form.getlist("account_code")
    debits = form.getlist("debit")
    credits = form.getlist("credit")
    parties = form.getlist("party")

    lines: list[tuple[str, float, float, str]] = []
    total_dr = total_cr = 0.0
    for i, code in enumerate(codes):
        code = (code or "").strip()
        if not code:
            continue
        dr = float(debits[i] or 0) if i < len(debits) and debits[i] else 0.0
        cr = float(credits[i] or 0) if i < len(credits) and credits[i] else 0.0
        if dr == 0 and cr == 0:
            continue
        party = (parties[i] if i < len(parties) else "") or ""
        lines.append((code, round(dr, 2), round(cr, 2), party))
        total_dr += dr
        total_cr += cr

    total_dr = round(total_dr, 2)
    total_cr = round(total_cr, 2)
    diff = round(total_dr - total_cr, 2)

    # ---- GATE: reject unbalanced entries and report the difference ----
    if len(lines) < 2:
        return RedirectResponse(
            "/finance/journal/new?error=A journal needs at least two lines", status_code=303)
    if total_dr <= 0:
        return RedirectResponse(
            "/finance/journal/new?error=Total debit must be greater than zero", status_code=303)
    if abs(diff) > 0.005:
        return RedirectResponse(
            f"/finance/journal/new?error=Out of balance by {diff:+.2f} "
            f"(Debit {total_dr:.2f} vs Credit {total_cr:.2f}). Entry not posted.",
            status_code=303)

    # A manual JV uses a unique source_no so post_journal's idempotency check
    # never blocks a legitimate second manual entry.
    src_no = next_document_no(db, _cid(request), "MJV", "MJV")
    jno = post_journal(db, request, source_type, src_no, memo, lines)
    if not jno:
        return RedirectResponse(
            "/finance/journal/new?error=Could not post journal (check balance)",
            status_code=303)
    return RedirectResponse(
        f"/finance/gl?success=Journal {jno} posted (Dr {total_dr:.2f} = Cr {total_cr:.2f})",
        status_code=303)


# ---------------------------------------------------------------------------
# 3. MANUAL A/R INVOICE
# ---------------------------------------------------------------------------
@router.get("/finance/ar/new")
def ar_form(request: Request, db: Session = Depends(get_db)):
    require_area(request, "finance")
    _ensure_finance_schema(db)
    customers = []
    try:
        # Batch 86 fix: was showing every company's customers mixed
        # together in this dropdown.
        customers = db.execute(text(
            "SELECT customer_name FROM customers WHERE (company_id = :cid OR company_id IS NULL) ORDER BY customer_name LIMIT 1000"
        ), {"cid": _cid(request)}).mappings().all()
    except Exception:
        pass
    return render(request, "finance/ar_form.html", {
        "customers": customers, "page_title": "New A/R Invoice",
    })


@router.post("/finance/ar/create")
def create_ar_invoice(
    request: Request,
    customer_name: str = Form(...),
    order_no: str = Form(""),
    amount: float = Form(0),
    remarks: str = Form(""),
    post_now: str = Form(""),
    db: Session = Depends(get_db),
):
    require_action(request, "finance", "add")
    _ensure_finance_schema(db)
    if not customer_name.strip() or amount <= 0:
        return RedirectResponse(
            "/finance/ar/new?error=Customer and a positive amount are required",
            status_code=303)

    inv_no = next_document_no(db, _cid(request), "ARI", "ARI")
    status = "Posted" if post_now else "Draft"
    try:
        db.execute(text("""
            INSERT INTO ar_invoices (company_id, invoice_no, order_no, customer_name,
                                     invoice_date, status, amount, created_by, remarks)
            VALUES (:cid, :inv, :ono, :cn, CURDATE(), :st, :amt, :by, :rm)
        """), {"cid": _cid(request), "inv": inv_no, "ono": order_no.strip() or None,
               "cn": customer_name.strip(), "st": status, "amt": amount,
               "by": _user(request), "rm": remarks.strip() or None})
        db.commit()
    except Exception as e:
        db.rollback()
        return RedirectResponse(f"/finance/ar/new?error={str(e)[:120]}", status_code=303)

    # If posted, create the balanced receivable journal immediately.
    if post_now:
        # Batch 67: VAT split — Dr 1120 AR / Cr 4100 Revenue (net) + Cr 2300 VAT.
        vat_rate = 0.15
        net = round(amount / (1 + vat_rate), 2)
        vat = round(amount - net, 2)
        ar_lines = [("1120", amount, 0.0, customer_name),
                    ("4100", 0.0, net, customer_name)]
        if vat > 0:
            ar_lines.append(("2300", 0.0, vat, customer_name))
        post_journal(
            db, request, "AR_INVOICE", inv_no,
            f"A/R Invoice {inv_no} - {customer_name}",
            ar_lines,
        )
    return RedirectResponse(
        f"/finance?success=A/R invoice {inv_no} created ({status})", status_code=303)
