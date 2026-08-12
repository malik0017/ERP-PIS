"""ISFC PIMS — workflow lock test (Batch 100).

Once work moves forward, the stage it left must close behind it. Three locks
were added this batch and this pins them shut:

  1. KITCHEN — a section cannot transfer a product it has not produced.
     "Without complete information the order cannot move to the next
     section": yield and wastage are recorded at production, so transferring
     first leaves a permanent hole in the reporting exactly where the work
     happened.

  2. KITCHEN — a section cannot re-produce or re-transfer work it has already
     handed on. This one was a live integrity bug, not just a missing guard:
     produce_final_product() used ON DUPLICATE KEY UPDATE and unconditionally
     set status back to 'PRODUCED', so a chef reopening an old order in a
     section that had already transferred it would silently drag the product
     BACK out of the next section's queue. The next section would simply stop
     seeing work it had been given, with no error raised anywhere.

  3. DISPATCH — a Delivered record is locked. Before this it stayed fully
     editable: you could satisfy the Batch 80 proof-of-delivery gate with a
     photo, save, then quietly rewrite the quantities and driver the proof was
     attached to, leaving the delivery note and the AR invoice raised from it
     unreconcilable with their own source record.

USAGE
    python scripts/test_workflow_locks.py
    python scripts/test_workflow_locks.py --verbose
"""
from __future__ import annotations

import os as _os
import sys as _sys

_here = _os.path.dirname(_os.path.abspath(__file__))
_root = _here
for _ in range(4):
    if _os.path.isdir(_os.path.join(_root, "app")):
        break
    _p = _os.path.dirname(_root)
    if _p == _root:
        break
    _root = _p
if not _os.path.isdir(_os.path.join(_root, "app")):
    _sys.stderr.write("ERROR: could not locate the project root (folder containing app/).\n")
    _sys.exit(2)
if _root not in _sys.path:
    _sys.path.insert(0, _root)
_os.chdir(_root)

try:
    import httpx  # noqa: F401
except ModuleNotFoundError:
    _sys.stderr.write("ERROR: httpx missing.  pip install httpx==0.24.1\n")
    _sys.exit(2)

import base64
import json

import itsdangerous
from sqlalchemy import text
from starlette.testclient import TestClient

from app.main import app
from app.config import SECRET_KEY
from app.database.session import SessionLocal
import app.core.kitchen_production as kp

VERBOSE = "--verbose" in _sys.argv
FAILS: list[str] = []

ORDER = "ORD-LOCKTEST"
SECTION = "Hot Kitchen"
RECIPE = "RCP-LOCKTEST"


def check(label, ok, detail=""):
    if ok:
        if VERBOSE:
            print(f"  [PASS] {label}")
    else:
        print(f"  [FAIL] {label}" + (f"  -- {detail}" if detail else ""))
        FAILS.append(label)


def admin_client():
    c = TestClient(app)
    d = {"user_id": 1, "username": "locktest", "user_role": "ADMIN",
         "role": "ADMIN", "company_id": 1}
    s = itsdangerous.TimestampSigner(str(SECRET_KEY))
    c.cookies.set("isfc_session",
                  s.sign(base64.b64encode(json.dumps(d).encode())).decode())
    return c


def main() -> int:
    db = SessionLocal()
    _orig = db.execute
    db.execute = lambda *a, **k: (db.commit(), _orig(*a, **k))[1]

    print("\n=== 1. Kitchen section locks ===")
    kp.ensure_schema(db)
    db.execute(text("DELETE FROM kitchen_production WHERE order_no=:o"), {"o": ORDER})
    db.execute(text("DELETE FROM kitchen_section_transactions WHERE order_no=:o"), {"o": ORDER})
    db.commit()

    blocked = False
    try:
        kp.transfer_product(db, ORDER, SECTION, RECIPE, "", user="tester")
    except kp.SectionLocked:
        blocked = True
    check("cannot transfer a product that was never produced", blocked,
          "work moved forward with no production recorded")

    kp.produce_final_product(db, ORDER, SECTION, RECIPE, 10, 1, "tester")
    check("production recorded", kp._product_status(db, ORDER, SECTION, RECIPE) == kp.PRODUCED)

    kp.transfer_product(db, ORDER, SECTION, RECIPE, "", user="tester")
    check("transfer succeeds once produced",
          kp._product_status(db, ORDER, SECTION, RECIPE) == kp.TRANSFERRED)

    blocked = False
    try:
        kp.transfer_product(db, ORDER, SECTION, RECIPE, "", user="tester")
    except kp.SectionLocked:
        blocked = True
    check("cannot transfer the same product twice", blocked,
          "re-homes ingredient rows again and overwrites transferred_at")

    blocked = False
    try:
        kp.produce_final_product(db, ORDER, SECTION, RECIPE, 99, 0, "tester")
    except kp.SectionLocked:
        blocked = True
    check("cannot re-produce after transfer (the silent pull-back bug)", blocked,
          "product would vanish from the next section's queue")

    check("status still TRANSFERRED after blocked attempts",
          kp._product_status(db, ORDER, SECTION, RECIPE) == kp.TRANSFERRED)

    print("\n=== 2. Delivered dispatch is locked ===")
    db.execute(text("DELETE FROM packing_dispatch WHERE order_no='ORD-DLOCK'"))
    db.execute(text("""
        INSERT INTO packing_dispatch
            (company_id, order_no, dispatch_no, dispatch_status, packed_portions, driver_name)
        VALUES (1, 'ORD-DLOCK', 'DSP-LOCKTEST', 'Delivered', 10, 'ali')
    """))
    db.commit()
    did = db.execute(text("SELECT id FROM packing_dispatch WHERE order_no='ORD-DLOCK'")).scalar()

    c = admin_client()
    r = c.post(f"/dispatch/{did}/update", data={
        "packed_portions": "999", "rejected_portions": "0",
        "driver_name": "TAMPERED", "vehicle_no": "XXX",
        "dispatch_status": "Out for Delivery", "remarks": "should not save",
    }, allow_redirects=False)

    row = db.execute(text("""
        SELECT packed_portions, driver_name, dispatch_status
        FROM packing_dispatch WHERE id = :i
    """), {"i": did}).mappings().first()

    check("edit attempt is rejected, not applied", r.status_code in (302, 303),
          f"HTTP {r.status_code}")
    check("quantity unchanged", float(row["packed_portions"] or 0) == 10.0,
          f"became {row['packed_portions']}")
    check("driver unchanged", row["driver_name"] == "ali",
          f"became {row['driver_name']}")
    check("status still Delivered", row["dispatch_status"] == "Delivered",
          f"became {row['dispatch_status']}")
    check("user is told why", "lock" in (r.headers.get("location") or "").lower(),
          "no explanation given")

    # Clean up so repeat runs start from the same place.
    db.execute(text("DELETE FROM kitchen_production WHERE order_no=:o"), {"o": ORDER})
    db.execute(text("DELETE FROM kitchen_section_transactions WHERE order_no=:o"), {"o": ORDER})
    db.execute(text("DELETE FROM packing_dispatch WHERE order_no='ORD-DLOCK'"))
    db.commit()
    db.close()

    print("\n" + "=" * 62)
    if FAILS:
        print(f"FAILED: {len(FAILS)}")
        for f in FAILS:
            print("   -", f)
    else:
        print("ALL WORKFLOW LOCKS HOLD.")
    print("=" * 62 + "\n")
    return 1 if FAILS else 0


if __name__ == "__main__":
    _sys.exit(main())
