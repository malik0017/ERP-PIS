# app/core/rbac.py — Batch 10
"""
Role-Based Access Control (RBAC) and Authorization

Batch 10 change (IMPORTANT):
The per-user access matrix managed from the UI
(Users & Access -> /admin/users/{id}/access, stored in `user_page_access`
and loaded into the session at login as `user_access` / `user_actions`)
is now the PRIMARY authority for access decisions.

Decision order:
  1. ADMIN / SUPER_ADMIN / ADMINISTRATOR  -> always full access (all modules,
     including Customer Portal and every dashboard).
  2. Session UI matrix (`user_access`)    -> if the admin has saved a matrix
     for this user, it is authoritative. No code change is ever needed to
     grant or revoke a module: tick/untick in the UI and the user re-logins.
  3. Role defaults (ROLE_PERMISSIONS)     -> fallback for users that have no
     saved matrix yet (fresh users), so the system remains usable.
  4. Parent-area inheritance              -> unchanged.

This file stays framework-light: it reads only request.session, so it is safe
in templates (sidebar/menus) and in route guards.
"""

from fastapi import Request

# ===== MODULE & PAGE DEFINITIONS =====

PAGE_AREAS = {
    # Core modules
    "module_home", "dashboard", "relationship", "reports", "masters", "recipes",
    "orders", "head_chef", "bom", "store", "kitchen", "qc", "packing", "dispatch",
    "settings", "users", "audit", "procurement", "inventory_valuation", "finance",
    "customer_portal", "hr", "subscriptions",

    # Granular pages
    "master_upload", "master_data", "recipe_list", "recipe_prepare", "recipe_missing",
    "recipe_approvals", "order_portal", "production_orders", "store_issuance",
    "kitchen_summary",

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
        "qc": True, "dispatch": True, "procurement": True,
        "inventory_valuation": True, "project_management": True, "reports": True,
        "subscriptions": True,
    },

    "SUPERVISOR": {
        "dashboard": True, "kitchen": True, "qc": True, "dispatch": True,
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
            # Distinguish "never saved" from "saved empty": auth stores the key
            # only after loading the DB; an empty string means no rows -> treat
            # as "no matrix" so role defaults still apply for fresh users.
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
    """
    Check if user can access a module/page area.

    Batch 65: a COMPANY-level module-visibility gate now runs FIRST. If the
    company has switched a module OFF (it hasn't bought/enabled it yet), the
    area is hidden for everyone — including admins — because the module simply
    isn't part of the product for that company. RBAC then decides per-user
    access among the modules that ARE enabled.

    Order:
      0. Module visibility (company bought/enabled the module?)   [Batch 65]
      1. Admin / superadmin / administrator -> full access to enabled modules
      2. UI matrix (session)  ->  3. role defaults  ->  4. parent area
    """
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
