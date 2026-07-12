# ISFC33 Updated Files Guide

Copy only these files into your current project, keeping the same folder paths.

## Updated files

1. `app/static/css/user.min.css`
   - Removed sidebar zoom problem by keeping the left sidebar at normal height.
   - Content still uses compact 80% view, but content margin is corrected so it does not touch/overlap the sidebar.
   - Fixed global table toolbar layout so row count, search, Print and CSV stay in one flex row.
   - Added table controls:
     - `data-isfc-tools="none"` = no automatic row/search/print/csv toolbar.
     - `data-isfc-search="false"` = hide only automatic table search.
     - `data-isfc-export="false"` = hide Print and CSV buttons.
   - Added one-line filter grid helper: `filter-grid-line`.
   - Standardized hero/header gradient colors for BOM and other pages.

2. `app/static/js/phoenix.js`
   - Automatic table toolbar now respects:
     - `data-isfc-tools="none"`
     - `data-isfc-search="false"`
     - `data-isfc-export="false"`
   - Table toolbar row count still works, but empty rows are ignored.
   - Print and CSV remain available unless hidden by attributes.

3. `app/templates/partials/sidebar.html`
   - Upload Master Data menu is now visible only for Admin, Super Admin, or Administrator.
   - Dashboard top search is now usable: it filters quick links, opens the result dropdown, and Enter opens the first visible result.

4. `app/modules/masters/routes.py`
   - `/masters/upload` is now admin/super-admin/administrator only.
   - `/masters/upload/{master_type}` is scoped role-based upload for a single master type.
   - `/masters/template/{master_type}` now checks permission before downloading a template.
   - Non-admin users cannot use the full master-type dropdown upload.

5. `app/templates/masters/upload.html`
   - Admin view keeps full upload page with master-type dropdown.
   - Scoped user view shows one fixed master type only, with no dropdown.
   - Non-admin scoped upload hides Master Archive button.

6. `app/templates/masters/list.html`
   - Upload button opens full upload for admin only.
   - Upload button opens scoped upload page for non-admin users.
   - Archive button is shown only for admin/super-admin/administrator.

7. `app/templates/production/bakery_pastry.html`
   - Section Workload Filter changed to one-line professional layout like Store Issuance.

8. `app/templates/production/bom_report.html`
   - BOM report hero/header uses the same project hero style.
   - Automatic duplicate table search is hidden because this page already has its own BOM search.

9. `app/templates/production/section.html`
   - Kitchen section pages now hide duplicate automatic table search because the page already has its own order search.

10. `app/templates/production/section_order.html`
   - Section order line table hides duplicate automatic table search.

## How to control table search / Print / CSV yourself

Open the HTML template where the table exists and edit the `<table>` tag.

Show everything:
```html
<table class="table">
```

Hide only automatic search but keep row count, Print, CSV:
```html
<table class="table" data-isfc-search="false">
```

Hide Print and CSV but keep row count and search:
```html
<table class="table" data-isfc-export="false">
```

Hide the full automatic toolbar:
```html
<table class="table" data-isfc-tools="none">
```

You can also wrap a table inside this class to disable toolbar:
```html
<div class="no-isfc-table-tools">
  <table class="table">...</table>
</div>
```

## How to find which file controls a URL

Use this simple rule:

1. Check the browser URL, for example:
   `/production/section/Cutting`
2. Search this text or part of it inside `app/modules/*/routes.py`.
   Example command:
```bash
grep -R "production/section" -n app/modules
```
3. Open the route file that appears, usually:
   `app/modules/production/routes.py`
4. Inside the route, look for `render(request, "...")`.
   That tells you the template file.
   Example:
   `render(request, "production/section.html", {...})`
5. Then open:
   `app/templates/production/section.html`
6. Search exact screen text from browser, for example:
```bash
grep -R "Orders Waiting in" -n app/templates
```

## How to add a date filter only on selected pages

Do not add filters globally. Add them only in the target template form and route.

Template example:
```html
<form method="get" class="filter-grid-line mb-3">
  <input type="date" name="date_from" value="{{ filters.date_from or '' }}">
  <input type="date" name="date_to" value="{{ filters.date_to or '' }}">
  <button type="submit">Filter</button>
</form>
```

Route example:
```python
date_from: str | None = None,
date_to: str | None = None,
```

Then apply SQLAlchemy filters only in that route.
