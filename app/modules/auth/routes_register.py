# app/modules/auth/routes_register.py
# =============================================================================
# Batch 71 — Public self-service registration
# -----------------------------------------------------------------------------
# Goal: anyone can register themselves, then log in, place orders and manage
# them in the customer portal. The old JSON `/api/auth/register` only created a
# User row with the CUSTOMER role and NO linked customer — so a self-registered
# user could log in but the portal couldn't resolve them to a customer, and
# they couldn't place an order.
#
# This form-based `POST /register`:
#   1. validates the input and uniqueness,
#   2. creates the User (CUSTOMER role),
#   3. AUTO-CREATES a matching `customers` row,
#   4. links them via users.customer_code,
#   5. logs the user straight in and sends them to the portal.
#
# Registered in main.py:
#     from app.modules.auth.routes_register import router as self_register_router
#     app.include_router(self_register_router)
#
# The GET /register page (already in main.py) renders auth/register.html, which
# this batch rewrites into a real form posting here.
# =============================================================================

import logging
import re
from datetime import datetime

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database.session import get_db
from app.core.security import hash_password
from app.core.templates import render
from app.models.user import User
from app.models.role import Role

logger = logging.getLogger(__name__)
router = APIRouter(tags=["auth"])


def _next_customer_code(db: Session) -> str:
    """Generate the next self-service customer code: SELF-000001, SELF-000002…"""
    try:
        row = db.execute(text("""
            SELECT customer_code FROM customers
            WHERE customer_code LIKE 'SELF-%'
            ORDER BY id DESC LIMIT 1
        """)).scalar()
        n = 1
        if row:
            m = re.search(r"(\d+)$", str(row))
            if m:
                n = int(m.group(1)) + 1
        return f"SELF-{n:06d}"
    except Exception:
        return f"SELF-{int(datetime.utcnow().timestamp())}"


def _ensure_customer_code_column(db: Session) -> None:
    """users.customer_code must exist for the portal to resolve the login."""
    try:
        db.execute(text("SELECT customer_code FROM users LIMIT 1"))
    except Exception:
        try:
            db.execute(text("ALTER TABLE users ADD COLUMN customer_code VARCHAR(50) NULL"))
            db.commit()
        except Exception:
            db.rollback()


def _fail(request: Request, msg: str, form: dict):
    return render(request, "auth/register.html", {
        "page_title": "Register - ISFC PIMS",
        "error": msg, "form": form,
    }, status_code=200)


@router.post("/register")
async def self_register(
    request: Request,
    full_name: str = Form(""),
    email: str = Form(""),
    username: str = Form(""),
    password: str = Form(""),
    phone: str = Form(""),
    db: Session = Depends(get_db),
):
    full_name = (full_name or "").strip()
    email = (email or "").strip().lower()
    username = (username or "").strip()
    phone = (phone or "").strip()
    form = {"full_name": full_name, "email": email, "username": username, "phone": phone}

    # --- validation ---
    if not full_name or not email or not username or not password:
        return _fail(request, "All fields are required.", form)
    if len(username) < 3:
        return _fail(request, "Username must be at least 3 characters.", form)
    if len(password) < 6:
        return _fail(request, "Password must be at least 6 characters.", form)
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        return _fail(request, "Please enter a valid email address.", form)

    existing = db.query(User).filter(
        (User.username == username) | (User.email == email)
    ).first()
    if existing:
        return _fail(request, "That username or email is already registered. Try logging in.", form)

    role = db.query(Role).filter(Role.name == "CUSTOMER").first()
    if not role:
        # create the CUSTOMER role on the fly so registration never dead-ends
        try:
            db.execute(text("INSERT INTO roles (name, description) VALUES ('CUSTOMER','Self-service customer')"))
            db.commit()
            role = db.query(Role).filter(Role.name == "CUSTOMER").first()
        except Exception:
            db.rollback()
    if not role:
        return _fail(request, "Registration is temporarily unavailable. Please contact support.", form)

    _ensure_customer_code_column(db)
    customer_code = _next_customer_code(db)

    # --- create the linked customer master row ---
    try:
        db.execute(text("""
            INSERT INTO customers (company_id, customer_code, customer_name, contact_person,
                                   phone, email, customer_type, status, is_active, created_at)
            VALUES (1, :code, :name, :name, :phone, :email, 'Self-Service', 'ACTIVE', 1, :now)
        """), {"code": customer_code, "name": full_name, "phone": phone,
               "email": email, "now": datetime.utcnow()})
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.warning("self-register customer create failed: %s", exc)

    # --- create the user, linked to that customer ---
    try:
        new_user = User(
            username=username, email=email, full_name=full_name,
            password_hash=hash_password(password), role_id=role.id,
            is_active=True, is_verified=False, preferred_language="en",
            created_at=datetime.utcnow(),
        )
        db.add(new_user)
        db.commit()
        db.refresh(new_user)
        db.execute(text("UPDATE users SET customer_code = :c WHERE id = :i"),
                   {"c": customer_code, "i": new_user.id})
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.error("self-register user create failed: %s", exc)
        return _fail(request, "Could not create your account. Please try again.", form)

    # --- log the user straight in ---
    request.session["user_id"] = new_user.id
    request.session["username"] = new_user.username
    request.session["user_role"] = "CUSTOMER"
    request.session["company_id"] = 1
    request.session["user_access"] = {}
    request.session["user_actions"] = {}

    return RedirectResponse(
        "/my?toast=success&title=Welcome&msg=Your account is ready — place your first order below.",
        status_code=303)
