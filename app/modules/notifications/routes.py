# app/modules/notifications/routes.py
"""Notification Center.

Computes live "pending work" counts across the production chain:
  - Head Chef approval pending  (orders in Submitted)
  - Store issuance pending      (orders BOM Generated / Store Pending)
  - QC pending                  (orders in production, no passed QC yet)
  - Dispatch pending            (orders Packed, not yet dispatched)

Two endpoints:
  GET /notifications/summary  -> JSON (used by the header bell in toast.html)
  GET /notifications          -> full page listing each pending document
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.templates import render
from app.core.rbac import can_access
from app.database.session import get_db

router = APIRouter(prefix="/notifications", tags=["Notifications"])


def _safe_rows(db: Session, sql: str, params: dict | None = None) -> list:
    """Run a query defensively - a missing table must never crash the bell."""
    try:
        return list(db.execute(text(sql), params or {}).mappings().all())
    except Exception:
        return []


def _collect(db: Session) -> dict:
    head_chef = _safe_rows(db, """
        SELECT order_no, customer_name, COALESCE(brand,'') AS brand,
               COALESCE(required_delivery_date,'') AS delivery_date, status
        FROM customer_orders
        WHERE status = 'Submitted'
        ORDER BY id DESC LIMIT 50
    """)

    store = _safe_rows(db, """
        SELECT order_no, customer_name, COALESCE(brand,'') AS brand,
               COALESCE(required_delivery_date,'') AS delivery_date, status
        FROM customer_orders
        WHERE status IN ('BOM Generated', 'Store Pending')
        ORDER BY id DESC LIMIT 50
    """)

    qc = _safe_rows(db, """
        SELECT co.order_no, co.customer_name, COALESCE(co.brand,'') AS brand,
               COALESCE(co.required_delivery_date,'') AS delivery_date, co.status
        FROM customer_orders co
        WHERE co.status = 'In Production'
          AND NOT EXISTS (
              SELECT 1 FROM qc_checks q
              WHERE q.order_no = co.order_no
                AND UPPER(COALESCE(q.status, q.qc_status, '')) = 'PASSED'
          )
        ORDER BY co.id DESC LIMIT 50
    """) or _safe_rows(db, """
        SELECT order_no, customer_name, COALESCE(brand,'') AS brand,
               COALESCE(required_delivery_date,'') AS delivery_date, status
        FROM customer_orders
        WHERE status = 'In Production'
        ORDER BY id DESC LIMIT 50
    """)

    dispatch = _safe_rows(db, """
        SELECT order_no, customer_name, COALESCE(brand,'') AS brand,
               COALESCE(required_delivery_date,'') AS delivery_date, status
        FROM customer_orders
        WHERE status IN ('Packed', 'Packing Pending', 'Out for Delivery')
        ORDER BY id DESC LIMIT 50
    """)

    # Batch 20: section-wise kitchen workload — pending receive lines per section.
    kitchen_sections = _safe_rows(db, """
        SELECT current_section AS section,
               COUNT(*) AS pending_lines,
               COUNT(DISTINCT order_no) AS orders
        FROM kitchen_section_transactions
        WHERE COALESCE(received_qty_standard, 0) <= 0
          AND UPPER(COALESCE(transaction_status,'')) NOT LIKE 'COMPLETED%'
          AND UPPER(COALESCE(transaction_status,'')) != 'TRANSFERRED'
        GROUP BY current_section
        ORDER BY pending_lines DESC
    """)

    return {
        "head_chef": head_chef,
        "store": store,
        "qc": qc,
        "dispatch": dispatch,
        "kitchen_sections": kitchen_sections,
    }


@router.get("/summary")
async def notifications_summary(request: Request, db: Session = Depends(get_db)):
    """Lightweight counts for the header bell. Requires login only."""
    if not request.session.get("user_id") and not request.session.get("username"):
        return JSONResponse({"total": 0, "items": []})

    data = _collect(db)
    counts = {
        "head_chef_pending": len(data["head_chef"]),
        "store_pending": len(data["store"]),
        "qc_pending": len(data["qc"]),
        "dispatch_pending": len(data["dispatch"]),
    }
    items = [
        {"key": "head_chef", "label": "Head Chef approval pending", "count": counts["head_chef_pending"], "url": "/production/head-chef"},
        {"key": "store", "label": "Store issuance pending", "count": counts["store_pending"], "url": "/production/store-issuance"},
        {"key": "qc", "label": "QC pending", "count": counts["qc_pending"], "url": "/qc"},
        {"key": "dispatch", "label": "Dispatch pending", "count": counts["dispatch_pending"], "url": "/dispatch"},
    ]
    # Batch 20: one bell entry per kitchen section with pending receive lines.
    for ks in data.get("kitchen_sections", []):
        sec = str(ks.get("section") or "")
        if not sec:
            continue
        slug = "Bakery-Pastry" if sec == "Bakery/Pastry" else ("Trayline-Packing" if sec == "Trayline / Packing" else sec.replace(" ", "-"))
        items.append({
            "key": f"section_{slug.lower()}",
            "label": f"{sec}: {ks['pending_lines']} line(s) to receive ({ks['orders']} order(s))",
            "count": int(ks["pending_lines"]),
            "url": f"/production/section/{slug}",
        })
    return JSONResponse({
        "total": sum(counts.values()),
        "counts": counts,
        "items": [i for i in items if i["count"] > 0],
    })


@router.get("")
async def notifications_page(request: Request, db: Session = Depends(get_db)):
    data = _collect(db)
    groups = [
        {"key": "head_chef", "title": "Head Chef Approval Pending", "icon": "uil-user-check",
         "hint": "Orders submitted by customers, waiting for cooking & material schedule approval.",
         "url": "/production/head-chef", "rows": data["head_chef"], "accent": "warning"},
        {"key": "store", "title": "Store Issuance Pending", "icon": "uil-store",
         "hint": "BOM released orders waiting for the store keeper to issue material.",
         "url": "/production/store-issuance", "rows": data["store"], "accent": "primary"},
        {"key": "qc", "title": "QC Pending", "icon": "uil-clipboard-notes",
         "hint": "Orders in production without a passed QC check.",
         "url": "/qc", "rows": data["qc"], "accent": "info"},
        {"key": "dispatch", "title": "Dispatch Pending", "icon": "uil-truck",
         "hint": "Packed orders waiting for dispatch / delivery.",
         "url": "/dispatch", "rows": data["dispatch"], "accent": "success"},
    ]
    total = sum(len(g["rows"]) for g in groups)
    return render(request, "notifications/index.html", {
        "groups": groups,
        "total": total,
        "page_title": "Notification Center",
    })
