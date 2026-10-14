#!/usr/bin/env python3
"""Write headlines between editions, so the deadline is spent choosing rather than typing.

THE PROBLEM THIS SOLVES. At 6 a.m. and 6 p.m. one Claude call has to write about fifty Drudge
headlines AND decide what the page is: what leads, what stacks, what holds over, what the mix
looks like. That call has run 20-40 minutes and has repeatedly blown its limit and printed
nothing (Oct 8, 2026: eight runs in a row, zero editions).

But writing a headline for one story does not depend on the rest of the page. Only the judgment
does. So the two can be split: write headlines as the stories arrive, and let the deadline call
spend its whole budget on editorial judgment over stories that already have headlines.

STRICTLY ADDITIVE. This writes data/headlines.json and nothing else. build.py attaches a
suggestion to a candidate only when one exists, and the editor is free to ignore any of them. If
this script never runs, the cache is missing, or every suggestion is rejected, the edition path
behaves exactly as it did before this file existed.

Usage:  python prewrite.py            # write headlines for anything new since last time
        python prewrite.py --dry-run  # show what it would ask for, call nothing
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import build

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "data" / "headlines.json"
WORK = ROOT / "data" / "build"

# Caps, so one run can never become the thing it was built to prevent.
MAX_BATCH = 25          # stories per run; a quiet hour does far fewer
KEEP_HOURS = 72         # forget a headline once the story is too old to run


def load_cache() -> dict:
    try:
        return json.loads(CACHE.read_text())
    except Exception:  # noqa: BLE001
        return {}


def save_cache(c: dict) -> None:
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(c, indent=0, sort_keys=True))


def prune(cache: dict, now: datetime) -> dict:
    out = {}
    for sid, rec in cache.items():
        h = build.hours_since(rec.get("at"), now)
        if h is not None and h <= KEEP_HOURS:
            out[sid] = rec
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--max", type=int, default=MAX_BATCH)
    args = ap.parse_args()

    now = build.utcnow()
    cfg = build.load_yaml(ROOT / "config.yaml")
    state = build.load_state()

    raw = build.fetch_all(build._feed_list(), cfg)
    cands = build.normalize_candidates(raw, cfg)
    cands = build.select_candidates(cands, state, cfg, now, offer_all=False)

    cache = prune(load_cache(), now)
    todo = [c for c in cands if c["id"] not in cache][: args.max]
    if not todo:
        build.log("prewrite: nothing new to write")
        save_cache(cache)
        return 0

    payload = {
        "task": "headline",
        "stories": [
            {"id": c["id"], "source": c["source"], "title": c["title"],
             "summary": (c["summary"] or "")[:200], "topic_hint": c["topic_hint"]}
            for c in todo
        ],
    }
    WORK.mkdir(parents=True, exist_ok=True)
    (WORK / "prewrite_in.json").write_text(json.dumps(payload, ensure_ascii=False, indent=1))
    build.log(f"prewrite: {len(todo)} stories need a headline")
    if args.dry_run:
        print(json.dumps(payload, ensure_ascii=False, indent=1)[:1500])
        return 0

    out = WORK / "prewrite_out.json"
    out.unlink(missing_ok=True)
    prompt = (
        "You write headlines for BEEHIVE WIRE. Read editorial.md for the voice — the section "
        "'The voice: Drudge-style headlines', including 'The six devices'. Then read "
        "data/build/prewrite_in.json and write ONE headline for each story in it.\n\n"
        "This is headline writing only. You are NOT choosing what goes on the page, what leads, "
        "or what stacks — an editor does that later with the whole wire in front of it. Judge each "
        "story on its own.\n\n"
        "Obey the accuracy rules in editorial.md: a preview or a question is never written as a "
        "result; a quote headline must be verbatim and on the record; a projection is written as a "
        "projection. If a story's own title does not support a sharp headline, write the plain "
        "accurate one.\n\n"
        'Write data/build/prewrite_out.json as {"headlines": [{"id": "<id>", "headline": '
        '"HEADLINE..."}]} — every id from the input, nothing else. No prose, no fences, no commit.'
    )
    cmd = [
        "claude", "-p", prompt,
        "--model", cfg.get("prewrite", {}).get("model", "claude-sonnet-5"),
        "--effort", cfg.get("prewrite", {}).get("effort", "low"),
        "--allowedTools", "Read,Write",
        "--permission-mode", "acceptEdits",
        "--max-turns", "6",
    ]
    try:
        subprocess.run(cmd, check=True, timeout=60 * 12, stdin=subprocess.DEVNULL)
    except Exception as e:  # noqa: BLE001 — a failed run just means no suggestions this hour
        build.log(f"prewrite: claude failed ({str(e)[:120]})")
        save_cache(cache)
        return 0

    try:
        got = json.loads(out.read_text()).get("headlines") or []
    except Exception as e:  # noqa: BLE001
        build.log(f"prewrite: unreadable output ({str(e)[:80]})")
        save_cache(cache)
        return 0

    by_id = {c["id"]: c for c in todo}
    n = 0
    for row in got:
        sid = str(row.get("id", ""))
        hl = str(row.get("headline", "")).strip()
        src = by_id.get(sid)
        if not src or not hl:
            continue
        cache[sid] = {"headline": build.finish_headline(hl, src["title"]),
                      "title": src["title"], "at": build.iso(now)}
        n += 1
    save_cache(cache)
    build.log(f"prewrite: cached {n} headlines ({len(cache)} held)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
