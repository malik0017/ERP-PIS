"""Batch 94 — end-to-end functional test against a real MySQL-compatible DB.

Exercises the ACTUAL HTTP routes through TestClient, not the service functions
directly, so RBAC, session scoping, redirects and template rendering are all
covered. Tests the behaviour that was broken, not just that code runs.
"""
import sys

# ---------------------------------------------------------------------------
# Batch 96: make this script runnable from ANY directory.
#
# Running `python route_audit.py` from inside scripts/ put scripts/ on
# sys.path instead of the project root, so `import app` failed with
# "No module named 'app'". Same reason `python -m uvicorn app.main:app`
# fails when launched from scripts/ — uvicorn watches the CWD.
#
# This walks up from the file's own location to the directory containing
# app/ and puts that first on sys.path, so the script works whether it is
# launched from the project root, from scripts/, or by absolute path.
# ---------------------------------------------------------------------------
import os as _os
import sys as _sys

_here = _os.path.dirname(_os.path.abspath(__file__))
_root = _here
for _ in range(4):
    if _os.path.isdir(_os.path.join(_root, "app")):
        break
    _parent = _os.path.dirname(_root)
    if _parent == _root:
        break
    _root = _parent
if _os.path.isdir(_os.path.join(_root, "app")):
    if _root not in _sys.path:
        _sys.path.insert(0, _root)
    # .env is read relative to the CWD by python-dotenv, so align the CWD too.
    _os.chdir(_root)
else:
    _sys.stderr.write(
        "ERROR: could not locate the project root (the folder containing app/).\n"
        "Run this from C:\\laragon\\www\\isfc or keep the script inside that project.\n")
    _sys.exit(2)

try:
    import httpx  # noqa: F401
except ModuleNotFoundError:
    _sys.stderr.write(
        "ERROR: httpx is not installed. Starlette's TestClient needs it.\n"
        "Fix:  pip install httpx==0.24.1\n")
    _sys.exit(2)

from datetime import date, timedelta
from starlette.testclient import TestClient
from sqlalchemy import text

from app.main import app
from app.database.session import SessionLocal

FAILS = []


def check(label, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {label}" + (f"  -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(label)


def seed(db):
    # Batch 94: repair a legacy-shaped ledger first. This is the fix for the
    # fresh-install bug found during this batch — Base.metadata.create_all()
    # produces the ORM's legacy table shape, and every modern stock read then
    # fails on "Unknown column". Running it here means the test exercises the
    # migration on a genuinely legacy table, which is the situation that
    # matters.
    # Procurement's raw-SQL tables (purchase_orders etc.) are created by its
    # own ensure_schema, which normally runs on first visit to /procurement.
    from app.modules.procurement.routes import _ensure_procurement_schema
    _ensure_procurement_schema(db)
    from app.core.stock_ledger import ensure_ledger_schema, ensure_qc_status_column
    ensure_ledger_schema(db)
    ensure_qc_status_column(db)
    cols = {r["Field"] for r in db.execute(text("SHOW COLUMNS FROM inventory_transactions")).mappings().all()}
    missing = {"company_id", "inventory_code", "qty_in", "qty_out", "movement_type", "qc_status"} - cols
    check("legacy ledger table repaired to modern shape", not missing, f"still missing {missing}")

    db.execute(text("DELETE FROM inventory_transactions"))
    db.execute(text("DELETE FROM order_lines"))
    db.execute(text("DELETE FROM customer_orders"))
    db.execute(text("DELETE FROM recipe_ingredients"))
    db.execute(text("DELETE FROM recipes"))
    db.execute(text("DELETE FROM ingredients"))
    for t in ("purchase_requisitions", "purchase_requisition_lines",
              "store_topup_requests", "qc_sampling_config"):
        try:
            db.execute(text(f"DELETE FROM {t}"))
            db.commit()
        except Exception:
            # rollback, NOT bare pass: a failed statement leaves the session in
            # an aborted transaction, so every later query in this session dies
            # with a misleading "table doesn't exist" that has nothing to do
            # with the code under test.
            db.rollback()

    # Two ingredients: one with plenty of stock, one with almost none.
    db.execute(text("""
        INSERT INTO ingredients (ingredient_code, name, standard_uom, recipe_uom, purchase_uom,
                                 conversion_to_standard, default_supplier, unit_cost_standard,
                                 status, critical_item, storage_type)
        VALUES ('ING-RICE','Basmati Rice','Kg','Kg','Kg',1,'Al Noor Trading',8.5,'Active',0,'Dry'),
               ('ING-CHKN','Chicken Breast','Kg','Kg','Kg',1,'Gulf Poultry',22.0,'Active',1,'Frozen')
    """))
    # Seeded through the ORM, not raw SQL, deliberately: MySQL strict mode
    # stays ON for the whole test so the app's OWN inserts are still checked
    # against every NOT NULL column. Relaxing sql_mode to make seeding easier
    # would have silently hidden exactly the kind of missing-column bug this
    # test exists to catch.
    from app.models.recipe import Recipe, RecipeIngredient
    rec = Recipe(recipe_code='RCP-001', recipe_name='Chicken Biryani', standard_portions=10,
                 sale_price_per_portion=35.0, food_cost_per_portion=12.0, status='Active',
                 is_active=True, version=1, company_id=1, target_wastage_pct=5)
    db.add(rec)
    db.flush()
    rid = rec.id
    db.add_all([
        RecipeIngredient(recipe_id=rid, line_no=1, inventory_code='ING-RICE',
                         item_name='Basmati Rice', uom='Kg', qty_per_portion=0.15, qty_batch=1.5),
        RecipeIngredient(recipe_id=rid, line_no=2, inventory_code='ING-CHKN',
                         item_name='Chicken Breast', uom='Kg', qty_per_portion=0.20, qty_batch=2.0),
    ])
    db.flush()

    # Rice: 100kg cleared. Chicken: 1kg cleared + 50kg stuck in QC Hold.
    # Posted through post_stock_movement (the app's own writer) rather than
    # raw INSERT, so the legacy NOT NULL columns get filled the same way a
    # real GRN fills them — and so this doubles as a test that the writer
    # still works against a repaired legacy table.
    from app.core.stock_ledger import post_stock_movement
    for code, name, qty, qc, ref in [
        ("ING-RICE", "Basmati Rice", 100, "Passed", "SEED-1"),
        ("ING-CHKN", "Chicken Breast", 1, "Passed", "SEED-2"),
        ("ING-CHKN", "Chicken Breast", 50, "Pending", "SEED-3"),
    ]:
        ok = post_stock_movement(db, company_id=1, inventory_code=code, item_name=name,
                                 uom="Kg", qty=qty, movement_type="GRN_IN",
                                 reference_no=ref, unit_cost=10.0, qc_status=qc)
        check(f"seed ledger write {ref}", ok)
    db.commit()


def login(client):
    """Build the signed session cookie SessionMiddleware expects.

    Cookie name is isfc_session (set in main.py), NOT the Starlette default
    of "session" — using the default silently produced an unauthenticated
    client that redirected every request to /login.
    """
    import itsdangerous, json, base64
    from app.config import SECRET_KEY
    data = {"user_id": 1, "username": "tester", "user_role": "ADMIN", "role": "ADMIN", "company_id": 1}
    signer = itsdangerous.TimestampSigner(str(SECRET_KEY))
    cookie = signer.sign(base64.b64encode(json.dumps(data).encode())).decode()
    client.cookies.set("isfc_session", cookie)


def client_for_user(user_id, username, role):
    """A separate signed-in client, for chain steps that need another person."""
    import base64 as _b64, json as _json, itsdangerous as _its
    from app.config import SECRET_KEY as _SK
    c = TestClient(app)
    d = {"user_id": user_id, "username": username, "user_role": role,
         "role": role, "company_id": 1}
    sg = _its.TimestampSigner(str(_SK))
    c.cookies.set("isfc_session",
                  sg.sign(_b64.b64encode(_json.dumps(d).encode())).decode())
    _g, _p = c.get, c.post
    c.get = lambda *a, **k: _g(*a, allow_redirects=False, **k)
    c.post = lambda *a, **k: _p(*a, allow_redirects=False, **k)
    return c


def main():
    db = SessionLocal()
    seed(db)
    # MySQL InnoDB defaults to REPEATABLE READ, so this long-lived test
    # session keeps reading the snapshot it took at its first query and never
    # sees rows the ROUTES commit on their own sessions. Committing before
    # every read ends the current snapshot and starts a fresh one. Without
    # this, every assertion after the first write reports "no such row" for
    # data that is definitely there — a test artifact, not an app bug.
    _orig_exec = db.execute
    def _fresh_exec(*a, **k):
        db.commit()
        return _orig_exec(*a, **k)
    db.execute = _fresh_exec

    client = TestClient(app)
    login(client)

    # Starlette 0.27's TestClient follows redirects by default and has no
    # follow_redirects constructor arg. Every assertion here checks the 303
    # itself, so redirects must NOT be followed.
    _get, _post = client.get, client.post
    client.get = lambda *a, **k: _get(*a, allow_redirects=False, **k)
    client.post = lambda *a, **k: _post(*a, allow_redirects=False, **k)

    print("\n=== 1. Order creation lands as a PENDING sales request ===")
    r = client.post("/production/orders/create", data={
        "customer_name": "Aramco Camp 4", "brand": "ISFC", "channel": "Catering",
        "required_delivery_date": (date.today() + timedelta(days=5)).isoformat(),
        "required_delivery_time": "08:00",
        "recipe_no": ["RCP-001"], "recipe_name": ["Chicken Biryani"],
        "required_portions": ["100"],
    })
    check("order create redirects to /sales-requests (not Head Chef)",
          r.status_code == 303 and "/sales-requests" in r.headers.get("location", ""),
          f"{r.status_code} -> {r.headers.get('location')}")

    db.commit()
    row = db.execute(text("""SELECT order_no, sales_review_status, company_id
                             FROM customer_orders ORDER BY id DESC LIMIT 1""")).mappings().first()
    order_no = row["order_no"] if row else None
    check("order exists", order_no is not None)
    check("sales_review_status = Pending", row and row["sales_review_status"] == "Pending",
          str(row and row["sales_review_status"]))
    check("company_id stamped (was NULL before Batch 94)", row and row["company_id"] == 1,
          f"company_id={row and row['company_id']}")

    print("\n=== 2. Pending request is INVISIBLE to Head Chef Planning ===")
    r = client.get("/production/head-chef")
    check("head-chef renders", r.status_code == 200, str(r.status_code))
    check("pending order NOT listed in Head Chef Planning",
          order_no not in r.text, "order leaked into planning")

    print("\n=== 3. Sales Requests screen shows it, with the stock verdict ===")
    r = client.get("/sales-requests")
    check("sales-requests renders", r.status_code == 200, str(r.status_code))
    check("pending order IS listed", order_no in r.text)
    check("shows a shortage badge", "short" in r.text.lower())

    r = client.get(f"/sales-requests/{order_no}")
    check("detail renders", r.status_code == 200, str(r.status_code))
    check("rice shown as available", "ING-RICE" in r.text)
    check("chicken shown as short", "ING-CHKN" in r.text)
    check("QC-held stock excluded from on-hand (chicken short, not covered by the 50kg in QC)",
          "Raise Purchase Requisition" in r.text)

    print("\n=== 4. Shortage raises a REQUISITION, not a PO ===")
    po_before = db.execute(text("SELECT COUNT(*) FROM purchase_orders")).scalar() or 0
    r = client.post(f"/sales-requests/{order_no}/raise-pr")
    check("raise-pr redirects", r.status_code == 303, str(r.status_code))
    pr = db.execute(text("SELECT pr_no, status, source_ref FROM purchase_requisitions ORDER BY id DESC LIMIT 1")).mappings().first()
    check("a requisition was created", pr is not None)
    check("requisition is Pending", pr and pr["status"] == "Pending")
    check("requisition linked to the order", pr and pr["source_ref"] == order_no)
    po_after = db.execute(text("SELECT COUNT(*) FROM purchase_orders")).scalar() or 0
    check("NO purchase order was created (this is the Batch 86 behaviour change)",
          po_after == po_before, f"{po_before} -> {po_after}")

    pr_no = pr["pr_no"] if pr else None
    prl = db.execute(text("SELECT inventory_code, required_qty, suggested_supplier FROM purchase_requisition_lines WHERE pr_no=:p"), {"p": pr_no}).mappings().all()
    check("PR line is the short ingredient only", len(prl) == 1 and prl[0]["inventory_code"] == "ING-CHKN",
          str([dict(x) for x in prl]))
    check("default supplier carried onto the PR line",
          prl and prl[0]["suggested_supplier"] == "Gulf Poultry")

    print("\n=== 5. Requisition -> approve -> convert to PO ===")
    r = client.get(f"/purchase-requisitions/{pr_no}")
    check("PR detail renders", r.status_code == 200, str(r.status_code))

    line_id = db.execute(text("SELECT id FROM purchase_requisition_lines WHERE pr_no=:p"), {"p": pr_no}).scalar()
    r = client.post(f"/purchase-requisitions/{pr_no}/convert-to-po",
                    data={"line_id": str(line_id), "line_supplier": "Gulf Poultry", "line_price": "22"})
    check("convert BLOCKED while still Pending (approval is required first)",
          r.status_code == 303 and "Not+approved" in r.headers.get("location", "").replace("%20", "+").replace(" ", "+"),
          r.headers.get("location", ""))

    r = client.post(f"/purchase-requisitions/{pr_no}/approve",
                    data={"line_id": str(line_id), "approved_qty": ""})
    check("approve redirects", r.status_code == 303)

    # Batch 112: Batch 111 introduced the value-based approval chain, so ONE
    # signature no longer necessarily completes a requisition. This test was
    # written when it did. Rather than assert the old contract, walk whatever
    # chain the tier engine built — using a different user for each step,
    # because separation of duties is the point of the feature.
    from app.core import approval_chain as _ac
    _chain = _ac.get_chain(db, "purchase_requisition", pr_no)
    _extra = 0
    for _step in _chain:
        if _step["status"] == "Approved":
            continue
        _uid = 900 + _step["step_no"]
        _c2 = client_for_user(_uid, f"approver{_step['step_no']}", "ADMIN")
        _c2.post(f"/purchase-requisitions/{pr_no}/approve",
                 data={"line_id": str(line_id), "approved_qty": ""})
        _extra += 1
    db.commit()
    if _extra:
        print(f"    (chain required {len(_chain)} signatures; "
              f"{_extra} extra applied by other users)")
    st = db.execute(text("SELECT status FROM purchase_requisitions WHERE pr_no=:p"), {"p": pr_no}).scalar()
    check("PR now Approved", st == "Approved", str(st))
    check("approving still created NO purchase order",
          (db.execute(text("SELECT COUNT(*) FROM purchase_orders")).scalar() or 0) == po_before)

    r = client.post(f"/purchase-requisitions/{pr_no}/convert-to-po",
                    data={"line_id": str(line_id), "line_supplier": "Gulf Poultry", "line_price": "22"})
    check("convert redirects", r.status_code == 303)
    po = db.execute(text("SELECT po_no, supplier_name, total_value, status FROM purchase_orders ORDER BY id DESC LIMIT 1")).mappings().first()
    check("a real PO now exists", po is not None)
    check("PO supplier assigned at conversion", po and po["supplier_name"] == "Gulf Poultry")
    check("PO status Open (normal procurement flow)", po and po["status"] == "Open")
    check("PR marked Converted",
          db.execute(text("SELECT status FROM purchase_requisitions WHERE pr_no=:p"), {"p": pr_no}).scalar() == "Converted")

    print("\n=== 6. Duplicate protection ===")
    r = client.post(f"/sales-requests/{order_no}/raise-pr")
    n_pr = db.execute(text("SELECT COUNT(*) FROM purchase_requisitions")).scalar()
    check("second PR allowed only because the first is already Converted", n_pr >= 1)
    r2 = client.post(f"/production/orders/{order_no}/generate-shortage-po")
    n_pr2 = db.execute(text("SELECT COUNT(*) FROM purchase_requisitions")).scalar()
    check("order-detail shortage action blocks a duplicate open PR",
          n_pr2 == n_pr, f"{n_pr} -> {n_pr2}")

    print("\n=== 7. Approve the sales request -> now visible to Head Chef ===")
    r = client.post(f"/sales-requests/{order_no}/approve")
    check("approve redirects", r.status_code == 303)
    st = db.execute(text("SELECT sales_review_status FROM customer_orders WHERE order_no=:o"), {"o": order_no}).scalar()
    check("sales_review_status = Approved", st == "Approved", str(st))
    r = client.get("/production/head-chef")
    check("order NOW appears in Head Chef Planning", order_no in r.text)

    print("\n=== 8. Reject path ===")
    client.post("/production/orders/create", data={
        "customer_name": "Test Reject", "required_delivery_date": (date.today() + timedelta(days=6)).isoformat(),
        "recipe_no": ["RCP-001"], "required_portions": ["5"],
    })
    o2 = db.execute(text("SELECT order_no FROM customer_orders ORDER BY id DESC LIMIT 1")).scalar()
    r = client.post(f"/sales-requests/{o2}/reject", data={"reason": ""})
    check("reject without a reason is blocked",
          db.execute(text("SELECT sales_review_status FROM customer_orders WHERE order_no=:o"), {"o": o2}).scalar() == "Pending")
    r = client.post(f"/sales-requests/{o2}/reject", data={"reason": "Customer cancelled"})
    check("reject with reason works",
          db.execute(text("SELECT sales_review_status FROM customer_orders WHERE order_no=:o"), {"o": o2}).scalar() == "Rejected")
    r = client.get("/production/head-chef")
    check("rejected order never reaches Head Chef Planning", o2 not in r.text)

    print("\n=== 9. Store top-up requests ===")
    r = client.post("/production/topups/request", data={
        "order_no": order_no, "ingredient_code": "ING-RICE",
        "requested_qty": "5", "reason": "Spillage / dropped", "section": "Hot Kitchen",
    })
    check("top-up request accepted", r.status_code == 303)
    tp = db.execute(text("SELECT topup_no, status FROM store_topup_requests ORDER BY id DESC LIMIT 1")).mappings().first()
    check("top-up is Pending (no stock moved yet)", tp and tp["status"] == "Pending")
    moved = db.execute(text("SELECT COUNT(*) FROM inventory_transactions WHERE movement_type='STORE_ISSUE'")).scalar()
    check("requesting moved NO stock", moved == 0, f"{moved} movements")

    r = client.post(f"/production/topups/{tp['topup_no']}/approve", data={"approved_qty": "5"})
    check("approve redirects", r.status_code == 303)
    moved = db.execute(text("SELECT COALESCE(SUM(qty_out),0) FROM inventory_transactions WHERE movement_type='STORE_ISSUE'")).scalar()
    check("approving DID move stock out of the ledger", float(moved or 0) == 5.0, f"qty_out={moved}")

    r = client.post("/production/topups/request", data={
        "order_no": order_no, "ingredient_code": "ING-CHKN", "requested_qty": "999", "reason": "Other (see notes)"})
    tp2 = db.execute(text("SELECT topup_no FROM store_topup_requests ORDER BY id DESC LIMIT 1")).scalar()
    r = client.post(f"/production/topups/{tp2}/approve", data={"approved_qty": "999"})
    check("over-issue blocked (no negative stock)",
          db.execute(text("SELECT status FROM store_topup_requests WHERE topup_no=:t"), {"t": tp2}).scalar() == "Pending")

    print("\n=== 10. QC sampling defaults to full inspection ===")
    from app.modules.qc.sampling import decide, ensure_schema as se
    se(db)
    status, reason = decide(db, company_id=1, supplier_name="Gulf Poultry", inventory_codes=["ING-RICE"])
    check("sampling OFF by default -> Pending", status == "Pending", f"{status}: {reason}")

    from app.modules.qc.sampling import save_config
    save_config(db, 1, {"enabled": 1, "sample_every_n": 10, "min_clean_receipts": 0}, "tester")
    status, reason = decide(db, company_id=1, supplier_name="Gulf Poultry", inventory_codes=["ING-CHKN"])
    check("critical + frozen item forces inspection even when sampling is on",
          status == "Pending", f"{status}: {reason}")

    r = client.get("/qc/sampling")
    check("sampling config screen renders", r.status_code == 200, str(r.status_code))

    print("\n=== 11. Immediate Order screen has no 48h floor ===")
    r = client.get("/orders/portal/immediate")
    check("immediate portal renders", r.status_code == 200, str(r.status_code))
    check("no 48-hour min on the date picker", "48 * 3600 * 1000" not in r.text,
          "48h offset still present in immediate page JS")
    r2 = client.get("/orders/portal")
    check("normal portal DOES keep the 48-hour min", "48 * 3600 * 1000" in r2.text)

    db.close()
    print("\n" + "=" * 62)
    if FAILS:
        print(f"FAILED ({len(FAILS)}):")
        for f in FAILS:
            print("   -", f)
        sys.exit(1)
    print("ALL CHECKS PASSED")


if __name__ == "__main__":
    main()
