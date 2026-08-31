# app/core/rbac.py — Batch 10


from fastapi import Request

# ===== MODULE & PAGE DEFINITIONS =====

PAGE_AREAS = {
    # Core modules
    "module_home", "dashboard", "relationship", "reports", "masters", "recipes",
    "orders", "head_chef", "bom", "store", "kitchen", "qc", "packing", "dispatch",
    "logistics",
    "settings", "users", "audit", "procurement", "inventory_valuation", "finance",
    "customer_portal", "hr", "subscriptions",

    # Granular pages
    "master_upload", "master_data", "recipe_list", "recipe_prepare", "recipe_missing",
    "recipe_approvals", "master_approvals", "order_portal", "immediate_order", "production_orders", "store_issuance",
    "kitchen_summary",


    "sales_review", "purchase_requisition",

    # Kitchen sections
    "section_cutting", "section_butchery",
    "section_hot_kitchen", "section_cold_kitchen", "section_bakery_pastry",

    # Project Management
    "project_management", "project_list", "project_detail", "project_team",
    "project_timeline",
}

AREA_PARENTS = {
    "master_upload": "masters",
    "master_data": "masters",
    "master_approvals": "masters",
    "recipe_list": "recipes",
    "recipe_prepare": "recipes",
    "recipe_missing": "recipes",
    "recipe_approvals": "recipes",
    "order_portal": "orders",
    "production_orders": "orders",
    "store_issuance": "store",
    "kitchen_summary": "kitchen",
    "section_cutting": "kitchen",
    "section_butchery": "kitchen",
    "section_hot_kitchen": "kitchen",
    "section_cold_kitchen": "kitchen",
    "section_bakery_pastry": "kitchen",
    "procurement": "orders",
    "sales_review": "orders",
    "purchase_requisition": "procurement",
    "inventory_valuation": "masters",
    "project_list": "project_management",
    "project_detail": "project_management",
    "project_team": "project_management",
    "project_timeline": "project_management",
}

PARENT_CHILDREN: dict[str, set] = {}
for child, parent in AREA_PARENTS.items():
    PARENT_CHILDREN.setdefault(parent, set()).add(child)

# Admin and privileged roles — these ALWAYS see every module.
ADMIN_ROLES = {"SUPER_ADMIN", "SUPERADMIN", "ADMIN", "ADMINISTRATOR"}
MANAGER_ROLES = {"MANAGER", "SUPERVISOR", "HEAD_CHEF", "HEAD_CHEF_PLANNING"}

# ===== DEFAULT ROLE PERMISSIONS (FALLBACK ONLY) =====
ROLE_PERMISSIONS = {
    "SUPER_ADMIN": lambda area: True,

    "ADMIN": {"all": True},

    "MANAGER": {
        "dashboard": True, "orders": True, "recipes": True, "kitchen": True,
        "qc": True, "dispatch": True, "logistics": True, "procurement": True,
        "inventory_valuation": True, "project_management": True, "reports": True,
        "subscriptions": True,
        "sales_review": True, "purchase_requisition": True,
    },

    "SUPERVISOR": {
        "dashboard": True, "kitchen": True, "qc": True, "dispatch": True, "logistics": True,
        "project_management": True, "reports": True,
    },

    "HEAD_CHEF": {
        "dashboard": True, "head_chef": True, "kitchen": True,
        "bom": True, "recipes": True, "subscriptions": True,
    },

    "HEAD_CHEF_PLANNING": {
        "dashboard": True, "production_orders": True, "head_chef": True, "bom": True,
    },

    "DEFAULT": {
        "dashboard": True, "reports": True,
    },


    "CUSTOMER": {
        "customer_portal": True,
    },
}


# ===== SESSION MATRIX HELPERS (Batch 10) =====

def _session_access_set(request: Request) -> set | None:
    """Parse the compact `user_access` string saved at login.

    Format: "area1|area2|area3". Returns None if no matrix was ever saved
    for the user (fresh user -> fall back to role defaults). Returns an empty
    set when a matrix exists but grants nothing (explicit deny-all).
    """
    try:
        raw = request.session.get("user_access")
    except Exception:
        return None
    if raw is None:
        return None
    if isinstance(raw, str):
        raw = raw.strip()
        if raw == "":
            return None
        return {p for p in raw.split("|") if p}
    if isinstance(raw, (list, set, tuple)):
        return set(raw)
    if isinstance(raw, dict):
        return {k for k, v in raw.items() if v}
    return None


def _session_actions_map(request: Request) -> dict | None:
    """Parse the compact `user_actions` string saved at login.

    Format: "area:view,add,edit;area2:view". Returns None if absent.
    """
    try:
        raw = request.session.get("user_actions")
    except Exception:
        return None
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or raw.strip() == "":
        return None
    out: dict[str, set] = {}
    for part in raw.split(";"):
        part = part.strip()
        if not part or ":" not in part:
            continue
        key, acts = part.split(":", 1)
        out[key.strip()] = {a.strip() for a in acts.split(",") if a.strip()}
    return out or None


# ===== UTILITY FUNCTIONS =====

def normalized_role(request: Request) -> str:
    """Normalize role name to uppercase with underscores."""
    try:
        role = str(request.session.get("user_role") or "").upper().replace(" ", "_").strip()
    except Exception:
        role = ""
    return role if role else "DEFAULT"


def is_admin(request: Request) -> bool:
    return normalized_role(request) in ADMIN_ROLES


def can_access(request: Request, area: str) -> bool:
   
    # 0. Company-level module gate (fail-open on infra errors).
    try:
        from app.core.module_visibility import area_enabled
        if not area_enabled(request, area):
            return False
    except Exception:
        pass

    # 1. Admin / superadmin / administrator: everything among enabled modules.
    if is_admin(request):
        return True

    # 2. UI-managed per-user matrix (authoritative when present).
    matrix = _session_access_set(request)
    if matrix is not None:
        if area in matrix:
            return True
        parent = AREA_PARENTS.get(area)
        if parent and parent in matrix:
            return True
        return False

    # 3. Role defaults (fresh users without a saved matrix).
    role = normalized_role(request)
    perms = ROLE_PERMISSIONS.get(role, ROLE_PERMISSIONS.get("DEFAULT", {}))
    if callable(perms):
        return bool(perms(area))
    if perms.get("all"):
        return True
    if area in perms:
        return bool(perms[area])

    # 4. Parent-area inheritance on role defaults.
    parent = AREA_PARENTS.get(area)
    if parent and parent in perms:
        return bool(perms[parent])

    return False


def current_access(request: Request) -> dict:
    """Get all areas the current user can access."""
    if is_admin(request):
        return {a: True for a in PAGE_AREAS}
    return {a: can_access(request, a) for a in PAGE_AREAS}


def can_action(request: Request, area: str, action: str) -> bool:
    """
    Check if user can perform a specific action in an area.
    Actions: view, add, edit, delete, export.
    """
    if not can_access(request, area):
        return False

    if is_admin(request):
        return True

    # UI action matrix (authoritative when present).
    actions = _session_actions_map(request)
    if actions is not None:
        entry = actions.get(area)
        if entry is None:
            parent = AREA_PARENTS.get(area)
            if parent:
                entry = actions.get(parent)
        if entry is not None:
            if isinstance(entry, dict):
                return bool(entry.get(action))
            return action in entry
        # Area granted via `user_access` but no action row: allow view only.
        return action == "view"

    # Role-default fallback.
    if action == "view":
        return True
    if action in ("add", "edit", "delete", "export"):
        return normalized_role(request) in MANAGER_ROLES
    return False


# ===== REQUEST GUARDS =====

def require_area(request: Request, area: str) -> None:
    """Require user to have access to an area."""
    if not can_access(request, area):
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail=f"Access to {area} denied")


def require_action(request: Request, area: str, action: str) -> None:
    """Require user to have permission for a specific action."""
    if not can_action(request, area, action):
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail=f"Action {action} in {area} denied")
