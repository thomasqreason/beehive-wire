#!/usr/bin/env python3
"""
Test every feed in feeds.yaml.

    python check_feeds.py            # report LIVE / STALE / DEAD for each feed
    python check_feeds.py --prune    # also rewrite feeds.yaml without the DEAD ones (a backup is kept)
"""
import argparse
import concurrent.futures as cf
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import feedparser
import requests
import yaml

ROOT = Path(__file__).resolve().parent
UA = "Mozilla/5.0 (compatible; BeehiveWire/1.0; +https://github.com/)"


def check(feed: dict) -> tuple[str, str, dict]:
    try:
        r = requests.get(feed["url"], timeout=15, headers={"User-Agent": UA, "Accept": "application/rss+xml, application/atom+xml, */*"})
        d = feedparser.parse(r.content)
        n = len(d.entries)
        if r.status_code >= 400 or n == 0:
            return "DEAD", f"http {r.status_code}, {n} items", feed
        newest = None
        for e in d.entries[:25]:
            tp = e.get("published_parsed") or e.get("updated_parsed")
            if tp:
                ts = datetime(*tp[:6], tzinfo=timezone.utc)
                newest = ts if newest is None or ts > newest else newest
        if newest is None:
            return "LIVE", f"{n} items, no dates", feed
        age_h = (datetime.now(timezone.utc) - newest).total_seconds() / 3600
        return ("LIVE" if age_h < 24 * 7 else "STALE"), f"{n} items, newest {age_h/24:.1f}d ago", feed
    except Exception as e:
        return "DEAD", str(e)[:70], feed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prune", action="store_true")
    args = ap.parse_args()

    path = ROOT / "feeds.yaml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    feeds = doc.get("feeds", [])
    with cf.ThreadPoolExecutor(16) as ex:
        results = list(ex.map(check, feeds))

    dead = []
    for status, info, feed in sorted(results, key=lambda r: (r[0] != "DEAD", r[2]["name"])):
        print(f"{status:5} {feed['name']:34} {info}")
        if status == "DEAD":
            dead.append(feed["url"])
    live = sum(1 for r in results if r[0] == "LIVE")
    print(f"\n{live} live, {sum(1 for r in results if r[0]=='STALE')} stale, {len(dead)} dead of {len(feeds)}")

    if args.prune and dead:
        shutil.copy(path, path.with_suffix(".yaml.bak"))
        doc["feeds"] = [f for f in feeds if f["url"] not in dead]
        path.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=200), encoding="utf-8")
        print(f"pruned {len(dead)} dead feeds -> feeds.yaml (backup: feeds.yaml.bak)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
