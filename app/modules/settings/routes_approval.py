# app/modules/settings/routes_approval.py
# =============================================================================
# Batch 111 — approval tier configuration.
#
# Kept out of the engine (core/approval_chain.py) so the rules stay readable
# in one place and the screen stays a thin editor over them.
# =============================================================================
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import text
from sqlalchemy.orm import Session
from urllib.parse import quote

from app.core import approval_chain as ac
from app.core.rbac import require_area, require_action
from app.core.templates import render
from app.database.session import get_db

router = APIRouter(prefix="/settings/approvals", tags=["Settings"])

DOC_TYPES = [
    ("purchase_requisition", "Purchase Requisition"),
]


def _cid(request: Request) -> int:
    return int(request.session.get("company_id") or 1)


@router.get("")
def approvals_screen(request: Request, db: Session = Depends(get_db)):
    require_area(request, "settings")
    ac.ensure_schema(db)
    cid = _cid(request)
    doc_type = (request.query_params.get("doc_type") or "purchase_requisition").strip()

    try:
        tiers = [dict(r) for r in db.execute(text("""
            SELECT * FROM approval_tiers
            WHERE doc_type = :d AND (company_id = :cid OR company_id IS NULL)
            ORDER BY min_value
        """), {"d": doc_type, "cid": cid}).mappings().all()]
    except Exception:
        tiers = []

    # In-flight documents, so an admin can see what changing a tier will and
    # will not affect. Tiers already assigned to a document are locked in.
    try:
        active = [dict(r) for r in db.execute(text("""
            SELECT doc_no, MAX(doc_value) AS doc_value,
                   COUNT(*) AS total_steps,
                   SUM(status = 'Approved') AS done_steps
            FROM approval_steps
            WHERE doc_type = :d AND (company_id = :cid OR company_id IS NULL)
            GROUP BY doc_no
            HAVING done_steps < total_steps
            ORDER BY doc_value DESC LIMIT 50
        """), {"d": doc_type, "cid": cid}).mappings().all()]
    except Exception:
        active = []

    return render(request, "settings/approvals.html", {
        "tiers": tiers, "doc_type": doc_type, "doc_types": DOC_TYPES,
        "roles": sorted(ac.ROLE_RANK, key=lambda r: ac.ROLE_RANK[r]),
        "in_flight": active,
        "page_title": "Approval Hierarchy",
    })


@router.post("/save")
async def save_tiers(request: Request, db: Session = Depends(get_db)):
    """Replace the ladder for one document type.

    Validated before anything is written: a gap or an overlap in the ranges
    means some value has no tier or two tiers, and the engine would silently
    pick one. Better to refuse and say where the gap is.
    """
    require_action(request, "settings", "edit")
    ac.ensure_schema(db)
    cid = _cid(request)
    form = await request.form()
    doc_type = (form.get("doc_type") or "purchase_requisition").strip()

    mins = form.getlist("min_value")
    maxs = form.getlist("max_value")
    steps = form.getlist("steps")

    rows = []
    for i in range(len(mins)):
        raw_steps = (steps[i] if i < len(steps) else "").strip()
        if not raw_steps:
            continue
        try:
            mn = float(mins[i] or 0)
        except ValueError:
            mn = 0.0
        mx_raw = (maxs[i] if i < len(maxs) else "").strip()
        mx = None
        if mx_raw:
            try:
                mx = float(mx_raw)
            except ValueError:
                mx = None
        clean = [x.strip().upper().replace(" ", "_") for x in raw_steps.split(",") if x.strip()]
        if not clean:
            continue
        rows.append({"min": mn, "max": mx, "steps": ",".join(clean)})

    if not rows:
        return RedirectResponse(
            f"/settings/approvals?doc_type={doc_type}&toast=warning"
            f"&title={quote('Nothing saved')}&msg={quote('Define at least one tier.')}",
            status_code=303)

    rows.sort(key=lambda r: r["min"])

    # Validation: the ladder must cover every value with no gap and no overlap.
    problems = []
    if rows[0]["min"] > 0:
        problems.append(f"Nothing covers values below {rows[0]['min']:.2f}.")
    for i, r in enumerate(rows[:-1]):
        nxt = rows[i + 1]
        if r["max"] is None:
            problems.append(f"Tier starting at {r['min']:.2f} is open-ended but is not the last tier.")
        elif abs(r["max"] - nxt["min"]) > 0.0001:
            # Ranges are [min, max) so contiguity means max == next min
            # exactly. Anything else is a gap or an overlap.
            if r["max"] < nxt["min"]:
                problems.append(f"Gap: nothing covers {r['max']:.2f} to {nxt['min']:.2f}.")
            else:
                problems.append(f"Overlap: {r['min']:.2f}–{r['max']:.2f} overlaps the next tier "
                                f"starting at {nxt['min']:.2f}.")
    if rows[-1]["max"] is not None:
        problems.append(f"The highest tier must be open-ended, or values above "
                        f"{rows[-1]['max']:.2f} have no approval path.")

    if problems:
        return RedirectResponse(
            f"/settings/approvals?doc_type={doc_type}&toast=danger"
            f"&title={quote('Ladder is not valid')}&msg={quote(' '.join(problems[:3]))}",
            status_code=303)

    try:
        db.execute(text("DELETE FROM approval_tiers WHERE doc_type = :d "
                        "AND (company_id = :cid OR company_id IS NULL)"),
                   {"d": doc_type, "cid": cid})
        for r in rows:
            db.execute(text("""
                INSERT INTO approval_tiers
                    (company_id, doc_type, min_value, max_value, steps, is_active, updated_by)
                VALUES (:cid, :d, :mn, :mx, :s, 1, :by)
            """), {"cid": cid, "d": doc_type, "mn": r["min"], "mx": r["max"],
                   "s": r["steps"], "by": request.session.get("username", "system")})
        db.commit()
    except Exception:
        db.rollback()
        return RedirectResponse(
            f"/settings/approvals?doc_type={doc_type}&toast=danger"
            f"&title={quote('Save failed')}&msg={quote('The tiers could not be written.')}",
            status_code=303)

    return RedirectResponse(
        f"/settings/approvals?doc_type={doc_type}&toast=success"
        f"&title={quote('Approval Ladder Saved')}"
        f"&msg={quote(f'{len(rows)} tier(s) saved. Documents already part-way through approval keep the ladder they started with.')}",
        status_code=303)
