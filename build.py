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
VIDEO_PUBLISHED = DATA / "video" / "published.json"  # what video.py recorded after each YouTube upload
SEEN_HOURS = 240   # how long state.json remembers a story it was offered (10 days; was 72h until Oct 20, 2026)
TOPICS = ["iran_mideast", "ukraine_russia", "world", "immigration", "crime", "trump_watch", "elections",
          "politics_culture_world", "media", "faith_family_schools", "military", "business_ai", "tech", "surveillance",
          "american_life", "economy", "investing", "energy", "housing", "health", "utah_mormon", "sports", "pop_culture",
          "weather_disasters", "weird", "video"]


def desk_candidates(cfg: dict, now: datetime) -> list[dict]:
    """The Market Desk's latest edition (data/markets/<date>.json) offered to the editor as candidates on the
    investing beat: each story and the New America profile, linking to the desk page's anchor."""
    try:
        files = sorted((DATA / "markets").glob("????-??-??.json"))
        if not files:
            return []
        ed = json.loads(files[-1].read_text(encoding="utf-8"))
        age = hours_since(ed.get("generated"), now)
        if age is None or age > 30:
            return []
        base = _base_url(cfg) + "markets/"
        out = []
        for s in ed.get("stories", []):
            a = s.get("article") or {}
            news = "; ".join(n.get("title", "") for n in (s.get("news") or [])[:4] if n.get("title"))
            summary = ((a.get("deck") or "") + (" NEWS: " + news if news else "")).strip()
            if a.get("headline"):
                out.append({"title": a["headline"], "url": f"{base}#{s['sym']}", "summary": summary,
                            "source": "Beehive Wire Market Desk", "published": ed.get("generated"), "topic": "investing", "weight": 1.5})
        p = ed.get("profile") or {}
        if (p.get("article") or {}).get("headline"):
            out.append({"title": "New America: " + p["article"]["headline"], "url": f"{base}#profile-{p['sym']}",
                        "summary": p["article"].get("deck") or "", "source": "Beehive Wire Market Desk",
                        "published": ed.get("generated"), "topic": "investing", "weight": 1.5})
        return out
    except Exception as e:  # noqa: BLE001
        log(f"desk candidates skipped ({e})")
        return []

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


URL_DATE_RE = re.compile(r"/(20\d{2})[/-](\d{1,2})[/-](\d{1,2})(?:/|-|$)")


def url_date(url: str) -> datetime | None:
    """The date a publisher put in its own URL path, e.g. /2026/08/25/.

    Costs nothing and it does not lie. A feed's timestamp does: an aggregator restamps a story with
    the moment IT saw the piece, which is how a months-old obituary reached the page dated today.
    When the publisher's own path disagrees with the feed, the path wins.
    """
    m = URL_DATE_RE.search(urlparse(url).path)
    if not m:
        return None
    try:
        y, mo, d = (int(x) for x in m.groups())
        return datetime(y, mo, d, 12, tzinfo=timezone.utc)
    except ValueError:
        return None


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

def blocked_source(source: str, topic: str, cfg: dict) -> bool:
    """config.yaml `blocklist`: outlets the editor never gets to see. Substring match on the source
    name, case-insensitive — globally (`sources`) or on one beat (`by_topic.<topic>`). The sports list
    is the TEGNA station domains, which Google News uses to label 'Locked On' podcast episodes."""
    bl = cfg.get("blocklist") or {}
    names = list(bl.get("sources") or []) + list((bl.get("by_topic") or {}).get(topic) or [])
    s = (source or "").lower()
    return any(str(n).lower() in s for n in names if n)


def fetch_feed(feed: dict, cfg: dict) -> list[dict]:
    import feedparser
    import requests

    out: list[dict] = []
    skipped = 0
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
        if blocked_source(source, feed.get("topic", ""), cfg):
            skipped += 1
            continue
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
    log(f"ok    {feed['name']}: {len(out)} items" + (f" ({skipped} blocklisted)" if skipped else ""))
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


# ───────────────────────────────────────────────────────────── a death runs once (Oct 27, 2026)
#
# `seen` remembers LINKS, so a death came back every time a new outlet ran its own copy: Jim Bakker
# led seven straight editions (Oct 20-26) from Christian Post, Fulton County News, BBC, IMDb,
# news-daily... each a "new" link about a death that was weeks old. The fix remembers PEOPLE: once
# an obituary has been on the page, no later story about that person reaches the editor for a year
# (columns, retrospectives, "who was he really", relatives' statements) — whatever the outlet or
# URL. config.yaml `spiked:` is the hand-kept list of names/phrases that never run again at all.

OBITS_FILE = DATA / "obits.json"
OBIT_DAYS = 365
_NAME = r"([A-Z][a-zA-Z'’.\-]+(?: [A-Z][a-zA-Z'’.\-]+){1,2})"
_DEATH = r"(?:dies|died|dead|has died|passes away|passed away|obituary|death of|RIP)"
_OBIT_PATTERNS = [
    re.compile(_NAME + r"(?:(?:,[^,]{0,160}){1,3},)?\s+(?i:dies|died|dead at|has died|passes away|passed away)\b"),
    re.compile(r"^(?:RIP|Obituary|OBITUARY)[:\s]+" + _NAME),
    re.compile(r"\bdeath of\s+" + _NAME),
]
_NOT_SURNAME = {"Times", "News", "Post", "Daily", "Star", "Dead", "Dies", "Press", "Club", "Show",
                "Family", "House", "Award", "Music", "Band", "Group", "World", "Sports", "Media",
                # surnames that are also places or so common they'd block unrelated stories
                # (Sass Jordan's death must not spike the country of Jordan; Clark is Caitlin Clark)
                "Jordan", "Georgia", "Washington", "Jackson", "Lincoln", "Houston", "Austin", "Kennedy",
                "Johnson", "Williams", "Brown", "Smith", "Jones", "Davis", "Miller", "Wilson", "Moore",
                "Taylor", "Thomas", "White", "Harris", "Martin", "Thompson", "Garcia", "Clark", "Lewis",
                "Young", "Allen", "Wright", "Scott", "Green", "Baker", "Adams", "Nelson", "Campbell",
                "Mitchell", "Roberts", "Carter", "Phillips", "Evans", "Turner", "Parker", "Collins",
                "Edwards", "Stewart", "Morris", "Murphy", "Rogers", "Morgan", "Cooper", "Peterson",
                "Bailey", "Howard", "Richardson", "Watson", "Brooks", "Bennett", "Hughes", "Price",
                "Sanders", "Foster", "Powell", "Russell", "Sullivan", "Trump", "Biden", "Paris", "Israel",
                "Christian", "Grace", "Church"}


def obit_names(title: str) -> list[str]:
    """Names an obituary headline is about ("Jim Bakker, televangelist ..., dies at 86")."""
    t = title or ""
    if not re.search(_DEATH, t, re.I):
        return []
    out = []
    for rx in _OBIT_PATTERNS:
        for m in rx.finditer(t):
            name = m.group(1).strip(" .,'’")
            words = name.split()
            # drop leading descriptors the regex swallowed ("Disgraced Televangelist Jim Bakker" -> keep last two)
            if len(words) == 3 and words[0].lower() in {"star", "icon", "legend", "rocker", "singer", "actor", "actress", "televangelist", "disgraced", "former", "veteran", "author", "artist", "pastor", "evangelist", "composer", "music"}:
                words = words[1:]
            if len(words) >= 2:
                out.append(" ".join(words))
    return list(dict.fromkeys(out))


def load_obits() -> dict:
    try:
        return json.loads(OBITS_FILE.read_text()) if OBITS_FILE.exists() else {}
    except Exception:
        return {}


def save_obits(obits: dict, now: datetime) -> None:
    keep = {k: v for k, v in obits.items() if (hours_since(v, now) or 0) <= OBIT_DAYS * 24}
    OBITS_FILE.write_text(json.dumps(keep, indent=1, sort_keys=True, ensure_ascii=False), encoding="utf-8")


def record_obits(items: list[dict], obits: dict, now: datetime) -> None:
    for it in items:
        for name in obit_names(it.get("title") or ""):
            obits.setdefault(name, iso(now))


def _person_patterns(name: str) -> list[re.Pattern]:
    pats = [re.compile(r"\b" + re.escape(name) + r"\b", re.I)]
    last = name.split()[-1]
    if len(last) >= 5 and last not in _NOT_SURNAME:
        pats.append(re.compile(r"\b" + re.escape(last) + r"(?:'s|’s)?\b"))   # surname, capitalised as written
    return pats


def spiked_reason(c: dict, obits: dict, cfg: dict) -> str | None:
    text = f"{c.get('title') or ''} {c.get('summary') or ''}"
    for term in cfg.get("spiked") or []:
        if term and re.search(r"\b" + re.escape(str(term)) + r"\b", text, re.I):
            return f"spiked term '{term}'"
    for name in obits:
        if any(p.search(c.get("title") or "") for p in _person_patterns(name)):
            return f"death of {name} already ran"
    return None


def select_candidates(cands: list[dict], state: dict, cfg: dict, now: datetime, offer_all: bool,
                      verify: bool = True) -> list[dict]:
    rolling = cfg["rolling"]
    on_page = {x["id"] for x in page_links(state)}
    seen = state.get("seen", {})
    window = rolling["candidate_window_hours"]

    obits = load_obits()
    record_obits(page_links(state), obits, now)        # anything on the page now counts as run
    fresh, seen_titles = [], set()
    for c in cands:
        if c["id"] in on_page:
            continue
        why = spiked_reason(c, obits, cfg)
        if why:
            log(f"spiked: {c['source']} — {c['title'][:60]} ({why})")
            continue
        if not offer_all and c["id"] in seen:
            continue
        age = hours_since(c["published"], now)
        if age is not None and age > window:
            continue
        # The publisher's own URL date overrules the feed's timestamp (see url_date): this is the
        # cheap half of the stale-story guard, and it needs no network call.
        real = url_date(c["url"])
        if real is not None:
            true_age = round((now - real).total_seconds() / 3600, 1)
            if true_age > window + 24:        # a day of slack for timezone and backdated paths
                log(f"stale: {c['source']} — {c['title'][:60]} is dated {real:%b %d} in its own URL")
                continue
            age = max(age or 0.0, true_age)
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
    if verify:
        save_obits(obits, now)
    if verify and picked:
        # The expensive half of the stale-story guard: every candidate's own page is asked when it
        # was published (see verify_dates). The feed's date decided nothing above but the window.
        cache = load_dates()
        n = len(picked)
        picked = drop_stale(picked, window + 24, cfg, now, cache, "candidate")
        save_dates(cache, now)
        if len(picked) < n:
            log(f"dates: {n - len(picked)} candidate(s) the feeds called new were old; {len(picked)} remain")
    return picked


# ───────────────────────────────────────────────────────────── story stacks (clustering)

# Words that link two stories only by accident. Everything here is either grammar, a word every
# news title uses, or a name so common on this page (TRUMP, ISRAEL) that sharing it means nothing.
STACK_STOP = {
    "about", "after", "again", "against", "ahead", "alleged", "also", "amid", "among", "announce",
    "announced", "announces", "another", "around", "back", "because", "been", "before", "being",
    "between", "biden", "biggest", "billion", "break", "breaking", "called", "calls", "claim",
    "claims", "come", "comes", "coming", "could", "court", "data", "deal", "december", "despite",
    "does", "done", "down", "during", "each", "early", "every", "exclusive", "expected", "faces",
    "first", "following", "former", "found", "from", "full", "gets", "going", "government", "group",
    "have", "here", "high", "higher", "house", "huge", "into", "investigation", "january", "just",
    "keep", "known", "large", "last", "late", "later", "launch", "leader", "less", "like", "likely",
    "little", "long", "longer", "look", "looking", "major", "make", "makes", "making", "many",
    "march", "massive", "many", "million", "more", "most", "move", "much", "must", "near", "need",
    "never", "news", "next", "night", "northern", "note", "november", "october", "offer", "official",
    "officials", "only", "open", "order", "other", "others", "over", "part", "past", "people",
    "percent", "plan", "plans", "possible", "president", "press", "public", "push", "really",
    "record", "released", "report", "reportedly", "reports", "revealed", "reveals", "right",
    "rises", "said", "says", "second", "sees", "september", "set", "several", "share", "should",
    "show", "shows", "since", "some", "southern", "start", "state", "states", "still", "story",
    "support", "take", "takes", "talks", "tells", "than", "that", "their", "them", "then", "there",
    "these", "they", "thing", "things", "think", "third", "this", "those", "three", "through",
    "time", "times", "today", "told", "took", "trump", "under", "united", "until", "update",
    "video", "warn", "warns", "watch", "week", "weeks", "were", "what", "when", "where", "which",
    "while", "will", "with", "within", "without", "work", "world", "would", "year", "years",
    "american", "america", "washington", "white", "york", "video", "live", "latest", "after",
    # verbs a headline uses whoever the story is about
    "accused", "arrested", "backs", "bans", "blasts", "blocks", "boost", "charged", "cuts",
    "dies", "drops", "ends", "faces", "fight", "fights", "hits", "jumps", "kills", "leaves",
    "loses", "names", "opens", "picks", "quits", "raises", "rejects", "rips", "searched",
    "seeks", "sends", "signs", "slams", "slashing", "sues", "unveils", "urges", "vows", "wins",
}

# Names that scope a BEAT rather than a story. Two Utah stories share "Utah" the way two sports
# stories share "game" — it is the section, not the subject. Left out of the linking signal so a
# stack is built on what actually happened (HOUTHI, GUILFOYLE, CORNELL), not on where.
STACK_THEME_NAMES = {
    "africa", "afghan", "afghanistan", "arabia", "asia", "australia", "brazil", "britain",
    "british", "california", "canada", "chinese", "china", "congress", "democrat", "democrats",
    "egypt", "england", "europe", "european", "florida", "france", "french", "gaza", "german",
    "germany", "india", "indian", "iran", "iranian", "iraq", "israel", "israeli", "italy",
    "japan", "japanese", "jerusalem", "korea", "kremlin", "lebanon", "london", "mexico",
    "michigan", "moscow", "nato", "ohio", "ottawa", "pakistan", "palestine", "palestinian",
    "paris", "pennsylvania", "qatar", "republican", "republicans", "russia", "russian", "saudi",
    "senate", "spain", "supreme", "syria", "taiwan", "tehran", "texas", "turkey", "ukraine",
    "ukrainian", "utah", "venezuela", "virginia", "yemen",
}

_STACK_WORD = re.compile(r"[A-Za-z][A-Za-z'\-]{2,}")


def _stack_tokens(text: str) -> tuple[set[str], set[str], set[str]]:
    """(all distinctive tokens, tokens capitalized mid-title, tokens whose casing was judgeable).

    The second set is the proper-noun vote, and it is only meaningful when the title's casing
    carries information. ALL-CAPS and Title Case titles capitalize everything, so they vote on
    nothing — otherwise every word in a Title Case feed would read as a name.
    """
    words = _STACK_WORD.findall(text or "")
    keep, caps = set(), set()
    big = [w for w in words if len(w) >= 4]
    if big:
        cap_share = sum(1 for w in big if w[0].isupper()) / len(big)
    else:
        cap_share = 0.0
    informative = cap_share < 0.65          # not ALL CAPS, not Title Case
    judged = set()
    for i, w in enumerate(words):
        lw = w.lower().strip("'-")
        if len(lw) < 4 or lw in STACK_STOP:
            continue
        keep.add(lw)
        # Position 0 is capitalized whatever it is, so it votes on nothing either way. Counting it
        # in the denominator is what used to sink "Cornell bans..." — a real name, never judged.
        if informative and i > 0:
            judged.add(lw)
            if w[0].isupper():
                caps.add(lw)
    return keep, caps, judged


def find_clusters(cands: list[dict], state: dict, cfg: dict, max_clusters: int = 14) -> list[dict]:
    """Group stories that are plainly about the same thing, so the editor is handed ready-made
    stacks instead of hunting for them in 180 flat lines.

    The signal is token RARITY, not capitalization — RSS titles are inconsistently cased, but a
    word carried by two or three stories out of two hundred is almost always the thing itself
    (GUILFOYLE, ZACHARIA, HORMUZ), while a word carried by twenty is noise. Held-over page items
    are included so an evening development can stack under a story the morning edition already ran.

    Returns [{"lead": id, "followers": [id, ...], "shared": [token, ...]}].
    """
    page = {x["id"]: x for x in page_links(state)}
    docs: dict[str, set[str]] = {}
    cap_votes: dict[str, int] = {}
    seen_votes: dict[str, int] = {}
    def absorb(sid: str, text: str) -> None:
        toks, caps, judged = _stack_tokens(text)
        docs[sid] = toks
        for t in judged:
            seen_votes[t] = seen_votes.get(t, 0) + 1
        for t in caps:
            cap_votes[t] = cap_votes.get(t, 0) + 1

    for c in cands:
        absorb(c["id"], f'{c["title"]} {(c.get("summary") or "")[:120]}')
    for sid, it in page.items():
        absorb(sid, it.get("title") or it.get("headline") or "")

    df: dict[str, int] = {}
    for toks in docs.values():
        for t in toks:
            df[t] = df.get(t, 0) + 1

    # A name is a token that shows up capitalized mid-title most of the times it appears somewhere
    # the casing means something. HOUTHI and ZACHARIA pass; "race", "choose" and "power" do not,
    # which is the whole difference between a real stack and three strangers sharing a verb.
    names = {
        t for t, n in cap_votes.items()
        if n >= 1 and n / max(seen_votes.get(t, 1), 1) >= 0.6 and t not in STACK_THEME_NAMES
    }
    # A token too rare to have been judged (it only ever led a title, or only appeared in Title
    # Case feeds) still counts as a name when it is distinctive enough — but a pair linked only by
    # these needs two of them, so one stray capital cannot invent a stack.
    weak = {
        t for t, n in df.items()
        if n <= 3 and t not in names and t not in STACK_THEME_NAMES and cap_votes.get(t, 0) == 0
        and seen_votes.get(t, 0) == 0
    }

    # a token shared by 2-6 of the pool identifies a story; 1 is unique, 7+ is a theme
    linking = {t: n for t, n in df.items() if 2 <= n <= 6}
    index: dict[str, list[str]] = {}
    for sid, toks in docs.items():
        for t in toks & linking.keys():
            index.setdefault(t, []).append(sid)

    # Pair up on EVIDENCE, not transitively. A shared name is the floor — two ordinary words in
    # common is a coincidence ("CUTS INTERNET" twice over) — and the stack is then built as a star
    # around one lead rather than a chain, so A-B-C-D cannot drag four unrelated wars into one blob.
    shared_of: dict[tuple[str, str], set[str]] = {}
    for t, ids in index.items():
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                shared_of.setdefault((a, b) if a < b else (b, a), set()).add(t)

    links: dict[str, dict[str, float]] = {}
    for (a, b), toks in shared_of.items():
        shared_names = toks & names
        shared_weak = toks & weak
        if not shared_names and len(shared_weak) < 2:
            continue                      # no subject in common, only grammar
        if shared_names:
            rarest = min(linking[t] for t in shared_names)
            if len(toks) < 2 and rarest > 3:
                continue                  # a single common name is not a story
        shared_names = shared_names or shared_weak
        strength = sum(1.0 / linking[t] for t in toks) + len(shared_names)
        links.setdefault(a, {})[b] = strength
        links.setdefault(b, {})[a] = strength

    weight = {c["id"]: c.get("weight", 1.0) for c in cands}
    age = {c["id"]: c.get("age_h", 0.0) for c in cands}

    # Leads first: a story already on the page leads (an evening development stacks under the
    # morning's headline), then the heaviest and freshest.
    order = sorted(
        links.keys(),
        key=lambda s: (0 if s in page else 1, -weight.get(s, 1.0), age.get(s, 0.0)),
    )
    taken: set[str] = set()
    clusters = []
    for lead in order:
        if lead in taken:
            continue
        kin = sorted(
            (s for s in links[lead] if s not in taken and s != lead),
            key=lambda s: -links[lead][s],
        )[:3]
        if not kin:
            continue
        members = [lead] + kin
        if all(m in page for m in members):
            continue                      # nothing new to say; the page already has all of it
        taken.update(members)
        shared = sorted(
            {t for k in kin for t in shared_of.get((lead, k) if lead < k else (k, lead), set())},
            key=lambda t: linking[t],
        )[:4]
        clusters.append({
            "lead": lead,
            "followers": kin,
            "shared": shared,
            "_score": sum(links[lead][k] for k in kin) + sum(weight.get(m, 1.0) for m in members),
        })

    clusters.sort(key=lambda c: -c["_score"])
    for c in clusters:
        c.pop("_score", None)
    return clusters[:max_clusters]


# ────────────────────────────────────────────────────────────────────────── page helpers

def page_links(state: dict) -> list[dict]:
    links = []
    if state.get("top"):
        links.append(state["top"])
    links.extend(state.get("flash") or [])
    links.extend(state.get("items") or [])
    return links


def age_out(state: dict, cfg: dict, now: datetime) -> dict:
    """Retire stories from the page.

    Two rules, and the EDITION COUNT is the one that matters (Todd's rule, Oct 18 2026: a story
    never rides more than two editions). Counted in editions, not hours, because hours were the
    bug: `max_story_age_hours` measured the age the FEED claimed for the article, so a story the
    feed stamped fresh could sit on the page for five editions. A re-print of the same slot does
    not count twice — the stamp is the edition's own id, so a catch-up run or a hand-dispatched
    rebuild leaves the counter alone.

    `editions` is how many editions the story has been HELD OVER into; it debuts uncounted, so
    max_editions_on_page=2 means one debut plus one hold-over, then it goes.
    `max_story_age_hours` stays underneath as a backstop for anything that arrives mis-dated.
    """
    max_age = cfg["rolling"]["max_story_age_hours"]
    max_eds = int(cfg["rolling"].get("max_editions_on_page", 2))
    local = now.astimezone(ZoneInfo(cfg["site"]["timezone"]))
    this_ed = f"{local:%Y-%m-%d}-{_slot_of(edition_of(local, cfg)[0])}"

    def keep(it: dict) -> bool:
        if it.get("last_edition") != this_ed:        # first build of a new edition: tick it over
            it["editions"] = int(it.get("editions") or 0) + 1
            it["last_edition"] = this_ed
        if max_eds > 0 and int(it.get("editions") or 0) >= max_eds:
            return False
        ref = it.get("published") or it.get("first_seen")
        age = hours_since(ref, now)
        return age is None or age <= max_age

    before = len(page_links(state))
    state["items"] = [it for it in state.get("items", []) if keep(it)]
    state["flash"] = [it for it in state.get("flash", []) if keep(it)]
    if state.get("top") and not keep(state["top"]):
        state["top"] = None
    retired = before - len(page_links(state))
    if retired:
        log(f"retired {retired} stories that had run their {max_eds} editions")
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


def prewritten_headlines(cands: list[dict] | None = None) -> dict:
    """Headlines written between editions by prewrite.py, keyed by story id.

    Missing or unreadable is the normal case and means nothing: the editor simply writes every
    headline itself, exactly as it did before the pre-writer existed.

    A pre-written headline is only offered while the story still has the title it was written
    for (Oct 20, 2026). Outlets rewrite a live story's title as it moves — 'Historic town hall set
    alight' became 'Suspect arrested after historic town hall set alight' under the same URL — and
    a headline written for the earlier title is a stale headline, not a draft.
    """
    try:
        data = json.loads((DATA / "headlines.json").read_text())
        pre = {k: v for k, v in data.items() if isinstance(v, dict) and v.get("headline")}
    except Exception:  # noqa: BLE001
        return {}
    if cands is None:
        return pre
    titles = {c["id"]: norm_title(c.get("title") or "") for c in cands}
    out, moved = {}, 0
    for k, v in pre.items():
        if k in titles and v.get("title") and norm_title(v["title"]) != titles[k]:
            moved += 1
            continue
        out[k] = v
    if moved:
        log(f"prewrite: dropped {moved} headline(s) whose story has moved on since they were written")
    return out


def editor_payload(state: dict, cands: list[dict], cfg: dict, now: datetime) -> dict:
    def brief(it: dict) -> dict:
        return {
            "id": it["id"],
            "headline": it.get("headline"),
            "source": it.get("source"),
            "topic": it.get("topic"),
            "age_h": hours_since(it.get("published") or it.get("first_seen"), now),
        }

    local = now.astimezone(ZoneInfo(cfg["site"]["timezone"]))
    try:
        no = edition_number(local, edition_of(local, cfg)[0])
    except Exception:  # noqa: BLE001
        no = 0
    # The three beats that wore the page out when they ran together: one gets the spotlight each edition,
    # the other two sit out unless the news is genuinely big (see editorial.md, "The rotation").
    wheel = ["immigration_enforcement", "h1b_workforce", "election_integrity"]
    spot = wheel[no % 3]
    clusters = find_clusters(cands, state, cfg)
    pre = prewritten_headlines(cands)
    if pre:
        n = sum(1 for c in cands if c["id"] in pre)
        log(f"payload: {n}/{len(cands)} candidates arrive with a headline already written")
    return {
        "now_utc": iso(now),
        "now_local": local.strftime("%a %b %d %Y %I:%M %p %Z"),
        "edition_no": no,
        "rotation": {"spotlight": spot, "benched": [w for w in wheel if w != spot]},
        # Pre-grouped stories that look like the same thing (see find_clusters). These are a
        # starting point for stacks, not orders: ignore a group that is a coincidence, and stack
        # two stories the grouper missed if they belong together.
        "clusters": clusters,
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
            {"id": c["id"], "source": c["source"], "title": c["title"], "summary": (c["summary"] or "")[:220], "age_h": c["age_h"], "topic_hint": c["topic_hint"],
             **({"suggested": pre[c["id"]]["headline"]} if c["id"] in pre else {})}
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
            it.pop("under", None)  # stacks are re-decided every edition, even for kept stories
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
        if isinstance(entry.get("under"), str) and entry["under"].strip():
            it["under"] = entry["under"].strip()
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
        # Hard fifty (Oct 7, 2026): the page as a whole gets max_swaps new stories, so a new top or flash line
        # counts against it. No slack, and aged-out slots no longer stack on top of the fifty; they only raise
        # the ceiling when more than max_swaps slots are actually empty.
        new_up_top = sum(1 for x in ([top] if top else []) + flash if x["id"] not in current)
        allowed = max(max_swaps - new_up_top, shortfall, 0)
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

    # ── fifty is a FLOOR, not just a ceiling (Oct 18, 2026) ──
    # The cap above stops the editor over-swapping. Nothing stopped the opposite, and the opposite
    # is what actually made the page stale: an editor that returned 33 new stories left the other
    # 67 slots holding yesterday's links, every edition, for ever. If the plan came up short, take
    # the best candidates it did not use and put them in, retiring whichever stories have ridden
    # longest to make room. Stacks are never broken apart to do it, and a backfilled story keeps a
    # written headline when the pre-writer already made one for it.
    if current and cap_churn:
        short = max_swaps - sum(1 for x in ([top] if top else []) + flash + items if x["id"] not in current)
        if short > 0:
            pre = prewritten_headlines(cands)
            leads = {x["under"] for x in items if x.get("under")}
            riders = sorted((i for i, it in enumerate(items)
                             if it["id"] in current and not it.get("under") and it["id"] not in leads),
                            key=lambda i: (-int(items[i].get("editions") or 0),
                                           items[i].get("first_seen") or ""))
            spare = [c for c in sorted(cands, key=lambda c: (-c["weight"], c["age_h"]))
                     if c["id"] not in used and c["id"] not in current]
            filled = 0
            for c in spare:
                if short <= 0 or not riders:
                    break
                head = (pre.get(c["id"]) or {}).get("headline") or c["title"]
                y = make({"id": c["id"], "headline": head, "topic": c.get("topic_hint")}, keep_headline=False)
                if not y or y["id"] in used or y["url"] in used_urls:
                    continue
                used.add(y["id"])
                used_urls.add(y["url"])
                items[riders.pop(0)] = y
                short -= 1
                filled += 1
            if filled:
                log(f"backfill: added {filled} fresh stories the editor left out")
            if short > 0:
                log(f"backfill: still {short} short of {max_swaps} — not enough fresh candidates")

        # ...and make sure the page is actually FULL. When more stories retire than max_swaps — which
        # happens on the first edition after a retirement rule changes, and any time a whole day's
        # links age out together — the empty slots are holes, not swaps, so they are filled from the
        # candidate list rather than left blank. Without this the page simply prints short.
        if len(items) < room:
            pre = prewritten_headlines(cands)
            holes = room - len(items)
            for c in sorted(cands, key=lambda c: (-c["weight"], c["age_h"])):
                if len(items) >= room:
                    break
                if c["id"] in used or c["id"] in current:
                    continue
                head = (pre.get(c["id"]) or {}).get("headline") or c["title"]
                y = make({"id": c["id"], "headline": head, "topic": c.get("topic_hint")}, keep_headline=False)
                if y and take(y):
                    items.append(y)
            log(f"backfill: filled {holes - (room - len(items))} of {holes} empty slots to keep the page at {total}")

    # Ten days, not three (Oct 20, 2026). `seen` is also the memory that stops a story the page
    # already ran from coming back as new when a feed re-stamps it: the Omar raid ran Oct 12,
    # Google News re-dated ABC's piece on Oct 20, and three days of memory had long forgotten it.
    seen = {k: v for k, v in (state.get("seen") or {}).items() if (hours_since(v, now) or 0) <= SEEN_HOURS}
    for c in cands:
        seen.setdefault(c["id"], iso(now))

    # ── story stacks: validate every `under` pointer before it reaches the page ──
    # A follower must point at a column item that is itself a lead, stacks are one level deep,
    # and no lead carries more than four. Anything else loses its pointer and renders as a
    # normal link, so a confused plan degrades to a flat page rather than a broken one.
    by_id = {it["id"]: it for it in items}
    for it in items:
        tgt = it.get("under")
        if not tgt or tgt == it["id"] or tgt not in by_id:
            it.pop("under", None)
    for it in items:                      # no chains: a follower cannot be led by a follower
        tgt = it.get("under")
        if tgt and by_id[tgt].get("under"):
            it.pop("under", None)
    tally: dict[str, int] = {}
    for it in items:
        tgt = it.get("under")
        if not tgt:
            continue
        tally[tgt] = tally.get(tgt, 0) + 1
        if tally[tgt] > 4:
            it.pop("under", None)
    n_stacks = len({it["under"] for it in items if it.get("under")})
    if n_stacks:
        log(f"stacks: {n_stacks} leads carrying {sum(1 for it in items if it.get('under'))} follow-ups")

    if current and cap_churn:
        fresh_n = sum(1 for x in ([top] if top else []) + flash + items if x["id"] not in current)
        if fresh_n != max_swaps:
            log(f"hard fifty: this edition has {fresh_n} new stories, the rule is {max_swaps}")

    new_state = {
        "updated": iso(now),
        "top": top,
        "flash": flash,
        "items": items,
        "seen": seen,
        "notes": str(plan.get("notes", ""))[:300],
    }
    return new_state


def resolve_google_news(url: str) -> str | None:
    """A Google News RSS link (news.google.com/rss/articles/<token>) -> the publisher's URL, via the same
    endpoint Google's own page calls. Returns None when anything about it changes."""
    try:
        import requests

        if "news.google.com" not in url or "/articles/" not in url:
            return None
        tok = url.split("/articles/")[1].split("?")[0]
        h = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/128"}
        r = requests.get(f"https://news.google.com/articles/{tok}", headers=h, timeout=10)
        sg = re.search(r'data-n-a-sg="([^"]+)"', r.text)
        ts = re.search(r'data-n-a-ts="([^"]+)"', r.text)
        if not (sg and ts):
            return None
        req = ["garturlreq", [["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1, None, None, None, None, None, 0, 1],
                              "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0], tok, int(ts.group(1)), sg.group(1)]
        rr = requests.post("https://news.google.com/_/DotsSplashUi/data/batchexecute",
                           headers=h | {"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
                           data={"f.req": json.dumps([[["Fbv4je", json.dumps(req)]]])}, timeout=10)
        body = rr.text.split("\n", 1)[1] if rr.text.startswith(")]}'") else rr.text
        for c in re.findall(r"\[\[.*?\]\](?=\n|$)", body, re.S):
            try:
                outer = json.loads(c)
            except ValueError:
                continue
            for item in outer:
                if isinstance(item, list) and len(item) > 2 and isinstance(item[2], str) and "garturlres" in item[2]:
                    out = json.loads(item[2])[1]
                    return out if isinstance(out, str) and out.startswith("http") else None
    except Exception:  # noqa: BLE001
        return None
    return None


PUB_DATE_KEYS = r"(?:article:published_time|og:article:published_time|article:published|pubdate|publishdate|publish-date|publish_date|date|dc\.date|dc\.date\.issued|dcterms\.created|parsely-pub-date|sailthru\.date|datepublished)"
PUB_DATE_RES = [
    re.compile(r'<meta[^>]+(?:property|name|itemprop)=["\']' + PUB_DATE_KEYS + r'["\'][^>]+content=["\']([^"\']+)', re.I),
    re.compile(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name|itemprop)=["\']' + PUB_DATE_KEYS + r'["\']', re.I),
    re.compile(r'"datePublished"\s*:\s*"([^"]+)"', re.I),
    re.compile(r'<time[^>]+datetime=["\']([^"\']+)["\'][^>]*(?:pubdate|itemprop=["\']datePublished)', re.I),
]


def fetch_page_meta(url: str, cfg: dict) -> dict:
    """One fetch of the story's own page: its og:image and the date the PUBLISHER says it was published.

    Returns {"image": str|None, "published": datetime|None}; never raises. A Google News link is
    resolved to the publisher first (the redirect page only offers Google's logo and no date).
    The date is the earliest publication stamp on the page — article:published_time, the JSON-LD
    datePublished and the like — never a modified/updated time, which is exactly the stamp that
    lies. Anything outside 2020..tomorrow is treated as a broken template and ignored.
    """
    out: dict = {"image": None, "published": None}
    # NOTE: a paywalled publisher (USA Today answers 402) yields nothing; the editor-side fallback is a hand-set state image.
    try:
        import requests

        real = resolve_google_news(url)
        if real:
            url = real
        r = requests.get(url, timeout=8, headers={"User-Agent": cfg["fetch"]["user_agent"]})
        text = r.text
        m = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)', text, re.I) or re.search(
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', text, re.I
        )
        img = html.unescape(m.group(1)) if m else None
        # A Google News redirect page's og:image is Google's own logo (lh3.googleusercontent.com) — never print it.
        if img and not ("googleusercontent.com" in img or "news.google.com" in img or "gstatic.com" in img):
            out["image"] = img
        now = utcnow()

        def sane(raw: str) -> datetime | None:
            dt = parse_iso(html.unescape(raw).strip())
            return dt if dt and datetime(2020, 1, 1, tzinfo=timezone.utc) <= dt <= now + timedelta(days=1) else None

        # The <meta> stamps first (both attribute orders), then the JSON-LD, then a <time> tag — and
        # only the FIRST family that answers. A page's JSON-LD can also list related or older
        # stories with their own datePublished, so it is a fallback, and only its first entry counts.
        meta = [d for rx in PUB_DATE_RES[:2] for raw in rx.findall(text) if (d := sane(raw))]
        if meta:
            out["published"] = min(meta)
        else:
            for rx in PUB_DATE_RES[2:]:
                first = next((d for raw in rx.findall(text) if (d := sane(raw))), None)
                if first:
                    out["published"] = first
                    break
    except Exception:  # noqa: BLE001
        pass
    return out


def fetch_og_image(url: str, cfg: dict) -> str | None:
    return fetch_page_meta(url, cfg).get("image")


# ───────────────────────────────────────── the publisher's date, for EVERY story (Oct 20, 2026)
#
# The feeds lie about dates and the editor can only see the feed's date. A Google News search
# feed re-stamps an old page with the moment Google re-crawled it: that is how an Aug 8 story
# about Caitlin Clark's technical foul, an Oct 12 raid and a three-week-old obituary all reached
# the page on Oct 20 as the morning's news, after every standing order about old news had
# already been written. So the date is no longer taken on trust from anyone. Before the editor
# sees a candidate, the story's own page is asked when it was published, and the answer is
# remembered in data/dates.json so each page is fetched once. The same check sweeps the page
# itself, so a story that got through before this existed is pulled the next time the page
# is built. No date on the page means no verdict (fail open); a date that says the story is
# old means it is gone, whatever the feed said.

DATES_FILE = DATA / "dates.json"


def load_dates() -> dict:
    try:
        d = json.loads(DATES_FILE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def save_dates(cache: dict, now: datetime) -> None:
    keep = {k: v for k, v in cache.items()
            if isinstance(v, dict) and (hours_since(v.get("t"), now) or 0) <= SEEN_HOURS}
    DATA.mkdir(parents=True, exist_ok=True)
    DATES_FILE.write_text(json.dumps(keep, indent=0, sort_keys=True), encoding="utf-8")


def verify_dates(stories: list[dict], cfg: dict, now: datetime, cache: dict) -> dict:
    """{story id: publisher's published datetime, or None when the page gives no date}.

    The URL path answers first (free); then the cache; then one fetch of the page, in parallel.
    Every fetch's answer is cached, including 'no date found', so a page is asked once in ten days.
    """
    out: dict = {}
    todo: list[dict] = []
    for it in stories:
        real = url_date(it["url"])
        if real is not None:
            out[it["id"]] = real
            continue
        rec = cache.get(it["id"])
        if isinstance(rec, dict) and (hours_since(rec.get("t"), now) or 0) <= SEEN_HOURS:
            out[it["id"]] = parse_iso(rec.get("p"))
            continue
        todo.append(it)
    if todo:
        with cf.ThreadPoolExecutor(max_workers=cfg["fetch"]["workers"]) as ex:
            for it, meta in zip(todo, ex.map(lambda i: fetch_page_meta(i["url"], cfg), todo)):
                dt = meta.get("published")
                cache[it["id"]] = {"p": iso(dt), "t": iso(now)}
                out[it["id"]] = dt
        log(f"dates: asked {len(todo)} publishers, {sum(1 for i in todo if out.get(i['id']))} answered "
            f"({len(stories) - len(todo)} known already)")
    return out


def drop_stale(stories: list[dict], limit_hours: float, cfg: dict, now: datetime, cache: dict, what: str) -> list[dict]:
    """The stories whose publisher date is within `limit_hours`, or unknown. Logs every one it drops."""
    if not stories:
        return stories
    dates = verify_dates(stories, cfg, now, cache)
    keep = []
    for it in stories:
        dt = dates.get(it["id"])
        age = (now - dt).total_seconds() / 3600 if dt else None
        if age is not None and age > limit_hours:
            log(f"STALE {what}: {it.get('source')} — {str(it.get('title'))[:70]} — the publisher dates it "
                f"{dt:%b %d}, {age / 24:.0f} days ago (the feed said {it.get('published')})")
            continue
        if age is not None and age > (hours_since(it.get("published"), now) or 0):
            it["published"] = iso(dt)             # the editor sees the true age, not the feed's
            if "age_h" in it:
                it["age_h"] = round(age, 1)
        keep.append(it)
    return keep


def sweep_page(state: dict, cfg: dict, now: datetime, cache: dict) -> dict:
    """Pull every story on the page whose publisher date says it is old. Held-over links are only
    ever checked once (the cache), so this costs nothing after the first pass."""
    limit = float(cfg["rolling"]["max_story_age_hours"]) + 24
    before = len(page_links(state))
    ids_before = {it["id"] for it in page_links(state)}
    top = drop_stale([state["top"]], limit, cfg, now, cache, "lead") if state.get("top") else []
    state["top"] = top[0] if top else None
    state["flash"] = drop_stale(list(state.get("flash") or []), limit, cfg, now, cache, "flash line")
    state["items"] = drop_stale(list(state.get("items") or []), limit, cfg, now, cache, "story")
    ids = {it["id"] for it in state["items"]}
    for it in state["items"]:                                   # a follow-up whose lead is gone stands alone
        if it.get("under") and it["under"] not in ids:
            it.pop("under", None)
    if not state.get("top") and state["flash"]:
        promoted = state["flash"].pop(0)
        promoted["urgent"] = False
        state["top"] = promoted
        log(f"top: promoted the first flash line — {promoted['headline']}")
    after = {it["id"] for it in page_links(state)}
    for sid in ids_before - after:                              # never offered back by the wire
        state.setdefault("seen", {}).setdefault(sid, iso(now))
    gone = before - len(page_links(state))
    if gone:
        log(f"sweep: pulled {gone} re-dated stor{'y' if gone == 1 else 'ies'} off the page")
    else:
        log("sweep: every story on the page checks out against its publisher's date")
    return state


# ────────────────────────────────────────────────────────────────────────── render


# ───────────────────────────────────────────────────────── the evergreen shelf

SHELF_STATE = DATA / "shelf.json"

# A few feeds are published from a host that is not the paper's front door. Without these the
# shelf would link readers at bbci.co.uk or content.dowjones.io, which are plumbing, not papers.
OUTLET_HOSTS = {
    "bbci.co.uk": "bbc.com",
    "content.dowjones.io": "marketwatch.com",
    "972mag.com": "www.972mag.com",
    "cbo.gov": "www.cbo.gov",
    "news.antiwar.com": "antiwar.com",
}


def _outlet_links(limit: int) -> list[dict]:
    """The outlet columns, derived from feeds.yaml so the shelf points at the wire we actually read.

    One entry per publisher (several feeds often share a domain), heaviest weight first, and the
    Google News search feeds are left out — they are a query, not a paper anyone would want to visit.
    """
    best: dict[str, dict] = {}
    for f in _feed_list():
        url = f.get("url") or ""
        host = urlparse(url).netloc.lower()
        if not host or "news.google.com" in host:
            continue
        host = re.sub(r"^(www|rss|feeds?|cms|moxie|api)\.", "", host)
        host = OUTLET_HOSTS.get(host, host)
        # Trim a feed's section suffix ("Daily Mail Health") without eating a hyphenated
        # masthead ("Al-Monitor", "19FortyFive"): only a SPACED dash or a bracket starts a suffix.
        name = re.sub(r"\s+[-—–]\s.*$|\s*[(\[].*$", "", f.get("name") or "").strip()
        name = re.sub(r"\s+(Health|Business|World|US|SciTech|Femail|Politics|Sports|Tech)$", "", name).strip()
        if not name:
            continue
        w = float(f.get("weight", 1.0) or 1.0)
        cur = best.get(host)
        if not cur or w > cur["weight"]:
            best[host] = {"name": name.upper(), "url": f"https://{host}/", "weight": w}
    out = sorted(best.values(), key=lambda x: (-x["weight"], x["name"]))[:limit]
    return sorted(out, key=lambda x: x["name"])


def _check_links(urls: list[str], timeout: int) -> set[str]:
    """Which of these answer right now. HEAD, then GET for the servers that refuse HEAD."""
    import urllib.request

    ok: set[str] = set()

    hdrs = {"User-Agent": "Mozilla/5.0 (compatible; BeehiveWire/1.0)",
            "Accept": "text/html,application/xhtml+xml"}

    def once(u: str, method: str) -> int | None:
        try:
            req = urllib.request.Request(u, method=method, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code
        except Exception:  # noqa: BLE001 — timeout, DNS, TLS: not today
            return None

    # What we are actually asking is "can a READER open this", not "will this server talk to a
    # datacenter IP". Plenty of live sites (congress.gov, FRED, Kalshi, Substack) answer a bot
    # with 403 or 429, and treating that as death quietly guts the shelf. So a link counts as
    # dead only when the page is really gone or the host does not exist.
    GONE = {404, 410}

    def probe(u: str) -> str | None:
        st = once(u, "HEAD")
        if st in (403, 405, 501, 429):        # refused the method or the visitor, not missing
            st = once(u, "GET")
            if st in (403, 429, 401):
                return u                      # it is there; it just does not like us
        if st is None:
            return None                       # DNS, TLS or timeout: nothing answered
        return None if st in GONE else u

    with cf.ThreadPoolExecutor(max_workers=12) as ex:
        for got in ex.map(probe, urls):
            if got:
                ok.add(got)
    return ok


def shelf(cfg: dict, now: datetime, check: bool = True) -> dict | None:
    """The permanent links at the foot of the page, with the dead ones left off.

    A link is shown if it answered this build, or answered within `stale_after_days` — so one bad
    minute on someone else's server does not blank a row, but a tracker that genuinely dies drops
    off the page on its own. No Claude call, no editor time: this is all deterministic.
    """
    try:
        sh = load_yaml(ROOT / "shelf.yaml")
    except Exception as e:  # noqa: BLE001
        log(f"shelf: no shelf.yaml ({e})")
        return None
    if not sh or not sh.get("enabled", True):
        return None

    sections = sh.get("sections") or []
    outlets_cfg = sh.get("outlets") or {}
    outlets = _outlet_links(int(outlets_cfg.get("count", 56))) if outlets_cfg.get("enabled", True) else []

    every = [l["url"] for s in sections for l in (s.get("links") or [])] + [o["url"] for o in outlets]
    try:
        seen = json.loads(SHELF_STATE.read_text()) if SHELF_STATE.exists() else {}
    except Exception:  # noqa: BLE001
        seen = {}

    trusted = False
    if check and every:
        alive = _check_links(every, int(sh.get("check_timeout_seconds", 8)))
        # Only believe the check if most of the web answered. If a quarter or fewer did, the
        # problem is this runner's network, not twenty dead trackers at once — and letting that
        # verdict stand would blank the whole shelf over one bad minute.
        trusted = len(alive) > len(every) * 0.25
        if trusted:
            for u in alive:
                seen[u] = iso(now)
            SHELF_STATE.parent.mkdir(parents=True, exist_ok=True)
            SHELF_STATE.write_text(json.dumps(seen, indent=0, sort_keys=True))
        log(f"shelf: {len(alive)}/{len(every)} links answered"
            f"{'' if trusted else ' — too few to trust, keeping the last known-good shelf'}")
    stale = float(sh.get("stale_after_days", 7)) * 24

    def live(u: str) -> bool:
        h = hours_since(seen.get(u), now)
        if h is None:
            # Never confirmed either way: show it unless a check we trust has just had its say.
            return not trusted
        return h <= stale

    rows = []
    for s in sections:
        links = [l for l in (s.get("links") or []) if live(l["url"])]
        if links:
            rows.append({"title": s.get("title", ""), "links": links})
    kept_outlets = [o for o in outlets if live(o["url"])]
    if kept_outlets:
        rows.append({"title": outlets_cfg.get("title", "The wire"), "links": kept_outlets, "wide": True})
    if not rows and not sh.get("house"):
        return None
    return {"house": sh.get("house") or [], "sections": rows}

def _pick_pictures(items: list[dict], cfg: dict, now: datetime) -> None:
    """Mark a few column stories to run with their picture.

    Costs nothing: most feeds hand us an image with the story, so this is choosing which of the
    ones we already have to show. Pictures go to NEW stories (a reader should see what changed),
    never to a stack follow-up (which must stay tight under its lead), and never two in a row.
    """
    want = int(cfg["page"].get("images_per_edition", 3))
    for it in items:
        it.pop("show_image", None)
    if want <= 0:
        return
    ref = parse_iso(items[0].get("first_seen")) if items else None

    def is_new(it: dict) -> bool:
        fs = parse_iso(it.get("first_seen"))
        return bool(fs) and (now - fs).total_seconds() < 3600 * 6

    # Beats where a picture earns its place. A chart-free economy link gains nothing from one.
    PICTORIAL = {"pop_culture", "weird", "sports", "weather_disasters", "american_life",
                 "utah_mormon", "world", "crime", "health"}
    # Leave the first few slots clear: a photo directly beneath the giant headline competes
    # with it, and the lead is supposed to own the top of the page.
    FLOOR = 4
    # New stories get the pictures first, but a short night should not leave the page grey: once
    # the new ones with art are used up we keep going into the held-over ones until we hit `want`.
    eligible = [i for i, it in enumerate(items) if i >= FLOOR and it.get("image") and not it.get("under")]
    cand = [i for i in eligible if is_new(items[i])]
    cand.sort(key=lambda i: (0 if items[i].get("topic") in PICTORIAL else 1, i))
    rest = [i for i in eligible if not is_new(items[i])]
    rest.sort(key=lambda i: (0 if items[i].get("topic") in PICTORIAL else 1, i))
    cand += rest

    GAP = 5                                          # never bunch them up
    chosen: list[int] = []
    for i in cand:
        if len(chosen) >= want:
            break
        if any(abs(i - j) < GAP for j in chosen):
            continue
        chosen.append(i)
    for i in chosen:
        items[i]["show_image"] = True


def _ordered_items(items: list[dict]) -> list[dict]:
    """Reading order with stacks intact: each lead is followed immediately by its follow-ups.

    The editor sends `items` in its own reading order and marks a follow-up with `under: <lead id>`.
    Followers may sit anywhere in that list, so they are lifted out and re-seated under their lead;
    a pointer to a story that is not on the page has already been stripped in apply_plan, but one
    is tolerated here too and simply renders flat."""
    kids: dict[str, list[dict]] = {}
    for it in items:
        tgt = it.get("under")
        if tgt:
            kids.setdefault(tgt, []).append(it)
    out: list[dict] = []
    for it in items:
        if it.get("under"):
            continue
        row = dict(it)
        row["sub"] = False
        out.append(row)
        for k in kids.get(it["id"], []):
            sub = dict(k)
            sub["sub"] = True
            sub.pop("show_image", None)          # a follow-up stays a line, never a picture
            out.append(sub)
    if len(out) != len(items):  # orphaned follow-ups keep their slot rather than vanishing
        placed = {x["id"] for x in out}
        for it in items:
            if it["id"] not in placed:
                row = dict(it)
                row["sub"] = False
                out.append(row)
    return out


def _split_columns(items: list[dict], ncols: int) -> list[list[dict]]:
    """Deal the reading order into `ncols` columns of about equal length, left to right, without
    ever cutting a stack: a lead and the follow-ups seated under it (`sub`) move as one block, so
    the break between columns always falls between unrelated stories. With one column this is
    just the list. (Oct 27, 2026: the page went from one column to two.)"""
    if ncols <= 1 or not items:
        return [list(items)] if ncols <= 1 else [list(items)] + [[] for _ in range(ncols - 1)]
    blocks: list[list[dict]] = []
    for it in items:
        if it.get("sub") and blocks:
            blocks[-1].append(it)
        else:
            blocks.append([it])
    cols: list[list[dict]] = [[] for _ in range(ncols)]
    remaining = len(items)
    ci = 0
    share = -(-remaining // ncols)
    for b in blocks:
        # A column's share is its fair cut of what was left when it opened; a block that would push
        # it past that share opens the next column, unless this one is still empty.
        if cols[ci] and ci < ncols - 1 and len(cols[ci]) + len(b) > share:
            ci += 1
            share = -(-remaining // (ncols - ci))
        cols[ci].extend(b)
        remaining -= len(b)
    return cols


def _page_html(state: dict, cfg: dict, now: datetime, root: str = "", archived: bool = False,
               edition_no: int = 0, shelf_rows: dict | None = None):
    """Render one edition. `root` is how many levels up the assets sit; archived pages
    carry a banner and skip the install prompt."""
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    env = Environment(loader=FileSystemLoader(ROOT / "templates"), autoescape=select_autoescape(["html"]))
    tpl = env.get_template("page.html")

    ncols = max(1, int(cfg["page"].get("columns") or 1))
    _pick_pictures(state.get("items") or [], cfg, now)
    items = _ordered_items(state.get("items") or [])

    # Held over or new this edition? A story's first_seen is stamped the edition it arrived, so
    # anything first seen before this edition's own stamp was on the previous page. The template
    # grays those out and the masthead counts the new ones.
    ref = parse_iso(state.get("updated")) or now

    def held(it: dict) -> bool:
        fs = parse_iso(it.get("first_seen"))
        return bool(fs) and fs < ref - timedelta(minutes=5)

    columns = []
    prev = None
    for chunk in _split_columns(items, ncols):
        col = [dict(x) for x in chunk]
        for it in col:
            # A rule separates topic clusters, but never cuts into a stack: a follow-up is part of
            # the block above it, so it never carries one. The rule is reckoned across the column
            # seam too, so a phone (where the columns stack) still gets one between the last story
            # of one column and the first of the next; the stylesheet hides it when the columns
            # sit side by side.
            it["sep"] = bool(prev) and not it.get("sub") and it.get("topic") != prev.get("topic")
            it["held"] = held(it)
            prev = it
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
        shelf=shelf_rows,
        video=edition_video(local, edition, state, cfg),
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


def edition_video(local: datetime, edition: str, state: dict, cfg: dict) -> dict | None:
    """The YouTube video for the edition being rendered, or None.

    video.py records every upload in data/video/published.json under the same
    `{date}-{slot}` key the archive uses, so the page can find its own video without
    knowing anything about YouTube. Three deliberate rules:

      * key match only — the moment a new edition prints, the previous edition's video
        stops matching and the block disappears by itself. No stale video on a fresh page.
      * a remake hides the video it replaces (`hidden`), so hidden records are skipped and
        the newest surviving record for the key wins.
      * an EXTRA never gets a video, and its key never matches one.
      * `offpage: true` on a record keeps that video off the front page without touching
        YouTube — for an edition whose video narrates a story the page has since pulled
        (Oct 20, 2026: the morning video led on a week-old Omar raid). `hidden` is left alone
        so a forced remake still takes the old video down on YouTube.

    Never raises: a missing or malformed file just means no video block.
    """
    if not (cfg.get("video") or {}).get("show_on_site", True):
        return None
    if state.get("extra"):
        return None
    key = f"{local.strftime('%Y-%m-%d')}-{_slot_of(edition)}"
    try:
        records = json.loads(VIDEO_PUBLISHED.read_text(encoding="utf-8"))
    except Exception:                      # noqa: BLE001 — no video is a normal state, not an error
        return None
    live = [r for r in records
            if isinstance(r, dict) and r.get("key") == key and r.get("video_id")
            and not r.get("hidden") and not r.get("offpage")]
    if not live:
        return None
    v = live[-1]
    return {"id": v["video_id"], "title": v.get("title") or "", "url": f"https://www.youtube.com/watch?v={v['video_id']}"}


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
    try:                                       # one network check per build, shared by both renders
        shelf_rows = shelf(cfg, now)
    except Exception as e:  # noqa: BLE001 — the shelf must never take the front page down
        log(f"shelf: skipped ({e})")
        shelf_rows = None
    html_out, local, edition = _page_html(state, cfg, now, edition_no=no, shelf_rows=shelf_rows)
    SITE.mkdir(parents=True, exist_ok=True)
    out = SITE / "index.html"
    out.write_text(html_out, encoding="utf-8")
    if cfg["site"].get("domain"):
        (SITE / "CNAME").write_text(cfg["site"]["domain"].strip() + "\n")
    (SITE / "robots.txt").write_text(
        f"User-agent: *\nAllow: /\nDisallow: /markets/\n\nSitemap: {_base_url(cfg)}sitemap.xml\n")
    if STATIC.is_dir():                       # app icons, manifest, service worker
        for f in sorted(STATIC.iterdir()):
            if f.is_file():
                shutil.copy2(f, SITE / f.name)

    arc_html, _, _ = _page_html(state, cfg, now, root="../", archived=True, edition_no=no,
                                shelf_rows=shelf_rows)
    archive_edition(arc_html, state, local, edition, no)
    n = publish_archive(cfg)
    publish_sources(cfg)
    write_sitemap(cfg, _archive_index(), now)
    try:                                       # the Market Desk (markets.py) — unlisted, re-rendered every build
        import markets
        markets.render_site(cfg)
    except Exception as e:  # noqa: BLE001 — the desk must never take the front page down
        log(f"market desk: skipped ({e})")

    log(f"rendered {out} — edition no. {no}, {len(page_links(state))} links; archive holds {n} editions")
    return out


# ────────────────────────────────────────────────────────────────────────── main

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="no API call; heuristic picks")
    ap.add_argument("--render-only", action="store_true")
    ap.add_argument("--sweep", action="store_true", help="pull re-dated stories off the current page (publisher dates), re-render, no editor")
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

    if args.sweep:
        cache = load_dates()
        state = sweep_page(state, cfg, now, cache)
        save_dates(cache, now)
        save_state(state)
        render(state, cfg, now)
        return 0

    if stand_down(state):        # anything past here is a scheduled edition
        save_state(state)        # persist it now, so an early exit still clears the siren

    state = age_out(state, cfg, now)
    if not args.no_network:
        cache = load_dates()                 # held-over stories are checked once each; cached after
        state = sweep_page(state, cfg, now, cache)
        save_dates(cache, now)
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
        desk = desk_candidates(cfg, now)
        if desk:
            log(f"market desk: offering {len(desk)} investing candidates")
            raw.extend(desk)
        offer_all = False
    cands = select_candidates(normalize_candidates(raw, cfg), state, cfg, now, offer_all,
                              verify=not (args.no_network or args.candidates_json))
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

    # The giant headline should carry a picture whenever one can be found. Retry every build while
    # it has none — a held-over top used to be stuck without one forever, because this only ran
    # when the top story changed.
    top = new_state.get("top")
    if top and not top.get("image") and not args.no_network:
        top["image"] = fetch_og_image(top["url"], cfg)
        if not top.get("image"):
            log(f"top image: none found for {top['source']}")

    save_state(new_state)
    render(new_state, cfg, now)
    changed = {x["id"] for x in page_links(new_state)} - {x["id"] for x in page_links(state)}
    log(f"done: {len(changed)} new links; notes: {new_state.get('notes', '')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
