"""Market Desk lesson videos: hand-written lessons on protecting capital, rendered in the market video's look.

  python lesson_video.py render lessons/<slug>.json      -> data/video/lessons/<slug>/ (video.mp4, thumbnail.png, meta.json)
  python lesson_video.py upload --dir <folder>          -> posts that folder's video (needs the YOUTUBE_* env vars)

A lesson file is a list of beats. Each beat is one slide and the narration spoken over it:
  {"type": "card",  "text": "...", "say": "..."}
  {"type": "text",  "title": "...", "body": "...", "say": "..."}
  {"type": "table", "title": "...", "cols": [...], "rows": [[...]], "note": "...", "say": "..."}
  {"type": "chart", "sym": "^GSPC", "start": "YYYY-MM-DD", "end": "YYYY-MM-DD", "title": "...",
   "shade": [[start, end]], "marks": [[date, label]], "levels": [[price, label]], "ma": [50, 200],
   "facts": [[label, value, "good"|"bad"|""]], "say": "..."}
Rendering runs locally (charts fetch prices with yfinance); the upload runs in CI, where the channel's token lives.
"""
from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

import requests

import build
import market_video as MV
import video as V
from video import BRAND_BG, BRAND_DIM, BRAND_GOLD, BRAND_INK, FONT_BOLD, FONT_REG, FPS, H, W, brand_card, font, frame, log

LDIR = build.DATA / "video" / "lessons"
LABEL = "MARKET DESK LESSON"
COLS = {"good": MV.GOOD, "bad": MV.BAD, "gold": BRAND_GOLD, "": None, None: None}


# ──────────────────────────────────────────────────────────────────────── charts

def prices(sym: str, start: str, end: str, weekly: bool):
    import pandas as pd
    import yfinance as yf
    pad = pd.Timestamp(start) - pd.Timedelta(days=420)          # room for the 200-day line at the left edge
    df = yf.download(sym, start=str(pad.date()), end=end, progress=False, auto_adjust=False,
                     interval="1wk" if weekly else "1d")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df["Close"].dropna()


def draw_hist(img, box, c, start: str, ma: list[int], weekly: bool, shade=(), marks=(), levels=(), log_scale=False,
              dist=()):
    """Close as a line, moving averages, shaded stretches (declines), dated marks and labelled price levels."""
    from PIL import Image, ImageDraw
    x0, y0, x1, y1 = box
    d = ImageDraw.Draw(img)
    d.rectangle(box, fill=MV.CHART_BG)
    lines = {}
    for n in ma:
        k = max(1, round(n / 5)) if weekly else n                # 50/200-day = 10/40-week on weekly bars
        lines[n] = c.rolling(k).mean()
    c = c[c.index >= start]
    lines = {n: s[s.index >= start] for n, s in lines.items()}
    vals = list(c.values) + [p for p, _ in levels] + [x for ln in lines.values() for x in ln.values if not math.isnan(x)]
    lo, hi = min(vals), max(vals)
    f = (lambda p: math.log(p)) if log_scale else (lambda p: p)
    pad = (f(hi) - f(lo)) * 0.06
    flo, fhi = f(lo) - pad, f(hi) + pad
    axis_w, pt, pb = 150, y0 + 40, y1 - 50
    px0, px1 = x0 + 16, x1 - axis_w
    n = len(c)
    xs = lambda i: px0 + i * (px1 - px0) / max(1, n - 1)
    ys = lambda p: pt + (fhi - f(p)) / (fhi - flo) * (pb - pt)
    idx = {str(t.date()): i for i, t in enumerate(c.index)}

    def at(day: str) -> int:
        keys = sorted(idx)
        best = min(keys, key=lambda k: abs((datetime.fromisoformat(k) - datetime.fromisoformat(day)).days))
        return idx[best]

    if shade:
        over = Image.new("RGBA", img.size, (0, 0, 0, 0))
        od = ImageDraw.Draw(over)
        for a, b in shade:
            od.rectangle([xs(at(a)), pt, xs(at(b)), pb], fill=(255, 107, 107, 40))
        img.paste(Image.alpha_composite(img.convert("RGBA"), over).convert("RGB"))
        d = ImageDraw.Draw(img)
    lblf, small = font(FONT_REG, 26), font(FONT_BOLD, 28)
    # price grid
    for g in nice_levels(lo, hi, log_scale):
        if pt + 10 <= ys(g) <= pb - 10:
            d.line([(px0, ys(g)), (px1, ys(g))], fill=MV.GRID, width=1)
            d.text((px1 + 12, ys(g)), f"{g:,.0f}", font=lblf, fill=BRAND_DIM, anchor="lm")
    # time axis: years when the span is long, months otherwise
    span_days = (c.index[-1] - c.index[0]).days
    last = None
    for i, t in enumerate(c.index):
        key = t.year if span_days > 500 else (t.year, t.month)
        if key != last:
            last = key
            if i > 0 and (span_days < 3000 or t.year % 2 == 0):
                d.line([(xs(i), pt), (xs(i), pb)], fill=MV.GRID, width=1)
                d.text((xs(i), y1 - 12), str(t.year) if span_days > 500 else t.strftime("%b"), font=lblf,
                       fill=BRAND_DIM, anchor="ms")
    for nma, col in zip(ma, (MV.MA50, MV.MA200)):
        s = lines[nma]
        pts = [(xs(i), ys(v)) for i, v in enumerate(s.values) if not math.isnan(v)]
        if len(pts) > 1:
            d.line(pts, fill=col, width=3, joint="curve")
    d.line([(xs(i), ys(v)) for i, v in enumerate(c.values)], fill=BRAND_INK, width=4, joint="curve")
    for day in dist:                                             # distribution days: red ticks under the line
        i = at(day)
        d.rectangle([xs(i) - 4, pb - 34, xs(i) + 4, pb - 4], fill=MV.BAD)
    for p, label in levels:
        MV._dash(d, px0, ys(p), px1, ys(p), BRAND_GOLD, 3)
        d.text((px1 - 10, ys(p) - 8), label, font=small, fill=BRAND_GOLD, anchor="rb")
    for day, label in marks:
        i = at(day)
        v = c.values[i]
        r = 10
        d.ellipse([xs(i) - r, ys(v) - r, xs(i) + r, ys(v) + r], outline=BRAND_GOLD, width=4)
        tx = min(max(xs(i), px0 + 150), px1 - 150)
        ty = ys(v) - 26 if ys(v) - 60 > pt else ys(v) + 50
        d.text((tx, ty), label, font=small, fill=BRAND_GOLD, anchor="ms")
    legend = [("Close", BRAND_INK)] + [(f"{m}-day line" if not weekly else f"{round(m / 5)}-week line", col)
                                       for m, col in zip(ma, (MV.MA50, MV.MA200))]
    lx = px0 + 10
    for name, col in legend:
        d.line([(lx, pt + 14), (lx + 34, pt + 14)], fill=col, width=5)
        d.text((lx + 44, pt + 14), name, font=lblf, fill=BRAND_DIM, anchor="lm")
        lx += 64 + d.textlength(name, font=lblf) + 30
    return img


def nice_levels(lo: float, hi: float, log_scale: bool) -> list[float]:
    if log_scale:
        out, p = [], 10 ** math.floor(math.log10(lo))
        while p <= hi * 2:
            out += [p, 2 * p, 5 * p]
            p *= 10
        return out
    step = 10 ** math.floor(math.log10((hi - lo) / 5 or 1))
    for m in (1, 2, 5, 10):
        if (hi - lo) / (step * m) <= 7:
            step *= m
            break
    g = math.floor(lo / step) * step
    out = []
    while g <= hi + step:
        out.append(g)
        g += step
    return out


def slide_chart(b: dict, right: str):
    weekly = b.get("weekly", False)
    c = prices(b["sym"], b["start"], b["end"], weekly)
    img, d = frame(f"BEEHIVE WIRE  |  {LABEL}", right)
    wide = not b.get("facts")
    box = (40, 126, W - 40 if wide else 1390, H - 40)
    d.text((60, 140), b.get("title", ""), font=font(FONT_BOLD, 44), fill=BRAND_GOLD)
    img = draw_hist(img, (box[0], box[1] + 60, box[2], box[3]), c, b["start"], b.get("ma", [50, 200]), weekly,
                    b.get("shade", []), b.get("marks", []), b.get("levels", []), b.get("log", False), b.get("dist", []))
    if not wide:
        from PIL import ImageDraw
        MV.facts_panel(ImageDraw.Draw(img), 1430, 160, [(a, v, COLS.get(k)) for a, v, k in b["facts"]])
    return img


# ──────────────────────────────────────────────────────────────────────── render

def cmd_render(args) -> int:
    v = MV.cfg_mv()
    lesson = json.loads(Path(args.lesson).read_text(encoding="utf-8"))
    out = LDIR / lesson["slug"]
    work = out / "work"
    if out.exists():
        shutil.rmtree(out)
    work.mkdir(parents=True)
    right = lesson.get("series", "PROTECTING CAPITAL")
    engine = args.tts or v.get("tts", "kokoro")
    pad = float(v.get("pad_seconds", 0.7))
    used, clips, chapters, t = set(), [], [], 0.0
    for n, b in enumerate(lesson["beats"]):
        kind = b["type"]
        if kind == "card":
            img = brand_card(b["text"], LABEL, (W, H))
        elif kind == "text":
            img = slide_lines(b["title"], b["body"], right)
        elif kind == "table":
            img = MV.slide_table(b["title"], b["cols"], b["rows"], "LESSON", b.get("note", ""),
                                 [[COLS.get(k) for k in r] for r in b["colors"]] if b.get("colors") else None)
            img = relabel(img, right)
        elif kind == "chart":
            img = slide_chart(b, right)
        else:
            raise SystemExit(f"beat {n}: unknown type {kind}")
        png, mp3, mp4 = work / f"{n:02d}.png", work / f"{n:02d}.mp3", work / f"{n:02d}.mp4"
        img.save(png)
        used.add(V.speak(MV.spoken(b["say"]), mp3, v, engine))
        dur = V.duration(mp3) + pad
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-loop", "1", "-framerate", str(FPS), "-i", str(png),
                        "-i", str(mp3), "-af", f"apad=pad_dur={pad}", "-t", f"{dur:.3f}", "-c:v", "libx264",
                        "-tune", "stillimage", "-pix_fmt", "yuv420p", "-r", str(FPS), "-c:a", "aac", "-b:a", "160k",
                        "-ar", "44100", "-ac", "2", "-movflags", "+faststart", str(mp4)], check=True)
        clips.append(mp4)
        if b.get("chapter"):
            chapters.append({"t": t, "label": b["chapter"]})
        t += dur
    listing = work / "list.txt"
    listing.write_text("".join(f"file '{c.name}'\n" for c in clips), encoding="utf-8")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(listing),
                    "-c", "copy", "-movflags", "+faststart", str(out / "video.mp4")], check=True)
    brand_card(lesson["thumb"], LABEL).save(out / "thumbnail.png")
    if chapters and chapters[0]["t"] > 0:
        chapters.insert(0, {"t": 0, "label": "Intro"})
    desc = [lesson["description"], "", "CHAPTERS", *[f"{V.fmt_ts(c['t'])} {c['label']}" for c in chapters], "",
            "The Market Desk, every trading day: https://beehivewire.com/markets",
            "The front page, free, twice a day: https://beehivewire.com", "",
            "Education only, not financial advice. Past results are no guarantee of future returns.",
            "Prices are index closes on price (dividends not included). Not affiliated with Investor's Business Daily.",
            "Narrated by a synthetic voice."]
    meta = {"slug": lesson["slug"], "title": lesson["title"], "description": "\n".join(desc)[:4900],
            "tags": lesson.get("tags", [])[:15], "duration": t, "tts": sorted(used), "chapters": chapters}
    (out / "meta.json").write_text(json.dumps(meta, indent=1, ensure_ascii=False), encoding="utf-8")
    if not args.keep_work:
        for p in work.glob("*.mp*"):
            p.unlink()
    log(f"rendered {out / 'video.mp4'} ({V.fmt_ts(t)}, voice: {'+'.join(sorted(used))}): {lesson['title']}")
    return 0


def slide_lines(title: str, body: str, right: str):
    """A title and a body that keeps its line breaks (rules, lists); each line wraps on its own."""
    from video import wrap
    img, d = frame(f"BEEHIVE WIRE  |  {LABEL}", right)
    d.text((80, 170), title, font=font(FONT_BOLD, 84), fill=BRAND_GOLD)
    parts = body.split("\n")
    for size in range(72, 34, -4):
        f = font(FONT_BOLD, size)
        rows = [[ln for ln in wrap(d, part, f, W - 160)] for part in parts]
        need = sum(len(r) for r in rows) * size * 1.2 + (len(parts) - 1) * size * 0.5
        if need <= H - 360:
            break
    y = 330
    for r in rows:
        for ln in r:
            d.text((80, y), ln, font=f, fill=BRAND_INK)
            y += size * 1.2
        y += size * 0.5
    return img


def relabel(img, right: str):
    """MV's slides carry the daily video's banner; lessons get their own."""
    from PIL import ImageDraw
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 96], fill=BRAND_GOLD)
    d.text((64, 48), f"BEEHIVE WIRE  |  {LABEL}", font=font(FONT_BOLD, 44), fill=BRAND_BG, anchor="lm")
    d.text((W - 64, 48), right, font=font(FONT_BOLD, 36), fill=BRAND_BG, anchor="rm")
    return img


# ──────────────────────────────────────────────────────────────────────── upload

def cmd_upload(args) -> int:
    folder = Path(args.dir)
    meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
    video = folder / "video.mp4"
    tok = V.access_token()
    ch = V.my_channel(tok)
    if args.expect.lower() not in ch["title"].lower():
        raise SystemExit(f"refusing to upload: credentials post to '{ch['title']}', not '{args.expect}'")
    auth = {"Authorization": f"Bearer {tok}"}
    body = {"snippet": {"title": meta["title"], "description": meta["description"],
                        "tags": [t for t in meta["tags"] if t][:15], "categoryId": "27"},
            "status": {"privacyStatus": args.privacy, "selfDeclaredMadeForKids": False,
                       "containsSyntheticMedia": True}}
    init = requests.post("https://www.googleapis.com/upload/youtube/v3/videos",
                         params={"uploadType": "resumable", "part": "snippet,status"},
                         headers={**auth, "Content-Type": "application/json; charset=UTF-8",
                                  "X-Upload-Content-Type": "video/mp4", "X-Upload-Content-Length": str(video.stat().st_size)},
                         json=body, timeout=60)
    if init.status_code != 200:
        raise SystemExit(f"upload start failed ({init.status_code}): {init.text[:400]}")
    data = video.read_bytes()
    for attempt in range(4):
        up = requests.put(init.headers["Location"], data=data, headers={"Content-Type": "video/mp4"}, timeout=900)
        if up.status_code in (200, 201):
            break
        if up.status_code < 500:
            raise SystemExit(f"upload failed ({up.status_code}): {up.text[:400]}")
        time.sleep(5 * 2 ** attempt)
    else:
        raise SystemExit("upload failed after retries")
    vid = up.json()["id"]
    log(f"uploaded https://youtu.be/{vid} to {ch['title']} ({up.json().get('status', {}).get('privacyStatus')})")
    th = requests.post("https://www.googleapis.com/upload/youtube/v3/thumbnails/set", params={"videoId": vid},
                       headers={**auth, "Content-Type": "image/png"}, data=(folder / "thumbnail.png").read_bytes(), timeout=60)
    if th.status_code != 200:
        log(f"WARN thumbnail not set ({th.status_code}): {th.text[:200]}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["render", "upload"])
    ap.add_argument("lesson", nargs="?")
    ap.add_argument("--dir")
    ap.add_argument("--expect", default="beehive wire markets")
    ap.add_argument("--privacy", default="public")
    ap.add_argument("--tts", choices=["kokoro", "edge", "espeak", "sapi", "silent"])
    ap.add_argument("--keep-work", action="store_true")
    args = ap.parse_args()
    return cmd_render(args) if args.cmd == "render" else cmd_upload(args)


if __name__ == "__main__":
    raise SystemExit(main())
