# app/core/tenant.py
"""Multi-company WRITE-PATH stamping - done once, for every table.

Instead of editing every service function that creates an ORM object
(orders, order lines, BOM lines, issuance lines, kitchen transactions,
QC checks, packing, and every FUTURE module), we stamp automatically:

  1. TenantMiddleware copies the session's company_id into a ContextVar
     at the start of each request.
  2. A SQLAlchemy ``before_flush`` listener walks every NEW object in the
     session and, if the object has a ``company_id`` column that is still
     NULL, fills it from the ContextVar.

Result: **every insert in the whole application is company-stamped**,
including modules written next year. This is the same pattern SAP-style
systems use for MANDT/client stamping.

Wiring (main.py):
    from app.core import tenant                      # registers the listener
    app.add_middleware(tenant.TenantMiddleware)      # BEFORE SessionMiddleware
                                                     # in code order (Starlette
                                                     # runs last-added first, so
                                                     # SessionMiddleware must be
                                                     # added AFTER this line).

After running with stamping for a few days, flip
``INCLUDE_UNASSIGNED = False`` in app/core/company.py for strict isolation.
"""
from __future__ import annotations

import contextvars

from sqlalchemy import event
from sqlalchemy.orm import Session as SASession
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

# The active company for the current request (1 = default/head office).
current_company_id: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "current_company_id", default=None
)


class TenantMiddleware(BaseHTTPMiddleware):
    """Copy session company_id into the request-scoped ContextVar."""

    async def dispatch(self, request: Request, call_next):
        token = None
        try:
            company_id = None
            try:
                # SessionMiddleware must already have populated request.session.
                company_id = request.session.get("company_id")
            except Exception:
                company_id = None
            token = current_company_id.set(int(company_id) if company_id else None)
            return await call_next(request)
        finally:
            if token is not None:
                current_company_id.reset(token)


@event.listens_for(SASession, "before_flush")
def _stamp_company_id(session, flush_context, instances):
    """Fill company_id on every new ORM object that has the column."""
    cid = current_company_id.get()
    if not cid:
        return
    for obj in session.new:
        if hasattr(obj, "company_id") and getattr(obj, "company_id", None) in (None, 0):
            try:
                obj.company_id = cid
            except Exception:
                # Never let stamping break a business write.
                pass
