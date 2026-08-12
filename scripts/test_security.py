"""ISFC PIMS — search & notification security test (Batch 98).

Two leaks were found by hand this batch and fixed. This locks them shut.

  1. NOTIFICATIONS: the delivery query filtered on user_id/role but NOT on
     company_id, so a ROLE-broadcast notification reached every holder of
     that role in EVERY company. A Company 2 procurement user received
     "Requisition PR-000001 approved" raised inside Company 1 — leaking
     document numbers, order references and approver names across the
     tenancy boundary, on the screen users check most often.

  2. GLOBAL SEARCH: company-scoped since Batch 81, but with no RBAC filter
     at all. A user with no procurement or finance access could type a
     supplier name and get back purchase orders, GRNs, AP invoices and
     payments with amounts. The links 403'd when clicked, but the results
     themselves had already disclosed the data — enumerating a company's
     supplier list through a search box is a disclosure whether or not the
     detail page opens.

USAGE
    python scripts/test_security.py
    python scripts/test_security.py --verbose
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

VERBOSE = "--verbose" in _sys.argv
LEAKS: list[str] = []
NOTES: list[str] = []


def check(label, ok, detail=""):
    if ok:
        if VERBOSE:
            print(f"  [PASS] {label}")
    else:
        print(f"  [LEAK] {label}" + (f"  -- {detail}" if detail else ""))
        LEAKS.append(label)


def client(company_id: int, role: str = "PROCUREMENT", user_id: int | None = None):
    c = TestClient(app)
    d = {"user_id": user_id or (900 + company_id), "username": f"co{company_id}",
         "user_role": role, "role": role, "company_id": company_id}
    s = itsdangerous.TimestampSigner(str(SECRET_KEY))
    c.cookies.set("isfc_session",
                  s.sign(base64.b64encode(json.dumps(d).encode())).decode())
    _g = c.get
    c.get = lambda *a, **k: _g(*a, allow_redirects=False, **k)
    return c


def main() -> int:
    db = SessionLocal()
    _orig = db.execute
    db.execute = lambda *a, **k: (db.commit(), _orig(*a, **k))[1]

    print("\n=== 1. Notifications do not cross companies ===")
    from app.core.notifications import ensure_notifications_schema
    ensure_notifications_schema(db)
    db.execute(text("DELETE FROM notifications WHERE title LIKE 'SECTEST%'"))
    db.execute(text("""
        INSERT INTO notifications (company_id, role, title, message, category)
        VALUES (1, 'PROCUREMENT', 'SECTEST co1 requisition approved',
                'Company One confidential', 'pr')
    """))
    db.commit()

    r = client(2).get("/notifications")
    check("company 2 cannot see company 1's role notification",
          "SECTEST" not in (r.text or ""), "visible on /notifications")

    r = client(2).get("/notifications/summary")
    check("company 2 summary excludes it",
          "SECTEST" not in (r.text or ""), "visible in the header dropdown")

    r = client(1).get("/notifications")
    check("company 1 still receives its OWN notification",
          "SECTEST" in (r.text or ""),
          "over-filtered — the fix broke legitimate delivery")

    print("\n=== 2. Global search respects module access ===")
    from app.modules.search import routes as S
    real = S.can_access
    try:
        S.can_access = lambda req, area: area in ("kitchen", "recipes")
        sample = [
            {"type": "PO", "title": "SECTEST-PO"},
            {"type": "Supplier", "title": "SECTEST-SUP"},
            {"type": "AP", "title": "SECTEST-AP"},
            {"type": "Payment", "title": "SECTEST-PAY"},
            {"type": "Employee", "title": "SECTEST-EMP"},
            {"type": "Recipe", "title": "SECTEST-RCP"},
            {"type": "Page", "title": "Dashboard"},
        ]
        kept = {x["type"] for x in S._filter_by_access(object(), sample)}
        for t, label in [("PO", "purchase orders"), ("Supplier", "suppliers"),
                         ("AP", "AP invoices"), ("Payment", "payments"),
                         ("Employee", "employee records")]:
            check(f"kitchen-only user cannot see {label} in search",
                  t not in kept, f"{t} returned")
        check("allowed types still returned (recipes)", "Recipe" in kept,
              "over-filtered")
        check("unmapped result types still returned (pages)", "Page" in kept,
              "over-filtered")

        # Fail-closed: an error inside the access check must hide sensitive
        # rows, not expose them.
        def boom(req, area):
            raise RuntimeError("access check unavailable")
        S.can_access = boom
        kept2 = {x["type"] for x in S._filter_by_access(object(), sample)}
        check("access-check failure hides sensitive results (fails closed)",
              kept2 == {"Page"}, f"kept {kept2}")
    finally:
        S.can_access = real

    print("\n=== 3. RBAC default posture (informational) ===")
    # Not a pass/fail: a design decision the business has to make knowingly.
    from app.core import rbac
    src = ""
    try:
        src = open(_os.path.join("app", "core", "rbac.py"), encoding="utf-8").read()
    except Exception:
        pass
    if 'if action == "view":\n        return True' in src:
        NOTES.append(
            "can_access() falls back to GRANTING VIEW on every area for any user "
            "with no rows in user_page_access. A newly created user therefore sees "
            "every module read-only until the access matrix is filled in. That is "
            "fail-OPEN. It is deliberate (the system would be unusable before "
            "configuration otherwise) but it must be a conscious choice before "
            "go-live, and every user needs an explicit matrix.")

    db.close()

    print("\n" + "=" * 66)
    if LEAKS:
        print(f"LEAKS: {len(LEAKS)}")
        for x in LEAKS:
            print("   -", x)
    else:
        print("NO LEAKS.")
    if NOTES:
        print("\nREVIEW BEFORE GO-LIVE:")
        for n in NOTES:
            print("   ! " + n)
    print("=" * 66 + "\n")
    return 1 if LEAKS else 0


if __name__ == "__main__":
    _sys.exit(main())
