"""The Market Desk promo video (Oct 30, 2026).

A short, hand-scripted pitch for the paid Market Desk, built with the same cards, voice and ffmpeg steps as
the edition videos (video.py). The narration lives in PROMO below: it is a sales script, so it is edited by
hand here, not written by Claude at run time.

    python promo.py render [--tts kokoro|edge|espeak|silent]   -> data/video/out/video.mp4, thumbnail, meta
    python promo.py upload [--force]                            -> YouTube (same credentials as the editions)

.github/workflows/promo.yml runs both on demand. The video's id then goes in markets.yaml desk.gate.video_id
so the gate page shows it to people deciding whether to subscribe.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path

import video as V
from video import FONT_BOLD, FONT_REG, FPS, H, OUT, W, brand_card, duration, fit_block, font, frame, log, speak, wrap

KEY = "promo-market-desk-2026-10"
LABEL = "MARKET DESK"
URL = "beehivewire.com/markets"

# (card headline, narration). The first and last are title cards; the rest are story-style slides.
PROMO = [
    ("THE MARKET DESK\nLeaders, bases and breakouts.", 
     "This is the Market Desk at Beehive Wire. Leaders, bases and breakouts. Numbers first."),
    ("THE BEEHIVE 100\nand the BIG CAP 40",
     "Every week the desk rebuilds the Beehive 100: the hundred strongest growth stocks, ranked by relative strength, "
     "earnings growth, sales growth and the chart. And the Big Cap 40: the same score, for companies worth forty "
     "billion dollars or more. Always a full list."),
    ("BUY POINT. BUY RANGE.\nHOW FAR FROM THE PIVOT.",
     "Every name on every list carries its buy point, its buy range, and how far it sits from the pivot, "
     "re-quoted on every pass through the trading day."),
    ("STOCKS ON THE MOVE\nBREAKOUTS. BLUE DOTS.",
     "Stocks on the move. Breakouts this week. Names near a buy point. Blue dots, when a stock's relative strength "
     "line makes a new high. And a chart for any ticker you type: daily, weekly, monthly, and intraday."),
    ("FIVE TO TEN STOCK STORIES\nEVERY TRADING DAY",
     "Five to ten stock stories every trading day, written from the numbers. And a scorecard that keeps the desk "
     "honest about every call it makes."),
    ("$9.99 FOR YOUR FIRST 21 DAYS\nTHEN $49.99 A MONTH",
     "Try it for nine ninety-nine for your first twenty-one days. Then forty-nine ninety-nine a month. Cancel any time."),
    (f"{URL}\nLink in the description",
     "Beehive wire dot com, slash markets. The link is in the description."),
]

TITLE = "BEEHIVE WIRE: The Market Desk, $9.99 for your first 21 days"
DESCRIPTION = """Subscribe to the Market Desk: https://beehivewire.com/markets

Leaders, bases and breakouts. Numbers first.

- The Beehive 100: the hundred strongest growth stocks, rebuilt every week and re-quoted every pass
- The Big Cap 40: the same score for companies worth $40 billion or more
- Buy points, buy ranges and distance from the pivot on every name
- Stocks on the move, breakouts this week, names near a buy point, blue dots
- A chart for any ticker: daily, weekly, monthly, intraday
- Five to ten stock stories every trading day, and a scorecard on every call

$9.99 for your first 21 days, then $49.99 a month. Cancel any time.
https://beehivewire.com/markets

The front page, free, twice a day: https://beehivewire.com

Narrated by a synthetic voice. Nothing here is investment advice."""
TAGS = ["Beehive Wire", "Market Desk", "stocks", "growth stocks", "breakouts", "buy point", "relative strength", "CAN SLIM", "IBD 50", "stock lists"]


def slide_pitch(head: str, idx: int, total: int, wm: str):
    img, d = frame(f"BEEHIVE WIRE  |  {LABEL}", f"{idx} of {total}")
    y = 96 + 120
    for pi, part in enumerate(head.split("\n")):
        f, lines, size = fit_block(d, part, FONT_BOLD, W - 160, 300, 150, 70)
        for ln in lines:
            d.text((80, y), ln, font=f, fill=V.BRAND_GOLD if pi == 0 else V.BRAND_INK)
            y += size * 1.12
        y += 40
    d.text((80, H - 120), "Leaders, bases and breakouts. Numbers first.", font=font(FONT_REG, 40), fill=V.BRAND_DIM, anchor="ls")
    d.text((W - 80, H - 120), wm, font=font(FONT_BOLD, 34), fill=V.BRAND_GOLD, anchor="rs")
    return img


def cmd_render(args) -> int:
    v = V.cfg_video()
    wm = v.get("watermark", "beehivewire.com")
    work = OUT / "work"
    if OUT.exists():
        shutil.rmtree(OUT)
    work.mkdir(parents=True)
    beats = []
    n = len(PROMO)
    for i, (head, say) in enumerate(PROMO):
        img = brand_card(head, LABEL, (W, H)) if i in (0, n - 1) else slide_pitch(head, i, n - 2, wm)
        beats.append((img, say))
    engine = args.tts or v.get("tts", "edge")
    used, clips, t = set(), [], 0.0
    pad = float(v.get("pad_seconds", 0.7))
    for k, (img, say) in enumerate(beats):
        png, mp3, mp4 = work / f"{k:02d}.png", work / f"{k:02d}.mp3", work / f"{k:02d}.mp4"
        img.save(png)
        used.add(speak(say, mp3, v, engine))
        dur = duration(mp3) + pad
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-loop", "1", "-framerate", str(FPS), "-i", str(png),
                        "-i", str(mp3), "-af", f"apad=pad_dur={pad}", "-t", f"{dur:.3f}", "-c:v", "libx264",
                        "-tune", "stillimage", "-pix_fmt", "yuv420p", "-r", str(FPS), "-c:a", "aac", "-b:a", "160k",
                        "-ar", "44100", "-ac", "2", "-movflags", "+faststart", str(mp4)], check=True)
        clips.append(mp4)
        t += dur
    listing = work / "list.txt"
    listing.write_text("".join(f"file '{c.name}'\n" for c in clips), encoding="utf-8")
    out = OUT / "video.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(listing),
                    "-c", "copy", "-movflags", "+faststart", str(out)], check=True)
    brand_card("THE MARKET DESK\n$9.99 FIRST 21 DAYS", LABEL).save(OUT / "thumbnail.png")
    meta = {"key": KEY, "edition": {"edition": "Promo", "date": "2026-10-30"}, "title": TITLE, "description": DESCRIPTION,
            "tags": TAGS, "duration": t, "tts": sorted(used), "stories": []}
    (OUT / "meta.json").write_text(json.dumps(meta, indent=1, ensure_ascii=False), encoding="utf-8")
    if not args.keep_work:
        shutil.rmtree(work)
    log(f"rendered {out} ({V.fmt_ts(t)}, voice: {'+'.join(sorted(used))}): {TITLE}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["render", "upload"])
    ap.add_argument("--tts", choices=["kokoro", "edge", "espeak", "sapi", "silent"])
    ap.add_argument("--force", action="store_true", help="upload again even if the promo is already up (hides the earlier one)")
    ap.add_argument("--keep-work", action="store_true", help="render: keep the slide PNGs in data/video/out/work")
    args = ap.parse_args()
    if args.cmd == "render":
        return cmd_render(args)
    return V.cmd_upload(args)


if __name__ == "__main__":
    raise SystemExit(main())
