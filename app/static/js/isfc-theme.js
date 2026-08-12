/* =========================================================================
 * ISFC PIMS — isfc-theme.js (Batch 100 — rewritten)
 *
 * Dark mode, toast colour semantics, and a self-diagnostic.
 *
 * -------------------------------------------------------------------------
 * WHY BATCH 99 DID NOT FIX IT, AND WHAT WAS ACTUALLY WRONG
 *
 * Batch 99 bound a `change` listener to the checkbox. That was the wrong
 * thing to bind to, for two compounding reasons found by reading the
 * rendered DOM and the vendor stylesheet:
 *
 *   1. The checkbox is `display:none`:
 *          .theme-control-toggle .theme-control-toggle-input { display:none }
 *      Only the two <label> elements are visible. Everything therefore
 *      depends on the label -> input association working perfectly.
 *
 *   2. That association was broken: partials/sidebar.html contained SEVEN
 *      navbar variants (default, slim, combo, dual-nav, ...) and every one
 *      of them used id="themeControlToggle". IDs must be unique. Every
 *      `<label for="themeControlToggle">` in all seven navbars resolved to
 *      the FIRST checkbox in the document, and the CSS that swaps the sun
 *      and moon icons is a SIBLING selector
 *          .theme-control-toggle-input:checked ~ .theme-control-toggle-dark
 *      which only matches inside the same navbar. So state and icon lived in
 *      different navbars and could never agree.
 *
 *   3. On top of that, Phoenix's own delegated body listener runs on the
 *      same click and can call window.location.reload() through its config
 *      layer — which is the "cursor moves and nothing happens" symptom.
 *
 * THE FIX: stop depending on any of it.
 *
 * This version binds directly to the visible LABEL, calls preventDefault()
 * so the hidden checkbox is never activated, and calls stopPropagation() so
 * Phoenix's body listener never sees the event at all. The theme is then
 * read from <html data-bs-theme>, flipped once, written back, and the
 * checkbox state is set manually so the sibling CSS updates the icon.
 *
 * One handler, one code path, no vendor involvement, no reload.
 *
 * VERIFY IT YOURSELF: open the browser console and run
 *     isfcTheme.debug()
 * It prints how many toggles were found, whether the ids are unique, the
 * current theme, and what is stored.
 * ========================================================================= */
(function () {
  'use strict';

  var KEY = 'phoenixTheme';
  var root = document.documentElement;

  function systemTheme() {
    try {
      return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
    } catch (e) { return 'light'; }
  }

  function resolve(v) {
    return v === 'auto' ? systemTheme() : (v === 'dark' ? 'dark' : 'light');
  }

  function stored() {
    try { return localStorage.getItem(KEY); } catch (e) { return null; }
  }

  function current() {
    return root.getAttribute('data-bs-theme') === 'dark' ? 'dark' : 'light';
  }

  function apply(value, persist) {
    var effective = resolve(value);
    root.setAttribute('data-bs-theme', effective);

    // Phoenix also reads this on some components.
    try { root.setAttribute('data-bs-theme-mode', value); } catch (e) {}

    if (persist !== false) {
      try { localStorage.setItem(KEY, value); } catch (e) { /* private mode */ }
    }
    syncControls(value, effective);

    // ECharts measures its container once at init and never re-measures, so
    // without this every chart keeps the previous theme's colours until the
    // next full page load.
    try {
      window.dispatchEvent(new Event('resize'));
      document.dispatchEvent(new CustomEvent('isfc:themechange', {
        detail: { value: value, effective: effective }
      }));
    } catch (e) {}
  }

  // Drive the hidden checkboxes manually. They are display:none, so their
  // only remaining job is to feed the `:checked ~ .theme-control-toggle-dark`
  // sibling rule that swaps the sun and moon icons.
  function syncControls(value, effective) {
    var boxes = document.querySelectorAll('input[type="checkbox"][data-theme-control="phoenixTheme"]');
    Array.prototype.forEach.call(boxes, function (el) {
      el.checked = (effective === 'dark');
    });
    var radios = document.querySelectorAll('input[type="radio"][data-theme-control="phoenixTheme"]');
    Array.prototype.forEach.call(radios, function (el) {
      el.checked = (el.value === value);
    });
  }

  // Apply the stored theme immediately, before first paint.
  var initial = stored() || root.getAttribute('data-bs-theme') || 'light';
  root.setAttribute('data-bs-theme', resolve(initial));

  function toggle(e) {
    // Both are essential:
    //   preventDefault  -> the hidden checkbox is never activated, so no
    //                      change event fires and nothing can double-toggle.
    //   stopPropagation -> Phoenix's delegated body listener never runs, so
    //                      it cannot reload the page or fight this handler.
    if (e) {
      e.preventDefault();
      e.stopPropagation();
      if (e.stopImmediatePropagation) e.stopImmediatePropagation();
    }
    apply(current() === 'dark' ? 'light' : 'dark', true);
  }


// ---------------------------------------------------------------------------
// Batch 101 — SVG-safe ancestor matching.
//
// The icon inside the label is a feather <svg>. Element.closest() exists on
// SVGElement, but the OLD failure was upstream of that: clicks on the
// transparent interior of an SVG do not hit the SVG at all (SVG defaults to
// pointer-events: visiblePainted, so only the painted stroke is clickable).
// The event then targets whatever is behind it, and the toggle never fires.
//
// The CSS in base.html now sets pointer-events:none on everything inside the
// label so the click always lands on the label's solid 2rem box. This walker
// is the belt to that braces: it climbs parentNode by hand rather than
// trusting closest(), which is missing on SVGElement in a few older engines
// and would silently return early there.
// ---------------------------------------------------------------------------
var TOGGLE_SEL = ['theme-control-toggle-label', 'theme-control-toggle'];

function matchesToggle(node) {
  var depth = 0;
  while (node && depth < 12) {
    if (node.nodeType === 1) {
      var cls = node.getAttribute && node.getAttribute('class');
      // className on SVG elements is an SVGAnimatedString, not a string —
      // getAttribute is the only form that behaves the same for both.
      if (typeof cls === 'string') {
        for (var i = 0; i < TOGGLE_SEL.length; i++) {
          if (cls.indexOf(TOGGLE_SEL[i]) !== -1) return true;
        }
      }
      if (node.id === 'launcherThemeToggle') return true;
      if (node.hasAttribute && node.hasAttribute('data-isfc-theme-toggle')) return true;
    }
    node = node.parentNode;
    depth++;
  }
  return false;
}

  function bind() {
    syncControls(initial, resolve(initial));

    // Capture phase on document: this runs BEFORE Phoenix's listener on
    // <body>, which is what lets stopPropagation actually stop it.
    document.addEventListener('click', function (e) {
      if (matchesToggle(e.target)) toggle(e);
    }, true);

    // The offcanvas Light/Dark/Auto radios are real, visible controls with no
    // label-overlay problem, so `change` is the right event for those.
    document.addEventListener('change', function (e) {
      var el = e.target;
      if (!el || !el.getAttribute) return;
      if (el.getAttribute('data-theme-control') !== 'phoenixTheme') return;
      if (el.type !== 'radio') return;   // checkboxes are handled by the click path
      apply(el.value, true);
    }, true);

    // Follow the OS only while the user has explicitly chosen "auto".
    try {
      window.matchMedia('(prefers-color-scheme: dark)')
        .addEventListener('change', function () {
          if (stored() === 'auto') apply('auto', false);
        });
    } catch (e) {}
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', bind);
  } else {
    bind();
  }

  // --- Self-diagnostic: run isfcTheme.debug() in the browser console -------
  window.isfcTheme = {
    apply: apply,
    toggle: function () { apply(current() === 'dark' ? 'light' : 'dark', true); },
    debug: function () {
      var boxes = document.querySelectorAll('input[data-theme-control="phoenixTheme"]');
      var ids = [];
      Array.prototype.forEach.call(boxes, function (b) { if (b.id) ids.push(b.id); });
      var dupes = ids.filter(function (v, i) { return ids.indexOf(v) !== i; });
      var labels = document.querySelectorAll('.theme-control-toggle-label');
      var info = {
        toggleInputsFound: boxes.length,
        labelsFound: labels.length,
        ids: ids,
        duplicateIds: dupes,
        currentTheme: current(),
        storedValue: stored(),
        handlerBound: true
      };
      console.table(info);
      if (dupes.length) {
        console.warn('DUPLICATE toggle ids still present:', dupes,
          '- labels will resolve to the wrong checkbox.');
      }
      return info;
    }
  };

  /* =======================================================================
   * TOAST COLOUR SEMANTICS (unchanged from Batch 99)
   * success -> green, warning -> amber, error -> red, anything else -> info.
   * ======================================================================= */
  var VARIANT = {
    success: 'success', ok: 'success', approved: 'success', done: 'success', delivered: 'success',
    warning: 'warning', warn: 'warning', pending: 'warning', caution: 'warning', locked: 'warning',
    danger: 'danger', error: 'danger', fail: 'danger', failed: 'danger', rejected: 'danger',
    info: 'info', primary: 'info', secondary: 'info'
  };

  window.isfcToastVariant = function (v) {
    return VARIANT[String(v || '').toLowerCase()] || 'info';
  };

  function autoToast() {
    var nodes = document.querySelectorAll('[data-isfc-autotoast]');
    Array.prototype.forEach.call(nodes, function (el) {
      var variant = window.isfcToastVariant(el.getAttribute('data-isfc-autotoast'));
      var title = el.getAttribute('data-isfc-autotoast-title') || '';
      var msg = (el.getAttribute('data-isfc-autotoast-msg')
        || el.textContent || '').trim().replace(/\s+/g, ' ');
      if (!msg) return;
      el.remove();
      if (typeof window.showToast === 'function') window.showToast(msg, variant, title);
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', autoToast);
  } else {
    autoToast();
  }
})();
