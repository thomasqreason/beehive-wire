// Beehive Wire quote relay — a Cloudflare Worker (free plan: 100,000 requests/day).
//
// The site is static, and Yahoo refuses requests from other websites' pages, so the chart popup asks
// this relay instead:   GET https://<your-worker>.workers.dev/?s=DELL      (up to 25 symbols: ?s=DELL,MU,AMD)
//
//   GET https://<your-worker>.workers.dev/?q=AAPL,MSFT,…   → bulk live price/volume/day range for the live screen (≤120)
//   GET https://<your-worker>.workers.dev/?hist=DELL   → two years of daily bars + company name and industry,
//   which the page's quote box runs through the same chart analysis the desk uses (cached 60 s).
//   GET https://<your-worker>.workers.dev/?intra=DELL  → five trading days of 5-minute bars, regular session
//   only, for the pop-up's Intraday tab (cached 60 s).
//   GET https://<your-worker>.workers.dev/?long=DELL   → ten years of weekly bars and every monthly bar Yahoo has,
//   so the pop-up's Weekly and Monthly tabs reach back further than the desk's two-year download (cached 1 h).
//
// The Market Desk gate (Oct 30, 2026) lives here too, under /gate/. It needs a KV namespace bound as GATE and
// three Worker secrets: GATE_ADMIN_TOKEN (for Todd's keys page), STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET.
//   GET  /gate/pubkey         the build fetches this and seals each edition's content key to it (RSA-OAEP)
//   POST /gate/unlock {code, wrapped}   gate page → the unsealed key, if the code is active
//   GET  /gate/claim?session=        the welcome page after a Stripe checkout → mints (or returns) the buyer's code
//   POST /gate/stripe         Stripe webhook: a cancelled or unpaid subscription revokes its code
//   GET  /gate/admin/list, POST /gate/admin/mint|revoke|restore   Todd's keys page (Bearer GATE_ADMIN_TOKEN)
//
// A reader's watchlists and price alerts (Nov 5, 2026) live here too, under /me, keyed by the reader's access code
// (an active code in GATE). Push goes through ntfy and needs one more secret, ALERT_SALT, and a Cron Trigger
// (*/5 * * * 1-5) that runs scheduled() below. Endpoints are listed with the code, at the bottom of this file.
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
    if (req.method === "OPTIONS") return new Response(null, { headers: { ...cors, "Access-Control-Allow-Methods": "GET, POST, PUT, OPTIONS", "Access-Control-Allow-Headers": "Authorization, Content-Type" } });
    const url = new URL(req.url);
    if (url.pathname.startsWith("/gate/")) return gate(req, url, env, { ...cors, "Access-Control-Allow-Headers": "Authorization, Content-Type", "Cache-Control": "no-store" });
    if (url.pathname === "/me" || url.pathname.startsWith("/me/")) return me(req, url, env, { ...cors, "Access-Control-Allow-Headers": "Content-Type", "Cache-Control": "no-store" });
    const hist = (url.searchParams.get("hist") || "").toUpperCase().trim();
    if (hist) {
      if (!SYM.test(hist)) return json({ error: "bad symbol" }, 400, cors);
      const hkey = new Request("https://quote-cache.local/hist/" + hist);
      const hhit = await caches.default.match(hkey);
      if (hhit) return withHeaders(hhit, cors);
      const [bars, prof] = await Promise.all([dailyBars(hist).catch(() => null), profile(hist).catch(() => null)]);
      if (!bars) return json({ error: "no data for " + hist }, 404, cors);
      const hres = json({ sym: hist, name: prof?.name || bars.name || null, industry: prof?.industry || null, sector: prof?.sector || null,
                          exchange: bars.exchange || null, ...bars.cols }, 200, { "Cache-Control": "public, max-age=60" });
      ctx.waitUntil(caches.default.put(hkey, hres.clone()));
      return withHeaders(hres, cors);
    }
    const long = (url.searchParams.get("long") || "").toUpperCase().trim();
    if (long) {
      if (!SYM.test(long)) return json({ error: "bad symbol" }, 400, cors);
      const lkey = new Request("https://quote-cache.local/long/" + long);
      const lhit = await caches.default.match(lkey);
      if (lhit) return withHeaders(lhit, cors);
      const [w, m] = await Promise.all([longBars(long, "1wk", "10y").catch(() => null), longBars(long, "1mo", "max").catch(() => null)]);
      if (!w && !m) return json({ error: "no long history for " + long }, 404, cors);
      const lres = json({ sym: long, w, m }, 200, { "Cache-Control": "public, max-age=3600" });
      ctx.waitUntil(caches.default.put(lkey, lres.clone()));
      return withHeaders(lres, cors);
    }
    const intra = (url.searchParams.get("intra") || "").toUpperCase().trim();
    if (intra) {
      if (!SYM.test(intra)) return json({ error: "bad symbol" }, 400, cors);
      const ikey = new Request("https://quote-cache.local/intra/" + intra);
      const ihit = await caches.default.match(ikey);
      if (ihit) return withHeaders(ihit, cors);
      const bars = await intradayBars(intra).catch(() => null);
      if (!bars) return json({ error: "no intraday data for " + intra }, 404, cors);
      const ires = json({ sym: intra, interval: "5m", ...bars }, 200, { "Cache-Control": "public, max-age=60" });
      ctx.waitUntil(caches.default.put(ikey, ires.clone()));
      return withHeaders(ires, cors);
    }
    // ── Bulk mode for the page's live screen: ?q=AAPL,MSFT,... (up to 120). Quote feed only — one Yahoo call per
    //    request, so a page refresh of the whole 583-name universe is six calls, shared by every reader for 60 s.
    const bulk = url.searchParams.get("q");
    if (bulk) {
      const list = [...new Set(bulk.toUpperCase().split(",").map(x => x.trim()).filter(x => SYM.test(x)))].slice(0, 120);
      if (!list.length) return json({ error: "pass ?q=SYM,SYM" }, 400, cors);
      const bkey = new Request("https://quote-cache.local/q/" + list.join(","));
      const bhit = await caches.default.match(bkey);
      if (bhit) return withHeaders(bhit, cors);
      const qs = await quoteFeed(list).catch(() => ({}));
      const out = {};
      let state = null;
      for (const sym of list) {
        const q = qs[sym]; if (!q) continue;
        state = state || q.marketState || null;
        out[sym] = [num(q.regularMarketPrice), num(q.regularMarketPreviousClose), num(q.regularMarketVolume),
                    num(q.regularMarketDayHigh), num(q.regularMarketDayLow), num(q.regularMarketTime)];
      }
      const bres = json({ asOf: Math.floor(Date.now() / 1000), marketState: state, cols: ["p", "pc", "v", "hi", "lo", "t"], quotes: out },
                        200, { "Cache-Control": "public, max-age=60" });
      if (Object.keys(out).length) ctx.waitUntil(caches.default.put(bkey, bres.clone()));
      return withHeaders(bres, cors);
    }
    const syms = [...new Set((url.searchParams.get("s") || "").toUpperCase().split(",").map(s => s.trim()).filter(s => SYM.test(s)))].slice(0, 25);
    if (!syms.length) return json({ error: "pass ?s=SYMBOL or ?hist=SYMBOL" }, 400, cors);

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

  // The Cron Trigger (Cloudflare: beehive-quotes → Settings → Triggers → Cron Triggers → */5 * * * 1-5).
  async scheduled(event, env, ctx) {
    ctx.waitUntil(runAlerts(env).then(r => console.log("alerts", JSON.stringify(r))).catch(e => console.log("alerts failed", String(e))));
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

// ── Two years of daily bars for the quote box (same source and shape the desk's Python download uses)
async function dailyBars(sym) {
  const r = await fetch(`https://query1.finance.yahoo.com/v8/finance/chart/${encodeURIComponent(sym)}?interval=1d&range=2y`,
    { headers: { "User-Agent": UA }, cf: { cacheTtl: 60 } });
  if (!r.ok) return null;
  const res = (await r.json())?.chart?.result?.[0];
  if (!res?.timestamp) return null;
  const m = res.meta || {}, q = res.indicators?.quote?.[0] || {}, off = m.gmtoffset || 0;
  const cols = { d: [], o: [], h: [], l: [], c: [], v: [] };
  res.timestamp.forEach((t, i) => {
    const o = q.open?.[i], h = q.high?.[i], l = q.low?.[i], c = q.close?.[i], v = q.volume?.[i];
    if ([o, h, l, c, v].some(x => x == null) || !(v > 0)) return;     // the desk drops empty and zero-volume days too
    const d = new Date((t + off) * 1000).toISOString().slice(0, 10);
    if (cols.d.length && cols.d[cols.d.length - 1] === d) {           // a duplicate live bar: keep the newer one
      for (const k of ["d", "o", "h", "l", "c", "v"]) cols[k].pop();
    }
    cols.d.push(d); cols.o.push(o); cols.h.push(h); cols.l.push(l); cols.c.push(c); cols.v.push(v);
  });
  if (cols.d.length < 15) return null;
  return { cols, name: m.longName || m.shortName, exchange: m.fullExchangeName };
}

// ── Weekly (1wk) or monthly (1mo) bars for the long view. Dated by Yahoo's bucket start (a Monday, or the 1st);
//    the page files each bar into its own week or month, so only the date's bucket matters.
async function longBars(sym, interval, range) {
  const r = await fetch(`https://query1.finance.yahoo.com/v8/finance/chart/${encodeURIComponent(sym)}?interval=${interval}&range=${range}`,
    { headers: { "User-Agent": UA }, cf: { cacheTtl: 3600 } });
  if (!r.ok) return null;
  const res = (await r.json())?.chart?.result?.[0];
  if (!res?.timestamp) return null;
  const m = res.meta || {}, q = res.indicators?.quote?.[0] || {}, off = m.gmtoffset || 0;
  const cols = { d: [], o: [], h: [], l: [], c: [], v: [] };
  res.timestamp.forEach((t, i) => {
    const o = q.open?.[i], h = q.high?.[i], l = q.low?.[i], c = q.close?.[i], v = q.volume?.[i];
    if ([o, h, l, c].some(x => x == null)) return;
    const d = new Date((t + off) * 1000).toISOString().slice(0, 10);
    const key = interval === "1mo" ? d.slice(0, 7) : d;
    const lastKey = cols.d.length ? (interval === "1mo" ? cols.d[cols.d.length - 1].slice(0, 7) : cols.d[cols.d.length - 1]) : null;
    if (lastKey === key) for (const k of ["d", "o", "h", "l", "c", "v"]) cols[k].pop();   // a duplicate live bar: keep the newer one
    const r2 = x => Math.round(x * 100) / 100;
    cols.d.push(d); cols.o.push(r2(o)); cols.h.push(r2(h)); cols.l.push(r2(l)); cols.c.push(r2(c)); cols.v.push(v == null ? 0 : v);
  });
  return cols.d.length ? cols : null;
}

// ── Five days of 5-minute bars for the pop-up's Intraday tab. Regular session only, so the day's shape is
//    the one the tape shows; timestamps are the exchange's own clock ("YYYY-MM-DD HH:MM") so the page needs no
//    time-zone work.
async function intradayBars(sym) {
  const r = await fetch(`https://query1.finance.yahoo.com/v8/finance/chart/${encodeURIComponent(sym)}?interval=5m&range=5d&includePrePost=false`,
    { headers: { "User-Agent": UA }, cf: { cacheTtl: 60 } });
  if (!r.ok) return null;
  const res = (await r.json())?.chart?.result?.[0];
  if (!res?.timestamp) return null;
  const m = res.meta || {}, q = res.indicators?.quote?.[0] || {}, off = m.gmtoffset || 0;
  const cols = { d: [], o: [], h: [], l: [], c: [], v: [] };
  res.timestamp.forEach((t, i) => {
    const o = q.open?.[i], h = q.high?.[i], l = q.low?.[i], c = q.close?.[i], v = q.volume?.[i];
    if ([o, h, l, c].some(x => x == null)) return;
    const d = new Date((t + off) * 1000).toISOString().slice(0, 16).replace("T", " ");
    cols.d.push(d); cols.o.push(o); cols.h.push(h); cols.l.push(l); cols.c.push(c); cols.v.push(v == null ? 0 : v);
  });
  if (cols.d.length < 10) return null;
  return { tz: m.exchangeTimezoneName || null, ...cols };
}

async function profile(sym, retried) {
  const s = await getSession(retried);
  const r = await fetch(`https://query1.finance.yahoo.com/v10/finance/quoteSummary/${encodeURIComponent(sym)}?modules=assetProfile,price&crumb=${encodeURIComponent(s.crumb)}`,
    { headers: { "User-Agent": UA, Cookie: s.cookie } });
  if ((r.status === 401 || r.status === 403) && !retried) return profile(sym, true);
  if (!r.ok) return null;
  const x = (await r.json())?.quoteSummary?.result?.[0] || {};
  return { name: x.price?.longName || x.price?.shortName || null, industry: x.assetProfile?.industry || null, sector: x.assetProfile?.sector || null };
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
  // The next report (Nov 5, 2026): unix seconds from the v7 quote, so a ticker typed into the quote box gets the
  // earnings date on its chart like the desk's own names. The page prefers start, then ts, and ignores past dates.
  const earnings = q && (q.earningsTimestamp != null || q.earningsTimestampStart != null)
    ? { ts: num(q.earningsTimestamp), start: num(q.earningsTimestampStart), end: num(q.earningsTimestampEnd) } : null;
  return { sym, name: q?.longName || q?.shortName || m.longName || m.shortName || null, regular: reg, extended, earnings,
           live: state ? state === "REGULAR" : !!(tp && Date.now() / 1000 >= tp.regular?.start && Date.now() / 1000 < tp.regular?.end),
           marketState: state || null };
}

const num = x => (typeof x === "object" && x !== null && "raw" in x ? num(x.raw) : (x == null || x === "" || isNaN(+x) ? null : +x));
const json = (o, status, h) => new Response(JSON.stringify(o), { status, headers: { "Content-Type": "application/json", ...h } });
function withHeaders(res, h) { const r = new Response(res.body, res); for (const [k, v] of Object.entries(h)) r.headers.set(k, v); return r; }


// ═══════════════════════════════════════════════════════════════ the Market Desk gate
// Codes live in KV (binding GATE): code:<CODE> → {email, note, source, status, created, customer, subscription},
// with the same object as the entry's metadata so a list needs no reads. session:<stripe session> → code and
// sub:<stripe subscription> → code let a checkout be re-claimed and a cancellation find its code.
// gate:priv / gate:pub → an RSA-OAEP keypair made on first use. The build seals each edition's content key to
// the public half and puts it in the page; only this worker can open it, and it only does so for an active code.
// No secret is shared between GitHub and Cloudflare.

const CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789";   // no 0/O, 1/I
const NO_STORE = { "Cache-Control": "no-store" };

function mintCode() {
  const b = new Uint8Array(8); crypto.getRandomValues(b);
  const s = [...b].map(x => CODE_ALPHABET[x % 32]).join("");
  return `BW-${s.slice(0, 4)}-${s.slice(4)}`;
}
const normCode = c => (c || "").replace(/\s+/g, "").toUpperCase();

async function gate(req, url, env, cors) {
  if (!env.GATE) return json({ error: "gate not configured: bind a KV namespace as GATE" }, 503, cors);
  const path = url.pathname.slice("/gate/".length);
  const auth = (req.headers.get("Authorization") || "").replace(/^Bearer\s+/i, "");
  const admin = !!env.GATE_ADMIN_TOKEN && auth === env.GATE_ADMIN_TOKEN;

  // ── the public key the build seals to
  if (path === "pubkey") {
    const kp = await gateKeys(env);
    return json({ alg: "RSA-OAEP-256", jwk: kp.pub }, 200, { ...cors, "Cache-Control": "public, max-age=300" });
  }

  // ── a reader's code + the page's sealed key → the key
  if (path === "unlock" && req.method === "POST") {
    const b = await req.json().catch(() => ({}));
    const code = normCode(b.code);
    if (!code || !b.wrapped) return json({ error: "need code and wrapped" }, 400, cors);
    const rec = await env.GATE.get("code:" + code, "json");
    if (!rec || rec.status !== "active") return json({ error: "no" }, 403, cors);
    const kp = await gateKeys(env);
    let key;
    try {
      const priv = await crypto.subtle.importKey("jwk", kp.priv, { name: "RSA-OAEP", hash: "SHA-256" }, false, ["decrypt"]);
      key = await crypto.subtle.decrypt({ name: "RSA-OAEP" }, priv, Uint8Array.from(atob(b.wrapped), c => c.charCodeAt(0)));
    } catch (e) { return json({ error: "this page was sealed to another key; the next build fixes it" }, 409, cors); }
    rec.last_used = new Date().toISOString(); rec.uses = (rec.uses || 0) + 1;
    await env.GATE.put("code:" + code, JSON.stringify(rec), { metadata: summary(rec) });
    return json({ key: btoa(String.fromCharCode(...new Uint8Array(key))) }, 200, cors);
  }

  // ── after a Stripe checkout: the welcome page claims the buyer's code
  if (path === "claim") {
    const sid = url.searchParams.get("session") || "";
    if (!/^cs_[A-Za-z0-9_]+$/.test(sid)) return json({ error: "bad session" }, 400, cors);
    if (!env.STRIPE_SECRET_KEY) return json({ error: "payments not configured" }, 503, cors);
    const had = await env.GATE.get("session:" + sid);
    if (had) { const rec = await env.GATE.get("code:" + had, "json"); return json({ code: had, email: rec?.email || null }, 200, cors); }
    const r = await fetch(`https://api.stripe.com/v1/checkout/sessions/${sid}`, { headers: { Authorization: "Bearer " + env.STRIPE_SECRET_KEY } });
    if (!r.ok) return json({ error: "stripe: " + r.status }, 502, cors);
    const cs = await r.json();
    if (cs.payment_status !== "paid" && cs.status !== "complete") return json({ error: "not paid" }, 402, cors);
    const code = mintCode();
    const rec = { code, email: cs.customer_details?.email || cs.customer_email || null, note: "Stripe", source: "stripe", status: "active",
                  created: new Date().toISOString(), customer: cs.customer || null, subscription: cs.subscription || null };
    await env.GATE.put("code:" + code, JSON.stringify(rec), { metadata: summary(rec) });
    await env.GATE.put("session:" + sid, code);
    if (rec.subscription) await env.GATE.put("sub:" + rec.subscription, code);
    return json({ code, email: rec.email }, 200, cors);
  }

  // ── Stripe webhook: a subscription that ends takes its code with it
  if (path === "stripe" && req.method === "POST") {
    const payload = await req.text();
    if (!env.STRIPE_WEBHOOK_SECRET || !(await stripeVerify(payload, req.headers.get("Stripe-Signature") || "", env.STRIPE_WEBHOOK_SECRET)))
      return json({ error: "bad signature" }, 400, NO_STORE);
    const ev = JSON.parse(payload), obj = ev.data?.object || {};
    const subId = obj.object === "subscription" ? obj.id : obj.subscription;
    if (subId) {
      const code = await env.GATE.get("sub:" + subId);
      if (code) {
        const rec = await env.GATE.get("code:" + code, "json");
        if (rec) {
          const dead = ev.type === "customer.subscription.deleted" || (ev.type === "customer.subscription.updated" && ["canceled", "unpaid", "incomplete_expired"].includes(obj.status));
          const alive = ev.type === "customer.subscription.updated" && ["active", "trialing"].includes(obj.status);
          if (dead) { rec.status = "revoked"; rec.revoked = new Date().toISOString(); rec.why = ev.type + ":" + (obj.status || ""); }
          if (alive && rec.status === "revoked" && (rec.why || "").startsWith("customer.subscription")) { rec.status = "active"; delete rec.revoked; delete rec.why; }
          await env.GATE.put("code:" + code, JSON.stringify(rec), { metadata: summary(rec) });
        }
      }
    }
    return json({ received: true }, 200, NO_STORE);
  }

  // ── Todd's keys page
  if (path.startsWith("admin/")) {
    if (!admin) return json({ error: "unauthorized" }, 401, cors);
    const op = path.slice("admin/".length);
    if (op === "list") {
      const out = []; let cursor;
      do { const l = await env.GATE.list({ prefix: "code:", cursor }); out.push(...l.keys.map(k => k.metadata || { code: k.name.slice(5) })); cursor = l.list_complete ? null : l.cursor; } while (cursor);
      out.sort((a, b) => (b.created || "").localeCompare(a.created || ""));
      return json({ codes: out }, 200, cors);
    }
    const b = req.method === "POST" ? await req.json().catch(() => ({})) : {};
    if (op === "mint") {
      const code = mintCode();
      const rec = { code, email: b.email || null, note: b.note || "", source: "hand", status: "active", created: new Date().toISOString() };
      await env.GATE.put("code:" + code, JSON.stringify(rec), { metadata: summary(rec) });
      return json(rec, 200, cors);
    }
    if (op === "revoke" || op === "restore") {
      const code = normCode(b.code), rec = await env.GATE.get("code:" + code, "json");
      if (!rec) return json({ error: "no such code" }, 404, cors);
      if (op === "revoke") { rec.status = "revoked"; rec.revoked = new Date().toISOString(); rec.why = "hand"; }
      else { rec.status = "active"; delete rec.revoked; delete rec.why; }
      await env.GATE.put("code:" + code, JSON.stringify(rec), { metadata: summary(rec) });
      return json(rec, 200, cors);
    }
  }
  return json({ error: "no such gate call" }, 404, cors);
}

async function gateKeys(env) {
  let priv = await env.GATE.get("gate:priv", "json"), pub = await env.GATE.get("gate:pub", "json");
  if (!priv || !pub) {
    const kp = await crypto.subtle.generateKey({ name: "RSA-OAEP", modulusLength: 2048, publicExponent: new Uint8Array([1, 0, 1]), hash: "SHA-256" }, true, ["encrypt", "decrypt"]);
    priv = await crypto.subtle.exportKey("jwk", kp.privateKey); pub = await crypto.subtle.exportKey("jwk", kp.publicKey);
    await env.GATE.put("gate:priv", JSON.stringify(priv)); await env.GATE.put("gate:pub", JSON.stringify(pub));
  }
  return { priv, pub };
}

const summary = r => ({ code: r.code, email: r.email, note: r.note, source: r.source, status: r.status, created: r.created, last_used: r.last_used || null, uses: r.uses || 0, subscription: r.subscription || null });

async function stripeVerify(payload, header, secret) {
  const parts = header.split(",").map(p => p.trim().split("=")), t = parts.find(p => p[0] === "t")?.[1], sigs = parts.filter(p => p[0] === "v1").map(p => p[1]);
  if (!t || !sigs.length || Math.abs(Date.now() / 1000 - Number(t)) > 300) return false;
  const enc = new TextEncoder();
  const key = await crypto.subtle.importKey("raw", enc.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const mac = [...new Uint8Array(await crypto.subtle.sign("HMAC", key, enc.encode(`${t}.${payload}`)))].map(b => b.toString(16).padStart(2, "0")).join("");
  return sigs.some(s => s.length === mac.length && timingSafeEqual(s, mac));
}
function timingSafeEqual(a, b) { let r = 0; for (let i = 0; i < a.length; i++) r |= a.charCodeAt(i) ^ b.charCodeAt(i); return r === 0; }

// ═══════════════════════════════════════════════════════════════ a reader's watchlists and price alerts
// Keyed by the reader's access code; only an active code in GATE reads or writes. KV keys:
//   wl:<CODE>      → {updated, lists: [{name, syms}]}
//   alerts:<CODE>  → {updated, rules: [{id, sym, kind: "above"|"below", price, label, created, armed_at, fired, fired_price}]}
//                    with metadata {armed: n}, so the cron's list skips readers with nothing armed (no read)
// The page keeps both in localStorage and syncs here; the newer `updated` wins.
//   POST /me {code}                             → {watchlists, alerts, topic}
//   PUT  /me/watchlists {code, updated, lists}  → 409 + the stored copy when the stored one is newer
//   PUT  /me/alerts {code, updated, rules}      → same; a rule the cron fired after the page armed it stays fired
//   POST /me/alerts/test {code}                 → one test line to the reader's ntfy topic
// Push: ntfy topic "bw-" + 12 hex of SHA-256(code + "|" + ALERT_SALT); unguessable without the code. No salt, no push.
// Regular-session prices only: a rule fires once, when Yahoo's regularMarketPrice crosses it during the session.

const NTFY = "https://ntfy.sh";
const HOLIDAYS_URL = "https://beehivewire.com/markets/holidays.json";    // written by markets.py at every render
const WL_MAX = 50, WL_LISTS = 30, AL_MAX = 200;

async function readerOK(env, code) {
  if (!code) return false;
  const rec = await env.GATE.get("code:" + code, "json");
  return !!rec && rec.status === "active";
}

async function topicFor(env, code) {
  if (!env.ALERT_SALT) return null;
  const h = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(code + "|" + env.ALERT_SALT));
  return "bw-" + [...new Uint8Array(h)].slice(0, 6).map(b => b.toString(16).padStart(2, "0")).join("");
}

async function ntfyPost(topic, title, body, click, tag) {
  const r = await fetch(`${NTFY}/${topic}`, { method: "POST", body,
    headers: { Title: title, Tags: tag || "bell", ...(click ? { Click: click } : {}) } });
  return r.ok;
}

function cleanLists(lists) {
  if (!Array.isArray(lists)) return null;
  const out = [], seen = new Set();
  for (const l of lists.slice(0, WL_LISTS)) {
    const name = String(l?.name || "").trim().slice(0, 30);
    if (!name || seen.has(name.toLowerCase())) continue;
    seen.add(name.toLowerCase());
    const syms = [...new Set((Array.isArray(l.syms) ? l.syms : []).map(s => String(s).toUpperCase().trim()).filter(s => SYM.test(s)))];
    out.push({ name, syms: syms.slice(0, WL_MAX) });
  }
  return out;
}

function cleanRules(rules) {
  if (!Array.isArray(rules)) return null;
  const out = [], t = new Date().toISOString();
  for (const r of rules.slice(0, AL_MAX)) {
    const sym = String(r?.sym || "").toUpperCase().trim(), price = Number(r?.price);
    if (!SYM.test(sym) || !(price > 0) || !["above", "below"].includes(r?.kind)) continue;
    const fired = r.fired ? String(r.fired).slice(0, 30) : null;
    out.push({ id: String(r.id || crypto.randomUUID()).slice(0, 40), sym, kind: r.kind, price: Math.round(price * 100) / 100,
               label: r.label ? String(r.label).slice(0, 60) : null, created: String(r.created || t).slice(0, 30),
               armed_at: String(r.armed_at || r.created || t).slice(0, 30), fired, fired_price: fired ? (Number(r.fired_price) || null) : null });
  }
  return out;
}
const armedCount = rules => rules.filter(r => !r.fired).length;
const newer = (a, b) => String(a?.updated || "") > String(b || "");

async function me(req, url, env, cors) {
  if (!env.GATE) return json({ error: "not configured: bind a KV namespace as GATE" }, 503, cors);
  if (req.method !== "POST" && req.method !== "PUT") return json({ error: "POST or PUT" }, 405, cors);
  const b = await req.json().catch(() => ({}));
  const code = normCode(b.code);
  if (!(await readerOK(env, code))) return json({ error: "unauthorized" }, 401, cors);
  const path = url.pathname.replace(/\/+$/, "");
  if (path === "/me") {
    const [wl, al] = await Promise.all([env.GATE.get("wl:" + code, "json"), env.GATE.get("alerts:" + code, "json")]);
    return json({ watchlists: wl, alerts: al, topic: await topicFor(env, code) }, 200, cors);
  }
  if (path === "/me/watchlists" && req.method === "PUT") {
    const lists = cleanLists(b.lists);
    if (!lists) return json({ error: "need lists" }, 400, cors);
    const cur = await env.GATE.get("wl:" + code, "json");
    if (newer(cur, b.updated)) return json({ error: "stale", watchlists: cur }, 409, cors);
    const doc = { updated: String(b.updated || new Date().toISOString()).slice(0, 30), lists };
    await env.GATE.put("wl:" + code, JSON.stringify(doc));
    return json({ ok: true, watchlists: doc }, 200, cors);
  }
  if (path === "/me/alerts" && req.method === "PUT") {
    let rules = cleanRules(b.rules);
    if (!rules) return json({ error: "need rules" }, 400, cors);
    const cur = await env.GATE.get("alerts:" + code, "json");
    if (newer(cur, b.updated)) return json({ error: "stale", alerts: cur }, 409, cors);
    const fired = new Map((cur?.rules || []).filter(r => r.fired).map(r => [r.id, r]));
    rules = rules.map(r => { const f = fired.get(r.id); return f && !r.fired && f.fired > r.armed_at ? { ...r, fired: f.fired, fired_price: f.fired_price } : r; });
    const doc = { updated: String(b.updated || new Date().toISOString()).slice(0, 30), rules };
    await env.GATE.put("alerts:" + code, JSON.stringify(doc), { metadata: { armed: armedCount(rules) } });
    return json({ ok: true, alerts: doc }, 200, cors);
  }
  if (path === "/me/alerts/test") {
    const topic = await topicFor(env, code);
    if (!topic) return json({ error: "push not configured: set the ALERT_SALT secret" }, 503, cors);
    const ok = await ntfyPost(topic, "Beehive Wire alerts", "Test. Market Desk price alerts arrive here.", "https://beehivewire.com/markets/", "bell").catch(() => false);
    return json({ ok, topic }, ok ? 200 : 502, cors);
  }
  return json({ error: "no such call" }, 404, cors);
}

// Every 5 minutes on weekdays: returns at once outside 9:30 a.m.-4:05 p.m. Eastern and on NYSE holidays, else checks
// every armed rule against Yahoo's regular-session price, pushes each hit to its reader's topic and marks it fired.
async function runAlerts(env, opts = {}) {
  if (!env.GATE) return { skipped: "no GATE binding" };
  const p = new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", weekday: "short", year: "numeric", month: "2-digit",
    day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }).formatToParts(opts.now || new Date()).reduce((o, x) => (o[x.type] = x.value, o), {});
  const mins = (+p.hour % 24) * 60 + +p.minute, day = `${p.year}-${p.month}-${p.day}`;
  if (!opts.force) {
    if (p.weekday === "Sat" || p.weekday === "Sun" || mins < 9 * 60 + 30 || mins > 16 * 60 + 5) return { skipped: "outside the session" };
    const cal = await fetch(HOLIDAYS_URL, { cf: { cacheTtl: 86400 } }).then(r => r.ok ? r.json() : null).catch(() => null);
    if (cal?.holidays?.includes(day)) return { skipped: "NYSE holiday" };
    if (cal?.early_closes?.includes(day) && mins > 13 * 60 + 5) return { skipped: "early close" };
  }
  const keys = []; let cursor;
  do {
    const l = await env.GATE.list({ prefix: "alerts:", cursor });
    keys.push(...l.keys.filter(k => (k.metadata?.armed ?? 1) > 0));
    cursor = l.list_complete ? null : l.cursor;
  } while (cursor);
  if (!keys.length) return { readers: 0 };
  const docs = await Promise.all(keys.map(async k => ({ code: k.name.slice("alerts:".length), doc: await env.GATE.get(k.name, "json") })));
  const syms = [...new Set(docs.flatMap(d => (d.doc?.rules || []).filter(r => !r.fired).map(r => r.sym)))];
  const quotes = {};
  for (let i = 0; i < syms.length; i += 25) Object.assign(quotes, await quoteFeed(syms.slice(i, i + 25)).catch(() => ({})));
  let fired = 0;
  for (const { code, doc } of docs) {
    const hits = [];
    for (const r of doc?.rules || []) {
      if (r.fired) continue;
      const q = quotes[r.sym], px = num(q?.regularMarketPrice);
      if (px == null || (!opts.force && q?.marketState && q.marketState !== "REGULAR")) continue;
      if ((r.kind === "above" && px >= r.price) || (r.kind === "below" && px <= r.price)) {
        r.fired = new Date().toISOString(); r.fired_price = Math.round(px * 100) / 100; hits.push(r);
      }
    }
    if (!hits.length || !(await readerOK(env, code))) continue;        // a revoked code gets nothing
    const topic = await topicFor(env, code);
    for (const r of hits) {
      if (topic) await ntfyPost(topic, `${r.sym} ${r.fired_price.toFixed(2)}`, `${r.sym} ${r.fired_price.toFixed(2)}: ${r.label || r.kind + " " + r.price.toFixed(2)}`,
                                `https://beehivewire.com/markets/#chart=${encodeURIComponent(r.sym)}`,
                                r.kind === "above" ? "chart_with_upwards_trend" : "chart_with_downwards_trend").catch(() => false);
    }
    doc.updated = new Date().toISOString();
    await env.GATE.put("alerts:" + code, JSON.stringify(doc), { metadata: { armed: armedCount(doc.rules) } });
    fired += hits.length;
  }
  return { readers: docs.length, symbols: syms.length, fired };
}
