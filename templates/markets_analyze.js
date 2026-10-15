/* Beehive Wire Market Desk — chart analysis in the browser, for the quote box.
   A line-for-line port of analyze(), rs_score() and angle() in markets.py, so any ticker typed into the box gets
   the same base, buy point, buy range, status and RS rank the desk's tables show. If you change the rules in
   markets.py, change them here too (test: python relay/parity_test.py). */
(function (root) {
  var NaN_ = NaN;
  function isnan(x) { return x == null || x !== x; }
  // Python's round(x, n): correctly rounded on the binary value (ties are vanishingly rare with real prices).
  function rnd(x, n) { if (isnan(x)) return null; var s = Number(x).toFixed(n); return Number(s); }
  function pct(a, b) { return (a != null && b) ? rnd((a / b - 1) * 100, 1) : null; }
  function mean(a, i, j) { var s = 0, n = 0; for (var k = i; k < j; k++) { s += a[k]; n++; } return n ? s / n : NaN_; }
  function maxOf(a, i, j) { var m = -Infinity; for (var k = i; k < j; k++) if (a[k] > m) m = a[k]; return m; }
  function minOf(a, i, j) { var m = Infinity; for (var k = i; k < j; k++) if (a[k] < m) m = a[k]; return m; }
  function argmax(a, i, j) { var m = -Infinity, w = i; for (var k = i; k < j; k++) if (a[k] > m) { m = a[k]; w = k; } return w; }
  function argmin(a, i, j) { var m = Infinity, w = i; for (var k = i; k < j; k++) if (a[k] < m) { m = a[k]; w = k; } return w; }

  function sma(x, n) {
    var out = x.map(function () { return NaN_; });
    if (x.length >= n) { var s = 0; for (var i = 0; i < x.length; i++) { s += x[i]; if (i >= n) s -= x[i - n]; if (i >= n - 1) out[i] = s / n; } }
    return out;
  }
  function ema(x, n) {
    var out = x.map(function () { return NaN_; });
    if (x.length < n) return out;
    var k = 2 / (n + 1); out[n - 1] = mean(x, 0, n);
    for (var i = n; i < x.length; i++) out[i] = x[i] * k + out[i - 1] * (1 - k);
    return out;
  }
  function atr(h, l, c, n) {
    n = n || 14;
    var out = c.map(function () { return NaN_; });
    if (c.length < n + 1) return out;
    var tr = c.map(function (_, i) {
      var pc = i === 0 ? c[0] : c[i - 1];
      return Math.max(h[i] - l[i], Math.abs(h[i] - pc), Math.abs(l[i] - pc));
    });
    out[n] = mean(tr, 1, n + 1);
    for (var i = n + 1; i < c.length; i++) out[i] = (out[i - 1] * (n - 1) + tr[i]) / n;
    return out;
  }
  function rsScore(c) {
    var n = c.length; if (n < 30) return null;
    function r(a, b) { if (n < a) return null; var end = b === 0 ? c[n - 1] : c[n - b]; return end / c[n - a] - 1; }
    var terms = [[3.0, r(26, 0)], [2.0, r(64, 26)], [1.0, r(127, 64)], [1.0, r(190, 127)], [0.5, r(253, 190)]].filter(function (t) { return t[1] != null; });
    if (!terms.length) return null;
    return terms.reduce(function (s, t) { return s + t[0] * t[1]; }, 0);
  }
  // RS rank 1-99 against the desk's latest universe (rs_dist.json, saved every pass): same formula as markets.py.
  function rsRank(score, ordered) {
    if (score == null || !ordered || !ordered.length) return null;
    var lo = 0, hi = ordered.length; while (lo < hi) { var mid = (lo + hi) >> 1; if (ordered[mid] < score) lo = mid + 1; else hi = mid; }
    return Math.max(1, Math.min(99, Math.round(100 * (lo + 1) / (ordered.length + 1))));
  }

  var STATUS_LABEL = { breakout: "Breakout", buy_zone: "In buy range", extended: "Extended", retest: "Back below buy point",
                       setting_up: "Setting up", in_base: "In base", no_base: "No base" };

  function analyze(sym, bars, opt) {
    opt = opt || {};
    var O = bars.o, H = bars.h, L = bars.l, C = bars.c, V = bars.v, dates = bars.d;
    var t = C.length - 1; if (t < 15) return null;
    var ipo = C.length < 130 ? C.length : null;
    var s21 = ema(C, 21), s50 = sma(C, 50), s200 = sma(C, 200);
    var avgv50 = t >= 10 ? mean(V, Math.max(0, t - 50), t) : V[t];
    var volRatio = avgv50 ? rnd(V[t] / avgv50, 2) : null;
    var hi252 = maxOf(H, Math.max(0, t - 252), t + 1), lo252 = minOf(L, Math.max(0, t - 252), t + 1);

    function findBase(start, minLen, ks) {
      for (var q = 0; q < ks.length; q++) {
        var k = ks[q], loI = Math.max(start, t - 320), hiI = t - k;
        if (hiI <= loI + 3) continue;
        var r = argmax(H, loI, hiI + 1), pivot = H[r], b = null;
        for (var j = r + 1; j <= t; j++) if (C[j] > pivot) { b = j; break; }
        var end = b != null ? b : t, length = end - r;
        var low = minOf(L, r, end + 1), depth = end > r ? (pivot - low) / pivot : 0;
        if (length >= minLen && depth >= 0.03)
          return { rim: r, pivot: pivot, breakout: b, length: length, depth: rnd(depth * 100, 1), low: low, low_i: argmin(L, r, end + 1) };
      }
      return null;
    }
    var base = findBase(0, ipo ? 8 : 20, ipo ? [5, 10, 15, 25, 40] : [15, 25, 40, 60]);
    if (base && base.breakout == null && C[t] < base.pivot * 0.90) {
      var inner = findBase(base.low_i + 1, ipo ? 8 : 20, [5, 10, 15, 25, 40]);
      if (inner && inner.rim > base.low_i) {
        inner.inside = { high: rnd(base.pivot, 2), high_date: dates[base.rim], low: rnd(base.low, 2), low_date: dates[base.low_i] };
        base = inner;
      }
    }
    var zone = opt.buyZonePct != null ? opt.buyZonePct : 5;
    var status = "no_base", fromPivot = null, daysSince = null, boVol = null;
    if (base) {
      var buyPoint = rnd(base.pivot + 0.10, 2);
      fromPivot = pct(C[t], buyPoint);
      if (base.breakout != null) {
        var bi = base.breakout; daysSince = t - bi;
        var pv = mean(V, Math.max(0, bi - 50), bi) || 1.0;
        boVol = rnd(V[bi] / pv, 2);
        status = fromPivot < 0 ? "retest" : fromPivot <= zone ? (daysSince <= 5 ? "breakout" : "buy_zone") : "extended";
      } else status = fromPivot >= -5 ? "setting_up" : "in_base";
      base.buy_point = buyPoint;
      base.buy_max = rnd(buyPoint * (1 + zone / 100), 2);
      base.kind = ipo ? "IPO base" : base.depth <= 15 ? "flat base" : base.depth <= 35 ? "cup" : "deep cup";
      base.weeks = Math.max(1, Math.round(base.length / 5));
    }
    var chg = rnd(C[t] - C[t - 1], 2), chgPct = pct(C[t], C[t - 1]);
    var above50 = 0;
    if (!isnan(s50[t])) { var n50 = 0, a50 = 0; for (var i = Math.max(0, t - 50); i <= t; i++) { n50++; if (C[i] > s50[i]) a50++; } above50 = a50 / n50; }
    var bounce50 = !isnan(s50[t]) && L[t] <= s50[t] * 1.02 && C[t] > s50[t] && C[t] > C[t - 1] && above50 >= 0.7;
    var nmC = -Infinity, nmS = -Infinity;
    for (var i2 = t - 5; i2 < t; i2++) { if (!isnan(C[i2]) && C[i2] > nmC) nmC = C[i2]; if (!isnan(s50[i2]) && s50[i2] > nmS) nmS = s50[i2]; }
    var breakdown50 = !isnan(s50[t]) && C[t] < s50[t] * 0.98 && nmS > -Infinity && nmC > nmS && above50 >= 0.6;
    var newHigh = H[t] >= hi252;
    var rs = rsScore(C);

    var a14 = atr(H, L, C, 14);
    // Distance from the 21-day EMA in ATRs: positive when the session's low held above
    // the line, negative when its high stayed under. Mirrors analyze() in markets.py.
    var extAtr = null;
    if (!isnan(s21[t]) && !isnan(a14[t]) && a14[t] > 0) {
      if (L[t] > s21[t]) extAtr = rnd((L[t] - s21[t]) / a14[t], 2);
      else if (H[t] < s21[t]) extAtr = rnd((H[t] - s21[t]) / a14[t], 2);
      else extAtr = 0;
    }

    var nb = opt.chartBars || 130, s0 = Math.max(0, t - nb + 1);
    function sl(a, f) { return a.slice(s0).map(f); }
    var series = { d: dates.slice(s0), o: sl(O, function (x) { return rnd(x, 2); }), h: sl(H, function (x) { return rnd(x, 2); }),
                   l: sl(L, function (x) { return rnd(x, 2); }), c: sl(C, function (x) { return rnd(x, 2); }), v: sl(V, function (x) { return Math.trunc(x); }),
                   e21: sl(s21, function (x) { return isnan(x) ? null : rnd(x, 2); }), s50: sl(s50, function (x) { return isnan(x) ? null : rnd(x, 2); }),
                   s200: sl(s200, function (x) { return isnan(x) ? null : rnd(x, 2); }), avgv50: Math.round(avgv50) };
    var baseOut = null;
    if (base) {
      series.rim_i = base.rim - s0; series.low_i = base.low_i - s0; series.bo_i = base.breakout != null ? base.breakout - s0 : null;
      baseOut = { pivot: base.pivot, breakout: base.breakout, length: base.length, depth: base.depth, low: base.low, buy_point: base.buy_point,
                  buy_max: base.buy_max, kind: base.kind, weeks: base.weeks, rim_date: dates[base.rim], low_date: dates[base.low_i],
                  breakout_date: base.breakout != null ? dates[base.breakout] : null };
      if (base.inside) baseOut.inside = base.inside;
    }
    var f = { sym: sym, date: dates[t], close: rnd(C[t], 2), chg: chg, chg_pct: chgPct, volume: Math.trunc(V[t]), avg_vol50: Math.round(avgv50),
              vol_ratio: volRatio, hi52: rnd(hi252, 2), lo52: rnd(lo252, 2), from_hi52: pct(C[t], hi252),
              ema21: isnan(s21[t]) ? null : rnd(s21[t], 2), sma50: isnan(s50[t]) ? null : rnd(s50[t], 2), sma200: isnan(s200[t]) ? null : rnd(s200[t], 2),
              atr14: isnan(a14[t]) ? null : rnd(a14[t], 2), vs_ema21: isnan(s21[t]) ? null : pct(C[t], s21[t]), ext_atr: extAtr,
              vs_sma50: isnan(s50[t]) ? null : pct(C[t], s50[t]), vs_sma200: isnan(s200[t]) ? null : pct(C[t], s200[t]),
              rs_score: rs, base: baseOut, status: status, from_pivot: fromPivot, days_since_breakout: daysSince, breakout_vol_ratio: boVol,
              new_high: newHigh, bounce50: bounce50, breakdown50: breakdown50, ipo_sessions: ipo, series: series };
    f.angle = angle(f);
    return f;
  }

  function angle(f) {
    var ds = f.days_since_breakout != null ? f.days_since_breakout : 99;
    if (f.status === "breakout") return "breakout past buy point";
    if (f.status === "extended" && ds <= 3) return "breakout past buy point, already extended";
    if (f.bounce50) return "bounce off 50-day line";
    if (f.breakdown50) return "breaks 50-day line";
    if (f.status === "buy_zone") return "still in buy range after breakout";
    if (f.status === "retest") return "slips back below buy point";
    if (f.status === "setting_up") return "nearing buy point";
    if (f.ipo_sessions && f.new_high) return "new issue at a new high";
    if (f.new_high) return "new 52-week high";
    if (Math.abs(f.chg_pct || 0) >= 4) return "big move on the day";
    return STATUS_LABEL[f.status].toLowerCase();
  }

  root.BWA = { analyze: analyze, rsScore: rsScore, rsRank: rsRank, angle: angle };
})(typeof window !== "undefined" ? window : globalThis);
