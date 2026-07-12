# app/models/company.py
from sqlalchemy import (
    Column,
    Integer,
    String
)

from sqlalchemy.orm import relationship

from app.database.base import Base


class Company(Base):

    __tablename__ = "companies"

    id = Column(Integer, primary_key=True)

    name = Column(String(255))

    name_ar = Column(String(255))

    email = Column(String(255))

    phone = Column(String(100))

    address = Column(String(500))

    logo = Column(String(255))

    # Relationship with users table
    users = relationship(
        "User",
        back_populates="company"
    )

