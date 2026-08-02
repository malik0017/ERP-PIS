# app/main.py
"""
FastAPI Application Entry Point
Production-ready architecture with session management and authentication
"""

from contextlib import asynccontextmanager
from pathlib import Path
import logging

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import APP_NAME, COMPANY_NAME, SECRET_KEY, DEBUG
from app.core.templates import render
from app.modules.auth.routes import router as auth_router
from app.modules.auth.routes_register import router as self_register_router  # Batch 71
from app.modules.dashboard.routes import router as dashboard_router
from app.modules.production.routes import router as production_router
from app.modules.recipes.routes import router as recipes_router
from app.modules.inventory.routes import router as inventory_router
from app.modules.masters.routes import router as masters_router
from app.modules.orders.routes import router as orders_router
from app.modules.qc.routes import router as qc_router
from app.modules.production.routes_docs import router as prod_docs_router  # Batch 70
from app.modules.production.routes_kitchen import router as kitchen_prod_router  # Batch 72
from app.modules.dispatch.routes import router as dispatch_router
from app.modules.packing.routes import router as packing_router
from app.modules.settings.routes import router as settings_router
from app.modules.settings.routes_modules import router as settings_modules_router  # Batch 65
from app.modules.reports.routes import router as reports_router
from app.modules.reports.routes_tree import router as reports_tree_router  # Batch 68
from app.modules.reports.routes_workflow import router as reports_workflow_router  # Batch 66
from app.modules.reports.routes_schedule import router as report_schedule_router  # Batch 74
from app.modules.users.routes import router as users_router
from app.modules.notifications.routes import router as notifications_router
from app.modules.search.routes import router as search_router
from app.modules.customer.routes import router as customer_router
from app.modules.procurement.routes import router as procurement_router
from app.modules.finance.routes import router as finance_router
from app.modules.projects.routes import router as projects_router
from app.modules.hr.routes import router as hr_router
from app.modules.hr.routes_payroll import router as hr_payroll_router  # Batch 74
from app.modules.subscriptions.routes import router as subscriptions_router  # Batch 76
from app.modules.subscriptions.routes_portal import router as subscriptions_portal_router  # Batch 76
from app.modules.printforms.routes import router as printforms_router  # Batch 77: was built, never wired in
# Batch 10: config-driven per-module dashboards (/module/{key}/dashboard)
from app.modules.module_dash.routes import router as module_dash_router

# Batch 22: new & extended routers (manual masters CRUD, finance extensions,
# PO print). These extend existing namespaces; base routers stay untouched.
from app.modules.masters_crud.routes import router as masters_crud_router
from app.modules.finance.routes_ext import router as finance_ext_router
from app.modules.finance.routes_statements import router as finance_statements_router  # Batch 67
from app.modules.finance.routes_periods import router as finance_periods_router  # Batch 73
from app.modules.procurement.routes_print import router as procurement_print_router
from app.modules.module_dash.routes_launcher import build_launcher_context


# ===== LOGGING =====
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


# ===== PATHS =====
BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"


# ===== MIDDLEWARE: Authentication Check =====
class AuthMiddleware(BaseHTTPMiddleware):
    """
    Middleware to enforce login for protected routes.
    Allows public routes without authentication.
    """

    PUBLIC_PATHS = {
        "/",
        "/login",
        "/register",
        "/api",
        "/api/auth/login",
        "/api/auth/register",
        "/health",
        "/docs",
        "/redoc",
        "/openapi.json",
        "/favicon.ico",
    }

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        if path in self.PUBLIC_PATHS or path.startswith("/static"):
            return await call_next(request)

        try:
            user_id = request.session.get("user_id")
        except Exception as e:
            logger.warning(f"Session access error: {e}")
            return RedirectResponse(url="/login", status_code=302)

        if not user_id:
            return RedirectResponse(url="/login", status_code=302)

        request.state.user_id = user_id
        request.state.username = request.session.get("username")
        request.state.user_role = request.session.get("user_role")

        return await call_next(request)


# ===== LIFESPAN CONTEXT =====
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(f"Starting {APP_NAME}...")
    logger.info(f"Company: {COMPANY_NAME}")
    yield
    logger.info(f"Shutting down {APP_NAME}...")


# ===== INITIALIZE FASTAPI =====
# Batch 77: /docs, /redoc and the raw OpenAPI schema handed the full route
# map (paths + parameter names) to anyone, logged in or not. RBAC still
# protects each route individually, but there's no reason to publish the
# blueprint. Only expose them when DEBUG=True (local development).
app = FastAPI(
    title=APP_NAME,
    description=f"{APP_NAME} - Production & Inventory Management System",
    version="1.0.0",
    docs_url="/docs" if DEBUG else None,
    redoc_url="/redoc" if DEBUG else None,
    openapi_url="/openapi.json" if DEBUG else None,
    lifespan=lifespan,
)


# ===== MIDDLEWARE STACK =====
# Last added middleware runs first.
# Required request flow:
# CORS -> Session -> Auth -> Route

app.add_middleware(AuthMiddleware)

app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    session_cookie="isfc_session",
    max_age=86400,
    same_site="lax",
    https_only=False,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost",
        "http://localhost:8000",
        "http://127.0.0.1",
        "http://127.0.0.1:8000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ===== MOUNT STATIC FILES =====
try:
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    logger.info(f"Static files mounted from: {STATIC_DIR}")
except Exception as e:
    logger.warning(f"Warning - Could not mount static files: {e}")


# ===== REGISTER ROUTERS =====
app.include_router(auth_router)
# Batch 71: public self-service registration (form POST /register)
app.include_router(self_register_router)
# Recipes router is intentionally registered before dashboard.
# Older versions had a placeholder /recipes route in dashboard, which shadowed
# the real recipe list and made the screen show zero records.
app.include_router(recipes_router)
app.include_router(dashboard_router)
app.include_router(production_router)
# Batch 72: kitchen production state machine (receive-all → produce → transfer)
app.include_router(kitchen_prod_router)
app.include_router(inventory_router)
app.include_router(masters_router)
app.include_router(orders_router)
app.include_router(qc_router)
app.include_router(packing_router)
app.include_router(dispatch_router)

# Batch 70: printable documents (QC certificate, delivery note)
app.include_router(prod_docs_router)
app.include_router(settings_router)
# Batch 68: SAP-style relationship tree (registered before reports_router so
# its /reports/relationship-tree page + /reports/api/tree-node endpoint win).
app.include_router(reports_tree_router)
app.include_router(reports_router)
# Batch 74: scheduled report exports
app.include_router(report_schedule_router)
app.include_router(users_router)
app.include_router(notifications_router)
app.include_router(search_router)
app.include_router(customer_router)
app.include_router(procurement_router)
app.include_router(finance_router)
app.include_router(projects_router)
app.include_router(hr_router)
# Batch 74: HCM payroll + leave + shifts
app.include_router(hr_payroll_router)
app.include_router(subscriptions_router)
app.include_router(subscriptions_portal_router)
# Batch 77: was fully built (all 6 print documents + templates) but never
# registered — wiring it in is the only change needed to make it reachable.
app.include_router(printforms_router)
app.include_router(module_dash_router)

# Batch 22: register new/extended routers.
app.include_router(masters_crud_router)
app.include_router(finance_ext_router)
app.include_router(procurement_print_router)

# Batch 65: module visibility admin (Settings ▸ Module Visibility)
app.include_router(settings_modules_router)

# Batch 66: ERP workflow / data-movement reference (Reports ▸ Data Flow)
app.include_router(reports_workflow_router)

# Batch 67: financial statements (P&L, Balance Sheet, Cash Flow, Aging)
app.include_router(finance_statements_router)
# Batch 73: finance period close + cost centers
app.include_router(finance_periods_router)

# ===== ROUTES =====

@app.get("/modules")
async def module_launcher(request: Request):
    """ERP Module Launcher.

    Only ADMIN / SUPER_ADMIN / MANAGER see the launcher overview page;
    everyone else is routed straight to the production dashboard so the
    launcher works as a management landing page (SAP-style cockpit).
    Each module card also shows a live KPI pulled with safe fallbacks.
    """
    # Batch 10: the launcher is now the landing page for EVERY logged-in
    # user (login redirect + logo both point here). Cards are filtered by
    # can_access() inside the template, so a user with a single module sees
    # one card. Admin/superadmin/administrator see every card.
    # Batch 22: live KPI tiles + real chart data (orders / inventory / AR-AP).
    ctx = {"stats": {}, "charts": {}}
    try:
        from app.database.session import SessionLocal
        _db = SessionLocal()
        try:
            ctx = build_launcher_context(_db)
        finally:
            _db.close()
    except Exception:
        ctx = {"stats": {}, "charts": {}}
    return render(request, "modules/index.html", {
        "page_title": "ERP Modules",
        "stats": ctx.get("stats", {}),
        "cards": ctx.get("cards", {}),
        "session_username": request.session.get("username"),
    })

@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "app": APP_NAME,
        "version": "1.0.0",
    }


@app.get("/")
async def root(request: Request):
    try:
        if request.session.get("user_id"):
            return RedirectResponse(url="/modules", status_code=302)
    except Exception:
        pass

    return RedirectResponse(url="/login", status_code=302)


@app.get("/login")
async def login_page(request: Request):
    try:
        if request.session.get("user_id"):
            return RedirectResponse(url="/modules", status_code=302)
    except Exception:
        pass

    return render(
        request,
        "auth/login.html",
        {"page_title": "Login - ISFC PIMS"},
    )


@app.get("/register")
async def register_page(request: Request):
    return render(
        request,
        "auth/register.html",
        {"page_title": "Register - ISFC PIMS"},
    )


@app.get("/api")
async def api_root():
    return {
        "message": f"Welcome to {APP_NAME} API",
        "version": "1.0.0",
        "docs": "/docs",
        "health": "/health",
        "endpoints": {
            "auth": "/api/auth/login",
            "register": "/api/auth/register",
            "production_orders": "/production/orders",
        },
    }


# ===== ERROR HANDLERS =====
@app.exception_handler(404)
async def not_found_handler(request: Request, exc):
    logger.warning(f"404 Error: {request.url.path}")
    try:
        return render(request, "errors/404.html", {}, status_code=404)
    except Exception:
        return JSONResponse(
            status_code=404,
            content={"detail": "Not found"},
        )


@app.exception_handler(500)
async def server_error_handler(request: Request, exc):
    logger.error(f"500 Server Error: {exc}")
    # Batch 77: the full exception string (which can include table/column
    # names or SQL fragments) used to render straight to the browser for
    # anyone who triggered a 500. Full detail still goes to the server log
    # above; the page itself only shows it when DEBUG=True.
    error_detail = str(exc) if DEBUG else "Something went wrong. The team has been notified."
    try:
        return render(
            request,
            "errors/500.html",
            {"error": error_detail},
            status_code=500,
        )
    except Exception as template_error:
        logger.error(f"Error rendering 500 template: {template_error}")
        return JSONResponse(
            status_code=500,
            content={"detail": "Internal server error"},
        )



@app.exception_handler(403)
async def forbidden_handler(request: Request, exc):
    detail = getattr(exc, "detail", "Access denied")
    logger.warning(f"403 Access denied: {request.url.path} - {detail}")
    accept = request.headers.get("accept", "")
    if "application/json" in accept and "text/html" not in accept:
        return JSONResponse(status_code=403, content={"detail": detail})
    try:
        return render(request, "errors/403.html", {"detail": detail}, status_code=403)
    except Exception:
        return JSONResponse(status_code=403, content={"detail": detail})

# ===== STARTUP EVENT =====
@app.on_event("startup")
async def startup_event():
    logger.info("Application startup complete")
    logger.info("API Documentation: http://localhost:8000/docs")


# ===== SHUTDOWN EVENT =====
@app.on_event("shutdown")
async def shutdown_event():
    logger.info("Application shutdown complete")


# ===== DEV SERVER =====
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="info",
    )

