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
    "Cutting",
    "Butchery",
    "Hot Kitchen",
    "Cold Kitchen",
    "Bakery/Pastry",
    "Packing",
    "Dispatch",
    "QC",
]

KITCHEN_SECTIONS = [
    "Cutting",
    "Butchery",
    "Hot Kitchen",
    "Cold Kitchen",
    "Bakery/Pastry",
    "Packing",
    "Dispatch",
]

EXCEL_SECTION_TO_SYSTEM = {
    "hot section": "Hot Kitchen",
    "cold section": "Cold Kitchen",
    "butchery section": "Butchery",
    "pastry section": "Bakery/Pastry",
    "bakery section": "Bakery/Pastry",
    "trayline section": "Trayline / Packing",
    "tray line section": "Trayline / Packing",
    "cutting section": "Cutting",
    "hot kitchen": "Hot Kitchen",
    "cold kitchen": "Cold Kitchen",
    "butchery": "Butchery",
    "bakery/pastry": "Bakery/Pastry",
    "trayline / packing": "Trayline / Packing",
    "cutting": "Cutting",
}

CUTTING_ITEM_CODE_PREFIX = "PRD1"
DEFAULT_ISSUE_SECTION = "Hot Kitchen"


def map_excel_section(raw: str | None) -> str | None:
    """Return the canonical system section for a sheet 'Section' value.

    Case/space tolerant. Returns None when the value is empty or unrecognised,
    so callers can decide their own fallback instead of silently defaulting.
    """
    key = (raw or "").strip().lower()
    if not key:
        return None
    return EXCEL_SECTION_TO_SYSTEM.get(key)


def resolve_issue_section(item_code: str | None, sheet_section: str | None,
                          fallback: str | None = None) -> str:
    
    if (item_code or "").upper().startswith(CUTTING_ITEM_CODE_PREFIX):
        return "Cutting"
    mapped = map_excel_section(sheet_section)
    if mapped:
        return mapped
    return fallback or DEFAULT_ISSUE_SECTION


MASS_UOMS = {"Kg", "g"}
VOLUME_UOMS = {"L", "mL"}
COUNT_UOMS = {"Each", "Pack", "Box", "Tray"}
ALL_UOMS = sorted(MASS_UOMS | VOLUME_UOMS | COUNT_UOMS)

DEFAULT_STATUS = "Active"