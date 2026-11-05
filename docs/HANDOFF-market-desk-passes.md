# HANDOFF — Market Desk: premarket pass, rolling articles, weighted distribution days

Written 2026-11-05 for the implementing session (Opus). Todd's asks are quoted verbatim in section 1;
everything after that is the spec, the code map, the decisions already made, the gotchas and the test plan.
Repo: `C:\Users\toddr\Documents\beehive-wire` (remote `thomasqreason/beehive-wire`, main). Last push
d239a485 (Nov 4). The tree has UNCOMMITTED work from a different task (the 10 AM video): `morning_video.py`,
`.github/workflows/morning-video.yml`, the `rotation:` block in `markets.yaml`, a `morning_video:` block in
`config.yaml`, `.gitignore`, and `relay/quote-worker.js` (`?long=` endpoint). Leave all of it in place; do not
revert it; commit it separately or not at all.

Read first: `markets_desk.md` (writer's orders), `.github/workflows/markets.yml`, `markets.py` (1,898 lines;
the function list is in section 3), `templates/markets.html`, `templates/markets_movers.html`,
`templates/markets_popup.html`, `desk_extras.py`, `markets.yaml`.

---

## 1. Todd's asks (verbatim, 2026-11-05)

> 1. let's do the first update at 7:15 am and show some early movers.. so change after the close to early movers.
> 2. at 7:15 am also update todays date at the top.
> 3. update the volume surges at live as usual starting at 7:45
> 4. actually let's call early mover "pre market" or whatever is gramatically correct.
> 5. on pre market and after hours make the chart like the rest of them showing how far from a pivot point etc
>    just like the chart for volume surges.
> 6. if it's not already there, put the next earnings date somewhere in the grapgh for all stocks even the ones
>    that i enter in on the quote box
> 7. for market status, let's qualify some of the distribution days and weight them.. maybe barely more volume
>    vs heavy, barely down in price vs sharply, and stalling days are even. so discount one and add to the other.
>    Got it? and run a few new articles each update and drop off a few. so for the articles space the timing
>    of them out evenly.
>
> and have it update the first run immediatly and from then on, on a schedule

All times below are Mountain unless marked. "Pivot" = buy point. "Chart" in ask 5 = the Volume Surges table
(columns Sym / Last / Chg / Vol vs avg / RS / Chart / Buy point / vs buy pt), not the SVG.

Standing rules that still apply to every line of copy (do not re-litigate):
- Headlines tell the COMPANY's story, never chart jargon (no buy point / pivot / clears / breakout / base / RS /
  50-day in a headline). `markets_desk.md` "The voice".
- Volume is information, not a condition. No volume gate anywhere, no "unconfirmed" breakouts.
- Copy is for experienced investors: numbers, no explaining, no asides.
- "Picks" not "calls" in anything the scorecard prints.

---

## 2. The new day (what the page does, hour by hour)

| Mountain | Pass (`mode`) | Prints |
|---|---|---|
| 7:15 am | `premarket` | **Premarket Movers** table at the top of the page (replaces After Hours in that slot). Page date and stamp become TODAY. Market note refreshed (prior close's state + index futures). Writer pass 1: 3 new stories. |
| 7:45 am | `movers` | First Volume Surges pass of the day (was 8:05). The live screen (`markets_live.js`, every minute during the session) keeps running as it does now. |
| 11:05 am | `midday` | Volume Surges, then writer pass 2: 3 new stories. |
| 2:05 pm | `movers` | Volume Surges with closing numbers (unchanged). |
| 3:20 pm | `edition` | **After Hours** table at the top (same format as Premarket), writer pass 3: 4 new stories, market note, Desk Chief, Company of the Day, The Lesson (Fri), Beehive 100 / Big Cap 40 (Fri). |

Article passes at 7:15, 11:05 and 3:20 are about four hours apart: that is "space the timing of them out
evenly". Three Opus writer calls a day instead of one (the Desk Chief stays once a day, at the close).
Subscription usage only, no new money; each writer call is 5-10 minutes of CI.

Story counts, keep-count and slot times live in `markets.yaml` so Todd can retune without code:

```yaml
desk:
  stories_keep: 10          # stories shown on the index page (newest first), across today and yesterday
  passes:                   # Mountain time; markets.py due + markets.yml read these
    premarket: {at: "07:15", stories: 3}
    movers_open: {at: "07:45"}
    midday:    {at: "11:05", stories: 3}
    movers_close: {at: "14:05"}
    close:     {at: "15:20", stories: 4}
extended:                   # Premarket / After Hours tables
  min_move_pct: 2.0
  min_price: 10
  min_volume: 20000         # extended-session shares, drops one-print noise
  rows_up: 20
  rows_down: 10
distribution:               # see section 6
  window_sessions: 25
  barely_down_pct: 0.5
  sharply_down_pct: 1.5
  barely_more_vol: 1.05
  heavy_vol: 1.20
  adj: 0.25
  stall_max_up_pct: 0.4
  expire_on_5pct_rally: true
```

---

## 3. Code map (as of d239a485)

`markets.py`:
- `SLOTS` 1762, `due_pass()` 1768, `main()` 1809 (subcommands scan / publish / render / scorecard / run / movers / lists / due).
- `scan()` 473: the edition payload. Pulse + distribution days at ~496-500 (the list comprehension),
  `big_picture()` 645, story choice ~545, `enrich()` 403 (info / quarters / calendar / news for chosen names),
  after hours ~590-606 (`after_hours_window()` 1037, `after_hours()` 1046, `ah_movers` built from the DESK LIST only,
  3%+), extras ~607-625 (`next_earnings_cached`, `desk_score`, `earnings_week`, scorecard, alerts), `save_series()` 1388.
- `movers()` 1205: Volume Surges over `broad_universe()` (~610 names), 2y download, `rs_all` → `rs_rank_of`,
  rows with `analyze()` on candidates, `save_series(mv_facts)`, webster screen, alerts, `save_screen`, `refresh_lists`,
  after-hours column when `not live and after_hours_window(now)`. `save_movers()` 1459 writes `movers-latest.json` +
  `movers/<YYYY-MM-DD-HHMM>.json` and `record_pass("movers", ...)`.
- `publish()` 844: merges writer JSON into the payload, draws `chart_svg()` 748, writes `data/markets/<date>.json`
  (OVERWRITES the whole file), `record_pass("edition", ...)`.
- `render_site()` 918: page date = edition date unless movers-latest is newer (~958-966); copies series docs;
  renders `index.html`, `<date>.html`, frozen `movers/<stamp>.html`, the archive. `KIND_LABEL` 1425.
- `session_fraction()` 1191, `is_trading_day()` 1177, `nyse_early_close()` 1182.
- `analyze()` 158: status / from_pivot logic at 218-234 (status from the LAST CLOSE vs buy point).
- `BOARD_KEYS` 470, `STATUS_LABEL` 339.

`desk_extras.py`: `next_earnings_cached()` 244 (fund_cache/SYM.cal.json, 2-day cache), `earnings_week()` 269,
`big_picture_alert()` 367, `desk_score()` 459, `webster_screen()` 379.

Templates (edit `templates/`, never `site/`): `markets.html` — stamp line 19, pulse 23-33, Market Note 36-38,
After Hours block 40-56, Volume Surges 58-89 (`mvtable` macro at 66-80, `#mv-live` wrapper), Earnings This Week
104-120, nav 122-134, `art` macro 140-200 (fact box 152-175, `fu.next_earnings` line 175), stories loop 203-204.
`markets_movers.html` — the same `mvtable` for frozen pages. `markets_popup.html` — `chart()` 35 (SVG labels at
53/56/70/80), `render()` 200-215 (`d.next_earnings` fact line 208), `live()` 221-232 (relay `?s=` quote),
`fresh()` 190-199 (typed ticker: relay `?hist=` + `BWA.analyze`). `markets_style.html` — CSS.

Workflow `.github/workflows/markets.yml`: crons (both DST hours, three shots 20 min apart), `check` job runs
`markets.py due`, `desk` job: "Decide the pass" fallback by UTC hour, Stocks on the Move (every pass), Scan,
Write (claude -p, `--max-turns 16`, Opus), Desk chief, Print, Lists (Fri), Render, Commit `data/markets`, Deploy.
`build.yml` `check` job (lines 66-81) also runs `markets.py due` hourly at :50 (cron-job.org pinger) and dispatches
`markets.yml -f mode=<due> -f catchup=true`.

Relay `relay/quote-worker.js` (Cloudflare Worker; TODD PASTES IT, the repo copy is not deployed by CI):
`?s=` v7 quote (`quoteFeed()` 237, `merge()` 249), `?hist=`, `?long=` (uncommitted), `?intra=`, `?q=`.

---

## 4. Premarket and After Hours tables (asks 1, 2, 4, 5)

Name: **"Premarket"** (AP style: one word, no hyphen). Section heading "Premarket Movers"; the evening one stays
"After Hours". Archive labels: Premarket / Midday / Edition.

One function for both, in `markets.py`:

```
extended_movers(kind, prices, bench, cfg, rs_rank_of) -> dict   # kind = "pre" | "post"
```
- Universe: `broad_universe(cfg)` (S&P 500 + Nasdaq-100 + desk list), same 2y daily download the movers pass
  already makes (RS rank and `analyze()` need it). After Hours moves from desk-list-only/3% to this universe
  and `extended.min_move_pct` (2%).
- Tape: `yf.download(part, period="2d", interval="5m", prepost=True, ...)` in chunks of 60, like
  `after_hours()`. Filter to TODAY's Eastern date, and to pre-session bars (`et.hour < 9 or (et.hour == 9 and
  et.minute < 30)`) for "pre", post-session (`et.hour >= 16`) for "post". Last print vs the prior official
  close (the last daily bar). Use `period="2d"` not `"1d"`: at 9:15 ET Yahoo's "1d" may still be yesterday.
  GOTCHA: at 7:15 MT the daily download has NO bar for today, so `today` (as movers() computes it) is
  yesterday; the premarket pass treats everything as "prior close + premarket print". Do not reuse the
  `d.index[-1].date() != today` skip from movers() unchanged.
- Row shape = the Volume Surges row (`mvtable` columns) plus the extended print:
  `sym, close (prior close), ext_price, ext_chg_pct, ext_vol, ext_as_of, rs_rank, status, angle, buy_point,
  buy_max, base_kind, base_weeks, from_pivot, next_earnings`. `from_pivot` is measured FROM THE EXTENDED PRICE
  (`pct(ext_price, buy_point)`): that is ask 5, "how far from a pivot point". Factor the status thresholds out of
  `analyze()` 218-234 into `status_at(price, base, ...)` and call it with the extended price for the Chart column
  label (so a name trading through its buy point premarket reads "in buy range" there); the chart pop-up keeps
  the closing status.
- Qualifiers: `|ext_chg_pct| >= min_move_pct`, `close >= min_price`, `ext_vol >= min_volume`. Top `rows_up` up
  and `rows_down` down by |move|. Also a `desk` flag per row when the name is on the desk list (green like story names).
- `save_series(facts)` for the candidates so every ticker opens a chart.
- Save: `data/markets/extended/<date>-pre.json` and `<date>-post.json`; `extended-latest.json` for the page.
  `record_pass("premarket", ...)` for the morning one (the After Hours one is part of the edition pass, as today).
  Keep `ed["ah_movers"]` in the edition JSON (same new row shape) so old `<date>.html` pages still render.
- Template: ONE block at the top (where After Hours is now, lines 40-56) rendered with the `mvtable` macro
  (add an "Extended" column: price + chg, labelled "Premarket" or "After hrs"; add an "Earnings" column).
  Show whichever is freshest: premarket when `extended-latest.json` is kind "pre" and dated today, else the
  edition's after-hours rows. Legend: "Premarket Movers, as of 9:14 AM ET, against yesterday's close. 612
  names screened; 2%+ moves on 20K+ shares." Same for After Hours against the 4 p.m. close.
- Page date (ask 2): in `render_site()` ~958-966 the page is dated by the freshest pass. Add the premarket
  artifact to that rule: `page_date = max(edition date, movers date, premarket date)`; `stamp_time` from the
  premarket `as_of`. The "the articles below are from Wednesday's close" note stays only until the premarket
  writer prints (section 5 replaces it with per-story stamps).
- Market note at 7:15: add `pulse["futures"]` from yfinance `ES=F`, `NQ=F`, `YM=F` (last print vs prior
  settle, one bulk `period="2d", interval="5m", prepost=True` call) and let the premarket writer use it
  ("S&P futures +0.4% at 9:14 ET"). If the futures call fails, the note just uses the prior close.
- `MARKETS_PRE=1` forces the premarket tape outside the window (like `MARKETS_AH=1`), for local tests.

Volume Surges (ask 3): only the first slot moves, 8:05 → 7:45 (`session_fraction` at 9:45 ET is 0.08 after
the floor, so the projection is x12; that is the usual early-read noise and the live screen refines it every
minute). 11:05 and 2:05 unchanged.

---

## 5. Rolling articles (the last ask)

Today: one edition at 3:20 with 5-10 stories; `publish()` overwrites `data/markets/<date>.json`.

New:
- `scan()` takes `--pass premarket|midday|close` (default close). The payload carries `pass`, `as_of`
  (UTC ISO) and, for premarket, `premarket: {price, chg_pct, as_of}` on each story (like `after_hours`);
  for midday, `intraday: true` and the facts come from the daily download's PARTIAL bar for today (movers()
  already relies on that bar during the session).
- Story choice per pass: `desk.passes[pass].stories` names from `ranked`, EXCLUDING any symbol with a story
  in the last `stories_keep` stories (today + yesterday) unless something happened to it: a breakout today
  (`days_since_breakout == 0`), an extended-session move of 3%+, or a status change since its last story.
  Premarket ranking: biggest premarket movers with news first (earnings reactions), then setups into the open
  (breakout / buy_zone / setting_up by score). Midday: by `story_score` on the partial bar. Close: as now.
  `enrich()` the chosen names (the news list is what the headline is built from).
- `publish()` gains append semantics: load today's JSON if it exists; each new story gets `pass` and
  `printed_at`; a re-covered symbol REPLACES its earlier story; `stories` is the union, sorted by `printed_at`
  descending; `passes_done[pass] = printed_at`. `market_note` is overwritten by every writer pass (premarket
  and midday notes are 2 sentences; see `markets_desk.md` change below). `chief`, `profile`, `corner` only
  from the close pass. `generated` = the latest pass's time; keep `edition_generated` for the close pass.
- Index page: the newest `stories_keep` stories across today's and yesterday's JSON, newest first; each
  article's kicker gets a stamp ("Premarket · 7:31 AM MT", "Midday · 11:22 AM MT", "Close · 3:41 PM MT").
  Stories that fall off the index page stay on their `<date>.html` (which shows every story of that day) and
  in the archive search (`record_pass` per writer pass: kinds `premarket`, `midday`, `edition`).
  The ticker nav (lines 122-134) follows the visible stories.
- `desk_candidates()` in `build.py` reads `ed["stories"]` and works unchanged; set each candidate's
  `published` from the story's `printed_at` instead of `ed["generated"]`.
- `markets_desk.md`: add a "Passes" section. `pass` is in the payload. Premarket stories LEAD with the
  premarket print and the overnight news, with the prior close second (the After Hours paragraph already
  does this for the evening; mirror it). Midday stories say "as of 11:05 a.m. Mountain" once and treat the
  partial bar as the session so far. Close: unchanged. Market note for premarket/midday: two sentences, the
  indexes/futures and the weighted distribution read. Story count = the payload's count, same order, same tickers.
- Scorecard: `collect_calls()` in `desk_extras.py` walks the edition JSON stories; with three passes a day,
  dedupe calls per (sym, base) per day so one name does not get three picks in one day.

---

## 6. Weighted distribution days (ask 7)

Replace the list comprehension in `scan()` (~496-500) with `distribution_days(df, cfg)` (put it in
`desk_extras.py`), returning one row per flagged session in the last `window_sessions`:

```
{date, chg_pct, vol_ratio (vol / prior vol), kind: "distribution" | "stalling", weight, why}
```

- Distribution day: close down ≥ 0.2% on volume above the prior session (today's rule). Weight =
  `1 + price_adj + vol_adj`:
  - price_adj: −adj if the drop is under `barely_down_pct` ("barely down"), +adj if ≥ `sharply_down_pct`
    ("sharply down"), else 0.
  - vol_adj: −adj if vol_ratio < `barely_more_vol` ("barely more volume"), +adj if ≥ `heavy_vol` ("heavy
    volume"), else 0.
  - Range 0.5 to 1.5. `why` is the two labels joined ("barely down, heavy volume").
- Stalling day (NEW; Todd: "stalling days are even"): close between −0.2% (exclusive) and `+stall_max_up_pct`,
  volume above the prior session, close in the lower half of the day's range, and the prior session closed up
  (the index was advancing). Weight 1.0, `why` = "stalling".
- `expire_on_5pct_rally`: drop a day once the index has closed 5%+ above that day's close (the standard
  expiry). Default on.
- `pulse[key]` gets `dist_days` (raw count, unchanged meaning), `dist_weighted` (sum of weights, one decimal)
  and `dist_detail` (the rows). `big_picture()` reads `dist_weighted` with the SAME thresholds as today (≤4
  confirmed uptrend, ≤6 under pressure, else correction); note text becomes "S&P 500 5 distribution days
  (weighted 4.3), Nasdaq 6 (6.5), over the last 25 sessions."
- Page: the pulse line (lines 23-33) prints "5 dist. days (4.3 wtd)"; under the Market Note line add a small
  per-index table of the flagged days: date, chg, vol vs prior, kind, weight, why. Writer: quote the weighted
  number and say "weighted" once.
- `big_picture_alert()` compares state labels only; unchanged. Grep `dist_days` across `markets_live.js`,
  `morning_video.py` and `promo2.py` for any second copy of the count and point it at the new function.

---

## 7. Next earnings date on every chart (ask 6)

Where it is today: the article fact box (`fu.next_earnings`, from `enrich()`), the pop-up fact box
(`d.next_earnings` from the series doc), the After Hours table, Earnings This Week. Only `scan()` fills
`f["next_earnings"]`, so series docs written by the movers/lists passes have none, and a ticker typed into
the quote box has none.

Do:
1. Series docs: in `movers()`, `extended_movers()` and `leader_lists()` set
   `f["next_earnings"] = next_earnings_cached(f["sym"])` for every fact that goes through `save_series()`
   (cached two days per symbol in `fund_cache/SYM.cal.json`; one Yahoo call per uncached name, ~60 a pass;
   `time.sleep(0.05)` between calls as scan() does).
2. Typed tickers: the pop-up's `live()` already fetches the relay's `?s=SYM` (Yahoo v7 quote) for every chart
   it opens. Yahoo's v7 quote carries `earningsTimestamp`, `earningsTimestampStart`, `earningsTimestampEnd`
   (unix seconds). In `relay/quote-worker.js` `merge()` add `earnings: {ts, start, end}` (via `num()`), and in
   `live()` if `d.next_earnings` is empty set it from `q.earnings` (prefer `start`, else `ts`; format
   YYYY-MM-DD in America/New_York) and insert the fact line + the chart label. Todd pastes the worker into
   Cloudflare (beehive-quotes → Edit code → Deploy); until then typed tickers simply show no date.
3. On the chart itself, both renderers: `chart_svg()` in `markets.py` and `chart()` in `markets_popup.html`.
   Draw a label at the top-right of the price panel: "Earnings Nov 19 · 9 sessions" (sessions counted with
   `is_trading_day` server-side; weekdays client-side), class `lbl earn`, and class `soon` (coral) inside 5
   sessions. If the date is unknown, draw nothing. Add the CSS to `markets_style.html`.
4. Columns: "Earnings" in the Premarket / After Hours table (section 4) and in `mvtable` (Volume Surges).

---

## 8. Schedule, catch-up and the first run

`SLOTS` / `due_pass()` (markets.py 1762-1806) read `desk.passes` from `markets.yaml` and detect each pass by
its artifact:
- `premarket` → `data/markets/extended/<today>-pre.json` exists.
- `movers_open` / `movers_close` → `movers/<today>-HHMM.json` at or after slot − 10 min (as now).
- `midday` → today's edition JSON has `passes_done.midday`.
- `close` (mode `edition`) → `passes_done.close` (NOT `generated`, which now moves with every pass).
A later edition still satisfies an earlier movers slot (as now). Return the EARLIEST overdue pass; edition wins
only over movers slots (as now), never over an unprinted midday (midday then runs at the next shot).

`markets.yml`:
- `mode` choices: premarket, movers, midday, edition, lists, corner.
- Steps: `premarket` = `python markets.py premarket` (table, page date, futures) → scan `--pass premarket`
  → Write → publish `--append` → render. `midday` = movers → scan `--pass midday` → Write → publish
  `--append` → render. `edition` = movers → scan `--pass close` → Write → Desk chief → publish → lists (Fri)
  → render. The "Decide the pass" fallback by UTC hour must know the new slots.
- Crons (UTC; both DST hours; three shots 20 min apart; `due` stands the extras down):
  - 7:15 MT: `15,35,55 13,14`
  - 7:45 MT: `45 13,14` and `5,25 14,15` (the existing 14/15 lines already cover :05/:25/:45)
  - 11:05, 2:05, 3:20: the existing lines.
  Update the header comment. The cron-job.org pinger (build.yml at :50) covers UTC 09-14 and 21-04 only;
  nothing pings 15:50-20:50 UTC, so the 11:05 midday pass still leans on GitHub's own cron (Todd was asked on
  10/12 to add hours 15-20 to the pinger job; unconfirmed). Hand rescue:
  `gh workflow run markets.yml --ref main -f mode=midday`.

**First run immediately (Todd's last line).** The moment the push lands, dispatch by hand the pass that
belongs to the current hour (`gh workflow run markets.yml --ref main -f mode=premarket|movers|midday|edition`),
watch it, and check beehivewire.com/markets/ (gated; Todd has a code). Outside market hours dispatch
`mode=edition` so the page carries the new After Hours table and the new story stamps tonight, and the 7:15
cron opens the new cycle next trading day. From then on the schedule and `due` own it.

---

## 9. Gotchas (all learned the hard way)

- Edit `templates/`, never `site/`. `python build.py --render-only` rewrites the newest `archive/*.html`;
  `git checkout -- archive/` after any test render.
- A LOCAL `publish` writes that date's edition JSON and loses the Opus prose on it. With append semantics this
  is less destructive, but still: `git checkout -- data/markets` before committing anything from a local test.
- Local `claude -p` failed on this machine 10/24 (OAuth expired). The CI token is a separate secret
  (`CLAUDE_CODE_OAUTH_TOKEN`). Test the writer step in CI, not locally; `markets.py run` publishes template prose.
- Local render with the gate: stub `desk_gate.gate_site` (or set `gate.enabled: false` in a scratch copy of
  `markets.yaml` — never commit that), serve `site/` on :8000 (the relay's CORS allows only :8000 locally).
- Pushes get rejected often (the bot commits every pass): `git pull --rebase` first. Commit `data/markets`,
  `data/state.json`, `archive` as the workflow does.
- `build.yml` ignores pushes that touch only `data/**` or `archive/**`; a code/template change forces a render.
- `markets.yaml`: the ticker `ON` must stay quoted.
- Series docs (`data/markets/series/*.json`, ~340 files, ~4 MB) are committed and overwritten daily; the
  premarket candidates add a few dozen. Watch repo growth.
- yfinance `prepost=True` 5-minute bars: `after_hours()` was only ever exercised on the post tape. Exercise
  the pre tape during a real premarket (4:00-9:30 ET) before trusting it; `MARKETS_PRE=1` is for plumbing tests.
- DST: Mountain fell back Nov 1; every slot is listed in both UTC hours and `due` (Mountain time) decides.
- Early-close days (`nyse_early_close`): the 2:05 pass is after the 1 p.m. ET bell either way; premarket unaffected.
- Headline / volume / audience rules in section 1 apply to the writer orders you add to `markets_desk.md`.

---

## 10. Test plan (do all of it before asking Todd for the push)

1. `python markets.py premarket` (with `MARKETS_PRE=1` outside the window) → `data/markets/extended/<date>-pre.json`
   and `extended-latest.json`; render → the index page is dated today at the top, the Premarket Movers table
   is first, columns match Volume Surges plus Extended and Earnings, `vs buy pt` is from the premarket price.
2. `python markets.py scan --pass midday --out data/build/markets_payload.json` → payload has `pass`,
   `as_of`, excludes symbols with a story in the last 10; `python markets.py publish --payload … --append`
   keeps the earlier stories; render shows the newest 10 with stamps, `<date>.html` shows all of the day's.
3. `python -c "from markets import due_pass; …"` at simulated Mountain times (7:00, 7:20, 7:50, 11:10, 14:10,
   15:25, each with and without the artifact present) → the right mode or none. Weekend/holiday → none.
4. Distribution: the raw counts equal the previous edition's `dist_days`; synthetic series: a −0.3% day on
   +2% volume → 0.5; −2.0% on +30% → 1.5; a stalling day → 1.0; a day the index then rallied 5% from → expired.
   The Market Note table renders for both indexes.
5. Earnings label: visible on an article SVG and in the pop-up for a scanned name; a movers-only name shows it
   after the pass; a typed ticker shows it only once the worker is deployed (test the worker change with
   `wrangler dev` or by pasting; the response must carry `earnings`).
6. Full render with the gate stubbed, served on :8000; screenshots of: top of page (date, Premarket table),
   the pulse/Market Note table, a story with its pass stamp, the pop-up with the earnings label.
7. `git checkout -- archive/ data/markets` of anything a local test wrote that is not the intended data change.
8. Commit; Todd (or this session, if the push is allowed) pushes on his go-ahead; then section 8's "first run
   immediately". Watch the run; confirm the page; log it in `project_log.md` and the handoff status below.

---

## 11. Decisions made for Todd (override by editing `markets.yaml`, not code)

- Story counts 3 / 3 / 4 at 7:15 / 11:05 / 3:20; page keeps 10. Midday writer shares the 11:05 run rather than
  adding a fourth slot.
- Premarket/After Hours: broad universe, 2%+ moves, 20K+ extended shares, 20 up / 10 down.
- Weights ±0.25 per qualifier (0.5-1.5 range); stalling = 1.0; thresholds 0.5% / 1.5% price, 1.05x / 1.20x
  volume; 5%-rally expiry on. `big_picture()` thresholds unchanged (≤4 / ≤6).
- Index futures in the 7:15 market note (ES=F, NQ=F, YM=F).
- "Premarket" as one word.

## 12. Status

- 2026-11-05: handoff written. Nothing implemented. Not committed.
- 2026-11-05 (afternoon): sections 4-8 built and committed on main, NOT pushed (waiting on Todd's go-ahead).
  Test plan run locally against the live tape (premarket at 9:25 ET, midday at 11:35 ET, close and after hours at
  4:05-4:10 ET): every item passed; test writes reverted. Raw distribution counts recomputed as of the Nov 4 close
  equal the printed edition (S&P 6, Nasdaq 5, Dow 3, same dates); weighted 5.8 / 6.2. `due` passes 20 simulated
  cases. Where the build differs from the spec, and why:
  - Yahoo's 5-minute tape carries NO share volume before the open (every bar reads 0; the quote and info carry
    none either), and none for some names after the close. `extended.min_volume` (20K) applies where volume is
    reported; elsewhere a name must have traded in `extended.min_prints` (6) five-minute bars. The table's fifth
    column is "Last print" (time, plus shares when reported) instead of shares.
  - Futures sit at the payload's top-level `futures`, not inside `pulse` (the page and market_video.py iterate
    `pulse` as index rows).
  - The chart's earnings tag rides the legend line, top LEFT ("21-day 50-day 200-day · Earnings Feb 2 · 59
    sessions"): top right sat on the BREAKOUT marker of a fresh breakout, the charts where it matters most.
  - The close pass publishes with `--append` too, so the day's premarket and midday stories stay on its date page.
  - A midday pass writes `data/markets/tried.json` when it starts; `due` counts a started-but-failed midday as
    done, so a broken midday cannot hold the 3:20 edition behind it. Premarket lapses at the 7:45 slot.
  - Until today's close prints, the index page keeps the last Desk Chief, Company of the Day and Lesson
    ("From Wednesday's close."), as it did before when the whole page was yesterday's edition.
  - market_video.py waits for `passes_done.close` (today's file now exists from 7:15 a.m.).
  - Story and desk-list names in the tables now actually print green (a global link colour had overridden it).
  - Scorecard copy says picks, not calls.
  Still open: Todd pastes relay/quote-worker.js (typed tickers show no earnings date until then); after the push,
  section 8's first run (dispatch the pass for the current hour, watch it, check the page). The writer's first
  real premarket/midday prose is only testable in CI. The no-writer fallback headlines (template_article) still
  use chart words ("Breaks Out Past ... Buy Point"); they print only when the writer fails.
- 2026-11-05: the queued second job (docs/HANDOFF-market-desk-watchlist-alerts.md) is built as its own commit on
  top of this one; both wait on one push. The relay paste carries both jobs' worker changes.
- 2026-11-05 2:45 PM MT: pushed on Todd's go-ahead (13d6aa5c, b523d2f8). First run: the push's build ran the new
  `due`, found no midday pass today and dispatched one (run 42115520502): all steps green, Opus wrote GTLB / TSLA /
  PLTR, the After Hours table screened 583 names, deployed. The 3:20 close pass is the schedule's. Follow-up
  f873c781 (writer: midday time from `as_of`; a late catch-up after the bell is the close) waits on a push.
