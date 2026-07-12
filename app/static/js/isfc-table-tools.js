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

  function text(el) { return (el.innerText || el.textContent || '').trim(); }

  function tableToRows(table, visibleOnly) {
    var rows = [];
    table.querySelectorAll('tr').forEach(function (tr) {
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
    // BOM so Arabic text opens correctly in Excel
    var blob = new Blob(['\uFEFF' + rows.join('\r\n')], { type: 'text/csv;charset=utf-8;' });
    var a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = (name || 'export') + '.csv';
    document.body.appendChild(a); a.click(); a.remove();
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
    w.document.write('<html dir="' + dir + '"><head><title>' + (title || 'Print') + '</title>' +
      '<style>body{font-family:"Nunito Sans",Arial,sans-serif;padding:24px;color:#1f2937}' +
      'h2{margin:0 0 4px}p{margin:0 0 16px;color:#6b7a90;font-size:12px}' +
      'table{border-collapse:collapse;width:100%;font-size:12px}' +
      'th,td{border:1px solid #d8e2ef;padding:6px 9px;text-align:' + (dir === 'rtl' ? 'right' : 'left') + '}' +
      'th{background:#f5f8fc;text-transform:uppercase;font-size:10px;letter-spacing:.04em}' +
      '.badge{border:1px solid #cbd5e1;border-radius:8px;padding:1px 6px;font-size:10px}</style></head><body>' +
      '<h2>' + (title || '') + '</h2><p>' + new Date().toLocaleString() + ' · ISFC ERP</p>' +
      table.outerHTML + '</body></html>');
    w.document.close(); w.focus();
    setTimeout(function () { w.print(); w.close(); }, 300);
  }

  function buildToolbar(table) {
    var wrap = document.createElement('div');
    wrap.className = 'd-flex flex-wrap align-items-center gap-2 mb-2 isfc-table-tools';
    var title = table.getAttribute('data-isfc-title') || document.title;

    // quick filter
    var search = document.createElement('input');
    search.className = 'form-control form-control-sm';
    search.style.maxWidth = '220px';
    search.placeholder = table.getAttribute('data-isfc-search-placeholder') || 'Filter rows...';
    search.addEventListener('input', function () {
      var q = search.value.toLowerCase();
      table.querySelectorAll('tbody tr').forEach(function (tr) {
        tr.style.display = text(tr).toLowerCase().indexOf(q) > -1 ? '' : 'none';
      });
    });
    wrap.appendChild(search);

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
