# app/models/chef.py
from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text, func
from app.database.base import Base

class Chef(Base):
    __tablename__ = "chefs"

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(Integer, nullable=False, default=1, index=True)

    chef_code = Column(String(50), nullable=False, index=True)
    chef_name = Column(String(255), nullable=False, index=True)

    job_title = Column(String(150), nullable=True)
    kitchen_section = Column(String(150), nullable=True)
    tasks = Column(String(255), nullable=True)
    brand_assign = Column(String(255), nullable=True)
    remarks = Column(Text, nullable=True)

    status = Column(String(20), nullable=False, default="ACTIVE")
    version = Column(Integer, nullable=False, default=1)
    is_active = Column(Boolean, nullable=False, default=True)

    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
