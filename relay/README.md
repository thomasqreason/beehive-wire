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

## What readers see
The pop-up opens on the pass's saved chart, then within a second the headline switches to the freshest
price, with "Live" (or "At close") and, outside regular hours, "Pre-market" / "After hours" / "Overnight"
lines and their times. If the relay is down the pop-up keeps the pass price; nothing breaks.

## Sources
Yahoo's chart feed (regular, pre-market, after hours; no login) and Yahoo's quote feed for overnight
(8 p.m.–4 a.m. ET; the relay fetches its own session cookie). Unofficial endpoints — fine at this scale,
not a licensed feed. Allowed sites are listed in ALLOWED at the top of the file.
