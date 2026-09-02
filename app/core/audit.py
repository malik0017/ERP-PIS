from __future__ import annotations
from sqlalchemy import text
from sqlalchemy.orm import Session
from fastapi import Request


def request_meta(request: Request | None) -> tuple[str | None, str | None]:
    if request is None:
        return None, None
    ip = None
    try:
        forwarded = request.headers.get("x-forwarded-for")
        ip = (forwarded.split(",")[0].strip() if forwarded else None) or (request.client.host if request.client else None)
    except Exception:
        ip = None
    try:
        ua = request.headers.get("user-agent", "")[:500]
    except Exception:
        ua = None
    return ip, ua


def write_audit(
    db: Session,
    user_id: int | None,
    action: str,
    table_name: str | None = None,
    record_id: int | None = None,
    description: str | None = None,
    request: Request | None = None,
) -> None:
   
    ip_address, user_agent = request_meta(request)
    try:
        db.execute(text(
            "INSERT INTO audit_logs (user_id, action, table_name, record_id, description, ip_address, user_agent, created_at) "
            "VALUES (:user_id, :action, :table_name, :record_id, :description, :ip_address, :user_agent, NOW())"
        ), {
            "user_id": user_id,
            "action": (action or "")[:255],
            "table_name": (table_name or '')[:100],
            "record_id": record_id,
            "description": (description or "")[:1000],
            "ip_address": (ip_address or "")[:80],
            "user_agent": (user_agent or "")[:500],
        })
    except Exception:
        try:
            db.execute(text(
                "INSERT INTO audit_logs (user_id, action, table_name, record_id, created_at) "
                "VALUES (:user_id, :action, :table_name, :record_id, NOW())"
            ), {"user_id": user_id, "action": (action or "")[:255], "table_name": (table_name or '')[:100], "record_id": record_id})
        except Exception:
            pass
