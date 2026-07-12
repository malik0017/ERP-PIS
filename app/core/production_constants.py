# app/core/production_constants.py
BRANDS = [
    "Diet World",
    "Gourmet 360",
    "Mishwi Kiwi",
    "Madghout Al Sidrah",
    "Sugar Batch",
]

GOURMET_CHANNELS = [
    "Corporate Customers",
    "Catering Events",
    "Cafeterias",
    "Cloud Kitchens",
]

KITCHENS = [
    "Central Kitchen - Ishbiliyah",
    "Nasriyah Kitchen",
]

ORDER_STATUSES = [
    "Draft",
    "Submitted",
    "BOM Generated",
    "Planning Pending",
    "Planning Approved",
    "Store Pending",
    "Store Issued",
    "In Production",
    "QC Pending",
    "Packed",
    "Dispatched",
    "Closed",
    "Cancelled",
]

SECTIONS = [
    "Store",
    "Thawing",
    "Cutting",
    "Butchery",
    "Marination",
    "Hot Kitchen",
    "Cold Kitchen",
    "Bakery/Pastry",
    "Packing",
    "Dispatch",
    "QC",
]

KITCHEN_SECTIONS = [
    "Thawing",
    "Cutting",
    "Butchery",
    "Marination",
    "Hot Kitchen",
    "Cold Kitchen",
    "Bakery/Pastry",
    "Packing",
    "Dispatch",
]

MASS_UOMS = {"Kg", "g"}
VOLUME_UOMS = {"L", "mL"}
COUNT_UOMS = {"Each", "Pack", "Box", "Tray"}
ALL_UOMS = sorted(MASS_UOMS | VOLUME_UOMS | COUNT_UOMS)

DEFAULT_STATUS = "Active"