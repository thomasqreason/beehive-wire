#!/usr/bin/env python3
"""
Beehive Wire — has the edition for the slot that just came round actually printed?

    python catchup.py                                  # answer for right now
    python catchup.py --event schedule                 # what build.yml's check job passes
    python catchup.py --event workflow_dispatch --catchup false     # a hand-run always goes
    python catchup.py --now 2026-09-15T10:12:00+00:00  # try any moment (tests)

GitHub delays scheduled workflows on public repos — by hours, some mornings — and under load it
drops them outright. This script is the memory the workflows lack. It names the slot that most
recently came round (morning or evening, Mountain time, by the same rule build.py names editions),
then looks in archive/index.json for an edition of that slot printed since the slot came round.

    build.yml    asks `go`      — should THIS run print? A cron run that GitHub delivers hours late,
                                   after the edition already went out some other way, stands down.
                                   Pushes and hand-runs always go.
    breaking.yml asks `catchup` — is the edition overdue and missing? Then the hourly siren pass
                                   dispatches build.yml itself, with catchup=true so it too stands
                                   down if something else got there first. No double printing.

Outputs, as key=value on stdout and in $GITHUB_OUTPUT when set:
    slot=morning|evening   date=YYYY-MM-DD   printed=yes|no   overdue_min=N   go=yes|no   catchup=yes|no
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import build

GRACE_MIN = 45   # build.py's own grace: a run that fires this much before a slot belongs to that slot


def clock(dt: datetime) -> str:
    """'Tue Sep 15 4:31 a.m. MDT' — no zero padding, and no %-I, which Windows lacks."""
    return f"{dt.strftime('%a %b')} {dt.day} {build._time_words(dt)} {dt.strftime('%Z')}"


def minutes_past(local: datetime, slot_mins: int) -> int:
    """Minutes since that slot came round; slightly negative inside the grace window."""
    mins = local.hour * 60 + local.minute
    d = (mins - slot_mins) % 1440
    return d - 1440 if d >= 1440 - GRACE_MIN else d


def answer(cfg: dict, now: datetime, event: str, catchup_run: bool, min_overdue: int) -> dict:
    local = now.astimezone(ZoneInfo(cfg["site"]["timezone"]))
    edition, _ = build.edition_of(local, cfg)            # "Morning Edition" / "Evening Edition"
    slot = build._slot_of(edition)                        # "morning" / "evening"
    slot_mins = dict((name, mins) for mins, name in build._slots(cfg))[edition]
    overdue = minutes_past(local, slot_mins)
    came_round = local - timedelta(minutes=overdue)       # the moment this slot's clock struck
    came_round = came_round.replace(second=0, microsecond=0)

    # Printed means: an edition of this slot in the book with a publish time at or after the slot
    # came round (less the grace window). Matching on time rather than the date label keeps a
    # late-night evening edition, which build.py files under the next day's date, from looking
    # unprinted — and stops the siren re-sending it every hour until dawn.
    printed_at = None
    for e in build._archive_index():
        if e.get("slot") != slot:
            continue
        when = build.parse_iso(e.get("iso"))
        if when and when >= came_round - timedelta(minutes=GRACE_MIN):
            if printed_at is None or when > printed_at:
                printed_at = when

    if event == "push":
        go = True                                  # a code push always re-renders and deploys
    elif event == "workflow_dispatch" and not catchup_run:
        go = True                                  # a hand-run always prints, even a re-print
    else:
        go = printed_at is None                    # cron and catch-up runs stand down if it's out
    catchup = printed_at is None and overdue >= min_overdue

    return {
        "slot": slot,
        "date": came_round.strftime("%Y-%m-%d"),
        "printed": "yes" if printed_at else "no",
        "printed_at": clock(printed_at.astimezone(ZoneInfo(cfg["site"]["timezone"]))) if printed_at else "",
        "overdue_min": str(overdue),
        "go": "yes" if go else "no",
        "catchup": "yes" if catchup else "no",
        "local": clock(local),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--event", default="schedule", help="github.event_name of the run asking (default: schedule)")
    ap.add_argument("--catchup", default="false", help="the workflow's catchup input, 'true' or 'false'")
    ap.add_argument("--min-overdue", type=int, default=10,
                    help="minutes past the slot before the siren may send the edition (default: 10)")
    ap.add_argument("--now", help="ISO-8601 moment to evaluate instead of the clock (tests)")
    args = ap.parse_args()

    cfg = build.load_yaml(build.ROOT / "config.yaml")
    now = build.parse_iso(args.now) if args.now else build.utcnow()
    out = answer(cfg, now, args.event.strip().lower(), args.catchup.strip().lower() == "true", args.min_overdue)

    if out["printed"] == "yes":
        line = f"{out['slot'].capitalize()} edition for {out['date']} already printed at {out['printed_at']}."
    else:
        line = f"{out['slot'].capitalize()} edition for {out['date']} has not printed ({out['overdue_min']} min past the slot)."
    verdict = {"yes": "This run goes.", "no": "This run stands down."}[out["go"]]
    print(f"{out['local']}: {line} {verdict} catchup={out['catchup']}")
    for k, v in out.items():
        print(f"{k}={v}")

    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a", encoding="utf-8") as f:
            for k, v in out.items():
                f.write(f"{k}={v}\n")
    gh_sum = os.environ.get("GITHUB_STEP_SUMMARY")
    if gh_sum:
        with open(gh_sum, "a", encoding="utf-8") as f:
            f.write(f"{line} {verdict}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
