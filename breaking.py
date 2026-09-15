"""The siren: watch the wire between editions, and break the schedule only when it is earned.

Runs hourly. The expensive part — asking Claude for a verdict — happens only when a cheap
Python trip-wire fires, so an ordinary day costs nothing at all.

    python breaking.py --emit-payload data/build/breaking.json   # writes only if something surged
    python breaking.py --plan-json  data/build/extra.json        # applies Claude's verdict

The trip-wire looks for one thing: a single story hitting many independent outlets at once that
is not already on the page. That is what a genuinely big event looks like on a wire. Whether it
clears the bar is Claude's call, against the standing orders in editorial.md — and the standing
answer there is no.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import build

ROOT = Path(__file__).resolve().parent

# Words that carry no signal when deciding whether two headlines are the same story.
STOP = set("""a an and are as at be been but by for from had has have he her his if in into is it
its of on or our she that the their them they this to was were what when which who will with you
your says say said after before over under new more most than then there here about against""".split())

WINDOW_MINUTES = 150      # how far back a story can be and still count as "breaking"
MIN_SOURCES = 5           # independent outlets carrying the same story before we even ask
COOLDOWN_HOURS = 6        # never two sirens inside this window
SIMILARITY = 0.42         # token overlap at which two headlines are the same story


def tokens(title: str) -> set[str]:
    words = re.findall(r"[a-z0-9']+", title.lower())
    return {w for w in words if len(w) > 3 and w not in STOP}


def cluster(cands: list[dict]) -> list[list[dict]]:
    """Greedy single-pass clustering on headline overlap. Good enough, and free."""
    groups: list[list[dict]] = []
    seen: list[set[str]] = []
    for c in cands:
        t = tokens(c["title"])
        if not t:
            continue
        for i, core in enumerate(seen):
            overlap = len(t & core) / max(1, min(len(t), len(core)))
            if overlap >= SIMILARITY:
                groups[i].append(c)
                seen[i] = core & t or core
                break
        else:
            groups.append([c])
            seen.append(t)
    return groups


def surging(cands: list[dict], state: dict, now: datetime) -> list[dict] | None:
    """The one cluster worth asking about, or nothing."""
    on_page = {it.get("id") for it in build.page_links(state)}
    cutoff = now - timedelta(minutes=WINDOW_MINUTES)

    fresh = []
    for c in cands:
        if c["id"] in on_page:
            continue
        when = build.parse_iso(c.get("published"))
        if when and when >= cutoff:
            fresh.append(c)

    best, best_n = None, 0
    for group in cluster(fresh):
        n = len({g["source"] for g in group})
        if n > best_n:
            best, best_n = group, n
    return best if best_n >= MIN_SOURCES else None


def cooling_down(state: dict, now: datetime) -> bool:
    last = build.parse_iso(state.get("last_extra_at"))
    return bool(last and (now - last) < timedelta(hours=COOLDOWN_HOURS))


def emit(out: Path) -> int:
    cfg = build.load_yaml(ROOT / "config.yaml")
    state = build.load_state()
    now = build.utcnow()

    if cooling_down(state, now):
        build.log("siren cooling down — not looking")
        return 0

    feeds = (build.load_yaml(ROOT / "feeds.yaml") or {}).get("feeds") or []
    cands = build.normalize_candidates(build.fetch_all(feeds, cfg), cfg)
    group = surging(cands, state, now)
    if not group:
        build.log(f"wire is calm — {len(cands)} candidates, nothing surging")
        return 0

    group.sort(key=lambda g: g.get("published") or "", reverse=True)
    sources = sorted({g["source"] for g in group})
    local = now.astimezone(ZoneInfo(cfg["site"]["timezone"]))
    build.log(f"surge: {len(group)} items across {len(sources)} sources — {group[0]['title'][:70]}")

    payload = {
        "now_local": local.strftime("%a %b %d %Y, %-I:%M %p %Z") if sys.platform != "win32"
                     else local.strftime("%a %b %d %Y, %I:%M %p %Z"),
        "distinct_sources": len(sources),
        "sources": sources,
        "current_top": {k: (state.get("top") or {}).get(k) for k in ("headline", "title", "source")},
        "cluster": [{k: g[k] for k in ("id", "title", "source", "published", "summary")} for g in group[:18]],
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8")
    (out.parent / "breaking-candidates.json").write_text(
        json.dumps(group, indent=1, ensure_ascii=False), encoding="utf-8")
    build.log(f"wrote {out} — asking for a verdict")
    return 0


def apply(plan_path: Path) -> int:
    cfg = build.load_yaml(ROOT / "config.yaml")
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except Exception as e:
        build.log(f"no readable verdict ({e}) — page untouched")
        return 0

    if not plan.get("extra"):
        build.log(f"verdict: no extra — {plan.get('reason', 'below the bar')}")
        return 0

    cands = {c["id"]: c for c in json.loads(
        (plan_path.parent / "breaking-candidates.json").read_text(encoding="utf-8"))}
    top_id = (plan.get("top") or {}).get("id")
    if top_id not in cands:
        build.log(f"verdict named an id we do not have ({top_id}) — page untouched")
        return 0

    state = build.load_state()
    now = build.utcnow()
    local = now.astimezone(ZoneInfo(cfg["site"]["timezone"]))

    def line(cand: dict, headline: str, **extra) -> dict:
        return {"id": cand["id"], "url": cand["url"], "title": cand["title"],
                "source": cand["source"], "published": cand.get("published"),
                "topic": cand.get("topic_hint", "politics_culture_world"),
                "headline": build.finish_headline(headline, cand["title"]), **extra}

    new_top = line(cands[top_id], plan["top"].get("headline", ""), urgent=True)

    flash = [line(cands[f["id"]], f.get("headline", ""))
             for f in (plan.get("flash") or []) if f.get("id") in cands and f["id"] != top_id]
    if state.get("top"):                       # yesterday's lead slides down rather than vanishing
        flash.append(state["top"])
    flash.extend(state.get("flash") or [])

    used, deduped = {new_top["id"]}, []
    for f in flash:
        if f.get("id") and f["id"] not in used:
            used.add(f["id"])
            deduped.append({k: v for k, v in f.items() if k != "urgent"})
    cut = cfg["page"]["flash_lines"]
    flash, bumped = deduped[:cut], deduped[cut:]   # what no longer fits slides into the columns

    keep_ids = {new_top["id"]} | {f["id"] for f in flash}
    items, seen_ids = [], set(keep_ids)
    for it in bumped + (state.get("items") or []):
        iid = it.get("id")
        if iid and iid not in seen_ids:
            seen_ids.add(iid)
            items.append(it)
    items = items[: cfg["page"]["total_links"] - 1 - len(flash)]

    seq = 1 + len([e for e in build._archive_index()
                   if e.get("date") == local.strftime("%Y-%m-%d")
                   and str(e.get("slot", "")).startswith("extra")])

    state.update({"top": new_top, "flash": flash, "items": items,
                  "updated": build.iso(now), "extra": True, "extra_seq": seq,
                  "last_extra_at": build.iso(now),
                  "notes": f"EXTRA: {plan.get('reason', '')}".strip()})
    build.save_state(state)
    build.render(state, cfg, now)
    (plan_path.parent / "PRINTED").write_text("1", encoding="utf-8")   # the workflow reads this
    build.log(f"EXTRA printed — {plan.get('reason', '')}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--emit-payload", type=Path)
    ap.add_argument("--plan-json", type=Path)
    args = ap.parse_args()
    if args.emit_payload:
        return emit(args.emit_payload)
    if args.plan_json:
        return apply(args.plan_json)
    ap.error("give --emit-payload or --plan-json")


if __name__ == "__main__":
    sys.exit(main())
