# app/modules/settings/routes.py

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database import get_db
from app.core.auth import get_current_user
from app.core.templates import render
from app.core.audit import write_audit
from app.models.setting import SystemSetting


router = APIRouter(prefix="/settings", tags=["Settings"])


def _company_id(user) -> int:
    return getattr(user, "company_id", None) or 1


def _column_exists(db: Session, table: str, column: str) -> bool:
    return bool(
        db.execute(
            text(
                """
                SELECT COUNT(*)
                FROM information_schema.columns
                WHERE table_schema = DATABASE()
                  AND table_name = :table_name
                  AND column_name = :column_name
                """
            ),
            {
                "table_name": table,
                "column_name": column,
            },
        ).scalar()
    )


def _ensure_settings_schema(db: Session) -> None:
    db.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS system_settings (
                id INT NOT NULL AUTO_INCREMENT PRIMARY KEY
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """
        )
    )

    columns = {
        "company_id": "INT NOT NULL DEFAULT 1 AFTER id",
        "company_name": "VARCHAR(255) NULL",
        "company_name_ar": "VARCHAR(255) NULL",
        "logo": "VARCHAR(255) NULL",
        "favicon": "VARCHAR(255) NULL",
        "default_language": "VARCHAR(10) NOT NULL DEFAULT 'en'",
        "timezone": "VARCHAR(100) NOT NULL DEFAULT 'Asia/Riyadh'",
        "currency": "VARCHAR(10) NOT NULL DEFAULT 'SAR'",
        "date_format": "VARCHAR(50) NOT NULL DEFAULT 'dd-mm-yyyy'",
        "number_format": "VARCHAR(50) NOT NULL DEFAULT '1,234.00'",
        "is_rtl_enabled": "TINYINT(1) NOT NULL DEFAULT 1",
        "is_active": "TINYINT(1) NOT NULL DEFAULT 1",
        "sidebar_title": "VARCHAR(100) NULL",
        "sidebar_subtitle": "VARCHAR(120) NULL",
        "header_title": "VARCHAR(100) NULL",
        "header_subtitle": "VARCHAR(120) NULL",
        "authenticator_enabled": "TINYINT(1) NOT NULL DEFAULT 0",
        "max_login_attempts": "INT NOT NULL DEFAULT 5",
        "two_factor_setup_valid_days": "INT NOT NULL DEFAULT 30",
        "created_at": "DATETIME DEFAULT CURRENT_TIMESTAMP",
        "updated_at": "DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP",
    }

    for column, definition in columns.items():
        if not _column_exists(db, "system_settings", column):
            db.execute(
                text(f"ALTER TABLE system_settings ADD COLUMN {column} {definition}")
            )

    db.execute(
        text(
            """
            UPDATE system_settings
            SET company_id = 1
            WHERE company_id IS NULL OR company_id = 0
            """
        )
    )

    db.commit()


def _get_settings(db: Session, company_id: int) -> SystemSetting:
    _ensure_settings_schema(db)

    row = (
        db.query(SystemSetting)
        .filter(
            SystemSetting.company_id == company_id,
            SystemSetting.is_active == True,
        )
        .first()
    )

    if row:
        return row

    row = SystemSetting(
        company_id=company_id,
        company_name="International Specialized Food Company",
        company_name_ar="الشركة العالمية المتخصصة للأغذية",
        default_language="en",
        timezone="Asia/Riyadh",
        currency="SAR",
        date_format="dd-mm-yyyy",
        number_format="1,234.00",
        is_rtl_enabled=True,
        is_active=True,
    )

    db.add(row)
    db.commit()
    db.refresh(row)

    return row


@router.get("", response_class=HTMLResponse)
def settings_page(
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    settings = _get_settings(db, _company_id(current_user))
    extra = db.execute(text("""
        SELECT sidebar_title, sidebar_subtitle, header_title, header_subtitle,
               authenticator_enabled, max_login_attempts, two_factor_setup_valid_days
        FROM system_settings WHERE id = :id
    """), {"id": settings.id}).mappings().first() or {}
    for k, v in dict(extra).items():
        try:
            setattr(settings, k, v)
        except Exception:
            pass

    return render(
        request,
        "settings/index.html",
        {
            "page_title": "System Settings",
            "settings": settings,
        },
    )


@router.post("")
async def save_settings(
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    form = await request.form()
    settings = _get_settings(db, _company_id(current_user))

    settings.company_name = str(form.get("company_name") or "").strip() or settings.company_name
    settings.company_name_ar = str(form.get("company_name_ar") or "").strip() or None

    # Optional branding/security columns are read/written with SQL so older ORM model remains compatible.
    sidebar_title = str(form.get("sidebar_title") or "ISFC PIS").strip()
    sidebar_subtitle = str(form.get("sidebar_subtitle") or "Production Intelligence").strip()
    header_title = str(form.get("header_title") or "ISFC").strip()
    header_subtitle = str(form.get("header_subtitle") or "Production").strip()
    authenticator_enabled = str(form.get("authenticator_enabled") or "0") == "1"
    max_login_attempts = int(form.get("max_login_attempts") or 5)
    two_factor_setup_valid_days = int(form.get("two_factor_setup_valid_days") or 30)
    db.execute(text("""
        UPDATE system_settings
        SET sidebar_title = :sidebar_title,
            sidebar_subtitle = :sidebar_subtitle,
            header_title = :header_title,
            header_subtitle = :header_subtitle,
            authenticator_enabled = :authenticator_enabled,
            max_login_attempts = :max_login_attempts,
            two_factor_setup_valid_days = :two_factor_setup_valid_days
        WHERE id = :id
    """), {
        "sidebar_title": sidebar_title,
        "sidebar_subtitle": sidebar_subtitle,
        "header_title": header_title,
        "header_subtitle": header_subtitle,
        "authenticator_enabled": authenticator_enabled,
        "max_login_attempts": max_login_attempts,
        "two_factor_setup_valid_days": two_factor_setup_valid_days,
        "id": settings.id,
    })

    request.session["sidebar_title"] = sidebar_title
    request.session["sidebar_subtitle"] = sidebar_subtitle
    request.session["header_title"] = header_title
    request.session["header_subtitle"] = header_subtitle

    settings.default_language = str(form.get("default_language") or "en").strip()
    settings.timezone = str(form.get("timezone") or "Asia/Riyadh").strip()
    settings.currency = str(form.get("currency") or "SAR").strip()
    settings.date_format = str(form.get("date_format") or "dd-mm-yyyy").strip()
    settings.number_format = str(form.get("number_format") or "1,234.00").strip()
    settings.is_rtl_enabled = str(form.get("is_rtl_enabled") or "0") == "1"

    write_audit(db, getattr(current_user, "id", None), "SYSTEM_SETTINGS_UPDATED", "system_settings", settings.id)
    db.commit()

    return RedirectResponse(
        url="/settings?toast=success&title=Settings Saved&msg=System settings updated successfully",
        status_code=303,
    )

@router.get("/set-language/{lang}")
async def set_language(request: Request, lang: str):
    """Switch UI language (en/ar). Session-based; users.preferred_language
    can be synced here later. Redirects back to the referring page."""
    from app.core.i18n import SUPPORTED
    if lang in SUPPORTED:
        request.session["lang"] = lang
    back = request.headers.get("referer") or "/dashboard"
    from fastapi.responses import RedirectResponse
    return RedirectResponse(back, status_code=303)


# =========================================================================
# COMPANY BRANDING (Batch 4): per-company logo upload
# Saved to /static/uploads/logos/company_<id>.png and injected into the
# navbar automatically via core/templates.py (company_logo).
# =========================================================================
import os as _os
from fastapi import UploadFile as _UploadFile, File as _File2

_LOGO_DIR = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.dirname(__file__))), "static", "uploads", "logos")


@router.post("/company-logo")
async def upload_company_logo(request: Request, logo: _UploadFile = _File2(...)):
    from app.core.rbac import require_action
    require_action(request, "settings", "edit")
    cid = request.session.get("company_id")
    if not cid:
        return RedirectResponse("/settings?error=No active company in session", status_code=303)
    ext = (logo.filename or "").rsplit(".", 1)[-1].lower()
    if ext not in {"png", "jpg", "jpeg", "webp"}:
        return RedirectResponse("/settings?error=Logo must be PNG/JPG/WEBP", status_code=303)
    data = await logo.read()
    if len(data) > 1 * 1024 * 1024:
        return RedirectResponse("/settings?error=Logo must be under 1MB", status_code=303)
    _os.makedirs(_LOGO_DIR, exist_ok=True)
    # Always store as company_<id>.png path (browser renders jpg bytes fine either way,
    # but keep the real extension for correctness)
    fname = f"company_{cid}.png"
    with open(_os.path.join(_LOGO_DIR, fname), "wb") as fh:
        fh.write(data)
    return RedirectResponse("/settings?success=Company logo updated - it now shows in the header", status_code=303)
