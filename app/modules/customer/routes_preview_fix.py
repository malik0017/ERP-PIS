# app/modules/customer/routes_preview_fix.py
from fastapi import APIRouter, Depends, Request, Form
from fastapi.responses import RedirectResponse
from sqlalchemy import text
from sqlalchemy.orm import Session
from app.core.templates import render
from app.database.session import get_db
import logging

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/customer", tags=["Customer Portal"])


@router.get("/admin-preview-list")
async def preview_list(request: Request, db: Session = Depends(get_db)):
    """
    Admin page: select a customer to preview their portal
    Batch 29: Enhanced UI with lock icon, clear instructions
    """
    if request.session.get("role") not in ("admin", "super"):
        return RedirectResponse("/", status_code=403)
    
    # Get list of all customers
    customers = db.execute(
        text("""
        SELECT customer_code, customer_name, brand
        FROM customers
        WHERE company_id = :cid
        ORDER BY customer_name ASC
        """),
        {"cid": request.session.get("company_id", 1)}
    ).mappings().all()
    
    return render(request, "customer/admin_preview_list.html", {
        "customers": [dict(c) for c in customers],
        "page_title": "Preview Customer Portal",
    })


@router.post("/admin-preview-enter")
async def preview_enter(
    request: Request,
    customer_code: str = Form(...),
    db: Session = Depends(get_db),
):
    """
    Start admin preview session for a specific customer
    """
    if request.session.get("role") not in ("admin", "super"):
        return RedirectResponse("/", status_code=403)
    
    # Verify customer exists
    customer = db.execute(
        text("""
        SELECT customer_code, customer_name
        FROM customers
        WHERE customer_code = :code AND company_id = :cid
        """),
        {"code": customer_code, "cid": request.session.get("company_id", 1)}
    ).mappings().first()
    
    if not customer:
        return RedirectResponse("/customer/admin-preview-list")
    
    # Set admin preview session
    request.session["admin_preview_mode"] = True
    request.session["preview_customer_code"] = customer_code
    request.session["preview_customer_name"] = customer["customer_name"]
    
    logger.info(f"Admin {request.session.get('user_id')} previewing customer {customer_code}")
    
    return RedirectResponse("/customer/orders")


@router.post("/admin-preview-exit")
async def preview_exit(request: Request):
    """
    Exit admin preview mode
    """
    if request.session.get("admin_preview_mode"):
        logger.info(f"Admin {request.session.get('user_id')} exiting preview of {request.session.get('preview_customer_code')}")
        
        request.session.pop("admin_preview_mode", None)
        request.session.pop("preview_customer_code", None)
        request.session.pop("preview_customer_name", None)
    
    return RedirectResponse("/customer/admin-preview-list")
