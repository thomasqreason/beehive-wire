#!/usr/bin/env python3
"""
Beehive Wire -> YouTube. A short video of each edition's top stories, twice a day.

    python video.py due                                  # is there an edition that has no video yet?
    python video.py --emit-payload data/video/payload.json   # pick the stories Claude will script
    python video.py render --script data/video/script.json   # slides + voice + ffmpeg -> data/video/out/
    python video.py render --fallback                        # no Claude script: read the headlines straight
    python video.py upload                               # push out/video.mp4 to YouTube, record it
    python video.py whoami                               # which YouTube channel do the credentials post to?

Same three-step shape as build.py: Python picks the stories, Claude (video_editorial.md) writes the
narration, Python renders and publishes. If the Claude step fails, `render --fallback` reads the front-page
headlines and their sources aloud, so a slot is never skipped over a bad script.

Needs ffmpeg on the PATH, Pillow, and a text-to-speech engine: Kokoro (default, runs locally), edge-tts or espeak-ng.
Upload credentials come from environment variables -- see README "The YouTube video".
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

import build

ROOT = Path(__file__).resolve().parent
VDIR = build.DATA / "video"
OUT = VDIR / "out"
PUBLISHED = VDIR / "published.json"
W, H = 1920, 1080
FPS = 30

RED, BLACK, WHITE, GREY = (192, 0, 0), (0, 0, 0), (255, 255, 255), (110, 110, 110)

FONT_BOLD = [
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
]
FONT_REG = [
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/Library/Fonts/Arial.ttf",
    "C:/Windows/Fonts/arial.ttf",
]


def log(msg: str) -> None:
    print(f"[video] {msg}", flush=True)


def cfg_video() -> dict:
    cfg = build.load_yaml(build.ROOT / "config.yaml")
    v = dict(cfg.get("video") or {})
    v["_site"] = cfg["site"]
    return v


def set_output(**kv: str) -> None:
    for k, val in kv.items():
        print(f"{k}={val}")
    gh = os.environ.get("GITHUB_OUTPUT")
    if gh:
        with open(gh, "a", encoding="utf-8") as f:
            for k, val in kv.items():
                f.write(f"{k}={val}\n")


# ──────────────────────────────────────────────────────────────────────── which edition, which stories

def published() -> list[dict]:
    try:
        return json.loads(PUBLISHED.read_text(encoding="utf-8"))
    except Exception:
        return []


def latest_edition() -> dict | None:
    """The newest regular (morning/evening) edition in the archive index. Siren extras don't get a video."""
    regular = [e for e in build._archive_index() if e.get("slot") in ("morning", "evening")]
    return regular[-1] if regular else None


def key_of(ed: dict) -> str:
    return f"{ed['date']}-{ed['slot']}"


def cmd_due(args) -> int:
    v = cfg_video()
    ed = latest_edition()
    if not ed:
        set_output(go="no", reason="no edition in the archive")
        return 0
    if not args.dry_run and not os.environ.get("YOUTUBE_REFRESH_TOKEN"):
        set_output(go="no", key=key_of(ed), reason="YouTube credentials are not set up yet (see README); nothing to upload to")
        return 0
    if args.force:
        set_output(go="yes", key=key_of(ed), reason="forced")
        return 0
    if any(p.get("key") == key_of(ed) for p in published()):
        set_output(go="no", key=key_of(ed), reason="already has a video")
        return 0
    when = build.parse_iso(ed.get("iso"))
    max_age = float(v.get("max_edition_age_hours", 10))
    if when and (build.utcnow() - when).total_seconds() > max_age * 3600:
        set_output(go="no", key=key_of(ed), reason=f"edition is more than {max_age:g}h old; skipped, not backfilled")
        return 0
    set_output(go="yes", key=key_of(ed), reason="new edition")
    return 0


def pick_stories(state: dict, n: int) -> list[dict]:
    """Top story, the flash lines, then the page's next links in order, one per topic until n are chosen.

    The front page is already ranked by the editor, so rank is the signal. A cap per topic keeps one war or one
    beat from filling the whole video. Anything without a headline is skipped.
    """
    seq = [state.get("top")] + list(state.get("flash") or []) + list(state.get("items") or [])
    seq = [s for s in seq if s and s.get("headline") and s.get("title")]
    per_topic_cap = 2
    chosen, count = [], {}
    for s in seq:
        t = s.get("topic") or "other"
        if count.get(t, 0) >= per_topic_cap:
            continue
        count[t] = count.get(t, 0) + 1
        chosen.append(s)
        if len(chosen) >= n:
            break
    return chosen


def story_view(s: dict) -> dict:
    return {k: s.get(k) for k in ("id", "headline", "title", "source", "topic", "published", "url")}


META_RE = re.compile(r'<meta[^>]+(?:property|name)=["\'](?:og:description|description)["\'][^>]*>', re.I)
CONTENT_RE = re.compile(r'content=["\']([^"\']+)["\']', re.I)
P_RE = re.compile(r"<p[^>]*>(.*?)</p>", re.I | re.S)


def extract_context(page: str, limit: int = 1300) -> str:
    """The description plus the first real paragraphs of an article page: enough to say more than the headline does."""
    bits = []
    m = META_RE.search(page)
    if m and (c := CONTENT_RE.search(m.group(0))):
        bits.append(build.clean_text(c.group(1), 400))
    page = re.sub(r"<(script|style|nav|header|footer|aside)\b.*?</\1>", " ", page, flags=re.I | re.S)
    for para in P_RE.findall(page):
        t = build.clean_text(para, 500)
        if len(t) >= 80 and t not in bits:
            bits.append(t)
        if sum(map(len, bits)) >= limit:
            break
    return " ".join(bits)[:limit]


def fetch_context(url: str, ua: str) -> str:
    """Best effort. Google News links are redirect pages with no article behind them, so they get nothing."""
    if not url or "news.google.com" in url:
        return ""
    try:
        r = requests.get(url, headers={"User-Agent": ua}, timeout=10)
        if r.status_code == 200 and "html" in r.headers.get("content-type", ""):
            return extract_context(r.text)
    except requests.RequestException:
        pass
    return ""


def cmd_payload(args) -> int:
    v = cfg_video()
    ed = latest_edition()
    if not ed:
        log("no edition to make a video of")
        return 1
    state = build.load_state()
    stories = pick_stories(state, int(v.get("stories", 8)))
    if not stories:
        log("front page is empty")
        return 1
    ua = build.load_yaml(build.ROOT / "config.yaml")["fetch"]["user_agent"]
    views = [story_view(s) for s in stories]
    with build.cf.ThreadPoolExecutor(8) as ex:
        for view, ctx in zip(views, ex.map(lambda x: fetch_context(x["url"], ua), views)):
            view["context"] = ctx
    log(f"context found for {sum(1 for x in views if x['context'])} of {len(views)} stories")
    payload = {
        "channel": v.get("channel_name", "Boring Money"),
        "edition": {k: ed.get(k) for k in ("no", "date", "slot", "edition", "time")},
        "weekday": datetime.strptime(ed["date"], "%Y-%m-%d").strftime("%A, %B %d").replace(" 0", " "),
        "target_words": v.get("target_words", [350, 520]),
        "stories": views,
    }
    path = Path(args.emit_payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8")
    log(f"payload: {len(stories)} stories for the {ed['edition']} of {ed['date']} -> {path}")
    return 0


# ──────────────────────────────────────────────────────────────────────── script

def clean_say(text: str) -> str:
    """Narration is spoken, so strip markup and anything a voice would read out loud as noise."""
    text = re.sub(r"https?://\S+", "", text or "")
    text = re.sub(r"[*_#`>]+", "", text)
    return re.sub(r"\s+", " ", text).strip()


def validate_script(raw: dict, payload: dict) -> dict:
    """Keep only what the payload allows. A story id Claude invented is dropped, never trusted."""
    by_id = {s["id"]: s for s in payload["stories"]}
    segs, seen = [], set()
    for seg in raw.get("segments") or []:
        sid = seg.get("id")
        say = clean_say(seg.get("narration", ""))
        if sid in by_id and sid not in seen and len(say.split()) >= 6:
            seen.add(sid)
            segs.append({"id": sid, "narration": " ".join(say.split()[:240])})
    if len(segs) < 3:
        raise ValueError(f"script has only {len(segs)} usable segments")
    return {
        "title": clean_say(raw.get("title", ""))[:95],
        "intro": clean_say(raw.get("intro", "")),
        "outro": clean_say(raw.get("outro", "")),
        "tags": [clean_say(t)[:30] for t in (raw.get("tags") or []) if clean_say(t)][:12],
        "segments": segs,
    }


def fallback_script(payload: dict) -> dict:
    """No Claude script: read each headline's sourced title. Plain, but every word is the publisher's."""
    ed = payload["edition"]
    segs = []
    for s in payload["stories"]:
        title = s["title"].rstrip(".")
        segs.append({"id": s["id"], "narration": f"{title}. That's from {s['source']}."})
    return {
        "title": "",
        "intro": f"This is the Beehive Wire {ed['edition']} for {payload['weekday']}, from beehivewire dot com.",
        "outro": "That's the front page. The full list, with links to every story, is at beehivewire dot com.",
        "tags": [],
        "segments": segs,
    }


def default_title(payload: dict, v: dict) -> str:
    """No title from Claude: fall back to the front page's own headline, which already has the bite."""
    ed = payload["edition"]
    top = payload["stories"][0]
    head = (top.get("headline") or top["title"]).strip().rstrip(". ")
    head = head if len(head) <= 60 else head[:57].rsplit(" ", 1)[0] + "..."
    when = "Morning" if ed["slot"] == "morning" else "Evening"
    return f"{head} | {when} Wire, {datetime.strptime(ed['date'], '%Y-%m-%d').strftime('%b %d').replace(' 0', ' ')}"[:95]


# ──────────────────────────────────────────────────────────────────────── slides

def font(paths: list[str], size: int):
    from PIL import ImageFont
    for p in paths:
        if Path(p).exists():
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def wrap(draw, text: str, fnt, max_w: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        trial = f"{cur} {w}".strip()
        if draw.textlength(trial, font=fnt) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def fit_block(draw, text: str, paths: list[str], max_w: int, max_h: int, start: int, floor: int = 40):
    """Largest font at which the wrapped text fits the box."""
    for size in range(start, floor - 1, -4):
        f = font(paths, size)
        lines = wrap(draw, text, f, max_w)
        if len(lines) * size * 1.12 <= max_h:
            return f, lines, size
    f = font(paths, floor)
    return f, wrap(draw, text, f, max_w), floor


def frame(banner: str, right: str = ""):
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (W, H), WHITE)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 96], fill=RED)
    d.text((64, 48), banner, font=font(FONT_BOLD, 44), fill=WHITE, anchor="lm")
    if right:
        d.text((W - 64, 48), right, font=font(FONT_BOLD, 36), fill=WHITE, anchor="rm")
    d.rectangle([0, H - 12, W, H], fill=BLACK)
    return img, d


def slide_story(s: dict, idx: int, total: int, ed_label: str, wm: str) -> "Image":
    img, d = frame(f"BEEHIVE WIRE  |  {ed_label}", f"{idx} of {total}")
    f, lines, size = fit_block(d, s["headline"], FONT_BOLD, W - 160, 640, 190, 70)
    block = len(lines) * size * 1.12
    y = 96 + (H - 96 - 150 - block) / 2 + 20
    for ln in lines:
        d.text((80, y), ln, font=f, fill=BLACK)
        y += size * 1.12
    d.text((80, H - 120), f"Source: {s['source']}", font=font(FONT_REG, 40), fill=GREY, anchor="ls")
    d.text((W - 80, H - 120), wm, font=font(FONT_BOLD, 34), fill=RED, anchor="rs")
    return img


def slide_card(title: str, sub: str, ed_label: str, wm: str) -> "Image":
    img, d = frame(f"BEEHIVE WIRE  |  {ed_label}")
    f, lines, size = fit_block(d, title, FONT_BOLD, W - 200, 420, 150, 70)
    y = 300
    for ln in lines:
        d.text((100, y), ln, font=f, fill=BLACK)
        y += size * 1.12
    d.rectangle([100, y + 20, 460, y + 32], fill=RED)
    fs, slines, ssize = fit_block(d, sub, FONT_REG, W - 200, 260, 56, 36)
    y += 70
    for ln in slines:
        d.text((100, y), ln, font=fs, fill=GREY)
        y += ssize * 1.2
    d.text((W - 80, H - 120), wm, font=font(FONT_BOLD, 34), fill=RED, anchor="rs")
    return img


def thumbnail(top: dict, ed_label: str, wm: str) -> "Image":
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (1280, 720), WHITE)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, 1280, 84], fill=RED)
    d.text((40, 42), f"BEEHIVE WIRE  |  {ed_label}", font=font(FONT_BOLD, 38), fill=WHITE, anchor="lm")
    f, lines, size = fit_block(d, top["headline"], FONT_BOLD, 1200, 460, 150, 70)
    y = 84 + (720 - 84 - 80 - len(lines) * size * 1.12) / 2
    for ln in lines:
        d.text((40, y), ln, font=f, fill=BLACK)
        y += size * 1.12
    d.rectangle([0, 700, 1280, 720], fill=BLACK)
    d.text((1240, 672), wm, font=font(FONT_BOLD, 34), fill=RED, anchor="rs")
    return img


# ──────────────────────────────────────────────────────────────────────── voice

def tts_edge(text: str, voice: str, rate: str, dest: Path) -> None:
    import edge_tts
    last = None
    for attempt in range(3):
        try:
            asyncio.run(edge_tts.Communicate(text, voice, rate=rate).save(str(dest)))
            if dest.exists() and dest.stat().st_size > 1000:
                return
        except Exception as e:  # network blips and Microsoft throttling
            last = e
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"edge-tts failed: {last}")


KOKORO_FILES = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/"
_kokoro = None


def kokoro_engine():
    """Load Kokoro once. Its two model files (about 350 MB) are fetched the first time and kept in KOKORO_DIR."""
    global _kokoro
    if _kokoro is None:
        from kokoro_onnx import Kokoro
        home = Path(os.environ.get("KOKORO_DIR") or Path.home() / ".cache" / "kokoro")
        home.mkdir(parents=True, exist_ok=True)
        files = [home / "kokoro-v1.0.onnx", home / "voices-v1.0.bin"]
        for f in files:
            if not f.exists():
                log(f"fetching {f.name}")
                part = f.with_suffix(".part")
                with requests.get(KOKORO_FILES + f.name, stream=True, timeout=120) as r:
                    r.raise_for_status()
                    with open(part, "wb") as fh:
                        for chunk in r.iter_content(1 << 20):
                            fh.write(chunk)
                part.replace(f)
        _kokoro = Kokoro(str(files[0]), str(files[1]))
    return _kokoro


def tts_kokoro(text: str, voice: str, speed: float, dest: Path) -> None:
    """A neural voice that runs on this machine: free, no key, no service that can refuse the call."""
    import soundfile as sf
    audio, rate = kokoro_engine().create(text, voice=voice, speed=speed, lang="en-us")
    wav = dest.with_suffix(".wav")
    sf.write(str(wav), audio, rate)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(wav), str(dest)], check=True)
    wav.unlink(missing_ok=True)


def tts_espeak(text: str, dest: Path) -> None:
    wav = dest.with_suffix(".wav")
    subprocess.run(["espeak-ng", "-v", "en-us", "-s", "160", "-w", str(wav), text], check=True)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(wav), str(dest)], check=True)


def tts_sapi(text: str, dest: Path, voice: str = "", rate: int = 1) -> None:
    """Windows' built-in voices. Local preview only: no network needed, but robotic next to edge-tts."""
    wav, txt = dest.with_suffix(".wav"), dest.with_suffix(".say.txt")
    txt.write_text(text, encoding="utf-8")
    pick = f"$s.SelectVoice('{voice}');" if voice else ""
    ps = ("Add-Type -AssemblyName System.Speech;"
          "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
          f"{pick}$s.Rate = {rate};$s.SetOutputToWaveFile('{wav}');"
          f"$s.Speak([IO.File]::ReadAllText('{txt}',[Text.Encoding]::UTF8));$s.Dispose()")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(wav), str(dest)], check=True)
    for f in (wav, txt):
        f.unlink(missing_ok=True)


def tts_silent(text: str, dest: Path) -> None:
    """Test mode only: silence sized to the word count, so the pipeline can be checked with no TTS engine."""
    secs = max(2.0, len(text.split()) / 2.6)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono",
                    "-t", f"{secs:.2f}", "-q:a", "9", str(dest)], check=True)


def speak(text: str, dest: Path, v: dict, engine: str) -> str:
    """Speak with the chosen engine. Kokoro falls back to edge-tts, and edge-tts to espeak-ng, so a slot
    still gets a video; meta.json records which voice actually spoke."""
    if engine == "silent":
        tts_silent(text, dest)
        return "silent"
    if engine == "kokoro":
        try:
            tts_kokoro(text, v.get("kokoro_voice", "af_heart"), float(v.get("kokoro_speed", 1.0)), dest)
            return "kokoro"
        except Exception as e:
            log(f"WARN kokoro failed: {e}")
            engine = "edge"
    if engine == "sapi":
        tts_sapi(text, dest, v.get("sapi_voice", ""), int(v.get("sapi_rate", 1)))
        return "sapi"
    if engine == "edge":
        try:
            tts_edge(text, v.get("voice", "en-US-AndrewNeural"), v.get("rate", "+0%"), dest)
            return "edge"
        except Exception as e:
            log(f"WARN {e}")
            if not shutil.which("espeak-ng"):
                if os.name != "nt":
                    raise
                log("falling back to the Windows voice")
                tts_sapi(text, dest, v.get("sapi_voice", ""), int(v.get("sapi_rate", 1)))
                return "sapi"
            log("falling back to espeak-ng")
    tts_espeak(text, dest)
    return "espeak"


def duration(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True, check=True).stdout.strip()
    return float(out)


# ──────────────────────────────────────────────────────────────────────── render

def short(t: str, n: int = 60) -> str:
    t = t.strip()
    return t if len(t) <= n else t[: n - 3].rsplit(" ", 1)[0] + "..."


def fmt_ts(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 60}:{sec % 60:02d}"


def cmd_render(args) -> int:
    v = cfg_video()
    payload = json.loads(Path(args.payload).read_text(encoding="utf-8"))
    ed = payload["edition"]
    wm = v.get("watermark", "beehivewire.com")
    ed_label = f"{ed['edition'].upper()}  |  {payload['weekday'].upper()}"

    script = None
    if not args.fallback and Path(args.script).exists() and Path(args.script).stat().st_size > 0:
        try:
            script = validate_script(json.loads(Path(args.script).read_text(encoding="utf-8")), payload)
            log(f"script ok: {len(script['segments'])} segments")
        except (ValueError, json.JSONDecodeError) as e:
            log(f"WARN script unusable ({e}); reading the headlines straight")
    if script is None:
        script = fallback_script(payload)
    by_id = {s["id"]: s for s in payload["stories"]}
    title = script["title"] or default_title(payload, v)

    work = OUT / "work"
    if OUT.exists():
        shutil.rmtree(OUT)
    work.mkdir(parents=True)

    # (kind, image, narration, story)
    beats = [("intro", slide_card(f"The {ed['edition']}", f"{payload['channel']}  -  {payload['weekday']}",
                                  ed_label, wm), script["intro"], None)]
    total = len(script["segments"])
    for i, seg in enumerate(script["segments"], 1):
        s = by_id[seg["id"]]
        beats.append(("story", slide_story(s, i, total, ed_label, wm), seg["narration"], s))
    beats.append(("outro", slide_card("The full front page", "beehivewire.com  -  two editions a day, nothing in between",
                                      ed_label, wm), script["outro"], None))
    beats = [b for b in beats if b[2]]

    engine = args.tts or v.get("tts", "edge")
    used, clips, chapters, t = set(), [], [], 0.0
    pad = float(v.get("pad_seconds", 0.7))
    for n, (kind, img, say, s) in enumerate(beats):
        png, mp3, mp4 = work / f"{n:02d}.png", work / f"{n:02d}.mp3", work / f"{n:02d}.mp4"
        img.save(png)
        used.add(speak(say, mp3, v, engine))
        dur = duration(mp3) + pad
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-loop", "1", "-framerate", str(FPS), "-i", str(png),
                        "-i", str(mp3), "-af", f"apad=pad_dur={pad}", "-t", f"{dur:.3f}", "-c:v", "libx264",
                        "-tune", "stillimage", "-pix_fmt", "yuv420p", "-r", str(FPS), "-c:a", "aac", "-b:a", "160k",
                        "-ar", "44100", "-ac", "2", "-movflags", "+faststart", str(mp4)], check=True)
        clips.append(mp4)
        chapters.append({"t": t, "kind": kind, "story": s})
        t += dur

    listing = work / "list.txt"
    listing.write_text("".join(f"file '{c.name}'\n" for c in clips), encoding="utf-8")
    video = OUT / "video.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(listing),
                    "-c", "copy", "-movflags", "+faststart", str(video)], check=True)
    thumbnail(payload["stories"][0], ed_label, wm).save(OUT / "thumbnail.png")

    # Chapters need a 0:00 start and at least three of them; the intro card supplies the first.
    lines = []
    for c in chapters:
        label = {"intro": "Intro", "outro": "The full front page"}.get(c["kind"]) or short(c["story"]["title"])
        lines.append(f"{fmt_ts(c['t'])} {label}")
    src = [f"- {c['story']['source']}: {c['story']['title']}" + (f"\n  {c['story']['url']}" if len(c['story']['url']) <= 200 else "")
           for c in chapters if c["story"]]
    description = "\n".join([
        "Every story, with links, on the full front page: https://beehivewire.com", "",
        f"The {ed['edition']} of Beehive Wire for {payload['weekday']}: the front page, read out in a few minutes.", "",
        "CHAPTERS", *lines, "", "STORIES AND SOURCES", *src, "",
        "Narrated by a synthetic voice. Headlines and summaries are drawn from the linked publishers' reporting; "
        "follow the links for the full story.",
    ])
    meta = {"key": key_of(ed), "edition": ed, "title": title, "description": description[:4900],
            "tags": (v.get("tags") or []) + script["tags"], "duration": t, "tts": sorted(used),
            "stories": [s["id"] for s in payload["stories"] if s["id"] in {x["id"] for x in script["segments"]}]}
    (OUT / "meta.json").write_text(json.dumps(meta, indent=1, ensure_ascii=False), encoding="utf-8")
    shutil.rmtree(work)
    log(f"rendered {video} ({fmt_ts(t)}, voice: {'+'.join(sorted(used))}): {title}")
    return 0


# ──────────────────────────────────────────────────────────────────────── YouTube

def access_token() -> str:
    need = ("YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN")
    missing = [k for k in need if not os.environ.get(k)]
    if missing:
        raise SystemExit(f"missing YouTube credentials: {', '.join(missing)} (see README, 'The YouTube video')")
    r = requests.post("https://oauth2.googleapis.com/token", data={
        "client_id": os.environ["YOUTUBE_CLIENT_ID"], "client_secret": os.environ["YOUTUBE_CLIENT_SECRET"],
        "refresh_token": os.environ["YOUTUBE_REFRESH_TOKEN"], "grant_type": "refresh_token"}, timeout=30)
    if r.status_code != 200:
        raise SystemExit(f"token refresh failed ({r.status_code}): {r.text[:300]}")
    return r.json()["access_token"]


def my_channel(tok: str) -> dict:
    r = requests.get("https://www.googleapis.com/youtube/v3/channels", params={"part": "snippet", "mine": "true"},
                     headers={"Authorization": f"Bearer {tok}"}, timeout=30)
    r.raise_for_status()
    items = r.json().get("items") or []
    if not items:
        raise SystemExit("these credentials have no YouTube channel")
    return {"id": items[0]["id"], "title": items[0]["snippet"]["title"]}


def cmd_whoami(args) -> int:
    ch = my_channel(access_token())
    log(f"credentials post to: {ch['title']} ({ch['id']})")
    return 0


def cmd_upload(args) -> int:
    v = cfg_video()
    meta = json.loads((OUT / "meta.json").read_text(encoding="utf-8"))
    video = OUT / "video.mp4"
    if not args.force and any(p.get("key") == meta["key"] for p in published()):
        log(f"{meta['key']} already uploaded; standing down")
        return 0
    tok = access_token()
    want = (v.get("expect_channel") or "").lower()
    ch = my_channel(tok)
    if want and want not in ch["title"].lower():
        raise SystemExit(f"refusing to upload: credentials post to '{ch['title']}', not '{v['expect_channel']}'")

    auth = {"Authorization": f"Bearer {tok}"}
    body = {
        "snippet": {"title": meta["title"], "description": meta["description"],
                    "tags": [t for t in meta["tags"] if t][:15], "categoryId": str(v.get("category_id", 25))},
        "status": {"privacyStatus": v.get("privacy", "public"), "selfDeclaredMadeForKids": False,
                   "containsSyntheticMedia": bool(v.get("synthetic_disclosure", False))},
    }
    init = requests.post("https://www.googleapis.com/upload/youtube/v3/videos",
                         params={"uploadType": "resumable", "part": "snippet,status"},
                         headers={**auth, "Content-Type": "application/json; charset=UTF-8",
                                  "X-Upload-Content-Type": "video/mp4",
                                  "X-Upload-Content-Length": str(video.stat().st_size)},
                         json=body, timeout=60)
    if init.status_code != 200:
        raise SystemExit(f"upload start failed ({init.status_code}): {init.text[:400]}")
    loc = init.headers["Location"]
    data = video.read_bytes()
    for attempt in range(4):
        up = requests.put(loc, data=data, headers={"Content-Type": "video/mp4"}, timeout=600)
        if up.status_code in (200, 201):
            break
        if up.status_code < 500:
            raise SystemExit(f"upload failed ({up.status_code}): {up.text[:400]}")
        time.sleep(5 * 2 ** attempt)
    else:
        raise SystemExit("upload failed after retries")
    vid = up.json()["id"]
    log(f"uploaded https://youtu.be/{vid} to {ch['title']} ({up.json().get('status', {}).get('privacyStatus')})")

    rec = {"key": meta["key"], "video_id": vid, "title": meta["title"], "channel": ch["title"],
           "uploaded": build.iso(build.utcnow())}
    PUBLISHED.parent.mkdir(parents=True, exist_ok=True)
    PUBLISHED.write_text(json.dumps(published() + [rec], indent=1), encoding="utf-8")

    th = requests.post("https://www.googleapis.com/upload/youtube/v3/thumbnails/set", params={"videoId": vid},
                       headers={**auth, "Content-Type": "image/png"}, data=(OUT / "thumbnail.png").read_bytes(), timeout=60)
    if th.status_code != 200:   # custom thumbnails need a verified channel; the video is up regardless
        log(f"WARN thumbnail not set ({th.status_code}): {th.text[:200]}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", nargs="?", choices=["due", "render", "upload", "whoami"])
    ap.add_argument("--emit-payload", metavar="PATH")
    ap.add_argument("--payload", default=str(VDIR / "payload.json"))
    ap.add_argument("--script", default=str(VDIR / "script.json"))
    ap.add_argument("--fallback", action="store_true", help="skip the Claude script; read the headlines straight")
    ap.add_argument("--tts", choices=["kokoro", "edge", "espeak", "sapi", "silent"], help="override config video.tts")
    ap.add_argument("--dry-run", action="store_true", help="due: render-only run, so no YouTube credentials needed")
    ap.add_argument("--force", action="store_true", help="due, upload: go even if this edition already has a video")
    args = ap.parse_args()
    if args.emit_payload:
        return cmd_payload(args)
    if not args.cmd:
        ap.error("give a command or --emit-payload")
    return {"due": cmd_due, "render": cmd_render, "upload": cmd_upload, "whoami": cmd_whoami}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
