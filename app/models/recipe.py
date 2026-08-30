# app/modules/recipe.py
from sqlalchemy import (
    Column,
    Integer,
    String,
    Text,
    Numeric,
    Boolean,
    DateTime,
    ForeignKey,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import relationship

from app.database.base import Base

class Recipe(Base):
    __tablename__ = "recipes"

    id = Column(Integer, primary_key=True, index=True)

    company_id = Column(Integer, nullable=False, index=True)

    recipe_code = Column(String(50), nullable=False, index=True)
    recipe_name = Column(String(255), nullable=False)
    brand_name = Column(String(150), nullable=True)
    customer_name = Column(String(150), nullable=True, index=True)
    category = Column(String(150), nullable=True, index=True)
    # Batch 101: weekly menu day, read from the "Day" column that already
    # exists in the Recipe Ingredients sheet. Drives the Frsh day-wise
    # ordering flow. Nullable — most customers have no weekly cycle.
    day_of_week = Column(String(120), nullable=True, index=True)

    version = Column(Integer, nullable=False, default=1)
    status = Column(String(20), nullable=False, default="ACTIVE")

    is_active = Column(Boolean, nullable=False, default=True)
    approval_status = Column(String(20), nullable=False, default="APPROVED")
    approved_by = Column(Integer, nullable=True)
    approved_at = Column(DateTime, nullable=True)
    parent_recipe_id = Column(Integer, nullable=True)

    is_sub_recipe = Column(Boolean, nullable=False, default=False)
    has_sub_recipe = Column(Boolean, nullable=False, default=False)
    linked_sub_recipe_code = Column(String(50), nullable=True)
    linked_sub_recipe_name = Column(String(255), nullable=True)
    linked_sub_recipe_portions = Column(Numeric(18, 4), nullable=False, default=0)

    standard_portions = Column(Numeric(18, 4), nullable=False, default=1)
    weight_per_portion_g = Column(Numeric(18, 4), nullable=False, default=0)
    size_of_portion = Column(Numeric(18, 4), nullable=False, default=0)

    std_yield_pct = Column(Numeric(10, 4), nullable=False, default=0.95)
    target_wastage_pct = Column(Numeric(10, 4), nullable=False, default=0.05)

    packaging_cost = Column(Numeric(18, 4), nullable=False, default=0)
    labor_cost = Column(Numeric(18, 4), nullable=False, default=0)
    delivery_cost = Column(Numeric(18, 4), nullable=False, default=0)
    overheads = Column(Numeric(18, 4), nullable=False, default=0)
    other_costs = Column(Numeric(18, 4), nullable=False, default=0)
    margin_pct = Column(Numeric(10, 4), nullable=False, default=0.30)

    food_cost = Column(Numeric(18, 4), nullable=False, default=0)
    food_cost_per_portion = Column(Numeric(18, 6), nullable=False, default=0)

    total_cost = Column(Numeric(18, 4), nullable=False, default=0)
    total_cost_per_portion = Column(Numeric(18, 6), nullable=False, default=0)

    sale_price = Column(Numeric(18, 4), nullable=False, default=0)
    sale_price_per_portion = Column(Numeric(18, 6), nullable=False, default=0)

    missing_cost_lines = Column(Integer, nullable=False, default=0)

    notes = Column(Text, nullable=True)
    remark = Column(Text, nullable=True)

    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    lines = relationship(
        "RecipeIngredient",
        back_populates="recipe",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    __table_args__ = (
        UniqueConstraint(
            "company_id",
            "recipe_code",
            "version",
            name="uq_recipe_company_code_version",
        ),
    )


class RecipeIngredient(Base):
    __tablename__ = "recipe_ingredients"

    id = Column(Integer, primary_key=True, index=True)

    recipe_id = Column(Integer, ForeignKey("recipes.id", ondelete="CASCADE"), nullable=False)
    line_no = Column(Integer, nullable=False)

    line_type = Column(String(30), nullable=False, default="Main Recipe")
    sub_recipe_code = Column(String(50), nullable=True)

    inventory_code = Column(String(80), nullable=True, index=True)
    item_name = Column(String(255), nullable=False)
    uom = Column(String(50), nullable=True)

    # Batch 131 — kitchen section this ingredient line is issued to, captured
    # from the recipe workbook's "Section" column ("Hot Section" etc.). Stored
    # as the RAW sheet value; it is mapped to a canonical system section at BOM /
    # store-issuance time via app.core.production_constants.resolve_issue_section.
    # Added via an import-time schema guard in main.py (raw column, no ORM lag).
    kitchen_section = Column(String(80), nullable=True)
    # Batch 136: "Butchery Cutting / Portion size" from the recipe workbook
    # (e.g. "Chicken Breast Butterfly - 140 g"). Shown in the Butchery section so
    # the butcher knows the exact cut/portion. Free text; may be blank.
    cutting_portion_size = Column(String(255), nullable=True)

    qty_batch = Column(Numeric(18, 4), nullable=False, default=0)
    portions = Column(Numeric(18, 4), nullable=False, default=1)
    qty_per_portion = Column(Numeric(18, 6), nullable=False, default=0)

    cost_uom = Column(Numeric(18, 6), nullable=False, default=0)
    line_cost = Column(Numeric(18, 4), nullable=False, default=0)
    line_cost_per_portion = Column(Numeric(18, 6), nullable=False, default=0)

    remark = Column(Text, nullable=True)
    missing_cost = Column(Boolean, nullable=False, default=False)

    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    recipe = relationship("Recipe", back_populates="lines")