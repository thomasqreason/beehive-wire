#!/usr/bin/env python3
"""
Beehive Wire — build the front page.

    python build.py                      # full run: fetch feeds -> Claude editor -> render site/index.html
    python build.py --dry-run            # no API key needed: heuristic picks, headlines = titles
    python build.py --render-only        # re-render the current page from data/state.json
    python build.py --candidates-json f  # use a prepared candidate list instead of fetching feeds
    python build.py --plan-json f        # use a prepared editor plan instead of calling Claude
    python build.py --force              # ignore quiet hours
    python build.py --emit-payload P     # fetch + select, write the editor payload, stop (subscription workflow)

The page is rolling: data/state.json holds what is on the page now; each run offers the editor fresh
candidates and asks for a few swaps. If anything fails, the current page is re-rendered unchanged.
"""
from __future__ import annotations

import argparse
import calendar
import concurrent.futures as cf
import hashlib
import html
import json
import os
import re
import shutil
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from zoneinfo import ZoneInfo

import yaml

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
SITE = ROOT / "site"
ARCHIVE = ROOT / "archive"  # every edition ever printed, committed so it survives each build
STATIC = ROOT / "static"   # icons, manifest, service worker — copied verbatim into site/
STATE_FILE = DATA / "state.json"
TOPICS = ["iran_mideast", "ukraine_russia", "world", "immigration", "crime", "trump_watch", "elections",
          "politics_culture_world", "media", "faith_family_schools", "military", "business_ai", "tech", "economy",
          "energy", "housing", "health", "utah_mormon", "sports", "pop_culture", "weather_disasters", "weird", "video"]

# ────────────────────────────────────────────────────────────────────────── utils

def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds") if dt else None


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def hours_since(s: str | None, now: datetime) -> float | None:
    dt = parse_iso(s)
    return round((now - dt).total_seconds() / 3600, 1) if dt else None


TRACKING = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "fbclid", "gclid", "ref", "ncid", "ito", "cmpid", "sfnsn"}


def canonical_url(url: str) -> str:
    try:
        p = urlparse(url.strip())
        q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if k.lower() not in TRACKING]
        return urlunparse((p.scheme.lower() or "https", p.netloc.lower(), p.path or "/", "", urlencode(q), ""))
    except Exception:
        return url.strip()


def story_id(url: str) -> str:
    return hashlib.sha1(canonical_url(url).encode()).hexdigest()[:10]


TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


def clean_text(s: str | None, limit: int = 320) -> str:
    if not s:
        return ""
    s = html.unescape(TAG_RE.sub(" ", s))
    s = WS_RE.sub(" ", s).strip()
    return (s[: limit - 1].rsplit(" ", 1)[0] + "…") if len(s) > limit else s


def norm_title(t: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", t.lower()).strip()


def finish_headline(h: str, fallback: str) -> str:
    h = (h or "").strip() or fallback
    h = WS_RE.sub(" ", h).upper()
    h = re.sub(r"[.…]+$", "", h).rstrip() + "..."
    return h[:120]


# ────────────────────────────────────────────────────────────────────────── config / state

def load_yaml(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log("WARN state.json unreadable; starting fresh")
    return {"updated": None, "top": None, "flash": [], "items": [], "seen": {}}


def save_state(state: dict) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=1, ensure_ascii=False), encoding="utf-8")


# ────────────────────────────────────────────────────────────────────────── fetch feeds

def fetch_feed(feed: dict, cfg: dict) -> list[dict]:
    import feedparser
    import requests

    out: list[dict] = []
    try:
        r = requests.get(
            feed["url"],
            timeout=cfg["fetch"]["timeout_seconds"],
            headers={"User-Agent": cfg["fetch"]["user_agent"], "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*"},
        )
        r.raise_for_status()
        d = feedparser.parse(r.content)
    except Exception as e:  # dead feed never breaks the build
        log(f"skip  {feed['name']}: {str(e)[:80]}")
        return out

    for e in d.entries[:60]:
        link = e.get("link") or ""
        title = clean_text(e.get("title"), 200)
        if not link or not title:
            continue
        source = feed["name"]
        if feed.get("google"):  # Google News items: "Headline - Outlet", plus <source>
            src = (e.get("source") or {}).get("title")
            if src:
                source = src
                if title.endswith(" - " + src):
                    title = title[: -(len(src) + 3)].strip()
            else:
                m = re.match(r"^(.*)\s-\s([^-]{2,40})$", title)
                if m:
                    title, source = m.group(1).strip(), m.group(2).strip()
        published = None
        for k in ("published_parsed", "updated_parsed"):
            tp = e.get(k)
            if tp:
                try:
                    published = datetime(*tp[:6], tzinfo=timezone.utc)
                    break
                except Exception:
                    pass
        image = None
        for m in (e.get("media_content") or []) + (e.get("media_thumbnail") or []):
            u = m.get("url")
            if u and re.search(r"\.(jpe?g|png|webp)(\?|$)", u, re.I) and "logo" not in u.lower():
                image = u
                break
        if not image:
            for enc in e.get("enclosures") or []:
                if str(enc.get("type", "")).startswith("image"):
                    image = enc.get("href") or enc.get("url")
                    break
        summary = clean_text(e.get("summary") or e.get("description") or "")
        if summary and norm_title(summary[: len(title)]) == norm_title(title):
            summary = ""  # many feeds repeat the title
        out.append(
            {
                "id": story_id(link),
                "url": canonical_url(link),
                "title": title,
                "summary": summary,
                "source": source,
                "published": iso(published),
                "image": image,
                "topic_hint": feed.get("topic", "politics_culture_world"),
                "weight": float(feed.get("weight", 1.0)),
            }
        )
    log(f"ok    {feed['name']}: {len(out)} items")
    return out


def fetch_all(feeds: list[dict], cfg: dict) -> list[dict]:
    items: list[dict] = []
    with cf.ThreadPoolExecutor(max_workers=cfg["fetch"]["workers"]) as ex:
        for res in ex.map(lambda f: fetch_feed(f, cfg), feeds):
            items.extend(res)
    return items


# ────────────────────────────────────────────────────────────────────────── candidates

def normalize_candidates(raw: list[dict], cfg: dict) -> list[dict]:
    """Accept feed items or hand-made research items; fill in ids, canonical urls, defaults."""
    out = []
    for r in raw:
        url = r.get("url") or r.get("link")
        title = clean_text(r.get("title"), 200)
        if not url or not title:
            continue
        out.append(
            {
                "id": r.get("id") or story_id(url),
                "url": canonical_url(url),
                "title": title,
                "summary": clean_text(r.get("summary") or "", 320),
                "source": (r.get("source") or "").strip() or urlparse(url).netloc,
                "published": _coerce_date(r.get("published")),
                "image": r.get("image") or None,
                "topic_hint": r.get("topic_hint") or r.get("topic") or "politics_culture_world",
                "weight": float(r.get("weight", 1.0)),
            }
        )
    return out


def _coerce_date(s) -> str | None:
    """Full ISO stays; a bare '2026-09-13' becomes noon UTC that day; anything else -> None."""
    if not isinstance(s, str):
        return None
    if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        return s + "T12:00:00+00:00"
    return iso(parse_iso(s)) if parse_iso(s) else None


def select_candidates(cands: list[dict], state: dict, cfg: dict, now: datetime, offer_all: bool) -> list[dict]:
    rolling = cfg["rolling"]
    on_page = {x["id"] for x in page_links(state)}
    seen = state.get("seen", {})
    window = rolling["candidate_window_hours"]

    fresh, seen_titles = [], set()
    for c in cands:
        if c["id"] in on_page:
            continue
        if not offer_all and c["id"] in seen:
            continue
        age = hours_since(c["published"], now)
        if age is not None and age > window:
            continue
        nt = norm_title(c["title"])
        if nt in seen_titles:
            continue
        seen_titles.add(nt)
        c["age_h"] = age if age is not None else 0.0
        fresh.append(c)

    # rank: weight first, then freshness; cap per source; cap total
    fresh.sort(key=lambda c: (-c["weight"], c["age_h"]))
    per_src: dict[str, int] = {}
    picked = []
    for c in fresh:
        n = per_src.get(c["source"], 0)
        if n >= rolling["max_per_source"]:
            continue
        per_src[c["source"]] = n + 1
        picked.append(c)
        if len(picked) >= rolling["max_candidates"]:
            break
    return picked


# ────────────────────────────────────────────────────────────────────────── page helpers

def page_links(state: dict) -> list[dict]:
    links = []
    if state.get("top"):
        links.append(state["top"])
    links.extend(state.get("flash") or [])
    links.extend(state.get("items") or [])
    return links


def age_out(state: dict, cfg: dict, now: datetime) -> dict:
    max_age = cfg["rolling"]["max_story_age_hours"]

    def fresh(it: dict) -> bool:
        ref = it.get("published") or it.get("first_seen")
        age = hours_since(ref, now)
        return age is None or age <= max_age

    state["items"] = [it for it in state.get("items", []) if fresh(it)]
    state["flash"] = [it for it in state.get("flash", []) if fresh(it)]
    if state.get("top") and not fresh(state["top"]):
        state["top"] = None
    return state


def _hm(s: str) -> int:
    h, m = (int(x) for x in str(s).split(":"))
    return h * 60 + m


def _clock(mins: int) -> str:
    h, m = divmod(mins, 60)
    ampm = "a.m." if h < 12 else "p.m."
    return f"{(h % 12) or 12}:{m:02d} {ampm}"


def _slots(cfg: dict) -> list[tuple[int, str]]:
    eds = cfg["site"].get("editions") or {"morning": "07:00", "evening": "18:00"}
    return [(_hm(eds["morning"]), "Morning Edition"), (_hm(eds["evening"]), "Evening Edition")]


def _reader_clock(local: datetime, slot_mins: int, cfg: dict) -> tuple[str, str]:
    """That slot's time on the readers' clock, and the label for it."""
    reader_tz = cfg["site"].get("reader_timezone")
    if not reader_tz:
        return _clock(slot_mins), ""
    mins = local.hour * 60 + local.minute
    nxt = local.replace(hour=slot_mins // 60, minute=slot_mins % 60, second=0, microsecond=0)
    if slot_mins <= mins:
        nxt += timedelta(days=1)
    shown = nxt.astimezone(ZoneInfo(reader_tz))
    return (f"{(shown.hour % 12) or 12}:{shown.minute:02d} {'a.m.' if shown.hour < 12 else 'p.m.'}",
            cfg["site"].get("reader_tz_label", "").strip())


def next_scheduled(local: datetime, cfg: dict) -> tuple[int, str]:
    """The next scheduled edition strictly ahead of now — not simply the other one."""
    mins = local.hour * 60 + local.minute
    ahead = sorted(_slots(cfg), key=lambda s: (s[0] - mins) % 1440)
    return ahead[0]


def edition_of(local: datetime, cfg: dict) -> tuple[str, str]:
    """Name this edition and say when the next one lands.

    Picked by whichever scheduled time is nearest, so a build that fires a few
    minutes early or late is still labelled the edition it actually is.
    """
    mins = local.hour * 60 + local.minute
    slots = _slots(cfg)
    grace = 45          # a run that fires a little early still belongs to the slot it is aiming at

    def since(t: int) -> int:
        """Minutes since that slot came round. Slightly negative if we have jumped the gun."""
        d = (mins - t) % 1440
        return d - 1440 if d >= 1440 - grace else d

    # An edition is named for the slot that has PASSED, not the nearest one: an evening
    # edition that GitHub delays until ten o'clock is a late evening edition, not an early
    # morning one.
    this_slot = min(slots, key=lambda s: since(s[0]))
    next_slot = [s for s in slots if s is not this_slot][0]
    when = "this evening" if next_slot[1].startswith("Evening") else "tomorrow morning"

    # Four in five American readers are Eastern or Central, so the sign-off quotes their
    # clock. The dateline above it stays Mountain: this is a wire published from Salt Lake.
    clock, label = _reader_clock(local, next_slot[0], cfg)
    return this_slot[1], f"Next edition {when} at {clock}{(' ' + label) if label else ''}"


def in_quiet_hours(cfg: dict, now: datetime) -> bool:
    q = cfg["rolling"].get("quiet_hours") or {}
    if not q.get("enabled"):
        return False
    local = now.astimezone(ZoneInfo(cfg["site"]["timezone"]))
    return q["start"] <= local.hour < q["end"]


# ────────────────────────────────────────────────────────────────────────── the editor

def compact_json(obj, level: int = 0) -> str:
    """Pretty JSON, except that every list of records goes one record per line.

    The editor reads the payload with a tool that shows about 2,000 lines at most. At one
    candidate per line a 220-candidate pool and a 75-link page fit in ~350 lines; pretty-printed
    they would run past the window and the editor would never see the tail of the wire."""
    pad = " " * level
    if isinstance(obj, dict):
        if not obj:
            return "{}"
        rows = [f"{pad} {json.dumps(k, ensure_ascii=False)}: {compact_json(v, level + 1)}" for k, v in obj.items()]
        return "{\n" + ",\n".join(rows) + f"\n{pad}}}"
    if isinstance(obj, list):
        if obj and all(isinstance(x, dict) for x in obj):
            return "[\n" + ",\n".join(f"{pad} " + json.dumps(x, ensure_ascii=False) for x in obj) + f"\n{pad}]"
        return json.dumps(obj, ensure_ascii=False)
    return json.dumps(obj, ensure_ascii=False)


def editor_payload(state: dict, cands: list[dict], cfg: dict, now: datetime) -> dict:
    def brief(it: dict) -> dict:
        return {
            "id": it["id"],
            "headline": it.get("headline"),
            "source": it.get("source"),
            "topic": it.get("topic"),
            "age_h": hours_since(it.get("published") or it.get("first_seen"), now),
        }

    return {
        "now_utc": iso(now),
        "now_local": now.astimezone(ZoneInfo(cfg["site"]["timezone"])).strftime("%a %b %d %Y %I:%M %p %Z"),
        "rules": {
            "total_links": cfg["page"]["total_links"],
            "flash_lines": cfg["page"]["flash_lines"],
            "max_swaps": cfg["rolling"]["max_swaps_per_run"],
            "top_story_max_age_hours": cfg["rolling"]["top_story_max_age_hours"],
            "mix_targets": cfg.get("mix", {}),
            "topics": TOPICS,
        },
        "current_page": {
            "top": brief(state["top"]) if state.get("top") else None,
            "flash": [brief(x) for x in state.get("flash", [])],
            "items": [brief(x) for x in state.get("items", [])],
        },
        "candidates": [
            {"id": c["id"], "source": c["source"], "title": c["title"], "summary": (c["summary"] or "")[:220], "age_h": c["age_h"], "topic_hint": c["topic_hint"]}
            for c in cands
        ],
    }


def call_editor(editorial: str, payload: dict, cfg: dict) -> dict | None:
    import anthropic

    client = anthropic.Anthropic()
    ecfg = cfg["editor"]
    user = (
        "Update the page. Respond with the JSON object only.\n\n" + json.dumps(payload, ensure_ascii=False)
    )
    last_err = None
    for attempt in range(1, ecfg.get("retries", 2) + 2):
        try:
            resp = client.messages.create(
                model=ecfg["model"],
                max_tokens=ecfg["max_tokens"],
                temperature=ecfg.get("temperature", 0.7),
                system=[{"type": "text", "text": editorial, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": user}],
            )
            text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
            plan = parse_json(text)
            if plan and isinstance(plan.get("items"), list):
                usage = getattr(resp, "usage", None)
                if usage:
                    log(f"editor ok: in={usage.input_tokens} out={usage.output_tokens}")
                return plan
            last_err = "no valid JSON in response"
        except Exception as e:
            last_err = str(e)[:200]
        log(f"editor attempt {attempt} failed: {last_err}")
        time.sleep(3 * attempt)
    return None


def parse_json(text: str) -> dict | None:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
    return None


def heuristic_plan(state: dict, cands: list[dict], cfg: dict) -> dict:
    """No-API fallback: round-robin over topics by weight/freshness. Headlines are just titles."""
    total = cfg["page"]["total_links"]
    current = page_links(state)
    have = {x["id"] for x in current}
    by_topic: dict[str, list[dict]] = {t: [] for t in TOPICS}
    for c in cands:
        by_topic.setdefault(c["topic_hint"], []).append(c)
    picks: list[dict] = []
    need = max(0, total - len(current)) if current else total
    while len(picks) < need and any(by_topic.values()):
        for t in TOPICS:
            if by_topic.get(t):
                c = by_topic[t].pop(0)
                if c["id"] not in have:
                    picks.append({"id": c["id"], "headline": c["title"], "topic": t})
                    have.add(c["id"])
            if len(picks) >= need:
                break
    items = [{"id": x["id"], "headline": x["headline"], "topic": x.get("topic")} for x in state.get("items", [])] + picks
    top = {"id": state["top"]["id"], "headline": state["top"]["headline"], "urgent": False} if state.get("top") else (
        {"id": items[0]["id"], "headline": items[0]["headline"], "urgent": False} if items else None
    )
    if top and items and items[0]["id"] == top["id"]:
        items = items[1:]
    flash = [{"id": x["id"], "headline": x["headline"]} for x in state.get("flash", [])]
    if not flash and len(items) > 3:
        flash, items = [{"id": x["id"], "headline": x["headline"]} for x in items[:cfg["page"]["flash_lines"]]], items[cfg["page"]["flash_lines"]:]
    return {"top": top, "flash": flash, "items": items, "notes": "heuristic fill"}


# ────────────────────────────────────────────────────────────────────────── apply plan

def apply_plan(plan: dict, state: dict, cands: list[dict], cfg: dict, now: datetime, cap_churn: bool = True) -> dict:
    total = cfg["page"]["total_links"]
    max_flash = cfg["page"]["flash_lines"]
    max_swaps = cfg["rolling"]["max_swaps_per_run"]

    current = {x["id"]: x for x in page_links(state)}
    pool = {c["id"]: c for c in cands}
    pool_all = {**pool, **current}

    def make(entry: dict, keep_headline: bool) -> dict | None:
        if not isinstance(entry, dict):
            return None
        sid = str(entry.get("id", ""))
        src = pool_all.get(sid)
        if not src:
            return None
        if sid in current and keep_headline:
            it = dict(current[sid])
            if entry.get("topic") in TOPICS and not it.get("topic"):
                it["topic"] = entry["topic"]
        else:
            it = {
                "id": sid,
                "url": src["url"],
                "title": src["title"],
                "source": src["source"],
                "published": src.get("published"),
                "image": src.get("image"),
                "first_seen": current[sid]["first_seen"] if sid in current else iso(now),
                "topic": entry.get("topic") if entry.get("topic") in TOPICS else src.get("topic_hint") or src.get("topic"),
                "headline": finish_headline(entry.get("headline", ""), src["title"]),
            }
        return it

    used: set[str] = set()
    used_urls: set[str] = set()

    def take(it: dict | None) -> dict | None:
        if not it or it["id"] in used or it["url"] in used_urls:
            return None
        used.add(it["id"])
        used_urls.add(it["url"])
        return it

    # top
    top = take(make(plan.get("top") or {}, keep_headline=True))
    if top and isinstance(plan.get("top"), dict):
        top["urgent"] = bool(plan["top"].get("urgent"))
        if top["id"] not in current or plan["top"].get("headline"):
            # a re-picked top may get a new giant headline even if it was a column item before
            top["headline"] = finish_headline(plan["top"].get("headline", ""), top["title"])
    if not top:
        top = take(dict(state["top"])) if state.get("top") else None

    # flash
    flash = []
    for f in plan.get("flash") or []:
        it = take(make(f, keep_headline=True))
        if it:
            if it["id"] not in current and isinstance(f, dict) and f.get("headline"):
                it["headline"] = finish_headline(f["headline"], it["title"])
            flash.append(it)
        if len(flash) >= max_flash:
            break

    # columns
    items = []
    room = total - (1 if top else 0) - len(flash)
    for e in plan.get("items") or []:
        it = take(make(e, keep_headline=True))
        if it:
            items.append(it)
        if len(items) >= room:
            break

    # soft cap on churn (skip on first fill, and when an operator supplied the plan by hand)
    if current and cap_churn:
        adds = [it for it in items if it["id"] not in current]
        shortfall = max(0, room - len(state.get("items", [])))  # empty slots (aged-out stories) may always be filled
        allowed = max_swaps + 3 + shortfall
        if len(adds) > allowed:
            log(f"editor over-swapped ({len(adds)} adds); trimming to {allowed}")
            keep_add = {a["id"] for a in adds[:allowed]}
            items = [it for it in items if it["id"] in current or it["id"] in keep_add]
            dropped = [x for x in state.get("items", []) if x["id"] not in used]
            for x in dropped:
                if len(items) >= room:
                    break
                if take(x):
                    items.append(x)

    # top-up from current page then candidates if the editor came up short
    if len(items) < room:
        for x in state.get("items", []):
            if len(items) >= room:
                break
            y = take(x)
            if y:
                items.append(y)
    if len(items) < room and not current:
        for c in sorted(cands, key=lambda c: (-c["weight"], c["age_h"])):
            if len(items) >= room:
                break
            y = take(make({"id": c["id"], "headline": c["title"], "topic": c["topic_hint"]}, keep_headline=False))
            if y:
                items.append(y)

    seen = {k: v for k, v in (state.get("seen") or {}).items() if (hours_since(v, now) or 0) <= 72}
    for c in cands:
        seen.setdefault(c["id"], iso(now))

    new_state = {
        "updated": iso(now),
        "top": top,
        "flash": flash,
        "items": items,
        "seen": seen,
        "notes": str(plan.get("notes", ""))[:300],
    }
    return new_state


def fetch_og_image(url: str, cfg: dict) -> str | None:
    try:
        import requests

        r = requests.get(url, timeout=8, headers={"User-Agent": cfg["fetch"]["user_agent"]})
        m = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)', r.text, re.I) or re.search(
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', r.text, re.I
        )
        return html.unescape(m.group(1)) if m else None
    except Exception:
        return None


# ────────────────────────────────────────────────────────────────────────── render

def _page_html(state: dict, cfg: dict, now: datetime, root: str = "", archived: bool = False, edition_no: int = 0):
    """Render one edition. `root` is how many levels up the assets sit; archived pages
    carry a banner and skip the install prompt."""
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    env = Environment(loader=FileSystemLoader(ROOT / "templates"), autoescape=select_autoescape(["html"]))
    tpl = env.get_template("page.html")

    ncols = cfg["page"]["columns"]
    items = state.get("items") or []
    per = -(-len(items) // ncols) if items else 0

    # Held over or new this edition? A story's first_seen is stamped the edition it arrived, so
    # anything first seen before this edition's own stamp was on the previous page. The template
    # grays those out and the masthead counts the new ones.
    ref = parse_iso(state.get("updated")) or now

    def held(it: dict) -> bool:
        fs = parse_iso(it.get("first_seen"))
        return bool(fs) and fs < ref - timedelta(minutes=5)

    columns = []
    for i in range(ncols):
        col = [dict(x) for x in items[i * per : (i + 1) * per]]
        for j, it in enumerate(col):
            it["sep"] = j > 0 and it.get("topic") != col[j - 1].get("topic")
            it["held"] = held(it)
        columns.append(col)
    flash = [dict(x) for x in state.get("flash") or []]
    for f in flash:
        f["held"] = held(f)
    new_count = sum(1 for it in page_links(state) if not held(it))

    sources: dict[str, str] = {}
    for it in page_links(state):
        pr = urlparse(it["url"])
        sources.setdefault(it["source"], f"{pr.scheme}://{pr.netloc}/")

    local = (parse_iso(state.get("updated")) or now).astimezone(ZoneInfo(cfg["site"]["timezone"]))
    stamp = local.strftime("%a %b %d %Y · %I:%M %p %Z").replace(" 0", " ").upper()
    edition, next_edition = edition_of(local, cfg)
    since = "since last evening's edition" if edition.startswith("Morning") else "since the morning edition"
    if state.get("extra"):
        slot_mins, slot_name = next_scheduled(local, cfg)
        clock, label = _reader_clock(local, slot_mins, cfg)
        edition = "Extra"
        next_edition = (f"The regular {slot_name.split()[0].lower()} edition follows at "
                        f"{clock}{(' ' + label) if label else ''}")
    html_out = tpl.render(
        site=cfg["site"],
        updated_human=stamp,
        edition=edition,
        next_edition=next_edition,
        top=state.get("top"),
        flash=flash,
        columns=columns,
        count=len(page_links(state)),
        new_count=new_count,
        since=since,
        feed_count=len(_feed_list()),
        sources=[{"name": k, "url": v} for k, v in sorted(sources.items())],
        target=' target="_blank" rel="noopener"' if cfg["site"].get("open_links_in_new_tab") else "",
        root=root,
        archived=archived,
        base_url=_base_url(cfg),
        og_title=(state.get("top") or {}).get("headline") or cfg["site"]["name"],
        og_desc=f'{cfg["site"]["name"]} — {edition}. {cfg["site"].get("tagline", "")}'.strip(),
        edition_no=edition_no,
        extra=bool(state.get("extra")),
    )
    return html_out, local, edition


def _base_url(cfg: dict) -> str:
    d = (cfg["site"].get("domain") or "").strip().strip("/")
    return f"https://{d}/" if d else "/"


def write_sitemap(cfg: dict, entries: list[dict], now: datetime) -> None:
    """Every page we publish, so a crawler does not have to guess."""
    base, stamp = _base_url(cfg), now.strftime("%Y-%m-%d")
    urls = [(base, stamp, "always", "1.0"), (base + "archive/", stamp, "daily", "0.6"),
            (base + "sources/", stamp, "weekly", "0.3")]
    for e in entries:
        urls.append((f"{base}archive/{e['file']}", (e.get("iso") or stamp)[:10], "never", "0.4"))
    body = "\n".join(
        f"  <url><loc>{u}</loc><lastmod>{m}</lastmod>"
        f"<changefreq>{c}</changefreq><priority>{p}</priority></url>" for u, m, c, p in urls)
    (SITE / "sitemap.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' + body + "\n</urlset>\n",
        encoding="utf-8")


def _archive_index() -> list[dict]:
    try:
        return json.loads((ARCHIVE / "index.json").read_text(encoding="utf-8"))
    except Exception:
        return []


def _slot_of(edition: str) -> str:
    return "morning" if edition.lower().startswith("morning") else "evening"


def edition_number(local: datetime, edition: str, slot: str | None = None) -> int:
    """This edition's number in the run. Re-printing a slot keeps the number it already had."""
    idx = _archive_index()
    day, slot = local.strftime("%Y-%m-%d"), slot or _slot_of(edition)
    highest = max((int(e.get("no") or 0) for e in idx), default=0)
    for e in idx:
        if e.get("date") == day and e.get("slot") == slot and e.get("no"):
            return int(e["no"])
    return highest + 1


def _time_words(local: datetime) -> str:
    """7:04 a.m. — written out, not zero-padded, and platform-independent."""
    return f"{(local.hour % 12) or 12}:{local.minute:02d} {'a.m.' if local.hour < 12 else 'p.m.'}"


def archive_edition(html: str, state: dict, local: datetime, edition: str, no: int) -> None:
    """Keep this edition forever, exactly as it was printed."""
    ARCHIVE.mkdir(parents=True, exist_ok=True)
    slot = f"extra-{state.get('extra_seq', 1)}" if state.get("extra") else _slot_of(edition)
    day = local.strftime("%Y-%m-%d")
    fname = f"{day}-{slot}.html"
    (ARCHIVE / fname).write_text(html, encoding="utf-8")

    idx_file = ARCHIVE / "index.json"
    try:
        idx = json.loads(idx_file.read_text(encoding="utf-8"))
    except Exception:
        idx = []
    idx = [e for e in idx if e.get("file") != fname]      # a re-run replaces its own slot
    idx.append({
        "no": no, "file": fname, "date": day, "slot": slot, "edition": edition,
        "time": _time_words(local), "iso": local.isoformat(),
        "top": (state.get("top") or {}).get("headline", ""),
        "count": len(page_links(state)),
    })
    idx.sort(key=lambda e: (e["date"], 0 if e["slot"] == "morning" else 1))
    idx_file.write_text(json.dumps(idx, indent=1, ensure_ascii=False), encoding="utf-8")


BEAT_LABELS = {
    "iran_mideast": "Iran, Gaza and the Middle East", "ukraine_russia": "Ukraine and Russia", "world": "The world",
    "immigration": "Immigration", "crime": "Crime", "trump_watch": "Trump watch", "elections": "Midterms and elections",
    "politics_culture_world": "Washington and the culture war", "media": "Media", "faith_family_schools": "Faith, family, schools",
    "military": "Military and veterans", "business_ai": "AI and data centers", "tech": "Tech", "economy": "Economy",
    "energy": "Energy", "housing": "Housing", "health": "Health", "utah_mormon": "Utah and the Latter-day Saints",
    "sports": "Sports", "pop_culture": "Pop culture", "weather_disasters": "Weather and disasters",
    "weird": "Weird and wonderful", "video": "Video",
}


def _feed_list() -> list[dict]:
    try:
        return load_yaml(ROOT / "feeds.yaml").get("feeds", []) or []
    except Exception:
        return []


def publish_sources(cfg: dict) -> int:
    """The wire we read: every feed in feeds.yaml, grouped by beat, as a page a reader can check."""
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    feeds = _feed_list()
    groups: dict[str, dict] = {}
    for fd in feeds:
        topic = fd.get("topic") or "politics_culture_world"
        g = groups.setdefault(topic, {"label": BEAT_LABELS.get(topic, topic.replace("_", " ").title()),
                                      "outlets": [], "searches": []})
        name, url = str(fd.get("name", "")).strip(), str(fd.get("url", "")).strip()
        if fd.get("google") or name.lower().startswith("google news"):
            g["searches"].append(name.split(":", 1)[-1].strip() if ":" in name else name)
            continue
        pr = urlparse(url)
        if "youtube.com" in pr.netloc and "channel_id=" in url:
            home = "https://www.youtube.com/channel/" + url.split("channel_id=", 1)[1].split("&")[0]
        else:
            home = f"{pr.scheme}://{pr.netloc}/"
        g["outlets"].append({"name": name, "url": home})

    ordered = [groups[t] for t in TOPICS if t in groups] + [g for t, g in groups.items() if t not in TOPICS]
    for g in ordered:
        g["outlets"].sort(key=lambda o: o["name"].lower())

    out = SITE / "sources"
    out.mkdir(parents=True, exist_ok=True)
    env = Environment(loader=FileSystemLoader(ROOT / "templates"), autoescape=select_autoescape(["html"]))
    (out / "index.html").write_text(env.get_template("sources.html").render(
        site=cfg["site"], root="../", beats=ordered, total=len(feeds), base_url=_base_url(cfg),
        target=' target="_blank" rel="noopener"' if cfg["site"].get("open_links_in_new_tab") else "",
    ), encoding="utf-8")
    return len(feeds)


def publish_archive(cfg: dict) -> int:
    """Copy every archived edition into the site and print the calendar that indexes them."""
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    out = SITE / "archive"
    out.mkdir(parents=True, exist_ok=True)
    entries: list[dict] = []
    if ARCHIVE.is_dir():
        for f in sorted(ARCHIVE.glob("*.html")):
            shutil.copy2(f, out / f.name)
        try:
            entries = json.loads((ARCHIVE / "index.json").read_text(encoding="utf-8"))
        except Exception:
            entries = []

    by_day: dict[str, list[dict]] = {}
    for e in entries:
        by_day.setdefault(e["date"], []).append(e)

    months = []
    for ym in sorted({e["date"][:7] for e in entries}, reverse=True):
        y, m = int(ym[:4]), int(ym[5:7])
        weeks = []
        for week in calendar.Calendar(firstweekday=6).monthdatescalendar(y, m):
            row = []
            for d in week:
                if d.month != m:
                    row.append(None)
                    continue
                eds = sorted(by_day.get(d.isoformat(), []), key=lambda x: x.get("iso", ""))
                row.append({"day": d.day, "editions": [
                    {"abbr": "EXTRA" if str(e["slot"]).startswith("extra") else ("AM" if e["slot"] == "morning" else "PM"),
                     "extra": str(e["slot"]).startswith("extra"), "no": e.get("no"),
                     "time": e["time"], "file": e["file"], "top": e.get("top", "")} for e in eds]})
            weeks.append(row)
        months.append({"label": f"{calendar.month_name[m]} {y}", "weeks": weeks})

    first_human = ""
    if entries:
        d = entries[0]["date"]
        first_human = f"{calendar.month_name[int(d[5:7])]} {int(d[8:10])}, {d[:4]}"

    env = Environment(loader=FileSystemLoader(ROOT / "templates"), autoescape=select_autoescape(["html"]))
    (out / "index.html").write_text(env.get_template("archive.html").render(
        site=cfg["site"], root="../", months=months, total=len(entries),
        first_human=first_human, dow=["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"],
        base_url=_base_url(cfg),
        og_title=f'{cfg["site"]["name"]} — Archives',
        og_desc=f"Every edition since {first_human}. {len(entries)} in all." if entries
                else "Every edition, back to the first.",
    ), encoding="utf-8")
    return len(entries)


def stand_down(state: dict) -> bool:
    """A scheduled edition ends an EXTRA, whatever else it does or doesn't change.
    `last_extra_at` survives, because it is what holds the cooldown.
    Returns True if a siren was actually standing."""
    was = bool(state.pop("extra", None))
    state.pop("extra_seq", None)
    return was


def render(state: dict, cfg: dict, now: datetime) -> Path:
    probe = (parse_iso(state.get("updated")) or now).astimezone(ZoneInfo(cfg["site"]["timezone"]))
    no = edition_number(probe, edition_of(probe, cfg)[0],
                        f"extra-{state.get('extra_seq', 1)}" if state.get("extra") else None)
    html_out, local, edition = _page_html(state, cfg, now, edition_no=no)
    SITE.mkdir(parents=True, exist_ok=True)
    out = SITE / "index.html"
    out.write_text(html_out, encoding="utf-8")
    if cfg["site"].get("domain"):
        (SITE / "CNAME").write_text(cfg["site"]["domain"].strip() + "\n")
    (SITE / "robots.txt").write_text(
        f"User-agent: *\nAllow: /\n\nSitemap: {_base_url(cfg)}sitemap.xml\n")
    if STATIC.is_dir():                       # app icons, manifest, service worker
        for f in sorted(STATIC.iterdir()):
            if f.is_file():
                shutil.copy2(f, SITE / f.name)

    arc_html, _, _ = _page_html(state, cfg, now, root="../", archived=True, edition_no=no)
    archive_edition(arc_html, state, local, edition, no)
    n = publish_archive(cfg)
    publish_sources(cfg)
    write_sitemap(cfg, _archive_index(), now)

    log(f"rendered {out} — edition no. {no}, {len(page_links(state))} links; archive holds {n} editions")
    return out


# ────────────────────────────────────────────────────────────────────────── main

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="no API call; heuristic picks")
    ap.add_argument("--render-only", action="store_true")
    ap.add_argument("--candidates-json", type=Path)
    ap.add_argument("--plan-json", type=Path)
    ap.add_argument("--force", action="store_true", help="ignore quiet hours")
    ap.add_argument("--no-network", action="store_true", help="skip og:image lookups")
    ap.add_argument("--window-hours", type=float, help="override rolling.candidate_window_hours")
    ap.add_argument("--max-age-hours", type=float, help="override rolling.max_story_age_hours")
    ap.add_argument("--emit-payload", type=Path, help="fetch feeds, write the editor payload + candidate cache, then stop "
                                                      "(lets Claude Code do the editing, so runs bill to a Claude subscription)")
    args = ap.parse_args()

    cfg = load_yaml(ROOT / "config.yaml")
    if args.window_hours:
        cfg["rolling"]["candidate_window_hours"] = args.window_hours
    if args.max_age_hours:
        cfg["rolling"]["max_story_age_hours"] = args.max_age_hours
    if args.candidates_json:  # a prepared list is already curated: no per-source cap
        cfg["rolling"]["max_per_source"] = 10_000
        cfg["rolling"]["max_candidates"] = max(cfg["rolling"]["max_candidates"], 1_000)
    now = utcnow()
    state = load_state()

    if args.render_only:
        render(state, cfg, now)
        return 0

    if stand_down(state):        # anything past here is a scheduled edition
        save_state(state)        # persist it now, so an early exit still clears the siren

    state = age_out(state, cfg, now)
    page_empty = not state.get("top") and not state.get("items")

    if not args.force and not args.plan_json and not page_empty and in_quiet_hours(cfg, now):
        log("quiet hours — no editor call; re-rendering current page")
        render(state, cfg, now)
        return 0

    # candidates
    if args.candidates_json:
        raw = json.loads(args.candidates_json.read_text(encoding="utf-8"))
        offer_all = True
    else:
        feeds = load_yaml(ROOT / "feeds.yaml").get("feeds", [])
        raw = fetch_all(feeds, cfg)
        offer_all = False
    cands = select_candidates(normalize_candidates(raw, cfg), state, cfg, now, offer_all)
    log(f"{len(raw)} raw items -> {len(cands)} candidates; page has {len(page_links(state))} links")

    if not cands and not page_empty:
        log("no new candidates — re-rendering")
        render(state, cfg, now)
        return 0

    if args.emit_payload:
        out = args.emit_payload
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(compact_json(editor_payload(state, cands, cfg, now)), encoding="utf-8")
        cache = out.parent / "candidates.json"
        cache.write_text(json.dumps(cands, indent=1, ensure_ascii=False), encoding="utf-8")
        log(f"wrote {out} ({len(cands)} candidates) and {cache}")
        return 0

    # plan
    if args.plan_json:
        plan = json.loads(args.plan_json.read_text(encoding="utf-8"))
    elif args.dry_run or not os.environ.get("ANTHROPIC_API_KEY"):
        if not args.dry_run:
            log("ANTHROPIC_API_KEY not set — using heuristic fill")
        plan = heuristic_plan(state, cands, cfg)
    else:
        editorial = (ROOT / "editorial.md").read_text(encoding="utf-8")
        plan = call_editor(editorial, editor_payload(state, cands, cfg, now), cfg)
        if plan is None:
            log("editor failed — keeping current page")
            render(state, cfg, now)
            return 0

    new_state = apply_plan(plan, state, cands, cfg, now, cap_churn=not args.plan_json)

    # top image
    top = new_state.get("top")
    if top and not top.get("image") and not args.no_network:
        prev = state.get("top") or {}
        if prev.get("id") != top["id"]:
            top["image"] = fetch_og_image(top["url"], cfg)

    save_state(new_state)
    render(new_state, cfg, now)
    changed = {x["id"] for x in page_links(new_state)} - {x["id"] for x in page_links(state)}
    log(f"done: {len(changed)} new links; notes: {new_state.get('notes', '')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
