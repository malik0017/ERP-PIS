# app/models/setting.py

from sqlalchemy import Boolean, Column, DateTime, Integer, String, func

from app.database.base import Base


class SystemSetting(Base):
    __tablename__ = "system_settings"

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(Integer, nullable=False, default=1, index=True)

    company_name = Column(String(255), nullable=True)
    company_name_ar = Column(String(255), nullable=True)

    logo = Column(String(255), nullable=True)
    favicon = Column(String(255), nullable=True)

    default_language = Column(String(10), nullable=False, default="en")
    timezone = Column(String(100), nullable=False, default="Asia/Riyadh")
    currency = Column(String(10), nullable=False, default="SAR")

    date_format = Column(String(50), nullable=False, default="dd-mm-yyyy")
    number_format = Column(String(50), nullable=False, default="1,234.00")

    is_rtl_enabled = Column(Boolean, nullable=False, default=True)
    is_active = Column(Boolean, nullable=False, default=True)

    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())