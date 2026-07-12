# app/core/auth.py

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.user import User
from app.core.security import create_access_token as build_access_token
from app.core.security import verify_token


security_scheme = HTTPBearer(auto_error=False)


def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(security_scheme),
    db: Session = Depends(get_db),
) -> User:
    """
    Current user dependency for normal web pages.

    Priority:
    1. Session login: request.session["user_id"]
    2. Bearer token: Authorization: Bearer <token>
    """

    user_id = None

    try:
        user_id = request.session.get("user_id")
    except Exception:
        user_id = None

    if user_id:
        user = db.query(User).filter(User.id == int(user_id)).first()

        if user and getattr(user, "is_active", True):
            return user

    if credentials and credentials.credentials:
        try:
            token_data = verify_token(credentials.credentials)
        except Exception:
            token_data = None

        if token_data:
            token_user_id = getattr(token_data, "user_id", None)

            if token_user_id is None and isinstance(token_data, dict):
                token_user_id = token_data.get("user_id")

            if token_user_id:
                user = db.query(User).filter(User.id == int(token_user_id)).first()

                if user and getattr(user, "is_active", True):
                    return user

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Not authenticated",
    )


def get_current_active_user(
    current_user: User = Depends(get_current_user),
) -> User:
    if not getattr(current_user, "is_active", True):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User account is inactive",
        )

    return current_user


class AuthService:
    """Authentication and authorization service."""

    @staticmethod
    def create_access_token(user_id: int, username: str, role: str) -> str:
        return build_access_token(user_id, username, role)

    @staticmethod
    def get_current_user(
        request: Request,
        credentials: HTTPAuthorizationCredentials | None = Depends(security_scheme),
        db: Session = Depends(get_db),
    ) -> User:
        return get_current_user(request, credentials, db)

    @staticmethod
    def get_current_active_user(
        current_user: User = Depends(get_current_user),
    ) -> User:
        return get_current_active_user(current_user)