# Quote relay (live prices in the chart pop-up)

`quote-worker.js` is a Cloudflare Worker. The free plan (100,000 requests a day) is plenty: quotes are
cached 30 seconds, so the cost is at most one Yahoo call per symbol per 30 seconds however many readers click.

## Set up (about 5 minutes, once)
1. Sign up free at https://dash.cloudflare.com/sign-up (no card needed for Workers).
2. Workers & Pages → Create → Create Worker → name it `beehive-quotes` → Deploy.
3. Edit code → delete everything → paste the whole of
   https://raw.githubusercontent.com/thomasqreason/beehive-wire/main/relay/quote-worker.js → Deploy.
4. Copy the worker's address (https://beehive-quotes.<your-name>.workers.dev) and put it in
   markets.yaml under `desk: quote_relay:`. Push. The next render turns live prices on.

Test it: open https://beehive-quotes.<your-name>.workers.dev/?s=DELL in a browser.

## Updating the worker
After a change to quote-worker.js: Cloudflare → Workers & Pages → beehive-quotes → Edit code → select all →
paste the new file from the raw GitHub link above → Deploy. The site side ships on its own with each push.

## The quote box
Pinned to the bottom of every Market Desk page. Any ticker → the relay's `?hist=SYM` mode returns two years of
daily bars plus name and industry → templates/markets_analyze.js runs the desk's own chart rules on them in the
browser (base, buy point, buy range, status, distance from the buy point) → RS rank comes from rs_dist.json, the
universe the last pass ranked. markets_analyze.js is a port of analyze() in markets.py; after changing either,
run `python relay/parity_test.py` (400 synthetic charts, must be identical).

## The Intraday tab
The last tab on every chart in the pop-up, after Monthly, for desk-list names and typed tickers alike. It asks the
relay's `?intra=SYM` mode for five trading days of 5-minute bars (regular session only) the first time it is pressed,
and draws them with 20- and 50-bar lines. If the relay deployed on Cloudflare predates this mode the tab says so and
the chart stays where it was; paste the current worker in (above) to turn it on.

## What readers see
The pop-up opens on the pass's saved chart, then within a second the headline switches to the freshest
price, with "Live" (or "At close") and, outside regular hours, "Pre-market" / "After hours" / "Overnight"
lines and their times. If the relay is down the pop-up keeps the pass price; nothing breaks.

## Sources
Yahoo's chart feed (regular, pre-market, after hours; no login) and Yahoo's quote feed for overnight
(8 p.m.–4 a.m. ET; the relay fetches its own session cookie). Unofficial endpoints — fine at this scale,
not a licensed feed. Allowed sites are listed in ALLOWED at the top of the file.

## The Market Desk gate
The same worker keeps the desk's access codes (KV binding `GATE`) and talks to Stripe. Setup, secrets and the
Stripe steps are in the main README under "The Market Desk gate". Endpoints are listed at the top of
quote-worker.js.

## Watchlists and alerts (Nov 5, 2026)
The chart pop-up's Watchlist and Alerts buttons keep each reader's lists and price alerts in the browser and, under
the reader's access code, in the same KV namespace (`wl:<CODE>`, `alerts:<CODE>`; endpoints `/me` at the bottom of
quote-worker.js). Only codes the worker knows sync: Stripe codes and codes minted on /markets/keys/. A hand code
that lives only in the MARKETS_ACCESS_CODES GitHub secret opens the desk but keeps lists in that browser.
Two settings in Cloudflare, once, after pasting the worker:
1. Secret: beehive-quotes → Settings → Variables and Secrets → Add → Type Secret, name `ALERT_SALT`, value any long
   random string (it makes each reader's ntfy topic unguessable; changing it later moves every reader's topic).
2. Cron: beehive-quotes → Settings → Triggers → Cron Triggers → Add → `*/5 * * * 1-5`. Every five minutes on
   weekdays; it does nothing outside 9:30 a.m.-4:05 p.m. Eastern or on NYSE holidays (it reads
   beehivewire.com/markets/holidays.json, written at every render). Regular-session prices only.
Readers subscribe to their topic in the free ntfy app (iOS, Android, desktop) or at ntfy.sh/<topic>; the Alerts
panel shows it with a Test button. Pages that are open check alerts in the browser too, every minute.
