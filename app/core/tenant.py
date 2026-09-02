# app/core/tenant.py
from __future__ import annotations
import contextvars
from sqlalchemy import event
from sqlalchemy.orm import Session as SASession
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

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
