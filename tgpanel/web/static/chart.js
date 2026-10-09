/* Traffic charts (ECharts, vendored): per-user card and the server-wide dashboard chart. */
(function () {
  'use strict';
  var el = document.getElementById('traffic-chart') || document.getElementById('global-chart');
  if (!el || typeof echarts === 'undefined') { return; }
  var totals = document.getElementById(el.getAttribute('data-totals') || 'traffic-totals');
  var zoom = el.getAttribute('data-zoom') === '1';
  var chart = echarts.init(el);
  var data = { points: [] };
  var preset = el.getAttribute('data-default') || '30d';

  function css(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }
  function fmtBytes(n) {
    var units = ['Б', 'КБ', 'МБ', 'ГБ', 'ТБ'], i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return (i === 0 ? n.toFixed(0) : n.toFixed(1)) + ' ' + units[i];
  }
  function sum(lo, hi) {
    var up = 0, down = 0;
    data.points.forEach(function (p) { if (p[0] >= lo && p[0] <= hi) { up += p[1]; down += p[2]; } });
    return [up, down];
  }
  function showTotals(up, down) {
    if (totals) { totals.textContent = '↑ ' + fmtBytes(up) + '   ↓ ' + fmtBytes(down) + '   Σ ' + fmtBytes(up + down); }
  }
  function recompute() {
    var pts = data.points;
    if (!pts.length) { if (totals) { totals.textContent = ''; } return; }
    var lo = pts[0][0], hi = pts[pts.length - 1][0];
    var opt = chart.getOption();
    var dz = opt.dataZoom && opt.dataZoom[0];
    if (zoom && dz && dz.startValue != null && dz.endValue != null) { lo = dz.startValue; hi = dz.endValue; }
    var s = sum(lo, hi);
    showTotals(s[0], s[1]);
  }

  function draw() {
    var lu = el.getAttribute('data-label-up'), ld = el.getAttribute('data-label-down');
    var cu = css('--chart-up') || '#3566e8', cd = css('--chart-down') || '#12a37f';
    var text = css('--muted') || '#667085', line = css('--border') || '#e2e6ed';
    function series(name, color, idx) {
      return {
        name: name, type: 'line', smooth: 0.25, showSymbol: false, color: color,
        lineStyle: { width: 1.6 }, areaStyle: { opacity: 0.16 },
        data: data.points.map(function (p) { return [p[0], p[idx]]; })
      };
    }
    chart.setOption({
      animation: false,
      grid: { left: 8, right: 12, top: 28, bottom: zoom ? 56 : 12, containLabel: true },
      tooltip: {
        trigger: 'axis',
        valueFormatter: fmtBytes
      },
      legend: { data: [lu, ld], top: 0, textStyle: { color: text } },
      xAxis: { type: 'time', axisLabel: { color: text }, axisLine: { lineStyle: { color: line } } },
      yAxis: { type: 'value', axisLabel: { color: text, formatter: fmtBytes }, splitLine: { lineStyle: { color: line } } },
      dataZoom: zoom ? [{ type: 'slider' }, { type: 'inside' }] : [],
      series: [series(lu, cu, 1), series(ld, cd, 2)]
    }, true);
    recompute();
  }

  function load(p) {
    preset = p;
    document.querySelectorAll('[data-preset]').forEach(function (b) {
      b.setAttribute('aria-pressed', b.getAttribute('data-preset') === p ? 'true' : 'false');
    });
    fetch(el.getAttribute('data-url') + '?preset=' + encodeURIComponent(p), { credentials: 'same-origin' })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, body: j }; }); })
      .then(function (res) {
        if (!res.ok) { if (totals) { totals.textContent = res.body.error || el.getAttribute('data-label-error'); } return; }
        data = res.body;
        draw();
      })
      .catch(function () { if (totals) { totals.textContent = el.getAttribute('data-label-error'); } });
  }

  chart.on('datazoom', recompute);
  window.addEventListener('resize', function () { chart.resize(); });
  document.addEventListener('tgp-theme', draw);
  document.querySelectorAll('[data-preset]').forEach(function (b) {
    b.addEventListener('click', function () { load(b.getAttribute('data-preset')); });
  });
  load(preset);
})();
