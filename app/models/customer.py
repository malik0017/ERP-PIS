# app/models/customer.py
from sqlalchemy import Boolean, Column, DateTime, Integer, String, func
from app.database.base import Base


class Customer(Base):
    __tablename__ = "customers"

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(Integer, nullable=False, default=1, index=True)

    customer_code = Column(String(50), nullable=False, index=True)
    customer_name = Column(String(255), nullable=False, index=True)
    customer_name_ar = Column(String(255), nullable=True)

    sales_man = Column(String(150), nullable=True)
    contact_person = Column(String(150), nullable=True)
    phone = Column(String(80), nullable=True)
    email = Column(String(150), nullable=True)
    brand = Column(String(150), nullable=True)
    vat_number = Column(String(100), nullable=True)
    customer_type = Column(String(150), nullable=True)
    city = Column(String(100), nullable=True)
    payment_terms = Column(String(150), nullable=True)

    status = Column(String(20), nullable=False, default="ACTIVE")
    version = Column(Integer, nullable=False, default=1)
    is_active = Column(Boolean, nullable=False, default=True)

    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
