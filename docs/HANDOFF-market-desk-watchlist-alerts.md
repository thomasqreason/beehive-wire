# HANDOFF — Market Desk chart pop-up: Watchlist button and Alerts button

Written 2026-11-05 for the implementing session. Second job in the queue: do it AFTER
`docs/HANDOFF-market-desk-passes.md` is built and committed, as its own commit, because both touch
`templates/markets_popup.html` and `templates/markets_style.html`.

## 1. Todd's ask (verbatim, 2026-11-05)

> on the graph next to the intraday button, can we have a "watchlist" button and also, can we have a "set alert"
> button that works? on the watch list, i'd like it when you click on it and dropdown menu appears whereby you
> could have the stock save in a specific what list with the word "new list" at the bottom of the list with each
> list allowing up to 50 if there needs to be a max and when you open that list you can swipe left or right on a
> phone to see the next saved stock graph.. on a laptop arrow keys back an forth and the tab key forward and shift
> tab takes you to the previous one. can you do that?
>
> call it Alerts instead. if you want to save space
>
> on the phone in portrait you might just need an >> button to scroll to the right to see these added buttons

"The graph" = the chart pop-up (`templates/markets_popup.html`). "Next to the intraday button" = the tabs row
built by `tabs(d)` (Daily / Weekly / Monthly / Intraday). The second button is labelled **Alerts**.

## 2. Where things are

- Pop-up: `templates/markets_popup.html`. `tabs(d)` ~line 95 builds the row; `wireTabs()` ~104 handles clicks;
  `show(sym)` ~127 opens a desk-list name from `series/SYM.json`; `fresh(sym)` ~190 opens a typed ticker via the
  relay `?hist=`; `render(d)` ~200 draws the body; `live(sym)` ~221 fetches the relay `?s=` quote; `hide()` and
  the `keydown` Escape handler ~266; `a.tk` click handler ~264. The modal is `#modal`, body `#mbody`, scrim `#scrim`.
- Styles: `templates/markets_style.html` (`.tabs button`, `.modal`, `.facts`, `.board`).
- A per-reader storage pattern already exists: My Positions, `templates/markets_positions.html`
  (localStorage `bw-positions`, rows from `series/SYM.json`). Section `#positions` in `templates/markets.html` ~326.
- Reader identity: the gate keeps the reader's access code in localStorage `bw-desk-code`
  (`templates/markets_gate.html` ~86-124; `desk_gate.py`). Codes are records in the Worker's KV namespace `GATE`
  (`code:<CODE>` → {status: active|revoked, email, …}).
- The relay: `relay/quote-worker.js`, a single-file Cloudflare Worker Todd pastes in by hand (relay/README.md
  "Updating the worker"). It has `quoteFeed(syms)` (Yahoo v7 quote, 25 symbols a call), the `GATE` KV binding and
  the `/gate/*` endpoints. No cron handler yet.
- Desk-wide ntfy already exists for Todd's own phone alerts (`desk_extras.send_alerts`, secret `NTFY_TOPIC`).
  Readers' alerts are a separate thing (below); do not reuse Todd's topic.
- Page JS that runs every minute during the session: `templates/markets_live.js` (the live screen). It already has
  the relay quotes for the page's names; it is the natural place for in-page alert checks.

## 3. Watchlist

**Button.** `Watchlist` in the tabs row after Intraday. Filled star when the open symbol is on any list, hollow
when not. The row stays ONE line (Todd): on a phone in portrait it scrolls sideways, with a `»` button pinned at
the right edge whenever buttons are hidden past it (tap = scroll the row right; it becomes `«` at the end, tap =
back). Native horizontal scroll/swipe on the row works too; hide the scrollbar. The `»` must not be mistaken for
list mode's `›` arrow: list mode's strip is a separate line under the ticker.

**Dropdown** (click the button): the reader's lists, one row each, with a check mark on the lists that already
hold this symbol; clicking a row toggles the symbol in or out of that list. A row for a full list reads
"Name · full (50)" and does nothing. Last row: **New list…** → inline text box (name, 1-30 chars) + Enter; the
list is created with this symbol in it. Rows also offer **Open** (opens list mode on this symbol). Max per list:
`desk.watchlist_max` in `markets.yaml`, default 50. Closes on outside click or Escape (Escape closes the dropdown
first, the pop-up second).

**Watchlists section** on the page (new `#watchlists`, next to My Positions, in `templates/markets.html` and the
nav list): one block per list with its name, a count, a **Browse** link, a **Delete list** link, and the names as
`a.tk` chart links with a small × to remove. Rows show Last / Chg / Chart status / Buy point / vs buy pt / Earnings
from `series/SYM.json` where the desk has one (the Positions table already does this; reuse its code), and just
the ticker where it does not (typed tickers off the desk lists).

**List mode** (Browse, or Open from the dropdown, or a name clicked inside a list block): the pop-up opens on that
symbol and the header gains a strip: `‹  3 of 12 · Growth  ›`. Moving:
- Phone: swipe left = next, swipe right = previous. `touchstart`/`touchend` on `#modal`; horizontal delta ≥ 60px
  and larger than the vertical delta; ignore swipes that start inside the chart's crosshair layer if that layer
  already consumes horizontal drags (`templates/markets_crosshair.html`: check; if it does, start the swipe
  recogniser on the header/facts area and the strip's arrows, which always work).
- Laptop: ArrowRight / Tab = next, ArrowLeft / Shift+Tab = previous. Capture these on `document` while list mode
  is on and `preventDefault()` them, EXCEPT when focus is in an input or textarea (the Alerts price box, the New
  list name box): then Tab behaves normally.
- The strip's ‹ › arrows click too. Stop at the ends (no wrap); the arrow greys out. Escape leaves list mode and
  closes the pop-up as today.
- Opening a name: `show(sym)` when `series/SYM.json` exists, else `fresh(sym)` (typed ticker); prefetch the next
  symbol's doc after each move so swipes feel instant. The URL hash becomes `#list=<listname>&i=<n>` so a reload
  lands on the same chart.

**Storage and sync.** Lists live on the relay, keyed by the reader's code, with localStorage as the cache:
- localStorage `bw-watchlists` = `{v:1, updated:<ISO>, lists:[{name, syms:[...]}]}`.
- Relay: `GET /me?code=<CODE>` → `{watchlists, alerts, topic}`; `PUT /me/watchlists` body `{code, updated, lists}`;
  the worker checks `GATE` `code:<CODE>` is `active` (401 otherwise), stores `wl:<CODE>`. Last write wins by
  `updated`; on page open the page reads `/me`, keeps whichever of local/remote is newer, and writes the loser.
  No relay or no code (local test, or a reader on a code-less preview) = localStorage only, silently.
- Why sync at all: Todd reads on his phone and works on the laptop; the swipe/arrow asks only make sense if the same
  lists are on both.

## 4. Alerts

**Button.** `Alerts` in the tabs row after Watchlist. A small count badge when this symbol has armed alerts.

**Panel** (click): under the tabs, above the chart. For the open symbol:
- Existing rules, each with its state (armed / fired at 10:12 AM MT Nov 5) and a × to delete; a fired rule has
  **Re-arm**.
- Quick picks, prefilled from the chart's numbers and shown only when the number exists: **Above buy point
  236.74**, **Below 8% stop 218.12** (8% under the last price), **Above 52-week high 241.90**. One click arms it.
- Custom: `Above [____]` and `Below [____]` price boxes + **Arm**. Prices to the cent.
- Rule object: `{id, sym, kind: "above"|"below", price, created, fired: null|ISO, fired_price}`. One-shot: a rule
  fires once and sits as "fired" until re-armed or deleted. Rules are per reader, any symbol (desk names and typed
  tickers alike).

**Delivery, two channels, both built:**
1. **In the browser, while a desk page is open:** `markets_live.js` already pulls relay quotes every minute during
   the regular session; extend it to also pull quotes for every symbol with an armed rule (batches of 25 via `?s=`),
   compare, and on a hit: a banner at the top of the page ("NVDA 237.10 — above buy point 236.74 · 10:12 AM MT"),
   a browser `Notification` if the reader granted permission (ask once, from the Alerts panel, never on page load),
   and mark the rule fired (local + relay). Works with no app installed and no cron.
2. **Push when the page is closed:** ntfy. The reader's topic is `bw-` + first 12 hex of SHA-256(code + "|" +
   `ALERT_SALT`) (a new Worker secret; unguessable without the code). The Alerts panel shows it once with a
   subscribe link (`https://ntfy.sh/<topic>`, and the ntfy app on iOS/Android/desktop) and a **Test** button that
   POSTs a test line. The Worker gets a `scheduled(event, env, ctx)` handler: every 5 minutes on weekdays it
   returns early outside 9:30-16:05 ET (NYSE holidays: reuse the pass's `early_closes`/holiday list by shipping
   `data/markets/holidays.json` at render, fetched by the worker and cached a day; if that is more than an hour of
   work, accept that it checks on holidays and never fires since nothing trades), otherwise lists KV `alerts:*`,
   batches the symbols through `quoteFeed`, fires on regular-session price crossing (`regular.price` ≥ above /
   ≤ below), POSTs `https://ntfy.sh/<topic>` with title `SYM 237.10` and the rule line, and writes the rule back as
   fired. Todd adds the trigger in the Cloudflare dashboard: Workers → beehive-quotes → Settings → Triggers →
   Cron Triggers → `*/5 * * * 1-5`. Put that step in `relay/README.md`.
- Relay endpoints: `PUT /me/alerts` body `{code, updated, rules}` (same auth as watchlists; KV `alerts:<CODE>`);
  `POST /me/alerts/test {code}` → one ntfy post. Keep the whole `/me/*` surface in one block with the gate code.
- Regular-session prices only in v1 (no pre/after-hours fires). Say so in one line in the panel.

**Alerts section** on the page (new `#alerts`, next to Watchlists): every rule across symbols, sym / rule / state /
fired time, with × and Re-arm; the ntfy topic and subscribe link at the bottom; the Notification-permission button.

## 5. Config

```yaml
desk:
  watchlist_max: 50
  alerts:
    enabled: true
    ntfy_server: "https://ntfy.sh"
```
Both features switch off cleanly when `desk.quote_relay` is empty (watchlists then stay local-only; the Alerts
button is hidden, since nothing could deliver).

## 6. Rules and gotchas

- Copy for experienced investors: no explaining, no asides. Labels are nouns: Watchlist, Alerts, New list, Arm,
  Re-arm, Browse, Delete list, Test. No "picks"/"calls" language here at all.
- Edit `templates/`, never `site/`. `python build.py --render-only` rewrites the newest `archive/*.html`; `git checkout
  -- archive/` after a test render. Local render with the gate stubbed, served on :8000 (relay CORS allows :8000).
- The worker is pasted by Todd; the repo copy is the source. Keep it one file. New secrets: `ALERT_SALT`.
- `markets_live.js` is also exercised by `relay/parity_test.py` (it exports `BWLiveFacts`); keep that export intact.
- Focus handling: the Tab capture in list mode must not trap a reader who tabbed into the price box; test with a
  keyboard only.
- Swipe must not fight the crosshair (`markets_crosshair.html`) or page scroll: a vertical-dominant drag scrolls, a
  horizontal-dominant one moves lists, a tap does nothing.
- The `#modal` is full-width since Nov 4; the strip goes in the header line under the ticker, not over the chart.

## 7. Test plan

1. Dropdown: create a list, add a name, open another name, add it, uncheck it, fill a list to 50 (script it) and see
   "full (50)", create a second list, delete a list.
2. List mode: Browse a 3-name list; ArrowRight ×3 stops at the end with the arrow greyed; ArrowLeft back; Tab /
   Shift+Tab the same; click into the Alerts price box and Tab moves focus, not the chart. Phone width via
   `resize_window` preset mobile: swipe left/right with `left_click_drag`; a vertical drag scrolls.
3. Sync: with a code in `bw-desk-code` and the worker running under `wrangler dev` (or Todd's deployed one), lists
   round-trip through `/me`; the newer side wins.
4. Alerts: arm "above" at a price below the current quote → the in-page check fires within a minute during the
   session (or stub `Date` and the quote), banner + Notification; rule shows fired; Re-arm resets. ntfy: Test button
   posts to the topic (watch it at `https://ntfy.sh/<topic>` in a browser tab). Worker `scheduled` run via
   `wrangler dev --test-scheduled` (`curl "http://localhost:8787/__scheduled?cron=*/5+*+*+*+1-5"`) fires a rule
   and writes it back.
5. Screenshots: tabs row with both buttons (desktop and phone width), the dropdown, list mode strip, the Alerts
   panel, the Watchlists and Alerts sections.
6. `git checkout -- archive/ data/markets` of test residue; separate commit; status line below updated. Todd pushes
   on go-ahead and pastes the worker, then adds the cron trigger and the `ALERT_SALT` secret.

## 8. Decisions made for Todd

- Lists sync through the relay keyed by the access code (phone and laptop see the same lists); localStorage is the
  cache and the no-relay fallback.
- Alerts are one-shot price crossings (above/below) with buy-point, 8%-stop and 52-week-high quick picks; regular
  session only in v1.
- Push goes through ntfy (free app, no account), chosen over Web Push because Web Push needs VAPID signing and
  payload encryption inside the single-file worker; in-page banners and browser Notifications cover the laptop
  with nothing installed.
- "Alerts" (Todd), "Watchlist" (one word) as the button labels; on phones the row scrolls sideways behind a `»`
  button (Todd) rather than wrapping or shrinking.

## 9. Status

- 2026-11-05: handoff written. Not implemented. Queued behind HANDOFF-market-desk-passes.md.
- 2026-11-05 (afternoon): built and committed on main right after the passes commit, NOT pushed (waiting on
  Todd's go-ahead). Page side in templates/markets_watch.html (included by markets_popup.html, so every desk page
  has it); worker side at the bottom of relay/quote-worker.js; README "Watchlists and alerts" has Todd's two
  Cloudflare steps. Test plan run: 1-2 and 4-5 in the browser (desktop and phone width; swipes dispatched as touch
  events because the pane's drags arrive as mouse events; real Tab / ArrowRight presses); 3 and the cron against
  the real worker code under node with an in-memory KV and ntfy captured (no wrangler on this machine), including
  a live Yahoo quote through the stand-in. All passed; screenshots taken. Where the build differs from the spec:
  - Reads are `POST /me {code}`, not `GET /me?code=`: an access code in a URL ends up in logs and history.
  - The page's sections are titled "My Watchlists" / "My Alerts" (nav "My watchlists", "My alerts"): the desk
    already has a "Watch lists" section. The buttons are "Watchlist" and "Alerts" as asked.
  - The in-page check lives in markets_watch.html, not markets_live.js, so it runs on every desk page (dated pages
    and the frozen Volume Surges pages too), not only the index; it fires on the relay quote's `live` flag
    (regular session), so it needs no session clock of its own.
  - The » button jumps rather than smooth-scrolls (smooth scrolling never moved in a background tab).
  - Tab in list mode moves charts whenever focus is not in a text box; inside the Alerts price boxes Tab moves
    between them and Enter arms.
  Still open: Todd pastes relay/quote-worker.js, adds the ALERT_SALT secret and the */5 * * * 1-5 Cron Trigger.
  Only codes in the worker's KV sync (Stripe codes, codes minted on /markets/keys/): a hand code that exists only in
  the MARKETS_ACCESS_CODES GitHub secret keeps its lists and alerts in that browser, and gets no push.
  relay/live_parity_test.py shows 398/400 identical, the same two names as on the commit before today's work.
