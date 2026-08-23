# app/core/templates.py

from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.templating import Jinja2Templates

from app.core.i18n import translate, lang_from_request, is_rtl as _is_rtl
from app.core.rbac import can_access, current_access, normalized_role, can_action

BASE_DIR = Path(__file__).resolve().parents[1]
TEMPLATE_DIR = BASE_DIR / "templates"


def _inject_common_context(context: dict[str, Any] | None) -> dict[str, Any]:
    """Add common template values used by sidebar/header.

    This is intentionally centralized because some routes use render(...), while
    older routes call templates.TemplateResponse(...) directly.
    """
    data: dict[str, Any] = dict(context or {})
    request = data.get("request")

    if isinstance(request, Request):
        data.setdefault("access", current_access(request))
        data.setdefault("user_role", normalized_role(request))
        data.setdefault("can_access", lambda area: can_access(request, area))
        data.setdefault("can_action", lambda area, action="view": can_action(request, area, action))
        # Batch 65: company-level module gate, available in every template.
        try:
            from app.core.module_visibility import module_enabled as _mod_enabled
            data.setdefault("module_enabled", lambda key: _mod_enabled(request, key))
        except Exception:
            data.setdefault("module_enabled", lambda key: True)
        # Batch 121: pipeline step-locking helpers, available in every template.
        # `stage_locked(status, stage)` -> bool; `stage_lock_reason(...)` -> str.
        try:
            from app.core.stage_lock import is_stage_locked as _sl, lock_reason as _slr
            data.setdefault("stage_locked", lambda status, stage: _sl(status, stage))
            data.setdefault("stage_lock_reason", lambda status, stage: _slr(status, stage))
        except Exception:
            data.setdefault("stage_locked", lambda status, stage: False)
            data.setdefault("stage_lock_reason", lambda status, stage: "")
        # Expose username/role for the launcher header/footer.
        try:
            data.setdefault("session_username", request.session.get("username"))
        except Exception:
            data.setdefault("session_username", None)
        # Per-company branding: static/uploads/logos/company_<id>.png (if uploaded)
        try:
            import os as _os
            _cid = request.session.get("company_id")
            _logo_fs = _os.path.join(_os.path.dirname(_os.path.dirname(__file__)), "static", "uploads", "logos", f"company_{_cid}.png")
            data.setdefault("company_logo", f"/static/uploads/logos/company_{_cid}.png" if _cid and _os.path.exists(_logo_fs) else None)
        except Exception:
            data.setdefault("company_logo", None)
        # ------------------------------------------------------------------
        # Batch 99 — header essentials, injected globally so EVERY page's
        # navbar has them rather than only the handful of routes that
        # remembered to pass them.
        #
        #   user_avatar          the logged-in user's uploaded profile photo
        #   unread_notifications the count for the bell badge
        #   company_logo_small   50x50 header logo (see below)
        #
        # Each is wrapped defensively: a missing table or column must degrade
        # to a sensible default, never 500 the whole application on every
        # single page. That is the same reasoning behind the existing
        # company_logo block above.
        # ------------------------------------------------------------------
        try:
            import os as _os2
            _uid = request.session.get("user_id")
            _avatar = None
            if _uid:
                _base = _os2.path.join(_os2.path.dirname(_os2.path.dirname(__file__)),
                                       "static", "uploads", "avatars")
                for _ext in ("png", "jpg", "jpeg", "webp"):
                    _fs = _os2.path.join(_base, f"user_{_uid}.{_ext}")
                    if _os2.path.exists(_fs):
                        # Cache-bust on mtime so a re-upload shows immediately
                        # instead of being served from the browser cache.
                        _avatar = f"/static/uploads/avatars/user_{_uid}.{_ext}?v={int(_os2.path.getmtime(_fs))}"
                        break
            data.setdefault("user_avatar", _avatar)
        except Exception:
            data.setdefault("user_avatar", None)

        try:
            from app.database.session import SessionLocal as _SL
            from sqlalchemy import text as _text
            _uid = request.session.get("user_id")
            _role = request.session.get("user_role")
            _cid2 = int(request.session.get("company_id") or 1)
            _n = 0
            if _uid:
                _db = _SL()
                try:
                    # Batch 98 scoping applies here too — the badge must count
                    # only what this user in THIS company can actually open.
                    _n = _db.execute(_text("""
                        SELECT COUNT(*) FROM notifications
                        WHERE (company_id = :cid OR company_id IS NULL)
                          AND (user_id = :uid OR (role = :role AND role IS NOT NULL))
                          AND COALESCE(is_read, 0) = 0
                    """), {"uid": _uid, "role": _role, "cid": _cid2}).scalar() or 0
                finally:
                    _db.close()
            data.setdefault("unread_notifications", int(_n))
        except Exception:
            data.setdefault("unread_notifications", 0)

        _lang = lang_from_request(request)
        data.setdefault("lang", _lang)
        data.setdefault("is_rtl", _is_rtl(_lang))
        data.setdefault("t", lambda key: translate(key, _lang))
    else:
        data.setdefault("access", {})
        data.setdefault("user_role", "GUEST")
        data.setdefault("can_access", lambda area: False)
        data.setdefault("can_action", lambda area, action="view": False)
        data.setdefault("module_enabled", lambda key: True)
        data.setdefault("session_username", None)
        data.setdefault("user_avatar", None)
        data.setdefault("unread_notifications", 0)
        data.setdefault("company_logo", None)
        data.setdefault("lang", "en")
        data.setdefault("is_rtl", False)
        data.setdefault("t", lambda key: key)

    return data


class ISFCTemplates(Jinja2Templates):
    """Jinja templates with automatic ISFC context injection.

    Starlette/FastAPI changed TemplateResponse signatures across versions.
    This wrapper supports both styles:
      templates.TemplateResponse("page.html", {"request": request})
      templates.TemplateResponse(request, "page.html", context={...})
    """

    def TemplateResponse(self, *args: Any, **kwargs: Any):  # noqa: N802 - FastAPI API name
        if "context" in kwargs:
            kwargs["context"] = _inject_common_context(kwargs.get("context"))
            return super().TemplateResponse(*args, **kwargs)

        # Common old style: TemplateResponse(name, context, ...)
        if len(args) >= 2 and isinstance(args[1], dict):
            args = list(args)
            args[1] = _inject_common_context(args[1])
            return super().TemplateResponse(*args, **kwargs)

        # Common new style: TemplateResponse(request, name, context, ...)
        if len(args) >= 3 and isinstance(args[2], dict):
            args = list(args)
            args[2] = _inject_common_context(args[2])
            return super().TemplateResponse(*args, **kwargs)

        return super().TemplateResponse(*args, **kwargs)


templates = ISFCTemplates(directory=str(TEMPLATE_DIR))

# Also expose helpers as Jinja globals for templates that need direct checks.
templates.env.globals["can_access_area"] = can_access


# Batch 65: parse a JSON string in-template (used by launcher sparklines).
def _from_json(value):
    import json as _json
    if value is None or value == "":
        return []
    if isinstance(value, (list, dict)):
        return value
    try:
        return _json.loads(value)
    except Exception:
        return []


templates.env.filters["from_json"] = _from_json


def render(request: Request, template_name: str, context: dict | None = None, status_code: int = 200):
    data = dict(context or {})
    data["request"] = request
    data = _inject_common_context(data)
    return templates.TemplateResponse(template_name, data, status_code=status_code)
