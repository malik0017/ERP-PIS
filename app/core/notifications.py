# app/core/notifications.py
# =============================================================================
# Batch 78 — real, persisted, per-user/per-role notifications.
# -----------------------------------------------------------------------------
# This is deliberately separate from the "pending work queue" the bell has
# always shown (app/modules/notifications/routes.py::_collect) — that queue
# is a LIVE, recomputed-every-time view of orders currently stuck somewhere
# ("3 orders waiting on Head Chef approval right now"). There's nothing to
# mark as read there because it isn't a discrete event — it's a live count
# that changes the moment the underlying order moves.
#
# This module is the other half: a genuine notifications table for actual
# EVENTS ("Order ORD-... was submitted", "QC failed on ORD-...", "Payroll
# run #4 was finalized") that get created once, can be marked read, and
# persist even after the underlying condition that caused them changes.
#
# Usage from any module:
#   from app.core.notifications import create_notification, notify_role
#   create_notification(db, company_id=1, user_id=42, title="...", message="...", url="/...")
#   notify_role(db, company_id=1, role="HEAD_CHEF", title="...", message="...", url="/...")
#
# Both are best-effort: a notification failing to write NEVER raises and
# NEVER rolls back the caller's own transaction — the business action (order
# created, QC submitted, payroll finalized) must always succeed even if the
# notification side-effect can't.
# =============================================================================
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session


def ensure_notifications_schema(db: Session) -> None:
    try:
        db.execute(text("""
            CREATE TABLE IF NOT EXISTS notifications (
                id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                company_id INT NULL,
                user_id INT NULL,
                role VARCHAR(40) NULL,
                category VARCHAR(40) NOT NULL DEFAULT 'general',
                title VARCHAR(200) NOT NULL,
                message VARCHAR(500) NULL,
                url VARCHAR(300) NULL,
                is_read TINYINT(1) NOT NULL DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                read_at DATETIME NULL,
                KEY idx_notif_user (user_id, is_read),
                KEY idx_notif_role (role, is_read),
                KEY idx_notif_company (company_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """))
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass


def create_notification(
    db: Session,
    *,
    company_id: int | None,
    title: str,
    message: str | None = None,
    url: str | None = None,
    user_id: int | None = None,
    role: str | None = None,
    category: str = "general",
) -> None:
    """Create one notification, targeted at either a specific user_id OR a
    role (every user with that role sees it), never both null. Best-effort:
    swallows all errors so a notification can never break the real action
    that triggered it."""
    if not user_id and not role:
        return
    try:
        ensure_notifications_schema(db)
        db.execute(text("""
            INSERT INTO notifications (company_id, user_id, role, category, title, message, url)
            VALUES (:cid, :uid, :role, :cat, :title, :msg, :url)
        """), {
            "cid": company_id, "uid": user_id, "role": role, "cat": category[:40],
            "title": (title or "")[:200], "msg": (message or "")[:500], "url": (url or "")[:300],
        })
        db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass


def notify_role(db: Session, *, company_id: int | None, role: str, title: str,
                message: str | None = None, url: str | None = None, category: str = "general") -> None:
    create_notification(db, company_id=company_id, role=role, title=title,
                        message=message, url=url, category=category)


def notify_user(db: Session, *, company_id: int | None, user_id: int, title: str,
                message: str | None = None, url: str | None = None, category: str = "general") -> None:
    create_notification(db, company_id=company_id, user_id=user_id, title=title,
                        message=message, url=url, category=category)
