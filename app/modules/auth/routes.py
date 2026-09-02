# app/modules/auth/routes.py
from fastapi import APIRouter, Depends, HTTPException, status, Request
from fastapi.responses import RedirectResponse, JSONResponse
from sqlalchemy.orm import Session
from sqlalchemy import text
from datetime import datetime, timedelta
import base64
import hashlib
import hmac
import logging
import secrets
import struct
import time

from ...core.security import hash_password, verify_password
from ...core.templates import templates
from ...database import get_db
from ...models.user import User
from ...models.role import Role
from ...core.audit import write_audit
from ...schemas.auth import UserLoginRequest, UserRegisterRequest, LoginResponse, UserResponse
from app.core.security import needs_rehash, hash_password

logger = logging.getLogger(__name__)

SECURITY_COLUMNS = {
    "failed_login_attempts": "INT NOT NULL DEFAULT 0 AFTER is_verified",
    "locked_until": "DATETIME NULL AFTER failed_login_attempts",
    "two_factor_enabled": "TINYINT(1) NOT NULL DEFAULT 0 AFTER locked_until",
    "two_factor_secret": "VARCHAR(255) NULL AFTER two_factor_enabled",
    "two_factor_setup_required": "TINYINT(1) NOT NULL DEFAULT 0 AFTER two_factor_secret",
    "two_factor_setup_expires_at": "DATETIME NULL AFTER two_factor_setup_required",
    "two_factor_verified_at": "DATETIME NULL AFTER two_factor_setup_expires_at",
    "two_factor_reset_at": "DATETIME NULL AFTER two_factor_verified_at",
    "force_password_change": "TINYINT(1) NOT NULL DEFAULT 0 AFTER two_factor_reset_at",
}


def _column_exists(db: Session, table_name: str, column_name: str) -> bool:
    return bool(db.execute(text("""
        SELECT COUNT(*)
        FROM information_schema.columns
        WHERE table_schema = DATABASE()
          AND table_name = :table_name
          AND column_name = :column_name
    """), {"table_name": table_name, "column_name": column_name}).scalar())


def _ensure_auth_schema(db: Session) -> None:
    """Create security/access columns lazily so login will not fail on older DB copies."""
    for column, definition in SECURITY_COLUMNS.items():
        if not _column_exists(db, "users", column):
            db.execute(text(f"ALTER TABLE users ADD COLUMN {column} {definition}"))

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
    for column, definition in {
        "can_view": "TINYINT(1) NOT NULL DEFAULT 1 AFTER allowed",
        "can_add": "TINYINT(1) NOT NULL DEFAULT 0 AFTER can_view",
        "can_edit": "TINYINT(1) NOT NULL DEFAULT 0 AFTER can_add",
        "can_delete": "TINYINT(1) NOT NULL DEFAULT 0 AFTER can_edit",
        "can_export": "TINYINT(1) NOT NULL DEFAULT 0 AFTER can_delete",
    }.items():
        if not _column_exists(db, "user_page_access", column):
            db.execute(text(f"ALTER TABLE user_page_access ADD COLUMN {column} {definition}"))
    db.execute(text("""
        CREATE TABLE IF NOT EXISTS password_reset_tokens (
            id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
            user_id INT NOT NULL,
            token VARCHAR(128) NOT NULL,
            expires_at DATETIME NOT NULL,
            used_at DATETIME NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY uq_password_reset_token (token),
            KEY idx_password_reset_user (user_id),
            CONSTRAINT fk_password_reset_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """))
    db.commit()


def _load_user_access(db: Session, user_id: int) -> dict:
    rows = db.execute(text("""
        SELECT page_key, allowed, COALESCE(can_view, allowed) AS can_view
        FROM user_page_access
        WHERE user_id = :user_id
    """), {"user_id": user_id}).mappings().all()
    return {str(r["page_key"]): bool(r["allowed"]) or bool(r["can_view"]) for r in rows}


def _load_user_actions(db: Session, user_id: int) -> dict:
    rows = db.execute(text("""
        SELECT page_key, COALESCE(can_view, allowed) AS can_view, COALESCE(can_add,0) AS can_add,
               COALESCE(can_edit,0) AS can_edit, COALESCE(can_delete,0) AS can_delete, COALESCE(can_export,0) AS can_export
        FROM user_page_access
        WHERE user_id = :user_id
    """), {"user_id": user_id}).mappings().all()
    return {str(r["page_key"]): {"view": bool(r["can_view"]), "add": bool(r["can_add"]), "edit": bool(r["can_edit"]), "delete": bool(r["can_delete"]), "export": bool(r["can_export"])} for r in rows}



def _compact_user_access(access: dict) -> str:
    return "|".join(sorted([k for k, v in (access or {}).items() if v]))

def _compact_user_actions(actions: dict) -> str:
    parts = []
    for key, item in (actions or {}).items():
        if not isinstance(item, dict):
            continue
        acts = []
        for action in ("view", "add", "edit", "delete", "export"):
            if item.get(action) or item.get("can_" + action):
                acts.append(action)
        if acts:
            parts.append(f"{key}:{','.join(acts)}")
    return ";".join(sorted(parts))

def _first_allowed_url(access: dict) -> str:
    ordered = [
        ("module_home", "/modules"),
        ("dashboard", "/dashboard"),
        ("order_portal", "/orders/portal"),
        ("production_orders", "/production/orders"),
        ("head_chef", "/production/head-chef"),
        ("store_issuance", "/production/store-issuance"),
        ("kitchen_summary", "/production/kitchen-summary"),
        ("dispatch", "/dispatch"),
        ("reports", "/reports"),
        ("procurement", "/procurement"),
        ("inventory_valuation", "/inventory"),
        ("finance", "/finance"),
        ("recipe_list", "/recipes"),
    ]
    for key, url in ordered:
        if access.get(key):
            return url
    return "/users/profile"

def _load_system_branding(db: Session, company_id: int = 1) -> dict:
    try:
        cols = db.execute(text("""
            SELECT sidebar_title, sidebar_subtitle, header_title, header_subtitle,
                   authenticator_enabled, max_login_attempts
            FROM system_settings
            WHERE company_id = :company_id
            ORDER BY id DESC
            LIMIT 1
        """), {"company_id": company_id}).mappings().first()
        if cols:
            return dict(cols)
    except Exception:
        pass
    return {}




def _totp_now(secret: str, for_time: int | None = None, step: int = 30, digits: int = 6) -> str:
    if not secret:
        return ""
    clean = secret.replace(" ", "").upper()
    try:
        key = base64.b32decode(clean, casefold=True)
    except Exception:
        return ""
    counter = int((for_time or int(time.time())) / step)
    msg = struct.pack(">Q", counter)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7fffffff
    return str(code % (10 ** digits)).zfill(digits)


def _verify_totp(secret: str, code: str) -> bool:
    code = str(code or "").strip().replace(" ", "")
    if not code.isdigit() or len(code) != 6:
        return False
    now = int(time.time())
    # one time-step tolerance on either side for clock drift
    return any(hmac.compare_digest(_totp_now(secret, now + drift), code) for drift in (-30, 0, 30))


def _needs_2fa(db: Session, user_id: int, company_id: int) -> bool:
    row = db.execute(text("""
        SELECT two_factor_enabled, two_factor_secret, two_factor_verified_at,
               two_factor_setup_required, two_factor_setup_expires_at
        FROM users WHERE id = :id
    """), {"id": user_id}).mappings().first() or {}

    setup_required = bool(row.get("two_factor_setup_required"))
    expires_at = row.get("two_factor_setup_expires_at")
    if setup_required and expires_at and expires_at < datetime.now():
        db.execute(text("""
            UPDATE users
            SET two_factor_enabled=0, two_factor_secret=NULL, two_factor_setup_required=0,
                two_factor_setup_expires_at=NULL, two_factor_verified_at=NULL
            WHERE id=:id
        """), {"id": user_id})
        write_audit(db, user_id, "2FA_ENROLLMENT_AUTO_EXPIRED", "users", user_id)
        db.commit()
        return False

    enabled_user = bool(row.get("two_factor_enabled")) and bool(row.get("two_factor_secret")) and bool(row.get("two_factor_verified_at"))
    try:
        settings = _load_system_branding(db, company_id)
        system_enabled = bool(settings.get("authenticator_enabled"))
    except Exception:
        system_enabled = False
    return enabled_user and system_enabled

def _login_error_response(request: Request, is_json_request: bool, username: str | None, message: str, status_code: int = 401):
    if is_json_request:
        raise HTTPException(status_code=status_code, detail=message)
    return templates.TemplateResponse(
        name="auth/login.html",
        context={"request": request, "error": message, "username": username or ""},
        status_code=200,
    )

router = APIRouter(prefix="/api/auth", tags=["auth"])

# ===== API: REGISTER (JSON POST) =====
@router.post("/register", response_model=UserResponse)
async def api_register(
    user_data: UserRegisterRequest,
    db: Session = Depends(get_db)
):
    """
    API endpoint for user registration
    """
    
    logger.info(f"📝 Register attempt for user: {user_data.username}")
    
    # Check if user already exists
    existing_user = db.query(User).filter(
        (User.username == user_data.username) |
        (User.email == user_data.email)
    ).first()
    
    if existing_user:
        logger.warning(f"⚠️  User already exists: {user_data.username}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Username or email already registered"
        )
    
    # Get CUSTOMER role (default role for new users)
    customer_role = db.query(Role).filter(Role.name == "CUSTOMER").first()
    if not customer_role:
        logger.error("❌ CUSTOMER role not found")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="System error: default role not found"
        )
    
    # Hash password
    password_hash = hash_password(user_data.password)
    
    # Create new user
    new_user = User(
        username=user_data.username,
        email=user_data.email,
        full_name=user_data.full_name,
        password_hash=password_hash,
        role_id=customer_role.id,
        is_active=True,
        is_verified=False,
        preferred_language="en",
        created_at=datetime.utcnow()
    )
    
    db.add(new_user)
    db.commit()
    db.refresh(new_user)
    
    logger.info(f"✅ User registered successfully: {new_user.username}")
    
    return UserResponse(
        id=new_user.id,
        username=new_user.username,
        email=new_user.email,
        full_name=new_user.full_name,
        role=new_user.role.name if new_user.role else "CUSTOMER",
        language="en"
    )

# ===== LOGIN (POST) - Handles both Form and JSON =====
@router.post("/login")
async def login(
    request: Request,
    db: Session = Depends(get_db)
):
    
    
    username = None
    password = None
    is_json_request = False
    otp_code = None
    
    # Try to get data from JSON body
    try:
        body = await request.json()
        username = body.get("username")
        password = body.get("password")
        otp_code = body.get("otp_code")
        is_json_request = True
    except:
        # Not JSON, try form data
        try:
            form_data = await request.form()
            username = form_data.get("username")
            password = form_data.get("password")
            otp_code = form_data.get("otp_code")
        except:
            pass
    
    # Validate inputs
    if not username or not password:
        error_msg = "Username and password are required"
        logger.warning(f"⚠️  Login attempt with missing credentials")
        
        if is_json_request:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=error_msg
            )
        else:
            return templates.TemplateResponse(
                name="auth/login.html",
                context={
                    "request": request,
                    "error": error_msg,
                    "username": username or ""
                }
            )
    
    logger.info(f"🔐 Login attempt: {username}")

    from ...core.security import login_rate_limited, login_rate_reset
    _client_ip = (request.client.host if request.client else "") or (request.headers.get("x-forwarded-for", "").split(",")[0].strip())
    if login_rate_limited(_client_ip):
        logger.warning(f"⚠️  Login rate limit hit from {_client_ip}")
        try:
            write_audit(db, None, "LOGIN_RATE_LIMITED", "users", None, f"Too many login attempts from {_client_ip}", request)
            db.commit()
        except Exception:
            pass
        return _login_error_response(request, is_json_request, username,
                                     "Too many login attempts. Please wait a few minutes and try again.", 429)

    _ensure_auth_schema(db)

    # Find user
    user = db.query(User).filter(User.username == username).first()
    
    if not user:
        logger.warning(f"⚠️  User not found: {username}")
        write_audit(db, None, f"LOGIN_FAILED_UNKNOWN_USER:{username}", "users", None, "Unknown username login attempt", request)
        db.commit()
        return _login_error_response(request, is_json_request, username, "Invalid username or password", 401)

    security_row = db.execute(text("""
        SELECT failed_login_attempts, locked_until, two_factor_enabled
        FROM users
        WHERE id = :id
    """), {"id": user.id}).mappings().first() or {}

    locked_until = security_row.get("locked_until")
    if locked_until and locked_until > datetime.now():
        return _login_error_response(
            request,
            is_json_request,
            username,
            f"Account is locked until {locked_until}. Contact administrator or reset password.",
            403,
        )

    # Verify password and lock after 5 wrong attempts.
    if not verify_password(password, user.password_hash):
        logger.warning(f"⚠️  Invalid password: {username}")
        attempts = int(security_row.get("failed_login_attempts") or 0) + 1
        max_attempts = 5
        brand = _load_system_branding(db, getattr(user, "company_id", None) or 1)
        try:
            max_attempts = int(brand.get("max_login_attempts") or 5)
        except Exception:
            max_attempts = 5

        if attempts >= max_attempts:
            locked_until_new = datetime.now() + timedelta(minutes=30)
            db.execute(text("""
                UPDATE users
                SET failed_login_attempts = :attempts,
                    locked_until = :locked_until
                WHERE id = :id
            """), {"attempts": attempts, "locked_until": locked_until_new, "id": user.id})
            write_audit(db, user.id, "ACCOUNT_LOCKED_30_MINUTES", "users", user.id, f"Account locked after {attempts} failed attempts until {locked_until_new}", request)
            db.commit()
            return _login_error_response(
                request,
                is_json_request,
                username,
                f"Too many failed login attempts. Account locked for 30 minutes until {locked_until_new.strftime('%Y-%m-%d %H:%M:%S')}.",
                403,
            )

        db.execute(text("""
            UPDATE users
            SET failed_login_attempts = :attempts
            WHERE id = :id
        """), {"attempts": attempts, "id": user.id})
        write_audit(db, user.id, f"LOGIN_FAILED_ATTEMPT_{attempts}", "users", user.id, f"Invalid password attempt {attempts}/{max_attempts}", request)
        db.commit()
        return _login_error_response(
            request,
            is_json_request,
            username,
            f"Invalid username or password. Attempts used: {attempts}/{max_attempts}",
            401,
        )

    # Check if active
    if not user.is_active:
        logger.warning(f"⚠️  Inactive user: {username}")
        error_msg = "Account is inactive. Contact administrator."
        
        if is_json_request:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=error_msg
            )
        else:
            return templates.TemplateResponse(
                name="auth/login.html",
                context={
                    "request": request,
                    "error": error_msg,
                    "username": username
                }
            )
    
    company_id = getattr(user, "company_id", None) or 1
    two_factor_row = db.execute(text("SELECT two_factor_secret FROM users WHERE id = :id"), {"id": user.id}).mappings().first() or {}
    if _needs_2fa(db, user.id, company_id):
        if not otp_code:
            if is_json_request:
                return JSONResponse(status_code=428, content={"detail": "Authenticator code required. Open Google Authenticator/Microsoft Authenticator and enter the 6-digit code."})
            return templates.TemplateResponse(
                name="auth/login.html",
                context={"request": request, "error": "Authenticator code required.", "username": username or "", "need_2fa": True},
                status_code=200,
            )
        if not _verify_totp(str(two_factor_row.get("two_factor_secret") or ""), str(otp_code)):
            write_audit(db, user.id, "LOGIN_FAILED_INVALID_2FA", "users", user.id, "Invalid authenticator code", request)
            db.commit()
            return _login_error_response(request, is_json_request, username, "Invalid authenticator code", 401)

    db.execute(text("""
        UPDATE users
        SET failed_login_attempts = 0,
            locked_until = NULL,
            last_login = NOW()
        WHERE id = :id
    """), {"id": user.id})
    write_audit(db, user.id, "LOGIN_SUCCESS", "users", user.id, "Successful login", request)
    db.commit()

    if needs_rehash(user.password_hash):
        db.execute(text("UPDATE users SET password_hash = :h WHERE id = :id"),
                {"h": hash_password(password), "id": user.id})
        db.commit()

    # ✅ SET SESSION
    role_name = user.role.name if user.role else "CUSTOMER"
    request.session["user_id"] = user.id
    request.session["username"] = user.username
    request.session["user_role"] = role_name
    request.session["company_id"] = getattr(user, "company_id", None) or 1
    # Batch 155: clear the IP throttle on success + seed idle-expiry clock.
    login_rate_reset(_client_ip)
    import time as _t
    request.session["_last_activity"] = _t.time()
    user_access_map = _load_user_access(db, user.id)
    user_action_map = _load_user_actions(db, user.id)
    request.session["user_access"] = _compact_user_access(user_access_map)
    request.session["user_actions"] = _compact_user_actions(user_action_map)

    branding = _load_system_branding(db, getattr(user, "company_id", None) or 1)
    request.session["sidebar_title"] = branding.get("sidebar_title") or "ISFC PIS"
    request.session["sidebar_subtitle"] = branding.get("sidebar_subtitle") or "Production Intelligence"
    request.session["header_title"] = branding.get("header_title") or "ISFC"
    request.session["header_subtitle"] = branding.get("header_subtitle") or "Production"

    role_name = (user.role.name if user.role else "CUSTOMER") or "CUSTOMER"
    target_url = "/my" if role_name.upper() == "CUSTOMER" else "/modules"
    
    logger.info(f"✅ Login successful: {username}")
    
    if is_json_request:
        # Return JSON for API requests
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "access_token": "session",
                "token_type": "session",
                "user": {
                    "id": user.id,
                    "username": user.username,
                    "email": user.email,
                    "full_name": user.full_name,
                    "role": user.role.name if user.role else "CUSTOMER",
                    "language": user.preferred_language
                },
                "redirect_url": target_url
            }
        )
    else:
        return RedirectResponse(url=target_url, status_code=302)


@router.get("/forgot-password")
async def forgot_password_page(request: Request):
    return templates.TemplateResponse("auth/login.html", {"request": request, "error": "Password reset UI will be connected to email SMTP. For now, ask admin to reset password from User Management.", "username": ""})


@router.post("/forgot-password")
async def forgot_password(request: Request, db: Session = Depends(get_db)):
    _ensure_auth_schema(db)
    form = await request.form()
    email = str(form.get("email") or "").strip().lower()
    user = db.query(User).filter(User.email == email).first() if email else None
    if user:
        token = secrets.token_urlsafe(48)
        db.execute(text("""
            INSERT INTO password_reset_tokens (user_id, token, expires_at)
            VALUES (:user_id, :token, :expires_at)
        """), {"user_id": user.id, "token": token, "expires_at": datetime.utcnow() + timedelta(hours=2)})
        db.commit()
        # In production this token must be emailed using SMTP. It is not displayed to users.
        logger.info(f"Password reset token created for {email}: {token}")
    return RedirectResponse(url="/login?toast=success&title=Reset Requested&msg=If this email exists, a reset link will be sent.", status_code=303)

# ===== LOGOUT =====
@router.get("/logout")
async def logout(request: Request):
    """
    Clear session and redirect to login
    """
    username = request.session.get("username", "User")
    
    # Clear all session data
    request.session.clear()
    
    logger.info(f"👋 User logged out: {username}")
    
    return RedirectResponse(url="/login", status_code=302)

# ===== GET CURRENT USER =====
@router.get("/me", response_model=UserResponse)
async def get_current_user(
    request: Request,
    db: Session = Depends(get_db)
):
    """
    Get current authenticated user info
    """
    
    user_id = request.session.get("user_id")
    
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated"
        )
    
    user = db.query(User).filter(User.id == user_id).first()
    
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )
    
    return UserResponse(
        id=user.id,
        username=user.username,
        email=user.email,
        full_name=user.full_name,
        role=user.role.name if user.role else "CUSTOMER",
        language=user.preferred_language
    )