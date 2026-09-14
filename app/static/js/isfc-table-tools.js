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

  // Batch 176 ROOT CAUSE — "Portions"/"Protein" columns export blank.
  //
  // Several screens (Sales Request "Requested Items", Packing "Recipe Pack
  // Detail", etc.) put an editable <input> directly inside an exportable
  // cell, with no visible sibling text. text(td) reads innerText/textContent
  // — and a plain <input> exposes NEITHER; its typed value lives only in the
  // DOM .value property. So every CSV, PDF, copy and print built from these
  // tables silently dropped that column, no matter what the user typed.
  //
  // cellValue() is the one place all four export paths read a cell from, so
  // this fixes the whole class of bug at once instead of per-page: if the
  // cell contains a form control, read its value; otherwise fall back to the
  // original text-based read so every plain cell keeps working exactly as
  // before.
  function cellValue(td) {
    var field = td.querySelector('input, select, textarea');
    if (field) {
      if (field.type === 'checkbox' || field.type === 'radio') {
        return field.checked ? (field.value || '\u2713') : '';
      }
      return (field.value == null ? '' : String(field.value)).trim();
    }
    return text(td);
  }

  function tableToRows(table, visibleOnly) {
    var rows = [];
    table.querySelectorAll('tr').forEach(function (tr) {
      if (tr.classList.contains('isfc-colfilter-row')) return; // Batch 141: never export the filter row
      if (tr.offsetParent === null && visibleOnly) return; // filtered out
      var cells = [];
      tr.querySelectorAll('th,td').forEach(function (td) {
        if (visibleOnly && td.classList.contains('d-none')) return;
        cells.push(cellValue(td).replace(/\s+/g, ' '));
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
  // --------------------------------------------------------------------------
  // Batch 222 — Arabic PDFs.
  //
  // jsPDF's built-in fonts have no Arabic glyphs, and Arabic also needs shaping
  // (letters change form by position) and bidi reordering. None of that belongs
  // in the browser. When the page is Arabic, or the table itself contains
  // Arabic, the rows are posted to /export/table-pdf and rendered server-side
  // with the Amiri font — the same engine that already produces the picking
  // list. Latin-only tables keep the instant client-side path.
  // --------------------------------------------------------------------------
  var AR_RE = /[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]/;

  function needsServerPdf(table, title) {
    var lang = (document.documentElement.getAttribute('lang') || '').toLowerCase();
    var dir = (document.documentElement.getAttribute('dir') || '').toLowerCase();
    if (lang.indexOf('ar') === 0 || dir === 'rtl') return true;
    return AR_RE.test((title || '') + ' ' + (table.innerText || '').slice(0, 4000));
  }

  function serverPdf(table, name, title, btn) {
    var head = [], body = [];
    var clone = document.createElement('div');
    clone.innerHTML = printableTable(table);      // controls stripped, filter row gone
    var t = clone.querySelector('table');
    (t.querySelectorAll('thead tr')[0] || { cells: [] }).cells &&
      Array.prototype.forEach.call(t.querySelectorAll('thead tr')[0].cells, function (th) {
        // Strip the sort-arrow glyphs the header carries in the DOM.
        head.push((th.textContent || '').replace(/[\u25B2\u25BC\u2191\u2193\u21C5]/g, '')
                                        .replace(/\s+/g, ' ').trim());
      });
    Array.prototype.forEach.call(t.querySelectorAll('tbody tr'), function (tr) {
      if (tr.style.display === 'none') return;    // respect the current filter
      var row = [];
      Array.prototype.forEach.call(tr.cells, function (td) {
        // Amiri has no dingbats: a tick copied from a checkbox renders as a
        // missing-glyph box in the PDF. Send words instead of symbols.
        row.push((td.innerText || '')
          .replace(/\u2713/g, 'Y').replace(/\u2717|\u2718/g, 'N')
          .replace(/\s+/g, ' ').trim());
      });
      if (row.length) body.push(row);
    });
    var meta = [];
    try { meta = JSON.parse(table.getAttribute('data-isfc-meta') || '[]'); } catch (e) { meta = []; }

    return fetch('/export/table-pdf', {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        title: title || name || 'Export',
        subtitle: table.getAttribute('data-isfc-subtitle') || '',
        meta: meta, head: head, body: body,
        lang: document.documentElement.getAttribute('lang') || 'ar',
        landscape: head.length > 7
      })
    }).then(function (r) {
      if (!r.ok) throw new Error(r.status);
      return r.blob();
    }).then(function (blob) {
      var a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = (name || 'export') + '.pdf';
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(function () { URL.revokeObjectURL(a.href); }, 4000);
    });
  }

  function downloadPDF(table, name, title, btn) {
    var old = btn ? btn.innerHTML : '';
    if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span>'; }
    if (needsServerPdf(table, title)) {
      serverPdf(table, name, title, btn)
        .catch(function () {
          // Server route unreachable: fall back to the browser print window,
          // which renders Arabic correctly (Batch 211) — better than a PDF
          // with the Arabic silently removed.
          printTable(table, title);
        })
        .then(function () { if (btn) { btn.disabled = false; btn.innerHTML = old; } });
      return;
    }
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
      // ----------------------------------------------------------------------
      // Batch 211 (Images 6, 8) — every table PDF in the system now prints in
      // the Bill of Quantity house style instead of black-on-white: a navy
      // title band, the order context as fact boxes, a coloured column header,
      // zebra rows, status cells tinted by meaning, and page numbers.
      //
      // Done HERE, in the shared generator, rather than per screen: this one
      // function produces the PDF for every `data-isfc-table` in the system, so
      // Store Issuance Lines, Butchery Consolidated and roughly forty other
      // exports pick up the same look from a single change.
      //
      // Context comes from data-isfc-meta='[["Customer","…"],["Delivery","…"]]'
      // on the table. Absent on most screens — the header simply collapses.
      // ----------------------------------------------------------------------
      var NAVY = [19, 41, 71], BLUE = [30, 91, 184], MUTED = [107, 122, 144];
      var pageW = doc.internal.pageSize.getWidth();
      var meta = [];
      try { meta = JSON.parse(table.getAttribute('data-isfc-meta') || '[]'); } catch (e) { meta = []; }

      doc.setFillColor(NAVY[0], NAVY[1], NAVY[2]);
      doc.rect(0, 0, pageW, 46, 'F');
      doc.setTextColor(255, 255, 255);
      doc.setFontSize(14); doc.setFont(undefined, 'bold');
      doc.text(pdfClean(title || 'Export'), 40, 24);
      doc.setFont(undefined, 'normal'); doc.setFontSize(8);
      var _sub = table.getAttribute('data-isfc-subtitle');
      if (_sub) doc.text(pdfClean(_sub), 40, 37);
      var stampTxt = new Date().toLocaleString() + '  \u00b7  ISFC PIMS';
      doc.text(stampTxt, pageW - 40 - doc.getTextWidth(stampTxt), 37);

      var _yStart = 62;
      if (meta.length) {
        // Fact boxes, four per row, mirroring the BOQ sheet's header strip.
        var perRow = 4, boxW = (pageW - 80 - (perRow - 1) * 8) / perRow, boxH = 26, bx = 40, by = 56;
        meta.forEach(function (m, i) {
          if (i && i % perRow === 0) { by += boxH + 6; bx = 40; }
          doc.setDrawColor(214, 222, 234); doc.setFillColor(255, 255, 255);
          doc.roundedRect(bx, by, boxW, boxH, 3, 3, 'FD');
          doc.setFontSize(6); doc.setTextColor(MUTED[0], MUTED[1], MUTED[2]);
          doc.text(pdfClean(String(m[0] || '')).toUpperCase(), bx + 5, by + 9);
          doc.setFontSize(8.5); doc.setTextColor(19, 41, 71);
          doc.text(pdfClean(String(m[1] == null ? '' : m[1])), bx + 5, by + 20);
          bx += boxW + 8;
        });
        _yStart = by + boxH + 10;
      }

      // Status-like cells get a tint so exceptions are findable on paper.
      var TINT = [
        [/(^|\s)short/i,            [253, 232, 232], [164, 35, 35]],
        [/excess|over.?issued/i,    [255, 241, 224], [180, 83, 9]],
        [/pending|waiting|not\s/i,  [255, 248, 224], [146, 96, 10]],
        [/issued|exact|passed|ok|complete|delivered|transferred/i, [226, 246, 233], [10, 122, 51]],
        [/reject|fail|delay|late/i, [253, 232, 232], [164, 35, 35]]
      ];
      doc.autoTable({
        head: head, body: body, startY: _yStart,
        styles: { fontSize: 7, cellPadding: 3.2, overflow: 'linebreak',
                  lineColor: [223, 231, 240], lineWidth: 0.4,
                  halign: dir === 'rtl' ? 'right' : 'left' },
        headStyles: { fillColor: NAVY, textColor: 255, fontSize: 7, fontStyle: 'bold' },
        alternateRowStyles: { fillColor: [247, 250, 253] },
        margin: { left: 40, right: 40, top: 56 },
        didParseCell: function (d) {
          if (d.section !== 'body') return;
          var txt = String(d.cell.raw == null ? '' : d.cell.raw).trim();
          if (!txt || txt.length > 28) return;
          for (var i = 0; i < TINT.length; i++) {
            if (TINT[i][0].test(txt)) {
              d.cell.styles.fillColor = TINT[i][1];
              d.cell.styles.textColor = TINT[i][2];
              d.cell.styles.fontStyle = 'bold';
              return;
            }
          }
        },
        didDrawPage: function (d) {
          var n = doc.internal.getNumberOfPages();
          doc.setFontSize(7); doc.setTextColor(MUTED[0], MUTED[1], MUTED[2]);
          doc.text('Generated by ISFC PIMS', 40, doc.internal.pageSize.getHeight() - 18);
          var pg = 'Page ' + d.pageNumber + ' / ' + n;
          doc.text(pg, pageW - 40 - doc.getTextWidth(pg), doc.internal.pageSize.getHeight() - 18);
        }
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

  function printableTable(table) {
    // Batch 211: a printed sheet is a document, not a form. The live table
    // carries checkboxes, quantity inputs, section dropdowns, a per-column
    // filter row and an action column — all meaningless on paper and all
    // previously printed as-is. Clone it, replace each control with the value
    // it currently holds, and drop the filter row and action column.
    var clone = table.cloneNode(true);
    clone.querySelectorAll('input, select, textarea').forEach(function (el) {
      var span = document.createElement('span');
      if (el.type === 'checkbox' || el.type === 'radio') {
        span.textContent = el.checked ? '\u2713' : '';
      } else if (el.tagName === 'SELECT') {
        span.textContent = el.options.length && el.selectedIndex >= 0
          ? el.options[el.selectedIndex].text : '';
      } else {
        span.textContent = el.value || '';
      }
      el.parentNode.replaceChild(span, el);
    });
    // The filter row is the header row whose cells only ever held inputs.
    clone.querySelectorAll('thead tr').forEach(function (tr) {
      if (tr.classList.contains('isfc-filter-row') ||
          (tr.querySelectorAll('th').length &&
           !Array.prototype.some.call(tr.querySelectorAll('th'), function (th) {
             return (th.textContent || '').trim().length;
           }))) {
        tr.parentNode.removeChild(tr);
      }
    });
    // Drop columns marked as actions / no-print, header and body together.
    var drop = [];
    clone.querySelectorAll('thead tr:first-child th').forEach(function (th, i) {
      if (th.classList.contains('si-action-col') || th.hasAttribute('data-isfc-noprint') ||
          /^(action|save)s?$/i.test((th.textContent || '').trim())) drop.push(i);
    });
    if (drop.length) {
      clone.querySelectorAll('tr').forEach(function (tr) {
        drop.slice().reverse().forEach(function (i) {
          if (tr.cells[i]) tr.deleteCell(i);
        });
      });
    }
    return clone.outerHTML;
  }

  function printTable(table, title) {
    // Batch 211: the print view now uses the SAME stylesheet as every other
    // ISFC document (app/static/css/isfc-report.css, Batch 209), so "Print"
    // and "PDF" from a table look like the Bill of Quantity rather than a bare
    // browser table. Meta boxes come from data-isfc-meta, same as the PDF.
    var w = window.open('', '_blank');
    var dir = document.documentElement.getAttribute('dir') || 'ltr';
    var sub = table.getAttribute('data-isfc-subtitle');
    var meta = [];
    try { meta = JSON.parse(table.getAttribute('data-isfc-meta') || '[]'); } catch (e) { meta = []; }
    function esc(v) {
      return String(v == null ? '' : v).replace(/[&<>"]/g, function (c) {
        return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
      });
    }
    var facts = meta.length
      ? '<div class="rp-facts' + (meta.length > 4 ? ' rp-6' : '') + '">' +
        meta.map(function (m) { return '<div><b>' + esc(m[0]) + '</b>' + esc(m[1]) + '</div>'; }).join('') +
        '</div>'
      : '';
    var css = (document.querySelector('link[href*="isfc-report.css"]') || {}).href ||
              '/css/isfc-report.css';
    w.document.write(
      '<html dir="' + dir + '"><head><meta charset="utf-8"><title>' + esc(title || 'Print') + '</title>' +
      '<link rel="stylesheet" href="' + css + '">' +
      '<style>.rp-sheet{width:auto}.rp-status-short{background:#fde8e8;color:#a42323;font-weight:700}' +
      '.rp-status-excess{background:#fff1e0;color:#b45309;font-weight:700}' +
      '.rp-status-pending{background:#fff8e0;color:#92600a;font-weight:700}' +
      '.rp-status-ok{background:#e2f6e9;color:#0a7a33;font-weight:700}</style></head><body>' +
      '<div class="rp-sheet rp-wide"><div class="rp-head"><div><h1>' + esc(title || '') + '</h1>' +
      (sub ? '<div class="rp-sub">' + esc(sub) + '</div>' : '') + '</div>' +
      '<div class="rp-right"><div class="rp-sub">International Specialized Food Company</div>' +
      '<div class="rp-ref">' + new Date().toLocaleString() + ' \u00b7 ISFC PIMS</div></div></div>' +
      facts + printableTable(table) +
      '<div class="rp-stamp">Generated by ISFC PIMS</div></div>' +
      '<script>(function(){var re=[[/(^|\\s)short/i,"short"],[/excess|over.?issued/i,"excess"],' +
      '[/pending|waiting/i,"pending"],[/issued|exact|passed|delivered|transferred|complete/i,"ok"]];' +
      'document.querySelectorAll("tbody td").forEach(function(td){var t=(td.textContent||"").trim();' +
      'if(!t||t.length>28)return;for(var i=0;i<re.length;i++){if(re[i][0].test(t)){' +
      'td.className+=" rp-status-"+re[i][1];return;}}});})();<\/script>' +
      '</body></html>');
    w.document.close(); w.focus();
    setTimeout(function () { w.print(); w.close(); }, 400);
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
