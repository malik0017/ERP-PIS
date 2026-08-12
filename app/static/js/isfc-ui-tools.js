/* =========================================================================
 * ISFC PIMS — isfc-ui-tools.js (Batch 94)
 *
 * Two system-wide behaviours that were on the pending list, delivered as
 * ONE auto-attaching file rather than as edits to 120+ templates:
 *
 *   1. SORTABLE TABLE COLUMNS — every <table> on every page becomes
 *      click-to-sort, with no markup changes anywhere.
 *   2. FULLSCREEN PREVIEW — every card/chart gets an expand control.
 *
 * Loaded once from layouts/base.html, after phoenix.js and isfc-charts.js
 * (asset order matters — see the load-order note in that file).
 *
 * RTL and dark mode: uses only Phoenix/Bootstrap classes and logical CSS
 * properties, no hard-coded left/right or colours.
 * ========================================================================= */
(function () {
  'use strict';

  /* ---------------------------------------------------------------------
   * 1. SORTABLE COLUMNS
   * ------------------------------------------------------------------- */

  // Opt OUT, not opt in. Making this opt-in would have meant touching every
  // template — the exact "broad, touches many templates" problem that kept
  // this on the pending list. Tables that must not be sorted carry
  // data-no-sort (entry grids where row order is the data: PO line entry,
  // recipe step order, anything with inputs the user is filling in).
  function isSortable(table) {
    if (table.hasAttribute('data-no-sort')) return false;
    if (table.closest('[data-no-sort]')) return false;
    // A table the user is typing into is a form layout, not a data grid.
    if (table.querySelector('tbody input, tbody select, tbody textarea')) return false;
    var tbody = table.tBodies[0];
    if (!tbody || tbody.rows.length < 2) return false;
    return !!(table.tHead && table.tHead.rows.length);
  }

  function cellValue(row, idx) {
    var cell = row.cells[idx];
    if (!cell) return '';
    // data-sort wins when a template wants to sort by something other than
    // what's displayed (e.g. a badge showing "Pending" sorted by a stage no).
    if (cell.hasAttribute('data-sort')) return cell.getAttribute('data-sort');
    return (cell.innerText || cell.textContent || '').trim();
  }

  // Numbers, dates and text all need different comparisons. Getting this
  // wrong is why naive sorters put "100" before "9" and 01-02-2026 before
  // 15-01-2026.
  function parseValue(raw) {
    if (raw === '' || raw === '—' || raw === '-') return { t: 'empty', v: 0 };

    // Strip thousands separators, currency and Arabic-Indic digits before
    // deciding whether this is a number.
    var norm = raw.replace(/[\u0660-\u0669]/g, function (d) {
      return String.fromCharCode(d.charCodeAt(0) - 0x0660 + 48);
    }).replace(/[\u06F0-\u06F9]/g, function (d) {
      return String.fromCharCode(d.charCodeAt(0) - 0x06F0 + 48);
    });

    var numeric = norm.replace(/[,\s]/g, '').replace(/^[^\d\-+.]*/, '').replace(/[^\d.\-+eE]*$/, '');
    if (numeric !== '' && !isNaN(numeric) && /\d/.test(numeric)) {
      return { t: 'num', v: parseFloat(numeric) };
    }

    // ISO first (yyyy-mm-dd), then dd-mm-yyyy / dd/mm/yyyy which is the
    // system's configured display format.
    var iso = norm.match(/^(\d{4})[-/](\d{1,2})[-/](\d{1,2})/);
    if (iso) return { t: 'num', v: Date.UTC(+iso[1], +iso[2] - 1, +iso[3]) };
    var dmy = norm.match(/^(\d{1,2})[-/](\d{1,2})[-/](\d{4})/);
    if (dmy) return { t: 'num', v: Date.UTC(+dmy[3], +dmy[2] - 1, +dmy[1]) };

    return { t: 'str', v: norm.toLowerCase() };
  }

  function sortTable(table, idx, dir) {
    var tbody = table.tBodies[0];
    var rows = Array.prototype.slice.call(tbody.rows);

    // Rows spanning the full width are "no results" / group headers, not
    // data — they'd otherwise be shuffled into the middle of the results.
    var data = rows.filter(function (r) {
      return !(r.cells.length === 1 && r.cells[0].hasAttribute('colspan'));
    });
    var keep = rows.filter(function (r) { return data.indexOf(r) === -1; });

    var decorated = data.map(function (row, i) {
      return { row: row, i: i, k: parseValue(cellValue(row, idx)) };
    });

    decorated.sort(function (a, b) {
      // Blanks always sink to the bottom regardless of direction — an empty
      // delivery date is "unknown", not "earliest".
      if (a.k.t === 'empty' && b.k.t !== 'empty') return 1;
      if (b.k.t === 'empty' && a.k.t !== 'empty') return -1;

      var r;
      if (a.k.t === 'num' && b.k.t === 'num') {
        r = a.k.v - b.k.v;
      } else {
        // localeCompare with the page language so Arabic sorts correctly.
        r = String(a.k.v).localeCompare(String(b.k.v),
          document.documentElement.lang || undefined, { numeric: true, sensitivity: 'base' });
      }
      // Stable: equal keys keep their original order instead of jumping
      // around on every re-sort.
      return r !== 0 ? r * dir : a.i - b.i;
    });

    var frag = document.createDocumentFragment();
    decorated.forEach(function (d) { frag.appendChild(d.row); });
    keep.forEach(function (r) { frag.appendChild(r); });
    tbody.appendChild(frag);
  }

  function attachSorting(table) {
    var headRow = table.tHead.rows[table.tHead.rows.length - 1];
    Array.prototype.forEach.call(headRow.cells, function (th, idx) {
      if (th.hasAttribute('data-no-sort') || th.colSpan > 1) return;
      if (!(th.innerText || '').trim()) return;   // action / icon columns

      th.classList.add('isfc-sortable');
      th.style.cursor = 'pointer';
      th.style.userSelect = 'none';
      th.setAttribute('role', 'button');
      th.setAttribute('tabindex', '0');
      th.setAttribute('title', 'Click to sort');

      var caret = document.createElement('span');
      caret.className = 'isfc-sort-caret ms-1 opacity-25';
      caret.innerHTML = '&#8645;';
      th.appendChild(caret);

      function go() {
        var dir = th.getAttribute('data-dir') === 'asc' ? -1 : 1;
        Array.prototype.forEach.call(headRow.cells, function (o) {
          o.removeAttribute('data-dir');
          var c = o.querySelector('.isfc-sort-caret');
          if (c) { c.innerHTML = '&#8645;'; c.className = 'isfc-sort-caret ms-1 opacity-25'; }
        });
        th.setAttribute('data-dir', dir === 1 ? 'asc' : 'desc');
        caret.innerHTML = dir === 1 ? '&#9650;' : '&#9660;';
        caret.className = 'isfc-sort-caret ms-1 opacity-75';
        sortTable(table, idx, dir);
      }

      th.addEventListener('click', go);
      th.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); go(); }
      });
    });
  }

  /* ---------------------------------------------------------------------
   * 2. FULLSCREEN PREVIEW
   * ------------------------------------------------------------------- */

  var fsHost = null;
  var fsOrigin = null;
  var fsPlaceholder = null;

  function exitFullscreen() {
    if (!fsHost || !fsOrigin) return;
    fsPlaceholder.parentNode.replaceChild(fsOrigin, fsPlaceholder);
    fsHost.remove();
    fsHost = null; fsOrigin = null; fsPlaceholder = null;
    document.body.style.overflow = '';
    // ECharts instances measure their container on init and never re-measure
    // on their own — without this, every chart comes back from fullscreen at
    // the wrong size. Same root cause as the RTL zero-width chart bug.
    window.dispatchEvent(new Event('resize'));
  }

  function enterFullscreen(card) {
    if (fsHost) exitFullscreen();

    fsOrigin = card;
    fsPlaceholder = document.createElement('div');
    card.parentNode.replaceChild(fsPlaceholder, card);

    fsHost = document.createElement('div');
    fsHost.className = 'isfc-fs-host';
    fsHost.setAttribute('role', 'dialog');
    fsHost.setAttribute('aria-modal', 'true');

    var bar = document.createElement('div');
    bar.className = 'isfc-fs-bar';
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'btn btn-sm btn-phoenix-secondary';
    btn.innerHTML = '<i class="bi bi-fullscreen-exit me-1"></i>Close';
    btn.addEventListener('click', exitFullscreen);
    bar.appendChild(btn);

    var body = document.createElement('div');
    body.className = 'isfc-fs-body';
    body.appendChild(card);

    fsHost.appendChild(bar);
    fsHost.appendChild(body);
    document.body.appendChild(fsHost);
    document.body.style.overflow = 'hidden';
    btn.focus();
    window.dispatchEvent(new Event('resize'));
  }

  function attachFullscreen(card) {
    if (card.querySelector(':scope > .isfc-fs-btn')) return;
    var header = card.querySelector(':scope > .card-header');
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'btn btn-sm btn-link p-0 isfc-fs-btn';
    btn.title = 'Expand to fullscreen';
    btn.innerHTML = '<i class="bi bi-arrows-fullscreen"></i>';
    btn.addEventListener('click', function (e) {
      e.preventDefault(); e.stopPropagation();
      enterFullscreen(card);
    });

    if (header) {
      header.classList.add('d-flex', 'justify-content-between', 'align-items-center');
      header.appendChild(btn);
    } else {
      card.classList.add('position-relative');
      btn.classList.add('isfc-fs-float');
      card.appendChild(btn);
    }
  }

  function wantsFullscreen(card) {
    if (card.hasAttribute('data-no-fullscreen')) return false;
    if (card.closest('.isfc-fs-host')) return false;
    if (card.querySelector('[data-echart], .echart, canvas, table')) return true;
    return card.hasAttribute('data-fullscreen');
  }

  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape' && fsHost) exitFullscreen();
  });

  /* ---------------------------------------------------------------------
   * Styles — injected rather than added to a stylesheet so this stays a
   * single drop-in file with no CSS build step.
   * ------------------------------------------------------------------- */
  function injectStyles() {
    if (document.getElementById('isfc-ui-tools-css')) return;
    var css = document.createElement('style');
    css.id = 'isfc-ui-tools-css';
    css.textContent =
      '.isfc-sortable:hover{background-color:var(--phoenix-secondary-bg,rgba(0,0,0,.04))}' +
      '.isfc-sortable .isfc-sort-caret{font-size:.7em;display:inline-block}' +
      '.isfc-fs-host{position:fixed;inset:0;z-index:2000;background:var(--phoenix-body-bg,#fff);' +
      'display:flex;flex-direction:column;padding:1rem;overflow:auto}' +
      '.isfc-fs-bar{display:flex;justify-content:flex-end;margin-block-end:.75rem;flex:0 0 auto}' +
      '.isfc-fs-body{flex:1 1 auto;min-height:0}' +
      '.isfc-fs-body > .card{height:100%;margin:0!important}' +
      '.isfc-fs-body .card-body{height:calc(100% - 3.5rem);overflow:auto}' +
      '.isfc-fs-btn{opacity:.4;line-height:1;color:inherit}' +
      '.isfc-fs-btn:hover{opacity:1;color:inherit}' +
      '.isfc-fs-float{position:absolute;inset-block-start:.5rem;inset-inline-end:.75rem;z-index:5}' +
      '@media print{.isfc-fs-btn,.isfc-sort-caret{display:none!important}}';
    document.head.appendChild(css);
  }

  function init(root) {
    injectStyles();
    (root || document).querySelectorAll('table').forEach(function (t) {
      if (t.dataset.isfcSorted) return;
      if (!isSortable(t)) return;
      t.dataset.isfcSorted = '1';
      try { attachSorting(t); } catch (e) { /* never break a page over a sort */ }
    });
    (root || document).querySelectorAll('.card').forEach(function (c) {
      if (c.dataset.isfcFs) return;
      if (!wantsFullscreen(c)) return;
      c.dataset.isfcFs = '1';
      try { attachFullscreen(c); } catch (e) { /* same */ }
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { init(); });
  } else {
    init();
  }

  // Re-scan when content arrives later (modals, AJAX-refreshed panels).
  window.isfcUiTools = { init: init, exitFullscreen: exitFullscreen };
})();
