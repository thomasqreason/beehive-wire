#!/usr/bin/env python3
"""
Beehive Wire — Market Desk.

breakout-method stock articles, one edition a day after the close: breakouts, buy ranges, bases, leaders.
Every number in an article comes from price data computed here; Claude only writes the prose.

    python markets.py scan    --out data/build/markets_payload.json      # pull prices, find the stories (network)
    python markets.py publish --payload P [--articles A]                 # save the edition to data/markets/, render
    python markets.py render                                             # re-render site/markets/ from data/markets/
    python markets.py run                                                # scan -> desk prose -> publish (no Claude)

Editions live in data/markets/YYYY-MM-DD.json (committed). site/markets/ is regenerated every build.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data" / "markets"
SITE = ROOT / "site" / "markets"
CFG_FILE = ROOT / "markets.yaml"


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def load_cfg() -> dict:
    with open(CFG_FILE, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def universe(cfg: dict) -> list[str]:
    seen, out = set(), []
    for group in cfg.get("universe", {}).values():
        for t in group or []:
            t = str(t).strip().upper()
            if t and t not in seen:
                seen.add(t)
                out.append(t)
    return out


# ────────────────────────────────────────────────────────────────────────── prices

def download(tickers: list[str], period: str) -> dict:
    """Bulk download via yfinance in chunks with retries. Returns {sym: DataFrame(Open High Low Close Volume)}."""
    import pandas as pd
    import yfinance as yf

    out = {}
    chunk = 40
    for i in range(0, len(tickers), chunk):
        part = tickers[i:i + chunk]
        df = None
        for attempt in range(4):
            try:
                df = yf.download(part, period=period, interval="1d", group_by="ticker", auto_adjust=False,
                                 threads=True, progress=False, timeout=30)
                if df is not None and not df.empty:
                    break
                log(f"download {part[0]}..{part[-1]} came back empty; retry {attempt + 1}")
            except Exception as e:  # noqa: BLE001
                log(f"download {part[0]}..{part[-1]} failed ({e}); retry {attempt + 1}")
            time.sleep(10 * (attempt + 1))
        if df is None or df.empty:
            continue
        for sym in part:
            try:
                d = df[sym] if isinstance(df.columns, pd.MultiIndex) else df
            except KeyError:
                continue
            d = d[["Open", "High", "Low", "Close", "Volume"]].dropna()
            d = d[d["Volume"] > 0]
            if len(d) >= 15:                 # young IPOs (SpaceX) ride along with a few weeks of history
                out[sym] = d
        time.sleep(1.5)
    log(f"prices: {len(out)}/{len(tickers)} symbols")
    return out


def sma(x, n):
    import numpy as np
    x = np.asarray(x, dtype=float)
    out = np.full_like(x, np.nan)
    if len(x) >= n:
        c = np.cumsum(np.insert(x, 0, 0.0))
        out[n - 1:] = (c[n:] - c[:-n]) / n
    return out


def ema(x, n):
    import numpy as np
    x = np.asarray(x, dtype=float)
    out = np.full_like(x, np.nan)
    if len(x) < n:
        return out
    k = 2 / (n + 1)
    out[n - 1] = x[:n].mean()
    for i in range(n, len(x)):
        out[i] = x[i] * k + out[i - 1] * (1 - k)
    return out


def atr(h, l, c, n=14):
    """Wilder's Average True Range. Used to measure how far price has run from the
    21-day EMA in units of the stock's own daily range, so a $600 name and a $30 name
    are judged on the same scale."""
    import numpy as np
    h, l, c = (np.asarray(x, dtype=float) for x in (h, l, c))
    out = np.full_like(c, np.nan)
    if len(c) < n + 1:
        return out
    pc = np.concatenate(([c[0]], c[:-1]))
    tr = np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))
    out[n] = tr[1:n + 1].mean()
    for i in range(n + 1, len(c)):
        out[i] = (out[i - 1] * (n - 1) + tr[i]) / n
    return out


def rs_score(c) -> float | None:
    """MarketSurge-style recency-weighted 12-month return built from NON-overlapping periods:
    3.0*last 25 sessions + 2.0*rest of the latest quarter + 1.0*q2 + 1.0*q3 + 0.5*q4. Short histories drop old terms."""
    n = len(c)
    if n < 30:
        return None

    def r(a, b):  # return from index -a to index -b (a > b >= 0)
        if n < a:
            return None
        end = c[-1] if b == 0 else c[-b]
        return end / c[-a] - 1

    terms = [(3.0, r(26, 0)), (2.0, r(64, 26)), (1.0, r(127, 64)), (1.0, r(190, 127)), (0.5, r(253, 190))]
    terms = [(w, v) for w, v in terms if v is not None]
    return sum(w * v for w, v in terms) if terms else None


def pct(a, b):
    return round((a / b - 1) * 100, 1) if a is not None and b else None


def status_at(price: float, buy_point: float, buy_zone: float, days_since: int | None) -> tuple[str, float]:
    """analyze()'s chart status at a given price: (status, % from the buy point). `days_since` = sessions since the
    breakout close, None when the base has not broken out. A price through the buy point with no breakout close
    behind it (a premarket or after-hours print) reads as in the buy range or extended, never as a breakout: the
    close has not happened."""
    from_pivot = pct(price, buy_point)
    if days_since is not None:
        if from_pivot < 0:
            return "retest", from_pivot
        if from_pivot <= buy_zone:
            return ("breakout" if days_since <= 5 else "buy_zone"), from_pivot
        return "extended", from_pivot
    if price >= buy_point:
        return ("buy_zone" if from_pivot <= buy_zone else "extended"), from_pivot
    return ("setting_up" if from_pivot >= -5 else "in_base"), from_pivot


# ────────────────────────────────────────────────────────────────────────── chart analysis

def analyze(sym: str, df, bench, cfg: dict) -> dict | None:
    import numpy as np

    O, H, L, C, V = (df[k].to_numpy(dtype=float) for k in ("Open", "High", "Low", "Close", "Volume"))
    dates = [d.date().isoformat() for d in df.index]
    t = len(C) - 1
    if t < 15:
        return None
    ipo_sessions = len(C) if len(C) < 130 else None   # a new issue: fewer than ~6 months of trading
    s21, s50, s200 = ema(C, 21), sma(C, 50), sma(C, 200)
    a14 = atr(H, L, C, 14)
    avgv50 = float(np.mean(V[max(0, t - 50):t])) if t >= 10 else float(V[t])
    vol_ratio = round(V[t] / avgv50, 2) if avgv50 else None
    # How far the stock has run from its 21-day EMA, in ATRs. Positive = the session's LOW
    # is still above the line (an intact uptrend); negative = the HIGH is below it. Roughly
    # +3 and beyond is a stretched tape; 0 means price is straddling the line.
    ext_atr = None
    if not (np.isnan(s21[t]) or np.isnan(a14[t])) and a14[t] > 0:
        if L[t] > s21[t]:
            ext_atr = round((L[t] - s21[t]) / a14[t], 2)
        elif H[t] < s21[t]:
            ext_atr = round((H[t] - s21[t]) / a14[t], 2)
        else:
            ext_atr = 0.0
    hi252 = float(np.max(H[max(0, t - 252):t + 1]))
    lo252 = float(np.min(L[max(0, t - 252):t + 1]))

    # ── the base: the highest high of the window, excluding the last k sessions. A base needs
    #    min_len sessions from that high to the breakout (or to today) and a >= 3% dip.
    def find_base(start: int, min_len: int, ks: tuple) -> dict | None:
        for k in ks:
            lo_i, hi_i = max(start, t - 320), t - k
            if hi_i <= lo_i + 3:
                continue
            r = int(lo_i + np.argmax(H[lo_i:hi_i + 1]))
            pivot = float(H[r])
            b = None
            for j in range(r + 1, t + 1):
                if C[j] > pivot:
                    b = j
                    break
            end = b if b is not None else t
            length = end - r
            depth = (pivot - float(np.min(L[r:end + 1]))) / pivot if end > r else 0.0
            if length >= min_len and depth >= 0.03:
                return dict(rim=r, pivot=pivot, breakout=b, length=length, depth=round(depth * 100, 1),
                            low=float(np.min(L[r:end + 1])), low_i=int(r + np.argmin(L[r:end + 1])))
        return None

    # Regular names: a base is 20+ sessions. New issues: an IPO base can be as short as two weeks.
    base = find_base(0, 8 if ipo_sessions else 20, (15, 25, 40, 60) if not ipo_sessions else (5, 10, 15, 25, 40))
    # If the stock sits far under that high with no breakout, the actionable base is the one that formed
    # AFTER the correction low (an IPO base under the first-day spike, or a base in the right side of a cup).
    if base and base["breakout"] is None and C[t] < base["pivot"] * 0.90:
        inner = find_base(base["low_i"] + 1, 8 if ipo_sessions else 20, (5, 10, 15, 25, 40))
        if inner and inner["rim"] > base["low_i"]:
            inner["inside"] = {"high": round(base["pivot"], 2), "high_date": dates[base["rim"]],
                               "low": round(base["low"], 2), "low_date": dates[base["low_i"]]}
            base = inner
    buy_zone = cfg["desk"].get("buy_zone_pct", 5)
    status, from_pivot, days_since, bo_vol = "no_base", None, None, None
    if base:
        buy_point = round(base["pivot"] + 0.10, 2)
        if base["breakout"] is not None:
            bi = base["breakout"]
            days_since = t - bi
            pv = float(np.mean(V[max(0, bi - 50):bi])) or 1.0
            bo_vol = round(V[bi] / pv, 2)
        status, from_pivot = status_at(C[t], buy_point, buy_zone, days_since)
        base["buy_point"] = buy_point
        base["buy_max"] = round(buy_point * (1 + buy_zone / 100), 2)
        base["kind"] = "flat base" if base["depth"] <= 15 else ("cup" if base["depth"] <= 35 else "deep cup")
        if ipo_sessions:
            base["kind"] = "IPO base"
        base["weeks"] = max(1, round(base["length"] / 5))

    # ── flags
    chg = round(C[t] - C[t - 1], 2)
    chg_pct = pct(C[t], C[t - 1])
    above50 = float((C[max(0, t - 50):t + 1] > s50[max(0, t - 50):t + 1]).mean()) if not np.isnan(s50[t]) else 0.0
    bounce50 = bool(not np.isnan(s50[t]) and L[t] <= s50[t] * 1.02 and C[t] > s50[t] and C[t] > C[t - 1] and above50 >= 0.7)
    breakdown50 = bool(not np.isnan(s50[t]) and C[t] < s50[t] * 0.98
                       and np.nanmax(C[t - 5:t]) > np.nanmax(s50[t - 5:t]) and above50 >= 0.6)
    new_high = bool(H[t] >= hi252)
    rs = rs_score(C)
    rs_line = None
    line = None
    if bench is not None:
        bd = {d.date().isoformat(): float(v) for d, v in bench["Close"].items()}
        line = [C[i] / bd[dates[i]] if dates[i] in bd and bd[dates[i]] else None for i in range(len(C))]
        vals = [v for v in line[max(0, t - 252):] if v is not None]
        if vals and line[t] is not None:
            rs_line = {"new_high": bool(line[t] >= max(vals)), "from_high": pct(line[t], max(vals))}

    bars = int(cfg["desk"].get("chart_bars", 130))
    s0 = max(0, t - bars + 1)
    series = {
        "d": dates[s0:], "o": [round(x, 2) for x in O[s0:]], "h": [round(x, 2) for x in H[s0:]],
        "l": [round(x, 2) for x in L[s0:]], "c": [round(x, 2) for x in C[s0:]], "v": [int(x) for x in V[s0:]],
        "e21": [None if np.isnan(x) else round(x, 2) for x in s21[s0:]],
        "s50": [None if np.isnan(x) else round(x, 2) for x in s50[s0:]],
        "s200": [None if np.isnan(x) else round(x, 2) for x in s200[s0:]],
        "avgv50": round(avgv50),
    }
    if base:
        series["rim_i"] = base["rim"] - s0
        series["low_i"] = base["low_i"] - s0
        series["bo_i"] = (base["breakout"] - s0) if base["breakout"] is not None else None
    try:
        from desk_extras import frames, aggressive_entries
        series.update(frames(df))
        if line is not None:   # the RS line (stock / S&P 500), daily window and at each weekly bar's date
            series["rs"] = [None if v is None else round(v, 5) for v in line[s0:]]
            # the RS line at a new 52-week high on that bar (a running max over the prior 252 sessions)
            dots, run = [], None
            for i in range(len(line)):
                v = line[i]
                if v is None:
                    continue
                lo_w = max(0, i - 252)
                prior = [x for x in line[lo_w:i] if x is not None]
                if i >= 20 and (not prior or v >= max(prior)):
                    dots.append(i)
            series["rsdot"] = [i - s0 for i in dots if i >= s0]
            if series.get("w"):
                by_date = {dates[i]: line[i] for i in range(len(dates)) if line[i] is not None}
                series["w"]["rs"] = [None if by_date.get(d) is None else round(by_date[d], 5) for d in series["w"]["d"]]
                dot_dates = {dates[i] for i in dots}
                wd = series["w"]["d"]
                # a weekly bar gets the dot if any session of that week had one
                flags, k = [], 0
                for wi, wend in enumerate(wd):
                    wstart = wd[wi - 1] if wi else ""
                    flags.append(any(wstart < dd <= wend for dd in dot_dates))
                series["w"]["rsdot"] = [i for i, fl in enumerate(flags) if fl]
        aggressive = aggressive_entries(H, L, C, V, t, base, s21, s50, status, dates)
        series["blue_dot"] = (len(series["c"]) - 1) in (series.get("rsdot") or [])
    except Exception as e:  # noqa: BLE001
        aggressive = []
        log(f"{sym}: extras failed ({e})")

    ytd = None
    y0 = [i for i, d in enumerate(dates) if d[:4] == dates[t][:4]]
    if y0 and y0[0] > 0:
        ytd = pct(C[t], C[y0[0] - 1])
    base_out = None
    if base:
        base_out = {k: v for k, v in base.items() if k not in ("rim", "low_i")}
        base_out.update(rim_date=dates[base["rim"]], low_date=dates[base["low_i"]],
                        breakout_date=dates[base["breakout"]] if base["breakout"] is not None else None)
    return {
        "sym": sym, "date": dates[t], "close": round(C[t], 2), "chg": chg, "chg_pct": chg_pct,
        "volume": int(V[t]), "avg_vol50": round(avgv50), "vol_ratio": vol_ratio,
        "hi52": round(hi252, 2), "lo52": round(lo252, 2), "from_hi52": pct(C[t], hi252),
        "ema21": None if np.isnan(s21[t]) else round(s21[t], 2),
        "atr14": None if np.isnan(a14[t]) else round(a14[t], 2),
        "vs_ema21": None if np.isnan(s21[t]) else pct(C[t], s21[t]),
        "ext_atr": ext_atr,
        "sma50": None if np.isnan(s50[t]) else round(s50[t], 2),
        "sma200": None if np.isnan(s200[t]) else round(s200[t], 2),
        "vs_sma50": None if np.isnan(s50[t]) else pct(C[t], s50[t]),
        "vs_sma200": None if np.isnan(s200[t]) else pct(C[t], s200[t]),
        "ret_1m": pct(C[t], C[t - 21]) if t >= 21 else None, "ret_3m": pct(C[t], C[t - 63]) if t >= 63 else None,
        "ret_1y": pct(C[t], C[t - 252]) if t >= 252 else None, "ytd": ytd,
        "rs_score": rs, "rs_line": rs_line,
        "base": base_out,
        "status": status, "from_pivot": from_pivot, "days_since_breakout": days_since, "breakout_vol_ratio": bo_vol,
        "new_high": new_high, "bounce50": bounce50, "breakdown50": breakdown50, "ipo_sessions": ipo_sessions,
        "aggressive": aggressive,
        "series": series,
    }


STATUS_LABEL = {
    "breakout": "Breakout", "buy_zone": "In buy range", "extended": "Extended", "retest": "Back below buy point",
    "setting_up": "Setting up", "in_base": "In base", "no_base": "No base",
}


def story_score(f: dict, cfg: dict) -> float:
    s = {"breakout": 100, "buy_zone": 80, "setting_up": 45, "extended": 15, "retest": 35, "in_base": 0, "no_base": 0}[f["status"]]
    # Volume is reported, not scored (publisher's call, Oct 24 2026): institutions no longer dominate the tape, so a
    # heavy-volume breakout is information, not confirmation. No bonus, no penalty.
    if f["status"] == "extended" and (f.get("days_since_breakout") if f.get("days_since_breakout") is not None else 99) <= 3:
        s += 60                     # a fresh breakout that gapped past the buy range is still the day's story
    if f["status"] == "retest" and (f.get("days_since_breakout") or 99) > 20:
        s -= 30
    if f["bounce50"]:
        s += 60 if (f.get("from_pivot") is None or f["from_pivot"] > -15) else 40
    if f.get("ipo_sessions") and (f["new_high"] or abs(f["chg_pct"] or 0) >= 4):
        s += 45
    if f["breakdown50"]:
        s += 55
    if f["new_high"]:
        s += 25 if f["status"] != "extended" else 10
    if abs(f["chg_pct"] or 0) >= 4:
        s += 25
    s += (f.get("rs_rank") or 50) / 5
    s += min(f.get("vol_ratio") or 0, 3) * 6
    return s


def angle(f: dict) -> str:
    """One line: why this stock is a story today (drives the headline)."""
    if f["status"] == "breakout":
        return "breakout past buy point"
    if f["status"] == "extended" and (f.get("days_since_breakout") if f.get("days_since_breakout") is not None else 99) <= 3:
        return "breakout past buy point, already extended"
    if f["bounce50"]:
        return "bounce off 50-day line"
    if f["breakdown50"]:
        return "breaks 50-day line"
    if f["status"] == "buy_zone":
        return "still in buy range after breakout"
    if f["status"] == "retest":
        return "slips back below buy point"
    if f["status"] == "setting_up":
        return "nearing buy point"
    if f.get("ipo_sessions") and f["new_high"]:
        return "new issue at a new high"
    if f["new_high"]:
        return "new 52-week high"
    if abs(f["chg_pct"] or 0) >= 4:
        return "big move on the day"
    return STATUS_LABEL[f["status"]].lower()


# ────────────────────────────────────────────────────────────────────────── enrichment (selected names only)

def _num(x):
    try:
        v = float(x)
        return None if math.isnan(v) else v
    except (TypeError, ValueError):
        return None


def enrich(f: dict) -> None:
    import yfinance as yf
    tk = yf.Ticker(f["sym"])
    fund = {}
    try:
        info = tk.info or {}
        fund.update(name=info.get("longName") or info.get("shortName"), industry=info.get("industry"),
                    sector=info.get("sector"), market_cap=_num(info.get("marketCap")), pe_trailing=_num(info.get("trailingPE")),
                    pe_forward=_num(info.get("forwardPE")), currency=info.get("financialCurrency"),
                    summary=(info.get("longBusinessSummary") or "")[:600] or None)
    except Exception as e:  # noqa: BLE001
        log(f"{f['sym']}: info failed ({e})")
    try:
        q = tk.quarterly_income_stmt
        rows = []
        if q is not None and not q.empty:
            cols = sorted(q.columns, reverse=True)[:6]
            for c in cols:
                eps = _num(q.at["Diluted EPS", c]) if "Diluted EPS" in q.index else None
                rev = _num(q.at["Total Revenue", c]) if "Total Revenue" in q.index else None
                rows.append({"quarter": c.date().isoformat(), "eps": None if eps is None else round(eps, 2),
                             "revenue": None if rev is None else int(rev)})
        for i, r in enumerate(rows):
            r.setdefault("eps_yoy", None)
            r.setdefault("rev_yoy", None)
            if i + 4 < len(rows):
                prev = rows[i + 4]
                r["eps_yoy"] = pct(r["eps"], prev["eps"]) if r["eps"] is not None and prev["eps"] and prev["eps"] > 0 else None
                r["rev_yoy"] = pct(r["revenue"], prev["revenue"]) if r["revenue"] and prev["revenue"] else None
        fund["quarters"] = rows[:4]
    except Exception as e:  # noqa: BLE001
        log(f"{f['sym']}: income stmt failed ({e})")
    try:
        cal = tk.calendar or {}
        ed = cal.get("Earnings Date") if isinstance(cal, dict) else None
        if ed:
            fund["next_earnings"] = str(ed[0]) if isinstance(ed, (list, tuple)) else str(ed)
    except Exception as e:  # noqa: BLE001
        log(f"{f['sym']}: calendar failed ({e})")
    f["fundamentals"] = fund
    f["name"] = fund.get("name") or f["sym"]

    news = []
    try:
        for n in (tk.news or [])[:8]:
            c = n.get("content") or n
            title = c.get("title")
            url = ((c.get("canonicalUrl") or {}).get("url")) or ((c.get("clickThroughUrl") or {}).get("url")) or n.get("link")
            when = c.get("pubDate") or n.get("providerPublishTime")
            prov = ((c.get("provider") or {}).get("displayName")) or n.get("publisher")
            if title and url:
                news.append({"title": title, "url": url, "source": prov, "published": str(when) if when else None})
    except Exception as e:  # noqa: BLE001
        log(f"{f['sym']}: news failed ({e})")
    if not news:
        try:
            import feedparser
            fp = feedparser.parse(f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={f['sym']}&region=US&lang=en-US")
            for e in fp.entries[:8]:
                news.append({"title": e.get("title"), "url": e.get("link"), "source": None, "published": e.get("published")})
        except Exception as e:  # noqa: BLE001
            log(f"{f['sym']}: rss failed ({e})")
    f["news"] = news[:5]


# ────────────────────────────────────────────────────────────────────────── scan

BOARD_KEYS = ("sym", "close", "chg_pct", "status", "from_pivot", "rs_rank", "vol_ratio", "from_hi52", "vs_sma50", "angle")
ET = ZoneInfo("America/New_York")
WRITER_PASSES = ("premarket", "midday", "close")
PASS_DEFAULTS = {"premarket": {"at": "07:15", "stories": 3}, "movers_open": {"at": "07:45"},
                 "midday": {"at": "11:05", "stories": 3}, "movers_close": {"at": "14:05"}, "close": {"at": "15:20", "stories": 4}}
PASS_LABEL = {"premarket": "Premarket", "midday": "Midday", "close": "Close"}


def desk_passes(cfg: dict) -> dict:
    """markets.yaml desk.passes over the defaults: {name: {at: "HH:MM" Mountain, stories: n}}."""
    got = (cfg.get("desk") or {}).get("passes") or {}
    return {k: v | (got.get(k) or {}) for k, v in PASS_DEFAULTS.items()}


def session_date(now_utc: datetime | None = None) -> str:
    """Today's date on the exchange's clock."""
    return (now_utc or datetime.now(timezone.utc)).astimezone(ET).date().isoformat()


def before_session(prices: dict, session: str) -> dict:
    """Drop any bar dated `session` or later: the premarket pass works from the prior close. At 9:15 a.m. Eastern Yahoo
    has no daily bar for today; a hand-run later in the day would otherwise read the session's partial bar."""
    from datetime import date as _date
    day = _date.fromisoformat(session)
    out = {}
    for sym, d in prices.items():
        d2 = d[d.index.date < day]
        if len(d2) >= 15:
            out[sym] = d2
    return out


def recent_stories(n: int, upto: str) -> dict:
    """{sym: status} for the newest `n` stories across the last two editions dated `upto` or earlier (newest first)."""
    rows = []
    for p in [p for p in sorted(DATA.glob("????-??-??.json")) if p.stem <= upto][-2:]:
        try:
            e = json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        for s in e.get("stories") or []:
            rows.append((s.get("printed_at") or e.get("generated") or "", s["sym"], s.get("status")))
    rows.sort(reverse=True)
    out = {}
    for _, sym, st in rows[:n]:
        out.setdefault(sym, st)
    return out


TRIED_FILE = DATA / "tried.json"


def mark_tried(pass_name: str, day: str) -> None:
    """A writer pass started (data/markets/tried.json). due_pass() reads it so a pass that RAN and failed is not sent
    again every hour, which would hold the 3:20 edition behind a broken midday pass."""
    try:
        t = json.loads(TRIED_FILE.read_text(encoding="utf-8")) if TRIED_FILE.is_file() else {}
    except Exception:  # noqa: BLE001
        t = {}
    t[f"{day}-{pass_name}"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    t = dict(sorted(t.items())[-20:])
    DATA.mkdir(parents=True, exist_ok=True)
    TRIED_FILE.write_text(json.dumps(t, indent=0), encoding="utf-8")


def scan(cfg: dict, pass_name: str = "close") -> dict:
    """The writer's payload for one pass: premarket (7:15 a.m. Mountain, from the prior close plus the premarket tape),
    midday (11:05, today's partial bar) or close (3:20, the session's bar plus after hours)."""
    if pass_name not in WRITER_PASSES:
        raise ValueError(f"unknown pass {pass_name}")
    now = datetime.now(timezone.utc)
    session = session_date(now)
    if pass_name != "close":
        mark_tried(pass_name, session)
    tickers = universe(cfg)
    bench_syms = list(cfg.get("benchmarks", {}).values())
    prices = download(tickers + bench_syms, cfg["desk"].get("history_period", "2y"))
    if pass_name == "premarket":
        prices = before_session(prices, session)
    bench = prices.get(cfg["benchmarks"].get("spx", "^GSPC"))
    frac, live = session_fraction(now)

    facts = []
    for sym in tickers:
        if sym in prices:
            try:
                f = analyze(sym, prices[sym], bench, cfg)
                if f:
                    facts.append(f)
            except Exception as e:  # noqa: BLE001
                log(f"{sym}: analysis failed ({e})")
    if pass_name == "midday" and live and frac > 0:
        # today's bar is partial: project its volume to the full session, the way Volume Surges does
        for f in facts:
            if f["date"] == session and f.get("vol_ratio") is not None:
                f["vol_ratio"] = round(f["vol_ratio"] / frac, 2)
                f["vol_projected"] = True
    scored = sorted([f for f in facts if f["rs_score"] is not None], key=lambda f: f["rs_score"])
    for i, f in enumerate(scored):
        f["rs_rank"] = max(1, min(99, round(100 * (i + 1) / (len(scored) + 1))))
    for f in facts:
        f["score"] = round(story_score(f, cfg), 1)
        f["angle"] = angle(f)

    from desk_extras import distribution_days, distribution_summary
    pulse = {}
    for key, bsym in cfg.get("benchmarks", {}).items():
        d = prices.get(bsym)
        if d is None:
            continue
        c = d["Close"].to_numpy(dtype=float)
        s50v, s200v = sma(c, 50)[-1], sma(c, 200)[-1]
        pulse[key] = {"symbol": bsym, "close": round(c[-1], 2), "chg_pct": pct(c[-1], c[-2]),
                      "vs_sma50": None if math.isnan(s50v) else pct(c[-1], s50v),
                      "vs_sma200": None if math.isnan(s200v) else pct(c[-1], s200v),
                      "from_hi52": pct(c[-1], float(d["High"].tail(252).max())),
                      "date": d.index[-1].date().isoformat()} | distribution_summary(distribution_days(d, cfg))
    big = big_picture(pulse)

    # ── standing lists: breakouts this week, near a buy point, IPO watch, sell signals, groups
    recent_bo = [f for f in facts if f.get("days_since_breakout") is not None and f["days_since_breakout"] <= 5]
    lists = {
        "breakouts_week": sorted(recent_bo, key=lambda f: -(f.get("rs_rank") or 0)),
        "in_buy_range": sorted([f for f in facts if f["status"] in ("breakout", "buy_zone")], key=lambda f: -(f.get("rs_rank") or 0)),
        "near_buy_point": sorted([f for f in facts if f["status"] == "setting_up"], key=lambda f: -(f.get("rs_rank") or 0)),
        "ipo_watch": sorted([f for f in facts if f.get("ipo_sessions")], key=lambda f: -(f.get("rs_rank") or 0)),
        "sell_signals": sorted([f for f in facts if f["breakdown50"] or (f["status"] == "retest" and (f["from_pivot"] or 0) <= -7)],
                               key=lambda f: (f.get("from_pivot") or 0)),
        "aggressive": sorted([f for f in facts if any(a.get("live") for a in (f.get("aggressive") or []))],
                             key=lambda f: -(f.get("rs_rank") or 0)),
        # the RS line at a new 52-week high today. Names still under their buy point come first —
        # relative strength leading price is the version of the signal worth the most.
        "blue_dots": sorted([f for f in facts if (f.get("series") or {}).get("blue_dot")],
                            key=lambda f: (0 if f["status"] in ("setting_up", "in_base") else 1, -(f.get("rs_rank") or 0))),
    }
    lists = {k: [{kk: f.get(kk) for kk in BOARD_KEYS + ("days_since_breakout", "ipo_sessions", "breakout_vol_ratio")}
                 | {"buy_point": (f.get("base") or {}).get("buy_point"), "buy_max": (f.get("base") or {}).get("buy_max"),
                    "base_kind": (f.get("base") or {}).get("kind"), "base_weeks": (f.get("base") or {}).get("weeks"),
                    "aggressive": [a for a in (f.get("aggressive") or []) if a.get("live")][:1]}
                 for f in v[:15]] for k, v in lists.items()}
    by_sym = {f["sym"]: f for f in facts}
    groups = []
    for gname, syms in cfg.get("universe", {}).items():
        members = [by_sym[str(s).upper()] for s in syms or [] if str(s).upper() in by_sym and by_sym[str(s).upper()].get("rs_rank")]
        if len(members) >= 3:
            members.sort(key=lambda f: -f["rs_rank"])
            groups.append({"group": gname.replace("_", " "), "avg_rs": round(sum(f["rs_rank"] for f in members) / len(members)),
                           "n": len(members), "leaders": [f["sym"] for f in members[:4]],
                           "above50": round(100 * sum(1 for f in members if (f.get("vs_sma50") or 0) > 0) / len(members))})
    groups.sort(key=lambda g: -g["avg_rs"])

    # next earnings and the desk score for every name (cached; the story choice below reads the earnings dates)
    try:
        from desk_extras import next_earnings_cached, desk_score, cached_growth
        for f in facts:
            f["next_earnings"] = next_earnings_cached(f["sym"])
            f["desk_score"] = desk_score(f, cached_growth(f["sym"]))
            time.sleep(0.05)
    except Exception as e:  # noqa: BLE001
        log(f"earnings dates skipped ({e})")

    # ── the extended tape for the desk list: premarket prints before the open, after-hours prints after the close
    ext_key = {"premarket": "premarket", "close": "after_hours"}.get(pass_name)
    tape = {}
    if facts and (pass_name == "premarket" or (pass_name == "close" and after_hours_window(now))):
        kind = "pre" if pass_name == "premarket" else "post"
        forced = os.environ.get("MARKETS_PRE" if kind == "pre" else "MARKETS_AH") == "1"
        tape = extended_tape(kind, {f["sym"]: prices[f["sym"]] for f in facts}, None if forced else session)
        for f in facts:
            q = tape.get(f["sym"])
            # the writer gets the print, its time and, when Yahoo reports it, the shares (never "0 shares" premarket)
            f[ext_key] = None if q is None else {k: v for k, v in q.items() if k in ("price", "chg", "chg_pct", "as_of")
                                                 or (k == "volume" and v)}

    # ── story choice: this pass's count of new names, skipping any name that has a story among the newest
    #    `stories_keep` unless something happened to it since (a breakout today, a 3%+ extended move, a new status)
    passes = desk_passes(cfg)
    n_new = int(passes[pass_name].get("stories", 3))
    keep = int(cfg["desk"].get("stories_keep", 10))
    recent = recent_stories(keep, session)

    def news_since(f):
        if f["sym"] not in recent:
            return True
        if f.get("days_since_breakout") == 0 and f["date"] == session:
            return True
        x = f.get("premarket") or f.get("after_hours")
        if x and abs(x.get("chg_pct") or 0) >= 3:
            return True
        return f["status"] != recent[f["sym"]]

    pool = [f for f in facts if news_since(f)]
    ranked = sorted(pool, key=lambda f: -f["score"])
    if pass_name == "premarket":
        mm = float((cfg.get("extended") or {}).get("min_move_pct", 2.0))

        def reacting(f):   # reported since the last close: the calendar still shows that date or today's
            ne = (f.get("next_earnings") or "")[:10]
            return bool(ne) and f["date"] <= ne <= session

        movers_ = sorted([f for f in pool if f.get("premarket") and abs(f["premarket"]["chg_pct"]) >= mm],
                         key=lambda f: (not reacting(f), -abs(f["premarket"]["chg_pct"])))
        seen = {f["sym"] for f in movers_}
        setups = [f for f in ranked if f["sym"] not in seen and f["status"] in ("breakout", "buy_zone", "setting_up")]
        seen |= {f["sym"] for f in setups}
        ranked = movers_ + setups + [f for f in ranked if f["sym"] not in seen]
    chosen = ranked[:n_new]
    log(f"{pass_name} stories ({len(pool)} of {len(facts)} names eligible; {len(recent)} covered recently): "
        + ", ".join(f"{f['sym']}({f['status']},{f['score']})" for f in chosen))
    for f in chosen:
        enrich(f)
        time.sleep(0.5)

    # ── Company of the Day: one leader profiled per edition, never the same name twice in a month. Close pass only.
    profile = None
    if pass_name == "close":
        recent_p = set()
        for p in sorted(DATA.glob("????-??-??.json"))[-20:]:
            try:
                recent_p.add((json.loads(p.read_text(encoding="utf-8")).get("profile") or {}).get("sym"))
            except Exception:  # noqa: BLE001
                pass
        chosen_syms = {f["sym"] for f in chosen}
        cands = [f for f in sorted(facts, key=lambda f: -f["score"])
                 if (f.get("rs_rank") or 0) >= 80 and f["status"] in ("breakout", "buy_zone", "setting_up", "extended", "in_base")
                 and f["sym"] not in recent_p and (f.get("vs_sma50") or -99) > -3]
        cands.sort(key=lambda f: f["sym"] in chosen_syms)     # a name that is NOT already a story today goes first
        for f in cands[:4]:
            if f["sym"] not in chosen_syms:
                enrich(f)
            q = ((f.get("fundamentals") or {}).get("quarters") or [{}])[0]
            if (q.get("eps_yoy") or 0) >= 20 or (q.get("rev_yoy") or 0) >= 20:
                profile = f
                break
        if profile is None and cands:
            profile = cands[0]
        if profile:
            log(f"profile: {profile['sym']} (RS {profile.get('rs_rank')})")

    # ── The Lesson: Fridays (and any day the desk is run with MARKETS_CORNER=1). Close pass only.
    corner_topic = None
    topics = cfg.get("corner_topics") or []
    if pass_name == "close" and topics and (datetime.now(ZoneInfo(cfg["desk"].get("timezone", "America/Denver"))).weekday() == 4
                                            or os.environ.get("MARKETS_CORNER") == "1"):
        n_prev = sum(1 for p in DATA.glob("????-??-??.json") if '"corner":{' in p.read_text(encoding="utf-8"))
        corner_topic = topics[n_prev % len(topics)]
        log(f"the lesson: {corner_topic}")

    prices_through = max(f["date"] for f in facts) if facts else session
    # The premarket pass belongs to the session about to open: its stories go on today's page.
    from datetime import date as _date
    date = session if pass_name == "premarket" and is_trading_day(_date.fromisoformat(session)) else prices_through

    # ── After Hours (close pass): the movers pass that runs first screens the broad universe and saves the table;
    #    without it (a local `run`), the desk list's own tape stands in.
    ah = {}
    if pass_name == "close":
        ah = load_extended("post", date) or {}
        if not (ah.get("up") or ah.get("down")) and tape:
            ah = extended_movers("post", prices, bench, cfg, lambda s: (by_sym.get(s) or {}).get("rs_rank"), tape=tape)
    # ── extras: earnings this week, the scorecard (every pick since the first edition), phone alerts
    earnings = []
    try:
        from desk_extras import earnings_week, build_scorecard, send_alerts, breakout_alerts, holdings_alerts, big_picture_alert
        earnings = earnings_week(facts)
        log(f"earnings this week: {', '.join(r['sym'] for r in earnings) or 'none'}")
        if pass_name == "close":
            build_scorecard(prices, bench, download)
        prev_state = None
        eds_prev = sorted(DATA.glob("????-??-??.json"))
        if eds_prev:
            try:
                prev_state = (json.loads(eds_prev[-1].read_text(encoding="utf-8")).get("big_picture") or {}).get("state")
            except Exception:  # noqa: BLE001
                prev_state = None
        site_url = "https://beehivewire.com/"
        send_alerts(breakout_alerts(facts, site_url) + holdings_alerts(facts, site_url)
                    + big_picture_alert(big["state"], big["note"], prev_state, date, site_url), site_url)
    except Exception as e:  # noqa: BLE001
        log(f"extras skipped ({e})")
    save_series(facts)
    board_n = int(cfg["desk"].get("board_rows", 40))
    board = sorted(facts, key=lambda f: -(f.get("rs_rank") or 0))[:board_n]
    stamp = now.isoformat(timespec="seconds")
    out = {
        "date": date, "prices_through": prices_through, "pass": pass_name, "as_of": stamp, "generated": stamp,
        "universe_size": len(facts), "pulse": pulse, "big_picture": big, "lists": lists, "groups": groups,
        "earnings_week": earnings,
        "stories": chosen,  # full facts incl. series, fundamentals, news
        "profile": profile,
        "corner_topic": corner_topic,
        "board": [{k: f.get(k) for k in BOARD_KEYS + ("rs_score",)} for f in board],
    }
    if pass_name == "midday":
        out.update(intraday=bool(live), session_fraction=round(frac, 2))
    if pass_name == "premarket":
        out["futures"] = index_futures()
        out["premarket_as_of"] = next((v["as_of"] for v in sorted(tape.values(), key=lambda v: v.get("at") or "", reverse=True)), None)
    if pass_name == "close":
        out["after_hours_as_of"] = ah.get("tape_as_of_human") or next((v["as_of"] for v in tape.values()), None)
        out["ah_movers"] = [r | {"after_hours": {"price": r["ext_price"], "chg": r["ext_chg"], "chg_pct": r["ext_chg_pct"],
                                                  "as_of": r["ext_as_of"], "volume": r["ext_vol"]}}
                            for r in (ah.get("up") or []) + (ah.get("down") or [])]
        out["ah_meta"] = {k: ah.get(k) for k in ("kind", "date", "as_of", "tape_as_of_human", "universe_size", "criteria", "ref_date")} if ah else None
    return out


def pulse_keys(pulse: dict | None) -> list[str]:
    return [k for k, v in (pulse or {}).items() if isinstance(v, dict)]


def big_picture(pulse: dict) -> dict:
    """A factual read of the indexes in three states, from the 50-day line and the WEIGHTED distribution count
    (Nov 5, 2026: same thresholds as the raw count had, 4 and 6)."""
    spx, ndx = pulse.get("spx") or {}, pulse.get("nasdaq") or {}

    def wtd(x):
        return x.get("dist_weighted") if x.get("dist_weighted") is not None else (x.get("dist_days") or 0)

    dd = max(spx.get("dist_days") or 0, ndx.get("dist_days") or 0)
    dw = max(wtd(spx), wtd(ndx))
    above = [(x.get("vs_sma50") or 0) > 0 for x in (spx, ndx) if x]
    if above and all(above) and dw <= 4:
        state = "Confirmed uptrend"
    elif above and any(above) and dw <= 6:
        state = "Uptrend under pressure"
    else:
        state = "Market in correction"
    return {"state": state, "dist_days": dd, "dist_weighted": dw,
            "note": f"S&P 500 {spx.get('dist_days', 0)} distribution days (weighted {wtd(spx):.1f}), "
                    f"Nasdaq {ndx.get('dist_days', 0)} ({wtd(ndx):.1f}), over the last 25 sessions."}


# ────────────────────────────────────────────────────────────────────────── prose fallback (no Claude)

def money(n):
    if n is None:
        return "n/a"
    for div, suf in ((1e12, "T"), (1e9, "B"), (1e6, "M")):
        if abs(n) >= div:
            return f"${n / div:.1f}{suf}"
    return f"${n:,.0f}"


def template_article(f: dict) -> dict:
    name, sym, b = f.get("name") or f["sym"], f["sym"], f.get("base")
    st = f["status"]
    head = {
        "breakout": f"{name} Breaks Out Past {b['buy_point']:.2f} Buy Point" if b else f"{name} Breaks Out",
        "buy_zone": f"{name} Still In Buy Range After Breakout",
        "extended": f"{name} Extended Past Buy Range At New High" if f["new_high"] else f"{name} Runs Past Its Buy Range",
        "retest": f"{name} Slips Back Below Buy Point",
        "setting_up": f"{name} Nears Buy Point In {b['weeks']}-Week {b['kind'].title()}" if b else f"{name} Nears Buy Point",
        "in_base": f"{name} Builds A Base", "no_base": f"{name} In Focus",
    }[st]
    if f["bounce50"]:
        head = f"{name} Bounces Off 50-Day Line"
    if f["breakdown50"]:
        head = f"{name} Breaks Below 50-Day Line"
    paras = []
    move = f"{name} ({sym}) closed at {f['close']:.2f}, {'up' if (f['chg'] or 0) >= 0 else 'down'} {abs(f['chg_pct'] or 0):.1f}% on the day"
    move += f", trading {f['vol_ratio']:.1f}x its 50-day average volume." if f.get("vol_ratio") else "."
    paras.append(move)
    if b:
        base_line = (f"The chart shows a {b['weeks']}-week {b['kind']} with a {b['depth']:.0f}% correction from the {b['rim_date']} high. "
                     f"The buy point is {b['buy_point']:.2f}, ten cents above that high, and the buy range runs to {b['buy_max']:.2f}.")
        if b.get("breakout_date"):
            base_line += (f" Shares cleared the buy point on {b['breakout_date']}, with {f['breakout_vol_ratio']:.1f}x average"
                          f" volume that session, and now sit {f['from_pivot']:+.1f}% from it.")
        else:
            base_line += f" Shares are {abs(f['from_pivot']):.1f}% below the buy point."
        paras.append(base_line)
    fu = f.get("fundamentals") or {}
    q = (fu.get("quarters") or [None])[0]
    if q and (q.get("eps_yoy") is not None or q.get("rev_yoy") is not None):
        paras.append(f"Latest quarter ({q['quarter']}): EPS {q['eps'] if q['eps'] is not None else 'n/a'}"
                     + (f", {q['eps_yoy']:+.0f}% year over year" if q.get("eps_yoy") is not None else "")
                     + f"; revenue {money(q['revenue'])}" + (f", {q['rev_yoy']:+.0f}%." if q.get("rev_yoy") is not None else "."))
    rs = f"Relative strength ranks {f.get('rs_rank')} of 99 within the desk's list"
    if f.get("rs_line") and f["rs_line"].get("new_high"):
        rs += ", and the RS line against the S&P 500 is at a new high"
    if f.get("vs_sma50") is not None:
        rs += f". The stock trades {f['vs_sma50']:+.1f}% from its 50-day line and {f['from_hi52']:+.1f}% from its 52-week high."
    else:
        rs += "."
    paras.append(rs)
    if f.get("ext_atr") is not None:
        e = f["ext_atr"]
        where = ("the session's low held above the 21-day line" if e > 0 else
                 "the session's high stayed under the 21-day line" if e < 0 else
                 "price straddled the 21-day line")
        reach = (" — a stretched reading" if e >= 3 else
                 " — deep under it" if e <= -3 else "")
        paras.append(f"Measured against its own daily range, {sym} sits {abs(e):.1f} ATRs "
                     f"from the 21-day exponential average: {where}{reach}.")
    watch = {"breakout": "Watch for the stock to hold above the buy point on any pullback; a close 7-8% below it is the standard sell rule.",
             "buy_zone": "The buy range closes 5% above the buy point; beyond it the entry is extended.",
             "extended": "Extended stocks are watched, not chased; the next entry is a new base or a pullback to the 21-day or 50-day line.",
             "retest": "A quick recovery above the buy point would keep the breakout alive; a close 7-8% below it would not.",
             "setting_up": "A decisive close through the buy point is what starts the clock; the desk notes the day's volume but does not treat it as confirmation.",
             "in_base": "The base needs to finish forming before any buy point counts.", "no_base": "No proper base yet."}[st]
    if f["bounce50"]:
        watch = "A bounce off the 50-day line is an early entry; the line itself is the stop."
    if f["breakdown50"]:
        watch = "A decisive close below the 50-day line is a sell signal for holders."
    return {"sym": sym, "headline": head, "deck": f"{STATUS_LABEL[st]} · RS {f.get('rs_rank')} · {f['close']:.2f}",
            "body": paras, "watch": watch, "by": "desk"}


# ────────────────────────────────────────────────────────────────────────── charts (inline SVG)

def nice_step(raw: float) -> float:
    if raw <= 0:
        return 1
    mag = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 2.5, 5, 10):
        if raw <= m * mag:
            return m * mag
    return 10 * mag


def chart_svg(f: dict, cfg: dict) -> str:
    s = f["series"]
    n = len(s["c"])
    if n < 10:
        return ""
    W, H = 760, 420
    PL, PR, PT = 8, 62, 10
    PH, VT, VH = 290, 318, 90          # price panel height, volume panel top/height

    def xs(i):
        return PL + (i + 0.5) * (W - PL - PR) / n

    lo, hi = min(s["l"]), max(s["h"])
    b = f.get("base")
    if b:
        hi = max(hi, b["buy_max"])
    pad = (hi - lo) * 0.06 or 1
    lo, hi = lo - pad, hi + pad

    def ys(p):
        return PT + (hi - p) / (hi - lo) * PH

    vmax = max(s["v"]) or 1

    def yv(v):
        return VT + VH - v / vmax * VH

    bw = max(1.0, (W - PL - PR) / n * 0.6)
    out = [f'<svg class="chart" data-sym="{f["sym"]}" data-n="{n}"{(' data-buymax="' + str(b["buy_max"]) + '"') if b else ''} viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="{f["sym"]} daily chart">']
    out.append(f'<rect x="0" y="0" width="{W}" height="{H}" fill="var(--chart-bg)"/>')
    if b:
        y1, y2 = ys(b["buy_max"]), ys(b["buy_point"])
        out.append(f'<rect x="{PL}" y="{y1:.1f}" width="{W - PL - PR}" height="{max(1, y2 - y1):.1f}" fill="var(--zone)"/>')
        out.append(f'<line x1="{PL}" x2="{W - PR}" y1="{y2:.1f}" y2="{y2:.1f}" stroke="var(--pivot)" stroke-dasharray="6 4" stroke-width="1.4"/>')
        out.append(f'<text x="{W - PR + 4}" y="{y2 + 4:.1f}" class="lbl pivot">buy {b["buy_point"]:.2f}</text>')
        if s.get("rim_i") is not None and 0 <= s["rim_i"] < n:
            out.append(f'<circle cx="{xs(s["rim_i"]):.1f}" cy="{ys(s["h"][s["rim_i"]]):.1f}" r="4" fill="none" stroke="var(--pivot)" stroke-width="1.5"/>')
        if s.get("low_i") is not None and 0 <= s["low_i"] < n:
            out.append(f'<circle cx="{xs(s["low_i"]):.1f}" cy="{ys(s["l"][s["low_i"]]):.1f}" r="4" fill="none" stroke="var(--pivot)" stroke-width="1.5"/>')
        if s.get("bo_i") is not None and 0 <= s["bo_i"] < n:
            out.append(f'<text x="{xs(s["bo_i"]):.1f}" y="{ys(s["h"][s["bo_i"]]) - 8:.1f}" class="lbl bo" text-anchor="middle">breakout</text>')
    step = nice_step((hi - lo) / 5)
    g = math.ceil(lo / step) * step
    while g < hi:
        y = ys(g)
        out.append(f'<line x1="{PL}" x2="{W - PR}" y1="{y:.1f}" y2="{y:.1f}" stroke="var(--grid)" stroke-width="0.6"/>')
        out.append(f'<text x="{W - PR + 4}" y="{y + 4:.1f}" class="lbl">{g:g}</text>')
        g += step
    last = None
    for i, d in enumerate(s["d"]):
        m = d[:7]
        if m != last:
            last = m
            if i > 0:
                out.append(f'<text x="{xs(i):.1f}" y="{H - 4}" class="lbl" text-anchor="middle">{datetime.fromisoformat(d).strftime("%b")}</text>')
    rs = s.get("rs") or []
    rv = [v for v in rs if v is not None]
    if len(rv) > 5:                                  # the RS line: stock / S&P 500, drawn as a shape in the lower third
        rlo, rhi = min(rv), max(rv)
        rng = (rhi - rlo) or 1e-9
        pts = [f"{xs(i):.1f},{PT + PH * (0.95 - 0.33 * (v - rlo) / rng):.1f}" for i, v in enumerate(rs) if v is not None]
        out.append(f'<polyline points="{" ".join(pts)}" fill="none" stroke="var(--rs)" stroke-width="1.3" opacity=".9"/>')
        li = max(i for i, v in enumerate(rs) if v is not None)
        ly = PT + PH * (0.95 - 0.33 * (rs[li] - rlo) / rng)
        out.append(f'<text x="{xs(li) + 5:.1f}" y="{ly + 4:.1f}" class="lbl rsl">RS{" " + str(f["rs_rank"]) if f.get("rs_rank") else ""}</text>')
        for di in (s.get("rsdot") or []):
            if 0 <= di < n and rs[di] is not None:
                out.append(f'<circle cx="{xs(di):.1f}" cy="{PT + PH * (0.95 - 0.33 * (rs[di] - rlo) / rng):.1f}" r="3.2" fill="var(--rs)"/>')
        if li in (s.get("rsdot") or []):
            out.append(f'<text x="{PL + 4}" y="{PT + 28}" class="lbl rsl">RS line at a new 52-week high</text>')
    for key, col in (("s200", "var(--ma200)"), ("s50", "var(--ma50)"), ("e21", "var(--ma21)")):
        pts = [f"{xs(i):.1f},{ys(v):.1f}" for i, v in enumerate(s[key]) if v is not None]
        if len(pts) > 1:
            out.append(f'<polyline points="{" ".join(pts)}" fill="none" stroke="{col}" stroke-width="1.3"/>')
    for i in range(n):
        up = s["c"][i] >= (s["c"][i - 1] if i else s["o"][i])
        col = "var(--up)" if up else "var(--dn)"
        x = xs(i)
        out.append(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{ys(s["h"][i]):.1f}" y2="{ys(s["l"][i]):.1f}" stroke="{col}" stroke-width="1.2"/>')
        out.append(f'<line x1="{x:.1f}" x2="{x + bw / 2 + 1:.1f}" y1="{ys(s["c"][i]):.1f}" y2="{ys(s["c"][i]):.1f}" stroke="{col}" stroke-width="1.6"/>')
        out.append(f'<rect x="{x - bw / 2:.1f}" y="{yv(s["v"][i]):.1f}" width="{bw:.1f}" height="{VT + VH - yv(s["v"][i]):.1f}" fill="{col}" opacity=".75"/>')
    av = s.get("avgv50")
    if av:
        out.append(f'<line x1="{PL}" x2="{W - PR}" y1="{yv(av):.1f}" y2="{yv(av):.1f}" stroke="var(--avgv)" stroke-width="1.1"/>')
    out.append(f'<text x="{W - PR + 4}" y="{VT + 12}" class="lbl">vol</text>')
    # the next report rides the legend line (top left), clear of the breakout marker over the newest bars
    earn = earnings_label(f.get("next_earnings") or (f.get("fundamentals") or {}).get("next_earnings"))
    etag = f'  <tspan class="earn{" soon" if earn[1] else ""}">· {earn[0]}</tspan>' if earn else ""
    out.append(f'<text x="{PL + 4}" y="{PT + 12}" class="lbl"><tspan fill="var(--ma21)">21-day</tspan>  <tspan fill="var(--ma50)">50-day</tspan>  <tspan fill="var(--ma200)">200-day</tspan>{etag}</text>')
    out.append("</svg>")
    return "".join(out)


# ────────────────────────────────────────────────────────────────────────── editions + render

def edition_path(date: str) -> Path:
    return DATA / f"{date}.json"


def publish(payload: dict, articles: dict | None, cfg: dict, chief: dict | None = None, append: bool = False) -> Path:
    """Save one writer pass. append=True (every scheduled pass since Nov 5, 2026) adds this pass's stories to the
    day's file: a name covered again replaces its earlier story, and a re-run of the same pass replaces that pass's
    stories. The market note and every table follow the newest pass; the Desk Chief, Company of the Day, The Lesson
    and the After Hours table come only from the close pass and are kept through any later premarket/midday re-run."""
    pass_name = payload.get("pass") or "close"
    printed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    by_sym = {}
    if articles:
        for a in articles.get("stories") or []:
            if isinstance(a, dict) and a.get("sym"):
                by_sym[str(a["sym"]).upper()] = a
    stories = []
    for f in payload["stories"]:
        a = by_sym.get(f["sym"])
        ok = bool(a and a.get("headline") and isinstance(a.get("body"), list) and len(a["body"]) >= 2)
        if ok:
            art = {"sym": f["sym"], "headline": str(a["headline"]).strip(), "deck": str(a.get("deck") or "").strip(),
                   "body": [p.strip() for p in a["body"] if isinstance(p, str) and p.strip()],
                   "watch": str(a.get("watch") or "").strip(), "by": "editor"}
        else:
            art = template_article(f)
        stories.append(f | {"article": art, "chart": chart_svg(f, cfg), "pass": pass_name, "printed_at": printed_at})
    prev = {}
    p = edition_path(payload["date"])
    if append and p.is_file():
        try:
            prev = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            log(f"{p.name} unreadable ({e}); this pass starts the day's file over")
            prev = {}
    new_syms = {s["sym"] for s in stories}
    kept = [s | {"pass": s.get("pass") or "close", "printed_at": s.get("printed_at") or prev.get("generated")}
            for s in prev.get("stories") or [] if s["sym"] not in new_syms and (s.get("pass") or "close") != pass_name]
    ed = {k: v for k, v in payload.items() if k not in ("stories", "profile", "corner_topic", "ah_movers", "after_hours_as_of", "ah_meta")}
    for k in ("chief", "profile", "corner", "corner_topic", "ah_movers", "after_hours_as_of", "ah_meta", "edition_generated"):
        if k in prev and pass_name != "close":
            ed[k] = prev[k]
    if pass_name == "close":
        for k in ("corner_topic", "ah_movers", "after_hours_as_of", "ah_meta"):
            if payload.get(k) is not None:
                ed[k] = payload[k]
        ed["edition_generated"] = printed_at
    ed["stories"] = sorted(stories + kept, key=lambda s: s.get("printed_at") or "", reverse=True)
    ed["generated"] = printed_at
    ed["passes_done"] = (prev.get("passes_done") or {}) | {pass_name: printed_at}
    ed["market_note"] = (articles or {}).get("market_note") or (prev.get("market_note") if pass_name != "close" else None)
    # Company of the Day profile: the writer's piece if it is there, else the desk's numbers. Close pass only.
    pf = payload.get("profile") if pass_name == "close" else None
    if pf:
        a = (articles or {}).get("profile") or {}
        ok = bool(a.get("headline") and isinstance(a.get("body"), list) and len(a["body"]) >= 2)
        art = ({"sym": pf["sym"], "headline": str(a["headline"]).strip(), "deck": str(a.get("deck") or "").strip(),
                "body": [p.strip() for p in a["body"] if isinstance(p, str) and p.strip()],
                "watch": str(a.get("watch") or "").strip(), "by": "editor"} if ok else template_article(pf))
        ed["profile"] = pf | {"article": art, "chart": chart_svg(pf, cfg)}
    # The Lesson: only when the writer delivered one.
    c = ((articles or {}).get("corner") or {}) if pass_name == "close" else {}
    if c.get("headline") and isinstance(c.get("body"), list) and len(c["body"]) >= 3:
        ed["corner"] = {"topic": payload.get("corner_topic"), "headline": str(c["headline"]).strip(),
                        "deck": str(c.get("deck") or "").strip(), "example_sym": str(c.get("example_sym") or "").upper() or None,
                        "body": [p.strip() for p in c["body"] if isinstance(p, str) and p.strip()],
                        "takeaways": [t.strip() for t in (c.get("takeaways") or []) if isinstance(t, str) and t.strip()]}
    # The desk chief: Opus's judgment pass over every setup, when it delivered one.
    if pass_name == "close" and chief and isinstance(chief, dict) and (chief.get("verdict") or chief.get("picks")):
        ed["chief"] = {"verdict": str(chief.get("verdict") or "").strip(), "market": str(chief.get("market") or "").strip(),
                       "picks": [{"sym": str(x.get("sym") or "").upper(), "call": str(x.get("call") or "").strip(),
                                  "why": str(x.get("why") or "").strip(), "entry": str(x.get("entry") or "").strip()}
                                 for x in (chief.get("picks") or []) if isinstance(x, dict) and x.get("sym")][:6],
                       "avoid": [{"sym": str(x.get("sym") or "").upper(), "why": str(x.get("why") or "").strip()}
                                 for x in (chief.get("avoid") or []) if isinstance(x, dict) and x.get("sym")][:6],
                       "watch": str(chief.get("watch") or "").strip()}
    DATA.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(ed, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    log(f"edition saved {p} ({pass_name} pass) — {len(stories)} new stories "
        f"({sum(1 for s in stories if s['article']['by'] == 'editor')} by editor), {len(ed['stories'])} on the day")
    heads = [s["article"]["headline"] for s in stories]
    if pass_name == "close" and ed.get("profile"):
        heads.append("Company of the Day: " + ed["profile"]["article"]["headline"])
    if pass_name == "close" and ed.get("corner"):
        heads.append("The Lesson: " + ed["corner"]["headline"])
    kind, href = {"premarket": ("premarket", f"{payload['date']}.html#pass-premarket"),
                  "midday": ("midday", f"{payload['date']}.html#pass-midday")}.get(pass_name, ("edition", f"{payload['date']}.html"))
    record_pass(kind, payload["date"], printed_at, href, [s["sym"] for s in stories], heads,
                extra=(ed.get("big_picture") or {}).get("state"))
    return p


def editions() -> list[dict]:
    out = []
    for p in sorted(DATA.glob("????-??-??.json")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception as e:  # noqa: BLE001
            log(f"skip {p.name}: {e}")
    return out


def month_day(d: str | None) -> str:
    """2026-11-19 -> Nov 19."""
    try:
        x = datetime.fromisoformat(str(d)[:10])
    except (TypeError, ValueError):
        return "–"
    return f"{x.strftime('%b')} {x.day}"


def shares(n) -> str:
    if n is None:
        return "–"
    n = float(n)
    return f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.0f}K" if n >= 1e3 else f"{n:.0f}"


def human_date(d: str) -> str:
    dt = datetime.fromisoformat(d)
    return f"{dt.strftime('%A, %B')} {dt.day}, {dt.year}"


def render_site(site_cfg: dict | None = None) -> int:
    """Write site/markets/index.html (latest) + one page per edition. Called by build.py on every render."""
    from jinja2 import Environment, FileSystemLoader, select_autoescape
    cfg = load_cfg()
    eds = editions()
    if not eds:
        return 0
    env = Environment(loader=FileSystemLoader(str(ROOT / "templates")), autoescape=select_autoescape(["html"]))
    env.filters["money"] = money
    env.filters["md"] = month_day
    env.filters["shares"] = shares
    env.filters["wt"] = lambda w: "–" if w is None else (f"{w:.2f}"[:-1] if f"{w:.2f}".endswith("0") else f"{w:.2f}")
    env.globals["earn_soon"] = lambda d: bool((earnings_label(d) or ("", False))[1])
    tpl = env.get_template("markets.html")
    SITE.mkdir(parents=True, exist_ok=True)
    if SERIES_DIR.is_dir():
        import shutil
        (SITE / "series").mkdir(exist_ok=True)
        for f in SERIES_DIR.glob("*.json"):
            shutil.copy2(f, SITE / "series" / f.name)
    for name in ("rs_dist.json", "screen.json"):
        if (DATA / name).is_file():
            import shutil
            shutil.copy2(DATA / name, SITE / name)
    # The relay's alert cron skips NYSE holidays and stops at 1 p.m. Eastern on early-close days from this file.
    from datetime import date as _date, timedelta as _td
    yr = datetime.now(timezone.utc).year
    days = [_date(y, 1, 1) + _td(days=k) for y in (yr, yr + 1) for k in range(366) if (_date(y, 1, 1) + _td(days=k)).year == y]
    (SITE / "holidays.json").write_text(json.dumps({
        "holidays": sorted(d.isoformat() for y in (yr, yr + 1) for d in nyse_holidays(y)),
        "early_closes": [d.isoformat() for d in days if nyse_early_close(d)]}), encoding="utf-8")
    tz = ZoneInfo(cfg["desk"].get("timezone", "America/Denver"))
    site_name = ((site_cfg or {}).get("site") or {}).get("name") or "BEEHIVE WIRE"
    index = [{"date": e["date"], "human": human_date(e["date"]), "count": len(e["stories"]),
              "syms": [s["sym"] for s in e["stories"]]} for e in reversed(eds)]
    mv = load_movers()
    if mv:
        asof = datetime.fromisoformat(mv["as_of"]).astimezone(tz)
        mv["as_of_human"] = asof.strftime("%A %I:%M %p %Z").replace(" 0", " ")
    webster = None
    try:
        from desk_extras import load_webster
        webster = load_webster()
        if webster:
            webster["as_of_human"] = datetime.fromisoformat(webster["as_of"]).astimezone(tz).strftime("%A %I:%M %p %Z").replace(" 0", " ")
    except Exception:  # noqa: BLE001
        webster = None
    scorecard = None
    try:
        from desk_extras import load_scorecard
        scorecard = load_scorecard()
        if scorecard and scorecard.get("first_date"):
            scorecard["first_human"] = human_date(scorecard["first_date"])
    except Exception:  # noqa: BLE001
        scorecard = None
    ls = load_lists()
    if ls and ls.get("date"):
        ls["human"] = human_date(ls["date"])
        q = datetime.fromisoformat(ls["quotes_as_of"]).astimezone(tz) if ls.get("quotes_as_of") else datetime.fromisoformat(ls["generated"]).astimezone(tz)
        ls["quotes_human"] = q.strftime("%A %I:%M %p %Z").replace(" 0", " ")
    # The page is dated by its freshest pass: the 7:15 a.m. premarket table or an intraday movers run makes it
    # today's page even while the stories below are still yesterday's.
    ext_latest = load_extended()
    keep = int(cfg["desk"].get("stories_keep", 10))
    for i, e in enumerate(eds):
        latest = i == len(eds) - 1
        gen = datetime.fromisoformat(e["generated"]).astimezone(tz)
        page_date, stamps, ext_src = e["date"], [gen], edition_extended(e)
        if latest:
            if mv and mv.get("date") and mv["date"] > page_date:
                page_date = mv["date"]
            if mv and mv.get("date") and mv["date"] >= e["date"]:
                stamps.append(datetime.fromisoformat(mv["as_of"]).astimezone(tz))
            if ext_latest and ext_latest.get("date") and ext_latest["date"] >= e["date"]:
                ext_src = ext_latest                   # the freshest extended read: premarket, then after hours
                page_date = max(page_date, ext_latest["date"])
                stamps.append(datetime.fromisoformat(ext_latest["as_of"]).astimezone(tz))
        ext = ext_view(ext_src, tz)
        stamp_time = max(stamps).strftime("%I:%M %p %Z").lstrip("0")
        # The stories: the index page shows the newest `stories_keep` across today's and yesterday's files, newest
        # first, one per name; a dated page shows every story of its day.
        pool = [(s, e) for s in e["stories"]] + ([(s, eds[i - 1]) for s in eds[i - 1]["stories"]] if latest and i else [])
        pool.sort(key=lambda x: x[0].get("printed_at") or x[1].get("generated") or "", reverse=True)
        stories, seen = [], set()
        for s, src in pool:
            if s["sym"] in seen:
                continue
            seen.add(s["sym"])
            at = datetime.fromisoformat(s.get("printed_at") or src["generated"]).astimezone(tz)
            when = at.strftime("%I:%M %p").lstrip("0") + " MT"
            if src["date"] != page_date:
                when = datetime.fromisoformat(src["date"]).strftime("%a") + " " + when
            stories.append(s | {"stamp": f"{PASS_LABEL.get(s.get('pass') or 'close', 'Close')} · {when}"})
        if latest:
            stories = stories[:keep]
        # Until today's close prints, the index page keeps the last Desk Chief, Company of the Day and Lesson.
        view, carried = e, {}
        if latest and i and "close" not in (e.get("passes_done") or {"close": True}):
            prev_e = eds[i - 1]
            view = dict(e)
            for k in ("chief", "profile", "corner"):
                if not e.get(k) and prev_e.get(k):
                    view[k] = prev_e[k]
                    carried[k] = datetime.fromisoformat(prev_e["date"]).strftime("%A")
        ctx = dict(site_name=site_name, desk=cfg["desk"], ed=view, human=human_date(page_date), stamp_time=stamp_time,
                   first_pass_at=desk_passes(cfg)["premarket"]["at"].lstrip("0"),
                   edition_human=human_date(e["date"]), edition_day=datetime.fromisoformat(e["date"]).strftime("%A"),
                   stale_edition=(page_date != e["date"]), stories=stories, ext=ext, carried=carried,
                   generated=gen.strftime("%I:%M %p %Z").lstrip("0"), movers=mv if latest else None,
                   lists=ls if latest else None, scorecard=scorecard if latest else None,
                   webster=webster if latest else None, pass_label=PASS_LABEL,
                   status_label=STATUS_LABEL, index=index, latest=latest, root="../")
        html = tpl.render(**ctx)
        (SITE / f"{e['date']}.html").write_text(html, encoding="utf-8")
        if i == len(eds) - 1:
            (SITE / "index.html").write_text(html, encoding="utf-8")
    # frozen Stocks-on-the-Move pages, one per pass
    mtpl = env.get_template("markets_movers.html")
    (SITE / "movers").mkdir(exist_ok=True)
    for f in sorted((DATA / "movers").glob("*.json")) if (DATA / "movers").is_dir() else []:
        try:
            snap = json.loads(f.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        snap["as_of_human"] = datetime.fromisoformat(snap["as_of"]).astimezone(tz).strftime("%A, %B %d, %I:%M %p %Z").replace(" 0", " ")
        (SITE / "movers" / f"{f.stem}.html").write_text(
            mtpl.render(site_name=site_name, desk=cfg["desk"], movers=snap, status_label=STATUS_LABEL,
                        pass_no=pass_no_for(f"movers/{f.stem}.html") or "—", root="../../"), encoding="utf-8")
    # the archive: every pass, numbered day.pass, searchable client-side
    passes = load_passes()
    if passes:
        days = []
        for p in reversed(passes):
            at = datetime.fromisoformat(p["at"]).astimezone(tz)
            row = dict(p, time=at.strftime("%I:%M %p %Z").lstrip("0"), kind_label=KIND_LABEL.get(p["kind"], p["kind"]),
                       href="../" + p["href"])
            row["text"] = " ".join([p["no"], p["date"], human_date(p["date"]), at.strftime("%b %d %B"), row["kind_label"], p.get("extra") or ""]
                                   + p["syms"] + p["heads"])
            if not days or days[-1]["date"] != p["date"]:
                days.append({"date": p["date"], "day": p["day"], "human": human_date(p["date"]), "passes": [], "text": ""})
            days[-1]["passes"].append(row)
        for d in days:
            d["text"] = " ".join(x["text"] for x in d["passes"])
        (SITE / "archive").mkdir(exist_ok=True)
        (SITE / "archive" / "index.html").write_text(
            env.get_template("markets_archive.html").render(site_name=site_name, desk=cfg["desk"], days=days, passes=passes,
                                                            first_day=human_date(passes[0]["date"]), root="../../"), encoding="utf-8")
    log(f"market desk: rendered {len(eds)} editions, {len(passes)} passes -> {SITE}")
    if (cfg["desk"].get("gate") or {}).get("enabled"):
        # Readers need a code: every page is published sealed. If sealing cannot run, the desk must not go
        # out open: wipe the rendered pages so the deploy carries no unsealed desk, then raise.
        try:
            from desk_gate import gate_site
            gate_site(cfg, SITE, site_name, log=log)
        except Exception as e:  # noqa: BLE001
            for pth in SITE.rglob("*.html"):
                pth.unlink()
            log(f"market desk gate FAILED ({e}); desk pages removed from the site rather than published open")
            raise
    return len(eds)


# ────────────────────────────────────────────────────────────────────────── premarket and after hours

def after_hours_window(now_utc: datetime) -> bool:
    """True from 4:05 p.m. to 8:00 p.m. Eastern on a weekday (Yahoo's post-market tape), or when MARKETS_AH=1."""
    if os.environ.get("MARKETS_AH") == "1":
        return True
    et = now_utc.astimezone(ET)
    mins = et.hour * 60 + et.minute
    return et.weekday() < 5 and 16 * 60 + 5 <= mins <= 20 * 60


def extended_tape(kind: str, daily: dict, session: str | None = None) -> dict:
    """The last extended-session print per symbol from Yahoo's 5-minute pre/post tape: kind "pre" = 4:00-9:30 a.m.
    Eastern against the prior close, "post" = 4:00-8:00 p.m. against the day's close. `daily` is {sym: daily bars};
    the reference close is read from it. Only bars dated `session` (an Eastern date) count; session=None takes the
    newest date with such bars (MARKETS_PRE / MARKETS_AH plumbing tests). period="2d" because at 9:15 a.m. Eastern
    Yahoo's "1d" can still be yesterday. One bulk request per 60 names.
    Yahoo's tape carries no share volume before the open (and none for some names after the close): `bars` counts
    the 5-minute bars of the session that traded, the activity measure where `volume` reads 0.
    Returns {sym: {price, chg, chg_pct, as_of, at, volume, bars, session, ref_close, ref_date}}."""
    import pandas as pd
    import yfinance as yf
    from datetime import date as _date
    syms = [s for s in daily if not s.startswith("^")]
    want = _date.fromisoformat(session) if session else None
    out = {}
    for i in range(0, len(syms), 60):
        part = syms[i:i + 60]
        df = None
        for attempt in range(3):
            try:
                df = yf.download(part, period="2d", interval="5m", prepost=True, group_by="ticker", auto_adjust=False,
                                 progress=False, threads=True, timeout=30)
                if df is not None and not df.empty:
                    break
            except Exception as e:  # noqa: BLE001
                log(f"{kind} tape {part[0]}..{part[-1]} failed ({e}); retry {attempt + 1}")
            time.sleep(4 * (attempt + 1))
        if df is None or df.empty:
            continue
        for sym in part:
            try:
                d = (df[sym] if isinstance(df.columns, pd.MultiIndex) else df).dropna(subset=["Close"])
            except KeyError:
                continue
            if d.empty:
                continue
            idx = d.index.tz_convert(ET)
            mins = idx.hour * 60 + idx.minute
            keep = (mins < 9 * 60 + 30) if kind == "pre" else (mins >= 16 * 60)
            ext, eidx = d[keep], idx[keep]
            if ext.empty:
                continue
            day = want or max(x.date() for x in eidx)
            on_day = [x.date() == day for x in eidx]
            sel, sidx = ext[on_day], eidx[on_day]
            if sel.empty:
                continue
            dd = daily[sym]
            ref = dd[dd.index.date < day] if kind == "pre" else dd[dd.index.date <= day]
            if ref.empty or (kind == "post" and ref.index[-1].date() != day):
                continue                       # after hours needs the day's own close
            c = float(ref["Close"].iloc[-1])
            px = float(sel["Close"].iloc[-1])
            out[sym] = {"price": round(px, 2), "chg": round(px - c, 2), "chg_pct": round((px / c - 1) * 100, 1),
                        "as_of": sidx[-1].strftime("%I:%M %p ET").lstrip("0"), "at": sidx[-1].isoformat(),
                        "volume": int(sel["Volume"].fillna(0).sum()), "bars": int(len(sel)), "session": day.isoformat(),
                        "ref_close": round(c, 2), "ref_date": ref.index[-1].date().isoformat()}
        time.sleep(1.0)
    log(f"{kind} tape: {len(out)}/{len(syms)} quotes")
    return out


EXT_DIR = DATA / "extended"
EXT_LATEST = DATA / "extended-latest.json"
EXT_TITLE = {"pre": "Premarket Movers", "post": "After Hours"}
EXT_COL = {"pre": "Premarket", "post": "After hrs"}


def extended_movers(kind: str, prices: dict, bench, cfg: dict, rs_rank_of, session: str | None = None, tape: dict | None = None) -> dict:
    """The Premarket Movers (kind "pre") or After Hours ("post") table: names moving `extended.min_move_pct`+ in the
    extended session on `min_volume`+ shares (where Yahoo reports none, as before every open: traded in `min_prints`+
    five-minute bars), price `min_price`+, in the Volume Surges row shape with the extended print added. The buy-point distance and the Chart column are measured at the extended price (status_at); the
    chart pop-up keeps the closing status. Every row's chart is saved (save_series) with its next earnings date."""
    x = cfg.get("extended") or {}
    min_move, min_price, min_vol = float(x.get("min_move_pct", 2.0)), float(x.get("min_price", 10)), float(x.get("min_volume", 20000))
    min_prints = int(x.get("min_prints", 6))
    n_up, n_down = int(x.get("rows_up", 20)), int(x.get("rows_down", 10))
    zone = cfg["desk"].get("buy_zone_pct", 5)
    now = datetime.now(timezone.utc)
    if tape is None:
        tape = extended_tape(kind, prices, session)
    q_ok = {s: q for s, q in tape.items()
            if q["ref_close"] >= min_price and abs(q["chg_pct"]) >= min_move
            and (q["volume"] >= min_vol if q["volume"] > 0 else q.get("bars", 0) >= min_prints)}
    ups = sorted([s for s, q in q_ok.items() if q["chg_pct"] > 0], key=lambda s: -q_ok[s]["chg_pct"])[:n_up]
    downs = sorted([s for s, q in q_ok.items() if q["chg_pct"] < 0], key=lambda s: q_ok[s]["chg_pct"])[:n_down]
    desk = set(universe(cfg))
    ids = identities(ups + downs)
    try:
        from desk_extras import next_earnings_cached
    except Exception:  # noqa: BLE001
        next_earnings_cached = None
    facts, out = [], {"up": [], "down": []}
    from datetime import date as _date
    for side, syms in (("up", ups), ("down", downs)):
        for sym in syms:
            q = q_ok[sym]
            d = prices[sym]
            hist = d[d.index.date <= _date.fromisoformat(q["ref_date"])]
            ne = None
            if next_earnings_cached:
                ne = next_earnings_cached(sym)
                time.sleep(0.05)
            row = {"sym": sym, "name": (ids.get(sym) or {}).get("name"), "industry": (ids.get(sym) or {}).get("industry"),
                   "close": q["ref_close"], "chg_pct": None, "ext_price": q["price"], "ext_chg": q["chg"], "ext_chg_pct": q["chg_pct"],
                   "ext_vol": q["volume"] or None, "ext_bars": q.get("bars"), "ext_as_of": q["as_of"], "rs_rank": rs_rank_of(sym),
                   "desk": sym in desk,
                   "status": None, "close_status": None, "angle": None, "buy_point": None, "buy_max": None, "base_kind": None,
                   "base_weeks": None, "from_pivot": None, "next_earnings": ne}
            f = None
            try:
                f = analyze(sym, hist, bench, cfg) if len(hist) >= 16 else None
            except Exception as e:  # noqa: BLE001
                log(f"{sym}: analysis failed ({e})")
            if f:
                f["rs_rank"], f["next_earnings"] = row["rs_rank"], ne
                f["name"], f["industry"] = row["name"], row["industry"]
                facts.append(f)
                b = f.get("base") or {}
                st, fp = f["status"], None
                if b.get("buy_point"):
                    st, fp = status_at(q["price"], b["buy_point"], zone, f.get("days_since_breakout"))
                # the Chart column is the status at the extended price ("in buy range" for a name trading through its
                # buy point before the open); the session angle ("big move on the day") belongs to the prior close
                row.update(chg_pct=f.get("chg_pct"), status=st, close_status=f["status"], angle=STATUS_LABEL[st].lower(),
                           buy_point=b.get("buy_point"), buy_max=b.get("buy_max"), base_kind=b.get("kind"),
                           base_weeks=b.get("weeks"), from_pivot=fp)
            out[side].append(row)
    save_series(facts)
    last = max(tape.values(), key=lambda q: q["at"]) if tape else None
    day = (last or {}).get("session") or session or session_date(now)
    res = {"kind": kind, "date": day, "as_of": now.isoformat(timespec="seconds"),
           "tape_as_of": last["at"] if last else None, "tape_as_of_human": last["as_of"] if last else None,
           "ref_date": (last or {}).get("ref_date"), "universe_size": len([s for s in prices if not s.startswith("^")]),
           "quoted": len(tape), "criteria": {"min_move_pct": min_move, "min_price": min_price, "min_volume": min_vol,
                                             "min_prints": min_prints},
           **out}
    log(f"{EXT_TITLE[kind]}: {len(tape)} names quoted, {len(q_ok)} qualify; {len(out['up'])} up, {len(out['down'])} down")
    return res


def save_extended(ext: dict, record: bool = False) -> Path:
    EXT_DIR.mkdir(parents=True, exist_ok=True)
    p = EXT_DIR / f"{ext['date']}-{ext['kind']}.json"
    txt = json.dumps(ext, ensure_ascii=False, separators=(",", ":"))
    p.write_text(txt, encoding="utf-8")
    EXT_LATEST.write_text(txt, encoding="utf-8")
    log(f"{EXT_TITLE[ext['kind']]} saved -> {p}")
    if record:
        rows = ext["up"] + ext["down"]
        heads = [f"{r['sym']} {r['ext_chg_pct']:+.1f}% {EXT_COL[ext['kind']].lower()}" + (f", {r['angle']}" if r.get("angle") else "") for r in rows[:12]]
        record_pass("premarket" if ext["kind"] == "pre" else "edition", ext["date"], ext["as_of"], f"{ext['date']}.html#{'premarket' if ext['kind'] == 'pre' else 'after-hours'}",
                    [r["sym"] for r in rows], heads, extra=f"{len(rows)} movers")
    return p


def load_extended(kind: str | None = None, date: str | None = None) -> dict | None:
    """extended-latest.json, or with kind and date, that day's saved table."""
    p = EXT_LATEST if kind is None else EXT_DIR / f"{date}-{kind}.json"
    if p.is_file():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return None
    return None


def edition_extended(e: dict) -> dict | None:
    """An edition's own After Hours rows in the extended-table shape. Editions before Nov 5, 2026 hold the old
    desk-list rows (name, close, after_hours{...}); they map onto the same columns, with the chart columns blank."""
    rows = e.get("ah_movers") or []
    if not rows:
        return None
    meta = e.get("ah_meta") or {}
    conv = []
    for r in rows:
        if "ext_price" in r:
            conv.append(r)
            continue
        a = r.get("after_hours") or {}
        conv.append({"sym": r["sym"], "name": r.get("name"), "close": r.get("close"), "chg_pct": None, "ext_price": a.get("price"),
                     "ext_chg": a.get("chg"), "ext_chg_pct": a.get("chg_pct"), "ext_vol": a.get("volume"), "ext_as_of": a.get("as_of"),
                     "rs_rank": r.get("rs_rank"), "desk": True, "status": r.get("status"), "angle": None, "buy_point": None,
                     "buy_max": None, "base_kind": None, "base_weeks": None, "from_pivot": None, "next_earnings": r.get("next_earnings")})
    return {"kind": "post", "date": e["date"], "as_of": meta.get("as_of") or e.get("edition_generated") or e["generated"],
            "tape_as_of_human": meta.get("tape_as_of_human") or e.get("after_hours_as_of"), "ref_date": meta.get("ref_date") or e["date"],
            "universe_size": meta.get("universe_size"), "criteria": meta.get("criteria"), "legacy": not meta,
            "up": [r for r in conv if (r.get("ext_chg_pct") or 0) > 0], "down": [r for r in conv if (r.get("ext_chg_pct") or 0) < 0]}


def ext_view(x: dict | None, tz) -> dict | None:
    """The Premarket / After Hours block as the template prints it: heading, column label and the legend line."""
    if not x or not (x.get("up") or x.get("down")):
        return None
    from datetime import date as _date
    kind = x.get("kind") or "post"
    c = x.get("criteria") or {}
    ref = x.get("ref_date") or x.get("date")
    if x.get("legacy"):
        legend = (f"Desk-list names moving 3% or more in post-market trading as of {x.get('tape_as_of_human') or 'the evening'}, "
                  "against the 4 p.m. close.")
    else:
        if kind == "pre":
            against = f"against {datetime.fromisoformat(ref).strftime('%A')}'s close"
        else:
            against = "against the " + ("1 p.m." if ref and nyse_early_close(_date.fromisoformat(ref)) else "4 p.m.") + " close"
        legend = (f"As of {x.get('tape_as_of_human') or '–'}, {against}. {x.get('universe_size') or '–'} names screened "
                  f"(S&P 500, Nasdaq-100, the desk list); {c.get('min_move_pct', 2):g}%+ moves on "
                  f"{(c.get('min_volume') or 20000) / 1000:g}K+ shares or {c.get('min_prints', 6)}+ five-minute bars with trades, "
                  f"price ${c.get('min_price', 10):g}+. "
                  f"Buy-point distance and the Chart column are at the {EXT_COL[kind].lower().replace('hrs', 'hours')} price. "
                  "Green names are on the desk list.")
    return {"kind": kind, "anchor": "premarket" if kind == "pre" else "after-hours", "title": EXT_TITLE[kind], "col": EXT_COL[kind],
            "col_words": "premarket" if kind == "pre" else "after hours",
            "legend": legend, "up": x.get("up") or [], "down": x.get("down") or [], "date": x.get("date")}


FUTURES = {"ES=F": "S&P 500 futures", "NQ=F": "Nasdaq 100 futures", "YM=F": "Dow futures"}


def index_futures() -> list[dict]:
    """The index futures for the premarket market note: the last print against the prior session's settle. Empty if
    Yahoo does not answer; the note then works from the prior close alone."""
    try:
        import pandas as pd
        import yfinance as yf
        now = datetime.now(timezone.utc)
        today = now.astimezone(ET).date()
        daily = yf.download(list(FUTURES), period="7d", interval="1d", group_by="ticker", auto_adjust=False, progress=False, timeout=30)
        intra = yf.download(list(FUTURES), period="2d", interval="5m", prepost=True, group_by="ticker", auto_adjust=False,
                            progress=False, timeout=30)
        out = []
        for sym, name in FUTURES.items():
            d = daily[sym].dropna(subset=["Close"]) if isinstance(daily.columns, pd.MultiIndex) else daily.dropna(subset=["Close"])
            m = intra[sym].dropna(subset=["Close"]) if isinstance(intra.columns, pd.MultiIndex) else intra.dropna(subset=["Close"])
            prior = d[d.index.date < today]
            if prior.empty or m.empty:
                continue
            settle, px = float(prior["Close"].iloc[-1]), float(m["Close"].iloc[-1])
            out.append({"sym": sym, "name": name, "price": round(px, 2), "settle": round(settle, 2), "chg_pct": pct(px, settle),
                        "as_of": m.index[-1].tz_convert(ET).strftime("%I:%M %p ET").lstrip("0")})
        log("futures: " + ", ".join(f"{f['sym']} {f['chg_pct']:+.1f}%" for f in out))
        return out
    except Exception as e:  # noqa: BLE001
        log(f"futures skipped ({e})")
        return []


def rs_ranker(prices: dict):
    """RS rank (1-99) of each name within the whole screened universe, the way the weekly lists rank it."""
    import bisect
    rs_all = {}
    for sym, d in prices.items():
        if len(d) >= 30 and not sym.startswith("^"):
            v = rs_score(d["Close"].to_numpy(dtype=float))
            if v is not None:
                rs_all[sym] = v
    ordered = sorted(rs_all.values())

    def rs_rank_of(sym):
        v = rs_all.get(sym)
        return None if v is None else max(1, min(99, round(100 * (bisect.bisect_left(ordered, v) + 1) / (len(ordered) + 1))))
    return rs_all, ordered, rs_rank_of


def premarket(cfg: dict) -> dict:
    """The 7:15 a.m. Mountain pass's table: Premarket Movers over the broad universe, from the prior close. Saved to
    data/markets/extended/<today>-pre.json and extended-latest.json; the page is dated today from it."""
    now = datetime.now(timezone.utc)
    forced = os.environ.get("MARKETS_PRE") == "1"
    session = session_date(now)
    syms = broad_universe(cfg)
    spx = cfg["benchmarks"].get("spx", "^GSPC")
    prices = before_session(download(syms + [spx], cfg["desk"].get("history_period", "2y")), session)
    bench = prices.get(spx)
    _, _, rs_rank_of = rs_ranker(prices)
    ext = extended_movers("pre", prices, bench, cfg, rs_rank_of, session=None if forced else session)
    ext["futures"] = index_futures()
    return ext


# ────────────────────────────────────────────────────────────────────────── stocks on the move (intraday)

BROAD_FILE = DATA / "broad_universe.json"
WIKI = {"sp500": "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies", "ndx": "https://en.wikipedia.org/wiki/Nasdaq-100"}
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128 Safari/537.36"}


def broad_universe(cfg: dict) -> list[str]:
    """S&P 500 + Nasdaq-100 (Wikipedia, cached a week in data/markets/broad_universe.json) + the desk's own list."""
    import io
    cached = None
    if BROAD_FILE.is_file():
        try:
            cached = json.loads(BROAD_FILE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            cached = None
    fresh = cached and (datetime.now(timezone.utc) - datetime.fromisoformat(cached["fetched"])).days < 7
    syms = set(cached["symbols"]) if cached else set()
    if not fresh:
        try:
            import pandas as pd
            import requests
            got = set()
            for key, url in WIKI.items():
                r = requests.get(url, headers=UA, timeout=20)
                r.raise_for_status()
                for t in pd.read_html(io.StringIO(r.text)):
                    col = next((c for c in t.columns if "symbol" in str(c).lower() or "ticker" in str(c).lower()), None)
                    if col is not None and len(t) > 50:
                        got |= {str(s).strip().replace(".", "-") for s in t[col].tolist() if re.fullmatch(r"[A-Za-z.\-]{1,6}", str(s).strip())}
                        break
            if len(got) > 400:
                syms = got
                BROAD_FILE.parent.mkdir(parents=True, exist_ok=True)
                BROAD_FILE.write_text(json.dumps({"fetched": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                                  "symbols": sorted(syms)}), encoding="utf-8")
                log(f"broad universe refreshed: {len(syms)} index names")
        except Exception as e:  # noqa: BLE001
            log(f"broad universe fetch failed ({e}); using {'cache' if syms else 'desk list only'}")
    syms |= set(universe(cfg))
    return sorted(syms)


# ────────────────────────────────────────────────────────────────────────── NYSE trading calendar

def _easter(year: int):
    """Western Easter Sunday (Anonymous Gregorian algorithm)."""
    from datetime import date
    a = year % 19; b, c = divmod(year, 100); d, e = divmod(b, 4); f = (b + 8) // 25
    g = (b - f + 1) // 3; h = (19 * a + b - d - g + 15) % 30; i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7; m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _nth_weekday(year: int, month: int, weekday: int, n: int):
    """The n-th given weekday of a month (n=-1 for the last)."""
    from datetime import date, timedelta
    if n > 0:
        d = date(year, month, 1)
        d += timedelta(days=(weekday - d.weekday()) % 7)
        return d + timedelta(weeks=n - 1)
    d = date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _observed(d):
    """NYSE observance: a Saturday holiday closes Friday, a Sunday holiday closes Monday —
    except New Year's Day on a Saturday, which the exchange does not observe on Dec 31."""
    from datetime import timedelta
    if d.weekday() == 5:
        return None if (d.month == 1 and d.day == 1) else d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def nyse_holidays(year: int) -> set:
    """Full-day NYSE closures for a year (NYSE's standing holiday rules; special closures such as a
    national day of mourning are not known in advance and are not here)."""
    from datetime import date, timedelta
    fixed = [date(year, 1, 1), date(year, 6, 19), date(year, 7, 4), date(year, 12, 25)]
    floating = [
        _nth_weekday(year, 1, 0, 3),      # Martin Luther King Jr. Day — third Monday in January
        _nth_weekday(year, 2, 0, 3),      # Washington's Birthday — third Monday in February
        _easter(year) - timedelta(days=2),  # Good Friday
        _nth_weekday(year, 5, 0, -1),     # Memorial Day — last Monday in May
        _nth_weekday(year, 9, 0, 1),      # Labor Day — first Monday in September
        _nth_weekday(year, 11, 3, 4),     # Thanksgiving — fourth Thursday in November
    ]
    days = {_observed(d) for d in fixed} | set(floating)
    days.discard(None)
    return days


def is_trading_day(d) -> bool:
    """Weekday and not an NYSE holiday."""
    return d.weekday() < 5 and d not in nyse_holidays(d.year)


def nyse_early_close(d) -> bool:
    """1:00 p.m. Eastern closes: the day after Thanksgiving, and July 3 / December 24 when they fall on a
    weekday that is itself a trading day."""
    from datetime import date, timedelta
    if d == _nth_weekday(d.year, 11, 3, 4) + timedelta(days=1):
        return True
    return d in (date(d.year, 7, 3), date(d.year, 12, 24)) and is_trading_day(d)


def sessions_until(date_str: str | None, today=None) -> int | None:
    """NYSE sessions from today to a date, counting that day (0 = today). None when the date is past or unreadable."""
    from datetime import date, timedelta
    try:
        d = date.fromisoformat(str(date_str)[:10])
    except (TypeError, ValueError):
        return None
    today = today or datetime.now(ZoneInfo("America/New_York")).date()
    if d < today:
        return None
    n, x = 0, today
    while x < d:
        x += timedelta(days=1)
        n += is_trading_day(x)
    return n


def earnings_label(date_str: str | None, today=None) -> tuple[str, bool] | None:
    """The chart's earnings tag, "Earnings Nov 19 · 9 sessions", and whether it is inside 5 sessions."""
    n = sessions_until(date_str, today)
    if n is None:
        return None
    d = datetime.fromisoformat(str(date_str)[:10])
    when = "today" if n == 0 else f"{n} session{'' if n == 1 else 's'}"
    return f"Earnings {d.strftime('%b')} {d.day} · {when}", n <= 5


def session_fraction(now_utc: datetime) -> tuple[float, bool]:
    """How much of the regular session (9:30-16:00 Eastern) has elapsed, and whether we are inside it."""
    et = now_utc.astimezone(ZoneInfo("America/New_York"))
    if not is_trading_day(et.date()):
        return 1.0, False
    mins = et.hour * 60 + et.minute
    start, end = 9 * 60 + 30, (13 * 60 if nyse_early_close(et.date()) else 16 * 60)
    if mins <= start + 5:
        return 0.0, False
    if mins >= end:
        return 1.0, False
    return max(0.08, (mins - start) / (end - start)), True


def movers(cfg: dict) -> dict:
    """'Volume Surges': the biggest volume surges vs the 50-day average, projected to the full
    session when the market is open. Qualifiers are in markets.yaml under `movers`."""
    import numpy as np
    m = cfg.get("movers") or {}
    min_price, min_avgvol = float(m.get("min_price", 10)), float(m.get("min_avg_volume", 400_000))
    min_ratio, min_up, min_rs = float(m.get("min_volume_ratio", 1.5)), float(m.get("min_up_pct", 1.0)), int(m.get("min_rs_rank", 70))
    n_up, n_down = int(m.get("rows_up", 20)), int(m.get("rows_down", 10))
    now = datetime.now(timezone.utc)
    frac, live = session_fraction(now)
    syms = broad_universe(cfg)
    prices = download(syms, cfg["desk"].get("history_period", "2y"))   # two years: the RS rank needs it, and so does the base finder
    bench = prices.get(cfg["benchmarks"].get("spx", "^GSPC"))
    today = max(d.index[-1].date() for d in prices.values()).isoformat() if prices else now.date().isoformat()
    # RS rank within the whole screened universe, the same way the weekly lists rank it
    rs_all, ordered, rs_rank_of = rs_ranker(prices)
    if len(ordered) >= 100:     # the quote box ranks any ticker against this same universe (templates/markets_analyze.js)
        DATA.mkdir(parents=True, exist_ok=True)
        (DATA / "rs_dist.json").write_text(json.dumps({"as_of": now.isoformat(timespec="seconds"), "n": len(ordered),
                                                       "scores": [round(v, 6) for v in ordered]}, separators=(",", ":")), encoding="utf-8")

    rows = []
    for sym, d in prices.items():
        if d.index[-1].date().isoformat() != today or len(d) < 55:
            continue
        C, V = d["Close"].to_numpy(dtype=float), d["Volume"].to_numpy(dtype=float)
        avgv = float(np.mean(V[-51:-1]))
        if C[-1] < min_price or avgv < min_avgvol or not avgv:
            continue
        proj = V[-1] / frac if (live and frac > 0) else V[-1]
        ratio = proj / avgv
        chg = (C[-1] / C[-2] - 1) * 100
        if ratio < min_ratio:
            continue
        rows.append({"sym": sym, "close": round(C[-1], 2), "chg_pct": round(chg, 1), "vol": int(V[-1]), "proj_vol": int(proj),
                     "avg_vol50": int(avgv), "vol_ratio": round(ratio, 2), "vol_pct": round((ratio - 1) * 100),
                     "ret_1m": pct(C[-1], C[-22]), "rs_rank": rs_rank_of(sym)})
    rows.sort(key=lambda r: -r["vol_ratio"])
    ups = [r for r in rows if r["chg_pct"] >= min_up][: n_up * 3]
    downs = [r for r in rows if r["chg_pct"] <= -min_up][: n_down * 2]
    mv_facts = []
    try:
        from desk_extras import next_earnings_cached
    except Exception:  # noqa: BLE001
        next_earnings_cached = None
    for r in ups + downs:   # chart status for the candidates only
        r["next_earnings"] = next_earnings_cached(r["sym"]) if next_earnings_cached else None
        f = None
        try:
            f = analyze(r["sym"], prices[r["sym"]], bench, cfg)
        except Exception as e:  # noqa: BLE001
            log(f"{r['sym']}: analysis failed ({e})")
        if f:
            f["rs_rank"], f["next_earnings"] = r.get("rs_rank"), r["next_earnings"]
            mv_facts.append(f)
            b = f.get("base") or {}
            r.update(status=f["status"], angle=angle(f), from_pivot=f.get("from_pivot"), buy_point=b.get("buy_point"),
                     buy_max=b.get("buy_max"), base_kind=b.get("kind"), base_weeks=b.get("weeks"), new_high=f["new_high"],
                     from_hi52=f.get("from_hi52"), vs_sma50=f.get("vs_sma50"), ipo_sessions=f.get("ipo_sessions"))
        r["qualifies"] = (r.get("rs_rank") or 0) >= min_rs
    save_series(mv_facts)
    try:
        from desk_extras import webster_screen
        webster_screen(prices, rs_rank_of, identities(list(prices)), cfg, now, live, frac)
    except Exception as e:  # noqa: BLE001
        log(f"webster screen skipped ({e})")
    # phone alerts: watch-list names clearing a buy point right now (volume shown, not required), and the holdings' sell rules
    try:
        from desk_extras import send_alerts, breakout_alerts, holdings_alerts
        desk_facts = []
        for sym in universe(cfg):
            if sym in prices and (live or True):
                try:
                    f = analyze(sym, prices[sym], bench, cfg)
                    if f:
                        f["rs_rank"] = rs_rank_of(sym)
                        desk_facts.append(f)
                except Exception:  # noqa: BLE001
                    pass
        site_url = "https://beehivewire.com/"
        note = f"as of {round(frac * 100)}% of the session, volume projected" if live else ""
        if live:
            for f in desk_facts:  # intraday: project today's partial volume to the full day so the alert quotes a day-rate
                if f.get("vol_ratio") is not None and frac > 0:
                    f["vol_ratio"] = round(f["vol_ratio"] / frac, 2)
        send_alerts(breakout_alerts(desk_facts, site_url, note) + holdings_alerts(desk_facts, site_url), site_url)
    except Exception as e:  # noqa: BLE001
        log(f"alerts skipped ({e})")
    try:
        save_screen(prices, bench, cfg, rs_all, rs_rank_of, today, now, live, m)
    except Exception as e:  # noqa: BLE001
        log(f"live-screen snapshot failed ({e}); the page keeps the pass's tables")
    try:
        refresh_lists(prices, bench, cfg, rs_rank_of, now, live)
    except Exception as e:  # noqa: BLE001
        log(f"list quote refresh failed ({e}); the lists keep their last quotes")
    if not live and after_hours_window(now):
        # After Hours over the whole screened universe (the edition prints this table at the top of the page)
        try:
            tape = extended_tape("post", prices, None if os.environ.get("MARKETS_AH") == "1" else session_date(now))
            save_extended(extended_movers("post", prices, bench, cfg, rs_rank_of, tape=tape))
        except Exception as e:  # noqa: BLE001
            log(f"after-hours table skipped ({e})")
            tape = {}
        for r in ups + downs:
            r["after_hours"] = tape.get(r["sym"])
    ups_q = [r for r in ups if r["qualifies"]][:n_up]
    ups_x = [r for r in ups if not r["qualifies"]][: max(0, n_up - len(ups_q))]
    out = {"as_of": now.isoformat(timespec="seconds"), "date": today, "live": live, "session_fraction": round(frac, 2),
           "universe_size": len(prices), "criteria": {"min_price": min_price, "min_avg_volume": min_avgvol, "min_volume_ratio": min_ratio,
                                                       "min_up_pct": min_up, "min_rs_rank": min_rs},
           "up": ups_q, "up_low_rs": ups_x, "down": downs[:n_down]}
    log(f"movers: {len(rows)} names over {min_ratio}x volume; {len(ups_q)} qualify up, {len(ups_x)} up on low RS, {len(out['down'])} down"
        + (f" (live, {round(frac * 100)}% of session, volume projected)" if live else " (closed, full-session volume)"))
    return out


NAMES_FILE = DATA / "names.json"


def identities(syms: list[str], max_fetch: int = 40) -> dict:
    """{sym: {"name", "industry"}} for the chart headers. Reads the fundamentals cache first (any age — a company's
    name and industry do not go stale), then a small names cache, and fetches at most `max_fetch` unknown names
    per run so a pass never stalls on Yahoo; the rest fill in over the next passes."""
    out, names = {}, {}
    if NAMES_FILE.is_file():
        try:
            names = json.loads(NAMES_FILE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            names = {}
    missing = []
    for sym in dict.fromkeys(syms):
        fc = FUND_CACHE / f"{sym}.json"
        if fc.is_file():
            try:
                c = json.loads(fc.read_text(encoding="utf-8"))
                if c.get("name"):
                    out[sym] = {"name": c.get("name"), "industry": c.get("industry"), "sector": c.get("sector")}
                    if c.get("sector") or sym in names and names[sym].get("sector"):
                        out[sym]["sector"] = c.get("sector") or names[sym].get("sector")
                        continue
                    missing.append(sym)
                    continue
            except Exception:  # noqa: BLE001
                pass
        if sym in names and names[sym].get("name") and names[sym].get("sector"):
            out[sym] = names[sym]
            continue
        if sym in names and names[sym].get("name"):
            out[sym] = names[sym]          # known name, sector still to fetch
        missing.append(sym)
    if missing:
        import yfinance as yf
        fetched = 0
        for sym in missing[:max_fetch]:
            try:
                info = yf.Ticker(sym).info or {}
                nm = info.get("longName") or info.get("shortName")
                if nm:
                    names[sym] = out[sym] = {"name": nm, "industry": info.get("industry"), "sector": info.get("sector")}
                    fetched += 1
            except Exception as e:  # noqa: BLE001
                log(f"{sym}: name lookup failed ({e})")
            time.sleep(0.15)
        if fetched:
            DATA.mkdir(parents=True, exist_ok=True)
            NAMES_FILE.write_text(json.dumps(names, ensure_ascii=False, separators=(",", ":"), sort_keys=True), encoding="utf-8")
        log(f"identities: {len(out)} known, {fetched} fetched, {max(0, len(missing) - max_fetch)} left for later passes")
    return out


SERIES_DIR = DATA / "series"


def _score_now(f: dict):
    """The desk score for a name whose scan did not compute it (movers / lists passes): cached fundamentals only."""
    try:
        from desk_extras import desk_score, cached_growth
        return desk_score(f, cached_growth(f["sym"]))
    except Exception:  # noqa: BLE001
        return None


def save_series(facts: list[dict]) -> int:
    """One compact JSON per symbol (data/markets/series/SYM.json) so any ticker on the page can pop its chart.
    Overwritten on every scan that touches the symbol; copied into site/markets/series/ at render."""
    SERIES_DIR.mkdir(parents=True, exist_ok=True)
    n = 0
    ids = identities([f["sym"] for f in facts if f.get("series") and not (f.get("name") and f.get("industry"))])
    try:
        from desk_extras import next_earnings_cached
    except Exception:  # noqa: BLE001
        next_earnings_cached = None
    for f in facts:
        s = f.get("series")
        if not s:
            continue
        if "next_earnings" not in f and next_earnings_cached:   # every chart carries its next report (Nov 5, 2026)
            f["next_earnings"] = next_earnings_cached(f["sym"])
        b = f.get("base") or {}
        ident = ids.get(f["sym"]) or {}
        doc = {"sym": f["sym"], "name": f.get("name") or ident.get("name"), "industry": f.get("industry") or ident.get("industry"),
               "date": f["date"], "close": f["close"], "chg": f.get("chg"), "chg_pct": f.get("chg_pct"),
               "status": f.get("status"), "angle": f.get("angle") or angle(f), "rs_rank": f.get("rs_rank"), "vol_ratio": f.get("vol_ratio"),
               "from_pivot": f.get("from_pivot"), "hi52": f.get("hi52"), "from_hi52": f.get("from_hi52"), "vs_sma50": f.get("vs_sma50"),
               "ipo_sessions": f.get("ipo_sessions"), "next_earnings": f.get("next_earnings"),
               "aggressive": f.get("aggressive") or [], "desk_score": f.get("desk_score") or _score_now(f),
               "blue_dot": bool((f.get("series") or {}).get("blue_dot")),
               "base": {k: b.get(k) for k in ("buy_point", "buy_max", "kind", "weeks", "depth", "rim_date", "low_date", "breakout_date")} if b else None,
               "series": s}
        (SERIES_DIR / f"{f['sym']}.json").write_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        n += 1
    log(f"series saved for {n} symbols -> {SERIES_DIR}")
    return n


# ────────────────────────────────────────────────────────────────────────── the pass index (archive + search)

PASSES_FILE = DATA / "passes.json"
KIND_LABEL = {"movers": "Volume Surges", "premarket": "Premarket", "midday": "Midday", "edition": "Edition"}


def load_passes() -> list[dict]:
    if PASSES_FILE.is_file():
        try:
            return json.loads(PASSES_FILE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return []
    return []


def record_pass(kind: str, date: str, when_utc: str, href: str, syms: list[str], heads: list[str], extra: str | None = None, live: bool = False) -> str:
    """Append (or refresh) one pass in data/markets/passes.json and return its number, day.pass.
    Day numbers count trading days since the desk's first pass; pass numbers count runs within the day, by time."""
    passes = load_passes()
    passes = [p for p in passes if p.get("href") != href]
    passes.append({"kind": kind, "date": date, "at": when_utc, "href": href, "syms": syms[:24], "heads": heads[:24], "extra": extra, "live": live})
    passes.sort(key=lambda p: (p["date"], p["at"]))
    days = sorted({p["date"] for p in passes})
    for p in passes:
        p["day"] = days.index(p["date"]) + 1
    n = {}
    for p in passes:
        n[p["date"]] = n.get(p["date"], 0) + 1
        p["pass"] = n[p["date"]]
        p["no"] = f"{p['day']}.{p['pass']}"
    DATA.mkdir(parents=True, exist_ok=True)
    PASSES_FILE.write_text(json.dumps(passes, ensure_ascii=False, indent=0), encoding="utf-8")
    mine = next(p for p in passes if p["href"] == href)
    log(f"pass {mine['no']} recorded ({kind}, {href})")
    return mine["no"]


def pass_no_for(href: str) -> str | None:
    for p in load_passes():
        if p.get("href") == href:
            return p.get("no")
    return None


def save_movers(mv: dict) -> Path:
    DATA.mkdir(parents=True, exist_ok=True)
    p = DATA / "movers-latest.json"
    p.write_text(json.dumps(mv, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    hist = DATA / "movers"
    hist.mkdir(exist_ok=True)
    stamp = datetime.fromisoformat(mv["as_of"]).astimezone(ZoneInfo("America/Denver")).strftime("%Y-%m-%d-%H%M")
    (hist / f"{stamp}.json").write_text(p.read_text(encoding="utf-8"), encoding="utf-8")
    log(f"movers saved -> {p} (+ movers/{stamp}.json)")
    local_date = datetime.fromisoformat(mv["as_of"]).astimezone(ZoneInfo("America/Denver")).date().isoformat()
    syms = [r["sym"] for r in mv["up"]] + [r["sym"] for r in mv["up_low_rs"]]
    heads = [f"{r['sym']} {r['chg_pct']:+.1f}% on {r['vol_ratio']:.1f}x volume, {r.get('angle') or ''}".strip(", ") for r in mv["up"][:10]]
    record_pass("movers", local_date, mv["as_of"], f"movers/{stamp}.html", syms, heads,
                extra=f"{len(mv['up'])} qualifiers", live=bool(mv.get("live")))
    return p


def load_movers() -> dict | None:
    p = DATA / "movers-latest.json"
    if p.is_file():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return None
    return None


# ────────────────────────────────────────────────────────────────────────── the Beehive 100 and the Big Cap 40 (weekly)

FUND_CACHE = DATA / "fund_cache"


def fundamentals_cached(sym: str, max_age_days: int = 7) -> dict:
    """Name, market cap, industry, latest-quarter EPS/revenue growth. Cached per symbol in data/markets/fund_cache/."""
    FUND_CACHE.mkdir(parents=True, exist_ok=True)
    p = FUND_CACHE / f"{sym}.json"
    if p.is_file():
        try:
            c = json.loads(p.read_text(encoding="utf-8"))
            if (datetime.now(timezone.utc) - datetime.fromisoformat(c["fetched"])).days < max_age_days:
                return c
        except Exception:  # noqa: BLE001
            pass
    import yfinance as yf
    tk = yf.Ticker(sym)
    out = {"sym": sym, "fetched": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    try:
        info = tk.info or {}
        out.update(name=info.get("longName") or info.get("shortName"), industry=info.get("industry"), sector=info.get("sector"),
                   market_cap=_num(info.get("marketCap")), pe_forward=_num(info.get("forwardPE")), currency=info.get("financialCurrency"))
    except Exception as e:  # noqa: BLE001
        log(f"{sym}: info failed ({e})")
        return out  # not cached: retry next run
    try:
        q = tk.quarterly_income_stmt
        rows = []
        if q is not None and not q.empty:
            for c in sorted(q.columns, reverse=True)[:8]:
                eps = _num(q.at["Diluted EPS", c]) if "Diluted EPS" in q.index else None
                rev = _num(q.at["Total Revenue", c]) if "Total Revenue" in q.index else None
                rows.append({"quarter": c.date().isoformat(), "eps": eps, "revenue": rev})
        yo = []
        for i, r in enumerate(rows[:3]):
            if i + 4 < len(rows):
                pv = rows[i + 4]
                yo.append({"quarter": r["quarter"],
                           "eps_yoy": pct(r["eps"], pv["eps"]) if r["eps"] is not None and pv["eps"] and pv["eps"] > 0 else None,
                           "rev_yoy": pct(r["revenue"], pv["revenue"]) if r["revenue"] and pv["revenue"] else None})
        out["growth"] = yo
    except Exception as e:  # noqa: BLE001
        log(f"{sym}: income stmt failed ({e})")
    p.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return out


def leader_lists(cfg: dict) -> dict:
    """Rank the broad universe by a stated composite and keep the top 50 (growth leaders) and the top 20 big caps."""
    import numpy as np
    m = cfg.get("leaders") or {}
    syms = broad_universe(cfg)
    prices = download(syms, cfg["desk"].get("history_period", "2y"))
    bench = prices.get(cfg["benchmarks"].get("spx", "^GSPC"))
    facts = []
    for sym, d in prices.items():
        try:
            f = analyze(sym, d, bench, cfg)
        except Exception:  # noqa: BLE001
            f = None
        if f and f.get("rs_score") is not None and f["close"] >= float(m.get("min_price", 15)) \
                and f["close"] * f["avg_vol50"] >= float(m.get("min_dollar_volume", 20_000_000)):
            facts.append(f)
    scored = sorted(facts, key=lambda f: f["rs_score"])
    for i, f in enumerate(scored):
        f["rs_rank"] = max(1, min(99, round(100 * (i + 1) / (len(scored) + 1))))
    # fundamentals for the technically strongest names only (cached a week; a full refresh is a few hundred calls)
    pool = sorted(facts, key=lambda f: -f["rs_rank"])[: int(m.get("fundamentals_pool", 220))]
    log(f"leaders: {len(facts)} liquid names; fetching fundamentals for the top {len(pool)} by RS")
    def enrich(f):
        fu = fundamentals_cached(f["sym"])
        g = (fu.get("growth") or [{}])[0]
        f["name"], f["market_cap"], f["industry"] = fu.get("name") or f["sym"], fu.get("market_cap"), fu.get("industry")
        f["eps_yoy"], f["rev_yoy"] = g.get("eps_yoy"), g.get("rev_yoy")
        g2 = (fu.get("growth") or [{}, {}])[1] if len(fu.get("growth") or []) > 1 else {}
        f["eps_accel"] = (f["eps_yoy"] is not None and g2.get("eps_yoy") is not None and f["eps_yoy"] > g2["eps_yoy"])
        time.sleep(0.15)

    for f in pool:
        enrich(f)

    def composite(f):
        eps = min(max(f.get("eps_yoy") or 0, 0), 200) / 2          # 0-100
        rev = min(max(f.get("rev_yoy") or 0, 0), 100)               # 0-100
        tech = 0
        tech += 25 if (f.get("vs_sma50") or -99) > 0 else 0
        tech += 25 if (f.get("vs_sma200") or -99) > 0 else 0
        tech += 25 if (f.get("from_hi52") or -99) >= -15 else 0
        tech += 25 if f["status"] in ("breakout", "buy_zone", "setting_up", "extended") else 0
        tech += 10 if f.get("rs_line", {}) and f["rs_line"].get("new_high") else 0
        tech += 10 if f.get("eps_accel") else 0
        return round(0.40 * f["rs_rank"] + 0.25 * eps + 0.10 * rev + 0.25 * min(tech, 100), 1)

    for f in pool:
        f["composite"] = composite(f)
    elig = [f for f in pool if (f.get("eps_yoy") or 0) >= float(m.get("min_eps_growth", 15)) or (f.get("rev_yoy") or 0) >= float(m.get("min_rev_growth", 20))]
    elig.sort(key=lambda f: -f["composite"])
    big_min = float(m.get("big_cap_min", 40e9))
    big_size = int(m.get("big_cap_size", 20))
    big = [f for f in pool if (f.get("market_cap") or 0) >= big_min]
    # The Big Cap list is always full (Todd, Oct 29, 2026): when the strongest names hold fewer than big_size
    # companies over the floor, keep walking down the universe by RS, fetching fundamentals as needed, until it is.
    extra = []
    if len(big) < big_size:
        in_pool = {f["sym"] for f in pool}
        for f in sorted(facts, key=lambda f: -f["rs_rank"]):
            if len(big) >= big_size:
                break
            if f["sym"] in in_pool:
                continue
            enrich(f)
            if (f.get("market_cap") or 0) >= big_min:
                f["composite"] = composite(f)
                big.append(f)
                extra.append(f)
        log(f"big cap: {len(big) - len(extra)} in the top-{len(pool)} pool; {len(extra)} more pulled from further down the universe")
    big.sort(key=lambda f: -f["composite"])
    save_series(pool + extra)
    keys = ("sym", "name", "industry", "close", "chg_pct", "rs_rank", "eps_yoy", "rev_yoy", "eps_accel", "market_cap", "composite",
            "status", "from_pivot", "from_hi52", "vs_sma50", "vol_ratio", "ipo_sessions")
    row = lambda f, i: {k: f.get(k) for k in keys} | {"rank": i + 1, "buy_point": (f.get("base") or {}).get("buy_point"),  # noqa: E731
                                                      "buy_max": (f.get("base") or {}).get("buy_max"), "base_kind": (f.get("base") or {}).get("kind")}
    prev = load_lists() or {}
    prev50 = {r["sym"]: r["rank"] for r in prev.get("beehive50", [])}
    prev20 = {r["sym"]: r["rank"] for r in prev.get("bigcap20", [])}
    # List sizes are config (markets.yaml leaders.list_size / big_cap_size); the data keys keep their names.
    size = int(m.get("list_size", 50))
    b50 = [row(f, i) | {"prev_rank": prev50.get(f["sym"])} for i, f in enumerate(elig[:size])]
    b20 = [row(f, i) | {"prev_rank": prev20.get(f["sym"])} for i, f in enumerate(big[:big_size])]
    out = {"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "date": max(f["date"] for f in facts) if facts else None,
           "universe_size": len(facts), "pool": len(pool), "beehive50": b50, "bigcap20": b20,
           "method": {"rs": 40, "eps": 25, "rev": 10, "tech": 25, "list_size": size, "big_cap_size": big_size, "min_eps_growth": m.get("min_eps_growth", 15), "min_rev_growth": m.get("min_rev_growth", 20),
                      "big_cap_min": big_min, "min_price": m.get("min_price", 15), "min_dollar_volume": m.get("min_dollar_volume", 20_000_000)}}
    log(f"beehive {size}: {', '.join(r['sym'] for r in b50[:10])} …  big cap {big_size}: {', '.join(r['sym'] for r in b20[:8])} …")
    return out


def outer_base(b: dict, dates: list, cfg: dict) -> list:
    """When analyze() measured from an inner base (the stock sat 10%+ under its old high), it switches back to the
    old base once price recovers to within 10% of that high. Save the old base so the live screen can do the same."""
    ins = (b or {}).get("inside")
    if not ins:
        return [None, None, None, None, None]
    pivot, low = float(ins["high"]), float(ins["low"])
    zone = cfg["desk"].get("buy_zone_pct", 5)
    bp = round(pivot + 0.10, 2)
    depth = round((pivot - low) / pivot * 100, 1) if pivot else 0
    kind = "flat base" if depth <= 15 else ("cup" if depth <= 35 else "deep cup")
    length = (len(dates) - 1 - dates.index(ins["high_date"])) if ins.get("high_date") in dates else None   # rim to today
    return [round(pivot, 4), bp, round(bp * (1 + zone / 100), 2), kind, max(1, round(length / 5)) if length else None]


def save_screen(prices: dict, bench, cfg: dict, rs_all: dict, rs_rank_of, today: str, now: datetime, live: bool, m: dict) -> None:
    """The page's live screen (templates/markets_live.js) re-runs Volume Surges and the buy-range watch lists
    every minute of the regular session from live quotes. Everything it needs that does NOT change during the day is
    saved here, per name, from the bars BEFORE today's: prior close, 50-day average volume, RS rank, the base and
    its buy point, and the pieces of the 50-day line, 52-week high and breakdown test. The browser adds today's
    price, high, low and volume and applies analyze()'s own rules (same thresholds as movers())."""
    import numpy as np
    desk = set(universe(cfg))
    desk_scores = sorted(v for k, v in rs_all.items() if k in desk)
    import bisect

    def desk_rank(sym):
        v = rs_all.get(sym)
        return None if v is None or not desk_scores else max(1, min(99, round(100 * (bisect.bisect_left(desk_scores, v) + 1) / (len(desk_scores) + 1))))

    try:
        from desk_extras import next_earnings_cached
    except Exception:  # noqa: BLE001
        next_earnings_cached = None
    rows, hist_dates = [], []
    for sym, d in prices.items():
        hist = d.iloc[:-1] if (live and d.index[-1].date().isoformat() == today) else d
        if len(hist) < 54:
            continue
        hist_dates.append(hist.index[-1].date().isoformat())
        # analyze() picks its base from a window that slides with each new session, so run it as of the session being
        # screened: the history plus a placeholder bar for today (a copy of the last close). The base it lands on is
        # the one analyze() will use today; the browser supplies today's real price.
        import pandas as pd
        syn = pd.concat([hist, hist.iloc[[-1]].set_axis([hist.index[-1] + pd.offsets.BDay(1)])])
        try:
            f = analyze(sym, syn, bench, cfg)
        except Exception:  # noqa: BLE001
            f = None
        if not f:
            continue
        C, H, V = (hist[k].to_numpy(dtype=float) for k in ("Close", "High", "Volume"))
        s50 = sma(C, 50)
        b = f.get("base") or {}
        n50 = min(50, len(C))
        rows.append([sym, round(float(C[-1]), 4), int(np.mean(V[-50:])), rs_rank_of(sym), desk_rank(sym) if sym in desk else None,
                     b.get("buy_point"), b.get("buy_max"), b.get("kind"), b.get("weeks"),
                     round(float(b["pivot"]), 4) if b.get("pivot") is not None else None,
                     (len(C) - b["breakout"]) if b.get("breakout") is not None else None,          # sessions since, counting today
                     round(float(np.sum(C[-49:])), 4),
                     int(np.sum(C[-n50:] > s50[-n50:])), n50,
                     round(float(np.max(H[-252:])), 4),
                     bool(np.nanmax(C[-5:]) > np.nanmax(s50[-5:])) if not np.all(np.isnan(s50[-5:])) else False,
                     len(C) if len(C) < 129 else None,
                     *outer_base(b, [x.date().isoformat() for x in syn.index], cfg),
                     next_earnings_cached(sym, fetch=False) if next_earnings_cached else None])
    yr = datetime.now(timezone.utc).year
    from datetime import date as _date, timedelta as _td
    early = [dd.isoformat() for y in (yr, yr + 1) for dd in (_nth_weekday(y, 11, 3, 4) + _td(days=1), _date(y, 7, 3), _date(y, 12, 24))
             if nyse_early_close(dd)]
    out = {"built": now.isoformat(timespec="seconds"), "hist_date": max(hist_dates) if hist_dates else None,
           "criteria": {"min_price": float(m.get("min_price", 10)), "min_avg_volume": float(m.get("min_avg_volume", 400_000)),
                        "min_volume_ratio": float(m.get("min_volume_ratio", 1.5)), "min_up_pct": float(m.get("min_up_pct", 1.0)),
                        "min_rs_rank": int(m.get("min_rs_rank", 70)), "rows_up": int(m.get("rows_up", 20)), "rows_down": int(m.get("rows_down", 10)),
                        "buy_zone_pct": float(cfg["desk"].get("buy_zone_pct", 5))},
           "early_closes": early, "universe_size": len(prices),
           "cols": ["sym", "prev", "avgv", "rs", "rsd", "bp", "bm", "kind", "wk", "pv", "dsb", "s49", "a50", "n50", "hip", "bdp", "ipo",
                    "opv", "obp", "obm", "okind", "owk", "ne"],
           "rows": rows}
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / "screen.json").write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    log(f"live-screen snapshot: {len(rows)} names (history through {out['hist_date']})")


def refresh_lists(prices: dict, bench, cfg: dict, rs_rank_of, now: datetime, live: bool) -> None:
    """Every pass re-quotes the Beehive 100 and Big Cap 40 from the prices the movers pass already downloaded:
    last price, day change, RS rank, chart status, buy point and distance from it. Ranks, scores and the
    fundamentals stay as the Friday rebuild set them. Costs nothing extra — no new downloads, no Claude."""
    ls = load_lists()
    if not ls:
        return
    facts, n = [], 0
    for key in ("beehive50", "bigcap20"):
        for r in ls.get(key) or []:
            d = prices.get(r["sym"])
            if d is None or len(d) < 55:
                continue
            try:
                f = analyze(r["sym"], d, bench, cfg)
            except Exception as e:  # noqa: BLE001
                log(f"{r['sym']}: list refresh failed ({e})")
                f = None
            if not f:
                continue
            b = f.get("base") or {}
            f["rs_rank"] = rs_rank_of(r["sym"]) or r.get("rs_rank")
            f["name"], f["industry"] = r.get("name") or f.get("name"), r.get("industry")
            r.update(close=f["close"], chg_pct=f.get("chg_pct"), rs_rank=f["rs_rank"], status=f["status"], from_pivot=f.get("from_pivot"),
                     from_hi52=f.get("from_hi52"), vs_sma50=f.get("vs_sma50"), vol_ratio=f.get("vol_ratio"), ipo_sessions=f.get("ipo_sessions"),
                     buy_point=b.get("buy_point"), buy_max=b.get("buy_max"), base_kind=b.get("kind"))
            facts.append(f)
            n += 1
    ls["quotes_as_of"] = now.isoformat(timespec="seconds")
    ls["quotes_live"] = live
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / "lists-latest.json").write_text(json.dumps(ls, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    save_series(facts)
    log(f"lists re-quoted: {n} names ({'live' if live else 'at the close'})")


def save_lists(ls: dict) -> Path:
    DATA.mkdir(parents=True, exist_ok=True)
    p = DATA / "lists-latest.json"
    p.write_text(json.dumps(ls, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    hist = DATA / "lists"
    hist.mkdir(exist_ok=True)
    (hist / f"{ls['date']}.json").write_text(p.read_text(encoding="utf-8"), encoding="utf-8")
    log(f"lists saved -> {p}")
    return p


def load_lists() -> dict | None:
    p = DATA / "lists-latest.json"
    if p.is_file():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return None
    return None


# ────────────────────────────────────────────────────────────────────────── catch-up: which pass is overdue?

def slots(cfg: dict | None = None) -> list[tuple[str, int]]:
    """The day's passes in Mountain time, from markets.yaml desk.passes: (mode, minutes after midnight). Mode is what
    markets.yml runs: premarket (7:15), movers (7:45), midday (11:05), movers (2:05, the closing numbers) and
    edition (3:20). On an early-close day the 1 p.m. Eastern bell is past by 2:05 either way."""
    ps = desk_passes(cfg if cfg is not None else load_cfg())
    mode = {"premarket": "premarket", "movers_open": "movers", "midday": "midday", "movers_close": "movers", "close": "edition"}
    out = []
    for key, m in mode.items():
        hh, mm = str(ps[key]["at"]).split(":")
        out.append((m, int(hh) * 60 + int(mm)))
    return sorted(out, key=lambda x: x[1])


def due_pass(now_utc: datetime | None = None, cfg: dict | None = None) -> str | None:
    """GitHub drops and delays cron runs, so the hourly pinger-driven build asks: is a Market Desk pass overdue?
    Returns premarket / movers / midday / edition for a slot of today that has come round with nothing printed for
    it, else None. Never on a weekend or an NYSE holiday. Each pass is detected by what it prints:
      premarket - data/markets/extended/<today>-pre.json; it lapses once the next slot (the 7:45 Volume Surges)
                  comes round, since after the open it is no longer a premarket read
      movers    - movers/<today>-HHMM.json at or after the slot less 10 minutes (any later pass prints one too)
      midday    - today's edition has passes_done.midday, or a midday pass already started today (tried.json), so
                  a midday that ran and failed is not sent again every hour ahead of the close
      edition   - today's edition has passes_done.close (files from before Nov 5, 2026: `generated`)
    Midday covers any movers slot before it (it runs Volume Surges first); the edition covers every movers slot but
    never an unprinted midday: midday runs first and the edition at the next shot."""
    now = (now_utc or datetime.now(timezone.utc)).astimezone(ZoneInfo("America/Denver"))
    if not is_trading_day(now.date()):          # weekends and NYSE holidays: no passes
        return None
    today = now.date().isoformat()
    mins = now.hour * 60 + now.minute
    mover_times = []
    for f in (DATA / "movers").glob(f"{today}-????.json") if (DATA / "movers").is_dir() else []:
        hhmm = f.stem[-4:]
        mover_times.append(int(hhmm[:2]) * 60 + int(hhmm[2:]))

    def mt(iso):
        try:
            t = datetime.fromisoformat(iso).astimezone(ZoneInfo("America/Denver"))
            return t.hour * 60 + t.minute if t.date().isoformat() == today else None
        except Exception:  # noqa: BLE001
            return None

    done = {}
    ep = edition_path(today)
    if ep.is_file():
        try:
            e = json.loads(ep.read_text(encoding="utf-8"))
            pd_ = e.get("passes_done")
            done = {k: mt(v) for k, v in pd_.items()} if isinstance(pd_, dict) else {"close": mt(e.get("generated") or "")}
        except Exception:  # noqa: BLE001
            done = {}
    try:
        tried = json.loads(TRIED_FILE.read_text(encoding="utf-8")) if TRIED_FILE.is_file() else {}
    except Exception:  # noqa: BLE001
        tried = {}
    pre_done = (EXT_DIR / f"{today}-pre.json").is_file()
    sl = slots(cfg)
    overdue = []
    for n, (kind, slot) in enumerate(sl):
        if mins < slot:
            break
        later = [s for _, s in sl[n + 1:]]
        if kind == "premarket":
            if pre_done or (later and mins >= later[0]):
                continue
        elif kind == "movers":
            if any(t >= slot - 10 for t in mover_times):
                continue
            if any(t is not None and t >= slot for k, t in done.items() if k in ("midday", "close")):
                continue
        elif kind == "midday":
            if done.get("midday") is not None or f"{today}-midday" in tried or done.get("close") is not None:
                continue
        elif kind == "edition":
            if done.get("close") is not None and done["close"] >= slot - 10:
                continue
        overdue.append(kind)
    if not overdue:
        return None
    for kind in ("premarket", "midday", "edition"):
        if kind in overdue:
            return kind
    return "movers"


# ────────────────────────────────────────────────────────────────────────── main

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan")
    s.add_argument("--out", default="data/build/markets_payload.json")
    s.add_argument("--pass", dest="pass_name", choices=WRITER_PASSES, default="close")
    p = sub.add_parser("publish")
    p.add_argument("--payload", required=True)
    p.add_argument("--articles")
    p.add_argument("--chief")
    p.add_argument("--append", action="store_true", help="add this pass's stories to the day's file (every scheduled pass)")
    sub.add_parser("render")
    sub.add_parser("scorecard")
    r = sub.add_parser("run")
    r.add_argument("--pass", dest="pass_name", choices=WRITER_PASSES, default="close")
    r.add_argument("--append", action="store_true")
    sub.add_parser("movers")
    sub.add_parser("premarket")
    sub.add_parser("lists")
    sub.add_parser("due")
    a = ap.parse_args()
    cfg = load_cfg()

    if a.cmd == "due":
        kind = due_pass()
        print(kind or "none")
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as fh:
                fh.write("desk=" + (kind or "none") + chr(10))
        return 0

    if a.cmd == "movers":
        save_movers(movers(cfg))
        render_site()
        return 0
    if a.cmd == "premarket":
        save_extended(premarket(cfg), record=True)
        render_site()
        return 0
    if a.cmd == "lists":
        save_lists(leader_lists(cfg))
        render_site()
        return 0

    if a.cmd in ("scan", "run"):
        payload = scan(cfg, a.pass_name)
        out = ROOT / (a.out if a.cmd == "scan" else "data/build/markets_payload.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        log(f"payload -> {out} ({payload['pass']} pass, {len(payload['stories'])} stories, {payload['universe_size']} names)")
        slim = {k: v for k, v in payload.items() if k not in ("board", "lists", "ah_meta")}
        for k in pulse_keys(slim.get("pulse")):
            slim["pulse"][k] = {kk: vv for kk, vv in slim["pulse"][k].items() if kk != "dist_dates"}
        slim["stories"] = [{k: v for k, v in f.items() if k not in ("series", "score")} for f in payload["stories"]]
        if payload.get("profile"):
            slim["profile"] = {k: v for k, v in payload["profile"].items() if k not in ("series", "score")}
        slim["lists"] = {k: [{kk: r.get(kk) for kk in ("sym", "status", "close", "from_pivot", "rs_rank", "buy_point", "buy_max", "base_kind", "base_weeks", "ipo_sessions")}
                             for r in v[:8]] for k, v in payload.get("lists", {}).items()}
        wp = out.with_name("markets_writer.json")
        wp.write_text(json.dumps(slim, ensure_ascii=False, indent=1), encoding="utf-8")
        log(f"writer payload -> {wp} ({wp.stat().st_size // 1024} KB)")
        if a.cmd == "scan":
            return 0
        publish(payload, None, cfg, append=a.append)
        render_site()
        return 0
    if a.cmd == "publish":
        payload = json.loads(Path(a.payload).read_text(encoding="utf-8"))
        arts = None
        if a.articles and Path(a.articles).is_file() and Path(a.articles).stat().st_size > 0:
            try:
                raw = Path(a.articles).read_text(encoding="utf-8").strip()
                raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw)
                arts = json.loads(raw)
            except Exception as e:  # noqa: BLE001
                log(f"articles unreadable ({e}) — using desk prose")
        chief = None
        if a.chief and Path(a.chief).is_file() and Path(a.chief).stat().st_size > 0:
            try:
                raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", Path(a.chief).read_text(encoding="utf-8").strip())
                chief = json.loads(raw)
            except Exception as e:  # noqa: BLE001
                log(f"desk chief unreadable ({e})")
        publish(payload, arts, cfg, chief=chief, append=a.append)
        render_site()
        return 0
    if a.cmd == "scorecard":
        from desk_extras import build_scorecard, collect_calls
        syms = sorted({c["sym"] for c in collect_calls()})
        prices = download(syms + [cfg["benchmarks"].get("spx", "^GSPC")], "2y")
        build_scorecard(prices, prices.get(cfg["benchmarks"].get("spx", "^GSPC")), download)
        render_site()
        return 0
    if a.cmd == "render":
        render_site()
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
