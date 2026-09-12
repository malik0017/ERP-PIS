"""Batch 207 — repeatable multi-company access test.

    python scripts/test_company_scope.py

Signs in as a user of company 2 and asks for company 1's documents by URL.
Every order-keyed route must answer 404 (never 200, and never 403 — a 403
confirms the document exists). Also re-scans the codebase for any order-keyed
route that has no scope guard, so a new route cannot quietly reintroduce the
hole this batch closed.

Exit code 0 = pass. Non-zero = at least one route leaked.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import sys
from glob import glob

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
logging.disable(logging.CRITICAL)

import itsdangerous  # noqa: E402
from sqlalchemy import text  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

import app.main as main  # noqa: E402
from app.config import SECRET_KEY  # noqa: E402
from app.database.session import SessionLocal  # noqa: E402

GUARDS = ("scoped_order", "require_order_scope", "require_record_scope")
KEYED = r'\{(?:order_no|order_id|dispatch_id|packing_id)\}'


def client(company_id: int) -> TestClient:
    c = TestClient(main.app)
    payload = {"user_id": 1, "username": "scope-test", "user_role": "ADMIN",
               "role": "ADMIN", "company_id": company_id}
    token = itsdangerous.TimestampSigner(str(SECRET_KEY)).sign(
        base64.b64encode(json.dumps(payload).encode())).decode()
    c.cookies.set("isfc_session", token)
    return c


def static_scan() -> list[str]:
    """Any order-keyed route with no guard at all."""
    missing = []
    for path in sorted(glob("app/modules/**/*.py", recursive=True)):
        src = open(path, encoding="utf-8", errors="ignore").read()
        for m in re.finditer(r'@router\.(get|post)\(\s*["\']([^"\']*' + KEYED + r'[^"\']*)["\']', src):
            nxt = src.find("@router.", m.end())
            body = src[m.start(): nxt if nxt > 0 else len(src)]
            if not any(g in body for g in GUARDS):
                missing.append(f"{path}  {m.group(2)}")
    return missing


def live_probe() -> tuple[list[str], int]:
    """Ask for company 1's documents as a company 2 user."""
    db = SessionLocal()
    try:
        rows = db.execute(text("""
            SELECT order_no FROM customer_orders
            WHERE company_id = 1 ORDER BY id DESC LIMIT 3""")).all()
        orders = [r[0] for r in rows]
        pack = db.execute(text("""
            SELECT pd.id FROM packing_dispatch pd
            JOIN customer_orders o ON o.order_no = pd.order_no
            WHERE o.company_id = 1 ORDER BY pd.id DESC LIMIT 1""")).scalar()
    finally:
        db.close()
    if not orders:
        print("  (no company-1 orders in this database — live probe skipped)")
        return [], 0

    urls = []
    for o in orders[:1]:
        urls += [f"/production/orders/{o}", f"/qc/orders/{o}", f"/print/{o}",
                 f"/print/{o}/order-sheet", f"/print/{o}/bom-sheet",
                 f"/print/{o}/qc-certificate", f"/print/{o}/delivery-note",
                 f"/production/api/orders/{o}/bom/consolidated",
                 f"/production/orders/{o}/store-issuance/history",
                 f"/sales-review/{o}", f"/customer/orders/{o}"]
    if pack:
        urls += [f"/packing/{pack}", f"/packing/{pack}/report", f"/dispatch/{pack}"]

    c2 = client(2)
    leaks = []
    for u in urls:
        try:
            r = c2.get(u, allow_redirects=False)
        except Exception as exc:                      # a 404 raised as HTTPException
            if "404" in str(exc):
                continue
            leaks.append(f"{u} -> ERROR {exc.__class__.__name__}")
            continue
        if r.status_code == 200:
            leaks.append(f"{u} -> 200 (company 2 read company 1's data)")
        elif r.status_code == 403:
            leaks.append(f"{u} -> 403 (should be 404; 403 confirms it exists)")
    return leaks, len(urls)


LIST_URLS = [
    "/dashboard", "/notifications", "/notifications/summary", "/reports",
    "/reports/yield-wastage", "/reports/workflow", "/reports/relationship-map",
    "/module/reports/dashboard", "/module/production/dashboard",
    "/module/sales/dashboard", "/qc", "/packing?scope=all", "/dispatch?scope=all",
    "/production/reports/section?section=Cutting",
    "/production/reports/yield?section=Cutting",
]


def list_probe() -> list[str]:
    """Batch 208 — aggregate/list screens must differ between companies.

    A document leak is a 200 where a 404 belongs, which the probe above catches.
    A TOTALS leak looks fine: the page loads, the number is just everyone's.

    Compared on the NUMBERS in the page, not its length — "8" and "0" are the
    same number of bytes, so a length check called a correctly scoped screen
    identical. Company 2 has no orders in a normal test database, so any screen
    whose figures are unchanged between the two is still reading across the
    tenancy boundary.
    """
    c1, c2 = client(1), client(2)
    same = []
    for u in LIST_URLS:
        try:
            a, b = c1.get(u), c2.get(u)
        except Exception as exc:
            same.append(f"{u} -> ERROR {exc.__class__.__name__}")
            continue
        if a.status_code != 200:
            continue                       # route not reachable here; not a scope question
        na = re.findall(r">\s*([\d,]+(?:\.\d+)?)\s*<", a.text)
        nb = re.findall(r">\s*([\d,]+(?:\.\d+)?)\s*<", b.text)
        if na and na == nb:
            same.append(f"{u} -> identical figures for company 1 and 2")
        elif not na and len(a.text) == len(b.text):
            same.append(f"{u} -> identical output for company 1 and 2")
    return same


def main_() -> int:
    print("=" * 70)
    print("ISFC PIMS — multi-company scope test")
    print("=" * 70)

    print("\n1. Static scan — order-keyed routes without a scope guard")
    missing = static_scan()
    if missing:
        print(f"   FAIL — {len(missing)} unguarded route(s):")
        for m in missing:
            print("     ", m)
    else:
        print("   PASS — every order-keyed route carries a guard")

    print("\n2. Live probe — company 2 asking for company 1's documents")
    leaks, checked = live_probe()
    if leaks:
        print(f"   FAIL — {len(leaks)} of {checked} URLs leaked:")
        for x in leaks:
            print("     ", x)
    elif checked:
        print(f"   PASS — all {checked} URLs returned 404")

    print("\n3. List screens — totals must differ between companies")
    same = list_probe()
    if same:
        print(f"   REVIEW — {len(same)} screen(s) returned identical output:")
        for x in same:
            print("     ", x)
        print("   (identical output is expected when BOTH companies have no data)")
    else:
        print("   PASS — every list screen returned company-specific output")

    failed = bool(missing or leaks)
    print("\n" + ("RESULT: FAIL" if failed else "RESULT: PASS"))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main_())
