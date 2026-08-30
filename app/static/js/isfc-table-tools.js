/* =========================================================================
 * ISFC PIMS — isfc-table-tools.js (Batch 5)
 * Zero-dependency table toolbar, styled with Phoenix classes only.
 *
 * Usage: add data-isfc-table to any <table>. A toolbar is injected above it:
 *   [ quick filter ] [ Columns ▾ ] [ CSV ] [ Copy ] [ Print ]
 * Works in LTR and RTL, light and dark mode (uses theme variables/classes).
 * ========================================================================= */
(function () {
  'use strict';

  // Batch 141: inject the per-column-filter styling once, so the tool is
  // self-contained (no separate CSS deploy needed).
  (function injectCss() {
    if (document.getElementById('isfc-colfilter-css')) return;
    var st = document.createElement('style');
    st.id = 'isfc-colfilter-css';
    st.textContent =
      '.isfc-colfilter-row th{padding:4px 6px;background:#f4f8fc;}' +
      '.isfc-colfilter{width:100%;min-width:70px;border:1px solid #cbdbea;border-radius:6px;' +
      'padding:3px 7px;font-size:12px;font-weight:500;}' +
      '[data-bs-theme="dark"] .isfc-colfilter-row th{background:rgba(255,255,255,.03);}' +
      '[data-bs-theme="dark"] .isfc-colfilter{background:#0f1c2e;color:#e7eef7;border-color:#2a3a4f;}';
    document.head.appendChild(st);
  })();

  function text(el) { return (el.innerText || el.textContent || '').trim(); }

  function tableToRows(table, visibleOnly) {
    var rows = [];
    table.querySelectorAll('tr').forEach(function (tr) {
      if (tr.classList.contains('isfc-colfilter-row')) return; // Batch 141: never export the filter row
      if (tr.offsetParent === null && visibleOnly) return; // filtered out
      var cells = [];
      tr.querySelectorAll('th,td').forEach(function (td) {
        if (visibleOnly && td.classList.contains('d-none')) return;
        cells.push(text(td).replace(/\s+/g, ' '));
      });
      if (cells.length) rows.push(cells);
    });
    return rows;
  }

  function downloadCSV(table, name) {
    var rows = tableToRows(table, true).map(function (r) {
      return r.map(function (c) { return '"' + c.replace(/"/g, '""') + '"'; }).join(',');
    });
    // Batch 139: prepend an order/customer context line so the CSV is
    // self-describing (matches the PDF/print subtitle).
    var sub = table.getAttribute('data-isfc-subtitle');
    if (sub) rows.unshift('"' + sub.replace(/"/g, '""') + '"');
    // BOM so Arabic text opens correctly in Excel
    var blob = new Blob(['\uFEFF' + rows.join('\r\n')], { type: 'text/csv;charset=utf-8;' });
    var a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = (name || 'export') + '.csv';
    document.body.appendChild(a); a.click(); a.remove();
  }

  // ---- PDF export (Batch 120) -------------------------------------------
  // Lazy-load jsPDF + autoTable once, on first click, so pages that never
  // export PDF pay zero cost. Reuses tableToRows(table, true) so the PDF
  // honours the quick-filter AND the Columns show/hide state, exactly like CSV.
  var _pdfLibReady = null;
  function loadScript(src) {
    return new Promise(function (resolve, reject) {
      var s = document.createElement('script');
      s.src = src; s.async = true;
      s.onload = resolve; s.onerror = function () { reject(new Error('load failed: ' + src)); };
      document.head.appendChild(s);
    });
  }
  function ensurePdfLib() {
    if (_pdfLibReady) return _pdfLibReady;
    if (window.jspdf && window.jspdf.jsPDF) { _pdfLibReady = Promise.resolve(); return _pdfLibReady; }
    _pdfLibReady = loadScript('https://cdnjs.cloudflare.com/ajax/libs/jspdf/2.5.1/jspdf.umd.min.js')
      .then(function () {
        return loadScript('https://cdnjs.cloudflare.com/ajax/libs/jspdf-autotable/3.8.2/jspdf.plugin.autotable.min.js');
      });
    return _pdfLibReady;
  }
  function downloadPDF(table, name, title, btn) {
    var old = btn ? btn.innerHTML : '';
    if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span>'; }
    ensurePdfLib().then(function () {
      var jsPDF = window.jspdf.jsPDF;
      // Batch 122: the jsPDF core font (Helvetica) is Latin-1 only. Sortable
      // headers carry sort-arrow glyphs (▲▼↑↓⇅) plus non-breaking / zero-width
      // spaces that rendered as garbage ("!Å") in the PDF header. Strip those
      // and any remaining non-Latin1 codepoints for the PDF output only (CSV /
      // clipboard keep full unicode).
      function pdfClean(s) {
        return String(s == null ? '' : s)
          .replace(/[\u25B2\u25BC\u25B4\u25BE\u2191\u2193\u21C5\u2195\uFFFD]/g, '') // sort arrows
          .replace(/[\u00A0\u200B\u200C\u200D\uFEFF]/g, ' ')                        // nbsp / zero-width
          .replace(/[^\x00-\xFF]/g, '')                                              // non-Latin1
          .replace(/\s+/g, ' ')
          .trim();
      }
      var rawRows = tableToRows(table, true);
      var rows = rawRows.map(function (r) { return r.map(pdfClean); });
      if (!rows.length) { throw new Error('nothing to export'); }
      var head = [rows[0]];
      var body = rows.slice(1);
      var landscape = rows[0].length > 6;
      var doc = new jsPDF({ orientation: landscape ? 'landscape' : 'portrait', unit: 'pt', format: 'a4' });
      var dir = document.documentElement.getAttribute('dir') || 'ltr';
      doc.setFontSize(13);
      doc.text(pdfClean(title || 'Export'), 40, 34);
      doc.setFontSize(8); doc.setTextColor(120);
      // Batch 139: optional subtitle line (e.g. "Order … · Customer …") so every
      // exported document is self-describing.
      var _sub = table.getAttribute('data-isfc-subtitle');
      var _yStart = 60;
      if (_sub) { doc.text(pdfClean(_sub), 40, 48); doc.text(new Date().toLocaleString() + '  \u00b7  ISFC ERP', 40, 60); _yStart = 74; }
      else { doc.text(new Date().toLocaleString() + '  \u00b7  ISFC ERP', 40, 48); }
      doc.autoTable({
        head: head, body: body, startY: _yStart,
        styles: { fontSize: 7, cellPadding: 3, overflow: 'linebreak',
                  halign: dir === 'rtl' ? 'right' : 'left' },
        headStyles: { fillColor: [71, 85, 105], textColor: 255, fontSize: 7 },
        alternateRowStyles: { fillColor: [245, 248, 252] },
        margin: { left: 40, right: 40 },
      });
      doc.save((name || 'export') + '.pdf');
    }).catch(function (e) {
      console.error('PDF export failed', e);
      alert('PDF export is unavailable (could not load the PDF library). Please check your connection.');
    }).finally(function () {
      if (btn) { btn.disabled = false; btn.innerHTML = old; }
    });
  }

  function copyTable(table, btn) {
    var tsv = tableToRows(table, true).map(function (r) { return r.join('\t'); }).join('\n');
    navigator.clipboard.writeText(tsv).then(function () {
      var old = btn.innerHTML;
      btn.innerHTML = '<i class="bi bi-check2"></i>';
      setTimeout(function () { btn.innerHTML = old; }, 1200);
    });
  }

  function printTable(table, title) {
    var w = window.open('', '_blank');
    var dir = document.documentElement.getAttribute('dir') || 'ltr';
    var sub = table.getAttribute('data-isfc-subtitle');
    w.document.write('<html dir="' + dir + '"><head><title>' + (title || 'Print') + '</title>' +
      '<style>body{font-family:"Nunito Sans",Arial,sans-serif;padding:24px;color:#1f2937}' +
      'h2{margin:0 0 4px}p{margin:0 0 16px;color:#6b7a90;font-size:12px}' +
      'table{border-collapse:collapse;width:100%;font-size:12px}' +
      'th,td{border:1px solid #d8e2ef;padding:6px 9px;text-align:' + (dir === 'rtl' ? 'right' : 'left') + '}' +
      'th{background:#f5f8fc;text-transform:uppercase;font-size:10px;letter-spacing:.04em}' +
      '.badge{border:1px solid #cbd5e1;border-radius:8px;padding:1px 6px;font-size:10px}</style></head><body>' +
      '<h2>' + (title || '') + '</h2>' +
      (sub ? '<p style="font-weight:600;color:#334">' + sub + '</p>' : '') +
      '<p>' + new Date().toLocaleString() + ' · ISFC ERP</p>' +
      table.outerHTML + '</body></html>');
    w.document.close(); w.focus();
    setTimeout(function () { w.print(); w.close(); }, 300);
  }

  // Batch 141: per-column filters, generalised from the hand-copied Batch 137
  // versions. Opt in with data-isfc-colfilters on the table. Injects a filter
  // input under each header; inputs combine with AND and cooperate with the quick
  // filter and the Columns show/hide state. A single applyFilters() is the one
  // place that decides row visibility, so the three filtering mechanisms never
  // fight each other.
  function attachFiltering(table) {
    var thead = table.querySelector('thead');
    var headerRow = thead ? thead.querySelector('tr') : null;
    var wantCol = table.hasAttribute('data-isfc-colfilters');
    var colInputs = [];

    if (wantCol && headerRow) {
      var fRow = document.createElement('tr');
      fRow.className = 'isfc-colfilter-row';
      headerRow.querySelectorAll('th').forEach(function (th, idx) {
        var cell = document.createElement('th');
        // Let a column skip its filter with data-isfc-nofilter on the <th>.
        if (th.hasAttribute('data-isfc-nofilter')) { fRow.appendChild(cell); return; }
        var inp = document.createElement('input');
        inp.type = 'text';
        inp.className = 'isfc-colfilter';
        inp.setAttribute('data-col', idx);
        inp.placeholder = '\u2315';
        inp.addEventListener('keyup', applyFilters);
        inp.addEventListener('change', applyFilters);
        cell.appendChild(inp);
        colInputs.push(inp);
        fRow.appendChild(cell);
      });
      thead.appendChild(fRow);
    }

    function applyFilters() {
      var q = (table.__isfcQuick || '').toLowerCase();
      var terms = colInputs.map(function (i) {
        return { c: parseInt(i.getAttribute('data-col'), 10), v: (i.value || '').toLowerCase().trim() };
      }).filter(function (t) { return t.v; });
      table.querySelectorAll('tbody tr').forEach(function (tr) {
        var rowText = text(tr).toLowerCase();
        var ok = !q || rowText.indexOf(q) > -1;
        if (ok && terms.length) {
          var cells = tr.children;
          ok = terms.every(function (t) {
            var cell = cells[t.c];
            return cell && (cell.innerText || '').toLowerCase().indexOf(t.v) > -1;
          });
        }
        tr.style.display = ok ? '' : 'none';
      });
    }
    // expose so the quick-filter input can reuse the same pipeline
    table.__isfcApplyFilters = applyFilters;
    return applyFilters;
  }

  function buildToolbar(table) {
    var wrap = document.createElement('div');
    wrap.className = 'd-flex flex-wrap align-items-center gap-2 mb-2 isfc-table-tools';
    var title = table.getAttribute('data-isfc-title') || document.title;

    // per-column filters (opt-in) — set up first so the quick filter can reuse it
    var applyFilters = attachFiltering(table);

    // quick filter
    var search = document.createElement('input');
    search.className = 'form-control form-control-sm';
    search.style.maxWidth = '220px';
    search.placeholder = table.getAttribute('data-isfc-search-placeholder') || 'Filter rows...';
    search.addEventListener('input', function () {
      table.__isfcQuick = search.value;
      applyFilters();
    });
    if (table.getAttribute('data-isfc-search') !== 'false') wrap.appendChild(search);

    var spacer = document.createElement('div');
    spacer.className = 'ms-auto d-flex gap-2';
    wrap.appendChild(spacer);

    // Columns dropdown
    var dd = document.createElement('div');
    dd.className = 'dropdown';
    dd.innerHTML = '<button class="btn btn-sm btn-phoenix-secondary dropdown-toggle" data-bs-toggle="dropdown" data-bs-auto-close="outside"><i class="bi bi-layout-three-columns me-1"></i>Columns</button>' +
      '<ul class="dropdown-menu dropdown-menu-end p-2" style="min-width:220px;max-height:280px;overflow:auto"></ul>';
    var menu = dd.querySelector('ul');
    var headers = table.querySelectorAll('thead th');
    headers.forEach(function (th, idx) {
      var li = document.createElement('li');
      li.innerHTML = '<label class="dropdown-item d-flex align-items-center gap-2 mb-0 fs-9">' +
        '<input type="checkbox" class="form-check-input mt-0" checked> ' + (text(th) || ('Col ' + (idx + 1))) + '</label>';
      li.querySelector('input').addEventListener('change', function (e) {
        var show = e.target.checked;
        table.querySelectorAll('tr').forEach(function (tr) {
          var cell = tr.children[idx];
          if (cell) cell.classList.toggle('d-none', !show);
        });
      });
      menu.appendChild(li);
    });
    spacer.appendChild(dd);

    // CSV
    var csvBtn = document.createElement('button');
    csvBtn.className = 'btn btn-sm btn-phoenix-primary';
    csvBtn.innerHTML = '<i class="bi bi-filetype-csv me-1"></i>CSV';
    csvBtn.addEventListener('click', function () { downloadCSV(table, title.replace(/\W+/g, '_')); });
    spacer.appendChild(csvBtn);

    // PDF (Batch 120) — same data as CSV, honours filter + column visibility
    var pdfBtn = document.createElement('button');
    pdfBtn.className = 'btn btn-sm btn-phoenix-danger';
    pdfBtn.innerHTML = '<i class="bi bi-filetype-pdf me-1"></i>PDF';
    pdfBtn.title = 'Download as PDF';
    pdfBtn.addEventListener('click', function () {
      downloadPDF(table, title.replace(/\W+/g, '_'), title, pdfBtn);
    });
    spacer.appendChild(pdfBtn);

    // Copy
    var copyBtn = document.createElement('button');
    copyBtn.className = 'btn btn-sm btn-phoenix-secondary';
    copyBtn.innerHTML = '<i class="bi bi-clipboard"></i>';
    copyBtn.title = 'Copy to clipboard';
    copyBtn.addEventListener('click', function () { copyTable(table, copyBtn); });
    spacer.appendChild(copyBtn);

    // Print (users can Save as PDF from the print dialog)
    var printBtn = document.createElement('button');
    printBtn.className = 'btn btn-sm btn-phoenix-secondary';
    printBtn.innerHTML = '<i class="bi bi-printer"></i>';
    printBtn.title = 'Print / Save as PDF';
    printBtn.addEventListener('click', function () { printTable(table, title); });
    spacer.appendChild(printBtn);

    var host = table.closest('.table-responsive') || table;
    host.parentNode.insertBefore(wrap, host);
  }

  function initTables() {
    document.querySelectorAll('table[data-isfc-table]:not([data-isfc-ready])').forEach(function (t) {
      t.setAttribute('data-isfc-ready', '1');
      buildToolbar(t);
    });
  }

  // RTL chart safety: after load (and after any RTL/theme flip), force every
  // ECharts instance to resize — this fixes "no charts show in RTL".
  function resizeCharts() {
    if (!window.echarts) return;
    document.querySelectorAll('[_echarts_instance_], .echart-chart, [class*="echart"]').forEach(function (el) {
      try {
        var inst = window.echarts.getInstanceByDom(el);
        if (inst) inst.resize();
      } catch (e) { /* noop */ }
    });
  }

  document.addEventListener('DOMContentLoaded', function () {
    initTables();
    setTimeout(resizeCharts, 400);
    setTimeout(resizeCharts, 1200);
  });
  window.addEventListener('resize', function () { setTimeout(resizeCharts, 150); });
  window.isfcTableTools = { init: initTables, resizeCharts: resizeCharts };
})();
