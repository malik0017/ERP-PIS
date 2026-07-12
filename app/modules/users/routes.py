# app/modules/users/routes.py
from __future__ import annotations

from datetime import datetime, timedelta
import base64
import os
import urllib.parse
import hashlib
import hmac
import struct
import time
from io import BytesIO

from fastapi import APIRouter, Depends, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.core.rbac import PAGE_AREAS, can_access, normalized_role
from app.core.security import hash_password, verify_password
from app.core.templates import render
from app.core.audit import write_audit
from app.database import get_db
from app.models.role import Role
from app.models.user import User

router = APIRouter(tags=["Users & Access"])

MODULES = [
    ("dashboard", "Dashboard", "Executive production intelligence dashboard"),
    ("relationship", "Relationship Map", "SAP-style document relationship map"),
    ("reports", "Reports Center", "Operational and management reports"),
    ("master_upload", "Master Upload", "Upload customers, suppliers, inventory and recipes"),
    ("master_data", "Master Data Lists", "Customers, brands, suppliers, inventory and sections"),
    ("recipe_list", "Recipes & Costing", "Recipe master, costing and approvals"),
    ("recipe_prepare", "Prepare Recipe", "Manual recipe entry and costing"),
    ("recipe_missing", "Recipe Missing Data", "Missing cost/code control"),
    ("recipe_approvals", "Recipe Approvals", "Duplicate recipe version approval"),
    ("order_portal", "Customer / Internal Order", "Customer-facing order entry screen"),
    ("production_orders", "Production Orders", "Production order list and document"),
    ("head_chef", "Head Chef Planning", "Cooking/material receiving schedule approval"),
    ("bom", "BOM", "Generate and view BOM documents"),
    ("store_issuance", "Store Issuance", "Issue materials to production sections"),
    ("kitchen_summary", "All Section Summary", "Kitchen section summary and filters"),
    ("section_thawing", "Section: Thawing", "Thawing workstation"),
    ("section_cutting", "Section: Cutting", "Cutting workstation"),
    ("section_butchery", "Section: Butchery", "Butchery workstation"),
    ("section_marination", "Section: Marination", "Marination workstation"),
    ("section_hot_kitchen", "Section: Hot Kitchen", "Hot kitchen workstation"),
    ("section_cold_kitchen", "Section: Cold Kitchen", "Cold kitchen workstation"),
    ("section_bakery_pastry", "Section: Bakery/Pastry", "Bakery/Pastry workstation"),
    ("qc", "Quality Control", "QC checks and release to packing"),
    ("packing", "Trayline / Packing", "Packing, rejection and release to dispatch"),
    ("dispatch", "Dispatch / Delivery", "Dispatch, delivery and closure"),
    ("procurement", "Procurement (PO / GRN)", "Purchase orders, GRN and supplier purchasing"),
    ("inventory_valuation", "Inventory Valuation", "Stock ledger, on hand and item valuation"),
    ("finance", "Finance", "AR, AP, payments and statements"),
    # Batch 10: newly UI-manageable areas — tick these in the matrix, no code needed.
    ("module_home", "Module Launcher", "ERP module landing page after login"),
    ("project_management", "Project Management", "Projects, tasks, teams and timelines"),
    ("hr", "HCM (Human Capital)", "Employees, staff and HR dashboard"),
    ("customer_portal", "Customer Portal", "Customer-facing order tracking portal (/my)"),
    ("settings", "System Settings", "Company branding, security settings"),
    ("users", "Users & Roles", "User profile, role access and admin security"),
    ("audit", "Audit Log", "Login, lock, unlock, reset and access change history"),
]
ADMIN_ROLES = {"ADMIN", "SUPER_ADMIN", "ADMINISTRATOR"}


def _is_admin(request: Request) -> bool:
    return normalized_role(request) in ADMIN_ROLES


def _ensure_schema(db: Session) -> None:
    db.execute(text("""
        CREATE TABLE IF NOT EXISTS user_page_access (
            id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
            user_id INT NOT NULL,
            page_key VARCHAR(80) NOT NULL,
            allowed TINYINT(1) NOT NULL DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            UNIQUE KEY uq_user_page_access (user_id, page_key),
            KEY idx_user_access_user (user_id),
            CONSTRAINT fk_user_page_access_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """))
    for col, definition in {"can_view": "TINYINT(1) NOT NULL DEFAULT 1 AFTER allowed", "can_add": "TINYINT(1) NOT NULL DEFAULT 0 AFTER can_view", "can_edit": "TINYINT(1) NOT NULL DEFAULT 0 AFTER can_add", "can_delete": "TINYINT(1) NOT NULL DEFAULT 0 AFTER can_edit", "can_export": "TINYINT(1) NOT NULL DEFAULT 0 AFTER can_delete"}.items():
        exists = db.execute(text("""
            SELECT COUNT(*) FROM information_schema.columns
            WHERE table_schema = DATABASE() AND table_name = 'user_page_access' AND column_name = :col
        """), {"col": col}).scalar()
        if not exists:
            db.execute(text(f"ALTER TABLE user_page_access ADD COLUMN {col} {definition}"))

    for col, definition in {
        "failed_login_attempts": "INT NOT NULL DEFAULT 0 AFTER is_verified",
        "customer_code": "VARCHAR(50) NULL AFTER is_verified",
        "locked_until": "DATETIME NULL AFTER failed_login_attempts",
        "two_factor_enabled": "TINYINT(1) NOT NULL DEFAULT 0 AFTER locked_until",
        "two_factor_secret": "VARCHAR(255) NULL AFTER two_factor_enabled",
        "two_factor_setup_required": "TINYINT(1) NOT NULL DEFAULT 0 AFTER two_factor_secret",
        "two_factor_setup_expires_at": "DATETIME NULL AFTER two_factor_setup_required",
        "two_factor_verified_at": "DATETIME NULL AFTER two_factor_setup_expires_at",
        "two_factor_reset_at": "DATETIME NULL AFTER two_factor_verified_at",
        "force_password_change": "TINYINT(1) NOT NULL DEFAULT 0 AFTER two_factor_reset_at",
    }.items():
        exists = db.execute(text("""
            SELECT COUNT(*) FROM information_schema.columns
            WHERE table_schema = DATABASE() AND table_name = 'users' AND column_name = :col
        """), {"col": col}).scalar()
        if not exists:
            db.execute(text(f"ALTER TABLE users ADD COLUMN {col} {definition}"))
    db.commit()


def _load_access(db: Session, user_id: int) -> dict:
    rows = db.execute(text("""
        SELECT page_key, allowed,
               COALESCE(can_view, allowed) AS can_view,
               COALESCE(can_add, 0) AS can_add,
               COALESCE(can_edit, 0) AS can_edit,
               COALESCE(can_delete, 0) AS can_delete,
               COALESCE(can_export, 0) AS can_export
        FROM user_page_access WHERE user_id=:id
    """), {"id": user_id}).mappings().all()
    return {r["page_key"]: dict(r) for r in rows}

def _ensure_audit_schema(db: Session) -> None:
    db.execute(text("""
        CREATE TABLE IF NOT EXISTS audit_logs (
            id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
            user_id INT NULL,
            action VARCHAR(255) NULL,
            table_name VARCHAR(100) NULL,
            record_id INT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """))
    for col, definition in {
        "description": "TEXT NULL AFTER record_id",
        "ip_address": "VARCHAR(80) NULL AFTER description",
        "user_agent": "VARCHAR(500) NULL AFTER ip_address",
    }.items():
        exists = db.execute(text("""
            SELECT COUNT(*) FROM information_schema.columns
            WHERE table_schema = DATABASE() AND table_name = 'audit_logs' AND column_name = :col
        """), {"col": col}).scalar()
        if not exists:
            db.execute(text(f"ALTER TABLE audit_logs ADD COLUMN {col} {definition}"))
    db.commit()




def _new_totp_secret() -> str:
    return base64.b32encode(os.urandom(20)).decode("ascii").replace("=", "")


def _setup_expiry(days: int = 30):
    return datetime.utcnow() + timedelta(days=days)


def _otpauth_uri(username: str, secret: str, issuer: str = "ISFC PIS") -> str:
    """Build the standard otpauth URI used by Google/Microsoft Authenticator."""
    if not secret:
        return ""
    label = f"{issuer}:{username}"
    return "otpauth://totp/" + urllib.parse.quote(label) + "?" + urllib.parse.urlencode({
        "secret": secret,
        "issuer": issuer,
        "algorithm": "SHA1",
        "digits": "6",
        "period": "30",
    })

def _qr_png(data: str) -> bytes:
    """Render an otpauth:// URI as a PNG QR code.

    This helper was previously MISSING, which made /users/profile/2fa/qr.png
    return 500 and the profile page show a broken 'Authenticator QR' image.
    Primary: qrcode[pil] (already in requirements). Fallback: segno (pure
    Python). Last resort: a small PNG telling the user to use the manual key.
    """
    if not data:
        data = "otpauth://totp/ISFC"
    try:
        import qrcode  # qrcode[pil]==7.4.2 in requirements.txt
        img = qrcode.make(data, box_size=6, border=2)
        buf = BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        pass
    try:
        import segno
        buf = BytesIO()
        segno.make(data, error="m").save(buf, kind="png", scale=6, border=2)
        return buf.getvalue()
    except Exception:
        pass
    # 1x1 transparent PNG placeholder - manual secret entry still works
    return base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
        "YPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
    )


def _admin_required(request: Request):
    if not _is_admin(request):
        raise HTTPException(status_code=403, detail="Access denied for users")


@router.get("/users/profile", response_class=HTMLResponse)
def my_profile(request: Request, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    _ensure_schema(db)
    sec = db.execute(text("""
        SELECT failed_login_attempts, locked_until, two_factor_enabled, two_factor_secret,
               two_factor_setup_required, two_factor_setup_expires_at, two_factor_verified_at,
               force_password_change
        FROM users WHERE id = :id
    """), {"id": current_user.id}).mappings().first() or {}
    secret = str(sec.get("two_factor_secret") or "")
    return render(request, "users/profile.html", {"user": current_user, "security": sec, "otpauth_uri": _otpauth_uri(current_user.username, secret) if secret else ""})


@router.post("/users/profile")
async def save_profile(request: Request, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    form = await request.form()
    current_user.full_name = str(form.get("full_name") or current_user.full_name).strip()
    current_user.phone = str(form.get("phone") or "").strip() or None
    current_user.preferred_language = str(form.get("preferred_language") or "en").strip()
    current_user.updated_at = datetime.utcnow()
    db.commit()
    return RedirectResponse("/users/profile?toast=success&title=Profile Saved&msg=Your profile was updated.", status_code=303)


@router.post("/users/profile/password")
async def change_password(request: Request, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    form = await request.form()
    old_password = str(form.get("old_password") or "")
    new_password = str(form.get("new_password") or "")
    confirm_password = str(form.get("confirm_password") or "")
    if not verify_password(old_password, current_user.password_hash):
        return RedirectResponse("/users/profile?toast=error&title=Password Not Changed&msg=Current password is incorrect.", status_code=303)
    if len(new_password) < 8 or new_password != confirm_password:
        return RedirectResponse("/users/profile?toast=error&title=Password Not Changed&msg=Use at least 8 characters and confirm password correctly.", status_code=303)
    current_user.password_hash = hash_password(new_password)
    current_user.updated_at = datetime.utcnow()
    db.execute(text("UPDATE users SET failed_login_attempts = 0, locked_until = NULL, force_password_change = 0 WHERE id = :id"), {"id": current_user.id})
    db.commit()
    return RedirectResponse("/users/profile?toast=success&title=Password Changed&msg=Your password was updated successfully.", status_code=303)


@router.get("/admin/users", response_class=HTMLResponse)
def users_list(request: Request, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    _admin_required(request)
    _ensure_schema(db)
    users = db.execute(text("""
        SELECT u.id, u.username, u.email, u.full_name, u.phone, u.company_id,
               u.is_active, u.is_verified, u.last_login, u.failed_login_attempts,
               u.locked_until, u.two_factor_enabled, u.two_factor_setup_required, u.two_factor_setup_expires_at, r.name AS role_name
        FROM users u
        LEFT JOIN roles r ON r.id = u.role_id
        ORDER BY u.id
    """)).mappings().all()
    roles = db.query(Role).order_by(Role.name).all()
    return render(request, "users/admin_list.html", {"users": users, "roles": roles})


@router.post("/admin/users/create")
async def create_user(request: Request, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    _admin_required(request)
    _ensure_schema(db)
    form = await request.form()
    username = str(form.get("username") or "").strip()
    email = str(form.get("email") or "").strip()
    full_name = str(form.get("full_name") or username).strip()
    role_id = int(form.get("role_id") or 0)
    password = str(form.get("password") or "admin123")
    if not username or not email or not role_id:
        return RedirectResponse("/admin/users?toast=error&title=Missing Data&msg=Username, email and role are required.", status_code=303)
    existing = db.query(User).filter((User.username == username) | (User.email == email)).first()
    if existing:
        return RedirectResponse("/admin/users?toast=error&title=Duplicate User&msg=Username or email already exists.", status_code=303)
    user = User(username=username, email=email, full_name=full_name, password_hash=hash_password(password), role_id=role_id, company_id=1, is_active=True, is_verified=True, preferred_language="en")
    db.add(user)
    db.flush()
    write_audit(db, current_user.id, f"USER_CREATED:{username}", "users", user.id)
    db.commit()
    return RedirectResponse("/admin/users?toast=success&title=User Created&msg=User was created. Default password is set as entered.", status_code=303)


@router.get("/admin/users/{user_id}/access", response_class=HTMLResponse)
def user_access(request: Request, user_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    _admin_required(request)
    _ensure_schema(db)
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    roles = db.query(Role).order_by(Role.name).all()
    access = _load_access(db, user_id)
    return render(request, "users/access_edit.html", {"target_user": user, "roles": roles, "modules": MODULES, "access_map": access})


@router.post("/admin/users/{user_id}/access")
async def save_user_access(request: Request, user_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    _admin_required(request)
    _ensure_schema(db)
    form = await request.form()
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user.role_id = int(form.get("role_id") or user.role_id)
    user.is_active = str(form.get("is_active") or "0") == "1"
    user.is_verified = str(form.get("is_verified") or "0") == "1"
    two_factor = str(form.get("two_factor_enabled") or "0") == "1"
    force_password_change = str(form.get("force_password_change") or "0") == "1"
    db.execute(text("UPDATE users SET two_factor_enabled = :two_factor, force_password_change = :force WHERE id = :id"), {"two_factor": two_factor, "force": force_password_change, "id": user_id})

    selected = set(form.getlist("pages"))
    add_pages = set(form.getlist("add_pages"))
    edit_pages = set(form.getlist("edit_pages"))
    delete_pages = set(form.getlist("delete_pages"))
    export_pages = set(form.getlist("export_pages"))
    db.execute(text("DELETE FROM user_page_access WHERE user_id = :id"), {"id": user_id})
    for page_key, _, _ in MODULES:
        # If an action is ticked, View must be granted automatically.
        allowed = page_key in selected or page_key in add_pages or page_key in edit_pages or page_key in delete_pages or page_key in export_pages
        db.execute(text("""
            INSERT INTO user_page_access (user_id, page_key, allowed, can_view, can_add, can_edit, can_delete, can_export)
            VALUES (:user_id, :page_key, :allowed, :can_view, :can_add, :can_edit, :can_delete, :can_export)
        """), {
            "user_id": user_id, "page_key": page_key, "allowed": allowed,
            "can_view": allowed,
            "can_add": (page_key in add_pages),
            "can_edit": (page_key in edit_pages),
            "can_delete": (page_key in delete_pages),
            "can_export": (page_key in export_pages),
        })
    write_audit(db, current_user.id, f"ACCESS_UPDATED:{user.username}", "users", user_id)
    db.commit()
    return RedirectResponse(f"/admin/users/{user_id}/access?toast=success&title=Access Saved&msg=Access updated. User should login again to refresh menu.", status_code=303)


@router.post("/admin/users/{user_id}/delete")
async def delete_user(request: Request, user_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    _admin_required(request)
    _ensure_schema(db)
    if int(current_user.id) == int(user_id):
        return RedirectResponse("/admin/users?toast=error&title=Delete Blocked&msg=You cannot delete your own active login user.", status_code=303)
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return RedirectResponse("/admin/users?toast=error&title=Not Found&msg=User was not found.", status_code=303)
    username = user.username
    db.execute(text("DELETE FROM user_page_access WHERE user_id = :id"), {"id": user_id})
    db.delete(user)
    write_audit(db, current_user.id, f"USER_DELETED:{username}", "users", user_id)
    db.commit()
    return RedirectResponse("/admin/users?toast=success&title=User Deleted&msg=User and access matrix removed.", status_code=303)


@router.post("/admin/users/{user_id}/unlock")
async def unlock_user(request: Request, user_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    _admin_required(request)
    _ensure_schema(db)
    db.execute(text("UPDATE users SET failed_login_attempts = 0, locked_until = NULL, is_active = 1 WHERE id = :id"), {"id": user_id})
    write_audit(db, current_user.id, f"USER_UNLOCKED:{user_id}", "users", user_id)
    db.commit()
    return RedirectResponse("/admin/users?toast=success&title=User Unlocked&msg=User can login again.", status_code=303)


@router.post("/admin/users/{user_id}/reset-password")
async def admin_reset_password(request: Request, user_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    _admin_required(request)
    form = await request.form()
    password = str(form.get("new_password") or "admin123")
    if len(password) < 8:
        return RedirectResponse("/admin/users?toast=error&title=Weak Password&msg=Password must be at least 8 characters.", status_code=303)
    user = db.query(User).filter(User.id == user_id).first()
    if user:
        user.password_hash = hash_password(password)
        db.execute(text("UPDATE users SET failed_login_attempts = 0, locked_until = NULL, force_password_change = 1 WHERE id = :id"), {"id": user_id})
        write_audit(db, current_user.id, f"PASSWORD_RESET_BY_ADMIN:{user.username}", "users", user_id)
        db.commit()
    return RedirectResponse("/admin/users?toast=success&title=Password Reset&msg=Temporary password set to admin123 by default. User should change password on next login.", status_code=303)


@router.get("/admin/users/{user_id}/edit", response_class=HTMLResponse)
def edit_user(request: Request, user_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    _admin_required(request)
    _ensure_schema(db)
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    roles = db.query(Role).order_by(Role.name).all()
    sec = db.execute(text("""
        SELECT failed_login_attempts, locked_until, two_factor_enabled, two_factor_secret,
               two_factor_setup_required, two_factor_setup_expires_at, two_factor_verified_at,
               two_factor_reset_at, force_password_change
        FROM users WHERE id=:id
    """), {"id": user_id}).mappings().first() or {}
    secret = str(sec.get("two_factor_secret") or "")
    customers = db.execute(text("""
        SELECT customer_code, customer_name FROM customers
        ORDER BY customer_name LIMIT 1000
    """)).mappings().all()
    linked_code = str((db.execute(text("SELECT COALESCE(customer_code,'') FROM users WHERE id=:id"), {"id": user_id}).scalar()) or "")
    return render(request, "users/admin_edit.html", {"target_user": user, "roles": roles, "security": sec, "customers": customers, "linked_customer_code": linked_code, "otpauth_uri": _otpauth_uri(user.username, secret) if secret else ""})


@router.post("/admin/users/{user_id}/edit")
async def save_edit_user(request: Request, user_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    _admin_required(request)
    _ensure_schema(db)
    form = await request.form()
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user.username = str(form.get("username") or user.username).strip()
    user.email = str(form.get("email") or user.email).strip()
    user.full_name = str(form.get("full_name") or user.full_name).strip()
    user.phone = str(form.get("phone") or "").strip() or None
    user.company_id = int(form.get("company_id") or user.company_id or 1)
    user.role_id = int(form.get("role_id") or user.role_id)
    user.is_active = str(form.get("is_active") or "0") == "1"
    user.is_verified = str(form.get("is_verified") or "0") == "1"
    user.updated_at = datetime.utcnow()
    customer_code = str(form.get("customer_code") or "").strip()
    db.execute(text("UPDATE users SET customer_code=:cc WHERE id=:id"),
               {"cc": customer_code or None, "id": user_id})
    two_factor_enabled = str(form.get("two_factor_enabled") or "0") == "1"
    force_password_change = str(form.get("force_password_change") or "0") == "1"
    db.execute(text("UPDATE users SET two_factor_enabled=:tfa, force_password_change=:force WHERE id=:id"), {"tfa": two_factor_enabled, "force": force_password_change, "id": user_id})
    write_audit(db, current_user.id, f"USER_EDITED:{user.username}", "users", user_id)
    db.commit()
    return RedirectResponse(f"/admin/users/{user_id}/edit?toast=success&title=User Saved&msg=User profile and security flags updated.", status_code=303)


@router.post("/admin/users/{user_id}/2fa/generate")
async def generate_2fa_secret(request: Request, user_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    _admin_required(request)
    _ensure_schema(db)
    secret = _new_totp_secret()
    expires_at = _setup_expiry()
    # Important: do NOT enable login 2FA until the user scans QR and verifies a code.
    db.execute(text("""
        UPDATE users
        SET two_factor_secret=:secret,
            two_factor_enabled=0,
            two_factor_setup_required=1,
            two_factor_setup_expires_at=:expires_at,
            two_factor_verified_at=NULL,
            two_factor_reset_at=NOW()
        WHERE id=:id
    """), {"secret": secret, "expires_at": expires_at, "id": user_id})
    write_audit(db, current_user.id, f"2FA_ENROLLMENT_RESET:{user_id}", "users", user_id, f"Admin generated 30-day 2FA enrollment QR. Expires {expires_at}", request)
    db.commit()
    return RedirectResponse(f"/admin/users/{user_id}/edit?toast=success&title=2FA Enrollment Ready&msg=QR code generated. User must verify within 30 days; login will not require 2FA until verified.", status_code=303)


@router.get("/admin/users/{user_id}/2fa/qr.png")
def admin_user_2fa_qr(user_id: int, request: Request, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    _admin_required(request)
    _ensure_schema(db)
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    row = db.execute(text("SELECT two_factor_secret FROM users WHERE id=:id"), {"id": user_id}).mappings().first() or {}
    secret = str(row.get("two_factor_secret") or "")
    if not secret:
        raise HTTPException(status_code=404, detail="No 2FA secret found")
    return Response(content=_qr_png(_otpauth_uri(user.username, secret)), media_type="image/png")


@router.post("/admin/users/{user_id}/2fa/disable")
async def disable_2fa(request: Request, user_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    _admin_required(request)
    _ensure_schema(db)
    db.execute(text("UPDATE users SET two_factor_enabled=0, two_factor_secret=NULL, two_factor_setup_required=0, two_factor_setup_expires_at=NULL, two_factor_verified_at=NULL WHERE id=:id"), {"id": user_id})
    write_audit(db, current_user.id, f"2FA_DISABLED:{user_id}", "users", user_id)
    db.commit()
    return RedirectResponse(f"/admin/users/{user_id}/edit?toast=success&title=2FA Disabled&msg=Authenticator disabled for this user.", status_code=303)


@router.post("/users/profile/2fa/start")
async def profile_start_2fa(request: Request, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    _ensure_schema(db)
    secret = _new_totp_secret()
    expires_at = _setup_expiry()
    db.execute(text("""
        UPDATE users
        SET two_factor_secret=:secret,
            two_factor_enabled=0,
            two_factor_setup_required=1,
            two_factor_setup_expires_at=:expires_at,
            two_factor_verified_at=NULL,
            two_factor_reset_at=NOW()
        WHERE id=:id
    """), {"secret": secret, "expires_at": expires_at, "id": current_user.id})
    write_audit(db, current_user.id, "2FA_SELF_ENROLLMENT_STARTED", "users", current_user.id, "User generated authenticator setup QR", request)
    db.commit()
    return RedirectResponse("/users/profile?toast=success&title=Scan QR&msg=Scan the QR code and verify the 6-digit code to activate 2FA.", status_code=303)


@router.get("/users/profile/2fa/qr.png")
def profile_2fa_qr(request: Request, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    _ensure_schema(db)
    row = db.execute(text("SELECT two_factor_secret FROM users WHERE id=:id"), {"id": current_user.id}).mappings().first() or {}
    secret = str(row.get("two_factor_secret") or "")
    if not secret:
        raise HTTPException(status_code=404, detail="No 2FA setup found")
    return Response(content=_qr_png(_otpauth_uri(current_user.username, secret)), media_type="image/png")


@router.post("/users/profile/2fa/verify")
async def profile_verify_2fa(request: Request, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    _ensure_schema(db)
    form = await request.form()
    code = str(form.get("otp_code") or "").strip()
    row = db.execute(text("""
        SELECT two_factor_secret, two_factor_setup_expires_at
        FROM users WHERE id=:id
    """), {"id": current_user.id}).mappings().first() or {}
    secret = str(row.get("two_factor_secret") or "")
    expires_at = row.get("two_factor_setup_expires_at")
    if expires_at and expires_at < datetime.now():
        db.execute(text("UPDATE users SET two_factor_secret=NULL, two_factor_setup_required=0, two_factor_setup_expires_at=NULL WHERE id=:id"), {"id": current_user.id})
        write_audit(db, current_user.id, "2FA_ENROLLMENT_EXPIRED", "users", current_user.id, "User tried to verify after 30-day setup window", request)
        db.commit()
        return RedirectResponse("/users/profile?toast=error&title=2FA Expired&msg=Your setup window expired. Generate a new QR or ask admin to reset it.", status_code=303)
    if not _verify_totp(secret, code):
        write_audit(db, current_user.id, "2FA_SELF_VERIFY_FAILED", "users", current_user.id, "Invalid authenticator code during setup", request)
        db.commit()
        return RedirectResponse("/users/profile?toast=error&title=Invalid Code&msg=Open Authenticator and enter the current 6-digit code.", status_code=303)
    db.execute(text("""
        UPDATE users
        SET two_factor_enabled=1,
            two_factor_setup_required=0,
            two_factor_verified_at=NOW(),
            two_factor_setup_expires_at=NULL
        WHERE id=:id
    """), {"id": current_user.id})
    write_audit(db, current_user.id, "2FA_SELF_ACTIVATED", "users", current_user.id, "User scanned QR and verified 6-digit authenticator code", request)
    db.commit()
    return RedirectResponse("/users/profile?toast=success&title=2FA Activated&msg=Authenticator is now active for your login.", status_code=303)


@router.post("/users/profile/2fa/disable")
async def profile_disable_2fa(request: Request, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    _ensure_schema(db)
    form = await request.form()
    password = str(form.get("current_password") or "")
    if not verify_password(password, current_user.password_hash):
        return RedirectResponse("/users/profile?toast=error&title=Not Disabled&msg=Current password is required to disable 2FA.", status_code=303)
    db.execute(text("UPDATE users SET two_factor_enabled=0, two_factor_secret=NULL, two_factor_setup_required=0, two_factor_setup_expires_at=NULL, two_factor_verified_at=NULL WHERE id=:id"), {"id": current_user.id})
    write_audit(db, current_user.id, "2FA_SELF_DISABLED", "users", current_user.id, "User disabled authenticator from profile", request)
    db.commit()
    return RedirectResponse("/users/profile?toast=success&title=2FA Disabled&msg=Authenticator has been disabled.", status_code=303)



@router.get("/admin/audit-logs", response_class=HTMLResponse)
def audit_logs(request: Request, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    _admin_required(request)
    _ensure_audit_schema(db)
    q = str(request.query_params.get("q") or "").strip()
    params = {"q": f"%{q}%"}
    where = ""
    if q:
        where = "WHERE CAST(a.action AS CHAR) LIKE :q OR CAST(a.table_name AS CHAR) LIKE :q OR CAST(u.username AS CHAR) LIKE :q"
    rows = db.execute(text(f"""
        SELECT a.id, a.user_id, u.username, a.action, a.table_name, a.record_id,
               COALESCE(a.description,'') AS description, COALESCE(a.ip_address,'') AS ip_address,
               COALESCE(a.user_agent,'') AS user_agent, a.created_at
        FROM audit_logs a
        LEFT JOIN users u ON u.id = a.user_id
        {where}
        ORDER BY a.id DESC
        LIMIT 500
    """), params).mappings().all()
    return render(request, "users/audit_logs.html", {"logs": rows, "q": q})


# =========================================================================
# PROFILE MANAGEMENT EXTENSIONS (Batch 4): avatar upload
# (Profile save, password change and preferred_language already exist above.)
# =========================================================================
import os as _os
from fastapi import UploadFile, File as _File

_AVATAR_DIR = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.dirname(__file__))), "static", "uploads", "avatars")


@router.post("/users/profile/avatar")
async def upload_avatar(request: Request, avatar: UploadFile = _File(...),
                        db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Self-service profile photo. Saves to /static/uploads/avatars/user_<id>.<ext>
    and stores the URL on users.avatar (shown in the header)."""
    ext = (avatar.filename or "").rsplit(".", 1)[-1].lower()
    if ext not in {"png", "jpg", "jpeg", "webp"}:
        return RedirectResponse("/users/profile?toast=error&title=Avatar&msg=Use PNG, JPG or WEBP.", status_code=303)
    data = await avatar.read()
    if len(data) > 2 * 1024 * 1024:
        return RedirectResponse("/users/profile?toast=error&title=Avatar&msg=Max size is 2MB.", status_code=303)
    _os.makedirs(_AVATAR_DIR, exist_ok=True)
    fname = f"user_{current_user.id}." + ("jpg" if ext == "jpeg" else ext)
    with open(_os.path.join(_AVATAR_DIR, fname), "wb") as fh:
        fh.write(data)
    current_user.avatar = f"/static/uploads/avatars/{fname}"
    current_user.updated_at = datetime.utcnow()
    db.commit()
    return RedirectResponse("/users/profile?toast=success&title=Profile Photo&msg=Your photo was updated.", status_code=303)
