from sqlalchemy import (
    Column,
    Integer,
    String,
    DateTime,
    Table,
    ForeignKey
)

from sqlalchemy.orm import relationship
from datetime import datetime
from app.database.base import Base

role_permissions = Table(
    "role_permissions",
    Base.metadata,

    Column(
        "role_id",
        Integer,
        ForeignKey("roles.id")
    ),

    Column(
        "permission_id",
        Integer,
        ForeignKey("permissions.id")
    )
)

class Permission(Base):

    __tablename__ = "permissions"

    id = Column(Integer, primary_key=True)

    name = Column(
        String(100),
        unique=True,
        nullable=False
    )

    description = Column(String(255))

    created_at = Column(
        DateTime,
        default=datetime.utcnow
    )

    roles = relationship(
        "Role",
        secondary=role_permissions,
        back_populates="permissions"
    )