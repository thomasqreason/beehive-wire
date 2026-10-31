/* Beehive Wire Market Desk — the live screen. During the regular session only, every minute (and whenever the page
   is opened or brought back to the screen), re-runs Volume Surges and the buy-range watch lists from live
   quotes. Each pass saves screen.json: per name, everything that doesn't change during the day (prior close, 50-day
   average volume, RS rank, the base and buy point, the 50-day-line pieces). This file adds today's price, high, low
   and volume from the relay and applies movers() and analyze()'s own rules. Outside the session it does nothing and
   the page shows the pass's tables. */
(function () {
  var G = typeof window !== 'undefined' ? window : globalThis;
  G.BWLiveFacts = facts;                     // for relay/parity_test.py
  if (typeof document === 'undefined') return;
  var RELAY = G.BW_RELAY, BASE = G.BW_MBASE || '', STORY = G.BW_STORY || [];
  if (!RELAY || !document.getElementById('mv-live')) return;
  var LABEL = { breakout: 'Breakout', buy_zone: 'In buy range', extended: 'Extended', retest: 'Back below buy point',
                setting_up: 'Setting up', in_base: 'In base', no_base: 'No base' };
  var SCREEN = null, busy = false, timer = null;

  function r1(x) { return Number(Number(x).toFixed(1)); }
  function pct(a, b) { return (a != null && b) ? r1((a / b - 1) * 100) : null; }
  function f2(x) { return Number(x).toFixed(2); }
  function sg(x) { return (x >= 0 ? '+' : '') + Number(x).toFixed(1) + '%'; }
  function esc(t) { return String(t == null ? '' : t).replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); }
  function et(now) {   // Eastern date and minutes of the day
    var p = new Intl.DateTimeFormat('en-US', { timeZone: 'America/New_York', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false })
      .formatToParts(now).reduce(function (o, x) { o[x.type] = x.value; return o; }, {});
    return { date: p.year + '-' + p.month + '-' + p.day, mins: (+p.hour % 24) * 60 + (+p.minute) };
  }
  function sessionFraction(now) {   // movers()'s session_fraction()
    var e = et(now), start = 9 * 60 + 30, end = (SCREEN.early_closes || []).indexOf(e.date) >= 0 ? 13 * 60 : 16 * 60;
    if (e.mins <= start + 5) return [0, false];
    if (e.mins >= end) return [1, false];
    return [Math.max(0.08, (e.mins - start) / (end - start)), true];
  }

  // One name, live: analyze()'s status, angle and base columns with today's bar added to the saved history.
  function facts(r, q, zone) {
    var price = q.p, prev = q.pc || r.prev;
    var f = { sym: r.sym, close: price, chg_pct: pct(price, prev), rs_rank: r.rs, rsd: r.rsd };
    var s50 = r.s49 != null ? (r.s49 + price) / 50 : null;
    var above50 = s50 != null ? (r.a50 + (price > s50 ? 1 : 0)) / (r.n50 + 1) : 0;
    f.bounce50 = !!(s50 && q.lo != null && q.lo <= s50 * 1.02 && price > s50 && price > prev && above50 >= 0.7);
    f.breakdown50 = !!(s50 && price < s50 * 0.98 && r.bdp && above50 >= 0.6);
    f.new_high = q.hi != null && q.hi >= r.hip;
    f.ipo_sessions = r.ipo ? r.ipo + 1 : null;
    f.status = 'no_base'; f.from_pivot = null; f.days_since_breakout = null;
    var b = { bp: r.bp, bm: r.bm, kind: r.kind, wk: r.wk, pv: r.pv, dsb: r.dsb };
    // analyze() measures from the inner base only while price is 10%+ under the old high; above that, the old base
    if (r.opv && price >= r.opv * 0.90) b = { bp: r.obp, bm: r.obm, kind: r.okind, wk: r.owk, pv: r.opv, dsb: null };
    if (b.bp) {
      var ds = b.dsb != null ? b.dsb : (price > b.pv ? 0 : null);   // dsb already counts today
      var fp = pct(price, b.bp);
      f.from_pivot = fp; f.days_since_breakout = ds;
      f.status = ds != null ? (fp < 0 ? 'retest' : fp <= zone ? (ds <= 5 ? 'breakout' : 'buy_zone') : 'extended') : (fp >= -5 ? 'setting_up' : 'in_base');
      f.buy_point = b.bp; f.buy_max = b.bm; f.base_kind = b.kind; f.base_weeks = b.wk;
    }
    f.angle = (typeof BWA !== 'undefined') ? BWA.angle(f) : LABEL[f.status].toLowerCase();
    return f;
  }

  function symCell(sym) {
    return STORY.indexOf(sym) >= 0 ? '<td class="sym story"><a href="#' + esc(sym) + '">' + esc(sym) + '</a></td>'
                                   : '<td class="sym"><a class="tk" href="#chart=' + esc(sym) + '" data-sym="' + esc(sym) + '">' + esc(sym) + '</a></td>';
  }
  function mvTable(rows, title) {
    if (!rows.length) return '';
    var h = '<div class="list"><b>' + esc(title) + '</b><div class="tblwrap"><table class="board mv"><tr><th>Sym</th><th>Last</th><th>Chg</th><th>Vol vs avg</th><th>RS</th><th>Chart</th><th>Buy point</th><th>vs buy pt</th></tr>';
    rows.forEach(function (r) {
      h += '<tr>' + symCell(r.sym) + '<td>' + f2(r.close) + '</td><td class="' + ((r.chg_pct || 0) >= 0 ? 'pos' : 'neg') + '">' + sg(r.chg_pct || 0) + '</td>'
        + '<td><b>+' + r.vol_pct + '%</b> <span class="st">(' + r.vol_ratio.toFixed(1) + 'x)</span></td><td>' + (r.rs_rank != null ? r.rs_rank : '–') + '</td>'
        + '<td class="st">' + esc(r.angle || LABEL[r.status] || '–') + (r.base_kind ? ' &middot; ' + r.base_weeks + 'w ' + esc(r.base_kind) : '') + '</td>'
        + '<td>' + (r.buy_point ? f2(r.buy_point) + '&ndash;' + f2(r.buy_max) : '–') + '</td>'
        + '<td class="' + ((r.from_pivot || 0) >= 0 ? 'pos' : 'neg') + '">' + (r.from_pivot != null ? sg(r.from_pivot) : '–') + '</td></tr>';
    });
    return h + '</table></div></div>';
  }
  function mini(rows) {
    if (!rows.length) return '<div class="none">None right now.</div>';
    var h = '<table class="mini">';
    rows.forEach(function (r) {
      h += '<tr>' + symCell(r.sym).replace('class="sym story"', 'class="sym"') + '<td>' + f2(r.close) + '</td><td>' + (r.buy_point ? 'buy ' + f2(r.buy_point) : '') + '</td>'
        + '<td class="' + ((r.from_pivot || 0) >= 0 ? 'pos' : 'neg') + '">' + (r.from_pivot != null ? sg(r.from_pivot) : '') + '</td>'
        + '<td class="st">RS ' + (r.rsd != null ? r.rsd : '–') + (r.base_weeks ? ' &middot; ' + r.base_weeks + 'w ' + esc(r.base_kind) : '') + (r.ipo_sessions ? ' &middot; ' + r.ipo_sessions + ' sessions' : '') + '</td></tr>';
    });
    return h + '</table>';
  }
  function clock(d) { return d.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit', timeZone: 'America/Denver' }) + ' MT'; }

  function load() {
    if (SCREEN) return Promise.resolve(SCREEN);
    return fetch(BASE + 'screen.json', { cache: 'no-cache' }).then(function (x) { return x.ok ? x.json() : null; }).then(function (j) {
      if (!j || !j.rows) return null;
      j.recs = j.rows.map(function (a) { var o = {}; j.cols.forEach(function (c, i) { o[c] = a[i]; }); return o; });
      return (SCREEN = j);
    });
  }
  function quotes(syms) {
    var batches = [];
    for (var i = 0; i < syms.length; i += 100) batches.push(syms.slice(i, i + 100));
    return Promise.all(batches.map(function (b) {
      return fetch(RELAY + (RELAY.indexOf('?') < 0 ? '?' : '&') + 'q=' + b.join(',')).then(function (x) { return x.ok ? x.json() : null; }).catch(function () { return null; });
    })).then(function (rs) {
      var out = {}, states = {};
      rs.forEach(function (j) {
        if (!j || !j.quotes) return;
        states[j.marketState] = (states[j.marketState] || 0) + 1;
        Object.keys(j.quotes).forEach(function (s) { var a = j.quotes[s]; out[s] = { p: a[0], pc: a[1], v: a[2], hi: a[3], lo: a[4], t: a[5] }; });
      });
      return { q: out, regular: (states.REGULAR || 0) > 0 && (states.REGULAR || 0) >= rs.length / 2 };
    });
  }

  function run() {
    if (busy || document.hidden) return;
    busy = true;
    load().then(function (S) {
      if (!S) return null;
      var now = new Date(), sf = sessionFraction(now);
      if (!sf[1]) return null;                                    // regular session only
      return quotes(S.recs.map(function (r) { return r.sym; })).then(function (Q) {
        if (!Q.regular) return;
        var c = S.criteria, zone = c.buy_zone_pct || 5, today = et(now).date, frac = sf[0], rows = [], desk = [];
        S.recs.forEach(function (r) {
          var q = Q.q[r.sym];
          if (!q || q.p == null || !q.t) return;
          if (et(new Date(q.t * 1000)).date !== today || today <= S.hist_date) return;   // must be today's bar, after the saved history
          var f = facts(r, q, zone);
          if (r.rsd != null) desk.push(f);
          var proj = (q.v || 0) / frac, ratio = r.avgv ? proj / r.avgv : 0;
          if (q.p < c.min_price || r.avgv < c.min_avg_volume || ratio < c.min_volume_ratio) return;
          f.vol_ratio = Math.round(ratio * 100) / 100; f.vol_pct = Math.round((ratio - 1) * 100);
          f.qualifies = (r.rs || 0) >= c.min_rs_rank;
          rows.push(f);
        });
        rows.sort(function (a, b) { return b.vol_ratio - a.vol_ratio; });
        var ups = rows.filter(function (r) { return r.chg_pct >= c.min_up_pct; }).slice(0, c.rows_up * 3);
        var downs = rows.filter(function (r) { return r.chg_pct <= -c.min_up_pct; }).slice(0, c.rows_down * 2);
        var upQ = ups.filter(function (r) { return r.qualifies; }).slice(0, c.rows_up);
        var upX = ups.filter(function (r) { return !r.qualifies; }).slice(0, Math.max(0, c.rows_up - upQ.length));
        var stamp = clock(now), pctDone = Math.round(frac * 100);
        var html = '<div class="livetag"><span class="dot"></span>LIVE &middot; ' + stamp + ' &middot; ' + pctDone + '% of the session done; volume projected to the full day. '
          + S.recs.length + ' names re-screened every minute during the regular session. <button type="button" class="relive" aria-label="Refresh now">&#8635;</button></div>'
          + mvTable(upQ, 'Up on volume — qualifiers (top ' + upQ.length + ')')
          + (upQ.length ? '' : '<div class="none" style="font-size:13px;color:var(--mut)">No qualifying up-on-volume names right now.</div>')
          + mvTable(upX, 'Up on volume — RS below ' + c.min_rs_rank + ' (for the record)')
          + mvTable(downs.slice(0, c.rows_down), 'Down on volume');
        var mv = document.getElementById('mv-live'); if (mv) mv.innerHTML = html;
        G.BW_LIVE_ON = true;
        var byRs = function (a, b) { return (b.rsd || 0) - (a.rsd || 0); };
        var lists = {
          breakouts_week: desk.filter(function (f) { return f.days_since_breakout != null && f.days_since_breakout <= 5; }),
          in_buy_range: desk.filter(function (f) { return f.status === 'breakout' || f.status === 'buy_zone'; }),
          near_buy_point: desk.filter(function (f) { return f.status === 'setting_up'; })
        };
        Object.keys(lists).forEach(function (k) {
          var el = document.getElementById('wl-' + k); if (!el) return;
          el.innerHTML = mini(lists[k].sort(byRs).slice(0, 15));
          var t = el.parentNode.querySelector('.livemini'); if (!t) { t = document.createElement('span'); t.className = 'livemini'; el.parentNode.insertBefore(t, el); }
          t.innerHTML = '<span class="dot"></span>Live ' + stamp;
        });
      });
    }).catch(function () {}).then(function () { busy = false; });
  }
  document.addEventListener('click', function (e) { if (e.target.closest && e.target.closest('.relive')) { e.preventDefault(); run(); } });
  document.addEventListener('visibilitychange', function () { if (!document.hidden) run(); });
  window.addEventListener('pageshow', run);
  run();
  timer = setInterval(run, 60000);
})();
