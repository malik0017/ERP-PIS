# ISFC PIS Phoenix UI Conversion Status and ERP Roadmap

## What was updated in this package

The project has been upgraded with a unified Phoenix UI layer without editing `app/templates/layouts/base.html` and without editing the `app/templates/partials` folder.

Updated areas:

1. `app/static/css/user.min.css`
   - Added a global ISFC/PIS Phoenix consistency layer.
   - Standardized heroes, KPI cards, filters, buttons, forms, badges, tables and responsive grids.
   - Mapped old custom classes such as `pis-card`, `isfc-card`, `si-card`, `po-card`, `rep-kpi`, `kpi-card` into one professional Phoenix visual language.

2. `app/static/js/phoenix.js`
   - Added a global UX layer.
   - All HTML tables now automatically receive:
     - consistent Phoenix table styling,
     - row count,
     - local search,
     - copy to clipboard,
     - CSV download.
   - Added automatic initialization support for Choices, Flatpickr and ECharts when available.

3. Key templates improved:
   - `app/templates/orders/list.html`
   - `app/templates/orders/detail.html`
   - `app/templates/finance/index.html`
   - `app/templates/notifications/index.html`
   - `app/templates/reports/index.html`
   - `app/templates/dashboard/dashboard.html`

## Current project status

### Completed / substantially available now

| Area | Status | Notes |
|---|---|---|
| FastAPI architecture | Available | Modular app structure exists under `app/modules` with service/model separation. |
| Phoenix base layout | Available | `base.html`, header, sidebar and theme assets are already Phoenix based. |
| Authentication / users | Available | User, role, permissions and access screens exist. |
| Master data | Available | Customers, suppliers, chefs, brands, inventory and master upload flows exist. |
| Recipe/BOM | Available | Recipe list, recipe form, ingredients and approval screens exist. |
| Production orders | Available | Production order queue, detail, BOM generation and head chef planning exist. |
| Store issuance | Available | Item-wise material issue flow exists with issue quantity and section routing. |
| Kitchen execution | Available | Section workstations exist for production sections. |
| QC | Available | QC queue and order checklist route exist. |
| Packing / dispatch | Available | Packing and dispatch routes/templates exist. |
| Reports | Partially available | Reports center, relationship map and yield/wastage views exist. |
| UI consistency | Improved in this package | Global CSS/JS now unifies most pages without touching base/partials. |

### Pending / not yet full ERP

| Area | Status | Required next work |
|---|---|---|
| Inventory ERP module | Pending Phase 2 | GRN, stock lots, expiry, FEFO, transfers, stock counts, valuation. |
| Procurement | Pending Phase 2 | PR, RFQ, PO, GRN, supplier invoice matching and supplier performance. |
| Finance | Pending Phase 3 | GL, AP, AR, invoice posting, payments, VAT/ZATCA and financial reports. |
| HR | Pending Phase 4 | Employee master, attendance, shifts, payroll integration and productivity. |
| Warehouse module | Pending registration | `warehouse` route file exists but is not currently included in `main.py`. |
| Finance/HR route registration | Pending | Finance/HR route files are empty or not registered in `main.py`. |
| API layer | Partial | HTML routes exist; formal JSON API for each module still needs expansion. |
| Multi-company controls | Partial | Company model exists; every query and transaction should be company/brand scoped. |
| Approval engine | Partial | Roles/permissions exist; full configurable workflow matrix is still pending. |
| Audit trail | Partial | Audit model exists; every create/update/delete should consistently call audit logging. |

## Recommended ERP module structure going forward

Use this fixed sidebar/module structure so the system does not become random screens:

1. Executive Dashboard
2. Core Setup
   - Companies, branches, brands, kitchens, system settings
3. Master Data
   - Customers, suppliers, items, ingredients, recipes, UOM, warehouses, sections
4. Sales / Orders
   - Customer portal, internal orders, subscriptions, catering, quotations
5. Recipe / BOM / Costing
   - Recipe builder, sub-recipes, BOM, versioning, approval, costing
6. Production Planning / MRP
   - Demand, consolidated BOM, head chef plan, section plan
7. Store / Inventory
   - Store issuance, stock receipt, stock transfer, stock count, expiry, lots
8. Kitchen Execution
   - Thawing, cutting, butchery, hot, cold, bakery/pastry, trayline
9. Quality Control
   - QC checklist, hold, reject, rework, CAPA, sample retention
10. Packing / Dispatch
   - Packing list, driver assignment, delivery, OTP/POD
11. Procurement
   - PR, RFQ, PO, GRN, supplier invoice, performance
12. Finance
   - GL, AR, AP, costing, VAT/ZATCA, reports
13. HR
   - Employees, attendance, shifts, payroll integration
14. Reports / BI
   - Management, production, finance, inventory, wastage and audit reports
15. Admin / Security
   - Users, roles, permissions, audit logs

## UI standard to follow for every new page

Every module page should use the same screen pattern:

```text
Phoenix hero header
→ KPI row
→ Filter card / slicer area
→ Main data table or transaction form
→ Right-side / bottom insight cards
→ Audit / status timeline
```

Rules:

- Filters should always appear immediately under the hero/KPI area.
- Tables should use one common responsive Phoenix style.
- All actions should use the same button hierarchy:
  - Primary: create/post/approve
  - Secondary: open/back/export
  - Warning: hold/rework
  - Danger: reject/delete
- Reports should use ECharts for visual summaries and tables for drill-down.
- Each transaction should show document status and next step.

## Recommended next phase

### Phase 2 should be Inventory + Procurement

Build these before Finance because Finance needs correct stock and purchase values.

Minimum Phase 2 deliverables:

1. Item stock ledger
2. Warehouse/bin master
3. Stock lot and expiry tracking
4. GRN screen
5. Stock transfer screen
6. Store issue posting into inventory transactions
7. Stock adjustment with approval
8. Purchase requisition
9. Purchase order
10. Supplier invoice matching skeleton
11. Inventory valuation report
12. Low stock and expiry alerts

### Then Phase 3 should be Finance

Finance must consume confirmed transactions from sales, purchase, inventory and production:

1. Chart of accounts
2. Cost centers
3. Customer invoice posting after delivery/OTP
4. Supplier invoice posting after PO/GRN matching
5. AR/AP aging
6. VAT report
7. Recipe profitability
8. Customer profitability
9. Inventory valuation posting
10. Trial balance / P&L skeleton

## Important technical notes for developer

- Do not create new isolated CSS for each screen.
- Use the global Phoenix UI classes and the appended `user.min.css` layer.
- Keep `base.html` and `partials` as the layout foundation.
- Before adding a new module, create model, service, route, template and permissions together.
- Every query should be checked for company/brand scope as the system is intended for multiple companies and multiple brands.
- Every transaction should have status, approval, audit log and created/updated user fields.
