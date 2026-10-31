#!/usr/bin/env python3
"""
Beehive Wire -> YouTube: the free daily market wrap, one video per NYSE trading day, after the Market Desk
edition prints.

    python market_video.py due [--date D] [--dry-run] [--force]   # does today's desk edition need a video?
    python market_video.py --emit-payload data/video/market/payload.json [--date D]
    python market_video.py render [--script ...] [--fallback] [--tts kokoro|silent|...]
    python market_video.py upload [--force]                        # out/video.mp4 -> YouTube, recorded
    python market_video.py whoami                                  # which channel the credentials post to

Same three steps as video.py: Python picks the numbers from the desk's edition (data/markets/<date>.json),
Claude writes the narration (market_video_editorial.md), Python draws the slides, speaks them and uploads.
If the writer fails or leaves a segment out, that segment is read from a plain template built from the same
numbers, so the video never carries a number that is not in the payload.

The shape is a daily index wrap (docs/HANDOFF-daily-market-video.md section 3): the S&P 500 and Nasdaq charts
with the 50-day line and the distribution count, the desk's read of the market, two or three stocks near a buy
point, today's breakouts, after the close, the scorecard, tomorrow, and the offer.

Config: the `video:` block in config.yaml, overridden key by key by `market_video:` (channel guard, tags,
offer lines, length). Uploads are recorded in data/video/market_published.json under "market-<date>".
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import date as Date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

import build
import markets as M
import video as V
from video import (BRAND_BG, BRAND_DIM, BRAND_GOLD, BRAND_INK, FONT_BOLD, FONT_REG, FPS, H, W, brand_card,
                   fit_block, font, frame, log, wrap)

MDIR = build.DATA / "video" / "market"
OUT = MDIR / "out"
PUBLISHED = build.DATA / "video" / "market_published.json"
ET = ZoneInfo("America/New_York")
LABEL = "MARKET DESK"

UP, DN = (0x6a, 0xa0, 0xff), (0xe5, 0x53, 0x4b)
MA21, MA50, MA200 = (0x3c, 0xcf, 0x7a), BRAND_GOLD, (0xc9, 0xd1, 0xd9)
RSC, GRID, CHART_BG = (0xb4, 0x8c, 0xff), (0x2a, 0x32, 0x3c), (0x16, 0x1b, 0x22)
GOOD, BAD = (0x3c, 0xcf, 0x7a), (0xff, 0x6b, 0x6b)
INDEX_NAME = {"spx": "S&P 500", "nasdaq": "Nasdaq Composite"}


def cfg_mv() -> dict:
    """The edition video's settings with the market_video block laid over them (say_as is merged, not replaced)."""
    cfg = build.load_yaml(build.ROOT / "config.yaml")
    base, over = dict(cfg.get("video") or {}), dict(cfg.get("market_video") or {})
    say = {**(base.get("say_as") or {}), **(over.pop("say_as", None) or {})}
    v = {**base, **over, "say_as": say, "_site": cfg["site"]}
    return v


def published() -> list[dict]:
    try:
        return json.loads(PUBLISHED.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []


def key_of(d: str) -> str:
    return f"market-{d}"


def edition_dates() -> list[str]:
    return sorted(p.stem for p in M.DATA.glob("????-??-??.json"))


def load_edition(d: str) -> dict:
    return json.loads(M.edition_path(d).read_text(encoding="utf-8"))


def today_et() -> str:
    return datetime.now(timezone.utc).astimezone(ET).date().isoformat()


def weekday_label(d: str) -> str:
    return datetime.strptime(d, "%Y-%m-%d").strftime("%A, %B %d").replace(" 0", " ")


# ──────────────────────────────────────────────────────────────────────── due

def cmd_due(args) -> int:
    v = cfg_mv()
    d = args.date or today_et()
    day = Date.fromisoformat(d)
    if not M.is_trading_day(day):
        V.set_output(go="no", key=key_of(d), reason=f"{d} is not an NYSE trading day")
        return 0
    if not args.dry_run and not os.environ.get("YOUTUBE_REFRESH_TOKEN"):
        V.set_output(go="no", key=key_of(d), reason="YouTube credentials are not set; nothing to upload to")
        return 0
    if not M.edition_path(d).is_file():
        V.set_output(go="no", key=key_of(d), reason=f"the desk edition for {d} has not printed yet")
        return 0
    if args.force:
        V.set_output(go="yes", key=key_of(d), date=d, reason="forced")
        return 0
    if any(p.get("key") == key_of(d) for p in published()):
        V.set_output(go="no", key=key_of(d), reason="already has a video")
        return 0
    gen = build.parse_iso(load_edition(d).get("generated"))
    max_age = float(v.get("max_edition_age_hours", 6))
    if not args.dry_run and gen and (build.utcnow() - gen).total_seconds() > max_age * 3600:
        V.set_output(go="no", key=key_of(d), reason=f"edition is more than {max_age:g}h old; skipped, not backfilled")
        return 0
    V.set_output(go="yes", key=key_of(d), date=d, reason="new desk edition")
    return 0


# ──────────────────────────────────────────────────────────────────────── payload

_names = None


def name_of(sym: str, ed: dict | None = None) -> str:
    global _names
    if _names is None:
        try:
            _names = json.loads((M.DATA / "names.json").read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            _names = {}
    n = (_names.get(sym) or {}).get("name") if isinstance(_names.get(sym), dict) else _names.get(sym)
    if not n and ed:
        for f in (ed.get("stories") or []) + [ed.get("profile") or {}] + (ed.get("ah_movers") or []):
            if f.get("sym") == sym and f.get("name"):
                n = f["name"]
    if not n:
        p = M.SERIES_DIR / f"{sym}.json"
        if p.is_file():
            try:
                n = json.loads(p.read_text(encoding="utf-8")).get("name")
            except Exception:  # noqa: BLE001
                pass
    return short_name(n) if n else sym


def short_name(n: str) -> str:
    """'Analog Devices, Inc.' -> 'Analog Devices'. Spoken and on the slides; the ticker is printed beside it."""
    n = re.sub(r",?\s+(Inc|Corp|Corporation|Company|Co|Ltd|plc|PLC|N\.V|S\.A|Holdings|Group|Incorporated|Limited)\.?$", "", n.strip())
    n = re.sub(r",?\s+(Inc|Corp|Corporation|Ltd|plc)\.?$", "", n)
    return n.replace(" and Company", "").strip(" ,")


def trim_series(s: dict, d: str, n: int = 130) -> dict | None:
    """A per-symbol series cut to bars on or before the edition date (movers passes overwrite the files later)."""
    if not s or not s.get("d"):
        return None
    k = max((i for i, x in enumerate(s["d"]) if x[:10] <= d), default=None)
    if k is None or s["d"][k][:10] != d:
        return None                       # the file has no bar for the edition's day: no chart rather than a stale one
    lo = max(0, k + 1 - n)
    out = {key: s[key][lo:k + 1] for key in ("d", "o", "h", "l", "c", "v", "e21", "s50", "s200", "rs") if s.get(key)}
    for key in ("rim_i", "low_i", "bo_i"):
        if s.get(key) is not None and lo <= s[key] <= k:
            out[key] = s[key] - lo
    out["rsdot"] = [i - lo for i in (s.get("rsdot") or []) if lo <= i <= k]
    out["avgv50"] = s.get("avgv50")
    return out


def series_for(sym: str, ed: dict) -> dict | None:
    for f in ed.get("stories") or []:
        if f.get("sym") == sym and f.get("series"):
            return trim_series(f["series"], ed["date"])
    p = M.SERIES_DIR / f"{sym}.json"
    if p.is_file():
        try:
            return trim_series(json.loads(p.read_text(encoding="utf-8")).get("series"), ed["date"])
        except Exception:  # noqa: BLE001
            return None
    return None


def base_depth(sym: str, ed: dict):
    for f in ed.get("stories") or []:
        if f.get("sym") == sym and f.get("base"):
            return f["base"].get("depth")
    p = M.SERIES_DIR / f"{sym}.json"
    if p.is_file():
        try:
            return (json.loads(p.read_text(encoding="utf-8")).get("base") or {}).get("depth")
        except Exception:  # noqa: BLE001
            return None
    return None


def index_bars(d: str) -> dict:
    """Daily bars for the S&P 500 and the Nasdaq Composite up to the edition date, with the 21/50/200-day lines.
    The edition stores only the index numbers, so the bars are fetched here. Empty on failure: the slides then
    show the numbers without a chart."""
    syms = {"spx": "^GSPC", "nasdaq": "^IXIC"}
    try:
        cfgm = M.load_cfg()
        syms.update({k: s for k, s in (cfgm.get("benchmarks") or {}).items() if k in syms})
    except Exception:  # noqa: BLE001
        pass
    try:
        prices = M.download(list(syms.values()), "2y")
    except Exception as e:  # noqa: BLE001
        log(f"WARN index download failed: {e}")
        return {}
    out = {}
    for key, sym in syms.items():
        df = prices.get(sym)
        if df is None:
            continue
        df = df[[ix.date().isoformat() <= d for ix in df.index]]
        if not len(df) or df.index[-1].date().isoformat() != d:
            log(f"WARN {sym} has no bar for {d}; no chart for it")
            continue
        c = df["Close"].to_numpy(dtype=float)
        e21, s50, s200 = M.ema(c, 21), M.sma(c, 50), M.sma(c, 200)
        n = 130
        clean = lambda arr: [None if math.isnan(x) else round(float(x), 2) for x in arr[-n:]]
        out[key] = {"d": [ix.date().isoformat() for ix in df.index[-n:]],
                    "o": [round(float(x), 2) for x in df["Open"].to_numpy()[-n:]],
                    "h": [round(float(x), 2) for x in df["High"].to_numpy()[-n:]],
                    "l": [round(float(x), 2) for x in df["Low"].to_numpy()[-n:]],
                    "c": [round(float(x), 2) for x in c[-n:]],
                    "v": [float(x) for x in df["Volume"].to_numpy()[-n:]],
                    "e21": clean(e21), "s50": clean(s50), "s200": clean(s200)}
    return out


def follow_through(bars: dict, prev_state: str | None) -> bool:
    """O'Neil's follow-through day, only when the desk's prior read was a correction: on day four or later of a
    rally off the low of the last 25 sessions, a gain of 1.25% or more on volume above the prior session."""
    if not bars or prev_state != "Market in correction":
        return False
    c, v = bars["c"], bars["v"]
    if len(c) < 30:
        return False
    win = c[-25:]
    low_i = len(c) - 25 + win.index(min(win))
    day = len(c) - 1 - low_i + 1
    gain = c[-1] / c[-2] - 1
    return day >= 4 and gain >= 0.0125 and v[-1] > v[-2] and min(c[low_i:]) >= c[low_i]


def pick_setups(ed: dict, n: int) -> list[dict]:
    """Two or three names near a buy point with the base complete. The Desk Chief's picks and watch names come
    first (that is the desk's own judgment), then the strongest RS among names within 5% of the pivot, skipping
    any the chief said to avoid."""
    near = [f for f in (ed.get("lists") or {}).get("near_buy_point") or [] if f.get("buy_point")]
    chief = ed.get("chief") or {}
    picked = [p.get("sym") for p in chief.get("picks") or []]
    avoid = {a.get("sym") for a in chief.get("avoid") or []}
    near = [f for f in near if f["sym"] not in avoid and (f.get("from_pivot") or -99) >= -5]
    near.sort(key=lambda f: (f["sym"] not in picked, picked.index(f["sym"]) if f["sym"] in picked else 0,
                             -(f.get("rs_rank") or 0)))
    out = []
    for f in near:
        s = series_for(f["sym"], ed)
        if not s:
            continue
        call = next((p for p in chief.get("picks") or [] if p.get("sym") == f["sym"]), None)
        ag = [{"kind": a.get("kind"), "price": a.get("price"), "note": a.get("note")} for a in f.get("aggressive") or []]
        out.append({"sym": f["sym"], "name": name_of(f["sym"], ed), "close": f.get("close"), "chg_pct": f.get("chg_pct"),
                    "base_kind": f.get("base_kind"), "base_weeks": f.get("base_weeks"), "depth": base_depth(f["sym"], ed),
                    "buy_point": f.get("buy_point"), "buy_max": f.get("buy_max"), "from_pivot": f.get("from_pivot"),
                    "rs_rank": f.get("rs_rank"), "vs_sma50": f.get("vs_sma50"), "early_entries": ag,
                    "next_earnings": next((e.get("date") for e in ed.get("earnings_week") or []
                                           if e.get("sym") == f["sym"] and (e.get("days") or 0) >= 0), None),
                    "chief_call": (call or {}).get("call"), "chief_entry": (call or {}).get("entry"),
                    "_series": s})
        if len(out) >= n:
            break
    return out


def add_rs(setups: list[dict], spx: dict | None) -> list[dict]:
    """The per-symbol files keep only the weekly RS line, so the daily one (close / S&P 500 close, as the desk
    draws it) is rebuilt here from the index bars, matched by date."""
    if not spx:
        return setups
    by_d = dict(zip(spx["d"], spx["c"]))
    for s in setups:
        ser = s["_series"]
        if not [x for x in ser.get("rs") or [] if x is not None]:
            ser["rs"] = [round(c / by_d[d[:10]], 6) if by_d.get(d[:10]) else None for d, c in zip(ser["d"], ser["c"])]
    return setups


def headline_for(sym: str, ed: dict) -> str | None:
    for f in ed.get("stories") or []:
        if f.get("sym") == sym and (f.get("article") or {}).get("headline"):
            return f["article"]["headline"]
    p = ed.get("profile") or {}
    if p.get("sym") == sym and (p.get("article") or {}).get("headline"):
        return p["article"]["headline"]
    return None


def movers_for(d: str) -> list[dict]:
    """The closing Volume Surges list for the day: the latest movers pass printed on that date."""
    files = sorted((M.DATA / "movers").glob(f"{d}-????.json")) if (M.DATA / "movers").is_dir() else []
    doc = None
    for p in reversed(files):
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
            break
        except Exception:  # noqa: BLE001
            continue
    if doc is None:
        try:
            latest = json.loads((M.DATA / "movers-latest.json").read_text(encoding="utf-8"))
            doc = latest if latest.get("date") == d else None
        except Exception:  # noqa: BLE001
            doc = None
    if not doc:
        return []
    up = [m for m in doc.get("up") or [] if m.get("qualifies")]
    return [{"sym": m["sym"], "name": name_of(m["sym"]), "close": m.get("close"), "chg_pct": m.get("chg_pct"),
             "vol_pct": m.get("vol_pct"), "status": m.get("status"), "from_pivot": m.get("from_pivot")} for m in up[:3]]


def cmd_payload(args) -> int:
    v = cfg_mv()
    dates = edition_dates()
    d = args.date or (dates[-1] if dates else None)
    if not d or not M.edition_path(d).is_file():
        log(f"no desk edition for {d}")
        return 1
    ed = load_edition(d)
    prev_d = max((x for x in dates if x < d), default=None)
    prev = load_edition(prev_d) if prev_d else {}
    bars = index_bars(d)
    prev_state = (prev.get("big_picture") or {}).get("state")

    indexes = {}
    for key in ("spx", "nasdaq"):
        p = (ed.get("pulse") or {}).get(key) or {}
        pp = (prev.get("pulse") or {}).get(key) or {}
        b = bars.get(key)
        vs21 = None
        if b and b["e21"][-1]:
            vs21 = M.pct(b["c"][-1], b["e21"][-1])
        indexes[key] = {"name": INDEX_NAME[key], "close": p.get("close"), "chg_pct": p.get("chg_pct"),
                        "vs_sma50": p.get("vs_sma50"), "vs_sma200": p.get("vs_sma200"), "vs_ema21": vs21,
                        "from_hi52": p.get("from_hi52"), "dist_days": p.get("dist_days"),
                        "dist_days_prev": pp.get("dist_days"), "dist_today": d in (p.get("dist_dates") or []),
                        "dist_dates": p.get("dist_dates") or [],
                        "crossed_50": (None if pp.get("vs_sma50") is None or p.get("vs_sma50") is None
                                       else ("below" if pp["vs_sma50"] > 0 >= p["vs_sma50"] else
                                             "above" if pp["vs_sma50"] <= 0 < p["vs_sma50"] else None)),
                        "follow_through": follow_through(b, prev_state),
                        "_bars": b}
    chief = ed.get("chief") or {}
    bo_today = [f for f in (ed.get("lists") or {}).get("breakouts_week") or [] if f.get("days_since_breakout") == 0]
    breakouts = [{"sym": f["sym"], "name": name_of(f["sym"], ed), "buy_point": f.get("buy_point"), "close": f.get("close"),
                  "chg_pct": f.get("chg_pct"), "from_pivot": f.get("from_pivot"), "base_kind": f.get("base_kind"),
                  "base_weeks": f.get("base_weeks"), "rs_rank": f.get("rs_rank"),
                  "vol_vs_50day_avg": f.get("breakout_vol_ratio"), "headline": headline_for(f["sym"], ed)}
                 for f in bo_today[: int(v.get("max_breakouts", 5))]]
    in_range = len((ed.get("lists") or {}).get("in_buy_range") or [])

    ah = [m for m in ed.get("ah_movers") or [] if abs(((m.get("after_hours") or {}).get("chg_pct")) or 0) >= 2]
    reporting_today = {e["sym"] for e in ed.get("earnings_week") or [] if e.get("days") == 0}
    after = [{"sym": m["sym"], "name": short_name(m.get("name") or name_of(m["sym"], ed)), "close": m.get("close"),
              "after_hours_price": m["after_hours"].get("price"), "after_hours_chg_pct": m["after_hours"].get("chg_pct"),
              "as_of": m["after_hours"].get("as_of"), "reported_today": m["sym"] in reporting_today,
              "headline": headline_for(m["sym"], ed)} for m in ah[: int(v.get("max_after_hours", 6))]]
    tomorrow_reports = sorted([e for e in ed.get("earnings_week") or [] if e.get("days") == 1],
                              key=lambda e: -(e.get("rs_rank") or 0))
    tomorrow_reports = [{"sym": e["sym"], "name": name_of(e["sym"], ed), "date": e.get("date")} for e in tomorrow_reports[:6]]

    sc = (json.loads((M.DATA / "scorecard.json").read_text(encoding="utf-8")) if (M.DATA / "scorecard.json").is_file() else {})
    summ = sc.get("summary") or {}
    scorecard = {"as_of": (sc.get("generated") or "")[:10], "first_date": sc.get("first_date"), "calls": summ.get("n"),
                 "stopped_pct": summ.get("stopped_pct"), "hit20_pct": summ.get("hit20_pct")}
    for w in ("w1", "w4", "w8"):
        s, b = summ.get(w), summ.get(f"spx_{w}")
        scorecard[w] = None if not s else {"n": s.get("n"), "avg": s.get("avg"), "win": s.get("win"),
                                           "spx_avg": (b or {}).get("avg")}

    payload = {
        "show": "Beehive Wire Market Desk",
        "date": d, "weekday": weekday_label(d), "early_close": M.nyse_early_close(Date.fromisoformat(d)),
        "target_words": v.get("market_target_words", [850, 1150]),
        "indexes": indexes,
        "market": {"state": (ed.get("big_picture") or {}).get("state"), "state_prev": prev_state,
                   "market_note": ed.get("market_note"), "chief_verdict": chief.get("verdict"),
                   "chief_exposure": chief.get("market"), "dow": {k: ((ed.get("pulse") or {}).get("dow") or {}).get(k)
                                                                 for k in ("close", "chg_pct", "vs_sma50")}},
        "setups": add_rs(pick_setups(ed, int(v.get("setups", 3))), bars.get("spx")),
        "breakouts_today": breakouts, "in_buy_range_count": in_range,
        "volume_surges": movers_for(d),
        "after_close": {"as_of": ed.get("after_hours_as_of"), "movers": after, "reporting_tomorrow": tomorrow_reports},
        "scorecard": scorecard,
        "tomorrow": {"chief_watch": chief.get("watch")},
    }
    path = Path(args.emit_payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8")
    writer = {k: val for k, val in payload.items()}
    writer["indexes"] = {k: {kk: vv for kk, vv in x.items() if not kk.startswith("_")} for k, x in indexes.items()}
    writer["setups"] = [{kk: vv for kk, vv in s.items() if not kk.startswith("_")} for s in payload["setups"]]
    path.with_name("writer.json").write_text(json.dumps(writer, indent=1, ensure_ascii=False), encoding="utf-8")
    log(f"payload for {d}: {len(payload['setups'])} setups, {len(breakouts)} breakouts today, {len(after)} after-hours "
        f"movers, index bars for {', '.join(bars) or 'none'} -> {path} (+ writer.json for Claude)")
    return 0


# ──────────────────────────────────────────────────────────────────────── words

def num(x, dp: int = 2) -> str:
    if x is None:
        return "n/a"
    return f"{x:,.{dp}f}"


def pc(x, signed: bool = True) -> str:
    if x is None:
        return "n/a"
    return f"{x:+.1f}%" if signed else f"{abs(x):.1f}%"


def fallback_parts(p: dict) -> dict:
    """Plain narration from the payload's numbers. Used whole when there is no Claude script, and segment by
    segment for anything the script leaves out."""
    parts = {}
    for key in ("spx", "nasdaq"):
        x = p["indexes"][key]
        if x.get("close") is None:
            continue
        side = "above" if (x.get("vs_sma50") or 0) > 0 else "below"
        move = "rose" if (x.get("chg_pct") or 0) > 0 else "fell" if (x.get("chg_pct") or 0) < 0 else "was flat"
        t = (f"The {x['name']} {move} {pc(x['chg_pct'], False)} to {num(x['close'])}. "
             f"It closed {pc(x['vs_sma50'], False)} {side} its 50-day line")
        t += f" and {pc(x['vs_sma200'], False)} {'above' if (x.get('vs_sma200') or 0) > 0 else 'below'} the 200-day. " \
            if x.get("vs_sma200") is not None else ". "
        dd, dp = x.get("dist_days"), x.get("dist_days_prev")
        t += f"The distribution count stands at {dd} over the last 25 sessions"
        t += ", with a new one today." if x.get("dist_today") else (f", down from {dp}." if dp is not None and dd < dp else ".")
        if x.get("crossed_50"):
            t += f" Today's close took it back {x['crossed_50']} the 50-day line."
        if x.get("follow_through"):
            t += " By the desk's measure, today was a follow-through day."
        parts[key] = t
    m = p["market"]
    t = f"The desk's read of the market: {m['state']}."
    if m.get("state_prev") and m["state_prev"] != m["state"]:
        t += f" That is a change from {m['state_prev']}."
    if m.get("chief_exposure"):
        t += f" The Desk Chief says: {m['chief_exposure']}"
    parts["market"] = t
    segs = []
    for s in p["setups"]:
        t = (f"{s['name']} closed at {num(s['close'])}, {pc(s['from_pivot'], False)} below the {num(s['buy_point'])} buy point "
             f"of a {s['base_weeks']}-week {s['base_kind']}")
        t += f" {s['depth']:.0f}% deep." if s.get("depth") else "."
        t += f" The buy range runs to {num(s['buy_max'])}. Its RS rank on the desk is {s['rs_rank']}."
        if s.get("early_entries"):
            e = s["early_entries"][0]
            t += f" The desk also marks an early entry, a {e['kind']}, at {num(e['price'])}."
        if s.get("next_earnings"):
            t += f" It reports on {datetime.strptime(s['next_earnings'], '%Y-%m-%d').strftime('%B %d').replace(' 0', ' ')}."
        t += " The thing to watch is a decisive close above the buy point."
        segs.append({"sym": s["sym"], "narration": t})
    parts["setups"] = segs
    if p["breakouts_today"]:
        bits = [f"{b['name']} cleared the {num(b['buy_point'])} buy point of a {b['base_weeks']}-week {b['base_kind']} and closed at "
                f"{num(b['close'])}, {pc(b['from_pivot'], False)} past it" +
                (f", on volume {b['vol_vs_50day_avg']:.1f} times its 50-day average" if b.get("vol_vs_50day_avg") else "")
                for b in p["breakouts_today"]]
        parts["breakouts"] = "Breakouts today. " + ". ".join(bits) + "."
    else:
        parts["breakouts"] = "No stock on the desk's list cleared a buy point today."
    a = p["after_close"]
    if a["movers"]:
        bits = [f"{m['name']} {'fell' if m['after_hours_chg_pct'] < 0 else 'rose'} {pc(m['after_hours_chg_pct'], False)} "
                f"to {num(m['after_hours_price'])} after a regular close of {num(m['close'])}" for m in a["movers"][:5]]
        parts["after_close"] = f"After the close, as of {a['as_of']}. " + ". ".join(bits) + "."
    else:
        parts["after_close"] = "No name on the desk's list moved 2% or more after the close."
    if a["reporting_tomorrow"]:
        parts["after_close"] += " Reporting next: " + ", ".join(r["name"] for r in a["reporting_tomorrow"][:5]) + "."
    sc = p["scorecard"]
    best = next((sc[w] for w in ("w8", "w4", "w1") if sc.get(w)), None)
    wl = next((w for w in ("w8", "w4", "w1") if sc.get(w)), None)
    if best:
        span = {"w1": "one week", "w4": "four weeks", "w8": "eight weeks"}[wl]
        parts["scorecard"] = (f"The scorecard. Over {span}, the desk's {best['n']} calls average {pc(best['avg'])}, with "
                              f"{best['win']}% winners, against {pc(best['spx_avg'])} for the S&P 500. "
                              f"{sc['stopped_pct']}% have hit the 8% stop.")
    parts["tomorrow"] = f"For tomorrow. {p['tomorrow']['chief_watch']}" if p["tomorrow"].get("chief_watch") else ""
    parts["intro"] = f"This is the Beehive Wire Market Desk for {p['weekday']}, after the close."
    parts["outro"] = "That is the close."
    parts["title"] = ""
    parts["tags"] = []
    return parts


def validate_script(raw: dict, p: dict) -> tuple[dict, list[str]]:
    """Take what the writer got right, fill the rest from the template. Returns the script and what was filled."""
    fb = fallback_parts(p)
    out, filled = {}, []
    for k in ("intro", "spx", "nasdaq", "market", "breakouts", "after_close", "scorecard", "tomorrow", "outro"):
        say = V.clean_say(raw.get(k, "")) if isinstance(raw.get(k), str) else ""
        if len(say.split()) >= 4:
            out[k] = " ".join(say.split()[:320])
        else:
            out[k] = fb.get(k, "")
            if fb.get(k):
                filled.append(k)
    want = [s["sym"] for s in p["setups"]]
    got = {s.get("sym"): V.clean_say(s.get("narration", "")) for s in raw.get("setups") or [] if isinstance(s, dict)}
    fb_set = {s["sym"]: s["narration"] for s in fb["setups"]}
    out["setups"] = []
    for sym in want:
        if len((got.get(sym) or "").split()) >= 8:
            out["setups"].append({"sym": sym, "narration": got[sym]})
        else:
            out["setups"].append({"sym": sym, "narration": fb_set[sym]})
            filled.append(f"setup {sym}")
    out["title"] = V.clean_say(raw.get("title", ""))[:95]
    out["tags"] = [V.clean_say(t)[:30] for t in (raw.get("tags") or []) if isinstance(t, str) and V.clean_say(t)][:10]
    return out, filled


SPOKEN = [
    (re.compile(r"S&P"), "S and P"),
    (re.compile(r"(\d)%"), r"\1 percent"),
    (re.compile(r"\$9\.99\b"), "nine ninety-nine"),
    (re.compile(r"\bRS\b"), "R S"),
    (re.compile(r"\bATRs?\b"), "average true ranges"),
    (re.compile(r"\bEPS\b"), "earnings per share"),
    (re.compile(r"\bvs\.?\s"), "versus "),
    (re.compile(r"(\d),(\d{3})"), r"\1\2"),
]


def spoken(text: str) -> str:
    """Market copy has symbols a voice reads badly. The writer is told to spell them out; this catches the rest."""
    for rx, rep in SPOKEN:
        while rx.search(text):
            new = rx.sub(rep, text)
            if new == text:
                break
            text = new
    return text


# ──────────────────────────────────────────────────────────────────────── charts

def _dash(d, x1, y1, x2, y2, fill, width=2, on=12, off=8):
    length = math.hypot(x2 - x1, y2 - y1) or 1
    steps = int(length // (on + off)) + 1
    for i in range(steps):
        a = i * (on + off) / length
        b = min(1, (i * (on + off) + on) / length)
        d.line([(x1 + (x2 - x1) * a, y1 + (y2 - y1) * a), (x1 + (x2 - x1) * b, y1 + (y2 - y1) * b)], fill=fill, width=width)


def draw_chart(img, box: tuple, s: dict, base: dict | None = None, dist_dates: list | None = None, rs: bool = True):
    """Daily bars, the 21/50/200-day lines, the buy range shaded and the pivot dashed, the RS line in its own
    strip, and volume. Same marks as the desk's chart (markets.chart_svg), drawn at video size in the house
    colours. Distribution days on an index chart are marked with a red tick under the bar."""
    from PIL import Image, ImageDraw
    x0, y0, x1, y1 = box
    d = ImageDraw.Draw(img)
    d.rectangle(box, fill=CHART_BG)
    n = len(s["c"])
    axis_w = 150
    has_rs = rs and len([v for v in (s.get("rs") or []) if v is not None]) > 5
    vol_h = 150
    rs_h = 110 if has_rs else 0
    pt, pb = y0 + 54, y1 - vol_h - rs_h - 40
    px0, px1 = x0 + 16, x1 - axis_w
    xs = lambda i: px0 + (i + 0.5) * (px1 - px0) / n
    lo, hi = min(s["l"]), max(s["h"])
    if base and base.get("buy_max"):
        hi = max(hi, base["buy_max"])
        lo = min(lo, base["buy_point"])
    pad = (hi - lo) * 0.05 or 1
    lo, hi = lo - pad, hi + pad
    ys = lambda p: pt + (hi - p) / (hi - lo) * (pb - pt)
    lblf, small = font(FONT_REG, 26), font(FONT_BOLD, 26)

    step = M.nice_step((hi - lo) / 6)
    g = math.ceil(lo / step) * step
    taken = [ys(x) for x in ((base or {}).get("buy_point"), (base or {}).get("buy_max"), s["c"][-1]) if x]
    while g < hi:
        d.line([(px0, ys(g)), (px1, ys(g))], fill=GRID, width=1)
        if all(abs(ys(g) - y) > 34 for y in taken):
            d.text((px1 + 12, ys(g)), f"{g:,.0f}" if step >= 1 else f"{g:,.2f}", font=lblf, fill=BRAND_DIM, anchor="lm")
        g += step
    last_m = None
    for i, day in enumerate(s["d"]):
        if day[:7] != last_m:
            last_m = day[:7]
            if i > 2:
                d.line([(xs(i), pt), (xs(i), y1 - 34)], fill=GRID, width=1)
                d.text((xs(i), y1 - 8), datetime.fromisoformat(day[:10]).strftime("%b"), font=lblf, fill=BRAND_DIM, anchor="ms")

    if base and base.get("buy_point"):
        over = Image.new("RGBA", img.size, (0, 0, 0, 0))
        od = ImageDraw.Draw(over)
        od.rectangle([px0, ys(base["buy_max"]), px1, ys(base["buy_point"])], fill=(60, 207, 122, 46))
        img.paste(Image.alpha_composite(img.convert("RGBA"), over).convert("RGB"))
        d = ImageDraw.Draw(img)
        _dash(d, px0, ys(base["buy_point"]), px1, ys(base["buy_point"]), GOOD, 3)
        d.text((px1 + 12, ys(base["buy_point"])), f"buy {base['buy_point']:,.2f}", font=small, fill=GOOD, anchor="lm")
        d.text((px1 + 12, ys(base["buy_max"]) - 2), f"+5% {base['buy_max']:,.2f}", font=lblf, fill=GOOD, anchor="lb")
        for key, arr in (("rim_i", s["h"]), ("low_i", s["l"])):
            i = s.get(key)
            if i is not None and 0 <= i < n:
                r = 9
                d.ellipse([xs(i) - r, ys(arr[i]) - r, xs(i) + r, ys(arr[i]) + r], outline=GOOD, width=3)

    for key, col, wd in (("s200", MA200, 3), ("s50", MA50, 4), ("e21", MA21, 3)):
        run = []                                   # break the line where it leaves the price panel
        for i, v in enumerate((s.get(key) or []) + [None]):
            if v is not None and pt <= ys(v) <= pb:
                run.append((xs(i), ys(v)))
                continue
            if len(run) > 1:
                d.line(run, fill=col, width=wd, joint="curve")
            run = []

    bw = max(2.0, (px1 - px0) / n * 0.55)
    for i in range(n):
        up = s["c"][i] >= (s["c"][i - 1] if i else s["o"][i])
        col = UP if up else DN
        x = xs(i)
        d.line([(x, ys(s["h"][i])), (x, ys(s["l"][i]))], fill=col, width=3)
        d.line([(x - bw / 2 - 1, ys(s["o"][i])), (x, ys(s["o"][i]))], fill=col, width=3)
        d.line([(x, ys(s["c"][i])), (x + bw / 2 + 1, ys(s["c"][i]))], fill=col, width=3)

    yc = ys(s["c"][-1])
    d.rectangle([px1 + 4, yc - 18, x1 - 4, yc + 18], fill=BRAND_INK)
    d.text((px1 + 12, yc), f"{s['c'][-1]:,.2f}", font=small, fill=BRAND_BG, anchor="lm")

    if has_rs:
        r0, r1 = pb + 20, pb + 20 + rs_h - 20
        rv = [v for v in s["rs"] if v is not None]
        rlo, rhi = min(rv), max(rv)
        rng = (rhi - rlo) or 1e-9
        ry = lambda v: r1 - (v - rlo) / rng * (r1 - r0)
        pts = [(xs(i), ry(v)) for i, v in enumerate(s["rs"]) if v is not None]
        d.line(pts, fill=RSC, width=3)
        for i in s.get("rsdot") or []:
            if 0 <= i < n and s["rs"][i] is not None:
                d.ellipse([xs(i) - 6, ry(s["rs"][i]) - 6, xs(i) + 6, ry(s["rs"][i]) + 6], fill=RSC)
        d.text((px1 + 12, ry(rv[-1])), "RS line", font=lblf, fill=RSC, anchor="lm")

    v0, v1 = y1 - vol_h - 34, y1 - 40
    vmax = max(s["v"]) or 1
    dist = set(dist_dates or [])
    for i in range(n):
        up = s["c"][i] >= (s["c"][i - 1] if i else s["o"][i])
        x = xs(i)
        d.rectangle([x - bw / 2, v1 - s["v"][i] / vmax * (v1 - v0), x + bw / 2, v1], fill=UP if up else DN)
        if s["d"][i][:10] in dist:
            d.polygon([(x, v0 - 26), (x - 9, v0 - 42), (x + 9, v0 - 42)], fill=BAD)
    if s.get("avgv50"):
        yv = v1 - s["avgv50"] / vmax * (v1 - v0)
        d.line([(px0, yv), (px1, yv)], fill=MA50, width=2)
    d.text((px1 + 12, v1), "volume", font=lblf, fill=BRAND_DIM, anchor="lb")

    lx = x0 + 22
    for txt, col in (("21-day", MA21), ("50-day", MA50), ("200-day", MA200)):
        d.text((lx, y0 + 16), txt, font=small, fill=col)
        lx += d.textlength(txt, font=small) + 34
    if dist:
        d.polygon([(lx + 10, y0 + 40), (lx + 1, y0 + 24), (lx + 19, y0 + 24)], fill=BAD)
        d.text((lx + 30, y0 + 16), "distribution day", font=small, fill=BAD)
    return img


def facts_panel(d, x: int, y: int, rows: list[tuple], w: int = 470):
    """The numbers beside a chart: label above, value below, coloured where the sign matters."""
    lab, val = font(FONT_REG, 32), font(FONT_BOLD, 54)
    for label, value, col in rows:
        d.text((x, y), label.upper(), font=lab, fill=BRAND_DIM)
        f = val
        size = 54
        while d.textlength(value, font=f) > w and size > 30:
            size -= 4
            f = font(FONT_BOLD, size)
        d.text((x, y + 38), value, font=f, fill=col or BRAND_INK)
        y += 118
    return y


def sign_col(x):
    return None if x is None else (GOOD if x > 0 else BAD if x < 0 else None)


def slide_index(x: dict, state: str, label: str):
    img, d = frame(f"BEEHIVE WIRE  |  {LABEL}  |  {label}", x["name"].upper())
    b = x.get("_bars")
    if b:
        draw_chart(img, (40, 126, 1390, H - 40), b, None, x.get("dist_dates"), rs=False)
    else:
        d.text((715, 560), "Chart unavailable", font=font(FONT_BOLD, 60), fill=BRAND_DIM, anchor="mm")
    from PIL import ImageDraw
    d = ImageDraw.Draw(img)                  # draw_chart pasted a new image in, so take a fresh handle
    dd = x.get("dist_days")
    ddp = x.get("dist_days_prev")
    dd_s = f"{dd}" + (" (+1 today)" if x.get("dist_today") else (f" (was {ddp})" if ddp is not None and ddp != dd else ""))
    rows = [("Close", f"{num(x['close'])}", None), ("Today", pc(x["chg_pct"]), sign_col(x["chg_pct"])),
            ("vs 50-day line", pc(x["vs_sma50"]), sign_col(x["vs_sma50"])),
            ("vs 200-day line", pc(x["vs_sma200"]), sign_col(x["vs_sma200"])),
            ("Distribution days (25)", dd_s, BAD if (dd or 0) >= 5 else None),
            ("The desk's read", state, BRAND_GOLD)]
    facts_panel(d, 1430, 150, rows)
    return img


def slide_setup(s: dict, idx: int, total: int, label: str):
    img, d = frame(f"BEEHIVE WIRE  |  {LABEL}  |  {label}", f"SETTING UP  {idx} of {total}")
    draw_chart(img, (40, 126, 1390, H - 40), s["_series"], {"buy_point": s["buy_point"], "buy_max": s["buy_max"]})
    from PIL import ImageDraw
    d = ImageDraw.Draw(img)
    head = font(FONT_BOLD, 64)
    nm = s["name"]
    while d.textlength(nm, font=head) > 460 and head.size > 36:
        head = font(FONT_BOLD, head.size - 4)
    d.text((1430, 140), s["sym"], font=font(FONT_BOLD, 40), fill=BRAND_GOLD)
    d.text((1430, 186), nm, font=head, fill=BRAND_INK)
    kind = f"{s['base_weeks']}-week {s['base_kind']}" + (f", {s['depth']:.0f}% deep" if s.get("depth") else "")
    rows = [("Base", kind, None), ("Buy point", num(s["buy_point"]), GOOD), ("Buy range to", num(s["buy_max"]), None),
            ("Close / from pivot", f"{num(s['close'])}  {pc(s['from_pivot'])}", sign_col(s["from_pivot"])),
            ("RS rank (desk)", str(s.get("rs_rank") or "n/a"), None)]
    facts_panel(d, 1430, 290, rows)
    d.text((1430, H - 60), "Full list on the Market Desk", font=font(FONT_REG, 30), fill=BRAND_DIM)
    return img


def slide_table(title: str, cols: list[str], rows: list[list], label: str, note: str = "", colors: list | None = None):
    img, d = frame(f"BEEHIVE WIRE  |  {LABEL}  |  {label}", title.upper())
    d.text((80, 170), title, font=font(FONT_BOLD, 80), fill=BRAND_GOLD)
    if not rows:
        f, lines, size = fit_block(d, note, FONT_BOLD, W - 160, 500, 96, 50)
        y = 420
        for ln in lines:
            d.text((80, y), ln, font=f, fill=BRAND_INK)
            y += size * 1.15
        return img
    widths = [0.30, 0.17, 0.17, 0.17, 0.19][: len(cols)]
    tot = sum(widths)
    xs, x = [], 80
    for wd in widths:
        xs.append(x)
        x += (W - 160) * wd / tot
    hf, rf = font(FONT_REG, 34), font(FONT_BOLD, 48)
    y = 300
    for i, c in enumerate(cols):
        d.text((xs[i], y), c.upper(), font=hf, fill=BRAND_DIM)
    y += 60
    d.line([(80, y), (W - 80, y)], fill=GRID, width=2)
    y += 22
    rowh = min(96, int((H - 160 - y) / max(1, len(rows))))
    for r, row in enumerate(rows):
        for i, cell in enumerate(row):
            col = (colors[r][i] if colors and colors[r][i] else BRAND_INK) if i else BRAND_INK
            fnt = rf
            while d.textlength(str(cell), font=fnt) > (W - 160) * widths[i] / tot - 20 and fnt.size > 26:
                fnt = font(FONT_BOLD, fnt.size - 4)
            d.text((xs[i], y), str(cell), font=fnt, fill=col)
        y += rowh
    if note:
        d.text((80, H - 70), note, font=font(FONT_REG, 32), fill=BRAND_DIM)
    return img


def slide_text(title: str, body: str, label: str, right: str = ""):
    img, d = frame(f"BEEHIVE WIRE  |  {LABEL}  |  {label}", right or title.upper())
    d.text((80, 170), title, font=font(FONT_BOLD, 84), fill=BRAND_GOLD)
    f, lines, size = fit_block(d, body, FONT_BOLD, W - 160, 700, 78, 40)
    y = 320
    for ln in lines:
        d.text((80, y), ln, font=f, fill=BRAND_INK)
        y += size * 1.2
    return img


def human_day(d: str | None) -> str:
    return datetime.strptime(d, "%Y-%m-%d").strftime("%b %d").replace(" 0", " ") if d else "the start"


def slide_scorecard(sc: dict, label: str):
    img, d = frame(f"BEEHIVE WIRE  |  {LABEL}  |  {label}", "THE SCORECARD")
    d.text((80, 170), "Every call the desk has printed", font=font(FONT_BOLD, 76), fill=BRAND_GOLD)
    d.text((80, 270), f"{sc.get('calls') or 0} breakout and buy-range calls since {human_day(sc.get('first_date'))}, "
                      f"measured from the close each one printed.", font=font(FONT_REG, 38), fill=BRAND_DIM)
    x = 80
    for w, lab in (("w1", "1 WEEK"), ("w4", "4 WEEKS"), ("w8", "8 WEEKS")):
        box = sc.get(w)
        d.rectangle([x, 370, x + 560, 860], outline=GRID, width=3)
        d.text((x + 30, 400), lab, font=font(FONT_BOLD, 40), fill=BRAND_DIM)
        if box:
            d.text((x + 30, 470), pc(box["avg"]), font=font(FONT_BOLD, 120), fill=sign_col(box["avg"]) or BRAND_INK)
            d.text((x + 30, 630), f"{box['win']}% winners", font=font(FONT_BOLD, 50), fill=BRAND_INK)
            d.text((x + 30, 700), f"S&P 500: {pc(box['spx_avg'])}", font=font(FONT_REG, 44), fill=BRAND_DIM)
            d.text((x + 30, 760), f"{box['n']} calls", font=font(FONT_REG, 40), fill=BRAND_DIM)
        else:
            d.text((x + 30, 500), "Not enough\nhistory yet", font=font(FONT_BOLD, 64), fill=BRAND_DIM)
        x += 600
    d.text((80, 930), f"{sc.get('stopped_pct') or 0}% of calls have hit the 8% stop.  {sc.get('hit20_pct') or 0}% have reached +20%.",
           font=font(FONT_BOLD, 44), fill=BRAND_INK)
    return img


# ──────────────────────────────────────────────────────────────────────── render

def cmd_render(args) -> int:
    v = cfg_mv()
    p = json.loads(Path(args.payload).read_text(encoding="utf-8"))
    label = p["weekday"].upper()
    script, filled = None, []
    sp = Path(args.script)
    if not args.fallback and sp.exists() and sp.stat().st_size > 0:
        try:
            script, filled = validate_script(json.loads(sp.read_text(encoding="utf-8")), p)
            log(f"script ok; filled from the template: {', '.join(filled) or 'nothing'}")
        except (ValueError, json.JSONDecodeError) as e:
            log(f"WARN script unusable ({e}); reading the numbers straight")
    if script is None:
        script, _ = validate_script({}, p)
        filled = ["everything"]
    st = p["market"]["state"] or ""
    head = script["title"] or f"{st}: the S&P 500 {'gains' if (p['indexes']['spx'].get('chg_pct') or 0) >= 0 else 'slips'} " \
                              f"{pc(p['indexes']['spx'].get('chg_pct'), False)}"
    title = V.brand_title(head)

    work = OUT / "work"
    if OUT.exists():
        shutil.rmtree(OUT)
    work.mkdir(parents=True)

    offer_early = v.get("offer_early", "")
    offer = v.get("offer", "")
    beats = [("intro", brand_card(f"THE CLOSE\n{p['weekday']}", LABEL, (W, H)),
              f"{script['intro']} {offer_early}".strip(), "Intro")]
    for key in ("spx", "nasdaq"):
        if p["indexes"][key].get("close") is not None:
            beats.append((key, slide_index(p["indexes"][key], st, label), script[key], p["indexes"][key]["name"]))
    beats.append(("market", slide_text(st, "The Desk Chief: " + (p["market"].get("chief_exposure") or ""), label, "THE DESK'S READ"),
                  script["market"], "The desk's read"))
    by = {s["sym"]: s for s in p["setups"]}
    for i, seg in enumerate(script["setups"], 1):
        s = by[seg["sym"]]
        beats.append((f"setup:{s['sym']}", slide_setup(s, i, len(script["setups"]), label), seg["narration"],
                      f"Setting up: {s['name']}"))
    bo = p["breakouts_today"]
    beats.append(("breakouts", slide_table(
        "Breakouts today", ["Stock", "Buy point", "Close", "Past pivot", "Vol vs avg"],
        [[f"{b['sym']}  {b['name']}", num(b["buy_point"]), num(b["close"]), pc(b["from_pivot"]),
          f"{b['vol_vs_50day_avg']:.1f}x" if b.get("vol_vs_50day_avg") else "n/a"] for b in bo],
        label, "Nothing on the desk's list cleared a buy point today." if not bo else
        f"{p['in_buy_range_count']} names sit in a buy range on the full desk.",
        [[None, GOOD, None, None, None] for _ in bo]), script["breakouts"], "Breakouts today"))
    a = p["after_close"]
    beats.append(("after", slide_table(
        "After the close", ["Stock", "Close", "After hours", "Move", "Reported"],
        [[f"{m['sym']}  {m['name']}", num(m["close"]), num(m["after_hours_price"]), pc(m["after_hours_chg_pct"]),
          "today" if m["reported_today"] else ""] for m in a["movers"]],
        label, ("As of " + (a.get("as_of") or "") + ". " if a["movers"] else "") +
        ("Reporting next: " + ", ".join(r["sym"] for r in a["reporting_tomorrow"]) if a["reporting_tomorrow"] else ""),
        [[None, None, None, sign_col(m["after_hours_chg_pct"]), None] for m in a["movers"]]),
        script["after_close"], "After the close"))
    if script.get("scorecard"):
        beats.append(("scorecard", slide_scorecard(p["scorecard"], label), script["scorecard"], "The scorecard"))
    if script.get("tomorrow"):
        beats.append(("tomorrow", slide_text("Tomorrow", p["tomorrow"].get("chief_watch") or "", label, "WHAT TO WATCH"),
                      script["tomorrow"], "Tomorrow"))
    beats.append(("offer", brand_card("TRY THE MARKET DESK\nTHREE WEEKS FOR $9.99\nbeehivewire.com/markets", LABEL, (W, H)),
                  " ".join(x for x in (script["outro"], offer, v.get("market_signoff", "")) if x), "Try the Market Desk"))
    beats = [b for b in beats if b[2]]

    engine = args.tts or v.get("tts", "kokoro")
    used, clips, chapters, t = set(), [], [], 0.0
    pad = float(v.get("pad_seconds", 0.7))
    for n, (kind, img, say, chap) in enumerate(beats):
        png, mp3, mp4 = work / f"{n:02d}.png", work / f"{n:02d}.mp3", work / f"{n:02d}.mp4"
        img.save(png)
        used.add(V.speak(spoken(say), mp3, v, engine))
        dur = V.duration(mp3) + pad
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-loop", "1", "-framerate", str(FPS), "-i", str(png),
                        "-i", str(mp3), "-af", f"apad=pad_dur={pad}", "-t", f"{dur:.3f}", "-c:v", "libx264",
                        "-tune", "stillimage", "-pix_fmt", "yuv420p", "-r", str(FPS), "-c:a", "aac", "-b:a", "160k",
                        "-ar", "44100", "-ac", "2", "-movflags", "+faststart", str(mp4)], check=True)
        clips.append(mp4)
        if not chapters or chapters[-1]["label"] != chap:
            chapters.append({"t": t, "label": chap})
        t += dur
    listing = work / "list.txt"
    listing.write_text("".join(f"file '{c.name}'\n" for c in clips), encoding="utf-8")
    video = OUT / "video.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(listing),
                    "-c", "copy", "-movflags", "+faststart", str(video)], check=True)
    brand_card(f"THE CLOSE\n{head}", f"{LABEL}  |  {datetime.strptime(p['date'], '%Y-%m-%d').strftime('%b %d').upper()}").save(OUT / "thumbnail.png")

    link = v.get("offer_link", "https://beehivewire.com/markets")
    desc = [f"Try the Market Desk: three weeks for $9.99. {link}", "",
            f"The close for {p['weekday']}: the S&P 500 and the Nasdaq against their 50-day lines and distribution "
            "counts, stocks near a buy point, today's breakouts, after the close, and the desk's scorecard. "
            "Every number is from the Beehive Wire Market Desk's own data for the day.", "",
            "CHAPTERS", *[f"{V.fmt_ts(c['t'])} {c['label']}" for c in chapters], "",
            f"The Market Desk: {v.get('price_detail', '$9.99 for your first 21 days, then $49.99 a month. Cancel any time.')} {link}",
            "The front page, free, twice a day: https://beehivewire.com", "",
            "The desk follows the breakout discipline William O'Neil described in How to Make Money in Stocks. "
            "Not affiliated with Investor's Business Daily. Not investment advice.",
            "Narrated by a synthetic voice."]
    meta = {"key": key_of(p["date"]), "date": p["date"], "title": title, "description": "\n".join(desc)[:4900],
            "tags": (v.get("market_tags") or []) + script["tags"], "duration": t, "tts": sorted(used),
            "filled_from_template": filled, "chapters": chapters}
    (OUT / "meta.json").write_text(json.dumps(meta, indent=1, ensure_ascii=False), encoding="utf-8")
    if not args.keep_work:
        shutil.rmtree(work)
    log(f"rendered {video} ({V.fmt_ts(t)}, voice: {'+'.join(sorted(used))}): {title}")
    return 0


# ──────────────────────────────────────────────────────────────────────── YouTube

def cmd_whoami(args) -> int:
    ch = V.my_channel(V.access_token())
    log(f"credentials post to: {ch['title']} ({ch['id']})")
    return 0


def cmd_upload(args) -> int:
    v = cfg_mv()
    meta = json.loads((OUT / "meta.json").read_text(encoding="utf-8"))
    video = OUT / "video.mp4"
    if not args.force and any(p.get("key") == meta["key"] for p in published()):
        log(f"{meta['key']} already uploaded; standing down")
        return 0
    tok = V.access_token()
    want = (v.get("expect_channel") or "").lower()
    ch = V.my_channel(tok)
    if want and want not in ch["title"].lower():
        raise SystemExit(f"refusing to upload: credentials post to '{ch['title']}', not '{v['expect_channel']}'")
    auth = {"Authorization": f"Bearer {tok}"}
    body = {"snippet": {"title": meta["title"], "description": meta["description"],
                        "tags": [t for t in meta["tags"] if t][:15], "categoryId": str(v.get("market_category_id", 25))},
            "status": {"privacyStatus": v.get("privacy", "public"), "selfDeclaredMadeForKids": False,
                       "containsSyntheticMedia": bool(v.get("synthetic_disclosure", False))}}
    init = requests.post("https://www.googleapis.com/upload/youtube/v3/videos",
                         params={"uploadType": "resumable", "part": "snippet,status"},
                         headers={**auth, "Content-Type": "application/json; charset=UTF-8",
                                  "X-Upload-Content-Type": "video/mp4", "X-Upload-Content-Length": str(video.stat().st_size)},
                         json=body, timeout=60)
    if init.status_code != 200:
        raise SystemExit(f"upload start failed ({init.status_code}): {init.text[:400]}")
    data = video.read_bytes()
    for attempt in range(4):
        up = requests.put(init.headers["Location"], data=data, headers={"Content-Type": "video/mp4"}, timeout=600)
        if up.status_code in (200, 201):
            break
        if up.status_code < 500:
            raise SystemExit(f"upload failed ({up.status_code}): {up.text[:400]}")
        time.sleep(5 * 2 ** attempt)
    else:
        raise SystemExit("upload failed after retries")
    vid = up.json()["id"]
    log(f"uploaded https://youtu.be/{vid} to {ch['title']} ({up.json().get('status', {}).get('privacyStatus')})")
    earlier = published()
    for rec in earlier:
        if rec.get("key") == meta["key"] and not rec.get("hidden") and V.hide_video(tok, rec["video_id"]):
            rec["hidden"] = True
    rec = {"key": meta["key"], "date": meta["date"], "video_id": vid, "title": meta["title"], "channel": ch["title"],
           "uploaded": build.iso(build.utcnow())}
    PUBLISHED.parent.mkdir(parents=True, exist_ok=True)
    PUBLISHED.write_text(json.dumps(earlier + [rec], indent=1), encoding="utf-8")
    th = requests.post("https://www.googleapis.com/upload/youtube/v3/thumbnails/set", params={"videoId": vid},
                       headers={**auth, "Content-Type": "image/png"}, data=(OUT / "thumbnail.png").read_bytes(), timeout=60)
    if th.status_code != 200:
        log(f"WARN thumbnail not set ({th.status_code}): {th.text[:200]}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", nargs="?", choices=["due", "render", "upload", "whoami"])
    ap.add_argument("--date", help="the desk edition's date, YYYY-MM-DD (default: today in New York / the newest edition)")
    ap.add_argument("--emit-payload", metavar="PATH")
    ap.add_argument("--payload", default=str(MDIR / "payload.json"))
    ap.add_argument("--script", default=str(MDIR / "script.json"))
    ap.add_argument("--fallback", action="store_true", help="skip the Claude script; read the numbers straight")
    ap.add_argument("--tts", choices=["kokoro", "edge", "espeak", "sapi", "silent"])
    ap.add_argument("--keep-work", action="store_true", help="render: keep the slide PNGs")
    ap.add_argument("--dry-run", action="store_true", help="due: render-only run (no credentials needed, no age limit)")
    ap.add_argument("--force", action="store_true", help="due, upload: go even if the day already has a video")
    args = ap.parse_args()
    if args.emit_payload:
        return cmd_payload(args)
    if not args.cmd:
        ap.error("give a command or --emit-payload")
    return {"due": cmd_due, "render": cmd_render, "upload": cmd_upload, "whoami": cmd_whoami}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
