# app/init_db.py

from app.database.base import Base
from app.database.session import engine

from app.models.user import User
from app.models.role import Role
from app.models.company import Company
from app.models.setting import SystemSetting
from app.models.permission import Permission
from app.models.audit_log import AuditLog

print(Base.metadata.tables.keys())

Base.metadata.create_all(bind=engine)

print("Database Created Successfully")