# app/services/order_360.py
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

import logging

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

SECTIONS = ["Cutting", "Butchery", "Hot Kitchen", "Cold Kitchen", "Bakery/Pastry"]


def _f(v: Any) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _s(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, (datetime, date)):
        return v.strftime("%Y-%m-%d %H:%M") if isinstance(v, datetime) else v.isoformat()
    return str(v)


class _Q:

    def __init__(self, db: Session):
        self.db = db

    def rows(self, sql: str, p: dict) -> list[dict]:
        try:
            return [dict(r) for r in self.db.execute(text(sql), p).mappings().all()]
        except Exception as exc:
            logger.warning("order_360 query failed: %s", str(exc).splitlines()[0][:300])
            self.db.rollback()
            return []

    def one(self, sql: str, p: dict) -> dict:
        r = self.rows(sql, p)
        return r[0] if r else {}


def _clean(value):
   
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    if isinstance(value, (datetime, date)):
        return _s(value)
    if isinstance(value, Decimal):
        return float(value)
    return value


def build(db: Session, order_no: str, cid: int) -> dict:
    q = _Q(db)
    p = {"o": order_no, "cid": cid}

    order = q.one("""
        SELECT o.order_no, o.customer_name, COALESCE(o.brand,'') AS brand,
               COALESCE(o.status,'') AS status, COALESCE(o.priority,'Normal') AS priority,
               o.order_date, o.required_delivery_date, o.cooking_date, o.created_at,
               COALESCE(o.total_planned_portions,0) AS portions,
               COALESCE(o.total_estimated_food_cost,0) AS food_cost,
               COALESCE(o.total_estimated_selling_value,0) AS sale,
               COALESCE(o.total_estimated_margin,0) AS margin,
               COALESCE(o.kitchen,'') AS kitchen, COALESCE(o.channel,'') AS channel
        FROM customer_orders o
        WHERE o.order_no = :o AND (o.company_id = :cid OR o.company_id IS NULL)""", p)
    if not order:
        return {}

    recipes = q.rows("""
        SELECT ol.recipe_no, MAX(ol.recipe_name) AS recipe_name,
               SUM(COALESCE(ol.required_portions,0)) AS portions
        FROM order_lines ol WHERE ol.order_no = :o
        GROUP BY ol.recipe_no ORDER BY portions DESC""", p)

    mat = q.one("""
        SELECT COALESCE(SUM(COALESCE(b.net_required_qty_standard,
                                     b.total_required_with_waste_standard, 0)),0) AS net_required,
               COALESCE(SUM(COALESCE(b.total_required_with_waste_standard, 0)),0) AS gross_required,
               COALESCE(SUM(COALESCE(b.estimated_cost,0)),0) AS est_cost,
               COUNT(*) AS line_count
        FROM bom_lines b WHERE b.order_no = :o""", p)
    issued = q.one("""
        SELECT COALESCE(SUM(CASE WHEN s.issuance_status IN ('Issued','Short Issued')
                    THEN COALESCE(s.input_material_issued, s.issued_qty_standard, 0) END),0) AS issued_qty,
               SUM(s.issuance_status = 'Short Issued') AS short_lines,
               SUM(COALESCE(s.finalized,0) = 0) AS open_lines, COUNT(*) AS line_count
        FROM store_issuance_lines s WHERE s.order_no = :o""", p)
    kitchen = q.one("""
        SELECT COALESCE(SUM(k.received_qty_standard),0) AS received,
               COALESCE(SUM(k.waste_qty_standard),0) AS waste,
               COALESCE(SUM(k.returned_qty_standard),0) AS returned,
               COALESCE(SUM(CASE WHEN k.current_section = 'QC' THEN k.issued_qty_standard END),0) AS to_qc
        FROM kitchen_section_transactions k WHERE k.order_no = :o""", p)

    # ---------------------------------------------------------------- pipeline
    stage = q.one("""
        SELECT EXISTS(SELECT 1 FROM bom_lines b WHERE b.order_no = :o) AS bom,
               EXISTS(SELECT 1 FROM store_issuance_lines s WHERE s.order_no = :o
                      AND s.issuance_status IN ('Issued','Short Issued')) AS issued,
               EXISTS(SELECT 1 FROM kitchen_section_transactions k WHERE k.order_no = :o) AS kitchen,
               EXISTS(SELECT 1 FROM qc_checks c WHERE c.order_no = :o AND c.qc_status = 'Passed') AS qc,
               EXISTS(SELECT 1 FROM qc_checks c WHERE c.order_no = :o AND c.qc_status IN ('Rejected','Hold')) AS qc_bad,
               EXISTS(SELECT 1 FROM packing_dispatch d WHERE d.order_no = :o
                      AND d.dispatch_status IN ('Packed','Assigned','Out for Delivery','Delivered')) AS packed,
               EXISTS(SELECT 1 FROM packing_dispatch d WHERE d.order_no = :o
                      AND d.dispatch_status = 'Delivered') AS delivered""", p)

    def dot(done: bool, warn: bool = False) -> str:
        return "bad" if warn else ("ok" if done else "pending")

    pipeline = [
        {"key": "order", "label": "Sales Order", "ref": order["order_no"], "state": "ok"},
        {"key": "bom", "label": "BOM", "ref": f"{int(_f(mat.get('line_count')))} lines" if mat.get("line_count") else "—",
         "state": dot(bool(stage.get("bom")))},
        {"key": "issue", "label": "Material Issue",
         "ref": f"{_f(issued.get('issued_qty')):,.0f}" if issued.get("issued_qty") else "—",
         "state": dot(bool(stage.get("issued")), bool(_f(issued.get("short_lines"))))},
        {"key": "kitchen", "label": "Kitchen", "ref": f"{_f(kitchen.get('received')):,.0f} received"
         if _f(kitchen.get("received")) else "—", "state": dot(bool(stage.get("kitchen")))},
        {"key": "qc", "label": "QC Check", "ref": "—",
         "state": dot(bool(stage.get("qc")), bool(stage.get("qc_bad")))},
        {"key": "dispatch", "label": "Delivery",
         "ref": "Delivered" if stage.get("delivered") else ("Packed" if stage.get("packed") else "—"),
         "state": "ok" if stage.get("delivered") else ("half" if stage.get("packed") else "pending")},
    ]

    # ---------------------------------------------------------------- execution
    steps = q.rows("""
        SELECT k.current_section AS section,
               -- LINES is a reserved word in MySQL/MariaDB (same trap as
               -- Batch 208's DELAYED). Aliased, or the whole query is rejected
               -- and the Execution tab silently shows nothing.
               COUNT(*) AS line_count,
               COALESCE(SUM(k.received_qty_standard),0) AS received,
               COALESCE(SUM(k.transferred_qty_standard),0) AS transferred,
               COALESCE(SUM(k.waste_qty_standard),0) AS waste,
               MIN(k.received_at) AS first_at, MAX(k.transferred_at) AS last_at,
               GROUP_CONCAT(DISTINCT NULLIF(k.received_by,'') SEPARATOR ', ') AS people,
               ROUND(AVG(CASE WHEN k.received_at >= k.created_at
                    THEN TIMESTAMPDIFF(MINUTE, k.created_at, k.received_at) END),0) AS queue_min
        FROM kitchen_section_transactions k
        WHERE k.order_no = :o GROUP BY k.current_section
        ORDER BY MIN(k.route_step_no), MIN(k.id)""", p)
    for s in steps:
        rec, tr = _f(s["received"]), _f(s["transferred"])
        s["yield_pct"] = round(tr / rec * 100, 1) if rec > 0 else None
        s["first_at"], s["last_at"] = _s(s["first_at"]), _s(s["last_at"])

    # ---------------------------------------------------------------- materials
    materials = q.rows("""
        SELECT b.ingredient_code, b.ingredient_name, COALESCE(b.standard_uom,'') AS uom,
               COALESCE(b.net_required_qty_standard,
                        b.total_required_with_waste_standard, 0) AS net_required,
               COALESCE(b.total_required_with_waste_standard,0) AS gross_required,
               COALESCE(b.estimated_cost,0) AS est_cost,
               (SELECT COALESCE(SUM(COALESCE(s.input_material_issued, s.issued_qty_standard,0)),0)
                  FROM store_issuance_lines s
                 WHERE s.order_no = b.order_no AND s.ingredient_code = b.ingredient_code
                   AND s.issuance_status IN ('Issued','Short Issued')) AS issued,
               (SELECT COALESCE(SUM(k.waste_qty_standard),0)
                  FROM kitchen_section_transactions k
                 WHERE k.order_no = b.order_no AND k.ingredient_code = b.ingredient_code) AS waste
        FROM bom_lines b WHERE b.order_no = :o
        ORDER BY b.estimated_cost DESC, b.ingredient_name LIMIT 200""", p)
    for m in materials:
        m["variance"] = round(_f(m["issued"]) - _f(m["gross_required"]), 3) if _f(m["issued"]) else None

    # ---------------------------------------------------------------- quality
    checks = q.rows("""
        SELECT c.qc_no, COALESCE(c.recipe_name,'') AS recipe_name, COALESCE(c.section,'') AS section,
               COALESCE(c.check_type,'') AS check_type, c.qc_status, c.overall_score,
               c.temperature_c, c.checked_by, c.checked_at,
               COALESCE(c.issue_found,'') AS issue_found,
               COALESCE(c.corrective_action,'') AS corrective_action
        FROM qc_checks c WHERE c.order_no = :o ORDER BY c.id DESC LIMIT 50""", p)
    for c in checks:
        c["checked_at"] = _s(c["checked_at"])
    qsum = q.one("""
        SELECT SUM(c.qc_status='Passed') AS passed,
               SUM(c.qc_status IN ('Passed','Rejected','Hold')) AS decided,
               ROUND(AVG(c.overall_score),1) AS avg_score
        FROM qc_checks c WHERE c.order_no = :o""", p)
    nutrition = q.rows("""
        SELECT COALESCE(k.recipe_name, k.recipe_no) AS recipe_name,
               SUM(k.protein_g) AS protein, SUM(k.carb_g) AS carb, SUM(k.vegetable_g) AS veg,
               MAX(k.portion_weight_g) AS portion_weight
        FROM kitchen_section_transactions k
        WHERE k.order_no = :o AND k.current_section = 'QC'
        GROUP BY COALESCE(k.recipe_name, k.recipe_no)
        HAVING protein IS NOT NULL OR carb IS NOT NULL OR veg IS NOT NULL""", p)

    # ---------------------------------------------------------------- financial
    unit_waste_cost = (_f(mat.get("est_cost")) / _f(mat.get("gross_required"))) if _f(mat.get("gross_required")) else 0.0
    waste_cost = round(_f(kitchen.get("waste")) * unit_waste_cost, 2)
    sale, cost = _f(order["sale"]), _f(order["food_cost"])
    financial = [
        {"label": "Revenue", "value": sale, "basis": "Order / contract price"},
        {"label": "Material cost (estimated)", "value": cost, "basis": "BOM at standard cost"},
        {"label": "Waste cost", "value": waste_cost,
         "basis": "Recorded waste at the order's average material rate" if waste_cost else "No waste recorded"},
        {"label": "Gross margin", "value": round(sale - cost, 2) if sale else None,
         "basis": f"{(sale - cost) / sale * 100:.1f}% of revenue" if sale else "No selling value on the order"},
    ]

    # ---------------------------------------------------------------- audit
    audit = q.rows("""
        SELECT a.action, a.table_name, a.record_id, a.created_at,
               COALESCE(u.username, CONCAT('user #', a.user_id)) AS who
        FROM audit_logs a LEFT JOIN users u ON u.id = a.user_id
        WHERE a.record_id = :o OR (a.table_name = 'customer_orders' AND a.record_id = :o)
        ORDER BY a.id DESC LIMIT 40""", p)
    for a in audit:
        a["created_at"] = _s(a["created_at"])
    if not audit:
        # No audit rows is normal on orders created before audit logging, or by
        # an importer. Say that, rather than showing an empty panel that reads
        # as "nothing ever happened to this order".
        audit_note = "No audit entries recorded for this order."
    else:
        audit_note = ""

    return _clean({
        "order_no": order["order_no"],
        "header": {
            "customer": order["customer_name"], "brand": order["brand"],
            "status": order["status"], "priority": order["priority"],
            "delivery": _s(order["required_delivery_date"]), "cooking": _s(order["cooking_date"]),
            "ordered": _s(order["order_date"]), "kitchen": order["kitchen"], "channel": order["channel"],
            "recipes": recipes[:6], "recipe_count": len(recipes),
        },
        "tiles": [
            {"label": "Portions", "value": f"{_f(order['portions']):,.0f}"},
            {"label": "Waste", "value": (f"{_f(kitchen['waste']):,.1f} / "
                                         f"{_f(kitchen['waste']) / _f(kitchen['received']) * 100:.1f}%")
             if _f(kitchen.get("received")) else "—"},
            {"label": "Material cost", "value": f"SAR {cost:,.0f}" if cost else "—"},
            {"label": "Margin", "value": f"{(sale - cost) / sale * 100:.1f}%" if sale else "—"},
        ],
        "pipeline": pipeline,
        "execution": steps,
        "materials": materials,
        "material_totals": {
            "net_required": round(_f(mat.get("net_required")), 2),
            "gross_required": round(_f(mat.get("gross_required")), 2),
            "issued": round(_f(issued.get("issued_qty")), 2),
            "waste": round(_f(kitchen.get("waste")), 2),
            "returned": round(_f(kitchen.get("returned")), 2),
            "to_qc": round(_f(kitchen.get("to_qc")), 2),
            "short_lines": int(_f(issued.get("short_lines"))),
            "open_lines": int(_f(issued.get("open_lines"))),
        },
        "quality": {
            "checks": checks, "nutrition": nutrition,
            "passed": int(_f(qsum.get("passed"))), "decided": int(_f(qsum.get("decided"))),
            "avg_score": qsum.get("avg_score"),
            "pass_rate": round(_f(qsum.get("passed")) / _f(qsum.get("decided")) * 100, 1)
            if _f(qsum.get("decided")) else None,
        },
        "financial": financial,
        "audit": audit, "audit_note": audit_note,
    })
