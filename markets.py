#!/usr/bin/env python3
"""
Beehive Wire — Market Desk.

IBD-style stock articles, one edition a day after the close: breakouts, buy ranges, bases, leaders.
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
    avgv50 = float(np.mean(V[max(0, t - 50):t])) if t >= 10 else float(V[t])
    vol_ratio = round(V[t] / avgv50, 2) if avgv50 else None
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

    # Regular names: a base is 20+ sessions. New issues: IBD's IPO base can be as short as two weeks.
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
        from_pivot = pct(C[t], buy_point)
        if base["breakout"] is not None:
            bi = base["breakout"]
            days_since = t - bi
            pv = float(np.mean(V[max(0, bi - 50):bi])) or 1.0
            bo_vol = round(V[bi] / pv, 2)
            if from_pivot < 0:
                status = "retest"
            elif from_pivot <= buy_zone:
                status = "breakout" if days_since <= 5 else "buy_zone"
            else:
                status = "extended"
        else:
            status = "setting_up" if from_pivot >= -5 else "in_base"
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
        "series": series,
    }


STATUS_LABEL = {
    "breakout": "Breakout", "buy_zone": "In buy range", "extended": "Extended", "retest": "Back below buy point",
    "setting_up": "Setting up", "in_base": "In base", "no_base": "No base",
}


def story_score(f: dict, cfg: dict) -> float:
    s = {"breakout": 100, "buy_zone": 80, "setting_up": 45, "extended": 15, "retest": 35, "in_base": 0, "no_base": 0}[f["status"]]
    if f["status"] == "breakout" and (f.get("breakout_vol_ratio") or 0) >= cfg["desk"].get("breakout_volume_ratio", 1.2):
        s += 15
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


def scan(cfg: dict) -> dict:
    tickers = universe(cfg)
    bench_syms = list(cfg.get("benchmarks", {}).values())
    prices = download(tickers + bench_syms, cfg["desk"].get("history_period", "2y"))
    bench = prices.get(cfg["benchmarks"].get("spx", "^GSPC"))

    facts = []
    for sym in tickers:
        if sym in prices:
            try:
                f = analyze(sym, prices[sym], bench, cfg)
                if f:
                    facts.append(f)
            except Exception as e:  # noqa: BLE001
                log(f"{sym}: analysis failed ({e})")
    scored = sorted([f for f in facts if f["rs_score"] is not None], key=lambda f: f["rs_score"])
    for i, f in enumerate(scored):
        f["rs_rank"] = max(1, min(99, round(100 * (i + 1) / (len(scored) + 1))))
    for f in facts:
        f["score"] = round(story_score(f, cfg), 1)
        f["angle"] = angle(f)

    pulse = {}
    for key, bsym in cfg.get("benchmarks", {}).items():
        d = prices.get(bsym)
        if d is None:
            continue
        c = d["Close"].to_numpy(dtype=float)
        v = d["Volume"].to_numpy(dtype=float)
        s50v, s200v = sma(c, 50)[-1], sma(c, 200)[-1]
        # IBD distribution days: a drop of 0.2%+ on volume above the prior session, counted over the last 25 sessions
        dist = [d.index[i].date().isoformat() for i in range(max(1, len(c) - 25), len(c))
                if c[i] / c[i - 1] - 1 <= -0.002 and v[i] > v[i - 1] > 0]
        pulse[key] = {"symbol": bsym, "close": round(c[-1], 2), "chg_pct": pct(c[-1], c[-2]),
                      "vs_sma50": None if math.isnan(s50v) else pct(c[-1], s50v),
                      "vs_sma200": None if math.isnan(s200v) else pct(c[-1], s200v),
                      "from_hi52": pct(c[-1], float(d["High"].tail(252).max())),
                      "dist_days": len(dist), "dist_dates": dist}
    big = big_picture(pulse)

    # ── standing lists (IBD-style): breakouts this week, near a buy point, IPO watch, sell signals, groups
    recent_bo = [f for f in facts if f.get("days_since_breakout") is not None and f["days_since_breakout"] <= 5]
    lists = {
        "breakouts_week": sorted(recent_bo, key=lambda f: -(f.get("rs_rank") or 0)),
        "in_buy_range": sorted([f for f in facts if f["status"] in ("breakout", "buy_zone")], key=lambda f: -(f.get("rs_rank") or 0)),
        "near_buy_point": sorted([f for f in facts if f["status"] == "setting_up"], key=lambda f: -(f.get("rs_rank") or 0)),
        "ipo_watch": sorted([f for f in facts if f.get("ipo_sessions")], key=lambda f: -(f.get("rs_rank") or 0)),
        "sell_signals": sorted([f for f in facts if f["breakdown50"] or (f["status"] == "retest" and (f["from_pivot"] or 0) <= -7)],
                               key=lambda f: (f.get("from_pivot") or 0)),
    }
    lists = {k: [{kk: f.get(kk) for kk in BOARD_KEYS + ("days_since_breakout", "ipo_sessions", "breakout_vol_ratio")}
                 | {"buy_point": (f.get("base") or {}).get("buy_point"), "buy_max": (f.get("base") or {}).get("buy_max"),
                    "base_kind": (f.get("base") or {}).get("kind"), "base_weeks": (f.get("base") or {}).get("weeks")}
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

    ranked = sorted(facts, key=lambda f: -f["score"])
    n_max, n_min = int(cfg["desk"].get("stories_max", 10)), int(cfg["desk"].get("stories_min", 5))
    chosen = [f for f in ranked if f["score"] >= 60][:n_max]
    if len(chosen) < n_min:
        chosen = ranked[:n_min]
    log("stories: " + ", ".join(f"{f['sym']}({f['status']},{f['score']})" for f in chosen))
    for f in chosen:
        enrich(f)
        time.sleep(0.5)

    # ── New America: one leader profiled per edition, never the same name twice in a month.
    profile = None
    recent = set()
    for p in sorted(DATA.glob("????-??-??.json"))[-20:]:
        try:
            recent.add((json.loads(p.read_text(encoding="utf-8")).get("profile") or {}).get("sym"))
        except Exception:  # noqa: BLE001
            pass
    chosen_syms = {f["sym"] for f in chosen}
    cands = [f for f in ranked if (f.get("rs_rank") or 0) >= 80 and f["status"] in ("breakout", "buy_zone", "setting_up", "extended", "in_base")
             and f["sym"] not in recent and (f.get("vs_sma50") or -99) > -3]
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

    # ── Investor's Corner: Fridays (and any day the desk is run with MARKETS_CORNER=1).
    corner_topic = None
    topics = cfg.get("corner_topics") or []
    if topics and (datetime.now(ZoneInfo(cfg["desk"].get("timezone", "America/Denver"))).weekday() == 4
                   or os.environ.get("MARKETS_CORNER") == "1"):
        n_prev = sum(1 for p in DATA.glob("????-??-??.json") if '"corner":{' in p.read_text(encoding="utf-8"))
        corner_topic = topics[n_prev % len(topics)]
        log(f"investor's corner: {corner_topic}")

    date = max(f["date"] for f in facts) if facts else datetime.now().date().isoformat()
    ah = {}
    if facts and after_hours_window(datetime.now(timezone.utc)):
        ah = after_hours([f["sym"] for f in facts], {f["sym"]: f["close"] for f in facts})
        for f in facts:
            f["after_hours"] = ah.get(f["sym"])
    ah_movers = sorted([f for f in facts if f.get("after_hours") and abs(f["after_hours"]["chg_pct"]) >= 3],
                       key=lambda f: -abs(f["after_hours"]["chg_pct"]))
    for f in ah_movers:
        fu = f.get("fundamentals")
        if fu is None:                                   # name, industry, next earnings for the after-hours list
            fc = fundamentals_cached(f["sym"])
            f["name"] = fc.get("name") or f["sym"]
            f["industry"] = fc.get("industry")
    save_series(facts)
    board_n = int(cfg["desk"].get("board_rows", 40))
    board = sorted(facts, key=lambda f: -(f.get("rs_rank") or 0))[:board_n]
    return {
        "date": date, "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "universe_size": len(facts), "pulse": pulse, "big_picture": big, "lists": lists, "groups": groups,
        "stories": chosen,  # full facts incl. series, fundamentals, news
        "profile": profile,
        "corner_topic": corner_topic,
        "after_hours_as_of": next((v["as_of"] for v in ah.values()), None),
        "ah_movers": [{"sym": f["sym"], "name": f.get("name") or (f.get("fundamentals") or {}).get("name") or f["sym"],
                       "industry": f.get("industry") or (f.get("fundamentals") or {}).get("industry"), "close": f["close"],
                       "after_hours": f["after_hours"], "status": f["status"], "rs_rank": f.get("rs_rank"),
                       "next_earnings": (f.get("fundamentals") or {}).get("next_earnings")} for f in ah_movers[:15]],
        "board": [{k: f.get(k) for k in BOARD_KEYS + ("rs_score",)} for f in board],
    }


def big_picture(pulse: dict) -> dict:
    """A factual read of the indexes in IBD's three states, from the 50-day line and the distribution count."""
    spx, ndx = pulse.get("spx") or {}, pulse.get("nasdaq") or {}
    dd = max(spx.get("dist_days") or 0, ndx.get("dist_days") or 0)
    above = [(x.get("vs_sma50") or 0) > 0 for x in (spx, ndx) if x]
    if above and all(above) and dd <= 4:
        state = "Confirmed uptrend"
    elif above and any(above) and dd <= 6:
        state = "Uptrend under pressure"
    else:
        state = "Market in correction"
    return {"state": state, "dist_days": dd,
            "note": f"S&P 500 {spx.get('dist_days', 0)} distribution days, Nasdaq {ndx.get('dist_days', 0)}, over the last 25 sessions."}


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
    move += f", on volume {f['vol_ratio']:.1f}x its 50-day average." if f.get("vol_ratio") else "."
    paras.append(move)
    if b:
        base_line = (f"The chart shows a {b['weeks']}-week {b['kind']} with a {b['depth']:.0f}% correction from the {b['rim_date']} high. "
                     f"The buy point is {b['buy_point']:.2f}, ten cents above that high, and the buy range runs to {b['buy_max']:.2f}.")
        if b.get("breakout_date"):
            base_line += (f" Shares cleared the buy point on {b['breakout_date']} on volume {f['breakout_vol_ratio']:.1f}x average"
                          f" and now sit {f['from_pivot']:+.1f}% from it.")
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
    watch = {"breakout": "Watch for the stock to hold above the buy point on any pullback; a close 7-8% below it is the standard sell rule.",
             "buy_zone": "The buy range closes 5% above the buy point; beyond it the entry is extended.",
             "extended": "Extended stocks are watched, not chased; the next entry is a new base or a pullback to the 21-day or 50-day line.",
             "retest": "A quick recovery above the buy point would keep the breakout alive; a close 7-8% below it would not.",
             "setting_up": "A move through the buy point on volume at least 1.2x average is what counts as a breakout.",
             "in_base": "The base needs to finish forming before any buy point counts.", "no_base": "No proper base yet."}[st]
    if f["bounce50"]:
        watch = "A bounce off the 50-day line is an early entry; the line itself is the stop."
    if f["breakdown50"]:
        watch = "A decisive close below the 50-day line on heavy volume is a sell signal for holders."
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
    out = [f'<svg class="chart" viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="{f["sym"]} daily chart">']
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
    out.append(f'<text x="{PL + 4}" y="{PT + 12}" class="lbl"><tspan fill="var(--ma21)">21-day</tspan>  <tspan fill="var(--ma50)">50-day</tspan>  <tspan fill="var(--ma200)">200-day</tspan></text>')
    out.append("</svg>")
    return "".join(out)


# ────────────────────────────────────────────────────────────────────────── editions + render

def edition_path(date: str) -> Path:
    return DATA / f"{date}.json"


def publish(payload: dict, articles: dict | None, cfg: dict) -> Path:
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
        stories.append(f | {"article": art, "chart": chart_svg(f, cfg)})
    ed = {k: v for k, v in payload.items() if k != "stories"}
    ed["stories"] = stories
    ed["market_note"] = (articles or {}).get("market_note")
    # New America profile: the writer's piece if it is there, else the desk's numbers.
    pf = payload.get("profile")
    if pf:
        a = (articles or {}).get("profile") or {}
        ok = bool(a.get("headline") and isinstance(a.get("body"), list) and len(a["body"]) >= 2)
        art = ({"sym": pf["sym"], "headline": str(a["headline"]).strip(), "deck": str(a.get("deck") or "").strip(),
                "body": [p.strip() for p in a["body"] if isinstance(p, str) and p.strip()],
                "watch": str(a.get("watch") or "").strip(), "by": "editor"} if ok else template_article(pf))
        ed["profile"] = pf | {"article": art, "chart": chart_svg(pf, cfg)}
    # Investor's Corner: only when the writer delivered one.
    c = (articles or {}).get("corner") or {}
    if c.get("headline") and isinstance(c.get("body"), list) and len(c["body"]) >= 3:
        ed["corner"] = {"topic": payload.get("corner_topic"), "headline": str(c["headline"]).strip(),
                        "deck": str(c.get("deck") or "").strip(), "example_sym": str(c.get("example_sym") or "").upper() or None,
                        "body": [p.strip() for p in c["body"] if isinstance(p, str) and p.strip()],
                        "takeaways": [t.strip() for t in (c.get("takeaways") or []) if isinstance(t, str) and t.strip()]}
    DATA.mkdir(parents=True, exist_ok=True)
    p = edition_path(payload["date"])
    p.write_text(json.dumps(ed, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    log(f"edition saved {p} — {len(stories)} stories ({sum(1 for s in stories if s['article']['by'] == 'editor')} by editor)")
    heads = [s["article"]["headline"] for s in stories]
    if ed.get("profile"):
        heads.append("New America: " + ed["profile"]["article"]["headline"])
    if ed.get("corner"):
        heads.append("Investor's Corner: " + ed["corner"]["headline"])
    record_pass("edition", payload["date"], ed["generated"], f"{payload['date']}.html", [s["sym"] for s in stories], heads,
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
    tpl = env.get_template("markets.html")
    SITE.mkdir(parents=True, exist_ok=True)
    if SERIES_DIR.is_dir():
        import shutil
        (SITE / "series").mkdir(exist_ok=True)
        for f in SERIES_DIR.glob("*.json"):
            shutil.copy2(f, SITE / "series" / f.name)
    tz = ZoneInfo(cfg["desk"].get("timezone", "America/Denver"))
    site_name = ((site_cfg or {}).get("site") or {}).get("name") or "BEEHIVE WIRE"
    index = [{"date": e["date"], "human": human_date(e["date"]), "count": len(e["stories"]),
              "syms": [s["sym"] for s in e["stories"]]} for e in reversed(eds)]
    mv = load_movers()
    if mv:
        asof = datetime.fromisoformat(mv["as_of"]).astimezone(tz)
        mv["as_of_human"] = asof.strftime("%A %I:%M %p %Z").replace(" 0", " ")
    ls = load_lists()
    if ls and ls.get("date"):
        ls["human"] = human_date(ls["date"])
    for i, e in enumerate(eds):
        gen = datetime.fromisoformat(e["generated"]).astimezone(tz)
        ctx = dict(site_name=site_name, desk=cfg["desk"], ed=e, human=human_date(e["date"]),
                   generated=gen.strftime("%I:%M %p %Z").lstrip("0"), movers=mv if i == len(eds) - 1 else None,
                   lists=ls if i == len(eds) - 1 else None,
                   status_label=STATUS_LABEL, index=index, latest=(i == len(eds) - 1), root="../")
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
    return len(eds)


# ────────────────────────────────────────────────────────────────────────── after hours

def after_hours_window(now_utc: datetime) -> bool:
    """True from 4:05 p.m. to 8:00 p.m. Eastern on a weekday (Yahoo's post-market tape), or when MARKETS_AH=1."""
    if os.environ.get("MARKETS_AH") == "1":
        return True
    et = now_utc.astimezone(ZoneInfo("America/New_York"))
    mins = et.hour * 60 + et.minute
    return et.weekday() < 5 and 16 * 60 + 5 <= mins <= 20 * 60


def after_hours(symbols: list[str], closes: dict) -> dict:
    """Post-market price per symbol from Yahoo's 5-minute pre/post tape, vs the official close in `closes`.
    One bulk request per 60 names. Returns {sym: {price, chg, chg_pct, as_of, volume}}."""
    import pandas as pd
    import yfinance as yf
    out = {}
    for i in range(0, len(symbols), 60):
        part = symbols[i:i + 60]
        try:
            df = yf.download(part, period="1d", interval="5m", prepost=True, group_by="ticker", auto_adjust=False,
                             progress=False, threads=True, timeout=30)
        except Exception as e:  # noqa: BLE001
            log(f"after-hours download failed ({e})")
            continue
        if df is None or df.empty:
            continue
        for sym in part:
            try:
                d = (df[sym] if isinstance(df.columns, pd.MultiIndex) else df).dropna(subset=["Close"])
            except KeyError:
                continue
            if d.empty:
                continue
            et = d.index.tz_convert("America/New_York")
            post = d[(et.hour >= 16)]
            if post.empty or not closes.get(sym):
                continue
            px = float(post["Close"].iloc[-1])
            c = float(closes[sym])
            out[sym] = {"price": round(px, 2), "chg": round(px - c, 2), "chg_pct": round((px / c - 1) * 100, 1),
                        "as_of": post.index[-1].tz_convert("America/New_York").strftime("%I:%M %p ET").lstrip("0"),
                        "volume": int(post["Volume"].sum())}
    log(f"after hours: {len(out)}/{len(symbols)} quotes")
    return out


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


def session_fraction(now_utc: datetime) -> tuple[float, bool]:
    """How much of the regular session (9:30-16:00 Eastern) has elapsed, and whether we are inside it."""
    et = now_utc.astimezone(ZoneInfo("America/New_York"))
    if et.weekday() >= 5:
        return 1.0, False
    mins = et.hour * 60 + et.minute
    start, end = 9 * 60 + 30, 16 * 60
    if mins <= start + 5:
        return 0.0, False
    if mins >= end:
        return 1.0, False
    return max(0.08, (mins - start) / (end - start)), True


def movers(cfg: dict) -> dict:
    """IBD-style 'Stocks on the Move': the biggest volume surges vs the 50-day average, projected to the full
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
    rs_all = {}
    for sym, d in prices.items():
        if len(d) >= 30:
            v = rs_score(d["Close"].to_numpy(dtype=float))
            if v is not None:
                rs_all[sym] = v
    ordered = sorted(rs_all.values())
    import bisect

    def rs_rank_of(sym):
        v = rs_all.get(sym)
        return None if v is None else max(1, min(99, round(100 * (bisect.bisect_left(ordered, v) + 1) / (len(ordered) + 1))))

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
    for r in ups + downs:   # chart status for the candidates only
        f = None
        try:
            f = analyze(r["sym"], prices[r["sym"]], bench, cfg)
        except Exception as e:  # noqa: BLE001
            log(f"{r['sym']}: analysis failed ({e})")
        if f:
            f["rs_rank"] = r.get("rs_rank")
            mv_facts.append(f)
            b = f.get("base") or {}
            r.update(status=f["status"], angle=angle(f), from_pivot=f.get("from_pivot"), buy_point=b.get("buy_point"),
                     buy_max=b.get("buy_max"), base_kind=b.get("kind"), base_weeks=b.get("weeks"), new_high=f["new_high"],
                     from_hi52=f.get("from_hi52"), vs_sma50=f.get("vs_sma50"), ipo_sessions=f.get("ipo_sessions"))
        r["qualifies"] = (r.get("rs_rank") or 0) >= min_rs
    save_series(mv_facts)
    if not live and after_hours_window(now):
        ahq = after_hours([r["sym"] for r in ups + downs], {r["sym"]: r["close"] for r in ups + downs})
        for r in ups + downs:
            r["after_hours"] = ahq.get(r["sym"])
    ups_q = [r for r in ups if r["qualifies"]][:n_up]
    ups_x = [r for r in ups if not r["qualifies"]][: max(0, n_up - len(ups_q))]
    out = {"as_of": now.isoformat(timespec="seconds"), "date": today, "live": live, "session_fraction": round(frac, 2),
           "universe_size": len(prices), "criteria": {"min_price": min_price, "min_avg_volume": min_avgvol, "min_volume_ratio": min_ratio,
                                                       "min_up_pct": min_up, "min_rs_rank": min_rs},
           "up": ups_q, "up_low_rs": ups_x, "down": downs[:n_down]}
    log(f"movers: {len(rows)} names over {min_ratio}x volume; {len(ups_q)} qualify up, {len(ups_x)} up on low RS, {len(out['down'])} down"
        + (f" (live, {round(frac * 100)}% of session, volume projected)" if live else " (closed, full-session volume)"))
    return out


SERIES_DIR = DATA / "series"


def save_series(facts: list[dict]) -> int:
    """One compact JSON per symbol (data/markets/series/SYM.json) so any ticker on the page can pop its chart.
    Overwritten on every scan that touches the symbol; copied into site/markets/series/ at render."""
    SERIES_DIR.mkdir(parents=True, exist_ok=True)
    n = 0
    for f in facts:
        s = f.get("series")
        if not s:
            continue
        b = f.get("base") or {}
        doc = {"sym": f["sym"], "name": f.get("name"), "date": f["date"], "close": f["close"], "chg": f.get("chg"), "chg_pct": f.get("chg_pct"),
               "status": f.get("status"), "angle": f.get("angle") or angle(f), "rs_rank": f.get("rs_rank"), "vol_ratio": f.get("vol_ratio"),
               "from_pivot": f.get("from_pivot"), "hi52": f.get("hi52"), "from_hi52": f.get("from_hi52"), "vs_sma50": f.get("vs_sma50"),
               "ipo_sessions": f.get("ipo_sessions"),
               "base": {k: b.get(k) for k in ("buy_point", "buy_max", "kind", "weeks", "depth", "rim_date", "low_date", "breakout_date")} if b else None,
               "series": s}
        (SERIES_DIR / f"{f['sym']}.json").write_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        n += 1
    log(f"series saved for {n} symbols -> {SERIES_DIR}")
    return n


# ────────────────────────────────────────────────────────────────────────── the pass index (archive + search)

PASSES_FILE = DATA / "passes.json"
KIND_LABEL = {"movers": "Stocks on the Move", "edition": "Edition"}


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


# ────────────────────────────────────────────────────────────────────────── the Beehive 50 and the Big Cap 20 (weekly)

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
    for f in pool:
        fu = fundamentals_cached(f["sym"])
        g = (fu.get("growth") or [{}])[0]
        f["name"], f["market_cap"], f["industry"] = fu.get("name") or f["sym"], fu.get("market_cap"), fu.get("industry")
        f["eps_yoy"], f["rev_yoy"] = g.get("eps_yoy"), g.get("rev_yoy")
        g2 = (fu.get("growth") or [{}, {}])[1] if len(fu.get("growth") or []) > 1 else {}
        f["eps_accel"] = (f["eps_yoy"] is not None and g2.get("eps_yoy") is not None and f["eps_yoy"] > g2["eps_yoy"])
        time.sleep(0.15)

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
    save_series(pool)
    elig = [f for f in pool if (f.get("eps_yoy") or 0) >= float(m.get("min_eps_growth", 15)) or (f.get("rev_yoy") or 0) >= float(m.get("min_rev_growth", 20))]
    elig.sort(key=lambda f: -f["composite"])
    big_min = float(m.get("big_cap_min", 40e9))
    big = [f for f in pool if (f.get("market_cap") or 0) >= big_min]
    big.sort(key=lambda f: -f["composite"])
    keys = ("sym", "name", "industry", "close", "chg_pct", "rs_rank", "eps_yoy", "rev_yoy", "eps_accel", "market_cap", "composite",
            "status", "from_pivot", "from_hi52", "vs_sma50", "ipo_sessions")
    row = lambda f, i: {k: f.get(k) for k in keys} | {"rank": i + 1, "buy_point": (f.get("base") or {}).get("buy_point"),  # noqa: E731
                                                      "buy_max": (f.get("base") or {}).get("buy_max"), "base_kind": (f.get("base") or {}).get("kind")}
    prev = load_lists() or {}
    prev50 = {r["sym"]: r["rank"] for r in prev.get("beehive50", [])}
    prev20 = {r["sym"]: r["rank"] for r in prev.get("bigcap20", [])}
    b50 = [row(f, i) | {"prev_rank": prev50.get(f["sym"])} for i, f in enumerate(elig[:50])]
    b20 = [row(f, i) | {"prev_rank": prev20.get(f["sym"])} for i, f in enumerate(big[:20])]
    out = {"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "date": max(f["date"] for f in facts) if facts else None,
           "universe_size": len(facts), "pool": len(pool), "beehive50": b50, "bigcap20": b20,
           "method": {"rs": 40, "eps": 25, "rev": 10, "tech": 25, "min_eps_growth": m.get("min_eps_growth", 15), "min_rev_growth": m.get("min_rev_growth", 20),
                      "big_cap_min": big_min, "min_price": m.get("min_price", 15), "min_dollar_volume": m.get("min_dollar_volume", 20_000_000)}}
    log(f"beehive 50: {', '.join(r['sym'] for r in b50[:10])} …  big cap 20: {', '.join(r['sym'] for r in b20[:8])} …")
    return out


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

SLOTS = [("movers", 8 * 60 + 5), ("movers", 11 * 60 + 5), ("edition", 15 * 60 + 20)]   # Mountain time, weekdays


def due_pass(now_utc: datetime | None = None) -> str | None:
    """GitHub drops and delays cron runs, so the hourly pinger-driven build asks: is a Market Desk pass
    overdue? Returns 'movers' / 'edition' for the earliest slot of today that has come round with no
    artifact printed since it, else None."""
    now = (now_utc or datetime.now(timezone.utc)).astimezone(ZoneInfo("America/Denver"))
    if now.weekday() >= 5:
        return None
    today = now.date().isoformat()
    mins = now.hour * 60 + now.minute
    mover_times = []
    for f in (DATA / "movers").glob(f"{today}-????.json") if (DATA / "movers").is_dir() else []:
        hhmm = f.stem[-4:]
        mover_times.append(int(hhmm[:2]) * 60 + int(hhmm[2:]))
    edition_time = None
    ep = edition_path(today)
    if ep.is_file():
        try:
            gen = datetime.fromisoformat(json.loads(ep.read_text(encoding="utf-8"))["generated"]).astimezone(ZoneInfo("America/Denver"))
            edition_time = gen.hour * 60 + gen.minute if gen.date().isoformat() == today else None
        except Exception:  # noqa: BLE001
            edition_time = None
    for kind, slot in SLOTS:
        if mins < slot:
            break
        if kind == "movers" and any(t >= slot - 10 for t in mover_times):
            continue
        if kind == "edition" and edition_time is not None and edition_time >= slot - 10:
            continue
        # a movers slot is also satisfied by any later edition (the edition runs movers itself)
        if kind == "movers" and edition_time is not None and edition_time >= slot:
            continue
        return kind
    return None


# ────────────────────────────────────────────────────────────────────────── main

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("scan")
    s.add_argument("--out", default="data/build/markets_payload.json")
    p = sub.add_parser("publish")
    p.add_argument("--payload", required=True)
    p.add_argument("--articles")
    sub.add_parser("render")
    sub.add_parser("run")
    sub.add_parser("movers")
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
    if a.cmd == "lists":
        save_lists(leader_lists(cfg))
        render_site()
        return 0

    if a.cmd in ("scan", "run"):
        payload = scan(cfg)
        out = ROOT / (a.out if a.cmd == "scan" else "data/build/markets_payload.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        log(f"payload -> {out} ({len(payload['stories'])} stories, {payload['universe_size']} names)")
        slim = {k: v for k, v in payload.items() if k not in ("board", "lists")}
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
        publish(payload, None, cfg)
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
        publish(payload, arts, cfg)
        render_site()
        return 0
    if a.cmd == "render":
        render_site()
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
