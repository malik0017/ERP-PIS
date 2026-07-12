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
      body: v('--phoenix-body-color', '#31374a')
    };
  }

  function palette(c) { return [c.primary, c.info, c.success, c.warning, c.danger, '#8c68f5', '#12b8a6', '#e84f8a']; }

  function buildOption(type, labels, values, c) {
    var pal = palette(c);
    var pieData = labels.map(function (l, i) { return { name: l, value: values[i] }; });
    var base = { color: pal, tooltip: { trigger: type === 'pie' || type === 'donut' ? 'item' : 'axis' } };

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
    chart.setOption(buildOption(type, labels, values, vars()), true);
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
