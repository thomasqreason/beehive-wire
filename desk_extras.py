"""
Market Desk extras (Oct 24, 2026): weekly/monthly chart frames, aggressive early entries, the scorecard,
earnings-this-week, and phone alerts. Imported by markets.py; kept separate so the core stays readable.
"""
from __future__ import annotations

import json
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data" / "markets"


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


# ────────────────────────────────────────────────────────────────────────── weekly / monthly frames

def frames(df, weeks: int = 110, months: int = 36) -> dict:
    """Compact weekly (Friday-ended) and monthly OHLCV bars for the pop-up chart's tabs."""
    out = {}
    try:
        for key, rule, keep in (("w", "W-FRI", weeks), ("m", "ME", months)):
            r = df.resample(rule).agg({"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}).dropna()
            r = r.tail(keep)
            # the bucket's date is its last ACTUAL session, so the last bar is dated today, not next Friday
            last_dates = df["Close"].resample(rule).apply(lambda s: s.index[-1].date().isoformat() if len(s) else None).dropna().tail(keep)
            out[key] = {"d": list(last_dates.values), "o": [round(float(x), 2) for x in r["Open"]], "h": [round(float(x), 2) for x in r["High"]],
                        "l": [round(float(x), 2) for x in r["Low"]], "c": [round(float(x), 2) for x in r["Close"]],
                        "v": [int(x) for x in r["Volume"]]}
    except Exception as e:  # noqa: BLE001
        log(f"frames failed ({e})")
    return out


# ────────────────────────────────────────────────────────────────────────── aggressive (early) entries

def aggressive_entries(H, L, C, V, t: int, base: dict | None, s21, s50, status: str, dates: list) -> list[dict]:
    """Places a position can be started BEFORE the standard buy point, in the order IBD/Minervini teach them.
    Each entry: kind, price (the trigger), note, and 'live' = price is within reach today (-6% .. +3%).
    Only computed while the stock is still under its buy point (no breakout yet)."""
    import numpy as np
    out = []
    if not base or base.get("breakout") is not None or status in ("extended", "no_base"):
        return out
    pivot = float(base["pivot"])
    rim, low_i = int(base["rim"]), int(base["low_i"])
    close = float(C[t])

    def add(kind, price, note, where=None):
        price = round(float(price), 2)
        rel = (close / price - 1) * 100
        out.append({"kind": kind, "price": price, "note": note, "from_price": round(rel, 1), "live": -6 <= rel <= 3,
                    "date": dates[where] if where is not None else None})

    # 1. Handle high: a swing high in the last ~25 sessions, 3-15% under the pivot, with a shallow dip after it.
    lo_w = max(rim + 3, t - 25)
    best = None
    for i in range(t - 3, lo_w, -1):
        if i < 3:
            break
        if H[i] >= max(H[i - 3:i]) and H[i] >= max(H[i + 1:i + 4]) and pivot * 0.85 <= H[i] < pivot * 0.97:
            dip = (H[i] - float(np.min(L[i:t + 1]))) / H[i]
            if dip <= 0.15:
                best = i
                break
    if best is not None:
        add("handle high", H[best] + 0.10, f"the handle's high from {dates[best]}; clearing it is the early entry, "
                                             f"the standard buy point is {pivot + 0.10:.2f}", best)

    # 2. Downtrend-line break: the line from the base high through the highest lower high after it.
    if t - rim >= 10:
        j_lo, j_hi = rim + 4, t - 2
        if j_hi > j_lo:
            seg = H[j_lo:j_hi + 1]
            j = int(j_lo + np.argmax(seg))
            if H[j] < pivot and j > rim:
                slope = (H[j] - H[rim]) / (j - rim)
                if slope < 0:
                    line_t = H[rim] + slope * (t - rim)
                    line_p = H[rim] + slope * (t - 1 - rim)
                    crossed = close > line_t and C[t - 1] <= line_p
                    recent = any(C[k] > H[rim] + slope * (k - rim) >= C[k - 1] for k in range(max(rim + 2, t - 3), t + 1))
                    if close > line_t and (crossed or recent) and close < pivot:
                        add("downtrend line break", line_t, f"closed above the line drawn from the {dates[rim]} high through the "
                                                             f"{dates[j]} high; an early entry while the base finishes")

    # 3. Bounce off the 21-day or 50-day line inside the base (uptrend intact: price above the 50-day).
    if not (np.isnan(s50[t]) or np.isnan(s21[t])) and close > s50[t] and C[t] > C[t - 1]:
        if L[t] <= s50[t] * 1.02 and close > s50[t]:
            add("50-day bounce", H[t] + 0.10, "today's low touched the 50-day line and the stock closed above it; "
                                             "the entry is above today's high, the line is the stop", t)
        elif L[t] <= s21[t] * 1.01 and close > s21[t] and close > s50[t] * 1.03:
            add("21-day bounce", H[t] + 0.10, "pullback to the 21-day line and a close above it; the entry is above today's high", t)

    # 4. Shelf (the 'cheat' area): a tight 5+ session range in the right side of the base, 5%+ under the pivot.
    if t - low_i >= 8:
        for n in (5, 6, 7, 8, 10):
            if t - n + 1 <= low_i:
                break
            hh, ll = float(np.max(H[t - n + 1:t + 1])), float(np.min(L[t - n + 1:t + 1]))
            if hh and (hh - ll) / hh <= 0.06 and hh < pivot * 0.95 and close >= (hh + ll) / 2:
                add("shelf", hh + 0.10, f"a {n}-session shelf ({ll:.2f}–{hh:.2f}) in the right side of the base, "
                                        f"{(1 - hh / pivot) * 100:.0f}% under the buy point; clearing it is the 'cheat' entry")
                break
    return out[:3]


# ────────────────────────────────────────────────────────────────────────── the scorecard

SCORE_FILE = DATA / "scorecard.json"


def collect_calls() -> list[dict]:
    """Every actionable call the desk has printed since the first edition: a story or a watch-list row that was
    'breakout' or 'in buy range' that day. One call per symbol per base (same buy point within 1%)."""
    calls, last = [], {}
    for p in sorted(DATA.glob("????-??-??.json")):
        try:
            ed = json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        date = ed.get("date") or p.stem
        rows = []
        for s in ed.get("stories", []):
            if s.get("status") in ("breakout", "buy_zone"):
                rows.append(("story", s))
        for key in ("in_buy_range", "breakouts_week"):
            for r in (ed.get("lists") or {}).get(key, []) or []:
                if r.get("status") in ("breakout", "buy_zone"):
                    rows.append(("list", r))
        for src, r in rows:
            bp = r.get("buy_point") or (r.get("base") or {}).get("buy_point")
            if not bp or not r.get("close"):
                continue
            prev = last.get(r["sym"])
            if prev and abs(prev / bp - 1) < 0.01:
                continue
            last[r["sym"]] = bp
            calls.append({"date": date, "sym": r["sym"], "kind": r["status"], "source": src, "call_price": r["close"],
                          "buy_point": bp, "buy_max": r.get("buy_max") or (r.get("base") or {}).get("buy_max"),
                          "rs_rank": r.get("rs_rank")})
    return calls


def score_calls(calls: list[dict], prices: dict, bench) -> dict:
    """Outcomes for each call from the price history: 1/4/8-week returns from the call price, the best and worst
    points since, whether the 8% stop (from the call price) was hit, and the S&P over the same stretch."""
    import numpy as np
    bench_c = None
    if bench is not None:
        bench_c = {d.date().isoformat(): float(v) for d, v in bench["Close"].items()}
        bdates = sorted(bench_c)
    scored = []
    for c in calls:
        d = prices.get(c["sym"])
        if d is None:
            continue
        dates = [x.date().isoformat() for x in d.index]
        if c["date"] not in dates:
            later = [i for i, x in enumerate(dates) if x >= c["date"]]
            if not later:
                continue
            i0 = later[0]
        else:
            i0 = dates.index(c["date"])
        C, H, L = (d[k].to_numpy(dtype=float) for k in ("Close", "High", "Low"))
        base_px = float(c["call_price"])
        row = dict(c)
        row["sessions"] = len(C) - 1 - i0
        row["now"] = round(float(C[-1]), 2)
        row["now_pct"] = round((C[-1] / base_px - 1) * 100, 1)
        for lbl, n in (("w1", 5), ("w4", 20), ("w8", 40)):
            row[lbl] = round((C[i0 + n] / base_px - 1) * 100, 1) if i0 + n < len(C) else None
        after_h, after_l = H[i0 + 1:i0 + 41], L[i0 + 1:i0 + 41]
        row["max_gain"] = round((float(np.max(after_h)) / base_px - 1) * 100, 1) if len(after_h) else None
        row["max_loss"] = round((float(np.min(after_l)) / base_px - 1) * 100, 1) if len(after_l) else None
        row["stopped"] = bool(len(after_l) and float(np.min(after_l)) <= base_px * 0.92)
        row["hit20"] = bool(len(after_h) and float(np.max(after_h)) >= base_px * 1.20)
        if bench_c:
            bi = [x for x in bdates if x >= dates[i0]]
            if bi:
                b0 = bench_c[bi[0]]
                row["spx_now_pct"] = round((bench_c[bdates[-1]] / b0 - 1) * 100, 1)
                for lbl, n in (("spx_w1", 5), ("spx_w4", 20), ("spx_w8", 40)):
                    k = bdates.index(bi[0]) + n
                    row[lbl] = round((bench_c[bdates[k]] / b0 - 1) * 100, 1) if k < len(bdates) else None
        scored.append(row)

    def stats(rows, key):
        vals = [r[key] for r in rows if r.get(key) is not None]
        if not vals:
            return None
        return {"n": len(vals), "avg": round(sum(vals) / len(vals), 1), "win": round(100 * sum(1 for v in vals if v > 0) / len(vals)),
                "avg_win": round(sum(v for v in vals if v > 0) / max(1, sum(1 for v in vals if v > 0)), 1),
                "avg_loss": round(sum(v for v in vals if v <= 0) / max(1, sum(1 for v in vals if v <= 0)), 1)}

    summary = {"n": len(scored), "stopped_pct": round(100 * sum(1 for r in scored if r["stopped"]) / max(1, len(scored))),
               "hit20_pct": round(100 * sum(1 for r in scored if r["hit20"]) / max(1, len(scored))),
               "w1": stats(scored, "w1"), "w4": stats(scored, "w4"), "w8": stats(scored, "w8"), "now": stats(scored, "now_pct"),
               "spx_w1": stats(scored, "spx_w1"), "spx_w4": stats(scored, "spx_w4"), "spx_w8": stats(scored, "spx_w8"), "spx_now": stats(scored, "spx_now_pct"),
               "by_kind": {k: stats([r for r in scored if r["kind"] == k], "now_pct") for k in ("breakout", "buy_zone")}}
    scored.sort(key=lambda r: (r["date"], r["sym"]), reverse=True)
    return {"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "first_date": min((c["date"] for c in calls), default=None),
            "summary": summary, "calls": scored}


def build_scorecard(prices: dict, bench, download_fn) -> dict | None:
    calls = collect_calls()
    if not calls:
        return None
    missing = sorted({c["sym"] for c in calls} - set(prices))
    if missing:
        prices = dict(prices)
        prices.update(download_fn(missing, "2y"))
    sc = score_calls(calls, prices, bench)
    DATA.mkdir(parents=True, exist_ok=True)
    SCORE_FILE.write_text(json.dumps(sc, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    s = sc["summary"]
    log(f"scorecard: {s['n']} calls since {sc['first_date']}; now avg {s['now']['avg'] if s['now'] else '–'}%, "
        f"win {s['now']['win'] if s['now'] else '–'}%, stopped {s['stopped_pct']}%, 20%+ {s['hit20_pct']}%")
    return sc


def load_scorecard() -> dict | None:
    if SCORE_FILE.is_file():
        try:
            return json.loads(SCORE_FILE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return None
    return None


# ────────────────────────────────────────────────────────────────────────── earnings this week

CAL_DIR = DATA / "fund_cache"


def next_earnings_cached(sym: str, max_age_days: int = 2) -> str | None:
    """The next earnings date from Yahoo's calendar, cached two days per symbol (fund_cache/SYM.cal.json)."""
    CAL_DIR.mkdir(parents=True, exist_ok=True)
    p = CAL_DIR / f"{sym}.cal.json"
    if p.is_file():
        try:
            c = json.loads(p.read_text(encoding="utf-8"))
            if (datetime.now(timezone.utc) - datetime.fromisoformat(c["fetched"])).days < max_age_days:
                return c.get("next_earnings")
        except Exception:  # noqa: BLE001
            pass
    nxt = None
    try:
        import yfinance as yf
        cal = yf.Ticker(sym).calendar or {}
        ed = cal.get("Earnings Date") if isinstance(cal, dict) else None
        if ed:
            v = ed[0] if isinstance(ed, (list, tuple)) else ed
            nxt = str(v)[:10]
    except Exception:  # noqa: BLE001
        return None  # not cached; retry next run
    p.write_text(json.dumps({"fetched": datetime.now(timezone.utc).isoformat(timespec="seconds"), "next_earnings": nxt}), encoding="utf-8")
    return nxt


def earnings_week(facts: list[dict], days_ahead: int = 7) -> list[dict]:
    """Watch-list names reporting within the next `days_ahead` days (plus any that reported in the last 2)."""
    from datetime import date, timedelta
    today = date.today()
    out = []
    for f in facts:
        nxt = f.get("next_earnings")
        if not nxt:
            continue
        try:
            d = date.fromisoformat(nxt[:10])
        except ValueError:
            continue
        if today - timedelta(days=2) <= d <= today + timedelta(days=days_ahead):
            out.append({"sym": f["sym"], "name": f.get("name"), "date": d.isoformat(), "days": (d - today).days, "close": f["close"],
                        "status": f["status"], "rs_rank": f.get("rs_rank"), "from_pivot": f.get("from_pivot"),
                        "buy_point": (f.get("base") or {}).get("buy_point"), "vs_sma50": f.get("vs_sma50")})
    out.sort(key=lambda r: (r["date"], -(r["rs_rank"] or 0)))
    return out


# ────────────────────────────────────────────────────────────────────────── alerts (ntfy)

SENT_FILE = DATA / "alerts_sent.json"


def _sent() -> dict:
    if SENT_FILE.is_file():
        try:
            return json.loads(SENT_FILE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}
    return {}


def send_alerts(items: list[dict], site_url: str) -> int:
    """items: {key, title, body, url?}. Sends each once (keys kept in data/markets/alerts_sent.json, pruned to 10 days).
    Topic comes from the NTFY_TOPIC environment variable (a repo secret); no topic = nothing sent, quietly."""
    topic = (os.environ.get("NTFY_TOPIC") or "").strip()
    if not topic or not items:
        return 0
    import requests
    sent = _sent()
    today = datetime.now(timezone.utc).date().isoformat()
    sent = {k: v for k, v in sent.items() if (datetime.now(timezone.utc).date() - datetime.fromisoformat(v).date()).days <= 10}
    n = 0
    for it in items:
        if it["key"] in sent:
            continue
        title = it["title"].encode("ascii", "ignore").decode()
        body = it["body"].encode("ascii", "ignore").decode()
        try:
            r = requests.post(f"https://ntfy.sh/{topic}", data=body.encode(), timeout=10,
                              headers={"Title": title, "Click": it.get("url") or site_url, "Tags": it.get("tag", "chart_with_upwards_trend")})
            if r.status_code < 300:
                sent[it["key"]] = today
                n += 1
        except Exception as e:  # noqa: BLE001
            log(f"alert failed ({e})")
    DATA.mkdir(parents=True, exist_ok=True)
    SENT_FILE.write_text(json.dumps(sent, indent=0), encoding="utf-8")
    if n:
        log(f"alerts: {n} sent")
    return n


def breakout_alerts(facts: list[dict], site_url: str, live_note: str = "") -> list[dict]:
    """Watch-list names that cleared a buy point today. Volume is quoted in the message, never required."""
    out = []
    for f in facts:
        b = f.get("base") or {}
        if f.get("days_since_breakout") == 0 and b.get("buy_point"):
            out.append({"key": f"bo:{f['sym']}:{f['date']}", "title": f"{f['sym']} clears {b['buy_point']:.2f} buy point",
                        "body": f"{f['sym']} {f['close']:.2f} ({f.get('from_pivot', 0):+.1f}% past the buy point), volume {(f.get('vol_ratio') or 0):.1f}x average"
                                f"{', ' + live_note if live_note else ''}. Buy range to {b.get('buy_max', 0):.2f}.",
                        "url": f"{site_url}markets/#chart={f['sym']}"})
    return out


def holdings_alerts(facts: list[dict], site_url: str) -> list[dict]:
    """For the symbols in the MY_SYMBOLS secret (comma-separated): a close under the 50-day line, or 7%+ under a buy
    point after a breakout."""
    mine = {s.strip().upper() for s in (os.environ.get("MY_SYMBOLS") or "").split(",") if s.strip()}
    out = []
    for f in facts:
        if f["sym"] not in mine:
            continue
        if f.get("breakdown50"):
            out.append({"key": f"bd:{f['sym']}:{f['date']}", "title": f"{f['sym']} breaks the 50-day line", "tag": "warning",
                        "body": f"{f['sym']} closed {f['close']:.2f}, {f.get('vs_sma50', 0):+.1f}% vs the 50-day line. Sell rule territory.",
                        "url": f"{site_url}markets/#chart={f['sym']}"})
        elif f.get("status") == "retest" and (f.get("from_pivot") or 0) <= -7:
            out.append({"key": f"rt:{f['sym']}:{f['date']}", "title": f"{f['sym']} 7%+ under its buy point", "tag": "warning",
                        "body": f"{f['sym']} {f['close']:.2f}, {f['from_pivot']:+.1f}% from the buy point after a breakout. The 7-8% rule.",
                        "url": f"{site_url}markets/#chart={f['sym']}"})
    return out


def big_picture_alert(state: str, note: str, prev_state: str | None, date: str, site_url: str) -> list[dict]:
    if prev_state and state != prev_state:
        return [{"key": f"bp:{date}:{state}", "title": f"Market: {state}", "tag": "newspaper",
                 "body": f"The Big Picture moved from '{prev_state}' to '{state}'. {note}", "url": f"{site_url}markets/"}]
    return []


# ────────────────────────────────────────────────────────────────────────── the Webster screen (Oct 16, 2026 video)

WEBSTER_FILE = DATA / "webster.json"


def webster_screen(prices: dict, rs_rank_of, idents: dict, cfg: dict, now, live: bool, frac: float) -> dict | None:
    """Mike Webster's market-health screen: price above $10, dollar volume at least $25M (or $75M for a short list),
    above BOTH the 50-day and 200-day simple moving averages. Then grouped by sector and sorted by strength, so the
    sector table shows where money is flowing (most names passing, best week) and where the carnage is (worst week,
    fewest passing). Runs on every Stocks-on-the-Move pass over the same ~600-name universe."""
    import numpy as np
    rows, members = [], {}
    for sym, d in prices.items():
        if sym.startswith("^") or len(d) < 205:
            continue
        C, V = d["Close"].to_numpy(dtype=float), d["Volume"].to_numpy(dtype=float)
        if live and frac > 0:  # today's partial volume is not a day's volume; the average uses the prior 50 full sessions
            avgv = float(np.mean(V[-51:-1]))
        else:
            avgv = float(np.mean(V[-50:]))
        close = float(C[-1])
        s50, s200 = float(np.mean(C[-50:])), float(np.mean(C[-200:]))
        wk = (close / float(C[-6]) - 1) * 100 if len(C) > 6 else None
        mo = (close / float(C[-22]) - 1) * 100 if len(C) > 22 else None
        sector = (idents.get(sym) or {}).get("sector") or "Unclassified"
        dv = close * avgv
        members.setdefault(sector, []).append({"sym": sym, "wk": wk})
        if close < 10 or dv < 25e6 or not (close > s50 and close > s200):
            continue
        rows.append({"sym": sym, "name": (idents.get(sym) or {}).get("name"), "industry": (idents.get(sym) or {}).get("industry"),
                     "sector": sector, "close": round(close, 2), "dv": int(dv), "dv75": dv >= 75e6, "rs": rs_rank_of(sym),
                     "wk": round(wk, 1) if wk is not None else None, "mo": round(mo, 1) if mo is not None else None,
                     "vs50": round((close / s50 - 1) * 100, 1), "vs200": round((close / s200 - 1) * 100, 1)})
    sectors = []
    for sec, mem in members.items():
        passing = [r for r in rows if r["sector"] == sec]
        p75 = [r for r in passing if r["dv75"]]
        wks = [m["wk"] for m in mem if m["wk"] is not None]
        rs_vals = [r["rs"] for r in passing if r["rs"] is not None]
        passing.sort(key=lambda r: -(r["rs"] or 0))
        sectors.append({"sector": sec, "members": len(mem), "passing": len(passing), "passing75": len(p75),
                        "pct": round(100 * len(passing) / len(mem)) if mem else 0,
                        "wk": round(sum(wks) / len(wks), 1) if wks else None,
                        "avg_rs": round(sum(rs_vals) / len(rs_vals)) if rs_vals else None,
                        "leaders": [r["sym"] for r in passing[:5]],
                        "worst": sorted([m for m in mem if m["wk"] is not None], key=lambda m: m["wk"])[:3]})
    sectors.sort(key=lambda x: (-(x["wk"] if x["wk"] is not None else -99)))
    rows.sort(key=lambda r: (r["sector"], -(r["rs"] or 0)))
    out = {"as_of": now.isoformat(timespec="seconds"), "live": live, "universe": sum(len(m) for m in members.values()),
           "passing": len(rows), "passing75": sum(1 for r in rows if r["dv75"]), "sectors": sectors, "rows": rows}
    DATA.mkdir(parents=True, exist_ok=True)
    WEBSTER_FILE.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    log(f"webster screen: {len(rows)} of {out['universe']} pass at $25M ({out['passing75']} at $75M); "
        f"strongest sector {sectors[0]['sector'] if sectors else '–'}, weakest {sectors[-1]['sector'] if sectors else '–'}")
    return out


def load_webster() -> dict | None:
    if WEBSTER_FILE.is_file():
        try:
            return json.loads(WEBSTER_FILE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return None
    return None


# ────────────────────────────────────────────────────────────────────────── the desk score, per name

def cached_growth(sym: str) -> dict:
    """Latest-quarter growth from the fundamentals cache, without a network call. {} when not cached."""
    p = DATA / "fund_cache" / f"{sym}.json"
    if not p.is_file():
        return {}
    try:
        c = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    g = c.get("growth") or []
    out = {"have": True, "eps_yoy": g[0].get("eps_yoy") if g else None, "rev_yoy": g[0].get("rev_yoy") if g else None,
           "market_cap": c.get("market_cap"), "fetched": c.get("fetched")}
    if len(g) > 1 and g[0].get("eps_yoy") is not None and g[1].get("eps_yoy") is not None:
        out["eps_accel"] = g[0]["eps_yoy"] > g[1]["eps_yoy"]
    return out


def desk_score(f: dict, growth: dict | None) -> dict | None:
    """The Beehive 50 composite, computed for one name so the chart pop-up can show it with its parts:
    40% RS rank + 25% EPS growth (capped 200%) + 10% revenue growth (capped 100%) + 25% chart
    (25 each: above the 50-day, above the 200-day, within 15% of the 52-week high, in a buyable pattern;
    +10 for an RS-line high, +10 for accelerating EPS; chart capped at 100). Same arithmetic as leader_lists()."""
    if f.get("rs_rank") is None:
        return None
    g = growth or {}
    eps_yoy, rev_yoy = g.get("eps_yoy"), g.get("rev_yoy")
    eps = min(max(eps_yoy or 0, 0), 200) / 2
    rev = min(max(rev_yoy or 0, 0), 100)
    parts = []
    tech = 0
    if (f.get("vs_sma50") or -99) > 0:
        tech += 25; parts.append("above 50-day")
    if (f.get("vs_sma200") or -99) > 0:
        tech += 25; parts.append("above 200-day")
    if (f.get("from_hi52") or -99) >= -15:
        tech += 25; parts.append("within 15% of high")
    if f.get("status") in ("breakout", "buy_zone", "setting_up", "extended"):
        tech += 25; parts.append("buyable pattern")
    if (f.get("rs_line") or {}).get("new_high"):
        tech += 10; parts.append("RS line high")
    if g.get("eps_accel"):
        tech += 10; parts.append("EPS accelerating")
    tech = min(tech, 100)
    score = round(0.40 * f["rs_rank"] + 0.25 * eps + 0.10 * rev + 0.25 * tech, 1)
    return {"score": score, "rs": f["rs_rank"], "eps_yoy": eps_yoy, "rev_yoy": rev_yoy, "eps_pts": round(0.25 * eps, 1),
            "rev_pts": round(0.10 * rev, 1), "rs_pts": round(0.40 * f["rs_rank"], 1), "tech_pts": round(0.25 * tech, 1),
            "tech": tech, "tech_parts": parts, "have_fund": bool(g.get("have")),
            "note": None if g.get("have") else "fundamentals not fetched for this name yet; EPS and revenue count as zero"}
