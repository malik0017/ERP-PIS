# app/core/stage_lock.py
"""
Batch 121 — Pipeline step-locking.

Rule (client requirement): once an order has moved PAST a pipeline stage, that
earlier stage becomes VIEW-ONLY. You can still open it and extract reports/CSV/
PDF, but you cannot edit/re-issue/re-transfer it. Editing is only allowed on the
stage the order is currently at (or stages it hasn't reached yet, which simply
have nothing to edit).

This is deliberately centralised so every module asks the same question the same
way, instead of each route inventing its own status checks.

Design
------
* PIPELINE lists the ordered production stages.
* Each order status maps to the stage the order is currently AT (STATUS_STAGE).
* A stage is LOCKED for editing when the order's current stage index is strictly
  greater than that stage's index — i.e. the order has already moved on.
* Terminal/exception statuses (Delivered, Cancelled, QC Rejected, QC Hold) lock
  every production stage; correction happens through the dedicated flow, not by
  silently editing a finished step.

Admins can be allowed to override via `can_override` (kept centralised so the
policy is in one place); by default override is OFF to match "enforce fully".
"""
from __future__ import annotations

# Ordered pipeline stages (keys used by routes/templates).
PIPELINE = [
    "sales",        # sales request / review
    "head_chef",    # planning & schedule approval
    "bom",          # BOM generation
    "store",        # store issuance
    "kitchen",      # kitchen sections receive/process/transfer
    "qc",           # quality control
    "packing",      # trayline / packing
    "dispatch",     # dispatch / delivery
]
_INDEX = {s: i for i, s in enumerate(PIPELINE)}

# Map each order status to the stage the order is currently sitting AT.
# Anything not listed is treated as the earliest stage (nothing locked yet).
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


def is_stage_locked(status: str | None, stage: str) -> bool:
    """
    True when `stage` is view-only for an order in `status` — i.e. the order has
    already advanced beyond it, or the order is in a terminal/blocked state.

    Unknown stage names are treated as not-locked (fail open) so a typo can
    never wedge a screen; the RBAC layer still governs who may act at all.
    """
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
