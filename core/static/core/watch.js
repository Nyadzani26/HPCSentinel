/*
  The history map on the Overview: a row per node, a column per quarter of an hour, the last 24 hours.
  Drawn on a canvas from the data the server embeds in the page (see core/services/timeline.py).
  The node tiles below it are the accessible version of the same information.
*/
(function () {
  var host = document.getElementById('watch');
  var dataEl = document.getElementById('watch-data');
  if (!host || !dataEl) return;
  var data = JSON.parse(dataEl.textContent);
  var canvas = host.querySelector('canvas'), tip = host.querySelector('.watch-tip'), ctx = canvas.getContext('2d');
  var rows = data.rows, N = data.columns, step = data.step * 60000;
  var LEFT = 78, RIGHT = 6, AXIS = 16, BAND = 9, TOP = AXIS + BAND + 10, ROW = 10, GAP = 2, GROUP = 22, MIN_W = 300, FROM = 0;
  var groups = {}; rows.forEach(function (r) { groups[r.group] = true; });
  var grouped = Object.keys(groups).length > 1;
  var W = 0, H = 0, layout = [], colors = {}, hover = null, progress = 1, drawn = false;

  function css(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }
  function readColors() {
    colors = { ok: css('--ok'), bad: css('--bad'), shared: css('--shared'), quiet: css('--quiet'), ink: css('--ink'),
               muted: css('--muted'), panel: css('--panel'), rule: css('--rule-2') };
  }
  function pad(n) { return (n < 10 ? '0' : '') + n; }
  function clock(ms) { var d = new Date(ms); return pad(d.getUTCHours()) + ':' + pad(d.getUTCMinutes()); }
  var MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  function dayLabel(ms) { var d = new Date(ms); return d.getUTCDate() + ' ' + MONTHS[d.getUTCMonth()]; }

  function measure() {
    var avail = host.clientWidth - 2 * 20;
    W = Math.max(MIN_W, avail);
    // on a phone 96 columns would be hairlines: show the last 12 hours there, with the row labels still in view
    FROM = host.clientWidth < 620 ? Math.floor(N / 2) : 0;
    var title = document.getElementById('watch-h');
    if (title) title.textContent = 'Last ' + Math.round((N - FROM) * data.step / 60) + ' hours';
    var y = TOP, last = null;
    layout = rows.map(function (r) {
      if (grouped && r.group !== last) { y += last === null ? 0 : 6; r._header = y; y += GROUP; }
      last = r.group;
      var item = { y: y }; y += ROW + GAP; return item;
    });
    H = y + 4;
    var dpr = window.devicePixelRatio || 1;
    canvas.width = Math.round(W * dpr); canvas.height = Math.round(H * dpr);
    canvas.style.width = W + 'px'; canvas.style.height = H + 'px';
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }
  function colW() { return (W - LEFT - RIGHT) / (N - FROM); }
  function colX(i) { return LEFT + (i - FROM) * colW(); }
  function indexAt(ms) { return (ms - data.start) / step; }

  function draw() {
    ctx.clearRect(0, 0, W, H);
    ctx.font = '10px "Public Sans", system-ui, sans-serif'; ctx.textBaseline = 'middle';
    var cw = colW(), shown = FROM + Math.ceil((N - FROM) * progress);
    // time axis: a label every three hours, the date when a new day starts
    ctx.fillStyle = colors.muted; ctx.textAlign = 'left';
    for (var i = FROM; i < N; i++) {
      var t = data.start + i * step, d = new Date(t);
      if (d.getUTCMinutes() === 0 && d.getUTCHours() % 3 === 0 && colX(i) < W - RIGHT - 46) {    // leave room for "now"
        var label = d.getUTCHours() === 0 ? dayLabel(t) : clock(t);
        ctx.globalAlpha = 1; ctx.fillStyle = d.getUTCHours() === 0 ? colors.ink : colors.muted;
        ctx.fillText(label, colX(i), AXIS / 2);
        ctx.globalAlpha = .35; ctx.fillStyle = colors.muted; ctx.fillRect(Math.round(colX(i)), AXIS + 1, 1, 3);
      }
    }
    ctx.globalAlpha = 1; ctx.textAlign = 'right'; ctx.fillStyle = colors.muted; ctx.fillText('now', W - RIGHT, AXIS / 2);
    // cluster events along the top
    data.events.forEach(function (e) {
      var a = Math.max(indexAt(e.s), FROM), b = Math.min(e.e ? indexAt(e.e) : N, N);
      if (b <= a) return;
      var x = colX(a), w = Math.max((b - a) * cw, 3);
      if (x > colX(shown)) return;
      ctx.globalAlpha = 1; ctx.fillStyle = colors.shared; ctx.fillRect(x, AXIS + 3, Math.min(w, colX(shown) - x), BAND);
    });
    // rows
    rows.forEach(function (r, ri) {
      var y = layout[ri].y, active = hover && hover.row === ri;
      if (grouped && r._header !== undefined) {
        ctx.globalAlpha = 1; ctx.fillStyle = colors.muted; ctx.textAlign = 'left'; ctx.font = '600 11px "Public Sans", system-ui, sans-serif';
        ctx.fillText(r.group || 'Nodes', 0, r._header + GROUP / 2 + 1);
        ctx.font = '10px "Public Sans", system-ui, sans-serif';
      }
      ctx.globalAlpha = 1; ctx.textAlign = 'right'; ctx.fillStyle = active ? colors.ink : colors.muted;
      ctx.font = (active ? '650 ' : '') + '10px "Public Sans", system-ui, sans-serif';
      ctx.fillText(r.node, LEFT - 8, y + ROW / 2 + .5);
      for (var c = FROM; c < shown; c++) {
        var ch = r.cells.charAt(c);
        if (ch === '.') {                          // no data: a small dot, so the empty part still reads as a grid
          ctx.globalAlpha = .9; ctx.fillStyle = colors.rule;
          ctx.fillRect(Math.round(colX(c) + Math.max(cw - 1, 1) / 2 - 1), Math.round(y + ROW / 2 - 1), 2, 2); continue;
        }
        var color = ch === 'u' ? colors.bad : ch === 'e' ? colors.shared : colors[ch === 'n' ? 'ok' : 'quiet'];
        ctx.globalAlpha = ch === 'n' ? .3 : ch === 'q' ? .38 : 1;
        ctx.fillStyle = color; ctx.fillRect(colX(c), y, Math.max(cw - 1, 1), ROW);
      }
    });
    // when most of the day is blank, say so instead of leaving a field of dots unexplained
    var first = N;
    rows.forEach(function (r) { for (var k = 0; k < r.cells.length; k++) { if (r.cells.charAt(k) !== '.') { if (k < first) first = k; break; } } });
    if (first > FROM + 12 && first < N) {
      ctx.globalAlpha = 1; ctx.textAlign = 'left'; ctx.font = '12px "Public Sans", system-ui, sans-serif'; ctx.fillStyle = colors.muted;
      ctx.lineWidth = 6; ctx.strokeStyle = colors.panel; ctx.lineJoin = 'round';
      var msg = 'Nothing recorded before ' + clock(data.start + first * step) + ' UTC';
      var mx = LEFT + 8, my = grouped && rows[0]._header !== undefined ? rows[0]._header + GROUP / 2 + 1 : TOP + 8;
      ctx.strokeText(msg, mx, my); ctx.fillText(msg, mx, my);
      ctx.font = '10px "Public Sans", system-ui, sans-serif';
    }
    // hover guides
    if (hover) {
      ctx.globalAlpha = .1; ctx.fillStyle = colors.ink; ctx.fillRect(colX(hover.col), TOP - 4, Math.max(cw - 1, 1), H - TOP);
      var y2 = layout[hover.row].y;
      ctx.globalAlpha = 1; ctx.strokeStyle = colors.ink; ctx.lineWidth = 1.5; ctx.strokeRect(colX(hover.col) - .5, y2 - .5, Math.max(cw, 2), ROW + 1);
    }
    ctx.globalAlpha = 1;
  }

  var WORDS = { n: 'Normal', u: 'Unusual', e: 'Part of a cluster event', q: 'Not scored', '.': 'No data' };
  function describe(r, c) {
    var ch = r.cells.charAt(c), from = data.start + c * step, text = r.node + ' · ' + clock(from) + '–' + clock(from + step) + ' UTC · ' + WORDS[ch];
    if (ch === 'n' || ch === 'u') text += ' (score ' + (r.v[c] / 10).toFixed(1) + ')';
    return text;
  }
  function locate(ev) {
    var box = canvas.getBoundingClientRect(), x = ev.clientX - box.left, y = ev.clientY - box.top;
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
  function place(ev, text) {
    var box = host.getBoundingClientRect();
    tip.textContent = text; tip.style.display = 'block';
    var x = ev.clientX - box.left + host.scrollLeft, y = ev.clientY - box.top;
    tip.style.left = Math.min(Math.max(x + 14, 6), host.scrollLeft + host.clientWidth - tip.offsetWidth - 6) + 'px';
    tip.style.top = Math.max(y - 38, 4) + 'px';
  }
  canvas.addEventListener('mousemove', function (ev) {
    var hit = locate(ev);
    if (!hit) { hover = null; tip.style.display = 'none'; canvas.style.cursor = 'default'; draw(); return; }
    canvas.style.cursor = 'pointer';
    if (hit.event) {
      var e = hit.event; hover = null; draw();
      place(ev, 'Cluster event · ' + e.nodes + ' of ' + e.of + ' nodes · ' + clock(e.s) + (e.e ? '–' + clock(e.e) : ' onwards') + ' UTC');
      return;
    }
    hover = hit; draw(); place(ev, describe(rows[hit.row], hit.col));
  });
  canvas.addEventListener('mouseleave', function () { hover = null; tip.style.display = 'none'; draw(); });
  canvas.addEventListener('click', function (ev) {
    var hit = locate(ev); if (!hit) return;
    window.location.href = hit.event ? hit.event.url : rows[hit.row].url;
  });

  function render() { readColors(); measure(); draw(); }
  var reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  render();
  if (!reduce) {                                   // one reveal, left to right, when the page opens
    var t0 = null; progress = 0; draw();
    (function frame(now) {
      if (t0 === null) t0 = now;
      var k = Math.min((now - t0) / 650, 1); progress = 1 - Math.pow(1 - k, 3); draw();
      if (k < 1) requestAnimationFrame(frame); else progress = 1;
    })(performance.now());
  }
  var wait; window.addEventListener('resize', function () { clearTimeout(wait); wait = setTimeout(render, 80); });
  if (window.matchMedia) {
    var dark = window.matchMedia('(prefers-color-scheme: dark)');
    (dark.addEventListener ? dark.addEventListener.bind(dark, 'change') : dark.addListener.bind(dark))(render);
  }
})();
