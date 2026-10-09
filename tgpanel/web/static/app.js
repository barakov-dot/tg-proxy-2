(function () {
  'use strict';
  document.addEventListener('htmx:configRequest', function (e) {
    var m = document.querySelector('meta[name="csrf-token"]');
    if (m) { e.detail.headers['X-CSRF-Token'] = m.content; }
  });
  document.addEventListener('click', function (e) {
    var t = e.target;
    if (!(t instanceof Element)) { return; }
    var copy = t.closest('[data-copy]');
    if (copy && navigator.clipboard) { navigator.clipboard.writeText(copy.getAttribute('data-copy')); return; }
    var copyText = t.closest('[data-copy-text]');
    if (copyText && navigator.clipboard) {
      var ta = copyText.parentElement.querySelector('textarea');
      if (ta) { navigator.clipboard.writeText(ta.value); }
      return;
    }
    if (t.closest('[data-hide]')) {
      var box = t.closest('.reveal');
      if (box) { box.remove(); }
    }
  });
  document.addEventListener('change', function (e) {
    var t = e.target;
    if (t instanceof HTMLInputElement && t.hasAttribute('data-select-all')) {
      var name = t.getAttribute('data-select-all');
      document.querySelectorAll('input[type=checkbox][name="' + name + '"]').forEach(function (c) {
        c.checked = t.checked;
      });
    }
  });
})();
