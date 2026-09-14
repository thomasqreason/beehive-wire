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

## Notes

- Scheduled workflows pause after 60 days without a commit; the bot's state commits keep the repo active.
- Feeds die. `python check_feeds.py --prune` once a month keeps the list clean; a dead feed never breaks a build.
- The `<meta http-equiv="refresh">` in the template reloads the page in the reader's browser once an hour.
