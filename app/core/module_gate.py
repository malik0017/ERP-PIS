# app/core/module_gate.py
# =============================================================================
# Batch 241 — MODULE SECURITY GATE (central enforcement middleware)
# -----------------------------------------------------------------------------
# THE BUG THIS FIXES
#   Switching a module OFF in Settings → Module Visibility only hid it from the
#   sidebar/launcher. The pages themselves stayed reachable, because enforcement
#   depended on each route remembering to call rbac.require_area(...) — and many
#   routes never did (some route files have no guard at all).
#
# THE FIX
#   One middleware that maps the REQUEST PATH to a sellable module and returns
#   403 the moment that module is disabled for the company — for every user,
#   admins included, on every page, API and export under that module. Routes
#   that already call require_area keep working; this is the belt-and-suspenders
#   layer that guarantees a disabled module is genuinely dark.
#
#   Enforcement order in the stack (see app/main.py):
#       CORS → Session → CSRF → Auth → ModuleGate → route
#   Auth has already redirected anonymous users, so the gate only ever runs for
#   a logged-in session and can trust request.session.
#
# WIRING (app/main.py, immediately ABOVE `app.add_middleware(AuthMiddleware)`):
#       from app.core.module_gate import ModuleGateMiddleware
#       app.add_middleware(ModuleGateMiddleware)
#   (Adding it before Auth in source = it runs AFTER Auth at request time,
#    i.e. closest to the route — which is what we want.)
#
# FAIL-OPEN: any infrastructure error while reading the module map lets the
# request through. A gate that 500s the whole app is worse than one that misses
# a check during a DB blip; the per-route require_area calls still back it up.
# =============================================================================

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from app.core.templates import render
from app.core.module_visibility import module_enabled


# --- Never gated: infrastructure, auth, and always-on areas -----------------
# "users" (Users & Access) is locked ON in the catalog, so everything it owns
# (/settings, /admin, /users, /system, audit) is listed here and skipped.
_ALLOW_EXACT = {
    "", "/", "/login", "/logout", "/register",
    "/health", "/docs", "/redoc", "/openapi.json", "/favicon.ico",
    "/modules", "/module",           # launcher already renders enabled cards only
}

_ALLOW_PREFIX = (
    "/static", "/api",               # assets + JSON APIs (module pages gate their own UI)
    "/settings", "/system", "/admin",  # Users & Access (always on)
    "/users", "/notifications", "/search",
    "/print", "/export",             # shared render/export endpoints
    "/setup", "/set-language", "/i18n", "/lang", "/theme",
    "/modules",
)

# --- Ordered path → module-key rules (first match wins) ---------------------
# More specific prefixes MUST come before the general ones they sit under
# (e.g. /dispatch/logistics before /dispatch, /my/subscriptions before /my).
_RULES = [
    ("/dashboard",             "command_center"),
    ("/dispatch/logistics",    "logistics"),

    # Production Intelligence (orders → kitchen → qc → packing → dispatch)
    ("/production/orders",     "production"),
    ("/production/section",    "production"),
    ("/production/store",      "production"),
    ("/production/head-chef",  "production"),
    ("/production/kitchen",    "production"),
    ("/production/boq",        "production"),
    ("/production/bakery",     "production"),
    ("/production/tx",         "production"),
    ("/production/top",        "production"),
    ("/orders",                "production"),
    ("/qc",                    "production"),
    ("/packing",               "production"),
    ("/dispatch",              "production"),

    ("/inventory",             "inventory"),

    ("/procurement",           "procurement"),
    ("/purchase-requisitions", "procurement"),
    ("/requisitions",          "procurement"),

    ("/recipes",               "recipes"),
    ("/masters",               "masters"),
    ("/reports",               "reports"),
    ("/projects",              "projects"),
    ("/finance",               "finance"),
    ("/hr",                    "hcm"),

    ("/customer",              "customer_portal"),
    ("/my/subscriptions",      "subscriptions"),
    ("/my",                    "customer_portal"),
    ("/subscriptions",         "subscriptions"),

    ("/sales-requests",        "sales"),
    ("/sales-review",          "sales"),
]


def _match(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix + "/") or path.startswith(prefix + "?")


def resolve_module(path: str):
    """Return the sellable-module key that owns `path`, or None if it is not
    gated by module visibility."""
    # The Command Center home lives at the bare /production route; everything
    # deeper under /production belongs to Production Intelligence.
    if path == "/production" or path.startswith("/production?"):
        return "command_center"
    for prefix, key in _RULES:
        if _match(path, prefix):
            return key
    return None


def _is_allowed(path: str) -> bool:
    if path in _ALLOW_EXACT:
        return True
    for p in _ALLOW_PREFIX:
        if _match(path, p):
            return True
    return False


class ModuleGateMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        path = request.url.path

        if _is_allowed(path):
            return await call_next(request)

        # Only enforce for authenticated sessions. Auth middleware has already
        # bounced anonymous users to /login; this guard just avoids touching the
        # session store on the odd unauthenticated path that slips through.
        try:
            if not request.session.get("user_id"):
                return await call_next(request)
        except Exception:
            return await call_next(request)

        key = resolve_module(path)
        if key and key != "users":
            try:
                allowed = module_enabled(request, key)
            except Exception:
                allowed = True  # fail-open on infra error
            if not allowed:
                detail = "This module is turned off for your company."
                accept = request.headers.get("accept", "")
                wants_json = "application/json" in accept and "text/html" not in accept
                if wants_json:
                    return JSONResponse(
                        status_code=403,
                        content={"detail": f"Module '{key}' is disabled", "module": key},
                    )
                try:
                    return render(request, "errors/403.html",
                                  {"detail": detail}, status_code=403)
                except Exception:
                    return JSONResponse(status_code=403,
                                        content={"detail": detail, "module": key})

        return await call_next(request)
