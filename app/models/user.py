# app/models/user.py
from sqlalchemy import (
    Column,
    Integer,
    String,
    Boolean,
    DateTime,
    ForeignKey
)
from sqlalchemy.orm import relationship
from app.database.base import Base
from datetime import datetime

class User(Base):

    __tablename__ = "users"

    id = Column(Integer, primary_key=True)

    username = Column(String(100), unique=True, nullable=False, index=True)

    email = Column(String(255), unique=True, nullable=False)

    full_name = Column(String(255), nullable=False)

    full_name_ar = Column(String(255))

    password_hash = Column(String(255), nullable=False)

    employee_code = Column(String(50), unique=True)

    phone = Column(String(50))

    avatar = Column(String(255))

    company_id = Column(
        Integer,
        ForeignKey("companies.id"),
        nullable=True
    )

    role_id = Column(
        Integer,
        ForeignKey("roles.id"),
        nullable=False
    )

    last_login = Column(DateTime)

    is_active = Column(Boolean, default=True)

    is_verified = Column(Boolean, default=False)

    preferred_language = Column(
        String(10),
        default="en"
    )

    created_at = Column(
        DateTime,
        default=datetime.utcnow
    )

    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow
    )

    # Relationships
    role = relationship(
        "Role",
        back_populates="users"
    )

    company = relationship(
        "Company",
        back_populates="users"
    )