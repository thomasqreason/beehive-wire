"""Rebuild the archive from the repository's own history.

Every edition that ever went out left its `data/state.json` behind in a commit. This
walks those commits oldest to newest, re-renders each edition exactly as readers saw
it, numbers them in order, and writes `archive/` from scratch. Run it once after
turning archiving on; after that the normal build keeps the archive current.

    python backfill_archive.py            # rebuild, then re-render the live page
    python backfill_archive.py --dry-run  # just list what it would archive
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import build

ROOT = Path(__file__).resolve().parent
STATE_PATH = "data/state.json"


def git(*args: str) -> str:
    out = subprocess.run(["git", *args], capture_output=True, text=True, cwd=ROOT)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or f"git {' '.join(args)} failed")
    return out.stdout


def editions_from_history(cfg: dict) -> list[dict]:
    """Every distinct edition the repo remembers, oldest first.

    Two commits can touch the same slot — a re-render, a manual fix. The last one
    wins, because that is the version that was actually on the page."""
    revs = git("log", "--format=%H", "--reverse", "--", STATE_PATH).split()
    tz = ZoneInfo(cfg["site"]["timezone"])
    found: dict[tuple[str, str], dict] = {}

    for rev in revs:
        try:
            state = json.loads(git("show", f"{rev}:{STATE_PATH}"))
        except Exception:
            continue
        if not state.get("top"):
            continue
        stamp = build.parse_iso(state.get("updated"))
        if not stamp:
            continue
        local = stamp.astimezone(tz)
        edition, _ = build.edition_of(local, cfg)
        key = (local.strftime("%Y-%m-%d"), build._slot_of(edition))
        found[key] = {"state": state, "local": local, "edition": edition, "rev": rev[:7]}

    return sorted(found.values(), key=lambda e: e["local"])   # numbered by when they went out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="list what would be archived, write nothing")
    args = ap.parse_args()

    if not (ROOT / ".git").exists():
        print("No git history here — nothing to rebuild from.")
        return 1

    cfg = build.load_yaml(ROOT / "config.yaml")
    eds = editions_from_history(cfg)
    if not eds:
        print("No editions found in the history.")
        return 1

    print(f"Found {len(eds)} editions in the repository history:\n")
    for n, e in enumerate(eds, 1):
        print(f"  No. {n:>3}  {e['local']:%Y-%m-%d}  {e['edition']:<15} "
              f"{build._time_words(e['local']):>10}  {e['rev']}  {e['state']['top']['headline'][:54]}")

    if args.dry_run:
        print("\nDry run — nothing written.")
        return 0

    archive = ROOT / "archive"
    archive.mkdir(parents=True, exist_ok=True)
    for old in archive.glob("*.html"):
        old.unlink()
    (archive / "index.json").write_text("[]", encoding="utf-8")

    for n, e in enumerate(eds, 1):
        html, _, _ = build._page_html(e["state"], cfg, e["local"], root="../",
                                      archived=True, edition_no=n)
        build.archive_edition(html, e["state"], e["local"], e["edition"], n)

    print(f"\nArchived {len(eds)} editions. Re-rendering the live page…")
    state = build.load_state()
    build.render(state, cfg, build.utcnow())
    return 0


if __name__ == "__main__":
    sys.exit(main())
