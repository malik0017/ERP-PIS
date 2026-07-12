from datetime import datetime
from sqlalchemy import Column, Date, DateTime, Float, Integer, String, Text
from app.database.base import Base


class StockLot(Base):
    __tablename__ = "stock_lots"

    id = Column(Integer, primary_key=True, index=True)
    ingredient_code = Column(String(50), index=True, nullable=False)
    ingredient_name = Column(String(255), nullable=False)
    supplier_name = Column(String(255), nullable=True)
    lot_no = Column(String(100), index=True, nullable=False)
    received_date = Column(Date, nullable=True)
    expiry_date = Column(Date, nullable=True)
    standard_uom = Column(String(20), default="Kg")
    received_qty_standard = Column(Float, default=0)
    available_qty_standard = Column(Float, default=0)
    unit_cost_standard = Column(Float, default=0)
    storage_type = Column(String(50), nullable=True)
    status = Column(String(30), default="Available", index=True)
    remarks = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class InventoryTransaction(Base):
    __tablename__ = "inventory_transactions"

    id = Column(Integer, primary_key=True, index=True)
    transaction_no = Column(String(80), unique=True, index=True, nullable=False)
    transaction_date = Column(DateTime, default=datetime.utcnow, index=True)
    ingredient_code = Column(String(50), index=True, nullable=False)
    ingredient_name = Column(String(255), nullable=False)
    lot_no = Column(String(100), index=True, nullable=True)
    transaction_type = Column(String(50), index=True, nullable=False)  # GRN, Issue, Return, Adjustment, Waste, Transfer
    qty_standard = Column(Float, default=0)
    standard_uom = Column(String(20), default="Kg")
    from_location = Column(String(100), nullable=True)
    to_location = Column(String(100), nullable=True)
    reference_type = Column(String(80), nullable=True)
    reference_no = Column(String(100), nullable=True)
    performed_by = Column(String(255), nullable=True)
    remarks = Column(Text, nullable=True)