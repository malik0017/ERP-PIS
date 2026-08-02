# app/modules/notifications/routes.py
"""Notification Center.

Two independent feeds, shown together on this page and in the header bell:

1. LIVE "pending work" queue (unchanged from before) — computed fresh on
   every request from current order/kitchen state:
     - Head Chef approval pending  (orders in Submitted)
     - Store issuance pending      (orders BOM Generated / Store Pending)
     - QC pending                  (orders in production, no passed QC yet)
     - Dispatch pending            (orders Packed, not yet dispatched)
   There is nothing to "mark as read" here — it isn't a discrete event, it's
   a live count that changes the moment the underlying order moves.

2. REAL, persisted notifications (Batch 78) — actual events written once via
   app/core/notifications.py::create_notification() from elsewhere in the
   app (order submitted, QC failed, payroll finalized, ...), targeted at
   either a specific user or a role. These CAN be marked read, individually
   or all at once.

Endpoints:
  GET  /notifications/summary        -> JSON for the header bell (both feeds)
  GET  /notifications                -> full page, both feeds
  POST /notifications/{id}/read      -> mark one real notification read
  POST /notifications/mark-all-read  -> mark every real notification read
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import text
from sqlalchemy.orm import Session
from starlette.status import HTTP_303_SEE_OTHER

from app.core.templates import render
from app.core.rbac import can_access
from app.core.notifications import ensure_notifications_schema
from app.database.session import get_db

router = APIRouter(prefix="/notifications", tags=["Notifications"])


def _safe_rows(db: Session, sql: str, params: dict | None = None) -> list:
    """Run a query defensively - a missing table must never crash the bell."""
    try:
        return list(db.execute(text(sql), params or {}).mappings().all())
    except Exception:
        return []


def _real_notifications(db: Session, request: Request, limit: int = 30) -> list[dict]:
    """This user's own real notifications, plus any broadcast to their role
    — read and unread both (the page needs both; the badge count filters)."""
    user_id = request.session.get("user_id")
    role = request.session.get("user_role")
    if not user_id:
        return []
    ensure_notifications_schema(db)
    rows = _safe_rows(db, """
        SELECT id, title, message, url, category, is_read, created_at
        FROM notifications
        WHERE user_id = :uid OR (role = :role AND role IS NOT NULL)
        ORDER BY is_read ASC, created_at DESC
        LIMIT :lim
    """, {"uid": user_id, "role": role, "lim": limit})
    return [dict(r) for r in rows]


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
    work_items = [i for i in items if i["count"] > 0]

    # Batch 78: real, per-user notifications alongside the live work queue.
    real = _real_notifications(db, request, limit=10)
    unread_real = [n for n in real if not n["is_read"]]
    notif_items = [{
        "key": f"notif_{n['id']}", "label": n["title"], "count": 1,
        "url": n["url"] or "/notifications", "id": n["id"], "message": n.get("message"),
        "created_at": str(n.get("created_at") or ""),
    } for n in unread_real]

    return JSONResponse({
        "total": sum(counts.values()) + len(unread_real),
        "counts": counts,
        "items": work_items,
        "notifications": notif_items,
        "unread_notifications": len(unread_real),
    })


@router.post("/{notification_id}/read")
async def mark_notification_read(request: Request, notification_id: int, db: Session = Depends(get_db)):
    ensure_notifications_schema(db)
    user_id = request.session.get("user_id")
    role = request.session.get("user_role")
    if not user_id:
        return RedirectResponse("/login", status_code=HTTP_303_SEE_OTHER)
    db.execute(text("""
        UPDATE notifications SET is_read = 1, read_at = NOW()
        WHERE id = :id AND (user_id = :uid OR role = :role)
    """), {"id": notification_id, "uid": user_id, "role": role})
    db.commit()
    ref = request.headers.get("referer") or "/notifications"
    return RedirectResponse(ref, status_code=HTTP_303_SEE_OTHER)


@router.post("/mark-all-read")
async def mark_all_notifications_read(request: Request, db: Session = Depends(get_db)):
    ensure_notifications_schema(db)
    user_id = request.session.get("user_id")
    role = request.session.get("user_role")
    if not user_id:
        return RedirectResponse("/login", status_code=HTTP_303_SEE_OTHER)
    db.execute(text("""
        UPDATE notifications SET is_read = 1, read_at = NOW()
        WHERE (user_id = :uid OR role = :role) AND is_read = 0
    """), {"uid": user_id, "role": role})
    db.commit()
    ref = request.headers.get("referer") or "/notifications"
    return RedirectResponse(ref, status_code=HTTP_303_SEE_OTHER)


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
    real = _real_notifications(db, request, limit=100)
    unread_count = len([n for n in real if not n["is_read"]])
    return render(request, "notifications/index.html", {
        "groups": groups,
        "total": total,
        "notifications": real,
        "unread_count": unread_count,
        "page_title": "Notification Center",
    })
