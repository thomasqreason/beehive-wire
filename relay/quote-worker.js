// Beehive Wire quote relay — a Cloudflare Worker (free plan: 100,000 requests/day).
//
// The site is static, and Yahoo refuses requests from other websites' pages, so the chart popup asks
// this relay instead:   GET https://<your-worker>.workers.dev/?s=DELL      (up to 25 symbols: ?s=DELL,MU,AMD)
//
// Returns, per symbol: the regular-session price and change, plus the latest extended-hours price
// (pre-market, after hours, or overnight) when there is one, each with its time. Quotes are cached
// for 30 seconds, so a busy page costs Yahoo — and the free plan — one call per symbol per 30 s.
//
// Sources: Yahoo's chart feed (regular, pre-market, after hours; no login) and Yahoo's quote feed
// (adds overnight, 8 p.m.-4 a.m. Eastern; needs a session cookie + crumb, which the relay fetches
// itself and reuses). If the quote feed fails, the chart feed alone still answers.

const ALLOWED = ["https://beehivewire.com", "https://www.beehivewire.com", "http://localhost:8000", "http://127.0.0.1:8000"];
const TTL = 30;                        // seconds a quote is reused
const UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36";
const SYM = /^[A-Z0-9.\-^=]{1,12}$/;

let session = null;                    // { cookie, crumb, at } — reused while this Worker instance lives

export default {
  async fetch(req, env, ctx) {
    const origin = req.headers.get("Origin") || "";
    const cors = {
      "Access-Control-Allow-Origin": ALLOWED.includes(origin) ? origin : ALLOWED[0],
      "Access-Control-Allow-Methods": "GET, OPTIONS",
      "Vary": "Origin",
    };
    if (req.method === "OPTIONS") return new Response(null, { headers: cors });
    const url = new URL(req.url);
    const syms = [...new Set((url.searchParams.get("s") || "").toUpperCase().split(",").map(s => s.trim()).filter(s => SYM.test(s)))].slice(0, 25);
    if (!syms.length) return json({ error: "pass ?s=SYMBOL" }, 400, cors);

    const cache = caches.default;
    const key = new Request("https://quote-cache.local/" + syms.join(","));
    const hit = await cache.match(key);
    if (hit) return withHeaders(hit, cors);

    const [quotes, charts] = await Promise.all([
      quoteFeed(syms).catch(() => ({})),
      Promise.all(syms.map(s => chartFeed(s).catch(() => null))),
    ]);
    const out = {};
    syms.forEach((s, i) => { const q = merge(s, quotes[s], charts[i]); if (q) out[s] = q; });
    const res = json({ asOf: Math.floor(Date.now() / 1000), quotes: out }, 200, { "Cache-Control": `public, max-age=${TTL}` });
    ctx.waitUntil(cache.put(key, res.clone()));
    return withHeaders(res, cors);
  },
};

// ── Yahoo chart feed: regular + pre + post, 1-minute bars, no login
async function chartFeed(sym) {
  const r = await fetch(`https://query1.finance.yahoo.com/v8/finance/chart/${encodeURIComponent(sym)}?interval=1m&range=1d&includePrePost=true`,
    { headers: { "User-Agent": UA }, cf: { cacheTtl: TTL } });
  if (!r.ok) return null;
  const res = (await r.json())?.chart?.result?.[0];
  if (!res) return null;
  const m = res.meta, ts = res.timestamp || [], c = res.indicators?.quote?.[0]?.close || [];
  let last = null;
  for (let i = ts.length - 1; i >= 0; i--) if (c[i] != null) { last = { price: c[i], time: ts[i] }; break; }
  return { meta: m, last };
}

// ── Yahoo quote feed: adds overnight. Needs a cookie + crumb.
async function getSession(force) {
  if (session && !force && Date.now() - session.at < 6 * 3600e3) return session;
  const r1 = await fetch("https://fc.yahoo.com/", { headers: { "User-Agent": UA }, redirect: "manual" });
  const cookie = (r1.headers.get("set-cookie") || "").split(/,(?=\s*[A-Za-z0-9_]+=)/).map(c => c.split(";")[0].trim()).filter(Boolean).join("; ");
  if (!cookie) throw new Error("no cookie");
  const r2 = await fetch("https://query1.finance.yahoo.com/v1/test/getcrumb", { headers: { "User-Agent": UA, Cookie: cookie } });
  const crumb = (await r2.text()).trim();
  if (!r2.ok || !crumb || crumb.includes("<")) throw new Error("no crumb");
  return (session = { cookie, crumb, at: Date.now() });
}

async function quoteFeed(syms, retried) {
  const s = await getSession(retried);
  const r = await fetch(`https://query1.finance.yahoo.com/v7/finance/quote?symbols=${syms.map(encodeURIComponent).join(",")}&crumb=${encodeURIComponent(s.crumb)}`,
    { headers: { "User-Agent": UA, Cookie: s.cookie } });
  if ((r.status === 401 || r.status === 403) && !retried) return quoteFeed(syms, true);
  if (!r.ok) return {};
  const out = {};
  for (const q of (await r.json())?.quoteResponse?.result || []) out[q.symbol] = q;
  return out;
}

// ── One answer per symbol: the regular quote, plus the freshest extended-hours print.
function merge(sym, q, ch) {
  const m = ch?.meta || {};
  const reg = {
    price: num(q?.regularMarketPrice ?? m.regularMarketPrice),
    prevClose: num(q?.regularMarketPreviousClose ?? m.previousClose ?? m.chartPreviousClose),
    time: num(q?.regularMarketTime ?? m.regularMarketTime),
  };
  if (reg.price == null) return null;
  reg.chg = reg.prevClose != null ? reg.price - reg.prevClose : null;
  reg.chgPct = reg.prevClose ? (reg.chg / reg.prevClose) * 100 : null;

  // Extended-hours candidates, newest wins. Changes are measured from the latest regular-session price,
  // the way Yahoo shows them.
  const ext = [];
  const add = (session, price, time) => { if (num(price) != null && num(time) != null) ext.push({ session, price: num(price), time: num(time) }); };
  if (q) {
    add("pre", q.preMarketPrice, q.preMarketTime);
    add("post", q.postMarketPrice, q.postMarketTime);
    // Overnight fields: take any overnight*Price / overnight*Time pair Yahoo sends, whatever its exact name.
    const pk = Object.keys(q).find(k => /^overnight.*price$/i.test(k)), tk = Object.keys(q).find(k => /^overnight.*time$/i.test(k));
    if (pk && tk) add("overnight", q[pk], q[tk]);
  }
  const tp = m.currentTradingPeriod;
  if (ch?.last && tp) {
    const t = ch.last.time;
    const s = t >= tp.pre?.start && t < tp.pre?.end ? "pre" : t >= tp.post?.start && t < tp.post?.end ? "post" : null;
    if (s) add(s, ch.last.price, t);
  }
  ext.sort((a, b) => b.time - a.time);
  let extended = ext.find(e => e.time > reg.time) || null;
  if (extended) {
    extended.chg = extended.price - reg.price;
    extended.chgPct = (extended.chg / reg.price) * 100;
  }
  const state = (q?.marketState || "").toUpperCase();           // PRE, REGULAR, POST, POSTPOST, CLOSED, PREPRE …
  return { sym, name: q?.longName || q?.shortName || m.longName || m.shortName || null, regular: reg, extended,
           live: state ? state === "REGULAR" : !!(tp && Date.now() / 1000 >= tp.regular?.start && Date.now() / 1000 < tp.regular?.end),
           marketState: state || null };
}

const num = x => (typeof x === "object" && x !== null && "raw" in x ? num(x.raw) : (x == null || x === "" || isNaN(+x) ? null : +x));
const json = (o, status, h) => new Response(JSON.stringify(o), { status, headers: { "Content-Type": "application/json", ...h } });
function withHeaders(res, h) { const r = new Response(res.body, res); for (const [k, v] of Object.entries(h)) r.headers.set(k, v); return r; }
