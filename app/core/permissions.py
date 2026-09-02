# app/core/permissions.py
from typing import Optional
from fastapi import Depends, HTTPException, status
from sqlalchemy.orm import Session
from ..database.users import User
from .security import verify_token
from ..database import get_db

async def get_current_user(
    token: Optional[str] = None,
    db: Session = Depends(get_db)
) -> User:
    """Get current authenticated user"""
    
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    token_data = verify_token(token)
    if not token_data:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    user = db.query(User).filter(User.id == token_data.user_id).first()
    if not user or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or inactive",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    return user

def require_role(*allowed_roles: str):
    """Dependency to check if user has required role"""
    async def check_role(current_user: User = Depends(get_current_user)) -> User:
        if current_user.role.name not in allowed_roles and current_user.role.name != "ADMIN":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role {current_user.role.name} not allowed"
            )
        return current_user
    
    return check_role

def require_permission(permission_name: str):
    """Dependency to check if user has specific permission"""
    async def check_permission(current_user: User = Depends(get_current_user)) -> User:
        if not current_user.has_permission(permission_name):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Permission '{permission_name}' required"
            )
        return current_user
    
    return check_permission