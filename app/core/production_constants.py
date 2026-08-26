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

# Batch 20: Thawing and Marination are retired as standalone stations.
# Both activities are handled INSIDE Butchery. Do not re-add them here.
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

# ---------------------------------------------------------------------------
# Batch 131 — Store-issuance section routing.
#
# The FRSH / SMC recipe master (Recipe Ingredients sheet) carries a "Section"
# column per ingredient line: "Hot Section", "Cold Section", "Butchery Section",
# "Pastry Section", "TrayLine Section". Those are the KITCHEN sections the store
# must issue each ingredient to. The system's canonical section names differ
# slightly (see SECTIONS above), so we map the sheet vocabulary onto ours ONCE,
# here, and reuse it everywhere (import → BOM → store issuance).
#
# Client routing rule (from Malik, confirmed against the FRSH workbook):
#   1. If the ingredient Item Code starts with "PRD1" (fresh produce) it is
#      washed / cut first, so it ALWAYS issues to "Cutting" — regardless of the
#      Section written in the sheet.
#   2. Otherwise the ingredient issues to the section named in the sheet's
#      "Section" column, mapped through EXCEL_SECTION_TO_SYSTEM below.
#   3. If neither is available, fall back to "Hot Kitchen" (the historic
#      default) so nothing is ever left without a destination.
# ---------------------------------------------------------------------------
EXCEL_SECTION_TO_SYSTEM = {
    "hot section": "Hot Kitchen",
    "cold section": "Cold Kitchen",
    "butchery section": "Butchery",
    "pastry section": "Bakery/Pastry",
    "bakery section": "Bakery/Pastry",
    "trayline section": "Trayline / Packing",
    "tray line section": "Trayline / Packing",
    "cutting section": "Cutting",
    # Allow the canonical names to pass straight through as well, so a sheet that
    # already uses system vocabulary still maps cleanly.
    "hot kitchen": "Hot Kitchen",
    "cold kitchen": "Cold Kitchen",
    "butchery": "Butchery",
    "bakery/pastry": "Bakery/Pastry",
    "trayline / packing": "Trayline / Packing",
    "cutting": "Cutting",
}

# Item-code prefix that forces routing to Cutting (fresh produce).
CUTTING_ITEM_CODE_PREFIX = "PRD1"

# Historic default when a line has neither a recognised section nor a PRD1 code.
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
    """Central store-issuance routing decision (see rule block above).

    PRD1-* → Cutting; else mapped sheet section; else the caller's fallback;
    else DEFAULT_ISSUE_SECTION. This is the ONE place the rule lives — the BOM
    generator, the store-issuance generator and any re-sync all call it, so the
    routing can never drift between them.
    """
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