"""ISFC PIMS — approval hierarchy test (Batch 111).

This is a financial control, so the rules are pinned here rather than trusted.
Every assertion maps to a rule that exists to stop a specific abuse:

  * tiering        — a large requisition must not clear on one signature
  * sequencing     — step 2 cannot be signed before step 1
  * separation     — one person cannot sign two steps
  * self-approval  — the raiser cannot walk their own request through
  * seniority      — a junior role cannot cover a senior step
  * value lock     — editing the value after the first signature must not
                     change how many approvals are still required
  * boundaries     — a value on a tier edge must land in exactly one tier

USAGE
    python scripts/test_approvals.py
    python scripts/test_approvals.py --verbose
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

from sqlalchemy import text

from app.core import approval_chain as ac
from app.database.session import SessionLocal

VERBOSE = "--verbose" in _sys.argv
FAILS: list[str] = []


def check(label, cond, detail=""):
    if cond:
        if VERBOSE:
            print(f"  [PASS] {label}")
    else:
        print(f"  [FAIL] {label}" + (f"  -- {detail}" if detail else ""))
        FAILS.append(label)


def main() -> int:
    db = SessionLocal()
    _orig = db.execute
    db.execute = lambda *a, **k: (db.commit(), _orig(*a, **k))[1]
    ac.ensure_schema(db)

    def reset(doc):
        db.execute(text("DELETE FROM approval_steps WHERE doc_no = :d"), {"d": doc})
        db.commit()

    print("\n=== 1. Tiering by value ===")
    check("small value needs 1 approval", len(ac.tier_for(db, 3000, 1)) == 1,
          str(ac.tier_for(db, 3000, 1)))
    check("mid value needs 2 approvals", len(ac.tier_for(db, 20000, 1)) == 2,
          str(ac.tier_for(db, 20000, 1)))
    check("large value needs 3 approvals", len(ac.tier_for(db, 250000, 1)) == 3,
          str(ac.tier_for(db, 250000, 1)))

    print("\n=== 2. Tier boundaries are unambiguous ===")
    # Upper bounds are exclusive, so a value sitting exactly on an edge must
    # belong to exactly one tier. With <= it matched two and the winner
    # depended on ORDER BY.
    check("4999.99 -> 1 step", len(ac.tier_for(db, 4999.99, 1)) == 1)
    check("5000.00 -> 2 steps (upper bound exclusive)",
          len(ac.tier_for(db, 5000, 1)) == 2, str(ac.tier_for(db, 5000, 1)))
    check("49999.99 -> 2 steps", len(ac.tier_for(db, 49999.99, 1)) == 2)
    check("50000.00 -> 3 steps", len(ac.tier_for(db, 50000, 1)) == 3)

    print("\n=== 3. Sequencing and separation of duties ===")
    reset("T-BIG")
    chain = ac.build_chain(db, "purchase_requisition", "T-BIG", 250000, 1)
    check("chain built with 3 steps", len(chain) == 3)

    ok, why = ac.can_approve(chain, 91, "junior", "STAFF", raised_by="clerk")
    check("junior role blocked at step 1", not ok, why)

    ok, _msg, chain = ac.approve_step(db, "purchase_requisition", "T-BIG",
                                      92, "sup1", "SUPERVISOR")
    check("supervisor signs step 1", chain[0]["status"] == "Approved")
    check("not complete after 1 of 3", not ac.is_complete(chain))

    ok, why = ac.can_approve(chain, 92, "sup1", "SUPERVISOR", raised_by="clerk")
    check("same person cannot sign step 2", not ok, why)

    ok, why = ac.can_approve(chain, 93, "mgr1", "MANAGER", raised_by="clerk")
    check("a different manager may sign step 2", ok, why)

    ok, _msg, chain = ac.approve_step(db, "purchase_requisition", "T-BIG",
                                      93, "mgr1", "MANAGER")
    ok, why = ac.can_approve(chain, 94, "mgr2", "MANAGER", raised_by="clerk")
    check("manager cannot cover the ADMIN step", not ok, why)

    ok, _msg, chain = ac.approve_step(db, "purchase_requisition", "T-BIG",
                                      95, "admin1", "ADMIN")
    check("complete after all 3 signatures", ac.is_complete(chain))

    print("\n=== 4. The raiser cannot walk it through alone ===")
    reset("T-SELF")
    chain = ac.build_chain(db, "purchase_requisition", "T-SELF", 250000, 1)
    ok, _msg, chain = ac.approve_step(db, "purchase_requisition", "T-SELF",
                                      96, "clerk", "ADMIN")
    check("raiser may satisfy step 1", chain[0]["status"] == "Approved")
    ok, why = ac.can_approve(chain, 96, "clerk", "ADMIN", raised_by="clerk")
    check("raiser blocked beyond step 1", not ok, why)

    print("\n=== 5. Value is locked at the first signature ===")
    reset("T-LOCK")
    ac.build_chain(db, "purchase_requisition", "T-LOCK", 250000, 1)
    ac.approve_step(db, "purchase_requisition", "T-LOCK", 97, "sup2", "SUPERVISOR")
    # Rebuilding with a tiny value must NOT shrink an in-flight chain.
    again = ac.build_chain(db, "purchase_requisition", "T-LOCK", 100, 1)
    check("chain still has 3 steps after a value change", len(again) == 3,
          f"{len(again)} steps")
    check("recorded value unchanged", float(again[0]["doc_value"]) == 250000.0,
          str(again[0]["doc_value"]))

    print("\n=== 6. Rejection clears partial signatures ===")
    ac.reset_chain(db, "purchase_requisition", "T-LOCK")
    check("chain cleared so a resubmission starts at step 1",
          ac.get_chain(db, "purchase_requisition", "T-LOCK") == [])

    for doc in ("T-BIG", "T-SELF", "T-LOCK"):
        reset(doc)
    db.close()

    print("\n" + "=" * 60)
    if FAILS:
        print(f"FAILED: {len(FAILS)}")
        for f in FAILS:
            print("   -", f)
    else:
        print("ALL APPROVAL RULES HOLD.")
    print("=" * 60 + "\n")
    return 1 if FAILS else 0


if __name__ == "__main__":
    _sys.exit(main())
