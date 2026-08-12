"""ISFC PIMS — full route-by-route working check (Batch 95).

Requests EVERY registered GET route as an admin and records what happens.
Path parameters are filled from real rows in the database where one exists,
so /production/orders/{order_no} is exercised against an actual order rather
than a placeholder that would 404 either way.

What counts as a failure:
  500  -> the page is broken
  Jinja/SQL error text in a 200 body -> the page "works" but renders an error

What does NOT count as a failure:
  303/302 -> a redirect is normal (guards, "not found" toasts, list->detail)
  404     -> expected when no sample row exists for that entity
  403     -> RBAC working as designed

Usage:  python scripts/route_audit.py            (summary)
        python scripts/route_audit.py --verbose  (every route)
        python scripts/route_audit.py --csv out.csv
"""
from __future__ import annotations

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


import base64
import json
import re
import sys
import traceback

import itsdangerous
from sqlalchemy import text
from starlette.testclient import TestClient

from app.main import app
from app.config import SECRET_KEY
from app.database.session import SessionLocal

# Error fingerprints that can appear inside a 200 response body when a
# template swallows the exception and renders a partial page.
BODY_ERROR_PATTERNS = [
    r"jinja2\.exceptions",
    r"UndefinedError",
    r"TemplateNotFound",
    r"sqlalchemy\.exc",
    r"ProgrammingError",
    r"OperationalError",
    r"Unknown column",
    r"doesn&#39;t exist",
    r"doesn't exist",
    r"Traceback \(most recent call last\)",
    r"Internal Server Error",
]
BODY_ERROR_RE = re.compile("|".join(BODY_ERROR_PATTERNS), re.I)

# Routes that intentionally mutate, export, or would hang a smoke test.
SKIP_EXACT = {
    "/logout", "/auth/logout", "/openapi.json", "/docs", "/redoc",
    "/docs/oauth2-redirect",
}
SKIP_SUBSTR = [
    "/export", "/download", "/pdf", "/print", "/certificate", "/label",
    "/delete", "/backup", "/restore", "/seed", "/reset",
]


def sample_values(db):
    """Real IDs/codes pulled from the DB to fill path parameters."""
    def one(sql):
        try:
            v = db.execute(text(sql)).scalar()
            return str(v) if v is not None else None
        except Exception:
            return None

    v = {
        "order_no": one("SELECT order_no FROM customer_orders ORDER BY id DESC LIMIT 1"),
        "recipe_code": one("SELECT recipe_code FROM recipes LIMIT 1"),
        "pr_no": one("SELECT pr_no FROM purchase_requisitions ORDER BY id DESC LIMIT 1"),
        "po_no": one("SELECT po_no FROM purchase_orders ORDER BY id DESC LIMIT 1"),
        "grn_no": one("SELECT grn_no FROM grn_receipts ORDER BY id DESC LIMIT 1"),
        "topup_no": one("SELECT topup_no FROM store_topup_requests ORDER BY id DESC LIMIT 1"),
        "ingredient_code": one("SELECT ingredient_code FROM ingredients LIMIT 1"),
        "supplier_code": one("SELECT supplier_code FROM suppliers LIMIT 1"),
        "customer_code": one("SELECT customer_code FROM customers LIMIT 1"),
        "user_id": one("SELECT id FROM users LIMIT 1"),
        "company_id": "1",
        "id": "1",
        "section": "Hot Kitchen",
    }
    # Sensible fallbacks so a route is still exercised on an empty database.
    defaults = {
        "order_no": "ORD-00000000-0001", "recipe_code": "RCP-S-00001",
        "pr_no": "PR-000001", "po_no": "PO-000001", "grn_no": "GRN-000001",
        "topup_no": "TOP-000001", "ingredient_code": "ING-001",
        "supplier_code": "SUP-001", "customer_code": "CUS-001", "user_id": "1",
    }
    for k, d in defaults.items():
        if not v.get(k):
            v[k] = d
    return v


def fill(path: str, vals: dict) -> tuple[str, bool]:
    """Substitute {param} placeholders. Returns (url, had_unknown_param)."""
    unknown = False
    out = path
    for m in re.findall(r"\{([^}:]+)(?::[^}]+)?\}", path):
        key = m.strip()
        if key in vals:
            rep = vals[key]
        elif key.endswith("_id"):
            rep = "1"
            unknown = True
        elif key.endswith("_no") or key.endswith("_code"):
            rep = vals.get("order_no", "X")
            unknown = True
        else:
            rep = "1"
            unknown = True
        out = re.sub(r"\{" + re.escape(key) + r"(?::[^}]+)?\}", rep, out)
    return out, unknown


def collect_routes(application):
    """Walk the router tree. FastAPI wraps included routers, so plain
    iteration over app.routes only sees the top level."""
    found = {}

    def walk(routes, depth=0):
        if depth > 6:
            return
        for r in routes:
            p = getattr(r, "path", None)
            methods = getattr(r, "methods", None) or set()
            if p and methods:
                found.setdefault(p, set()).update(methods)
            orig = getattr(r, "original_router", None)
            if orig is not None:
                walk(getattr(orig, "routes", []), depth + 1)
            sub = getattr(r, "routes", None)
            if sub:
                walk(sub, depth + 1)

    walk(application.routes)
    return found


def admin_client():
    c = TestClient(app)
    data = {"user_id": 1, "username": "auditor", "user_role": "ADMIN",
            "role": "ADMIN", "company_id": 1}
    signer = itsdangerous.TimestampSigner(str(SECRET_KEY))
    c.cookies.set("isfc_session",
                  signer.sign(base64.b64encode(json.dumps(data).encode())).decode())
    return c


def main():
    verbose = "--verbose" in sys.argv
    csv_path = None
    if "--csv" in sys.argv:
        i = sys.argv.index("--csv")
        if i + 1 < len(sys.argv):
            csv_path = sys.argv[i + 1]

    db = SessionLocal()
    vals = sample_values(db)
    client = admin_client()

    routes = collect_routes(app)
    gets = sorted(p for p, m in routes.items() if "GET" in m)
    posts = sorted(p for p, m in routes.items() if "POST" in m)

    results = []
    broken, body_err, ok, redirect, notfound, forbidden, skipped = [], [], [], [], [], [], []

    for path in gets:
        if path in SKIP_EXACT or any(s in path.lower() for s in SKIP_SUBSTR):
            skipped.append(path)
            results.append((path, "SKIP", "", ""))
            continue

        url, guessed = fill(path, vals)
        try:
            r = client.get(url, allow_redirects=False)
            code = r.status_code
            note = ""
            if code >= 500:
                # Pull the exception class out of the body when present.
                m = re.search(r"(\w+Error|\w+Exception)", r.text or "")
                note = m.group(1) if m else "500"
                broken.append((path, url, note))
                status = "BROKEN"
            elif code == 200:
                m = BODY_ERROR_RE.search(r.text or "")
                if m:
                    note = m.group(0)[:60]
                    body_err.append((path, url, note))
                    status = "BODY-ERR"
                else:
                    ok.append(path)
                    status = "OK"
            elif code in (301, 302, 303, 307, 308):
                redirect.append(path)
                status = "REDIRECT"
                note = (r.headers.get("location") or "")[:70]
            elif code == 404:
                notfound.append(path)
                status = "404"
                note = "no sample row" if guessed else ""
            elif code == 403:
                forbidden.append(path)
                status = "403"
            else:
                status = str(code)
                ok.append(path)
            results.append((path, status, str(code), note))
            if verbose:
                print(f"  {status:9} {code:>3}  {url}  {note}")
        except Exception as exc:
            tb = traceback.format_exc()
            m = re.search(r"(\w+Error|\w+Exception)", tb)
            note = m.group(1) if m else type(exc).__name__
            broken.append((path, url, note))
            results.append((path, "BROKEN", "exc", note))
            if verbose:
                print(f"  BROKEN   exc  {url}  {note}")

    total = len(gets)
    print("\n" + "=" * 72)
    print("ROUTE AUDIT SUMMARY")
    print("=" * 72)
    print(f"  Registered routes .......... {len(routes)}  ({total} GET, {len(posts)} POST)")
    print(f"  Rendered clean (200) ....... {len(ok)}")
    print(f"  Redirects (guards/flow) .... {len(redirect)}")
    print(f"  404 (no sample data) ....... {len(notfound)}")
    print(f"  403 (RBAC) ................. {len(forbidden)}")
    print(f"  Skipped (export/mutating) .. {len(skipped)}")
    print(f"  BROKEN (500/exception) ..... {len(broken)}")
    print(f"  Rendered with error text ... {len(body_err)}")

    if broken:
        print("\n--- BROKEN ---")
        for p, u, n in broken:
            print(f"  {p}\n      tried: {u}\n      {n}")
    if body_err:
        print("\n--- RENDERS BUT CONTAINS ERROR TEXT ---")
        for p, u, n in body_err:
            print(f"  {p}  ({n})")

    if csv_path:
        with open(csv_path, "w", encoding="utf-8") as fh:
            fh.write("route,status,http_code,note\n")
            for row in results:
                fh.write(",".join('"' + str(c).replace('"', "'") + '"' for c in row) + "\n")
        print(f"\nCSV written: {csv_path}")

    db.close()
    print()
    return 1 if (broken or body_err) else 0


if __name__ == "__main__":
    sys.exit(main())
