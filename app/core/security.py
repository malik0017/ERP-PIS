from datetime import datetime, timedelta, timezone
from typing import Optional
import hashlib
import hmac

from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel

from ..config import SECRET_KEY, JWT_ALGORITHM, JWT_EXPIRATION_HOURS


class TokenData(BaseModel):
    """JWT token payload"""
    user_id: int
    username: str
    role: str


# ===== PASSWORD HASHING =====
# New passwords are hashed with bcrypt. Existing users created under the old
# SHA256+salt scheme can still log in (see verify_password), and login can
# transparently upgrade them to bcrypt via needs_rehash().

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# Legacy scheme (kept only so old hashes can still be verified once).
_LEGACY_SALT = "isfc-salt-key-2024-production"
_BCRYPT_PREFIXES = ("$2a$", "$2b$", "$2y$")


def _legacy_sha256(password: str) -> str:
    return hashlib.sha256((password + _LEGACY_SALT).encode()).hexdigest()


def hash_password(password: str) -> str:
    """Hash a password with bcrypt (truncated safely to 72 bytes by passlib)."""
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a password against either a bcrypt hash or a legacy SHA256 hash."""
    if not hashed_password:
        return False
    if hashed_password.startswith(_BCRYPT_PREFIXES):
        try:
            return pwd_context.verify(plain_password, hashed_password)
        except Exception:
            return False
    # Legacy SHA256+salt fallback (constant-time compare).
    return hmac.compare_digest(_legacy_sha256(plain_password), hashed_password)


def needs_rehash(hashed_password: str) -> bool:
    """True if the stored hash is legacy and should be re-hashed with bcrypt."""
    if not hashed_password:
        return False
    return not hashed_password.startswith(_BCRYPT_PREFIXES)


# ===== JWT (unchanged) =====
def create_access_token(user_id: int, username: str, role: str) -> str:
    """Create JWT access token"""
    expires_at = datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRATION_HOURS)

    payload = {
        "user_id": user_id,
        "username": username,
        "role": role,
        "exp": expires_at.timestamp(),
    }

    token = jwt.encode(payload, SECRET_KEY, algorithm=JWT_ALGORITHM)
    return token


def verify_token(token: str) -> Optional[TokenData]:
    """Verify and decode JWT token"""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[JWT_ALGORITHM])

        user_id: int = payload.get("user_id")
        username: str = payload.get("username")
        role: str = payload.get("role")

        if user_id is None or username is None:
            return None

        return TokenData(user_id=user_id, username=username, role=role)

    except JWTError as e:
        print(f"Token verification error: {e}")
        return None

# ---------------------------------------------------------------------------
# Batch 155 — IP-level login rate limiting (in-memory sliding window).
#
# Complements the per-account lockout: even against many DIFFERENT usernames,
# a single IP can only attempt LOGIN_MAX_ATTEMPTS logins per LOGIN_WINDOW_SEC.
# In-memory is fine for a single-process deploy (Laragon/uvicorn); for a
# multi-worker deploy, move this to Redis with the same interface.
# ---------------------------------------------------------------------------
import time as _rl_time
from collections import defaultdict as _rl_dd

_LOGIN_ATTEMPTS: dict = _rl_dd(list)
LOGIN_MAX_ATTEMPTS = 10          # per IP
LOGIN_WINDOW_SEC = 300           # 5 minutes


def login_rate_limited(ip: str) -> bool:
    """Record an attempt from `ip` and return True if it is now over the limit.

    Call this once per login POST. Old timestamps outside the window are pruned
    so memory stays bounded to active IPs."""
    now = _rl_time.time()
    bucket = _LOGIN_ATTEMPTS[ip]
    cutoff = now - LOGIN_WINDOW_SEC
    # prune
    while bucket and bucket[0] < cutoff:
        bucket.pop(0)
    bucket.append(now)
    return len(bucket) > LOGIN_MAX_ATTEMPTS


def login_rate_reset(ip: str) -> None:
    """Clear an IP's attempt history (call on successful login)."""
    _LOGIN_ATTEMPTS.pop(ip, None)


# ---------------------------------------------------------------------------
# Batch 155 — CSRF validation helper.
#
# Session holds `_csrf_token`; forms submit it as `_csrf` (hidden field) or the
# X-CSRF-Token header. Templates get the token + a {{ csrf_form_field()|safe }}
# helper (see app/core/templates.py). Enforcement is opt-in per the CSRFMiddleware
# in main.py, which starts in MONITOR mode (logs mismatches, does not block) so it
# can be rolled out without breaking existing forms, then flipped to ENFORCE once
# every POST form carries the field.
# ---------------------------------------------------------------------------
def csrf_valid(request) -> bool:
    """True if the request carries a CSRF token matching the session token.
    Safe methods (GET/HEAD/OPTIONS) are always valid."""
    if request.method in ("GET", "HEAD", "OPTIONS", "TRACE"):
        return True
    try:
        session_tok = request.session.get("_csrf_token")
    except Exception:
        return True  # no session → nothing to protect (login etc.)
    if not session_tok:
        return True
    sent = request.headers.get("x-csrf-token")
    if not sent:
        # form field is read by the caller (middleware) since body isn't parsed here
        sent = getattr(request.state, "_csrf_form_value", None)
    return bool(sent) and sent == session_tok
