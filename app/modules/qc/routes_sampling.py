# app/modules/qc/routes_sampling.py
# =============================================================================
# Batch 94 — QC sampling plan configuration screen.
#
# Kept in its own router file rather than appended to the already-600-line
# qc/routes.py, matching the routes_docs / routes_kitchen / routes_payroll
# pattern used elsewhere in this codebase. Same /qc prefix, so it behaves as
# one module from the outside.
# =============================================================================
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.templates import render
from app.core.rbac import require_area, require_action
from app.database.session import get_db
from app.modules.qc.sampling import get_config, save_config, ensure_schema
# qc_incoming_inspections is created by the QC module's own helper. This screen
# READS that table, so it has to guarantee it exists rather than assuming
# somebody visited /qc/inspection first — caught by the Batch 94 functional
# test, which hit a 500 on a database where Incoming QC had never been opened.
from app.modules.qc.routes import _ensure_incoming_qc_schema

router = APIRouter(prefix="/qc", tags=["QC"])


@router.get("/sampling")
def sampling_config(request: Request, db: Session = Depends(get_db)):
    require_area(request, "qc")
    ensure_schema(db)
    _ensure_incoming_qc_schema(db)
    cid = int(request.session.get("company_id") or 1)
    cfg = get_config(db, cid)

    # Recent auto-releases, so the effect of the plan is visible on the same
    # screen where it's configured rather than buried in the inspection list.
    recent = db.execute(text("""
        SELECT grn_no, supplier_name, notes, inspected_at
        FROM qc_incoming_inspections
        WHERE decision = 'Auto-Released' AND (company_id = :cid OR company_id IS NULL)
        ORDER BY id DESC LIMIT 20
    """), {"cid": cid}).mappings().all()

    stats = db.execute(text("""
        SELECT
          SUM(decision = 'Auto-Released') AS auto_released,
          SUM(decision = 'Passed')        AS inspected_pass,
          SUM(decision = 'Failed')        AS inspected_fail
        FROM qc_incoming_inspections
        WHERE (company_id = :cid OR company_id IS NULL)
    """), {"cid": cid}).mappings().first()

    return render(request, "qc/sampling.html", {
        "cfg": cfg, "recent": recent, "stats": stats or {},
        "page_title": "QC Sampling Plan",
    })


@router.post("/sampling")
async def sampling_save(request: Request, db: Session = Depends(get_db)):
    require_action(request, "qc", "edit")
    form = await request.form()
    cid = int(request.session.get("company_id") or 1)

    def _int(name: str, default: int, lo: int, hi: int) -> int:
        try:
            return max(lo, min(hi, int(form.get(name) or default)))
        except (TypeError, ValueError):
            return default

    save_config(db, cid, {
        "enabled": 1 if form.get("enabled") else 0,
        # Bounded deliberately. sample_every_n = 1 means "inspect everything"
        # (which is what disabling does) and a huge N means "inspect almost
        # nothing" — neither is a sampling plan, so the input can't express them.
        "sample_every_n": _int("sample_every_n", 10, 2, 100),
        "min_clean_receipts": _int("min_clean_receipts", 5, 0, 100),
        "failure_lookback_days": _int("failure_lookback_days", 30, 1, 365),
        "always_inspect_critical": 1 if form.get("always_inspect_critical") else 0,
        "always_inspect_cold_chain": 1 if form.get("always_inspect_cold_chain") else 0,
    }, updated_by=request.session.get("username", "system"))

    return RedirectResponse(
        "/qc/sampling?toast=success&title=Saved"
        "&msg=Sampling plan updated. It applies to receipts posted from now on.",
        status_code=303)
