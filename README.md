# Beehive Wire

A Drudge-style front page that edits itself — and only twice a day, on purpose.

The **morning edition** lands at 7:00 a.m. Mountain and the **evening edition** at 6:00 p.m., the way a
paper used to arrive. A GitHub Action pulls ~100 RSS feeds, hands the fresh stories to Claude with the
standing orders in `editorial.md`, turns over about half the page, and republishes one static HTML file to
GitHub Pages. No server, no database, no infinite scroll, and nothing new between editions: you read it,
you're caught up, you go do your work. The page tells the reader when the next edition lands so they stop
checking, and there is deliberately no auto-refresh in the HTML.

```
feeds.yaml ──► build.py --emit-payload ──► Claude Code (editorial.md) ──► plan.json
                                                                            │
                              site/index.html ◄── build.py --plan-json ◄────┘  ──► GitHub Pages
```

The build runs in three steps so the thinking happens in Claude Code: Python gathers the wire and picks
the candidates, Claude reads `editorial.md` and writes the edition plan, Python applies the plan and
renders. If the Claude step fails the workflow re-renders the edition already on the page, so the site
never goes blank.

## Deploy (10 minutes, free)

1. Create a **public** GitHub repo (public repos get unlimited Actions minutes) and push these files.
2. Run `claude setup-token` locally to mint a subscription token, then add it at repo
   **Settings → Secrets and variables → Actions → New repository secret** as `CLAUDE_CODE_OAUTH_TOKEN`.
3. Repo **Settings → Pages → Build and deployment → Source: GitHub Actions**.
4. **Actions** tab → `build` → **Run workflow**. The page is live at `https://<you>.github.io/<repo>/` a minute later,
   and rebuilds itself every morning and afternoon from then on.
5. Custom domain (already set to `beehivewire.com` in `config.yaml`, which writes `site/CNAME`):
   at the registrar, add four A records for the apex — `185.199.108.153`, `185.199.109.153`,
   `185.199.110.153`, `185.199.111.153` — and a CNAME for `www` pointing at `<you>.github.io`.
   Then repo **Settings → Pages → Custom domain** → `beehivewire.com` → Save, and tick **Enforce HTTPS**
   once the certificate is issued. DNS can take up to 24 hours; HTTPS usually follows within an hour of that.

`data/state.json` ships with a real front page already in it, so the site is full from the first deploy.

## Run it locally

```
pip install -r requirements.txt
python check_feeds.py            # see which feeds are alive (add --prune to drop dead ones)
python build.py --dry-run        # no API key: heuristic picks, headlines = titles → site/index.html
ANTHROPIC_API_KEY=sk-... python build.py
```

## The YouTube video (Boring Money channel)

After each edition prints, `.github/workflows/video.yml` makes a roughly ten-minute video of the top story, the flash
lines and the next dozen links on the page, and uploads it to the Boring Money channel. Twice a day, no extra work.

```
build finishes ─► video.py due ─► --emit-payload ─► Claude (video_editorial.md) ─► script.json
                                                                                       │
        YouTube ◄── video.py upload ◄── video.py render (slides + voice + ffmpeg) ◄───┘
```

White Drudge-style slides with the front-page headline and its source, a synthetic voice reading narration
Claude writes from the headlines (video_editorial.md bars it from inventing facts), chapters and source
links in the description. If the Claude step fails the video still goes out, with the headlines read straight.
Each edition gets exactly one video (`data/video/published.json`), and a missed slot is skipped, not backfilled.

**One-time setup** (about 15 minutes):

1. [Google Cloud Console](https://console.cloud.google.com/): create a project, enable **YouTube Data API v3**,
   configure the OAuth consent screen (External; add yourself as a test user), then create an
   **OAuth client ID** of type **Desktop app**. Note the client ID and secret.
2. On your own computer: `pip install requests`, then
   `python youtube_auth.py --client-id <ID> --client-secret <SECRET>`. Sign in as the Google account that owns
   Boring Money and **pick the Boring Money channel** when asked. It prints a refresh token.
3. Add three repo secrets: `YOUTUBE_CLIENT_ID`, `YOUTUBE_CLIENT_SECRET`, `YOUTUBE_REFRESH_TOKEN`.
   (`CLAUDE_CODE_OAUTH_TOKEN` is already there for the editions.)
4. Check the target: `YOUTUBE_CLIENT_ID=... YOUTUBE_CLIENT_SECRET=... YOUTUBE_REFRESH_TOKEN=... python video.py whoami`
   should name Boring Money. The upload also refuses to run if the channel name does not contain
   `video.expect_channel` in `config.yaml`.
5. Actions tab -> **video** -> Run workflow with **dry_run** ticked. The rendered video is attached to the run;
   watch it. Then run it once without dry_run to post for real.

**Things to know**

- **Private uploads.** Google locks videos uploaded through the API to *private* until the API project passes
  its compliance audit (the form is linked from the YouTube API docs, "Required minimum functionality"). Until
  then every video lands private on the channel and you flip it public by hand, whatever `video.privacy` says.
  Submit the audit once the first few have gone up.
- **Quota.** An upload costs 1,600 of the 10,000 daily units, so two a day is far inside the free limit.
- **Voice.** edge-tts (free Microsoft voices, no key). If Microsoft's service refuses the runner, the render falls
  back to espeak-ng, which works but sounds robotic; switch `video.tts`/`video.voice` in `config.yaml` if you prefer.
- **Content.** Slides are text only, with no publishers' photos. Headlines and sources are credited in the
  description, which links to each story. The description says the voice is synthetic;
  set `video.synthetic_disclosure: true` to also turn on YouTube's altered/synthetic label.
- **Cost.** One short Claude call per video on the same subscription as the editions; hosting and rendering are free Actions minutes.
- Local test, no accounts needed: `python video.py --emit-payload data/video/payload.json && python video.py render --fallback --tts silent`
  writes `data/video/out/video.mp4` (silent). Needs ffmpeg and Pillow.

## Tune it

| File | What it controls |
|---|---|
| `.github/workflows/build.yml` | When the editions run, and which credential pays for them. |
| `editorial.md` | The editor's brain: voice, lens, accuracy rules, rolling-update rules. Edit in plain English. |
| `feeds.yaml` | Sources, topic hints, weights. Google News query feeds are marked `google: true`. |
| `config.yaml` | 50-link page size, 3 flash lines, edition times, swaps per build, story age limits, model, topic mix. |
| `templates/page.html` | The look. One file, inline CSS, no JavaScript. |

## How an edition works

`data/state.json` is the page. Each build the editor sees the current page (with its headlines) plus up to
140 fresh candidates it has not been offered before, and returns the updated page. It keeps existing
headlines verbatim, turns over about `max_swaps_per_run` stories — half the page — re-picks the giant
headline, and drops anything older than `max_story_age_hours`.
`build.py` validates every id, caps churn, and if Claude or the feeds fail it re-renders the current page
unchanged — the site never goes blank, it just keeps the last good edition until the next build.

## Cost

**Nothing beyond a Claude subscription you already pay for.** The workflow authenticates with
`CLAUDE_CODE_OAUTH_TOKEN`, so each edition runs on your Pro/Max/Team plan and counts against that plan's
usage limits rather than API credits. Two editions a day of an ~18K-token prompt is a rounding error
against a Max plan. GitHub Actions is unlimited on public repos and the page is 22KB, so hosting is free
too.

Nothing here pays for search: the stories come from publishers' own RSS feeds, which cost nothing to
fetch. The only billable work is the one Claude call per edition that picks the stories and writes the
headlines.

If you would rather bill an API key — useful if you want the site independent of your personal account —
swap in `build-with-api-key.yml.example`, set an `ANTHROPIC_API_KEY` secret instead, and it costs about
$0.06 per edition, roughly **$3.70/month**. Hourly would be about $30/month, every 30 minutes about $62.

Changing the edition times means changing two places: `site.editions` in `config.yaml` (what the page
says) and the `cron` in `.github/workflows/build.yml` (when it actually builds, in UTC).

## The Market Desk gate (readers with a code)

Since Oct 30, 2026 the Market Desk (`/markets/` and everything under it) is for readers with an access code.
The site is static, so the gate is real encryption, not a hidden div: `desk_gate.py` seals every desk page
at render time (AES-256-GCM; the content key is wrapped once per code with PBKDF2) and the page unlocks in
the browser. A visitor without a code can leave an email, which opens a request to `desk.gate.request_email`
in `markets.yaml`. Nothing issues a code automatically.

**Codes come from two places.**

1. *Hand codes in a repository secret.* `MARKETS_ACCESS_CODES` (repo **Settings → Secrets and variables →
   Actions**): one code per line, case and spaces ignored. These are baked into the pages and work even if the
   relay is down. Change the secret, run the **build** workflow once, and the desk is re-sealed.
2. *Codes the relay keeps* (relay/quote-worker.js, `/gate/`): the ones you mint on the keys page and the ones
   Stripe buyers get. The build hands each edition's key to the relay (secret `GATE_ADMIN_TOKEN`), and the
   gate page asks the relay for it when a reader's code isn't a hand code. Revoking takes effect at once.

**Your keys page:** `https://beehivewire.com/markets/keys/`. Sign in with the admin token (it stays in that
browser). Mint a code for a friend, see every code with who has it and when it was last used, revoke or
restore. Nothing links to this page.

**Paid access (Stripe).** The pitch is **$9.99 for the first 21 days, then $49 a month.** One-time setup:

1. Cloudflare → Workers & Pages → `beehive-quotes` → Settings → Bindings: add a **KV namespace** binding named
   `GATE` (create a namespace called `beehive-gate`). Under Variables and Secrets add three secrets:
   `GATE_ADMIN_TOKEN` (make up a long one; the same value goes in the GitHub secret of that name),
   `STRIPE_SECRET_KEY` (Stripe → Developers → API keys, the secret key) and `STRIPE_WEBHOOK_SECRET` (step 4).
   Then paste the current `quote-worker.js` in and Deploy, as for any relay update.
2. GitHub: add the `GATE_ADMIN_TOKEN` repository secret with the same value.
3. Stripe → Product catalog: a product **Market Desk** with a recurring price of **$49 / month** and a one-time
   price of **$9.99** named "First 21 days". Then **Payment Links → New**: add the $49 monthly price with a
   **21-day free trial**, add the $9.99 one-time price to the same link, and under *After payment* choose
   "Don't show confirmation page" with the redirect
   `https://beehivewire.com/markets/welcome/?session_id={CHECKOUT_SESSION_ID}`. Copy the link into
   `markets.yaml` → `desk.gate.payment_link`; the Subscribe button appears on the gate at the next build.
4. Stripe → Developers → Webhooks → Add endpoint `https://beehive-quotes.toddridgway.workers.dev/gate/stripe`
   with the events `customer.subscription.deleted` and `customer.subscription.updated`. Copy its signing secret
   into the Worker secret `STRIPE_WEBHOOK_SECRET`. A cancelled or unpaid subscription then revokes its code.

Buying: pay on the Stripe page → land on the welcome page → the code is on screen and that browser is already
signed in. The same checkout always returns the same code. Stripe emails the receipt; nothing else is sent.

Not gated: the chart data files (`series/*.json`, `screen.json`, `rs_dist.json`), which are bare numbers.
Turn the gate off with `desk.gate.enabled: false`.

## Notes

- Scheduled workflows pause after 60 days without a commit; the bot's state commits keep the repo active.
- Feeds die. `python check_feeds.py --prune` once a month keeps the list clean; a dead feed never breaks a build.
- The `<meta http-equiv="refresh">` in the template reloads the page in the reader's browser once an hour.

<!-- Oct 18, 2026: pulled two re-dated obituaries (Parton, Beard) off the live page; see the obituary-trawler commit. -->
