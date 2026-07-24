# app/modules/finance/routes.py
from __future__ import annotations

import logging  # Batch 26: never hide finance query failures

from datetime import date
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.templates import render
from app.core.rbac import require_area, require_action
from app.database.session import get_db

router = APIRouter(prefix="/finance", tags=["Finance"])


def _user(request: Request) -> str:
    return request.session.get("username") or "system"


def _cid(request: Request):
    return request.session.get("company_id") or 1


def _next_no(db: Session, table: str, col: str, prefix: str) -> str:
    today = date.today().strftime("%Y%m%d")
    row = db.execute(text(f"SELECT {col} FROM {table} WHERE {col} LIKE :p ORDER BY id DESC LIMIT 1"), {"p": f"{prefix}-{today}-%"}).first()
    seq = int(row[0].rsplit("-", 1)[-1]) + 1 if row else 1
    return f"{prefix}-{today}-{seq:04d}"


def _ensure_finance_schema(db: Session) -> None:
    def _try(sql):
        try:
            db.execute(text(sql))
        except Exception:
            db.rollback()
    _try("""
        CREATE TABLE IF NOT EXISTS ar_invoices (
            id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
            company_id INT NULL, invoice_no VARCHAR(80) NOT NULL UNIQUE,
            order_no VARCHAR(80) NULL, customer_name VARCHAR(255) NULL,
            invoice_date DATE NULL, status VARCHAR(40) NOT NULL DEFAULT 'Draft',
            amount DECIMAL(18,4) NOT NULL DEFAULT 0,
            paid_amount DECIMAL(18,4) NOT NULL DEFAULT 0,
            remarks TEXT NULL, created_by VARCHAR(120) NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            KEY idx_ar_order (order_no), KEY idx_ar_status (status)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)
    _try("""
        CREATE TABLE IF NOT EXISTS ap_invoices (
            id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
            company_id INT NULL, ap_no VARCHAR(80) NOT NULL UNIQUE,
            supplier_name VARCHAR(255) NULL, po_no VARCHAR(80) NULL, grn_no VARCHAR(80) NULL,
            invoice_date DATE NULL, status VARCHAR(40) NOT NULL DEFAULT 'Draft',
            amount DECIMAL(18,4) NOT NULL DEFAULT 0,
            paid_amount DECIMAL(18,4) NOT NULL DEFAULT 0,
            match_status VARCHAR(40) NOT NULL DEFAULT 'Pending Match',
            remarks TEXT NULL, created_by VARCHAR(120) NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            KEY idx_ap_supplier (supplier_name), KEY idx_ap_po (po_no), KEY idx_ap_grn (grn_no)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)
    _try("""
        CREATE TABLE IF NOT EXISTS finance_payments (
            id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
            company_id INT NULL, payment_no VARCHAR(80) NOT NULL UNIQUE,
            party_type VARCHAR(20) NOT NULL, party_name VARCHAR(255) NULL,
            reference_no VARCHAR(80) NULL, payment_date DATE NULL,
            amount DECIMAL(18,4) NOT NULL DEFAULT 0,
            method VARCHAR(50) NULL, remarks TEXT NULL, created_by VARCHAR(120) NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            KEY idx_pay_ref (reference_no), KEY idx_pay_party (party_type, party_name)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)
    # ------------------------------------------------------------------
    # Batch 27 — SELF-HEALING COLUMN REPAIR.
    #
    # THE BUG THIS FIXES: "No GRNs found" in Supplier Invoice Entry even when
    # GRNs clearly exist on the PO screen.
    #
    # CREATE TABLE IF NOT EXISTS does NOTHING when the table already exists. On
    # databases where ap_invoices / ar_invoices were first created by the older
    # SQLAlchemy models, columns such as grn_no, match_status and paid_amount
    # were never added. The GRN picker joins ap_invoices ON grn_no, so the whole
    # query raised "Unknown column 'grn_no'" and the dropdown fell back to an
    # empty list — exactly the symptom reported.
    #
    # Rather than require a manual migration, we ALTER the tables into shape on
    # every request. Each ALTER is cheap and guarded: if the column is already
    # there MySQL raises #1060 and we simply move on.
    # ------------------------------------------------------------------
    _repair = {
        "ap_invoices": {
            "company_id":   "INT NULL",
            "po_no":        "VARCHAR(80) NULL",
            "grn_no":       "VARCHAR(80) NULL",
            "invoice_date": "DATE NULL",
            "status":       "VARCHAR(40) NOT NULL DEFAULT 'Open'",
            "amount":       "DECIMAL(18,4) NOT NULL DEFAULT 0",
            "paid_amount":  "DECIMAL(18,4) NOT NULL DEFAULT 0",
            "match_status": "VARCHAR(40) NOT NULL DEFAULT 'Pending Match'",
            "remarks":      "TEXT NULL",
            "created_by":   "VARCHAR(120) NULL",
        },
        "ar_invoices": {
            "company_id":   "INT NULL",
            "order_no":     "VARCHAR(80) NULL",
            "invoice_date": "DATE NULL",
            "status":       "VARCHAR(40) NOT NULL DEFAULT 'Open'",
            "amount":       "DECIMAL(18,4) NOT NULL DEFAULT 0",
            "paid_amount":  "DECIMAL(18,4) NOT NULL DEFAULT 0",
            "remarks":      "TEXT NULL",
            "created_by":   "VARCHAR(120) NULL",
        },
        "finance_payments": {
            "company_id":   "INT NULL",
            "party_type":   "VARCHAR(20) NULL",
            "party_name":   "VARCHAR(255) NULL",
            "reference_no": "VARCHAR(80) NULL",
            "payment_date": "DATE NULL",
            "amount":       "DECIMAL(18,4) NOT NULL DEFAULT 0",
            "method":       "VARCHAR(50) NULL",
            "remarks":      "TEXT NULL",
            "created_by":   "VARCHAR(120) NULL",
        },
    }
    for _table, _cols in _repair.items():
        try:
            existing = {r["Field"] for r in
                        db.execute(text(f"SHOW COLUMNS FROM {_table}")).mappings().all()}
        except Exception:
            db.rollback()
            continue  # table genuinely absent; the CREATE above handles it
        for _col, _ddl in _cols.items():
            if _col not in existing:
                _try(f"ALTER TABLE {_table} ADD COLUMN {_col} {_ddl}")
                logging.getLogger(__name__).warning(
                    "finance: repaired missing column %s.%s", _table, _col)

    # ------------------------------------------------------------------
    # Batch 28 — COLLATION REPAIR.
    #
    # MySQL error #1267 "Illegal mix of collations
    # (utf8mb4_0900_ai_ci,IMPLICIT) and (utf8mb4_unicode_ci,IMPLICIT)".
    #
    # A collation is the rule set MySQL uses to compare text. It REFUSES to
    # compare two strings governed by different rules, so a join such as
    #     inv.grn_no = g.grn_no
    # blows up when the two tables were created with different collations.
    #
    # On this database the procurement tables (grn_receipts, grn_lines,
    # purchase_orders, ...) came out as utf8mb4_0900_ai_ci — the MySQL 8
    # default — while the finance tables are utf8mb4_unicode_ci. The mismatch
    # was invisible until the GRN picker joined across the boundary, which is
    # why "No GRNs found" appeared even though GRNs plainly existed.
    #
    # The queries themselves now pin COLLATE explicitly, so they work either
    # way. This block additionally normalises the tables so EVERY future query
    # is safe, not just the ones we remembered to annotate.
    # ------------------------------------------------------------------
    _collation_tables = [
        "grn_receipts", "grn_lines", "purchase_orders", "purchase_order_lines",
        "goods_receipts", "goods_receipt_lines", "store_issue_audit",
        "ap_invoices", "ar_invoices", "finance_payments",
        "gl_accounts", "gl_journals", "gl_journal_lines", "document_sequences",
    ]
    try:
        wrong = {r["TABLE_NAME"] for r in db.execute(text("""
            SELECT TABLE_NAME
            FROM information_schema.TABLES
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_COLLATION IS NOT NULL
              AND TABLE_COLLATION <> 'utf8mb4_unicode_ci'
        """)).mappings().all()}
    except Exception:
        db.rollback()
        wrong = set()

    for _t in _collation_tables:
        if _t in wrong:
            _try(f"ALTER TABLE {_t} CONVERT TO CHARACTER SET utf8mb4 "
                 f"COLLATE utf8mb4_unicode_ci")
            logging.getLogger(__name__).warning(
                "finance: normalised collation of table %s", _t)

    try:
        db.commit()
    except Exception:
        db.rollback()


def _safe_scalar(db: Session, sql: str, params=None, default=0):
    """Run a scalar query, returning `default` on any error."""
    try:
        return db.execute(text(sql), params or {}).scalar() or default
    except Exception:
        return default
    try:
        return db.execute(text(sql), params or {}).scalar() or default
    except Exception:
        return default


def _auto_draft_delivered_orders(db: Session, request: Request) -> int:
    """Create AR draft invoices for delivered orders not already invoiced."""
    try:
        rows = db.execute(text("""
            SELECT po.order_no, po.customer_name, COALESCE(po.selling_value, po.total_sale_value, 0) AS amount
            FROM production_orders po
            WHERE LOWER(COALESCE(po.status,'')) IN ('delivered','dispatch delivered','closed')
              AND NOT EXISTS (SELECT 1 FROM ar_invoices ai WHERE ai.order_no = po.order_no)
            LIMIT 50
        """)).mappings().all()
    except Exception:
        rows = []
    count = 0
    for r in rows:
        inv = next_document_no(db, _cid(request), "AR", "AR")
        db.execute(text("""
            INSERT INTO ar_invoices (company_id, invoice_no, order_no, customer_name, invoice_date, status, amount, created_by, remarks)
            VALUES (:cid, :inv, :ord, :cust, CURDATE(), 'Draft', :amt, :by, 'Auto-drafted when dispatch/order status became Delivered')
        """), {"cid": _cid(request), "inv": inv, "ord": r["order_no"], "cust": r.get("customer_name"), "amt": r.get("amount") or 0, "by": _user(request)})
        count += 1
    if count:
        db.commit()
    return count


@router.get("")
def finance_dashboard(request: Request, db: Session = Depends(get_db)):
    require_area(request, "finance")
    _ensure_finance_schema(db)
    drafted = _auto_draft_delivered_orders(db, request)
    ar = db.execute(text("SELECT * FROM ar_invoices ORDER BY id DESC LIMIT 100")).mappings().all()
    ap = db.execute(text("SELECT * FROM ap_invoices ORDER BY id DESC LIMIT 100")).mappings().all()
    payments = db.execute(text("SELECT * FROM finance_payments ORDER BY id DESC LIMIT 100")).mappings().all()

    # ------------------------------------------------------------------
    # Batch 26 — GRN picker for Supplier Invoice Entry.
    #
    # Was: the query sat inside `except Exception: pass`, so ANY error left the
    # dropdown silently empty ("Manual / select GRN" with nothing under it) and
    # the Supplier / PO / Amount fields never auto-filled.
    #
    # Now: errors are logged, and each GRN carries the extra data the form needs
    # (received date, already-invoiced flag, remaining un-invoiced value) so the
    # UI can auto-fill everything and grey out GRNs that are already billed.
    # ------------------------------------------------------------------
    grns = []
    # Batch 27: TWO-STAGE, fault-tolerant GRN picker.
    # Stage 1 = full query (GRN value + already-invoiced + remaining).
    # Stage 2 = minimal fallback that touches ONLY grn_receipts/grn_lines, used
    #           if stage 1 fails for any reason. A schema problem in ap_invoices
    #           can therefore never again leave the user with an empty dropdown.
    try:
        grns = [dict(r) for r in db.execute(text("""
            SELECT  g.grn_no,
                    COALESCE(g.po_no, '')                           AS po_no,
                    COALESCE(po.supplier_name, g.supplier_name, '') AS supplier_name,
                    COALESCE(g.received_date, CURDATE())            AS received_date,
                    ROUND(COALESCE(v.grn_value, 0), 2)              AS amount,
                    ROUND(COALESCE(inv.invoiced, 0), 2)             AS invoiced,
                    ROUND(COALESCE(v.grn_value, 0)
                          - COALESCE(inv.invoiced, 0), 2)           AS remaining
            FROM grn_receipts g
            LEFT JOIN purchase_orders po
                   ON po.po_no COLLATE utf8mb4_unicode_ci = g.po_no COLLATE utf8mb4_unicode_ci
            LEFT JOIN (
                SELECT grn_no,
                       SUM(COALESCE(received_qty,0) * COALESCE(unit_price,0)) AS grn_value
                FROM grn_lines GROUP BY grn_no
            ) v ON v.grn_no COLLATE utf8mb4_unicode_ci = g.grn_no COLLATE utf8mb4_unicode_ci
            LEFT JOIN (
                SELECT grn_no, SUM(COALESCE(amount,0)) AS invoiced
                FROM ap_invoices
                WHERE COALESCE(status,'') <> 'Cancelled' AND grn_no IS NOT NULL
                GROUP BY grn_no
            ) inv ON inv.grn_no COLLATE utf8mb4_unicode_ci = g.grn_no COLLATE utf8mb4_unicode_ci
            ORDER BY g.id DESC
            LIMIT 200
        """)).mappings().all()]
    except Exception as exc:
        logging.getLogger(__name__).error(
            "finance: full GRN picker query failed (%s) - falling back", exc)
        try:
            grns = [dict(r) for r in db.execute(text("""
                SELECT  g.grn_no,
                        COALESCE(g.po_no, '')        AS po_no,
                        COALESCE(g.supplier_name,'') AS supplier_name,
                        COALESCE(g.received_date, CURDATE()) AS received_date,
                        ROUND(COALESCE((
                            SELECT SUM(COALESCE(l.received_qty,0)*COALESCE(l.unit_price,0))
                            FROM grn_lines l WHERE l.grn_no = g.grn_no
                        ),0),2) AS amount,
                        0 AS invoiced,
                        ROUND(COALESCE((
                            SELECT SUM(COALESCE(l.received_qty,0)*COALESCE(l.unit_price,0))
                            FROM grn_lines l WHERE l.grn_no = g.grn_no
                        ),0),2) AS remaining
                FROM grn_receipts g
                ORDER BY g.id DESC
                LIMIT 200
            """)).mappings().all()]
            logging.getLogger(__name__).warning(
                "finance: GRN fallback returned %d rows", len(grns))
        except Exception as exc2:
            logging.getLogger(__name__).error(
                "finance: GRN fallback ALSO failed: %s", exc2)
    kpis = {
        # Batch 26: an invoice that is Paid, Closed or Cancelled is NOT an open
        # item. Previously only 'Paid' was excluded, so manually closed and
        # voided invoices kept inflating the outstanding balances.
        "ar_open": _safe_scalar(db, "SELECT COUNT(*) FROM ar_invoices WHERE COALESCE(status,'') NOT IN ('Paid','Closed','Cancelled')"),
        "ar_amount": _safe_scalar(db, "SELECT ROUND(SUM(amount-COALESCE(paid_amount,0)),2) FROM ar_invoices WHERE COALESCE(status,'') NOT IN ('Paid','Closed','Cancelled')", default=0),
        "ap_open": _safe_scalar(db, "SELECT COUNT(*) FROM ap_invoices WHERE COALESCE(status,'') NOT IN ('Paid','Closed','Cancelled')"),
        "ap_amount": _safe_scalar(db, "SELECT ROUND(SUM(amount-COALESCE(paid_amount,0)),2) FROM ap_invoices WHERE COALESCE(status,'') NOT IN ('Paid','Closed','Cancelled')", default=0),
        "drafted": drafted,
    }
    return render(request, "finance/index.html", {"ar": ar, "ap": ap, "payments": payments, "grns": grns, "kpis": kpis, "aging": ar_aging(db), "page_title": "Finance"})


@router.post("/ap/create")
def create_ap_invoice(request: Request, supplier_name: str = Form(""), po_no: str = Form(""), grn_no: str = Form(""), amount: float = Form(0), remarks: str = Form(""), db: Session = Depends(get_db)):
    require_action(request, "finance", "add")
    _ensure_finance_schema(db)

    # Batch 26: basic validation — an AP invoice with no supplier or no value is
    # meaningless and used to be saved silently.
    if not (supplier_name or "").strip():
        return RedirectResponse(
            "/finance?toast=danger&title=AP Invoice&msg=Supplier is required",
            status_code=303)
    if float(amount or 0) <= 0:
        return RedirectResponse(
            "/finance?toast=danger&title=AP Invoice&msg=Amount must be greater than zero",
            status_code=303)

    match_status = "Matched"
    if grn_no:
        grn_value = _safe_scalar(db, "SELECT ROUND(SUM(COALESCE(received_qty,0)*COALESCE(unit_price,0)),2) FROM grn_lines WHERE grn_no=:g", {"g": grn_no}, 0)
        if abs(float(grn_value or 0) - float(amount or 0)) > 0.01:
            match_status = "Variance"
    ap_no = next_document_no(db, _cid(request), "AP", "AP")
    db.execute(text("""
        INSERT INTO ap_invoices (company_id, ap_no, supplier_name, po_no, grn_no, invoice_date, status, amount, match_status, remarks, created_by)
        VALUES (:cid, :ap, :sup, :po, :grn, CURDATE(), 'Open', :amount, :match, :remarks, :by)
    """), {"cid": _cid(request), "ap": ap_no, "sup": supplier_name, "po": po_no, "grn": grn_no, "amount": amount, "match": match_status, "remarks": remarks, "by": _user(request)})
    db.commit()
    if float(amount or 0) > 0:
        # ------------------------------------------------------------------
        # Batch 26 ACCOUNTING FIX.
        # This used to debit 5000 (Purchases / COGS). That DOUBLE-COUNTS cost:
        # the GRN already added the goods to stock, so the value is sitting in
        # 1300 Inventory. Expensing it again at invoice time overstates cost and
        # understates inventory. Correct entry for a stocked purchase is:
        #     Dr 1300 Inventory   /   Cr 2100 Accounts Payable
        # The cost only becomes an expense later, when the stock is issued to
        # production (Dr 5000 COGS / Cr 1300 Inventory).
        # A GRN-less invoice (services, utilities) still expenses to 5000.
        # ------------------------------------------------------------------
        debit_account = "1300" if grn_no else "5000"
        post_journal(db, request, "AP_INVOICE", ap_no,
                     f"AP invoice {ap_no} — {supplier_name}",
                     [(debit_account, float(amount), 0.0, supplier_name),
                      ("2100", 0.0, float(amount), supplier_name)])
    return RedirectResponse(f"/finance?toast=success&title=AP Invoice&msg={ap_no} saved and posted to GL", status_code=303)


# ---------------------------------------------------------------------------
# Batch 26 — DOCUMENT STATUS WORKFLOW (Oracle / SAP B1 style)
# ---------------------------------------------------------------------------
# Oracle and SAP B1 both let you CLOSE a document manually so it stops appearing
# in open-item lists even if it was never fully paid/received (e.g. the supplier
# short-shipped the rest and you agree to write it off).
#
# Statuses used here:
#   Open           — live, awaiting payment
#   Partially Paid — some money applied
#   Paid           — fully settled (set automatically by the payment routine)
#   Closed         — manually closed; excluded from open KPIs and aging
#   Cancelled      — voided
# ---------------------------------------------------------------------------
@router.post("/ap/{ap_no}/status")
def set_ap_status(request: Request, ap_no: str, new_status: str = Form(...), db: Session = Depends(get_db)):
    require_action(request, "finance", "edit")
    _ensure_finance_schema(db)
    allowed = {"Open", "Closed", "Cancelled"}
    if new_status not in allowed:
        return RedirectResponse("/finance?toast=danger&title=AP&msg=Invalid status", status_code=303)
    db.execute(text("UPDATE ap_invoices SET status = :s WHERE ap_no = :a"),
               {"s": new_status, "a": ap_no})
    db.commit()
    return RedirectResponse(
        f"/finance?toast=success&title=AP Invoice&msg={ap_no} marked {new_status}",
        status_code=303)


@router.post("/ar/{invoice_no}/status")
def set_ar_status(request: Request, invoice_no: str, new_status: str = Form(...), db: Session = Depends(get_db)):
    require_action(request, "finance", "edit")
    _ensure_finance_schema(db)
    allowed = {"Open", "Closed", "Cancelled"}
    if new_status not in allowed:
        return RedirectResponse("/finance?toast=danger&title=AR&msg=Invalid status", status_code=303)
    db.execute(text("UPDATE ar_invoices SET status = :s WHERE invoice_no = :i"),
               {"s": new_status, "i": invoice_no})
    db.commit()
    return RedirectResponse(
        f"/finance?toast=success&title=AR Invoice&msg={invoice_no} marked {new_status}",
        status_code=303)


@router.post("/payment/create")
def create_payment(request: Request, party_type: str = Form(...), party_name: str = Form(""), reference_no: str = Form(""), amount: float = Form(0), method: str = Form("Bank"), remarks: str = Form(""), db: Session = Depends(get_db)):
    require_action(request, "finance", "add")
    _ensure_finance_schema(db)
    pay_no = next_document_no(db, _cid(request), "PAY", "PAY")
    db.execute(text("""
        INSERT INTO finance_payments (company_id, payment_no, party_type, party_name, reference_no, payment_date, amount, method, remarks, created_by)
        VALUES (:cid, :pay, :ptype, :party, :ref, CURDATE(), :amount, :method, :remarks, :by)
    """), {"cid": _cid(request), "pay": pay_no, "ptype": party_type, "party": party_name, "ref": reference_no, "amount": amount, "method": method, "remarks": remarks, "by": _user(request)})
    if party_type.upper() == "CUSTOMER" and reference_no:
        db.execute(text("UPDATE ar_invoices SET paid_amount=LEAST(amount, paid_amount+:a), status=CASE WHEN paid_amount+:a >= amount THEN 'Paid' ELSE 'Partially Paid' END WHERE invoice_no=:r"), {"a": amount, "r": reference_no})
    if party_type.upper() == "SUPPLIER" and reference_no:
        db.execute(text("UPDATE ap_invoices SET paid_amount=LEAST(amount, paid_amount+:a), status=CASE WHEN paid_amount+:a >= amount THEN 'Paid' ELSE 'Partially Paid' END WHERE ap_no=:r"), {"a": amount, "r": reference_no})
    db.commit()
    if float(amount or 0) > 0:
        if party_type.upper() == "CUSTOMER":
            post_journal(db, request, "PAYMENT_IN", pay_no,
                         f"Customer payment {pay_no} ({party_name})",
                         [("1000", float(amount), 0.0, party_name),
                          ("1200", 0.0, float(amount), party_name)])
        elif party_type.upper() == "SUPPLIER":
            post_journal(db, request, "PAYMENT_OUT", pay_no,
                         f"Supplier payment {pay_no} ({party_name})",
                         [("2100", float(amount), 0.0, party_name),
                          ("1000", 0.0, float(amount), party_name)])
    return RedirectResponse("/finance?toast=success&title=Payment&msg=Payment recorded", status_code=303)


# ============================================================================
# Batch 13 — AR lifecycle (Draft -> Posted) + AR aging buckets
# ============================================================================

@router.post("/ar/{invoice_no}/post")
def post_ar_invoice(request: Request, invoice_no: str, db: Session = Depends(get_db)):
    """Post a draft AR invoice: locks it into the receivable ledger."""
    require_action(request, "finance", "edit")
    _ensure_finance_schema(db)
    db.execute(text("""
        UPDATE ar_invoices SET status = 'Posted'
        WHERE invoice_no = :i AND status = 'Draft'
    """), {"i": invoice_no})
    db.commit()
    inv = db.execute(text("SELECT customer_name, COALESCE(amount,0) AS amount FROM ar_invoices WHERE invoice_no=:i"), {"i": invoice_no}).mappings().first()
    if inv and float(inv["amount"] or 0) > 0:
        post_journal(db, request, "AR_INVOICE", invoice_no,
                     f"AR invoice {invoice_no} posted — {inv['customer_name']}",
                     [("1200", float(inv["amount"]), 0.0, inv["customer_name"]),
                      ("4000", 0.0, float(inv["amount"]), inv["customer_name"])])
    return RedirectResponse(f"/finance?toast=success&title=AR Posted&msg=Invoice {invoice_no} posted to receivables and GL", status_code=303)


@router.post("/ar/{invoice_no}/cancel")
def cancel_ar_invoice(request: Request, invoice_no: str, db: Session = Depends(get_db)):
    require_action(request, "finance", "delete")
    _ensure_finance_schema(db)
    db.execute(text("""
        UPDATE ar_invoices SET status = 'Cancelled'
        WHERE invoice_no = :i AND status IN ('Draft','Posted') AND COALESCE(paid_amount,0) = 0
    """), {"i": invoice_no})
    db.commit()
    return RedirectResponse(f"/finance?toast=success&title=AR Cancelled&msg=Invoice {invoice_no} cancelled", status_code=303)


def ar_aging(db: Session) -> list[dict]:
    """0-30 / 31-60 / 61-90 / 90+ day buckets on open AR (per invoice_date)."""
    try:
        rows = db.execute(text("""
            SELECT CASE
                     WHEN DATEDIFF(CURDATE(), invoice_date) <= 30 THEN '0-30 days'
                     WHEN DATEDIFF(CURDATE(), invoice_date) <= 60 THEN '31-60 days'
                     WHEN DATEDIFF(CURDATE(), invoice_date) <= 90 THEN '61-90 days'
                     ELSE '90+ days'
                   END AS bucket,
                   COUNT(*) AS invoices,
                   ROUND(SUM(COALESCE(amount,0)-COALESCE(paid_amount,0)),2) AS outstanding
            FROM ar_invoices
            WHERE COALESCE(status,'') NOT IN ('Paid','Cancelled')
            GROUP BY 1
        """)).mappings().all()
        order = ['0-30 days', '31-60 days', '61-90 days', '90+ days']
        found = {r["bucket"]: dict(r) for r in rows}
        return [found.get(b, {"bucket": b, "invoices": 0, "outstanding": 0.0}) for b in order]
    except Exception:
        return [{"bucket": b, "invoices": 0, "outstanding": 0.0}
                for b in ('0-30 days', '31-60 days', '61-90 days', '90+ days')]


# ============================================================================
# Batch 14 — General Ledger foundation + per-company document numbering
# ============================================================================
# - Chart of accounts (seeded once, editable later)
# - Journals auto-posted by business events:
#     AR posted            DR 1200 Accounts Receivable / CR 4000 Sales Revenue
#     Customer payment     DR 1000 Cash & Bank        / CR 1200 Accounts Receivable
#     AP posted            DR 5000 Purchases/COGS     / CR 2100 Accounts Payable
#     Supplier payment     DR 2100 Accounts Payable   / CR 1000 Cash & Bank
# - Per-company sequences: document_sequences(company_id, doc_type) -> next no
# - /finance/gl : journal browser + live trial balance

COA_SEED = [
    ("1000", "Cash & Bank", "Asset"),
    ("1200", "Accounts Receivable", "Asset"),
    ("1300", "Inventory", "Asset"),
    ("2100", "Accounts Payable", "Liability"),
    ("3000", "Owner Equity", "Equity"),
    ("4000", "Sales Revenue", "Income"),
    ("5000", "Purchases / COGS", "Expense"),
    ("5900", "Production Wastage", "Expense"),
]


def _ensure_gl_schema(db: Session) -> None:
    def _try(sql):
        try:
            db.execute(text(sql))
        except Exception:
            db.rollback()
    _try("""
        CREATE TABLE IF NOT EXISTS gl_accounts (
            id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
            company_id INT NULL,
            account_code VARCHAR(20) NOT NULL,
            account_name VARCHAR(255) NOT NULL,
            account_type VARCHAR(30) NOT NULL,
            is_active TINYINT(1) NOT NULL DEFAULT 1,
            UNIQUE KEY uq_gl_acct (company_id, account_code)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)
    _try("""
        CREATE TABLE IF NOT EXISTS gl_journals (
            id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
            company_id INT NULL,
            journal_no VARCHAR(80) NOT NULL UNIQUE,
            journal_date DATE NULL,
            source_type VARCHAR(40) NULL,
            source_no VARCHAR(80) NULL,
            memo VARCHAR(500) NULL,
            created_by VARCHAR(120) NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            KEY idx_gl_src (source_type, source_no)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)
    _try("""
        CREATE TABLE IF NOT EXISTS gl_journal_lines (
            id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
            journal_no VARCHAR(80) NOT NULL,
            account_code VARCHAR(20) NOT NULL,
            debit DECIMAL(18,4) NOT NULL DEFAULT 0,
            credit DECIMAL(18,4) NOT NULL DEFAULT 0,
            party VARCHAR(255) NULL,
            KEY idx_gll_journal (journal_no),
            KEY idx_gll_account (account_code)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)
    _try("""
        CREATE TABLE IF NOT EXISTS document_sequences (
            id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
            company_id INT NOT NULL,
            doc_type VARCHAR(30) NOT NULL,
            prefix VARCHAR(20) NOT NULL,
            next_no INT NOT NULL DEFAULT 1,
            UNIQUE KEY uq_seq (company_id, doc_type)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)
    # seed chart of accounts (idempotent)
    for code, name, typ in COA_SEED:
        try:
            n = db.execute(text(
                "SELECT COUNT(*) FROM gl_accounts WHERE account_code=:c AND COALESCE(company_id,0)=0"
            ), {"c": code}).scalar()
            if not n:
                db.execute(text("""
                    INSERT INTO gl_accounts (company_id, account_code, account_name, account_type)
                    VALUES (NULL, :c, :n, :t)
                """), {"c": code, "n": name, "t": typ})
        except Exception:
            db.rollback()
    try:
        db.commit()
    except Exception:
        db.rollback()


def next_document_no(db: Session, company_id: int, doc_type: str, prefix: str) -> str:
    """Per-company sequence: AR/AP/PAY/JV numbering like AR-C1-000123."""
    _ensure_gl_schema(db)
    try:
        row = db.execute(text("""
            SELECT next_no FROM document_sequences WHERE company_id=:c AND doc_type=:t
        """), {"c": company_id, "t": doc_type}).scalar()
        if row is None:
            db.execute(text("""
                INSERT INTO document_sequences (company_id, doc_type, prefix, next_no)
                VALUES (:c, :t, :p, 2)
            """), {"c": company_id, "t": doc_type, "p": prefix})
            n = 1
        else:
            n = int(row)
            db.execute(text("""
                UPDATE document_sequences SET next_no = next_no + 1
                WHERE company_id=:c AND doc_type=:t
            """), {"c": company_id, "t": doc_type})
        db.commit()
        return f"{prefix}-C{company_id}-{n:06d}"
    except Exception:
        db.rollback()
        # fallback to the legacy date-based numbering, never block the document
        return _next_no(db, "gl_journals", "journal_no", prefix)


def post_journal(db: Session, request: Request, source_type: str, source_no: str,
                 memo: str, lines: list[tuple[str, float, float, str]]) -> str | None:
    """Create a balanced journal. lines = [(account_code, debit, credit, party)].
    Skips silently if already posted for this source (idempotent) or unbalanced."""
    _ensure_gl_schema(db)
    try:
        dup = db.execute(text("""
            SELECT COUNT(*) FROM gl_journals WHERE source_type=:st AND source_no=:sn
        """), {"st": source_type, "sn": source_no}).scalar()
        if dup:
            return None
        total_dr = round(sum(l[1] for l in lines), 4)
        total_cr = round(sum(l[2] for l in lines), 4)
        if total_dr <= 0 or abs(total_dr - total_cr) > 0.005:
            return None
        cid = _cid(request)
        jno = next_document_no(db, cid, "JV", "JV")
        db.execute(text("""
            INSERT INTO gl_journals (company_id, journal_no, journal_date, source_type, source_no, memo, created_by)
            VALUES (:cid, :j, CURDATE(), :st, :sn, :m, :by)
        """), {"cid": cid, "j": jno, "st": source_type, "sn": source_no, "m": memo, "by": _user(request)})
        for code, dr, cr, party in lines:
            db.execute(text("""
                INSERT INTO gl_journal_lines (journal_no, account_code, debit, credit, party)
                VALUES (:j, :a, :d, :c, :p)
            """), {"j": jno, "a": code, "d": dr, "c": cr, "p": party})
        db.commit()
        return jno
    except Exception:
        db.rollback()
        return None


@router.get("/gl")
def general_ledger(request: Request, db: Session = Depends(get_db)):
    """Journal browser + trial balance."""
    require_area(request, "finance")
    _ensure_gl_schema(db)

    journals = []
    try:
        journals = db.execute(text("""
            SELECT j.journal_no, j.journal_date, j.source_type, j.source_no, j.memo,
                   ROUND(SUM(l.debit),2) AS total
            FROM gl_journals j LEFT JOIN gl_journal_lines l ON l.journal_no = j.journal_no
            GROUP BY j.journal_no, j.journal_date, j.source_type, j.source_no, j.memo
            ORDER BY MAX(j.id) DESC LIMIT 200
        """)).mappings().all()
    except Exception:
        pass

    trial = []
    try:
        trial = db.execute(text("""
            SELECT a.account_code, a.account_name, a.account_type,
                   ROUND(COALESCE(SUM(l.debit),0),2) AS debit,
                   ROUND(COALESCE(SUM(l.credit),0),2) AS credit,
                   ROUND(COALESCE(SUM(l.debit),0) - COALESCE(SUM(l.credit),0),2) AS balance
            FROM gl_accounts a
            LEFT JOIN gl_journal_lines l ON l.account_code = a.account_code
            GROUP BY a.account_code, a.account_name, a.account_type
            ORDER BY a.account_code
        """)).mappings().all()
    except Exception:
        pass
    totals = {
        "debit": round(sum(float(t["debit"] or 0) for t in trial), 2),
        "credit": round(sum(float(t["credit"] or 0) for t in trial), 2),
    }
    return render(request, "finance/gl.html", {
        "journals": journals, "trial": trial, "totals": totals,
        "page_title": "General Ledger",
    })
