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
    # -----------------------------------------------------------------------
    # Batch 161 — THE SECOND SECTION MAP.
    #
    # Batch 159 fixed SECTION_MAP in scripts/import_frsh_master.py. It did not
    # fix this one, and this is the map that actually decides where the store
    # issues to: resolve_issue_section() is called at BOM generation time and
    # writes store_issuance_lines.issue_to_section.
    #
    # Two maps for one concept, in two files, is how "Pastry/Bakery section"
    # ended up correct on the recipe line and still wrong on the issuance line.
    # The three SMC spellings that were missing here are the same three that
    # were missing there:
    #
    #   Pastry/Bakery section   67 lines   -> was unmapped -> Hot Kitchen default
    #   Breakfast              374 lines   -> was unmapped -> Hot Kitchen default
    #   Buchery Section         14 lines   -> was unmapped -> Hot Kitchen default
    #
    # "Breakfast" happened to land on the right answer by accident, because the
    # default IS Hot Kitchen. The other two did not.
    # -----------------------------------------------------------------------
    "pastry/bakery section": "Bakery/Pastry",
    "bakery/pastry section": "Bakery/Pastry",
    "buchery section": "Butchery",          # typo in the SMC workbook
    "breakfast": "Hot Kitchen",
    "breakfast section": "Hot Kitchen",
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