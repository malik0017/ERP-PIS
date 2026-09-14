"""Batch 221 — read helpers must not swallow query failures.

    python scripts/test_read_helpers.py

A helper that returns [] on a rejected query makes a broken panel look like an
empty table. That has produced three separate bugs in this project (Batch 203
reserved word DELAYED, Batch 212 a column that does not exist, Batch 216
reserved word LINES) and every one was found by accident.

Two checks:

  1. STATIC — scan every module for a named read helper (_rows, _one, _n,
     _scalar, _count, _safe_rows …) whose except-block neither logs nor
     re-raises. A new helper added without logging fails the build.

  2. LIVE — run a deliberately broken query through the shared helpers in
     app/core/db_read and assert each one both degrades gracefully AND emits a
     warning. A helper that stops logging fails here even if it still looks
     right in the source.

Exit code 0 = pass.
"""
from __future__ import annotations

import logging
import os
import re
import sys
from glob import glob

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

HELPERS = ("_rows", "_one", "_n", "_scalar", "_count", "_safe_rows", "_fetch", "_q")
LOG_MARKERS = ("logger", "logging", "db_read_log", "log_failure", "raise")


def static_scan() -> list[str]:
    bad = []
    for path in sorted(glob("app/**/*.py", recursive=True)):
        if path.endswith("core/db_read.py"):
            continue
        src = open(path, encoding="utf-8", errors="ignore").read()
        for name in HELPERS:
            for m in re.finditer(r"\ndef " + name + r"\([^\n]*\n(?:(?:[ \t]+[^\n]*)?\n){0,20}", src):
                block = m.group(0)
                if "except" not in block:
                    continue
                if any(k in block for k in LOG_MARKERS):
                    continue
                line = src[: m.start()].count("\n") + 2
                bad.append(f"{path}:{line}  {name}() swallows its exception")
    return bad


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.records = []

    def emit(self, record):
        self.records.append(record.getMessage())


def live_probe() -> list[str]:
    from app.core import db_read
    from app.database.session import SessionLocal

    cap = _Capture()
    log = logging.getLogger("isfc.db_read")
    log.addHandler(cap)
    log.setLevel(logging.WARNING)

    db = SessionLocal()
    problems = []
    try:
        BROKEN = "SELECT no_such_column FROM customer_orders"
        cases = [
            ("rows", lambda: db_read.rows(db, BROKEN, label="selftest"), []),
            ("one", lambda: db_read.one(db, BROKEN, label="selftest"), {}),
            ("number", lambda: db_read.number(db, BROKEN, label="selftest"), 0.0),
            ("scalar", lambda: db_read.scalar(db, BROKEN, label="selftest"), None),
        ]
        for name, call, expected in cases:
            before = len(cap.records)
            try:
                got = call()
            except Exception as exc:
                problems.append(f"{name}() raised instead of degrading: {exc.__class__.__name__}")
                continue
            if got != expected:
                problems.append(f"{name}() returned {got!r}, expected {expected!r}")
            if len(cap.records) == before:
                problems.append(f"{name}() failed SILENTLY — nothing logged")

        rows, err = db_read.rows_or_error(db, BROKEN, label="selftest")
        if rows != [] or not err:
            problems.append("rows_or_error() did not report the error")

        # And a query that works must return data and log nothing.
        before = len(cap.records)
        ok = db_read.rows(db, "SELECT 1 AS n", label="selftest")
        if not ok:
            problems.append("rows() returned nothing for a valid query")
        if len(cap.records) > before:
            problems.append("rows() logged a warning for a valid query")
    finally:
        log.removeHandler(cap)
        db.close()
    return problems


def main() -> int:
    print("=" * 70)
    print("ISFC PIMS — read-helper honesty test")
    print("=" * 70)

    print("\n1. Static scan — helpers that swallow their exception")
    bad = static_scan()
    if bad:
        print(f"   FAIL — {len(bad)} helper(s):")
        for b in bad:
            print("     ", b)
    else:
        print("   PASS — every read helper logs or re-raises")

    print("\n2. Live probe — a broken query must degrade AND be logged")
    problems = live_probe()
    if problems:
        print(f"   FAIL — {len(problems)} problem(s):")
        for p in problems:
            print("     ", p)
    else:
        print("   PASS — all helpers degraded gracefully and logged")

    failed = bool(bad or problems)
    print("\n" + ("RESULT: FAIL" if failed else "RESULT: PASS"))
    return 1 if failed else 0


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)          # keep app import noise out
    logging.disable(logging.NOTSET)
    raise SystemExit(main())
