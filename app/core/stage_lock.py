# app/core/stage_lock.py

from __future__ import annotations

PIPELINE = [
    "sales",        
    "head_chef",    
    "bom",         
    "store",        
    "kitchen",     
    "qc",           
    "packing",      
    "dispatch",     
]
_INDEX = {s: i for i, s in enumerate(PIPELINE)}


STATUS_STAGE = {
    # sales / intake
    "Pending": "sales",
    "Submitted": "sales",
    "Approved": "sales",
    # head chef
    "Head Chef Approved": "head_chef",
    "Approved by Head Chef": "head_chef",
    # bom
    "BOM Generated": "bom",
    "Generated": "bom",
    # store
    "Store Pending": "store",
    "Issued": "store",
    "Short Issued": "store",
    # kitchen / production
    "In Production": "kitchen",
    "Partially Received": "kitchen",
    "Received": "kitchen",
    # qc
    "QC In Progress": "qc",
    "QC Received": "qc",
    "QC Passed": "qc",
    # packing
    "Packing Pending": "packing",
    "Packed": "packing",
    # dispatch
    "Out for Delivery": "dispatch",
    "Dispatched": "dispatch",
    "Delivered": "dispatch",
}

# Statuses that lock the ENTIRE production chain (finished or blocked).
TERMINAL = {"Delivered", "Cancelled", "Dispatched"}
BLOCKED = {"QC Rejected", "QC Hold"}


def current_stage(status: str | None) -> str:
    """Return the pipeline stage key the given order status sits at."""
    return STATUS_STAGE.get((status or "").strip(), "sales")


def current_index(status: str | None) -> int:
    return _INDEX.get(current_stage(status), 0)


# ---------------------------------------------------------------------------
# Batch 152 — GLOBAL ENABLE SWITCH
# ---------------------------------------------------------------------------
def stage_locks_enabled() -> bool:
    import os
    return str(os.getenv("ISFC_STAGE_LOCKS", "1")).strip().lower() not in (
        "0", "false", "no", "off")


def is_stage_locked(status: str | None, stage: str) -> bool:
    """
    True when `stage` is view-only for an order in `status` — i.e. the order has
    already advanced beyond it, or the order is in a terminal/blocked state.

    Unknown stage names are treated as not-locked (fail open) so a typo can
    never wedge a screen; the RBAC layer still governs who may act at all.
    """
    if not stage_locks_enabled():
        return False
    s = (status or "").strip()
    if stage not in _INDEX:
        return False
    if s in TERMINAL or s in BLOCKED:
        return True
    return current_index(s) > _INDEX[stage]


def can_edit_stage(status: str | None, stage: str, *, is_admin: bool = False,
                   allow_override: bool = False) -> bool:
    """Inverse of is_stage_locked, with an optional (default-off) admin override."""
    if is_stage_locked(status, stage):
        return bool(is_admin and allow_override)
    return True


def lock_reason(status: str | None, stage: str) -> str:
    """Human-readable explanation for why a stage is locked (for toasts/UI)."""
    s = (status or "").strip()
    if s in TERMINAL:
        return f"This order is {s.lower()} — earlier steps are view-only. You can still open and export reports."
    if s in BLOCKED:
        return f"This order is on {s} — production steps are locked until the QC issue is resolved."
    label = stage.replace("_", " ").title()
    return (f"The {label} step is already complete and locked. It's view-only now — "
            f"open it to review or extract a report; edits happen at the current stage.")
