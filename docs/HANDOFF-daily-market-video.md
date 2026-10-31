# Handoff: a free daily stock-market roundup video, and a Beehive Wire YouTube channel

**For:** a fresh Claude session in the `beehive-wire` repo.
**From:** Todd Ridgway (publisher), Oct 31, 2026.
**Read first:** `CLAUDE.md`, then `docs/HANDOFF-market-desk-promo.md` (the product, the method, the naming rules),
then this file. The promo handoff's rules on names (no IBD feature names; attribute the method to O'Neil; not
affiliated with IBD) apply here too.

---

## 1. What Todd wants

A **free daily video**, every trading day at 4:30 p.m. Eastern (and possibly 2:30 p.m.; §3): a stock-market roundup in the Beehive Wire voice,
built from the Market Desk's own numbers. It is marketing for the paid desk and a product in itself.

- **Free.** It never shows the whole paid page, but it can and should show real numbers from that day: the
  market note, the day's breakouts, a few names from the Volume Surges screen, one chart. Enough to be useful,
  not enough to replace the subscription (no full Beehive 100, no live quotes, no buy points on every name).
- **Linked from the front page** of beehivewire.com, the way the edition video already is (`show_on_site` in
  `config.yaml`, the `.watch` block in `templates/page.html`, `build.py` finds the video by edition key in
  `data/video/published.json`). The roundup needs its own slot, under or beside the edition video, labelled
  "Today's market" or similar; and a link on the Market Desk gate page.
- **A Beehive Wire YouTube channel.** Todd would rather these go on a channel named Beehive Wire than on the
  Boring Money channel the editions use. See §4; this is the one part that needs him.
- Ends with the paid-desk pitch: **three weeks for $9.99** (say it that way; the $49.99 regular price stays off the
  video and on the checkout page and description), link in the description. Same attribution/disclaimer lines as the promo (see the promo handoff, §5 and `promo.py`).

---

## 2. What already exists to build on

- **`video.py`** makes the twice-daily edition video: payload → Claude writes narration from
  `video_editorial.md` → slides (`brand_card`, `slide_story`, `frame`) → Kokoro voice → ffmpeg → YouTube upload
  with chapters and a description → `data/video/published.json`. `.github/workflows/video.yml` runs it after
  each build. Copy its shape; don't fork it in place.
- **`promo.py`** shows a hand-scripted variant of the same pipeline.
- **The Market Desk data**, written every pass under `data/markets/`: the day's edition JSON (stories with
  their numbers, the market note, the Desk Chief's verdict, the watch lists), `movers-latest.json` (Volume
  Surges), `lists-latest.json` (Beehive 100 / Big Cap 40), `scorecard.json`, per-symbol `series/*.json` for
  charts. `markets.py` has the chart renderer (`chart_svg`-style code around line 776) that can be rasterized
  for a slide, and `templates/markets_popup.html` has the browser version.
- **The voice and style rules:** `video_editorial.md` (how narration is written: only facts from the payload,
  attribute, short sentences, the fixed sign-off) and `markets_desk.md` (numbers first; volume is reported, not a
  verdict; never invent a number).
- The Market Desk edition prints after the 2 p.m. Mountain close; the markets workflow (`markets.yml`) has the
  slot times. The roundup should run once the edition pass has committed, roughly 3:30–4:00 p.m. Mountain.

---

## 3. The shape: IBD's daily market wrap, done our way (Todd's spec, Oct 31, 2026)

The model is Investor's Business Daily's end-of-day market video: an anchor walks the index charts, calls the
market's state, then shows a few stocks setting up or breaking out, then names in the news after the close.
Copy the structure and the discipline; never the name, the look or the phrases that are theirs (promo handoff §5).
**Length 5–8 minutes.** Every segment below is a slide or two plus narration.

1. **The index charts, with the numbers on them** (60–90 s). Two chart slides, **S&P 500** and **Nasdaq
   Composite**, daily bars, the **50-day moving average** drawn and labelled, today's close marked, and the
   **distribution-day count** for each index printed on the slide (the desk already computes it: down days of
   0.2%+ on volume above the prior session over the last 25 sessions; see `markets.py` ~line 503 and the
   `big_picture` block). Narration: where each index closed and its percent move, above or below the 50-day and
   by how much, the distribution count and whether it rose today, the market's three-state read (the desk's
   "confirmed uptrend / under pressure / in correction" equivalent, from `big_picture.state`) and the Desk Chief's
   exposure line. Add the 200-day line and the 21-day line where they matter. If the day was a follow-through
   day or a count hit five or six, say so plainly. Keep the index data in the payload: the desk downloads ^GSPC
   and ^IXIC (check `markets.yaml` `benchmarks`); add the Nasdaq if it is not already there.
2. **Stocks setting up** (90–120 s). Two or three names from the watch lists that are **near a buy point with a
   base complete** or in a tightening pattern (Near a buy point; Aggressive entries). One chart slide each with
   the base drawn, the buy point and buy range shaded, the RS line; narration gives the base kind and length, the
   buy point to the cent, how far below it the stock closed, and the next thing to watch. This is the segment that
   sells the desk: show the chart, say the pivot, then say the full list is on the desk.
3. **Breakouts today** (45–60 s). What cleared a pivot today (Breakouts this week filtered to today; In the buy
   range). Name, pivot, close, distance past the pivot, volume versus the 50-day average as a fact. If nothing broke
   out, say so in one line; do not pad.
4. **After the close** (45–90 s). Big names reporting earnings after the bell or moving in extended hours: the
   `after_hours` block and `ah_movers` in the edition payload (Yahoo's post-market tape, 4:05–8 p.m. Eastern), and
   Earnings This Week for who reports tonight and tomorrow morning. For a Google or Nvidia report: the regular close,
   the after-hours price and move, and whatever the payload's headline says about the report. Never numbers that
   are not in the payload.
5. **Scorecard line** (15 s). The desk's calls over the last 1, 4 and 8 weeks: average, win rate, versus the S&P,
   with the losers counted. One sentence.
6. **Tomorrow** (15 s). The Desk Chief's watch line, and any index level that matters (a 50-day retest, a count).
7. **The offer** (15–20 s, every video, and in the first 30 seconds as a one-line mention). **"Try the Market Desk:
   three weeks for $9.99."** Do not state the regular price in the video; it is on the checkout page and in the
   description. Link in the description, with the attribution line ("follows the breakout discipline William
   O'Neil described in How to Make Money in Stocks") and "Not affiliated with Investor's Business Daily. Not
   investment advice."

**Timing.** Two slots on market days, Todd's choice to be confirmed: **2:30 p.m. Eastern** (a midday read while
the market is open; the charts show the session so far and the narration says so) **and 4:30 p.m. Eastern**
(the close). If only one, the 4:30 close is the one that matches IBD's wrap. Market days only: weekdays minus NYSE
holidays, and on early-close days (the day after Thanksgiving, Christmas Eve, July 3 when observed) the close video
runs at 1:30 p.m. Eastern. `markets.py` already has `nyse_early_close` and the holiday logic the movers passes use;
reuse it, and give the workflow a `due` check the way `video.py due` works so a holiday never produces a video and
a missed slot is skipped, not backfilled. GitHub's cron is late and lossy (see the comments in `markets.yml`), so
schedule three shots per slot and let `due` stand the extras down, and let the cron-job.org pinger drive it the way
the editions are driven.

**Charts for the slides.** The desk's SVG chart code (`markets.py`, the chart function near line 776; the
browser version in `templates/markets_popup.html`) draws exactly what is needed: bars, the 21/50/200-day lines,
the buy range, the RS line. Render it for ^GSPC and ^IXIC with the distribution count and the 50-day distance as
labels, rasterize with the same approach `video.py` uses for slides (Pillow; or Playwright screenshotting the SVG
at 1920×1080). Keep the house palette; do not imitate IBD's chart colouring.

**Narration rules.** As `video_editorial.md`: short sentences, spoken aloud, every number from the payload,
attribute headlines, no predictions ("will"), losses as plainly as wins, "the desk" not "we recommend." Model the
anchor's register on a calm desk editor, not a hype channel. Fixed sign-off after the outro, as the editions have.

## 4. The channel: what only Todd can do

A new YouTube channel is a Google account action; no API creates one. The one-time steps, same pattern as
`youtube_auth.py` and README "The YouTube video":

1. Todd, signed in to the Google account that owns Boring Money: YouTube → profile → Switch account → **Create a
   channel** (a Brand Account), named **Beehive Wire**. Upload the icon from `static/icon-512.png` and the social
   card from `site/social-card.png` as banner.
2. The existing OAuth client works for any channel on the account. Todd runs
   `python youtube_auth.py --client-id <ID> --client-secret <SECRET>` on his own machine and **picks the Beehive
   Wire channel** when asked; it prints a refresh token.
3. That token becomes a new repository secret `YOUTUBE_REFRESH_TOKEN_BW` (keep the Boring Money one for the
   editions). Todd cannot currently sign in to GitHub (lost password; "Forgot password?" on github.com/login
   recovers it), so either he recovers it, or the token goes into the Cloudflare/Claude-app secrets and the
   upload is done from a session rather than from Actions. Decide with him.
4. `config.yaml` → a second `video_desk:` block (or `channel:` key) with `expect_channel: "beehive wire"`; the
   upload refuses any other channel, as today.
5. Until the channel exists, the roundup can go to Boring Money under the existing credentials so the pipeline
   is proven; moving it is a config change.

Also open: whether the twice-daily edition videos should move to the Beehive Wire channel too. Todd's phrase was
"that would be the best thing." Raise it with him once the channel exists; it is the same config change.

---

## 5. Deliverables

- `market_video.py` (or a mode in `video.py`) with `due`, `--emit-payload`, `render`, `upload`.
- `market_video_editorial.md`.
- `.github/workflows/market-video.yml`, triggered after the markets edition pass, with a dry-run input.
- The front-page slot and the gate-page link for the latest roundup.
- A dry-run mp4 sent to Todd with `SendUserFile` before the first real upload (he cannot easily fetch GitHub
  artifacts).
- A line in `CLAUDE.md` "Open items" when done, and the session renamed per the standing order.
