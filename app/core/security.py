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