/* =========================================================================
   ISFC PIMS — isfc-charts.js (Batch 9: ENHANCED)
   
   Batch 9 enhancements:
   - Zero-width guard: if container width is 0 at init, defer render.
   - ResizeObserver: watches each chart element; re-renders on size changes.
     This fixes RTL and tab-switching where containers start hidden.
   - Dark/theme-aware: re-renders when data-bs-theme changes.
   
   One engine, every chart. Any element like:
     <div data-isfc-chart="bar" data-labels='["A","B"]' data-values='[3,5]'
          data-types="bar,hbar,line,area,pie,donut,radar,gauge"
          style="min-height:320px"></div>
   renders an ECharts chart AND shows a switcher for chart types.
   ========================================================================= */
(function () {
  'use strict';
  if (!window.echarts) return;

  var TYPE_LABELS = {
    bar: 'Bar', hbar: 'H-Bar', line: 'Line', area: 'Area',
    pie: 'Pie', donut: 'Donut', radar: 'Radar', gauge: 'Gauge'
  };

  function vars() {
    var css = getComputedStyle(document.documentElement);
    function v(n, f) { return (css.getPropertyValue(n) || '').trim() || f; }
    return {
      primary: v('--phoenix-primary', '#3874ff'),
      info: v('--phoenix-info', '#0097eb'),
      success: v('--phoenix-success', '#25b003'),
      warning: v('--phoenix-warning', '#e5780b'),
      danger: v('--phoenix-danger', '#fa3b1d'),
      text: v('--phoenix-secondary-color', '#6e7891'),
      grid: v('--phoenix-border-color', '#e3e6ed'),
      body: v('--phoenix-body-color', '#31374a'),
      // Batch 106: tooltip surface taken from the theme rather than left to
      // ECharts' hard-coded near-white, which was invisible in dark mode.
      tipBg: v('--phoenix-body-bg', '#ffffff'),
      tipText: v('--phoenix-body-color', '#31374a')
    };
  }

  function palette(c) { return [c.primary, c.info, c.success, c.warning, c.danger, '#8c68f5', '#12b8a6', '#e84f8a']; }

  function buildOption(type, labels, values, c, unit, note) {
    unit = unit || '';
    note = note || '';
    var pal = palette(c);
    var pieData = labels.map(function (l, i) { return { name: l, value: values[i] }; });
    // =====================================================================
    // Batch 106 — READABLE TOOLTIPS, SYSTEM-WIDE.
    //
    // The tooltip had a `trigger` and nothing else. Two consequences:
    //
    //   1. NO STYLING. ECharts defaults to a near-white background with dark
    //      text. In dark mode that is white-on-white — the tooltip was firing
    //      the whole time and you simply could not read it. That is why it
    //      looked like "no information, just a number".
    //   2. NO FORMATTER. Even when visible it showed a bare value with no
    //      share of total, so a bar reading "6" told you nothing about
    //      whether 6 was most of the pipeline or a rounding error.
    //
    // Every chart in the system is built through this one function, so fixing
    // it here fixes all of them at once.
    // =====================================================================
    var total = values.reduce(function (a, b) { return a + (Math.abs(+b) || 0); }, 0);

    function fmtNum(n) {
      var x = +n || 0;
      // Whole numbers stay whole (counts of orders), decimals keep 2 places
      // (money and quantities). Printing "6.00 orders" reads like an error.
      return (Math.abs(x % 1) < 1e-9) ? x.toLocaleString()
                                      : x.toLocaleString(undefined, { minimumFractionDigits: 2,
                                                                      maximumFractionDigits: 2 });
    }

    function share(v) {
      if (!total) return '';
      var pct = (Math.abs(+v) || 0) / total * 100;
      return ' <span style="opacity:.7">(' + pct.toFixed(1) + '% of ' + fmtNum(total) + ')</span>';
    }

    var tip = {
      trigger: (type === 'pie' || type === 'donut' || type === 'gauge') ? 'item' : 'axis',
      confine: true,                 // never let it fall outside a fullscreen card
      backgroundColor: c.tipBg,
      borderColor: c.grid,
      borderWidth: 1,
      padding: [8, 12],
      extraCssText: 'border-radius:8px;box-shadow:0 6px 18px rgba(0,0,0,.18);',
      textStyle: { color: c.tipText, fontSize: 12 },
      axisPointer: {
        type: (type === 'line' || type === 'area') ? 'line' : 'shadow',
        lineStyle: { color: c.grid },
        shadowStyle: { color: 'rgba(125,145,180,.14)' }
      },
      formatter: function (params) {
        // Batch 107 — tooltips now carry CONTEXT, not just a repeat of the
        // number already printed on the bar. Hovering "Submitted 6" used to
        // tell you "6", which you could already see. It now tells you what
        // that 6 means relative to everything else on the chart:
        //
        //     Submitted
        //     6 orders          46.2% of 13
        //     Largest of 6 · 3 more than the next
        //
        // `unit` and `note` come from optional data attributes on the chart
        // element, so each chart can name its own unit and explain itself
        // without touching this file.
        var arr = Array.isArray(params) ? params : [params];
        if (!arr.length) return '';
        var head = arr[0].name || '';
        var out = '<div style="font-weight:700;margin-bottom:5px;font-size:13px">' + head + '</div>';

        arr.forEach(function (p) {
          var v = (p.value && typeof p.value === 'object') ? p.value.value : p.value;
          var n = Math.abs(+v) || 0;
          out += '<div style="display:flex;align-items:baseline;gap:6px;margin-bottom:2px">'
               + (p.marker || '')
               + '<strong style="font-size:15px">' + fmtNum(v) + '</strong>'
               + (unit ? '<span style="opacity:.75">' + unit + '</span>' : '')
               + share(v)
               + '</div>';

          // Rank and gap — the two things a reader is actually working out in
          // their head when they look at a bar chart.
          if (values.length > 1) {
            var sorted = values.map(function (x) { return Math.abs(+x) || 0; })
                               .sort(function (a, b) { return b - a; });
            var rank = sorted.indexOf(n) + 1;
            var bits = [];
            if (rank === 1) {
              var gap = sorted[0] - sorted[1];
              bits.push('Largest of ' + values.length);
              if (gap > 0) bits.push(fmtNum(gap) + ' more than the next');
            } else if (rank === values.length) {
              bits.push('Smallest of ' + values.length);
            } else {
              bits.push('#' + rank + ' of ' + values.length);
            }
            out += '<div style="opacity:.7;font-size:11px">' + bits.join(' · ') + '</div>';
          }
        });

        if (note) {
          out += '<div style="opacity:.65;font-size:11px;margin-top:5px;'
               + 'border-top:1px solid rgba(125,145,180,.25);padding-top:4px;'
               + 'max-width:230px;white-space:normal">' + note + '</div>';
        }
        return out;
      }
    };

    var base = { color: pal, tooltip: tip };

    if (type === 'pie' || type === 'donut') {
      base.legend = { bottom: 0, textStyle: { color: c.text }, type: 'scroll' };
      base.series = [{
        type: 'pie',
        radius: type === 'donut' ? ['45%', '72%'] : '72%',
        center: ['50%', '46%'],
        data: pieData,
        label: { color: c.text },
        itemStyle: { borderRadius: 6, borderWidth: 2, borderColor: 'transparent' }
      }];
      return base;
    }
    if (type === 'radar') {
      var max = Math.max.apply(null, values.concat([1])) * 1.2;
      base.radar = {
        indicator: labels.map(function (l) { return { name: l, max: max }; }),
        axisName: { color: c.text }, splitLine: { lineStyle: { color: c.grid } },
        splitArea: { show: false }, axisLine: { lineStyle: { color: c.grid } }
      };
      base.series = [{ type: 'radar', data: [{ value: values }], areaStyle: { opacity: .25 } }];
      return base;
    }
    if (type === 'gauge') {
      var total = values.reduce(function (a, b) { return a + (+b || 0); }, 0);
      var first = +values[0] || 0;
      var pct = total ? Math.round(first / total * 100) : 0;
      base.series = [{
        type: 'gauge', progress: { show: true, width: 12 },
        axisLine: { lineStyle: { width: 12, color: [[1, c.grid]] } },
        axisTick: { show: false }, splitLine: { length: 8, lineStyle: { color: c.text } },
        axisLabel: { color: c.text, distance: 18, fontSize: 10 },
        pointer: { itemStyle: { color: c.primary } },
        detail: { formatter: '{value}%', color: c.body, fontSize: 22 },
        title: { show: true, offsetCenter: [0, '75%'], color: c.text },
        data: [{ value: pct, name: labels[0] || '' }]
      }];
      return base;
    }

    // axis charts: bar / hbar / line / area
    var horizontal = type === 'hbar';
    var catAxis = { type: 'category', data: labels,
      axisLabel: { color: c.text, fontWeight: 600, interval: 0, rotate: horizontal ? 0 : (labels.length > 6 ? 28 : 0) },
      axisLine: { lineStyle: { color: c.grid } }, axisTick: { show: false } };
    var valAxis = { type: 'value', axisLabel: { color: c.text }, splitLine: { lineStyle: { color: c.grid } } };
    base.grid = { left: 8, right: 26, top: 18, bottom: 8, containLabel: true };
    base.xAxis = horizontal ? valAxis : catAxis;
    base.yAxis = horizontal ? catAxis : valAxis;
    base.series = [{
      type: (type === 'line' || type === 'area') ? 'line' : 'bar',
      data: values, smooth: true,
      barWidth: '55%',
      itemStyle: { color: c.primary, borderRadius: horizontal ? [0, 6, 6, 0] : [6, 6, 0, 0] },
      areaStyle: type === 'area' ? { opacity: .25 } : undefined,
      label: { show: labels.length <= 12, position: horizontal ? 'right' : 'top', color: c.text, fontWeight: 700 }
    }];
    return base;
  }

  function render(el) {
    // Batch 9: Guard against zero-width container (happens in RTL tabs/collapsible).
    var w = el.offsetWidth || el.clientWidth || 0;
    if (w <= 0) return null; // Defer; ResizeObserver will retry.
    
    var labels = JSON.parse(el.dataset.labels || '[]');
    var values = JSON.parse(el.dataset.values || '[]').map(Number);
    var type = el.dataset.isfcChart || 'bar';
    var chart = echarts.getInstanceByDom(el) || echarts.init(el);
    chart.setOption(buildOption(type, labels, values, vars(), el.dataset.unit, el.dataset.note), true);
    return chart;
  }

  function attachSwitcher(el) {
    var types = (el.dataset.types || '').split(',').map(function (s) { return s.trim(); })
      .filter(function (t) { return TYPE_LABELS[t]; });
    if (types.length < 2) return;
    var bar = document.createElement('div');
    bar.className = 'isfc-chart-tools d-flex gap-1 flex-wrap justify-content-end mb-2';
    types.forEach(function (t) {
      var b = document.createElement('button');
      b.type = 'button';
      b.className = 'btn btn-sm ' + (t === el.dataset.isfcChart ? 'btn-primary' : 'btn-phoenix-secondary');
      b.textContent = TYPE_LABELS[t];
      b.addEventListener('click', function () {
        el.dataset.isfcChart = t;
        bar.querySelectorAll('button').forEach(function (x) { x.className = 'btn btn-sm btn-phoenix-secondary'; });
        b.className = 'btn btn-sm btn-primary';
        render(el);
      });
      bar.appendChild(b);
    });
    el.parentNode.insertBefore(bar, el);
  }

  function boot() {
    var els = document.querySelectorAll('[data-isfc-chart]');
    if (!els.length) return;
    
    els.forEach(function (el) {
      attachSwitcher(el);
      render(el);
      
      // Batch 9: ResizeObserver watches for size changes. When container becomes
      // visible (e.g., tab switched, collapse opened), re-render the chart.
      if (window.ResizeObserver) {
        try {
          var ro = new ResizeObserver(function () {
            var inst = echarts.getInstanceByDom(el);
            if (inst) {
              inst.resize();
            } else {
              var r = render(el);
              if (!r && el.offsetWidth > 0) {
                // Tried to render but failed; try again soon.
                setTimeout(function () { render(el); }, 100);
              }
            }
          });
          ro.observe(el);
        } catch (e) { /* noop */ }
      }
    });

    window.addEventListener('resize', function () {
      els.forEach(function (el) { var c = echarts.getInstanceByDom(el); if (c) c.resize(); });
    });

    // Re-render on theme change (data-bs-theme attribute).
    new MutationObserver(function () {
      els.forEach(render);
    }).observe(document.documentElement, { attributes: true, attributeFilter: ['data-bs-theme'] });
  }

  if (document.readyState !== 'loading') boot();
  else document.addEventListener('DOMContentLoaded', boot);
})();
