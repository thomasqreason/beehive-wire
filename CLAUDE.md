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
