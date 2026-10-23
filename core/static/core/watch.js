/*
  The last 24 hours of the whole cluster on the Overview, drawn on two canvases that share one time axis:
    1. the score of every node as a line, with the alert level and the cluster events behind it;
    2. a row per node and a column per quarter of an hour, so you can see who was unusual and when.
  Both are drawn from the data the server embeds in the page (see core/services/timeline.py).
  The Nodes page lists the same information as a table, which is the accessible version of these pictures.
*/
(function () {
  var host = document.getElementById('watch');
  var dataEl = document.getElementById('watch-data');
  if (!host || !dataEl) return;
  var data = JSON.parse(dataEl.textContent);
  var chart = document.getElementById('fleet-chart'), map = document.getElementById('fleet-map'), tip = host.querySelector('.watch-tip');
  var cctx = chart.getContext('2d'), ctx = map.getContext('2d');
  var rows = data.rows, N = data.columns, step = data.step * 60000;
  var FONT = '"Inter Tight", system-ui, sans-serif';
  // shared geometry: the same left edge and the same column width on both pictures, so a column lines up
  var LEFT = 78, RIGHT = 8, MIN_W = 300, FROM = 0;
  // the map
  var AXIS = 16, BAND = 9, TOP = AXIS + BAND + 8, ROW = 9.5, GAP = 1.5, GROUP = 24, GROUP_GAP = 8;
  // the chart
  var CH = 190, C_TOP = 8, C_BOTTOM = 6;
  var groups = {}; rows.forEach(function (r) { groups[r.group] = true; });
  var grouped = Object.keys(groups).length > 1;
  var W = 0, H = 0, layout = [], colors = {}, hover = null, chartHover = null, progress = 1, ymax = 6;

  function css(name, fallback) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback; }
  function readColors() {
    colors = { bad: css('--bad', '#c9382d'), warn: css('--warn', '#dc9a0f'), warntx: css('--warntx', '#7a5600'), evt: css('--evt', '#6a60d8'),
               cell: css('--cell', '#e2dfd8'), norm: css('--norm', '#c8e2d1'), quiet: css('--quiet', '#cdd0d6'), ink: css('--ink', '#1f1e1b'), mid: css('--mid', '#5f5c55'),
               faint: css('--faint', '#8f8b82'), line: css('--line', '#dfdcd5'), line2: css('--line2', '#ebe8e2'), edge: css('--edgeline', '#cfccc4'),
               panel: css('--panel', '#fff'), grey: '#7c8794' };
  }
  function pad(n) { return (n < 10 ? '0' : '') + n; }
  // Times are drawn in the zone the server shows everywhere else (South African time on Ranger), not in the browser's own.
  var ZONE = host.getAttribute('data-tz') || 'UTC', ZONE_LABEL = host.getAttribute('data-tz-label') || ZONE, zoneFmt = null;
  try {
    zoneFmt = new Intl.DateTimeFormat('en-GB', { timeZone: ZONE, hourCycle: 'h23', month: 'numeric', day: 'numeric', hour: 'numeric', minute: 'numeric' });
  } catch (err) { zoneFmt = null; ZONE_LABEL = 'UTC'; }
  function parts(ms) {
    if (!zoneFmt) { var u = new Date(ms); return { day: u.getUTCDate(), month: u.getUTCMonth() + 1, hour: u.getUTCHours(), minute: u.getUTCMinutes() }; }
    var o = {}; zoneFmt.formatToParts(new Date(ms)).forEach(function (p) { o[p.type] = +p.value; });
    return { day: o.day, month: o.month, hour: o.hour % 24, minute: o.minute };
  }
  function clock(ms) { var p = parts(ms); return pad(p.hour) + ':' + pad(p.minute); }
  var MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  function dayLabel(ms) { var p = parts(ms); return p.day + ' ' + MONTHS[p.month - 1]; }
  function colW() { return (W - LEFT - RIGHT) / (N - FROM); }
  function colX(i) { return LEFT + (i - FROM) * colW(); }
  function indexAt(ms) { return (ms - data.start) / step; }
  function sized(canvas, c, w, h) {
    var dpr = window.devicePixelRatio || 1;
    canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr);
    canvas.style.width = w + 'px'; canvas.style.height = h + 'px';
    c.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  function measure() {
    W = Math.max(MIN_W, host.clientWidth - 28);
    // on a phone 96 columns would be hairlines: show the last 12 hours there, with the row labels still in view
    FROM = host.clientWidth < 620 ? Math.floor(N / 2) : 0;
    var title = document.getElementById('watch-h');
    if (title) title.textContent = 'Last ' + Math.round((N - FROM) * data.step / 60) + ' hours';
    var top = 0; rows.forEach(function (r) { for (var k = 0; k < r.v.length; k++) if (r.cells.charAt(k) !== '.' && r.cells.charAt(k) !== 'q') top = Math.max(top, r.v[k] / 10); });
    ymax = Math.min(Math.max(6, Math.ceil(top / 2) * 2), 12);
    var y = TOP, last = null;
    layout = rows.map(function (r) {
      if (grouped && r.group !== last) { y += last === null ? 0 : GROUP_GAP; r._header = y; y += GROUP; } else { r._header = undefined; }
      last = r.group;
      var item = { y: y }; y += ROW + GAP; return item;
    });
    H = y + 6;
    sized(chart, cctx, W, CH);
    sized(map, ctx, W, H);
  }

  // ---------- the score of every node ----------
  function cy(v) { return C_TOP + (CH - C_TOP - C_BOTTOM) * (1 - Math.min(v / ymax, 1)); }
  function scored(ch) { return ch === 'n' || ch === 'u' || ch === 'w' || ch === 'e'; }
  function drawLine(r, color, width, alpha) {
    cctx.beginPath(); var on = false;
    for (var i = FROM; i < N; i++) {
      if (!scored(r.cells.charAt(i))) { on = false; continue; }
      var x = colX(i) + colW() / 2, y = cy(r.v[i] / 10);
      if (on) cctx.lineTo(x, y); else cctx.moveTo(x, y);
      on = true;
    }
    cctx.globalAlpha = alpha; cctx.strokeStyle = color; cctx.lineWidth = width; cctx.lineJoin = 'round'; cctx.stroke(); cctx.globalAlpha = 1;
  }
  function drawChart() {
    cctx.clearRect(0, 0, W, CH);
    cctx.font = '11px ' + FONT; cctx.textBaseline = 'middle';
    for (var g = 0; g <= ymax; g += 2) {
      var gy = cy(g);
      cctx.fillStyle = colors.line2; cctx.fillRect(LEFT, Math.round(gy), W - LEFT - RIGHT, 1);
      cctx.fillStyle = colors.faint; cctx.textAlign = 'right'; cctx.fillText(String(g), LEFT - 10, gy);
    }
    for (var i = FROM; i < N; i++) {                                    // an hour line every three hours
      var d = parts(data.start + i * step);
      if (d.minute === 0 && d.hour % 3 === 0) { cctx.fillStyle = colors.line2; cctx.fillRect(Math.round(colX(i) + colW() / 2), C_TOP, 1, CH - C_TOP - C_BOTTOM); }
    }
    data.events.forEach(function (e) {                                  // cluster events as purple bands
      var a = Math.max(indexAt(e.s), FROM), b = Math.min(e.e ? indexAt(e.e) : N, N);
      if (b <= a) return;
      cctx.globalAlpha = .13; cctx.fillStyle = colors.evt; cctx.fillRect(colX(a), C_TOP, Math.max((b - a) * colW(), 4), CH - C_TOP - C_BOTTOM); cctx.globalAlpha = 1;
    });
    rows.forEach(function (r) { if (!r.state) drawLine(r, colors.grey, 1, .32); });
    if (data.level !== null && data.level !== undefined) {
      var ly = Math.round(cy(data.level)) + .5;
      cctx.setLineDash([5, 4]); cctx.strokeStyle = colors.ink; cctx.lineWidth = 1.2; cctx.beginPath(); cctx.moveTo(LEFT, ly); cctx.lineTo(W - RIGHT, ly); cctx.stroke(); cctx.setLineDash([]);
      cctx.fillStyle = colors.ink; cctx.font = '600 11px ' + FONT; cctx.textAlign = 'left'; cctx.fillText('Alert level ' + Number(data.level).toFixed(1), LEFT + 6, ly - 8);
    }
    rows.forEach(function (r) { if (r.state === 'w') drawLine(r, colors.warn, 2.2, 1); });
    rows.forEach(function (r) { if (r.state === 'u') drawLine(r, colors.bad, 2.4, 1); });
    if (chartHover !== null) {
      var x = colX(chartHover) + colW() / 2;
      cctx.fillStyle = colors.ink; cctx.globalAlpha = .35; cctx.fillRect(Math.round(x), C_TOP, 1, CH - C_TOP - C_BOTTOM); cctx.globalAlpha = 1;
    }
  }

  // ---------- the map: a row per node ----------
  function drawMap() {
    ctx.clearRect(0, 0, W, H);
    ctx.font = '11px ' + FONT; ctx.textBaseline = 'middle';
    var cw = colW(), shown = FROM + Math.ceil((N - FROM) * progress);
    // time axis: a label every three hours, the date when a new day starts
    for (var i = FROM; i < N; i++) {
      var t = data.start + i * step, d = parts(t);
      if (d.minute === 0 && d.hour % 3 === 0 && colX(i) < W - RIGHT - 46) {    // leave room for "now"
        ctx.globalAlpha = 1; ctx.fillStyle = d.hour === 0 ? colors.ink : colors.faint; ctx.textAlign = 'center';
        ctx.fillText(d.hour === 0 ? dayLabel(t) : clock(t), colX(i) + cw / 2, AXIS / 2);
      }
    }
    ctx.globalAlpha = 1; ctx.textAlign = 'right'; ctx.fillStyle = colors.faint; ctx.fillText('now', W - RIGHT, AXIS / 2);
    // cluster events: a strip along the top and a faint column through every row
    data.events.forEach(function (e) {
      var a = Math.max(indexAt(e.s), FROM), b = Math.min(e.e ? indexAt(e.e) : N, N);
      if (b <= a) return;
      var x = colX(a), w = Math.max((b - a) * cw, 3);
      if (x > colX(shown)) return;
      var wd = Math.min(w, colX(shown) - x);
      ctx.globalAlpha = 1; ctx.fillStyle = colors.evt; ctx.fillRect(x, AXIS + 3, wd, BAND);
      ctx.globalAlpha = .10; ctx.fillRect(x, TOP - 2, wd, H - TOP);
    });
    ctx.globalAlpha = 1;
    // rows
    rows.forEach(function (r, ri) {
      var y = layout[ri].y, active = hover && hover.row === ri;
      if (r._header !== undefined) {
        ctx.textAlign = 'left'; ctx.fillStyle = colors.ink; ctx.font = '700 12.5px ' + FONT; ctx.fillText(r.group || 'Other nodes', 0, r._header + GROUP / 2 + 2);
        ctx.font = '11px ' + FONT;
      }
      var hot = r.state === 'u' ? colors.bad : r.state === 'w' ? colors.warntx : null;
      ctx.textAlign = 'left'; ctx.fillStyle = active ? colors.ink : hot || colors.mid;
      ctx.font = ((active || hot) ? '700 ' : '500 ') + '10.5px ' + FONT;
      ctx.fillText(r.node, 0, y + ROW / 2 + .5);
      for (var c = FROM; c < shown; c++) {
        var ch = r.cells.charAt(c), x = colX(c), w = Math.max(cw - 1.1, 1);
        if (ch === '.') {                                      // no data: an empty outlined square, so the blank part still reads as a grid
          ctx.fillStyle = colors.panel; ctx.fillRect(x, y, w, ROW);
          ctx.strokeStyle = '#d2cfc7'; ctx.lineWidth = .7; ctx.strokeRect(x + .35, y + .35, w - .7, ROW - .7);
          continue;
        }
        ctx.fillStyle = ch === 'u' ? colors.bad : ch === 'w' ? colors.warn : ch === 'e' ? colors.evt : ch === 'q' ? colors.quiet : colors.norm;
        ctx.fillRect(x, y, w, ROW);
      }
    });
    // an hour line every three hours over the rows, so a square can be read against the clock
    ctx.globalAlpha = .75; ctx.fillStyle = '#fff';
    for (var k = FROM; k < N; k++) {
      var dd = parts(data.start + k * step);
      if (dd.minute === 0 && dd.hour % 3 === 0) rows.forEach(function (r, ri) { ctx.fillRect(Math.round(colX(k) + cw / 2), layout[ri].y, 1, ROW); });
    }
    ctx.globalAlpha = 1;
    // when most of the day is blank, say so instead of leaving a field of empty squares unexplained
    var first = N;
    rows.forEach(function (r) { for (var q = 0; q < r.cells.length; q++) { if (r.cells.charAt(q) !== '.') { if (q < first) first = q; break; } } });
    if (first > FROM + 12 && first < N) {
      ctx.textAlign = 'left'; ctx.font = '12px ' + FONT; ctx.fillStyle = colors.mid; ctx.lineWidth = 6; ctx.strokeStyle = colors.panel; ctx.lineJoin = 'round';
      var msg = 'Nothing recorded before ' + clock(data.start + first * step) + ' ' + ZONE_LABEL;
      var mx = LEFT + 8, my = rows[0]._header !== undefined ? rows[0]._header + GROUP / 2 + 2 : TOP + 8;
      ctx.strokeText(msg, mx, my); ctx.fillText(msg, mx, my);
    }
    // hover guides
    if (hover) {
      ctx.globalAlpha = .1; ctx.fillStyle = colors.ink; ctx.fillRect(colX(hover.col), TOP - 4, Math.max(cw - 1, 1), H - TOP);
      var y2 = layout[hover.row].y;
      ctx.globalAlpha = 1; ctx.strokeStyle = colors.ink; ctx.lineWidth = 1.5; ctx.strokeRect(colX(hover.col) - .5, y2 - .5, Math.max(cw, 2), ROW + 1);
    }
    ctx.globalAlpha = 1;
  }

  var WORDS = { n: 'Normal', u: 'Unusual', w: 'Unusual, and someone is looking into it', e: 'Part of a cluster event', q: 'Not scored', '.': 'No data' };
  function span(c) { var from = data.start + c * step; return clock(from) + '–' + clock(from + step) + ' ' + ZONE_LABEL; }
  function describe(r, c) {
    var ch = r.cells.charAt(c), text = r.node + ' · ' + span(c) + ' · ' + WORDS[ch];
    if (scored(ch)) text += ' (score ' + (r.v[c] / 10).toFixed(1) + ')';
    return text;
  }
  function locate(ev) {
    var box = map.getBoundingClientRect(), x = ev.clientX - box.left, y = ev.clientY - box.top;
    if (y >= AXIS && y < AXIS + BAND + 6) {
      var idx = (x - LEFT) / colW() + FROM, found = null;
      data.events.forEach(function (e) { if (idx >= indexAt(e.s) - .3 && idx <= (e.e ? indexAt(e.e) : N) + .3) found = e; });
      return found ? { event: found } : null;
    }
    var col = Math.floor((x - LEFT) / colW()) + FROM;
    if (col < FROM || col >= N) return null;
    for (var i = 0; i < rows.length; i++) if (y >= layout[i].y - GAP / 2 && y < layout[i].y + ROW + GAP / 2) return { row: i, col: col };
    return null;
  }
  function place(canvas, ev, text) {
    var box = host.getBoundingClientRect();
    tip.textContent = text; tip.style.display = 'block';
    var x = ev.clientX - box.left + host.scrollLeft, y = ev.clientY - box.top;
    tip.style.left = Math.min(Math.max(x + 14, 6), host.scrollLeft + host.clientWidth - tip.offsetWidth - 6) + 'px';
    tip.style.top = Math.max(y - 38, 4) + 'px';
  }
  map.addEventListener('mousemove', function (ev) {
    var hit = locate(ev);
    if (!hit) { hover = null; tip.style.display = 'none'; map.classList.remove('hit'); drawMap(); return; }
    map.classList.add('hit');
    if (hit.event) {
      var e = hit.event; hover = null; drawMap();
      place(map, ev, 'Cluster event · ' + e.nodes + ' of ' + e.of + ' nodes · ' + clock(e.s) + (e.e ? '–' + clock(e.e) : ' onwards') + ' ' + ZONE_LABEL);
      return;
    }
    hover = hit; drawMap(); place(map, ev, describe(rows[hit.row], hit.col));
  });
  map.addEventListener('mouseleave', function () { hover = null; tip.style.display = 'none'; drawMap(); });
  map.addEventListener('click', function (ev) {
    var hit = locate(ev); if (!hit) return;
    window.location.href = hit.event ? hit.event.url : rows[hit.row].url;
  });

  // the chart: the column under the pointer, how many nodes were above the alert level, and the highest
  chart.addEventListener('mousemove', function (ev) {
    var box = chart.getBoundingClientRect(), col = Math.floor((ev.clientX - box.left - LEFT) / colW()) + FROM;
    if (col < FROM || col >= N) { chartHover = null; tip.style.display = 'none'; drawChart(); return; }
    chartHover = col;
    var above = 0, top = null;
    rows.forEach(function (r) {
      if (!scored(r.cells.charAt(col))) return;
      var v = r.v[col] / 10;
      if (data.level !== null && v >= data.level) above++;
      if (!top || v > top.v) top = { v: v, node: r.node };
    });
    drawChart();
    var text = span(col) + ' · ';
    text += top ? (data.level !== null ? above + ' node' + (above === 1 ? '' : 's') + ' above the alert level · ' : '') + 'highest ' + top.node + ' (' + top.v.toFixed(1) + ')' : 'no node was scored';
    place(chart, ev, text);
  });
  chart.addEventListener('mouseleave', function () { chartHover = null; tip.style.display = 'none'; drawChart(); });

  function render() { readColors(); measure(); drawChart(); drawMap(); }
  var reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  render();
  if (!reduce) {                                   // one reveal, left to right, when the page opens
    var t0 = null; progress = 0; drawMap();
    (function frame(now) {
      if (t0 === null) t0 = now;
      var k = Math.min((now - t0) / 650, 1); progress = 1 - Math.pow(1 - k, 3); drawMap();
      if (k < 1) requestAnimationFrame(frame); else progress = 1;
    })(performance.now());
  }
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(function () { drawChart(); drawMap(); });
  var wait; window.addEventListener('resize', function () { clearTimeout(wait); wait = setTimeout(render, 80); });
})();
