from datetime import date
from typing import List, Optional
from pydantic import BaseModel, Field


class OrderLineIn(BaseModel):
    recipe_no: str
    recipe_name: Optional[str] = None
    required_portions: float = Field(gt=0)
    selling_price_per_portion: float = 0
    customer_notes: Optional[str] = None


class CustomerOrderCreate(BaseModel):
    order_no: Optional[str] = None
    order_date: Optional[date] = None
    required_delivery_date: Optional[date] = None
    required_delivery_time: Optional[str] = None
    cooking_date: Optional[date] = None
    cooking_time: Optional[str] = None
    material_receiving_date: Optional[date] = None
    material_receiving_time: Optional[str] = None
    customer_no: Optional[str] = None
    customer_name: str
    brand: Optional[str] = None
    channel: Optional[str] = None
    kitchen: Optional[str] = None
    order_type: str = "Corporate"
    priority: str = "Normal"
    notes: Optional[str] = None
    lines: List[OrderLineIn]


class StoreIssueUpdate(BaseModel):
    input_material_issued: float
    issued_uom: str
    issue_to_section: str
    lot_no: Optional[str] = None
    supplier_name: Optional[str] = None
    store_remarks: Optional[str] = None