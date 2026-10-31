# Working in this repo

## Session titles (Todd's standing order, Oct 28, 2026)

At the end of every piece of work, rename the session so it can be found later in the claude.ai/code
list. Always put the date first, then the repo, then what was done and anything still waiting on Todd:

    Oct 28, 2026 — Beehive Wire: two columns + switch, Intraday chart tab (relay paste still needed)

Use the `set_session_title` tool when it is available. Rename again if the session's work changes.

## How the site goes live

- A push to `main` that touches anything outside `data/` and `archive/` redeploys the site within a few
  minutes (`.github/workflows/build.yml`). Editorial and template changes go live that way.
- `relay/quote-worker.js` is NOT deployed by a push. Todd pastes it into the Cloudflare Worker by hand
  (see `relay/README.md`). Say so whenever that file changes.
- Local test renders (`python build.py --render-only`) rewrite files under `archive/`; revert those
  before committing rather than committing a hand-run render.

## Market Desk preferences (Todd)

- Volume is not a read-out Todd cares much about. Don't add volume columns to the lists; the distance from
  the pivot (buy point) is what he wants to see at a glance (Oct 29, 2026).
- The Beehive 100 and the Big Cap 40 are always full. The Big Cap 40 keeps its $40B floor; if fewer than 40
  companies over the floor sit among the strongest names, search further down the universe for more until
  there are 40 (Oct 29, 2026). Both sizes live in markets.yaml (leaders.list_size, leaders.big_cap_size).

## The Market Desk gate (Oct 30, 2026)

- `/markets/` is for readers with a code. `desk_gate.py` seals every desk page at render time. Codes are
  hand codes in the `MARKETS_ACCESS_CODES` secret, codes Todd mints on /markets/keys/, or codes Stripe buyers
  claim after paying ($9.99 for the first 21 days, then $49 a month; relay/quote-worker.js `/gate/`).
  Never add any other way to get a code, and never publish the desk pages unsealed while the gate is on.
- A local `python build.py --render-only` with the variable unset seals the desk with no codes (locked for
  everyone); set `MARKETS_ACCESS_CODES=test` to look at it locally.
- Todd does not give out free codes. The only hand-minted code is his own; every other reader pays through
  Stripe. Don't suggest comp codes or a "request a code" path as the way in (Oct 31, 2026).
