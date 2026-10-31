# Handoff: a free daily stock-market roundup video, and a Beehive Wire YouTube channel

**For:** a fresh Claude session in the `beehive-wire` repo.
**From:** Todd Ridgway (publisher), Oct 31, 2026.
**Read first:** `CLAUDE.md`, then `docs/HANDOFF-market-desk-promo.md` (the product, the method, the naming rules),
then this file. The promo handoff's rules on names (no IBD feature names; attribute the method to O'Neil; not
affiliated with IBD) apply here too.

---

## 1. What Todd wants

A **free daily video**, every trading day after the close: a stock-market roundup in the Beehive Wire voice,
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
- Ends with the paid-desk pitch and the price: $9.99 for the first 21 days, then $49.99 a month, link in the
  description. Same attribution/disclaimer lines as the promo (see the promo handoff, §5 and `promo.py`).

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

## 3. Suggested shape (4–6 minutes)

1. **Market note** (30 s): the indexes, the three-state read, the distribution count. One sentence of the
   Desk Chief's exposure call.
2. **Breakouts and buy-range names today** (60–90 s): from the watch lists. Name, what the base was, that it
   cleared the pivot. Three to five names. A chart slide for the best one.
3. **Volume Surges** (45 s): the top handful, with the ratio to the 50-day average, reported plainly.
4. **One story from the edition** (45 s): the top stock story, read the way the edition videos read headlines.
5. **Scorecard line** (15 s): how the desk's calls stand this week or month, win rate, versus the S&P. The
   honesty beat. Include losses.
6. **Tomorrow** (15 s): the Desk Chief's "watch" line, earnings this week.
7. **Pitch** (15 s): the paid desk, the price, the link. Fixed sign-off.

Script written by Claude each day from a payload (`--emit-payload`), with a `market_video_editorial.md` of
standing orders modelled on `video_editorial.md`. Hard rules: every number from the payload; no predictions, no
"will"; losses stated as plainly as wins; "the desk," never "we recommend"; the disclaimer in the description.

Title pattern: `BEEHIVE WIRE MARKET DESK: <the day's one-line read> (<Mon Oct 27>)`. Thumbnail: the brand card
with the day's headline number (the S&P's move, or the breakout count).

---

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
