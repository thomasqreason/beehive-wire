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
//
// The Market Desk gate (Oct 30, 2026) lives here too, under /gate/. It needs a KV namespace bound as GATE and
// three Worker secrets: GATE_ADMIN_TOKEN (for Todd's keys page), STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET.
//   GET  /gate/pubkey         the build fetches this and seals each edition's content key to it (RSA-OAEP)
//   POST /gate/unlock {code, wrapped}   gate page → the unsealed key, if the code is active
//   GET  /gate/claim?session=        the welcome page after a Stripe checkout → mints (or returns) the buyer's code
//   POST /gate/stripe         Stripe webhook: a cancelled or unpaid subscription revokes its code
//   GET  /gate/admin/list, POST /gate/admin/mint|revoke|restore   Todd's keys page (Bearer GATE_ADMIN_TOKEN)
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
    if (req.method === "OPTIONS") return new Response(null, { headers: { ...cors, "Access-Control-Allow-Headers": "Authorization, Content-Type" } });
    const url = new URL(req.url);
    if (url.pathname.startsWith("/gate/")) return gate(req, url, env, { ...cors, "Access-Control-Allow-Headers": "Authorization, Content-Type", "Cache-Control": "no-store" });
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
  return { sym, name: q?.longName || q?.shortName || m.longName || m.shortName || null, regular: reg, extended,
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
