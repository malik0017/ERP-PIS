from datetime import datetime
from sqlalchemy import Boolean, Column, Date, DateTime, Float, Integer, String, Text
from app.database.base import Base


class CustomerOrder(Base):
    __tablename__ = "customer_orders"

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(Integer, index=True, nullable=True)
    order_no = Column(String(80), unique=True, index=True, nullable=False)
    order_date = Column(Date, nullable=True)
    required_delivery_date = Column(Date, nullable=True)
    required_delivery_time = Column(String(20), nullable=True)
    cooking_date = Column(Date, nullable=True)
    cooking_time = Column(String(20), nullable=True)
    material_receiving_date = Column(Date, nullable=True)
    material_receiving_time = Column(String(20), nullable=True)
    customer_no = Column(String(80), nullable=True)
    customer_name = Column(String(255), nullable=False)
    brand = Column(String(100), nullable=True)
    channel = Column(String(100), nullable=True)
    kitchen = Column(String(100), nullable=True)
    order_type = Column(String(80), default="Corporate")
    priority = Column(String(30), default="Normal")
    created_by = Column(String(255), nullable=True)
    approved_by = Column(String(255), nullable=True)
    status = Column(String(50), default="Draft", index=True)
    notes = Column(Text, nullable=True)
    total_planned_portions = Column(Float, default=0)
    total_estimated_food_cost = Column(Float, default=0)
    total_estimated_selling_value = Column(Float, default=0)
    total_estimated_margin = Column(Float, default=0)
    # Batch 88: an explicit Sales review checkpoint on the order itself —
    # before it's even visible to the Head Chef, someone with sales
    # authority confirms it should proceed at all. Deliberately a NEW,
    # separate field rather than repurposing `status` — the existing
    # status machine (Submitted/Head Chef Approved/BOM Generated/...) is
    # read by many other pages, and overloading it here would risk
    # breaking those. New orders default to Pending (the gate applies);
    # orders that already existed before this batch are backfilled to
    # Approved at the database level when the column is first added (see
    # the ALTER TABLE in production/routes.py), so nothing already in
    # flight gets newly blocked.
    sales_review_status = Column(String(20), default="Pending", nullable=True)
    sales_reviewed_by = Column(String(255), nullable=True)
    sales_reviewed_at = Column(DateTime, nullable=True)
    sales_review_reason = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class OrderLine(Base):
    __tablename__ = "order_lines"

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(Integer, index=True, nullable=True)
    order_no = Column(String(80), index=True, nullable=False)
    line_no = Column(Integer, default=1)
    recipe_no = Column(String(50), index=True, nullable=False)
    recipe_name = Column(String(255), nullable=False)
    required_portions = Column(Float, default=0)
    planned_batches = Column(Float, default=0)
    portion_size_g = Column(Float, default=0)
    selling_price_per_portion = Column(Float, default=0)
    customer_notes = Column(Text, nullable=True)
    status = Column(String(50), default="Open", index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class BOMLine(Base):
    __tablename__ = "bom_lines"

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(Integer, index=True, nullable=True)
    order_no = Column(String(80), index=True, nullable=False)
    order_line_id = Column(Integer, nullable=True)
    recipe_no = Column(String(50), index=True, nullable=True)
    recipe_name = Column(String(255), nullable=True)
    ingredient_code = Column(String(50), index=True, nullable=False)
    ingredient_name = Column(String(255), nullable=False)
    ingredient_category = Column(String(100), nullable=True)
    ingredient_main_category = Column(String(150), nullable=True)
    ingredient_sub_category = Column(String(150), nullable=True)
    original_recipe_qty = Column(Float, default=0)
    recipe_uom = Column(String(20), default="Kg")
    required_qty_recipe_uom = Column(Float, default=0)
    standard_uom = Column(String(20), default="Kg")
    required_qty_standard = Column(Float, default=0)
    wastage_pct = Column(Float, default=0)
    expected_waste_qty_standard = Column(Float, default=0)
    total_required_with_waste_standard = Column(Float, default=0)
    unit_cost_standard = Column(Float, default=0)
    estimated_cost = Column(Float, default=0)
    default_issue_section = Column(String(80), default="Hot Kitchen")
    route_template = Column(Text, nullable=True)
    bom_status = Column(String(50), default="Generated", index=True)
    approved_by_head_chef = Column(Boolean, default=False)
    approved_by = Column(String(255), nullable=True)
    approved_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class HeadChefPlan(Base):
    __tablename__ = "head_chef_plans"

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(Integer, index=True, nullable=True)
    order_no = Column(String(80), index=True, nullable=False)
    order_line_id = Column(Integer, nullable=True)
    recipe_no = Column(String(50), index=True, nullable=True)
    recipe_name = Column(String(255), nullable=True)
    planned_date = Column(Date, nullable=True)
    shift = Column(String(50), nullable=True)
    kitchen = Column(String(100), nullable=True)
    planned_section = Column(String(100), nullable=True)
    assigned_chef = Column(String(255), nullable=True)
    planned_portions = Column(Float, default=0)
    planned_batches = Column(Float, default=0)
    planned_start_time = Column(String(20), nullable=True)
    planned_end_time = Column(String(20), nullable=True)
    special_instructions = Column(Text, nullable=True)
    planning_status = Column(String(50), default="Pending", index=True)
    approved_by = Column(String(255), nullable=True)
    approved_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class StoreIssuanceLine(Base):
    __tablename__ = "store_issuance_lines"

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(Integer, index=True, nullable=True)
    order_no = Column(String(80), index=True, nullable=False)
    order_line_id = Column(Integer, nullable=True)
    bom_line_id = Column(Integer, index=True, nullable=True)
    recipe_no = Column(String(50), index=True, nullable=True)
    recipe_name = Column(String(255), nullable=True)
    ingredient_code = Column(String(50), index=True, nullable=False)
    ingredient_name = Column(String(255), nullable=False)
    ingredient_main_category = Column(String(150), nullable=True)
    ingredient_sub_category = Column(String(150), nullable=True)
    required_qty_standard = Column(Float, default=0)
    required_qty_with_waste_standard = Column(Float, default=0)
    standard_uom = Column(String(20), default="Kg")
    requested_qty = Column(Float, default=0)
    requested_uom = Column(String(20), default="Kg")
    input_material_issued = Column(Float, default=0)
    issued_uom = Column(String(20), default="Kg")
    issued_qty_standard = Column(Float, default=0)
    variance_qty_standard = Column(Float, default=0)
    variance_pct = Column(Float, default=0)
    issue_to_section = Column(String(100), default="Hot Kitchen", index=True)
    route_template = Column(Text, nullable=True)
    lot_no = Column(String(100), nullable=True)
    supplier_name = Column(String(255), nullable=True)
    expiry_date = Column(Date, nullable=True)
    available_stock_standard = Column(Float, default=0)
    store_remarks = Column(Text, nullable=True)
    issuance_status = Column(String(50), default="Pending", index=True)
    issued_by = Column(String(255), nullable=True)
    issued_at = Column(DateTime, nullable=True)
    finalized = Column(Boolean, default=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class KitchenSectionTransaction(Base):
    __tablename__ = "kitchen_section_transactions"

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(Integer, index=True, nullable=True)
    order_no = Column(String(80), index=True, nullable=False)
    order_line_id = Column(Integer, nullable=True)
    recipe_no = Column(String(50), index=True, nullable=True)
    recipe_name = Column(String(255), nullable=True)
    ingredient_code = Column(String(50), index=True, nullable=False)
    ingredient_name = Column(String(255), nullable=False)
    standard_uom = Column(String(20), default="Kg")
    from_section = Column(String(100), nullable=True)
    current_section = Column(String(100), index=True, nullable=False)
    to_section = Column(String(100), nullable=True)
    route_step_no = Column(Integer, default=0)
    route_template = Column(Text, nullable=True)
    issued_qty_standard = Column(Float, default=0)
    received_qty_standard = Column(Float, default=0)
    processed_qty_standard = Column(Float, default=0)
    waste_qty_standard = Column(Float, default=0)
    returned_qty_standard = Column(Float, default=0)
    transferred_qty_standard = Column(Float, default=0)
    balance_qty_standard = Column(Float, default=0)
    received_by = Column(String(255), nullable=True)
    processed_by = Column(String(255), nullable=True)
    transferred_by = Column(String(255), nullable=True)
    received_at = Column(DateTime, nullable=True)
    processed_at = Column(DateTime, nullable=True)
    transferred_at = Column(DateTime, nullable=True)
    transaction_status = Column(String(50), default="Pending Receive", index=True)
    waste_reason = Column(String(255), nullable=True)
    section_remarks = Column(Text, nullable=True)
    qc_hold = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class QCCheck(Base):
    __tablename__ = "qc_checks"

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(Integer, index=True, nullable=True)
    qc_no = Column(String(80), unique=True, index=True, nullable=False)
    order_no = Column(String(80), index=True, nullable=True)
    batch_no = Column(String(80), nullable=True)
    recipe_no = Column(String(50), nullable=True)
    recipe_name = Column(String(255), nullable=True)
    section = Column(String(100), nullable=True)
    check_type = Column(String(80), default="In Process")
    temperature_c = Column(Float, nullable=True)
    appearance_score = Column(Float, default=0)
    taste_score = Column(Float, default=0)
    portion_weight_score = Column(Float, default=0)
    packaging_score = Column(Float, default=0)
    hygiene_score = Column(Float, default=0)
    overall_score = Column(Float, default=0)
    qc_status = Column(String(50), default="Pending", index=True)
    checked_by = Column(String(255), nullable=True)
    checked_at = Column(DateTime, nullable=True)
    issue_found = Column(Text, nullable=True)
    corrective_action = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class PackingDispatch(Base):
    __tablename__ = "packing_dispatch"

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(Integer, index=True, nullable=True)
    dispatch_no = Column(String(80), unique=True, index=True, nullable=False)
    order_no = Column(String(80), index=True, nullable=False)
    customer_name = Column(String(255), nullable=True)
    packed_portions = Column(Float, default=0)
    rejected_portions = Column(Float, default=0)
    packed_bags = Column(Integer, nullable=True)  # Batch 121: physical bag/tray count
    region = Column(String(50), nullable=True)  # Batch 129: delivery region (Riyadh/Eastern/Jeddah/Makkah)
    # Batch 152a: region-wise bag allocation, e.g. {"Riyadh": 10, "Dammam": 8}.
    # Stored as a JSON string; the total should reconcile to packed_bags. Added
    # via an import-time schema guard (raw TEXT column, no ORM lag).
    region_bags = Column(Text, nullable=True)
    dispatch_date = Column(Date, nullable=True)
    vehicle_no = Column(String(80), nullable=True)
    driver_name = Column(String(255), nullable=True)
    delivery_temperature_c = Column(Float, nullable=True)
    dispatch_status = Column(String(50), default="Pending", index=True)
    remarks = Column(Text, nullable=True)
    # Batch 80: proof-of-delivery — added defensively via ALTER TABLE in
    # dispatch/routes.py::_ensure_delivery_confirmation_schema() on a fresh
    # DB copy that predates this column; declared here too so the ORM
    # actually exposes them as row attributes in templates/routes.
    delivery_otp = Column(String(10), nullable=True)
    delivery_otp_generated_at = Column(DateTime, nullable=True)
    delivery_confirmed_by = Column(String(20), nullable=True)
    pod_photo_path = Column(String(300), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)