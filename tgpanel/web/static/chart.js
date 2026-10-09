(function () {
  'use strict';
  var el = document.getElementById('traffic-chart');
  if (!el || typeof echarts === 'undefined') { return; }
  var chart = echarts.init(el);
  var totals = document.getElementById('traffic-totals');
  var data = { points: [] };

  function fmtBytes(n) {
    var units = ['Б', 'КБ', 'МБ', 'ГБ', 'ТБ'], i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return (i === 0 ? n.toFixed(0) : n.toFixed(1)) + ' ' + units[i];
  }

  function recompute() {
    var opt = chart.getOption();
    var dz = opt.dataZoom && opt.dataZoom[0];
    var pts = data.points;
    if (!pts.length) { totals.textContent = ''; return; }
    var lo = pts[0][0], hi = pts[pts.length - 1][0];
    if (dz && dz.startValue != null && dz.endValue != null) { lo = dz.startValue; hi = dz.endValue; }
    var up = 0, down = 0;
    pts.forEach(function (p) { if (p[0] >= lo && p[0] <= hi) { up += p[1]; down += p[2]; } });
    totals.textContent = '↑ ' + fmtBytes(up) + '  ↓ ' + fmtBytes(down) + '  Σ ' + fmtBytes(up + down);
  }

  function draw() {
    var lu = el.getAttribute('data-label-up'), ld = el.getAttribute('data-label-down');
    chart.setOption({
      tooltip: { trigger: 'axis', valueFormatter: fmtBytes },
      legend: { data: [lu, ld] },
      xAxis: { type: 'time' },
      yAxis: { type: 'value', axisLabel: { formatter: fmtBytes } },
      dataZoom: [{ type: 'slider' }, { type: 'inside' }],
      series: [
        { name: lu, type: 'bar', stack: 't', data: data.points.map(function (p) { return [p[0], p[1]]; }) },
        { name: ld, type: 'bar', stack: 't', data: data.points.map(function (p) { return [p[0], p[2]]; }) }
      ]
    }, true);
    recompute();
  }

  function load(preset) {
    fetch(el.getAttribute('data-url') + '?preset=' + encodeURIComponent(preset), { credentials: 'same-origin' })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, body: j }; }); })
      .then(function (res) {
        if (!res.ok) { totals.textContent = res.body.error || el.getAttribute('data-label-error'); return; }
        data = res.body;
        draw();
      })
      .catch(function () { totals.textContent = el.getAttribute('data-label-error'); });
  }

  chart.on('datazoom', recompute);
  window.addEventListener('resize', function () { chart.resize(); });
  document.querySelectorAll('[data-preset]').forEach(function (b) {
    b.addEventListener('click', function () { load(b.getAttribute('data-preset')); });
  });
  load('30d');
})();
