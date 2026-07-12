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
        # Per-company branding: static/uploads/logos/company_<id>.png (if uploaded)
        try:
            import os as _os
            _cid = request.session.get("company_id")
            _logo_fs = _os.path.join(_os.path.dirname(_os.path.dirname(__file__)), "static", "uploads", "logos", f"company_{_cid}.png")
            data.setdefault("company_logo", f"/static/uploads/logos/company_{_cid}.png" if _cid and _os.path.exists(_logo_fs) else None)
        except Exception:
            data.setdefault("company_logo", None)
        _lang = lang_from_request(request)
        data.setdefault("lang", _lang)
        data.setdefault("is_rtl", _is_rtl(_lang))
        data.setdefault("t", lambda key: translate(key, _lang))
    else:
        data.setdefault("access", {})
        data.setdefault("user_role", "GUEST")
        data.setdefault("can_access", lambda area: False)
        data.setdefault("can_action", lambda area, action="view": False)
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


def render(request: Request, template_name: str, context: dict | None = None, status_code: int = 200):
    data = dict(context or {})
    data["request"] = request
    data = _inject_common_context(data)
    return templates.TemplateResponse(template_name, data, status_code=status_code)
