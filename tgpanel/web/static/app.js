(function () {
  'use strict';
  var doc = document;
  var selected = new Set();

  /* CSRF header for every htmx request */
  doc.addEventListener('htmx:configRequest', function (e) {
    var m = doc.querySelector('meta[name="csrf-token"]');
    if (m) { e.detail.headers['X-CSRF-Token'] = m.content; }
  });
  /* polling (data-poll) is paused while the tab is hidden */
  doc.addEventListener('htmx:beforeRequest', function (e) {
    var el = e.detail && e.detail.elt;
    if (doc.hidden && el && el.hasAttribute && el.hasAttribute('data-poll')) { e.preventDefault(); }
  });
  doc.addEventListener('visibilitychange', function () {
    if (!doc.hidden && window.htmx) {
      doc.querySelectorAll('[data-poll]').forEach(function (el) { window.htmx.trigger(el, 'tgp-refresh'); });
    }
  });

  /* theme toggle (saved in localStorage when available) */
  function currentTheme() { return doc.documentElement.getAttribute('data-theme') === 'dark' ? 'dark' : 'light'; }
  function setTheme(theme) {
    doc.documentElement.setAttribute('data-theme', theme);
    try { window.localStorage.setItem('tgp-theme', theme); } catch (e) { /* storage may be blocked */ }
    doc.dispatchEvent(new CustomEvent('tgp-theme', { detail: theme }));
  }

  /* bulk bar: fields that belong to the chosen action, selection counter */
  function applyBulk() {
    doc.querySelectorAll('form[data-bulk]').forEach(function (form) {
      var action = form.querySelector('[data-bulk-action]');
      if (!action) { return; }
      form.querySelectorAll('[data-for]').forEach(function (el) { el.hidden = el.getAttribute('data-for') !== action.value; });
    });
  }
  function visibleIds() {
    var ids = new Set();
    doc.querySelectorAll('input[type=checkbox][name="ids"]').forEach(function (c) { ids.add(c.value); });
    return ids;
  }
  function updateCount() {
    doc.querySelectorAll('[data-selected-count]').forEach(function (el) {
      var n = doc.querySelectorAll('input[type=checkbox][name="ids"]:checked').length;
      el.textContent = n ? (el.getAttribute('data-label') || '') + ' ' + n : '';
    });
  }
  /* after a live re-render keep the selection of rows that are still on the page */
  function restoreSelection() {
    var vis = visibleIds();
    selected = new Set(Array.from(selected).filter(function (id) { return vis.has(id); }));
    doc.querySelectorAll('input[type=checkbox][name="ids"]').forEach(function (c) { c.checked = selected.has(c.value); });
    updateCount();
  }

  doc.addEventListener('click', function (e) {
    var t = e.target;
    if (!(t instanceof Element)) { return; }
    if (t.closest('[data-theme-toggle]')) { setTheme(currentTheme() === 'dark' ? 'light' : 'dark'); return; }
    if (t.closest('[data-nav-toggle]')) { doc.body.classList.toggle('nav-open'); return; }
    var copy = t.closest('[data-copy]');
    if (copy && navigator.clipboard) { navigator.clipboard.writeText(copy.getAttribute('data-copy')); return; }
    var copyText = t.closest('[data-copy-text]');
    if (copyText && navigator.clipboard) {
      var ta = copyText.closest('.reveal').querySelector('textarea');
      if (ta) { navigator.clipboard.writeText(ta.value); }
      return;
    }
    if (t.closest('[data-hide]')) {
      var box = t.closest('.reveal');
      if (box) { box.remove(); }
    }
  });

  doc.addEventListener('change', function (e) {
    var t = e.target;
    if (!(t instanceof HTMLInputElement || t instanceof HTMLSelectElement)) { return; }
    if (t instanceof HTMLInputElement && t.hasAttribute('data-select-all')) {
      var name = t.getAttribute('data-select-all');
      doc.querySelectorAll('input[type=checkbox][name="' + name + '"]').forEach(function (c) {
        c.checked = t.checked;
        if (t.checked) { selected.add(c.value); } else { selected.delete(c.value); }
      });
      updateCount();
    } else if (t instanceof HTMLInputElement && t.name === 'ids') {
      if (t.checked) { selected.add(t.value); } else { selected.delete(t.value); }
      updateCount();
    } else if (t.hasAttribute('data-bulk-action')) {
      applyBulk();
    }
  });

  doc.addEventListener('htmx:afterSwap', function () { restoreSelection(); applyBulk(); });
  doc.addEventListener('DOMContentLoaded', function () { applyBulk(); updateCount(); });
  doc.addEventListener('keydown', function (e) { if (e.key === 'Escape') { doc.body.classList.remove('nav-open'); } });
})();
