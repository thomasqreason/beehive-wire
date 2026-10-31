# Handoff: the Market Desk promo video, the pitch, and a trademark review

**For:** a fresh Claude session in the `beehive-wire` repo.
**From:** Todd Ridgway (publisher) by way of the Oct 27–31, 2026 session.
**Read first:** `CLAUDE.md` (standing orders), then this file. Do not spend time on the back office: the
paywall, Stripe, Cloudflare and GitHub plumbing are done and documented in README under "The Market Desk gate."
This brief is about the FRONT of the house: what the Market Desk is, how to sell it, the video, and what on the
site might get Todd sued.

---

## 1. What you are being asked to do

1. **Make a better promo video** for the Market Desk than the one in `promo.py` (which is a 67-second slide
   deck with a synthetic voice: serviceable, "pretty lame" in Todd's words). Same pipeline is fine (cards +
   Kokoro voice + ffmpeg, see `video.py` and `promo.py`), but the script, structure and visuals should actually
   show the product. Target 2–4 minutes. It goes on the Boring Money YouTube channel and on the gate page at
   `beehivewire.com/markets` (set `desk.gate.video_id` in `markets.yaml` after upload; `.github/workflows/promo.yml`
   renders and uploads on demand).
2. **Audit the site for terms and features that may be Investor's Business Daily (IBD) property**, and tell Todd
   plainly what to rename or drop before he sells access. He does not want to be sued. Section 5 below is your
   starting list; walk the live site and the templates and add to it.
3. Rewrite the pitch copy on the gate page (`templates/markets_gate.html`, the "Subscribe" block) and the
   YouTube description in `promo.py` to match whatever the video says, in the house voice.
4. **Build a preview for people without a code.** Right now a visitor sees only the gate: a code box and a
   Subscribe button, and "people need to be able to see something." Todd's one hard rule: **nothing close to
   real-time. Period.** No live quotes, no today's lists, no current buy points. What is allowed: a frozen
   sample edition from weeks ago, clearly dated, with the real layout (a story, a slice of the Beehive 100,
   a watch list, a chart pop-up, the scorecard as of that date); a short tutorial page or walkthrough that
   explains each section; and the video embedded. Ideas: render one old edition from the archive
   (`data/markets/` holds every past pass and `markets.py render` makes the pages) into a `/markets/sample/`
   folder that `desk_gate.py` leaves unsealed (mark its pages `data-bw-nogate`), stamp every table "SAMPLE,
   as of <date>" with the live numbers blurred or replaced, and link it from the gate page under the video.
   The chart pop-up on the sample must not call the live relay for prices; disable the quote box and the
   live price line there. Treat anything newer than 30 days as live.

Todd's standing rule: if you can do a thing without him clicking through a website, do it. He does not give out
free codes; every reader pays. The only hand-minted code is his own.

---

## 2. The product in one paragraph

The Market Desk is a daily, numbers-first stock page for people who trade **breakouts from bases** the way
William O'Neil taught it: find the market's strongest stocks by relative strength and earnings, wait for a
proper base (cup, cup-with-handle, flat base, double bottom, IPO base), buy when price clears the base's buy
point, add nothing that is more than 5% past the pivot, and sell any position that falls 7–8% below the buy
point. The desk does the screening every trading day from price data, writes five to ten short stock stories
after the close, keeps two ranked leader lists, runs the watch lists intraday, and keeps a public scorecard of
every call it has made. Price: **$9.99 for the first 21 days, then $49.99 a month.** Cancel any time.

The voice is Beehive Wire's: dry, specific, a number in every sentence, no hype. The house line is
"Leaders, bases and breakouts. Numbers first."

---

## 3. Every feature, in the order a reader meets it

Walk `beehivewire.com/markets` with a code (ask Todd for one; he mints them at `/markets/keys/`) or render it locally:
`MARKETS_ACCESS_CODES=test python build.py --render-only` then open `site/markets/index.html` and enter `test`.

**The daily edition (after the 2 p.m. Mountain close)**
- **Big Picture note**: 3–4 sentences on the market's state from the indexes' position against their 50-day
  lines and a distribution-day count (down days on rising volume over the last 25 sessions).
- **Desk Chief**: the one opinion on the page. An editor's pass over every setup the numbers found: a verdict,
  suggested exposure, ranked picks with an entry, names to pass on, and what to watch tomorrow.
- **Five to ten stock stories**: each has the headline, the setup (base kind, weeks, depth), the buy point and
  buy range, distance from the pivot, the breakout date, RS rank, a chart, and 3–5 "what to watch" lines.
  Every number comes from price data; the writer may not invent one.
- **After Hours** (when the edition prints after 5 p.m. Eastern): post-market moves of 2%+ lead the story.
- **Earnings This Week**: desk names reporting, with their setup and distance from the buy point.
- **New America**: one leader profiled a day: what it does, why it's winning, what the chart says.
- **Investor's Corner**: a 400–600-word lesson on one rule, taught on that day's chart.

**The lists (the part Todd thinks sells it)**
- **The Beehive 100**: the hundred strongest growth stocks in the S&P 500, Nasdaq-100 and the desk's list,
  rebuilt after Friday's close. Score = 40% RS rank + 25% latest-quarter EPS growth + 10% revenue growth +
  25% chart (above the 50- and 200-day lines, within 15% of the high, in a buyable pattern; bonuses for an
  RS-line high and accelerating EPS). A name needs 15%+ EPS growth or 20%+ revenue growth to qualify.
  Columns: rank (with last week's in parentheses), last, day change, RS, EPS and revenue growth, market cap,
  score, chart status, buy point and buy range, **distance from the pivot**, vs the 52-week high, vs the 50-day.
  Re-quoted every pass through the day. Always 100 names.
- **The Big Cap 40**: the same score for companies worth $40 billion or more, no growth floor. Always 40 names:
  if the top of the universe holds fewer, the desk walks down the list until it has 40.
- **Watch lists**, live during the session: Breakouts this week, In the buy range (pivot to +5%), Near a buy
  point (within 5% below, base complete), **RS-line new highs** (see §5 on the name "Blue dots"), Aggressive
  entries (handle high, downtrend-line break, 50-day or 21-day bounce, tight shelf), IPO watch, Sell signals
  (leaders breaking the 50-day, or 7%+ under a buy point after a breakout).
- **Stocks on the Move**: the day's biggest volume surges vs the 50-day average, projected to the full session,
  refreshed every minute during market hours. Frozen copies of every pass are archived.
- **Groups**: the watch list's industry groups ranked by average RS, with the share above the 50-day line.
- **The Webster Screen**: a market-health screen (price $10+, $25M+ daily dollar volume, above both the 50- and
  200-day lines) grouped by sector and sorted by the week's move. See §5: it is named for a person.
- **Leaders board**: the whole ~200-name watch list ranked by relative strength.

**Tools**
- **Charts on every ticker**: click any symbol for a pop-up chart with Daily, Weekly, Monthly and Intraday tabs
  (five days of 5-minute bars, a dotted line between sessions). Bars keep a fixed width, so a phone shows fewer
  and a desktop more; nothing is squeezed. Crosshair with O/H/L/C, volume, ATR. The buy range is shaded on the
  chart; the RS line is drawn beneath price with a dot where it makes a 52-week high.
- **The quote box**: type ANY ticker, not just desk names, and the desk's own rules run on it in the browser:
  base, buy point, buy range, status, distance from the pivot, RS rank against the desk's universe, and a
  desk score. Live price from the relay, with pre-market/after-hours lines outside regular hours.
- **My Positions**: paste your holdings (symbol, buy price, shares) and every pass grades them against the
  rulebook: distance from your buy, from the 8% stop, from the 20% take-profit zone, position vs the 50-day.
  Stored only in the browser; nothing is sent anywhere.
- **Scorecard**: every "breakout" or "in buy range" call since the desk started, measured from the close it
  printed: 1-week, 4-week, 8-week average and win rate, against the S&P, how many hit the 8% stop, how many
  reached 20%+. This is the honesty feature and should be in the video.
- **Archive**: every pass, numbered day.pass, searchable.

**Cadence**: an edition after every close; movers passes at 8:05, 11:05, 2:05 and 3:20 Mountain; lists rebuilt
Fridays; charts and quotes live during the session.

---

## 4. The method, and who wrote the books (for the script)

The desk follows the **breakout method**: buy strong stocks as they clear a sound base on the way to new highs,
and cut losses small. It is not the desk's invention and the video should say so, briefly and accurately.
Name the lineage; do not claim any of these people endorse the site.

- **Jesse Livermore** (1877–1940): *Reminiscences of a Stock Operator* (Edwin Lefèvre, 1923) and *How to Trade
  in Stocks* (1940). Pivotal points, buying as a stock breaks into new ground, pyramiding winners.
- **Richard Wyckoff** (1873–1934): accumulation, markup, distribution; the idea that bases are where big money
  builds positions. *The Richard D. Wyckoff Method of Trading and Investing in Stocks* (1931).
- **Nicolas Darvas** (1920–1977): *How I Made $2,000,000 in the Stock Market* (1960). The "box" method:
  buy when price breaks out of the top of a box, stop just under it.
- **William O'Neil** (1933–2023): founder of Investor's Business Daily. *How to Make Money in Stocks* (1988,
  4th ed. 2009). CAN SLIM; cup-with-handle; buy at the pivot; never let a loss run past 7–8%; relative
  strength; the market direction rule. The desk's rulebook is closest to his.
- **Stan Weinstein**: *Secrets for Profiting in Bull and Bear Markets* (1988). Stage analysis; buy Stage 2
  breakouts above the 30-week line.
- **Mark Minervini**: *Trade Like a Stock Market Wizard* (2013), *Think & Trade Like a Champion* (2017).
  Volatility contraction pattern (VCP), the Trend Template; two-time U.S. Investing Champion.
- **David Ryan**, **Jim Roppel**, **Mike Webster**: O'Neil's students and IBD alumni who popularized the
  practical version of the method (Ryan: three-time U.S. Investing Champion; the desk's story style is modelled
  on IBD's and Roppel's reports; the Webster Screen is named for a screen Mike Webster described).

Keep the history to 20–30 seconds in the video. The point is "this is a 100-year-old discipline; the desk does
the daily grind of it for you."

One editorial stance to carry: the desk **reports volume but does not let it rule**. The publisher's view is
that with more than half of U.S. volume now off-exchange, a big bar no longer proves institutions are buying,
so a breakout is a decisive close through the pivot, and volume is a detail (`markets_desk.md`, "On volume").
Also: the desk's "RS rank" is a rank within its own ~200-name universe, never IBD's 1–99 rating.

---

## 5. What may be IBD's property. Audit this before selling.

Todd's worry is legitimate. IBD (a News Corp company since 2021) holds registered trademarks and is known to
police them. The method itself (buying breakouts, 7–8% stops, relative strength) is not protectable; it is
public and a century old. **Names, labels and distinctive presentation are.** Walk the live site and the
templates (`templates/markets*.html`, `markets.py`, `desk_extras.py`, `markets_desk.md`, `promo.py`) and report
every hit. Known ones:

| On the site | Problem | Suggested fix |
|---|---|---|
| **"Blue dots"** (watch list title, chart label, pop-up "Blue dot" fact, promo script) | IBD's MarketSurge/Leaderboard "blue dot" marker for an RS line at a new high is a recognizable IBD feature and name. | Rename everywhere to **"RS line new highs"** (list) and "RS line at a new 52-week high" (chart). The concept is just arithmetic and is fine to keep. |
| **"CAN SLIM"** (promo tags, writer's orders, README) | A registered IBD trademark. | Never use it in anything public (video, description, tags, gate page). In private writer instructions it is a reference, not a claim; still better to say "O'Neil's method." |
| **"IBD 50"** in the YouTube tags | Registered. | Remove. |
| **"The Beehive 100"** named by analogy to "IBD 50" | The format (a numbered, ranked growth list) is not protectable; the name is ours. Fine, but do not describe it as "our IBD 50." | Keep; the on-page blurb already says "Not IBD's list." |
| **"New America"** (section) | IBD's long-running column name. Probably a common-law mark. | Rename: "Company of the Day," "One Leader," "The Profile." |
| **"Investor's Corner"** (section) | IBD's long-running column name. | Rename: "The Lesson," "Rulebook," "How the Desk Reads It." |
| **"The Big Picture"** (market note, in writer orders) | IBD's daily column name. | Call it "Market Note" or "The Tape." Already labelled `market_note` in code. |
| **"Stocks on the Move"** (section, movers pages, archive labels) | IBD's screen name; likely a common-law mark. | Rename: "Volume Surges," "Movers," "Unusual Activity." The archive URLs (`movers/`) are fine. |
| **"The Webster Screen"** | Named for a living person (Mike Webster, IBD). Using a real person's name to label a paid feature invites a complaint even if the criteria are generic. | Rename to what it measures: "The Health Screen" or "Above Both Lines," and credit the idea in one line of the legend if at all. |
| **"Leaderboard"** | Not used as a title on the site (check). It is an IBD product name; do not introduce it. | n/a |
| **"Market Desk"** | Generic. Fine. | Keep. |
| **"Desk Chief," "Beehive 100," "Big Cap 40," "Scorecard," "My Positions," "Aggressive entries," "IPO watch," "Sell signals"** | Generic or ours. | Keep. |
| Distribution days, RS line, buy point, buy range, cup with handle, flat base, pivot, 7–8% stop, 20–25% take profit, "the 50-day line" | Vocabulary of the method; used across the industry and in O'Neil's published books. | Keep. Do not attribute to IBD as if quoting a product. |
| Chart styling | IBD's charts have a distinct look (blue/pink bars, RS line below). Ours uses blue up / pink down bars too. | Low risk, but changing the down-bar color to red or the house gold removes the resemblance for free. |

Also check:
- Any sentence on the site or in the promo that could read as "this is IBD" or "like IBD." Comparison by name
  in marketing is allowed in the U.S. if true and not confusing, but the safer path is to describe the method
  and name the books, not the publication.
- `markets_desk.md` tells the writer "The model is Investor's Business Daily's stock stories." That is a private
  instruction and fine, but make sure the public page never says it.

Deliver this as a short list to Todd: what to rename, what the new names are, and a one-line reason each. Then,
if he agrees, make the renames (templates, `markets.py` labels, `desk_extras.py`, `promo.py`, README, and the
`KIND_LABEL`/pass labels used by the archive). Keep URL paths and data keys as they are; only the words readers
see change.

---

## 6. The video: what Todd wants it to do

The current one is seven cards of text. The better one should **show the page**. Ideas, in rough order:

1. **Cold open on a chart.** A real breakout from the scorecard with the buy range shaded, the RS line making a
   new high, the pivot labeled. "This is what the desk looks for. Every day. Across a thousand stocks."
2. **The problem, 15 seconds.** Breakout investing works, and it is a grind: screening, drawing bases, computing
   pivots, re-quoting, keeping honest records. Most people do it badly or quit.
3. **The lineage, 25 seconds.** Livermore, Darvas, O'Neil, Weinstein, Minervini, in a line, one sentence each,
   book titles on screen. "The desk runs their rulebook."
4. **The tour, 90 seconds**, with real renders of the page (screenshot the local render; `playwright` is in the
   environment and `promo.py`/`video.py` show how slides are built): the Beehive 100 with the pivot column; the
   Big Cap 40; the watch lists; the pop-up chart with Daily/Weekly/Monthly/Intraday; the quote box on a ticker the
   viewer picks; My Positions; the Scorecard, with its real numbers.
5. **The honesty beat, 15 seconds.** The scorecard again. "Every call we've printed, graded against the S&P.
   You can see what we got wrong."
6. **The stance, 10 seconds.** Numbers before adjectives. Volume reported, not worshipped. No hype.
7. **The offer, 15 seconds.** $9.99 for your first 21 days, then $49.99 a month. Cancel any time. Your code is
   on screen the moment you pay. beehivewire.com/markets.

Rules for the script: every number spoken must be true on the day it's recorded (pull from `data/markets/`);
say "the desk" not "we"; no promises of returns; a one-line disclaimer in the description ("Not investment
advice"); never say CAN SLIM, IBD 50, Blue dots, or compare to IBD by name.

Deliverables: the new `promo.py` (or a `promo2.py` beside it), a dry-run render Todd can watch (send him the
mp4 with `SendUserFile`; he cannot easily download GitHub artifacts), then on his word the upload and the
`video_id` in `markets.yaml`.

---

## 7. Practical notes

- Local render of the promo: `pip install kokoro-onnx soundfile` then `python promo.py render` (first run
  downloads the voice model, ~350 MB). `--tts silent` for a quick layout check; `--keep-work` keeps the PNGs.
- Slide builders: `video.py` has `brand_card`, `frame`, `slide_story`, `fit_block`, `wrap`; colors are
  `BRAND_BG`, `BRAND_GOLD`, `BRAND_INK`, `BRAND_DIM`; the logo is `assets/source-icon.png`.
- Screenshots of the live page for slides: the session can't reach beehivewire.com (proxy), so render locally
  and screenshot `site/markets/index.html` over `python -m http.server`, unlocking with the test code.
- The gate page's Subscribe block is in `templates/markets_gate.html`; the promo embed slot is there too.
- Rename the session when you finish, date first, per `CLAUDE.md`.
