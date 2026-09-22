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
from app.modules.exports.routes_pdf import router as exports_pdf_router   # Batch 222
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

    try:
        await startup_event()
    except Exception as exc:  # never block the app from starting
        logger.error(f"Startup guards failed: {exc}")
    yield
    try:
        await shutdown_event()
    except Exception:
        pass
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

from app.core.module_gate import ModuleGateMiddleware
app.add_middleware(ModuleGateMiddleware)

app.add_middleware(AuthMiddleware)

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
from app.modules.recipes.routes_excel import router as recipes_excel_router  
from app.modules.recipes.routes_bulk import router as recipes_bulk_router    
from app.modules.reports.routes_inventory import router as inv_reports_router 
from app.modules.masters.routes_bulk import router as masters_bulk_router      
from app.modules.production.routes_boq import router as boq_router            
from app.modules.admin.routes_audit import router as audit_viewer_router       
from app.modules.inventory.routes_reorder import router as reorder_router      
from app.modules.procurement.routes_match import router as match_router        
from app.modules.setup.routes_import import router as setup_import_router      
from app.modules.finance.routes_coa import router as coa_router              
from app.modules.settings.routes_approval import router as approval_router     
from app.modules.procurement.routes_landed import router as landed_router     
from app.modules.qc.routes_recall import router as recall_router               
from app.modules.reports.routes_builder import router as rbuilder_router       
from app.modules.finance.routes_budget import router as budget_router          
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
from app.modules.orders.routes_menu import router as menu_router  
app.include_router(menu_router)
app.include_router(sales_review_router)   
app.include_router(purchase_req_router)   
app.include_router(qc_router)
app.include_router(qc_sampling_router)    
app.include_router(topup_router)          
app.include_router(packing_router)
app.include_router(exports_pdf_router)  
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

from app.modules.sla.routes import router as sla_router  
from app.modules.requisitions.routes import router as requisitions_router  
app.include_router(requisitions_router)

app.include_router(sla_router)
from app.modules.sla.routes import ops_router as sla_ops_router 
app.include_router(sla_ops_router)

# ===== ROUTES =====

@app.get("/modules")
async def module_launcher(request: Request):
   
    ctx = {"stats": {}, "charts": {}}
    hero = None
    cid = int(request.session.get("company_id") or 1)
    try:
        from app.database.session import SessionLocal
        _db = SessionLocal()
        try:
            ctx = build_launcher_context(_db, cid) 
            from app.modules.module_dash.routes_launcher import command_centre_card
            try:
                hero = command_centre_card(_db, cid)
            except Exception as exc:
                logger.warning("Launcher hero card unavailable: %s", exc)
        finally:
            _db.close()
    except Exception:
        ctx = {"stats": {}, "charts": {}}
    return render(request, "modules/index.html", {
        "page_title": "ERP Modules",
        "stats": ctx.get("stats", {}),
        "cards": ctx.get("cards", {}),
        "hero": hero,
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
    # ------------------------------------------------------------------
    # Batch 235 (Image 1) — A 500 HAS TO BE FINDABLE.
    #
    # The page said "sequence item 2: expected str instance, list found" and
    # nothing else: no URL, no file, no line, and the log held only the same
    # one-line message because this handler used logger.error(), which does
    # not write a traceback. A message like that could come from any of the
    # dozens of `" AND ".join(where)` sites in the app, so the only way to
    # chase it was to guess.
    #
    # Now: the full traceback goes to the log, together with the method, path
    # and query string that produced it, under a short reference code that is
    # also printed on the screen. The user reads out "PIS-4F2A9C" and the
    # exact frame is one grep away. The code is a hash of the failing frame,
    # so the SAME fault always gets the SAME reference and a recurrence is
    # recognisable rather than looking like a new bug.
    # ------------------------------------------------------------------
    import hashlib
    import traceback as _tb

    frames = _tb.extract_tb(exc.__traceback__) if getattr(exc, "__traceback__", None) else []
    last = frames[-1] if frames else None
    where_ = f"{last.filename}:{last.lineno} in {last.name}" if last else "unknown frame"
    ref = hashlib.sha1(
        f"{exc.__class__.__name__}|{exc}|{where_}".encode("utf-8", "replace")
    ).hexdigest()[:6].upper()

    logger.error(
        "500 [PIS-%s] %s %s%s -> %s: %s (at %s)",
        ref, request.method, request.url.path,
        f"?{request.url.query}" if request.url.query else "",
        exc.__class__.__name__, exc, where_,
    )
    if frames:
        logger.error("500 [PIS-%s] traceback:\n%s", ref, "".join(_tb.format_tb(exc.__traceback__)))

    error_detail = (
        f"{exc.__class__.__name__}: {exc}\n{where_}" if DEBUG
        else "Something went wrong. The team has been notified."
    )
    try:
        return render(
            request,
            "errors/500.html",
            {"error": error_detail, "error_ref": f"PIS-{ref}",
             "error_path": request.url.path},
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


def _ensure_recipe_menu_columns() -> None:
   
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
                    pass   
                _db.commit()
                logger.info("Added recipes.day_of_week VARCHAR(120)")
            elif int(info) < 120:
                _db.execute(_t("ALTER TABLE recipes MODIFY COLUMN day_of_week VARCHAR(120) NULL"))
                _db.commit()
                logger.info(f"Widened recipes.day_of_week from VARCHAR({info}) to VARCHAR(120)")
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
        logger.error(f"Schema guard failed (recipes.day_of_week): {exc}")


_ensure_recipe_menu_columns()


def _ensure_packing_bags_column() -> None:
   
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
            ("byproduct_qty_standard", "DECIMAL(18,4) NULL"),
            # Batch 235 — the grain link. See KitchenSectionTransaction.bom_line_id.
            ("bom_line_id", "INT NULL"),
        ],
        "bom_lines": [
            ("gross_required_qty_standard", "DECIMAL(18,4) NULL"),
            ("net_required_qty_standard", "DECIMAL(18,4) NULL"),
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


def _backfill_kitchen_bom_line_id() -> None:
    """Batch 235 — link EXISTING kitchen transactions to their BOM line.

    New rows get bom_line_id at store-issue time (production_service). Rows
    already in the database have nothing, so the reports would keep falling
    back to the coarse (order, recipe, ingredient) join for every order placed
    before this batch — i.e. the bug would look "fixed for new orders only",
    which is the worst kind of half-fix to hand to a kitchen.

    How the link is recovered:

      1. store_issuance_lines already carries bom_line_id AND is the row each
         kitchen transaction was created from. Match on
         (order_no, ingredient_code, order_line_id) and, where a recipe lists
         the same ingredient more than once, pair them by ORDINAL — both sides
         were created in bom_lines order, in the same pass, so the n-th
         issuance line corresponds to the n-th kitchen transaction.
      2. Anything still unmatched is left NULL on purpose. The reports fall
         back to the old ingredient-grain join for those rows and label it,
         rather than guessing a link and printing a confident wrong number.

    Runs once: guarded on there being any NULL bom_line_id row left to fill,
    so a restart on an already-linked database costs one cheap COUNT(*).
    """
    try:
        from app.database.session import SessionLocal as _SL
        from sqlalchemy import text as _t
        _db = _SL()
        try:
            has_col = _db.execute(_t("""
                SELECT COUNT(*) FROM information_schema.columns
                WHERE table_schema = DATABASE()
                  AND table_name = 'kitchen_section_transactions'
                  AND column_name = 'bom_line_id'
            """)).scalar()
            if not has_col:
                return
            pending = _db.execute(_t("""
                SELECT COUNT(*) FROM kitchen_section_transactions
                WHERE bom_line_id IS NULL
            """)).scalar()
            if not pending:
                return

            # Ordinal pairing on both sides. ROW_NUMBER() needs MySQL 8 /
            # MariaDB 10.2+, which this deployment already requires elsewhere.
            _db.execute(_t("""
                UPDATE kitchen_section_transactions k
                JOIN (
                    SELECT id, order_no, ingredient_code,
                           COALESCE(order_line_id, 0) AS oli,
                           ROW_NUMBER() OVER (
                               PARTITION BY order_no, ingredient_code,
                                            COALESCE(order_line_id, 0)
                               ORDER BY id) AS rn
                      FROM kitchen_section_transactions
                     WHERE bom_line_id IS NULL
                ) kk ON kk.id = k.id
                JOIN (
                    SELECT bom_line_id, order_no, ingredient_code,
                           COALESCE(order_line_id, 0) AS oli,
                           ROW_NUMBER() OVER (
                               PARTITION BY order_no, ingredient_code,
                                            COALESCE(order_line_id, 0)
                               ORDER BY id) AS rn
                      FROM store_issuance_lines
                     WHERE bom_line_id IS NOT NULL
                ) s ON s.order_no        = kk.order_no
                   AND s.ingredient_code = kk.ingredient_code
                   AND s.oli             = kk.oli
                   AND s.rn              = kk.rn
                SET k.bom_line_id = s.bom_line_id
            """))
            _db.commit()
            # Batch 236: count what is ACTUALLY unlinked.
            #
            # Your first run reported "filled 0 of 3 rows (3 still unlinked)",
            # which reads like a failure and is not one. A recipe-level OUTPUT
            # row — the one process_recipe_output() writes when a section
            # finishes a whole recipe — has ingredient_code = recipe_no,
            # because it IS the recipe, not an ingredient of it. It has no BOM
            # line by design and never will. Counting those as "unlinked"
            # invites someone to go hunting for a bug that does not exist.
            #
            # They are excluded from the figure and reported separately.
            outputs = _db.execute(_t("""
                SELECT COUNT(*) FROM kitchen_section_transactions
                WHERE bom_line_id IS NULL
                  AND ingredient_code = recipe_no
            """)).scalar() or 0
            left = (_db.execute(_t("""
                SELECT COUNT(*) FROM kitchen_section_transactions
                WHERE bom_line_id IS NULL
                  AND (recipe_no IS NULL OR ingredient_code <> recipe_no)
            """)).scalar()) or 0
            filled = int(pending) - int(left) - int(outputs)
            msg = (f"Batch 235 backfill: kitchen_section_transactions.bom_line_id "
                   f"linked {filled} row(s)")
            if outputs:
                msg += f"; {outputs} recipe-level output row(s) need no BOM line"
            if left:
                msg += (f"; {left} ingredient row(s) still unlinked "
                        f"- those fall back to the pro-rata split in reports")
            logger.info(msg)
        finally:
            _db.close()
    except Exception as exc:
        # Never block startup on a backfill. The reports degrade to the old
        # join, which is exactly where they were before this batch.
        logger.error(f"Schema guard failed (bom_line_id backfill): {exc}")


_backfill_kitchen_bom_line_id()


def _ensure_packing_pack_lines_table() -> None:
  
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

def _ensure_sla_target_tables() -> None:
   
    _DDL = {
        "sla_rules": """
            CREATE TABLE sla_rules (
                id INT AUTO_INCREMENT PRIMARY KEY,
                company_id INT NULL,
                rule_name VARCHAR(150) NOT NULL,
                customer_name VARCHAR(255) NULL,
                order_type VARCHAR(80) NULL,
                sla_minutes INT NOT NULL DEFAULT 480,
                at_risk_minutes INT NOT NULL DEFAULT 120,
                grace_minutes INT NOT NULL DEFAULT 15,
                starts_on VARCHAR(30) NOT NULL DEFAULT 'CONFIRMED',
                ends_on VARCHAR(30) NOT NULL DEFAULT 'DELIVERED',
                basis VARCHAR(20) NOT NULL DEFAULT 'DEADLINE',
                priority INT NOT NULL DEFAULT 100,
                notify_at_risk TINYINT(1) NOT NULL DEFAULT 1,
                notify_overdue TINYINT(1) NOT NULL DEFAULT 1,
                status VARCHAR(20) NOT NULL DEFAULT 'Active',
                created_at DATETIME NULL, updated_at DATETIME NULL,
                KEY ix_sla_cust (customer_name), KEY ix_sla_status (status)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """,
        "order_sla": """
            CREATE TABLE order_sla (
                id INT AUTO_INCREMENT PRIMARY KEY,
                company_id INT NULL,
                order_no VARCHAR(50) NOT NULL,
                sla_rule_id INT NULL,
                rule_name VARCHAR(150) NULL,
                started_at DATETIME NULL,
                due_at DATETIME NULL,
                at_risk_at DATETIME NULL,
                grace_until DATETIME NULL,
                completed_at DATETIME NULL,
                status VARCHAR(20) NOT NULL DEFAULT 'ON_TRACK',
                created_at DATETIME NULL, updated_at DATETIME NULL,
                UNIQUE KEY uq_order_sla (order_no),
                KEY ix_osla_status (status), KEY ix_osla_due (due_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """,
        "sla_exceptions": """
            CREATE TABLE sla_exceptions (
                id INT AUTO_INCREMENT PRIMARY KEY,
                company_id INT NULL,
                order_no VARCHAR(50) NOT NULL,
                extend_minutes INT NOT NULL DEFAULT 0,
                reason VARCHAR(255) NULL,
                approved_by VARCHAR(120) NULL,
                created_at DATETIME NULL,
                KEY ix_slaex_order (order_no)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """,
        "performance_targets": """
            CREATE TABLE performance_targets (
                id INT AUTO_INCREMENT PRIMARY KEY,
                company_id INT NULL,
                target_name VARCHAR(150) NOT NULL,
                metric_code VARCHAR(40) NOT NULL,
                customer_name VARCHAR(255) NULL,
                period VARCHAR(20) NOT NULL DEFAULT 'DAILY',
                target_value DECIMAL(18,4) NOT NULL DEFAULT 0,
                unit VARCHAR(30) NULL,
                effective_from DATE NULL,
                effective_to DATE NULL,
                status VARCHAR(20) NOT NULL DEFAULT 'Active',
                created_at DATETIME NULL, updated_at DATETIME NULL,
                KEY ix_tgt_metric (metric_code), KEY ix_tgt_status (status)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """,
    }
    try:
        from app.database.session import SessionLocal as _SL
        from sqlalchemy import text as _t
        _db = _SL()
        try:
            for table, ddl in _DDL.items():
                exists = _db.execute(_t("""
                    SELECT COUNT(*) FROM information_schema.tables
                    WHERE table_schema = DATABASE() AND table_name = :t
                """), {"t": table}).scalar()
                if not exists:
                    _db.execute(_t(ddl))
                    _db.commit()
                    logger.info(f"Created table {table}")
        finally:
            _db.close()
    except Exception as exc:
        logger.error(f"Schema guard failed (sla/targets): {exc}")


_ensure_sla_target_tables()


def _ensure_recipe_ingredient_section_column() -> None:
   
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

    _ensure_recipe_menu_columns()   
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

    try:
        from app.database.session import SessionLocal
        from app.core.stock_ledger import ensure_qc_status_column, ensure_ledger_schema
        _db = SessionLocal()
        try:
            ensure_ledger_schema(_db)
            ensure_qc_status_column(_db)
            logger.info("Verified inventory_transactions schema (shape + qc_status)")
        finally:
            _db.close()
    except Exception as exc:
        logger.error(f"Startup schema check failed (qc_status): {exc}")

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

