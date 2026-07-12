# app/models/role.py 

from sqlalchemy import (
    Column,
    Integer,
    String,
    DateTime
)

from sqlalchemy.orm import relationship
from datetime import datetime

from app.database.base import Base
from app.models.permission import role_permissions

class Role(Base):

    __tablename__ = "roles"

    id = Column(Integer, primary_key=True)

    name = Column(
        String(100),
        unique=True,
        nullable=False
    )

    description = Column(
        String(255)
    )

    created_at = Column(
        DateTime,
        default=datetime.utcnow
    )

    users = relationship(
        "User",
        back_populates="role"
    )

    permissions = relationship(
    "Permission",
    secondary=role_permissions,
    back_populates="roles"
    )