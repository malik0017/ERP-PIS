"""ISFC PIMS — KPI card filter-reactivity audit (Batch 97).

THE GAP THIS CLOSES
-------------------
route_audit.py proves a page RENDERS. It says nothing about whether the
numbers on that page are correct. The specific failure it cannot see:

    A KPI card that ignores the active filter.

That bug is invisible in every way that matters. The page returns 200. The
card shows a plausible number. The table below it shows filtered rows. Nobody
notices until someone reconciles the card against the table and finds they
disagree — which is exactly what happened on Head Chef Planning, where the
"awaiting" card excluded pending sales review and the table beneath it did
not, so the card said 3 while the table listed 11.

HOW IT WORKS
------------
For each screen, request it twice:

    1. unfiltered   ->  /purchase-requisitions
    2. filtered     ->  /purchase-requisitions?status=Rejected

...using a filter deliberately chosen to return few or no rows. Then compare
the KPI card values scraped from each response.

  * Card values CHANGE between the two -> the card is filter-reactive. PASS.
  * Card values IDENTICAL but the row count changed -> the filter demonstrably
    did something to the table while the cards sat still. FLAGGED.
  * Row count also identical -> the filter had no effect at all; inconclusive,
    reported separately rather than counted as a pass or a failure.

WHAT THIS DOES NOT DO
---------------------
It cannot tell you a card is arithmetically WRONG — only whether it responds
to filtering. A card that is filter-reactive and still computes the wrong
figure passes here. Treat FLAGGED as "go look at this", not as proof of a
bug: some cards are company-wide totals BY DESIGN (e.g. "Total Users"), and
those correctly ignore a date filter. The tool reports; you judge.

USAGE
    python scripts/audit_kpi_filters.py
    python scripts/audit_kpi_filters.py --verbose
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# Batch 96 bootstrap — run from any directory.
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
import re

import itsdangerous
from starlette.testclient import TestClient

from app.main import app
from app.config import SECRET_KEY

VERBOSE = "--verbose" in _sys.argv

# (label, base path, filter query) — the filter must be one that genuinely
# narrows the result set on a normal database.
SCREENS = [
    ("Purchase Requisitions", "/purchase-requisitions", "status=Rejected"),
    ("Sales Requests",        "/sales-requests",        "status=Rejected"),
    ("Head Chef Planning",    "/production/head-chef",  "date_from=2099-01-01&date_to=2099-12-31"),
    ("Store Top-Ups",         "/production/topups",     "status=Rejected"),
    ("Procurement",           "/procurement",           "status=Cancelled"),
    ("Incoming QC",           "/qc/inspection",         "status=Failed"),
    ("Customer Complaints",   "/qc/complaints",         "status=Resolved"),
    ("Kitchen Summary",       "/production/kitchen-summary", "date_from=2099-01-01&date_to=2099-12-31"),
    ("Store Issuance",        "/production/store-issuance",  "date_from=2099-01-01&date_to=2099-12-31"),
    ("Inventory",             "/inventory",             "search=ZZZNOMATCHZZZ"),
    ("Subscriptions",         "/subscriptions",         "status=Cancelled"),
    ("Dispatch",              "/packing",               "status=Cancelled"),
    ("Finance",               "/finance",               "date_from=2099-01-01&date_to=2099-12-31"),
    ("Reports: Yield",        "/reports/yield-wastage", "date_from=2099-01-01&date_to=2099-12-31"),
]

# KPI cards in this codebase render as a big number inside an element whose
# class marks it as a stat. Matching on the visual shape rather than one
# template pattern, because the screens use several card markups.
NUM_PATTERNS = [
    re.compile(r'class="[^"]*(?:fs-1|fs-2|fs-3|fs-4)[^"]*fw-bold[^"]*"[^>]*>\s*([\d,]+(?:\.\d+)?)\s*<', re.I),
    re.compile(r'class="[^"]*fw-bold[^"]*(?:fs-1|fs-2|fs-3|fs-4)[^"]*"[^>]*>\s*([\d,]+(?:\.\d+)?)\s*<', re.I),
    re.compile(r'<div class="metric"[^>]*>.*?<strong>\s*([\d,]+(?:\.\d+)?)\s*</strong>', re.I | re.S),
    re.compile(r'class="[^"]*kpi[^"]*"[^>]*>\s*([\d,]+(?:\.\d+)?)\s*<', re.I),
    # Batch 97: the screens use several different card markups. The two below
    # cover "<div class=kpi><span>Label</span><strong>N</strong></div>" (packing,
    # dispatch, inventory) and the hc-kpi variant using <b> (head chef,
    # kitchen summary). Without these, eight screens reported "no KPI cards
    # detected" and were silently skipped — a checker that quietly checks
    # nothing is worse than no checker.
    re.compile(r'class="[^"]*(?:kpi|metric|stat)[^"]*"[^>]*>.*?<(?:strong|b)>\s*([\d,]+(?:\.\d+)?)\s*</(?:strong|b)>', re.I | re.S),
    re.compile(r'<(?:strong|b)>\s*([\d,]+(?:\.\d+)?)\s*</(?:strong|b)>\s*</(?:div|a)>', re.I),
]

ROW_RE = re.compile(r"<tr[\s>]", re.I)


def cards(html: str) -> list[str]:
    out: list[str] = []
    for pat in NUM_PATTERNS:
        out.extend(pat.findall(html or ""))
    return out


def rows(html: str) -> int:
    return len(ROW_RE.findall(html or ""))


def client() -> TestClient:
    c = TestClient(app)
    d = {"user_id": 1, "username": "kpi_auditor", "user_role": "ADMIN",
         "role": "ADMIN", "company_id": 1}
    s = itsdangerous.TimestampSigner(str(SECRET_KEY))
    c.cookies.set("isfc_session",
                  s.sign(base64.b64encode(json.dumps(d).encode())).decode())
    return c


def main() -> int:
    c = client()
    reactive, flagged, inconclusive, unreachable = [], [], [], []

    for label, path, qs in SCREENS:
        try:
            a = c.get(path, allow_redirects=False)
            b = c.get(f"{path}{'&' if '?' in path else '?'}{qs}", allow_redirects=False)
        except Exception as exc:
            unreachable.append((label, path, type(exc).__name__))
            continue

        if a.status_code != 200 or b.status_code != 200:
            unreachable.append((label, path, f"HTTP {a.status_code}/{b.status_code}"))
            continue

        ca, cb = cards(a.text), cards(b.text)
        ra, rb = rows(a.text), rows(b.text)

        if not ca:
            unreachable.append((label, path, "no KPI cards detected"))
            continue

        if ca != cb:
            reactive.append((label, len(ca)))
            if VERBOSE:
                print(f"  [REACTIVE] {label}: {ca} -> {cb}")
        elif ra != rb:
            flagged.append((label, path, qs, ca, ra, rb))
            if VERBOSE:
                print(f"  [FLAGGED]  {label}: cards {ca} unchanged, rows {ra} -> {rb}")
        else:
            inconclusive.append((label, path, qs))
            if VERBOSE:
                print(f"  [INCONCL]  {label}: filter changed nothing")

    print("\n" + "=" * 70)
    print("KPI CARD FILTER-REACTIVITY AUDIT")
    print("=" * 70)
    print(f"  Screens checked ................ {len(SCREENS)}")
    print(f"  Cards react to the filter ...... {len(reactive)}")
    print(f"  FLAGGED (cards static, rows moved) {len(flagged)}")
    print(f"  Inconclusive (filter no-op) .... {len(inconclusive)}")
    print(f"  Not checkable .................. {len(unreachable)}")

    if flagged:
        print("\n--- FLAGGED: cards did not move while the table did ---")
        print("    Review each. A company-wide total SHOULD ignore a filter;")
        print("    a 'matching this view' count should not.")
        for label, path, qs, ca, ra, rb in flagged:
            print(f"\n  {label}   {path}?{qs}")
            print(f"      card values (both requests): {ca}")
            print(f"      table rows: {ra} -> {rb}")

    if inconclusive:
        print("\n--- INCONCLUSIVE (filter returned the same set; seed more data) ---")
        for label, path, qs in inconclusive:
            print(f"  {label}  ({path}?{qs})")

    if unreachable:
        print("\n--- NOT CHECKABLE ---")
        for label, path, why in unreachable:
            print(f"  {label}  {path}  — {why}")

    print()
    return 1 if flagged else 0


if __name__ == "__main__":
    _sys.exit(main())
