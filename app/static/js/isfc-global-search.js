/* ISFC Global Search: works with all Phoenix navbar search inputs and the modal search. */
(function () {
  function findBox(input) { return input.closest('.search-box') || input.closest('.modal-content') || input.parentElement; }
  function ensureMenu(input) {
    var box = findBox(input);
    var menu = box.querySelector('.isfc-global-search-menu');
    if (!menu) {
      menu = document.createElement('div');
      menu.className = 'dropdown-menu border start-0 py-0 overflow-hidden w-100 show isfc-global-search-menu';
      menu.style.maxHeight = '30rem';
      menu.style.overflowY = 'auto';
      menu.style.display = 'none';
      menu.style.zIndex = '1080';
      box.style.position = box.style.position || 'relative';
      box.appendChild(menu);
    }
    return menu;
  }
  function escapeHtml(s) {
    return String(s || '').replace(/[&<>'"]/g, function (c) { return {'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]; });
  }
  function render(input, data) {
    var menu = ensureMenu(input);
    var q = (input.value || '').trim();
    if (!q) { menu.style.display = 'none'; menu.innerHTML = ''; return; }
    var rows = (data && data.results) || [];
    if (!rows.length) {
      menu.innerHTML = '<div class="p-3 text-center text-body-tertiary">No live result found. Press Enter for full search.</div>';
      menu.style.display = 'block'; return;
    }
    menu.innerHTML = '<h6 class="dropdown-header text-body-highlight fs-9 border-bottom border-translucent py-2 lh-sm">Live ERP Results</h6>' + rows.map(function (r) {
      return '<a class="dropdown-item py-2" href="' + escapeHtml(r.url) + '">' +
        '<div class="d-flex justify-content-between gap-2"><div class="min-w-0">' +
        '<div class="fw-bold text-body-highlight text-truncate">' + escapeHtml(r.title) + '</div>' +
        '<div class="fs-10 text-body-tertiary text-truncate">' + escapeHtml(r.subtitle || '') + '</div>' +
        '</div><span class="badge badge-phoenix badge-phoenix-secondary flex-shrink-0">' + escapeHtml(r.type) + '</span></div></a>';
    }).join('') + '<div class="border-top p-2"><a class="btn btn-sm btn-primary w-100" href="/search?q=' + encodeURIComponent(q) + '">Open full search results</a></div>';
    menu.style.display = 'block';
  }
  var timers = new WeakMap();
  function bind(input) {
    if (!input || input.dataset.isfcLiveSearchBound === '1') return;
    input.dataset.isfcLiveSearchBound = '1';
    input.addEventListener('input', function () {
      var q = (input.value || '').trim();
      clearTimeout(timers.get(input));
      if (!q) { render(input, {results: []}); return; }
      timers.set(input, setTimeout(function () {
        fetch('/search/api?q=' + encodeURIComponent(q), {headers: {'Accept':'application/json'}})
          .then(function (r) { return r.json(); })
          .then(function (data) { render(input, data); })
          .catch(function () { render(input, {results: []}); });
      }, 180));
    });
    input.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') {
        e.preventDefault();
        var q = (input.value || '').trim();
        if (q) window.location.href = '/search?q=' + encodeURIComponent(q);
      }
      if (e.key === 'Escape') { var m = ensureMenu(input); m.style.display = 'none'; input.blur(); }
    }, true);
  }
  function bindAll() {
    document.querySelectorAll('input[type="search"], input.search-input, #isfcGlobalSearch, #isfcModalSearch').forEach(bind);
  }
  document.addEventListener('DOMContentLoaded', bindAll);
  document.addEventListener('shown.bs.modal', bindAll);
  document.addEventListener('click', function(e){
    document.querySelectorAll('.isfc-global-search-menu').forEach(function(menu){ if(!menu.parentElement.contains(e.target)) menu.style.display='none'; });
  });
})();
