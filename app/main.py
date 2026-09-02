# app/main.py
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
from app.modules.auth.routes_register import router as self_register_router 
from app.modules.dashboard.routes import router as dashboard_router
from app.modules.production.routes import router as production_router
from app.modules.recipes.routes import router as recipes_router
from app.modules.inventory.routes import router as inventory_router
from app.modules.masters.routes import router as masters_router
from app.modules.orders.routes import router as orders_router
from app.modules.sales_review.routes import router as sales_review_router
from app.modules.purchase_req.routes import router as purchase_req_router
from app.modules.qc.routes import router as qc_router
from app.modules.production.routes_docs import router as prod_docs_router  
from app.modules.production.routes_kitchen import router as kitchen_prod_router 
from app.modules.production.routes_topup import router as topup_router  
from app.modules.qc.routes_sampling import router as qc_sampling_router  
from app.modules.dispatch.routes import router as dispatch_router
from app.modules.packing.routes import router as packing_router
from app.modules.settings.routes import router as settings_router
from app.modules.settings.routes_modules import router as settings_modules_router  
from app.modules.reports.routes import router as reports_router
from app.modules.reports.routes_tree import router as reports_tree_router  
from app.modules.reports.routes_workflow import router as reports_workflow_router 
from app.modules.reports.routes_schedule import router as report_schedule_router 
from app.modules.users.routes import router as users_router
from app.modules.notifications.routes import router as notifications_router
from app.modules.search.routes import router as search_router
from app.modules.customer.routes import router as customer_router
from app.modules.procurement.routes import router as procurement_router
from app.modules.finance.routes import router as finance_router
from app.modules.projects.routes import router as projects_router
from app.modules.hr.routes import router as hr_router
from app.modules.hr.routes_payroll import router as hr_payroll_router 
from app.modules.subscriptions.routes import router as subscriptions_router 
from app.modules.subscriptions.routes_portal import router as subscriptions_portal_router  
from app.modules.printforms.routes import router as printforms_router
from app.modules.module_dash.routes import router as module_dash_router
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

        # Batch 155: session idle-expiry. If there's been no activity for longer
        # than IDLE_TIMEOUT_MINUTES, clear the session and force re-login. This is
        # in addition to the cookie max_age (absolute lifetime) — idle-expiry caps
        # how long an unattended, logged-in browser stays usable.
        import time as _time
        IDLE_TIMEOUT_MINUTES = 60
        now = _time.time()
        last = request.session.get("_last_activity")
        if last and (now - float(last)) > IDLE_TIMEOUT_MINUTES * 60:
            request.session.clear()
            return RedirectResponse(url="/login?expired=1", status_code=302)
        request.session["_last_activity"] = now

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


# Batch 155 — CSRF protection in MONITOR mode.
# Reads the _csrf form field / X-CSRF-Token header on state-changing requests and
# logs a warning when it doesn't match the session token, WITHOUT blocking. This
# lets the token be rolled into forms safely; once every POST form carries
# {{ csrf_form_field()|safe }}, flip CSRF_ENFORCE=True to reject mismatches.
CSRF_ENFORCE = False


class CSRFMiddleware(BaseHTTPMiddleware):
    SAFE = {"GET", "HEAD", "OPTIONS", "TRACE"}
    EXEMPT_PREFIXES = ("/login", "/api/", "/static", "/logout")

    async def dispatch(self, request: Request, call_next):
        if request.method in self.SAFE or any(request.url.path.startswith(p) for p in self.EXEMPT_PREFIXES):
            return await call_next(request)
        try:
            session_tok = request.session.get("_csrf_token")
        except Exception:
            session_tok = None
        if session_tok:
            sent = request.headers.get("x-csrf-token")
            # Monitor mode checks the header only; reading the form body here could
            # interfere with downstream handlers on some setups. The per-route
            # csrf_valid() helper validates the _csrf FORM field when enforcing.
            if sent is not None and sent != session_tok:
                logger.warning(f"CSRF header mismatch on {request.method} {request.url.path} (monitor mode)")
                if CSRF_ENFORCE:
                    from starlette.responses import PlainTextResponse
                    return PlainTextResponse("CSRF validation failed", status_code=403)
        return await call_next(request)


app.add_middleware(CSRFMiddleware)

app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    session_cookie="isfc_session",
    # Batch 155: absolute cookie lifetime capped at 12h (was 24h); combined with
    # the 60-min idle-expiry in AuthMiddleware, an unattended session can't linger.
    max_age=43200,
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
app.include_router(self_register_router)
# Batch 101: registered BEFORE recipes_router on purpose. That router has a
# catch-all /recipes/{recipe_id}, which matched "/recipes/template" first and
# turned the template download into a 401. FastAPI resolves in registration
# order, so the specific paths have to come before the parameterised one.
from app.modules.recipes.routes_excel import router as recipes_excel_router  # Batch 101
from app.modules.recipes.routes_bulk import router as recipes_bulk_router    # Batch 103
from app.modules.reports.routes_inventory import router as inv_reports_router  # Batch 103
from app.modules.masters.routes_bulk import router as masters_bulk_router      # Batch 104
from app.modules.production.routes_boq import router as boq_router             # Batch 105
from app.modules.admin.routes_audit import router as audit_viewer_router       # Batch 106
from app.modules.inventory.routes_reorder import router as reorder_router      # Batch 107
from app.modules.procurement.routes_match import router as match_router        # Batch 108
from app.modules.setup.routes_import import router as setup_import_router      # Batch 109
from app.modules.finance.routes_coa import router as coa_router                # Batch 110
from app.modules.settings.routes_approval import router as approval_router     # Batch 111
from app.modules.procurement.routes_landed import router as landed_router      # Batch 112
from app.modules.qc.routes_recall import router as recall_router               # Batch 112
from app.modules.reports.routes_builder import router as rbuilder_router       # Batch 113
from app.modules.finance.routes_budget import router as budget_router          # Batch 114
app.include_router(recipes_excel_router)
app.include_router(recipes_bulk_router)
app.include_router(inv_reports_router)
app.include_router(masters_bulk_router)
app.include_router(boq_router)
app.include_router(audit_viewer_router)
app.include_router(reorder_router)
app.include_router(match_router)
app.include_router(coa_router)
app.include_router(approval_router)
app.include_router(landed_router)
app.include_router(recall_router)
app.include_router(rbuilder_router)
app.include_router(budget_router)
app.include_router(setup_import_router)
app.include_router(recipes_router)
app.include_router(dashboard_router)
app.include_router(production_router)
app.include_router(kitchen_prod_router)
app.include_router(inventory_router)
app.include_router(masters_router)
app.include_router(orders_router)
from app.modules.orders.routes_menu import router as menu_router  # Batch 102
app.include_router(menu_router)
app.include_router(sales_review_router)   
app.include_router(purchase_req_router)   
app.include_router(qc_router)
app.include_router(qc_sampling_router)    
app.include_router(topup_router)          
app.include_router(packing_router)
app.include_router(dispatch_router)
app.include_router(prod_docs_router)
app.include_router(settings_router)
app.include_router(reports_tree_router)
app.include_router(reports_router)
app.include_router(report_schedule_router)
app.include_router(users_router)
app.include_router(notifications_router)
app.include_router(search_router)
app.include_router(customer_router)
app.include_router(procurement_router)
app.include_router(finance_router)
app.include_router(projects_router)
app.include_router(hr_router)
app.include_router(hr_payroll_router)
app.include_router(subscriptions_router)
app.include_router(subscriptions_portal_router)
app.include_router(printforms_router)
app.include_router(module_dash_router)
app.include_router(masters_crud_router)
app.include_router(finance_ext_router)
app.include_router(procurement_print_router)
app.include_router(settings_modules_router)
app.include_router(reports_workflow_router)
app.include_router(finance_statements_router)
app.include_router(finance_periods_router)

# ===== ROUTES =====

@app.get("/modules")
async def module_launcher(request: Request):
   
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
# =============================================================================
# Batch 102 — schema guards that run at IMPORT, not only at startup.
#
# WHY THIS MOVED.  Batch 101 added recipes.day_of_week to the ORM model and put
# the ALTER inside @app.on_event("startup"). That is how every other migration
# in this file works, and it still produced a hard 500 on /recipes/upload-excel:
#
#     Unknown column 'recipes.day_of_week' in 'field list'
#
# The moment a column exists on the ORM model, EVERY query SQLAlchemy builds
# for that model selects it. So the window between "model imported" and
# "startup event finished" is a window in which the entire Recipes module is
# broken — and anything that imports app.main without running the lifespan
# (a script, a worker, a test client that isn't used as a context manager, or
# a reload that races) never closes that window at all.
#
# My own note from Batch 89 says additive features should prefer raw SQL over
# ORM model changes precisely because of this. Adding the column to the model
# was the right call for readability, so the guard has to be stronger instead:
# it now runs at import time, before the app object can serve anything, AND
# again at startup. It is idempotent, so running twice costs one cheap
# information_schema lookup.
# =============================================================================
def _ensure_recipe_menu_columns() -> None:
    """Add recipes.day_of_week if missing, and WIDEN it if it exists too narrow.

    Batch 132: FRSH multi-day recipes (salads/snacks) store the explicit day
    list, e.g. "Saturday & Sunday & Monday & Tuesday & Wednesday & Thursday"
    (59 chars). The original column was VARCHAR(20), so those inserts died with
    MySQL 1406 'Data too long for column day_of_week' and every multi-day recipe
    silently vanished from the menu (the missing salads). We create at — and
    grow existing installs to — VARCHAR(120). Idempotent."""
    try:
        from app.database.session import SessionLocal as _SL
        from sqlalchemy import text as _t
        _db = _SL()
        try:
            info = _db.execute(_t("""
                SELECT CHARACTER_MAXIMUM_LENGTH FROM information_schema.columns
                WHERE table_schema = DATABASE() AND table_name = 'recipes'
                  AND column_name = 'day_of_week'
            """)).scalar()
            if info is None:
                _db.execute(_t("ALTER TABLE recipes ADD COLUMN day_of_week VARCHAR(120) NULL"))
                try:
                    _db.execute(_t("CREATE INDEX idx_recipes_day ON recipes (day_of_week)"))
                except Exception:
                    pass   # index already there, or insufficient privilege
                _db.commit()
                logger.info("Added recipes.day_of_week VARCHAR(120)")
            elif int(info) < 120:
                # Column exists but is the old narrow width — widen in place.
                _db.execute(_t("ALTER TABLE recipes MODIFY COLUMN day_of_week VARCHAR(120) NULL"))
                _db.commit()
                logger.info(f"Widened recipes.day_of_week from VARCHAR({info}) to VARCHAR(120)")
            # Batch 158: SMC meal_order (BREAKFAST/LUNCH/DINNER).
            has_meal = _db.execute(_t("""
                SELECT COUNT(*) FROM information_schema.columns
                WHERE table_schema = DATABASE() AND table_name = 'recipes'
                  AND column_name = 'meal_order'
            """)).scalar()
            if not has_meal:
                _db.execute(_t("ALTER TABLE recipes ADD COLUMN meal_order VARCHAR(64) NULL"))
                _db.commit()
                logger.info("Added recipes.meal_order")
        finally:
            _db.close()
    except Exception as exc:
        # Never block import over this — log loudly and let startup retry.
        logger.error(f"Schema guard failed (recipes.day_of_week): {exc}")


# Run immediately at import, before any router can receive a request.
_ensure_recipe_menu_columns()


def _ensure_packing_bags_column() -> None:
    """Batch 122 — add packing_dispatch.packed_bags if missing, AT IMPORT TIME.

    Root cause of the 500s in Batch 121: the ORM model gained `packed_bags`,
    but the migration only ran in the startup event — which fires AFTER the app
    can serve requests, and is skipped entirely if an earlier guard raised or if
    the running process was never restarted. Every `db.query(PackingDispatch)`
    then failed with 'Unknown column packed_bags'. Following the day_of_week
    pattern, this guard runs at import, before any router is live, and is
    idempotent so repeat calls cost one cheap information_schema lookup.
    """
    try:
        from app.database.session import SessionLocal as _SL
        from sqlalchemy import text as _t
        _db = _SL()
        try:
            has = _db.execute(_t("""
                SELECT COUNT(*) FROM information_schema.columns
                WHERE table_schema = DATABASE() AND table_name = 'packing_dispatch'
                  AND column_name = 'packed_bags'
            """)).scalar()
            if not has:
                _db.execute(_t("ALTER TABLE packing_dispatch ADD COLUMN packed_bags INT NULL"))
                _db.commit()
                logger.info("Added packing_dispatch.packed_bags")
        finally:
            _db.close()
    except Exception as exc:
        logger.error(f"Schema guard failed (packing_dispatch.packed_bags): {exc}")


# Run immediately at import — the packed_bags column is read by the packing,
# dispatch AND QC-pass flows, so it must exist before any of them is hit.
_ensure_packing_bags_column()


def _ensure_dispatch_region_column() -> None:
    """Batch 129 — add packing_dispatch.region if missing, at import time.
    The Dispatch screen and Logistics report group deliveries by region
    (Riyadh / Eastern / Jeddah / Makkah). Idempotent."""
    try:
        from app.database.session import SessionLocal as _SL
        from sqlalchemy import text as _t
        _db = _SL()
        try:
            has = _db.execute(_t("""
                SELECT COUNT(*) FROM information_schema.columns
                WHERE table_schema = DATABASE() AND table_name = 'packing_dispatch'
                  AND column_name = 'region'
            """)).scalar()
            if not has:
                _db.execute(_t("ALTER TABLE packing_dispatch ADD COLUMN region VARCHAR(50) NULL"))
                _db.commit()
                logger.info("Added packing_dispatch.region")
            # Batch 152a: region-wise bag allocation JSON.
            has_rb = _db.execute(_t("""
                SELECT COUNT(*) FROM information_schema.columns
                WHERE table_schema = DATABASE() AND table_name = 'packing_dispatch'
                  AND column_name = 'region_bags'
            """)).scalar()
            if not has_rb:
                _db.execute(_t("ALTER TABLE packing_dispatch ADD COLUMN region_bags TEXT NULL"))
                _db.commit()
                logger.info("Added packing_dispatch.region_bags")
        finally:
            _db.close()
    except Exception as exc:
        logger.error(f"Schema guard failed (packing_dispatch.region): {exc}")


_ensure_dispatch_region_column()


def _ensure_output_capture_columns() -> None:
    """Batch 153 — real columns for kitchen output and packed nutrition.

    Until now these values were appended to the remark string as stamps:
        [C12 P30 V0 Y5]        Hot Kitchen nutrition split
        [OUT 12Gram Wt250g]    Cold Kitchen / Bakery produced portion
        [NUT w= p= c=]         recipe-level process output

    That was a deliberate short-term choice — it avoided a migration — but it
    does not survive contact with reporting: you cannot SUM a remark, group by
    it, or chart it. With dashboards and reporting as the next phase, these have
    to become columns before anything is built on top of them.

    Existing stamped rows are NOT lost. scripts/backfill_output_capture.py
    parses them out of the remarks and fills these columns; run it once after
    this guard has created them.

    Idempotent: each column is checked against information_schema first, because
    ADD COLUMN IF NOT EXISTS is not supported on this MySQL version.
    """
    _WANTED = {
        "kitchen_section_transactions": [
            ("produced_portion", "DECIMAL(14,4) NULL"),
            ("portion_weight_g", "DECIMAL(14,4) NULL"),
            ("output_uom", "VARCHAR(20) NULL"),
            ("carb_g", "DECIMAL(14,4) NULL"),
            ("protein_g", "DECIMAL(14,4) NULL"),
            ("vegetable_g", "DECIMAL(14,4) NULL"),
            ("yield_g", "DECIMAL(14,4) NULL"),
        ],
        "packing_dispatch": [
            ("packed_protein_g", "DECIMAL(14,4) NULL"),
            ("packed_carb_g", "DECIMAL(14,4) NULL"),
            ("packed_veg_g", "DECIMAL(14,4) NULL"),
        ],
    }
    try:
        from app.database.session import SessionLocal as _SL
        from sqlalchemy import text as _t
        _db = _SL()
        try:
            for table, cols in _WANTED.items():
                for col, ddl in cols:
                    has = _db.execute(_t("""
                        SELECT COUNT(*) FROM information_schema.columns
                        WHERE table_schema = DATABASE() AND table_name = :t
                          AND column_name = :c
                    """), {"t": table, "c": col}).scalar()
                    if not has:
                        _db.execute(_t(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}"))
                        _db.commit()
                        logger.info(f"Added {table}.{col}")
        finally:
            _db.close()
    except Exception as exc:
        logger.error(f"Schema guard failed (output capture columns): {exc}")


_ensure_output_capture_columns()


def _ensure_packing_pack_lines_table() -> None:
    """Batch 157 — per-recipe packed weights recorded at Trayline.

    THE DESIGN DECISION I WAS BLOCKED ON, resolved without forcing it.

    I asked whether packed weight should be captured once per recipe, or per
    recipe per region. Rather than guess and risk a table that has to be rebuilt,
    the table carries a nullable `region` column and a unique key of
    (order_no, recipe_no, region):

      * TODAY the UI writes region = '' — one row per recipe, which is the
        simpler workflow and what the Excel sheet shows.
      * IF you later want per-region capture, the UI writes region = 'Riyadh'
        etc. and the same table holds both. No migration, no rebuild, and orders
        captured under the old shape keep working because '' is just another
        region value to the unique key.

    So the answer can change later at the cost of a UI change only. That is why
    this ships now instead of waiting.

    Idempotent — checks information_schema before creating.
    """
    try:
        from app.database.session import SessionLocal as _SL
        from sqlalchemy import text as _t
        _db = _SL()
        try:
            exists = _db.execute(_t("""
                SELECT COUNT(*) FROM information_schema.tables
                WHERE table_schema = DATABASE() AND table_name = 'packing_pack_lines'
            """)).scalar()
            if not exists:
                _db.execute(_t("""
                    CREATE TABLE packing_pack_lines (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        company_id INT NULL,
                        order_no VARCHAR(50) NOT NULL,
                        recipe_no VARCHAR(50) NOT NULL,
                        region VARCHAR(100) NOT NULL DEFAULT '',
                        packed_portion DECIMAL(14,4) NULL,
                        packed_protein_g DECIMAL(14,4) NULL,
                        packed_carb_g DECIMAL(14,4) NULL,
                        packed_veg_g DECIMAL(14,4) NULL,
                        remarks VARCHAR(255) NULL,
                        created_at DATETIME NULL,
                        updated_at DATETIME NULL,
                        UNIQUE KEY uq_pack_line (order_no, recipe_no, region),
                        KEY ix_pack_order (order_no)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """))
                _db.commit()
                logger.info("Created table packing_pack_lines")
        finally:
            _db.close()
    except Exception as exc:
        logger.error(f"Schema guard failed (packing_pack_lines): {exc}")


_ensure_packing_pack_lines_table()

def _ensure_meal_order_width() -> None:
    """Batch 159 — widen recipes.meal_order from VARCHAR(30) to VARCHAR(64).

    meal_order now holds a per-day map, at its longest
    "SAT=LD|SUN=LD|MON=LD|TUE=LD|WED=LD|THU=LD|FRI=LD" — 48 characters.
    At VARCHAR(30) MySQL would silently truncate that to
    "SAT=LD|SUN=LD|MON=LD|TUE=LD|WE", leaving the last three days with no
    meals at all: recipes would vanish from Thursday and Friday menus with no
    error anywhere. This is the same silent overflow as the day_of_week
    VARCHAR fix in Batch 131, so it gets the same guard.
    """
    try:
        from app.database.session import SessionLocal as _SL
        from sqlalchemy import text as _t
        _db = _SL()
        try:
            ln = _db.execute(_t("""
                SELECT CHARACTER_MAXIMUM_LENGTH FROM information_schema.columns
                WHERE table_schema = DATABASE() AND table_name = 'recipes'
                  AND column_name = 'meal_order'
            """)).scalar()
            if ln is not None and int(ln) < 64:
                _db.execute(_t("ALTER TABLE recipes MODIFY meal_order VARCHAR(64) NULL"))
                _db.commit()
                logger.info(f"Widened recipes.meal_order {ln} -> 64")
        finally:
            _db.close()
    except Exception as exc:
        logger.error(f"Schema guard failed (recipes.meal_order width): {exc}")


_ensure_meal_order_width()


def _ensure_recipe_ingredient_section_column() -> None:
    """Batch 131 — add recipe_ingredients.kitchen_section if missing, at import.

    The store-issuance section routing (PRD1 → Cutting; else the recipe's own
    kitchen section) needs the workbook's "Section" value carried down to each
    ingredient line. The ORM model now declares `kitchen_section`, so — per the
    day_of_week / packed_bags precedent — the column MUST exist before any route
    queries RecipeIngredient, or every recipe/BOM read would 500 with 'Unknown
    column kitchen_section'. Raw additive column, idempotent."""
    try:
        from app.database.session import SessionLocal as _SL
        from sqlalchemy import text as _t
        _db = _SL()
        try:
            has = _db.execute(_t("""
                SELECT COUNT(*) FROM information_schema.columns
                WHERE table_schema = DATABASE() AND table_name = 'recipe_ingredients'
                  AND column_name = 'kitchen_section'
            """)).scalar()
            if not has:
                _db.execute(_t("ALTER TABLE recipe_ingredients ADD COLUMN kitchen_section VARCHAR(80) NULL"))
                _db.commit()
                logger.info("Added recipe_ingredients.kitchen_section")
            # Batch 136: Butchery cutting / portion-size column (same guard).
            has2 = _db.execute(_t("""
                SELECT COUNT(*) FROM information_schema.columns
                WHERE table_schema = DATABASE() AND table_name = 'recipe_ingredients'
                  AND column_name = 'cutting_portion_size'
            """)).scalar()
            if not has2:
                _db.execute(_t("ALTER TABLE recipe_ingredients ADD COLUMN cutting_portion_size VARCHAR(255) NULL"))
                _db.commit()
                logger.info("Added recipe_ingredients.cutting_portion_size")
        finally:
            _db.close()
    except Exception as exc:
        logger.error(f"Schema guard failed (recipe_ingredients.kitchen_section): {exc}")


_ensure_recipe_ingredient_section_column()


@app.on_event("startup")
async def startup_event():
    logger.info("Application startup complete")
    logger.info("API Documentation: http://localhost:8000/docs")

    try:
        from app.database.session import SessionLocal
        from app.modules.production.routes import _ensure_sales_review_schema
        _db = SessionLocal()
        try:
            _ensure_sales_review_schema(_db)
            logger.info("Verified customer_orders.sales_review_status schema")
        finally:
            _db.close()
    except Exception as exc:
        logger.error(f"Startup schema check failed (sales_review_status): {exc}")

    _ensure_recipe_menu_columns()   # Batch 102: also runs at import — see below

    try:
        from app.database.session import SessionLocal
        from app.modules.purchase_req.routes import ensure_schema as _pr_ensure_schema
        _db = SessionLocal()
        try:
            _pr_ensure_schema(_db)
            logger.info("Verified purchase_requisitions schema")
        finally:
            _db.close()
    except Exception as exc:
        logger.error(f"Startup schema check failed (purchase_requisitions): {exc}")

    # Batch 121: packing_dispatch.packed_bags — the packer records physical
    # bag/tray count; Dispatch reads it. Needs to exist before packing save.
    try:
        from app.database.session import SessionLocal
        from app.modules.packing.routes import ensure_schema as _packing_schema
        _db = SessionLocal()
        try:
            _packing_schema(_db)
            logger.info("Verified packing_dispatch.packed_bags schema")
        finally:
            _db.close()
    except Exception as exc:
        logger.error(f"Startup schema check failed (packed_bags): {exc}")

    # Batch 94: top-up requests and the QC sampling config, same startup
    # migration reasoning as everything above it.
    try:
        from app.database.session import SessionLocal
        from app.modules.production.routes_topup import ensure_schema as _topup_schema
        from app.modules.qc.sampling import ensure_schema as _sampling_schema
        _db = SessionLocal()
        try:
            _topup_schema(_db)
            _sampling_schema(_db)
            logger.info("Verified store_topup_requests / qc_sampling_config schema")
        finally:
            _db.close()
    except Exception as exc:
        logger.error(f"Startup schema check failed (topup/sampling): {exc}")

    # Batch 93: same reasoning as above — the new Incoming QC gate needs
    # inventory_transactions.qc_status to exist before any GRN posts or
    # any stock-availability query runs, not just when /qc/inspection or
    # a GRN receipt happens to be the first thing hit.
    try:
        from app.database.session import SessionLocal
        from app.core.stock_ledger import ensure_qc_status_column, ensure_ledger_schema
        _db = SessionLocal()
        try:
            # Batch 94: repair a legacy-shaped ledger table BEFORE the
            # qc_status check — on a fresh database created through
            # init_db.py the ORM model wins the CREATE TABLE race and
            # produces a table with none of the modern columns, which breaks
            # every stock read in the system. See ensure_ledger_schema().
            ensure_ledger_schema(_db)
            ensure_qc_status_column(_db)
            logger.info("Verified inventory_transactions schema (shape + qc_status)")
        finally:
            _db.close()
    except Exception as exc:
        logger.error(f"Startup schema check failed (qc_status): {exc}")

    # Batch 95: same reasoning again — supplier ratings are read by the PO
    # creation and PO detail supplier dropdowns, so the column needs to
    # exist before either of those is ever hit, not just when the ratings
    # screen itself happens to be visited first.
    try:
        from app.database.session import SessionLocal
        from app.modules.procurement.routes import _ensure_supplier_rating_schema
        _db = SessionLocal()
        try:
            _ensure_supplier_rating_schema(_db)
            logger.info("Verified suppliers.rating schema")
        finally:
            _db.close()
    except Exception as exc:
        logger.error(f"Startup schema check failed (supplier rating): {exc}")


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

