/* Sets the colour theme before the first paint (kept external: the CSP forbids inline scripts). */
(function () {
  'use strict';
  var saved = null;
  try { saved = window.localStorage.getItem('tgp-theme'); } catch (e) { saved = null; }
  var dark = !!(window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches);
  var theme = saved === 'dark' || saved === 'light' ? saved : (dark ? 'dark' : 'light');
  document.documentElement.setAttribute('data-theme', theme);
})();
