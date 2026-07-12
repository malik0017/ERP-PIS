# app/models/audit.py
from sqlalchemy import (
    Column,
    Integer,
    String,
    DateTime
)

from datetime import datetime

from app.database.base import Base


class AuditLog(Base):

    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True)

    user_id = Column(Integer)

    action = Column(String(255))

    table_name = Column(String(100))

    record_id = Column(Integer)

    created_at = Column(
        DateTime,
        default=datetime.utcnow
    )