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

from app.config import APP_NAME, COMPANY_NAME, SECRET_KEY
from app.core.templates import render
from app.modules.auth.routes import router as auth_router
from app.modules.dashboard.routes import router as dashboard_router
from app.modules.production.routes import router as production_router
from app.modules.recipes.routes import router as recipes_router
from app.modules.inventory.routes import router as inventory_router
from app.modules.masters.routes import router as masters_router
from app.modules.orders.routes import router as orders_router
from app.modules.qc.routes import router as qc_router
from app.modules.dispatch.routes import router as dispatch_router
from app.modules.packing.routes import router as packing_router
from app.modules.settings.routes import router as settings_router
from app.modules.reports.routes import router as reports_router
from app.modules.users.routes import router as users_router
from app.modules.notifications.routes import router as notifications_router
from app.modules.search.routes import router as search_router
from app.modules.customer.routes import router as customer_router
from app.modules.procurement.routes import router as procurement_router
from app.modules.finance.routes import router as finance_router
from app.modules.projects.routes import router as projects_router
# Batch 10: config-driven per-module dashboards (/module/{key}/dashboard)
from app.modules.module_dash.routes import router as module_dash_router


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
app = FastAPI(
    title=APP_NAME,
    description=f"{APP_NAME} - Production & Inventory Management System",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
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
# Recipes router is intentionally registered before dashboard.
# Older versions had a placeholder /recipes route in dashboard, which shadowed
# the real recipe list and made the screen show zero records.
app.include_router(recipes_router)
app.include_router(dashboard_router)
app.include_router(production_router)
app.include_router(inventory_router)
app.include_router(masters_router)
app.include_router(orders_router)
app.include_router(qc_router)
app.include_router(packing_router)
app.include_router(dispatch_router)
app.include_router(settings_router)
app.include_router(reports_router)
app.include_router(users_router)
app.include_router(notifications_router)
app.include_router(search_router)
app.include_router(customer_router)
app.include_router(procurement_router)
app.include_router(finance_router)
app.include_router(projects_router)
app.include_router(module_dash_router)

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
    stats = {}
    try:
        from sqlalchemy import text as _text
        from app.database.session import SessionLocal
        _db = SessionLocal()

        def _n(sql: str) -> int:
            try:
                return int(_db.execute(_text(sql)).scalar() or 0)
            except Exception:
                return 0
        stats = {
            "open_orders": _n("SELECT COUNT(*) FROM customer_orders WHERE COALESCE(status,'') NOT IN ('Delivered','Closed','Cancelled')"),
            "inventory_items": _n("SELECT COUNT(*) FROM ingredients"),
            "open_pos": _n("SELECT COUNT(*) FROM purchase_orders WHERE COALESCE(status,'') NOT IN ('Closed','Cancelled')"),
            "ar_open": _n("SELECT COUNT(*) FROM ar_invoices WHERE status <> 'Paid'"),
            "customers": _n("SELECT COUNT(*) FROM customers"),
        }
        _db.close()
    except Exception:
        stats = {}
    return render(request, "modules/index.html", {"page_title": "ERP Modules", "stats": stats})

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
    try:
        return render(
            request,
            "errors/500.html",
            {"error": str(exc)},
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

