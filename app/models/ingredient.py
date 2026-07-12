from datetime import datetime
from sqlalchemy import Boolean, Column, DateTime, Float, Integer, String, Text
from app.database.base import Base


class Ingredient(Base):
    __tablename__ = "ingredients"

    id = Column(Integer, primary_key=True, index=True)
    ingredient_code = Column(String(50), unique=True, index=True, nullable=False)
    name = Column(String(255), nullable=False, index=True)
    category = Column(String(100), nullable=True)
    main_category = Column(String(150), nullable=True)
    sub_category = Column(String(150), nullable=True)

    purchase_uom = Column(String(20), default="Kg")
    recipe_uom = Column(String(20), default="Kg")
    standard_uom = Column(String(20), default="Kg")
    uom_group = Column(String(20), default="Mass")
    conversion_to_standard = Column(Float, default=1.0)

    default_supplier = Column(String(255), nullable=True)
    storage_type = Column(String(50), nullable=True)
    shelf_life_days = Column(Float, default=0)
    unit_cost_standard = Column(Float, default=0)
    min_stock_standard = Column(Float, default=0)
    reorder_level_standard = Column(Float, default=0)
    expected_yield_pct = Column(Float, default=100)

    default_issue_section = Column(String(80), default="Hot Kitchen")
    requires_thawing = Column(Boolean, default=False)
    requires_cutting = Column(Boolean, default=False)
    requires_butchery = Column(Boolean, default=False)
    requires_marination = Column(Boolean, default=False)
    is_bakery_item = Column(Boolean, default=False)
    is_cold_kitchen_item = Column(Boolean, default=False)
    expiry_tracking = Column(Boolean, default=True)
    lot_tracking = Column(Boolean, default=True)
    critical_item = Column(Boolean, default=False)

    status = Column(String(30), default="Active", index=True)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)