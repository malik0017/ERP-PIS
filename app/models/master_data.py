# app/models/master_data.py
from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text, func
from app.database.base import Base


class MasterRecord(Base):
    """Generic archive table that preserves every uploaded Excel row as JSON."""
    __tablename__ = "master_records"

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(Integer, nullable=False, default=1, index=True)
    master_type = Column(String(50), nullable=False, index=True)
    code = Column(String(100), nullable=False, index=True)
    name_en = Column(String(255), nullable=True)
    name_ar = Column(String(255), nullable=True)
    version = Column(Integer, nullable=False, default=1)
    status = Column(String(20), nullable=False, default="ACTIVE")
    is_active = Column(Boolean, nullable=False, default=True)
    approval_status = Column(String(20), nullable=False, default="APPROVED")
    raw_json = Column(Text, nullable=True)
    remarks = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class Brand(Base):
    __tablename__ = "brands"

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(Integer, nullable=False, default=1, index=True)
    brand_code = Column(String(50), nullable=False, index=True)
    brand_name_en = Column(String(255), nullable=False)
    brand_name_ar = Column(String(255), nullable=True)
    short_code = Column(String(50), nullable=True)
    revenue_stream_code = Column(String(255), nullable=True)
    revenue_stream_name = Column(String(255), nullable=True)
    default_kitchen_code = Column(String(50), nullable=True)
    status = Column(String(20), nullable=False, default="ACTIVE")
    # Kept in database for internal audit compatibility, hidden from UI by client request.
    version = Column(Integer, nullable=False, default=1)
    is_active = Column(Boolean, nullable=False, default=True)
    approval_status = Column(String(20), nullable=False, default="APPROVED")
    remarks = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class RevenueStream(Base):
    __tablename__ = "revenue_streams"

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(Integer, nullable=False, default=1, index=True)
    stream_code = Column(String(50), nullable=False, index=True)
    stream_name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    revenue_category = Column(String(150), nullable=True)
    status = Column(String(20), nullable=False, default="ACTIVE")
    version = Column(Integer, nullable=False, default=1)
    is_active = Column(Boolean, nullable=False, default=True)
    approval_status = Column(String(20), nullable=False, default="APPROVED")
    remarks = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class KitchenLocation(Base):
    __tablename__ = "kitchen_locations"

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(Integer, nullable=False, default=1, index=True)
    kitchen_code = Column(String(50), nullable=False, index=True)
    kitchen_name = Column(String(255), nullable=False)
    kitchen_type = Column(String(150), nullable=True)
    location = Column(String(255), nullable=True)
    city = Column(String(100), nullable=True)
    brand_supported = Column(Text, nullable=True)
    capacity = Column(String(100), nullable=True)
    manager = Column(String(255), nullable=True)
    status = Column(String(20), nullable=False, default="ACTIVE")
    version = Column(Integer, nullable=False, default=1)
    is_active = Column(Boolean, nullable=False, default=True)
    approval_status = Column(String(20), nullable=False, default="APPROVED")
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class KitchenSection(Base):
    __tablename__ = "kitchen_sections"

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(Integer, nullable=False, default=1, index=True)
    section_code = Column(String(50), nullable=False, index=True)
    section_name = Column(String(255), nullable=False)
    section_name_ar = Column(String(255), nullable=True)
    kitchen_code = Column(String(50), nullable=True)
    sequence_no = Column(Integer, nullable=False, default=1)
    status = Column(String(20), nullable=False, default="ACTIVE")
    is_active = Column(Boolean, nullable=False, default=True)
    remarks = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
