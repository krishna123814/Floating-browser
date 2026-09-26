// binance-proxy.ts  —  Render par chalne wala Binance proxy
//
// Kya badla (bandwidth bachane ke liye):
//   1. ON-DEMAND SNAPSHOT  →  GET /snapshot?strikes=20[&expiries=N]
//      Proxy Binance ke 3 WebSocket (option mark, option trade, spot) khud sunta hai
//      aur latest data apni memory mein rakhta hai. Binance → Render ka data Render
//      ke liye INBOUND hai (bill nahi hota). HF Space jab chahe /snapshot maangti
//      hai aur sirf ATM ± N strikes ka COMPACT + GZIP data wapas milta hai.
//      Jab koi /snapshot nahi maangta (IDLE_STOP_SEC), Binance WS band ho jaate
//      hain — tab Render ko koi kaam nahi, aur free instance so sakta hai.
//   2. FILTERED REST  →  /eapi/v1/exchangeInfo, /eapi/v1/ticker, /eapi/v1/mark
//      (bina `symbol` param ke) ab sirf BTC ke contracts (aur exchangeInfo mein
//      sirf zaroori fields) wapas karte hain, chhote server-side cache ke saath.
//   3. GZIP  →  saare bade JSON responses gzip hote hain (agar client accept kare).
//   4. LEGACY  →  purane /ws/mark, /ws/trade, /ws/spot relay abhi bhi maujood hain
//      (rollback ke liye) — par ye poora data forward karte hain, bandwidth zyada.
//
// NEW: /trade/... endpoints (Binance Options, token-protected) — neeche "TRADE MODULE" dekho.
// STEP 1 (safety): (a) public REST forward ab ALLOWLIST + GET-only hai, signed/private calls block,
//   (b) POST /trade/panic (sab orders cancel + sab positions reduceOnly close),
//   (c) Binance tick/qty-step exchangeInfo se (GET /trade/ticksize), order rejects se pehle check,
//   (d) 429/418 backoff, GET retry, -1021 time-sync auto-fix.
//
// Env vars (sab optional):
//   PORT             (Render khud deta hai)
//   IDLE_STOP_SEC    default 60   — itni der /snapshot na aaye to Binance WS band
//   TICKER_TTL_SEC   default 5    — 24h ticker (OI/volume/chg%) refresh gap
//   FILTER_REST      default true — "false" karne par REST ka filtering band
//   EXTRA_PUBLIC_PATHS  comma-separated extra public REST paths (allowlist mein jodne ke liye)

// ── Config ────────────────────────────────────────────────────────────────
const PORT = Number(Deno.env.get("PORT") ?? 8000);
const IDLE_STOP_MS = Number(Deno.env.get("IDLE_STOP_SEC") ?? 60) * 1000;
const TICKER_TTL_MS = Number(Deno.env.get("TICKER_TTL_SEC") ?? 5) * 1000;
const FILTER_REST = (Deno.env.get("FILTER_REST") ?? "true").toLowerCase() !== "false";

const SYMBOL_PREFIX = "BTC-";          // sirf BTC options (app.py bhi sirf BTCUSDT use karta hai)
const DEFAULT_STRIKES = 20;            // ATM ke dono taraf — app.py BINANCE_OC_STRIKE_WINDOW jaisa
const WARMUP_MS = 6000;                // cold start par pehla data aane tak max wait
const QUOTE_PRUNE_MS = 30 * 60 * 1000; // itni der se update nahi hua (expired contract) to hata do

const UPSTREAM_MAP: Record<string, string> = {
  "/api/": "https://api.binance.com/api/",
  "/eapi/": "https://eapi.binance.com/eapi/",
};

const BN_WS = {
  mark: "wss://fstream.binance.com/market/stream?streams=btcusdt@optionMarkPrice",
  trade: "wss://fstream.binance.com/public/stream?streams=btcusdt@optionTrade",
  spot: "wss://stream.binance.com:9443/ws/btcusdt@aggTrade",
};

// ── Helpers ───────────────────────────────────────────────────────────────
const nowMs = () => Date.now();
const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

// ── Binance request-rate tracker (ADVANCED — 2026-09-25) ──────────────────
// Pehle sirf ek total counter tha (bnReqLog) — "60s mein kitni requests gayi"
// bata deta tha, par "kaunsi request se problem hai" ka jawab nahi deta tha.
// Ab har outbound Binance call ek endpoint-tag + (optional) symbol + cache-hit
// flag + estimated weight ke saath log hoti hai. Isse ye sab nikal sakte hain:
//   - endpoint ke hisaab se breakdown (depth vs mark vs ticker vs exchangeInfo...)
//   - depth ke liye per-symbol breakdown (kaunsa strike sabse zyada poll ho raha)
//   - cache HIT vs MISS split (sirf MISS = asli Binance call; poll-frequency ka
//     doosra number hai, isse "browser bahut fast poll kar raha" vs "cache leak"
//     alag pehchaan sakte hain)
//   - short burst windows (5s/10s) — Binance ka 418 IP-ban weight-limit se ALAG
//     ek burst/order-rate limit bhi hai (dekho _ocPollDepth comment), 60s window
//     itna chhota burst nahi pakadta
//   - estimated weight (Binance ka official per-route weight-table public nahi
//     mila reliably, isliye ye APPROX hai — relative comparison ke liye kaafi
//     hai; asli calibration ke liye Binance ke response header
//     x-mbx-used-weight-1m ka delta bhi saath mein track karte hain)

interface BnCall { ts: number; endpoint: string; symbol: string | null; cacheHit: boolean; weight: number }
const BN_LOG_MAX = 4000;              // ~ka andar rakhte hain, purane trim ho jaate hain
const bnLog: BnCall[] = [];

let bnLastUsedWeight: number | null = null;
let bnLastUsedWeightAt = 0;

// ── IP-ban state (418) — probe-loop (neeche) isi flag ko dekhta hai ────────
// 429 (rate-limit warning) alag hai — wo खुद हल्का हो jaata hai jab hum kam
// requests bhejein, retry-loop nahi chahiye. 418 (hard IP ban) hi wo state
// hai jiske liye "kab khatam hui" check karna zaroori hai — isliye sirf 418
// par ye flag set hota hai.
let bnIpBanned = false;
let bnIpBanSince = 0;

// Estimated weight table — Binance EAPI (options) public GET routes zyaadatar
// weight 1 hain; depth limit ke hisaab se badhta hai (Binance ke aam pattern
// jaisa — 50 tak halka, jitna bada limit utna weight). Calibrate karne ke
// liye bnLastUsedWeight ke actual delta ko is estimate se compare karo.
function estimateWeight(endpoint: string, params?: URLSearchParams): number {
  if (endpoint === "/eapi/v1/depth") {
    const limit = params ? (parseInt(params.get("limit") ?? "", 10) || 100) : 100;
    if (limit <= 50) return 1;
    if (limit <= 100) return 5;
    if (limit <= 500) return 10;
    return 20;
  }
  return 1; // exchangeInfo, ticker, mark, time, klines, trades, index, signed order calls — sab default 1
}

// endpoint: jaisa "/eapi/v1/depth", "ticker-refresh", "time-sync" (readable tag)
// cacheHit=true wale calls Binance tak nahi jaati (sirf proxy-cache serve hui) —
// inpar weight estimate nahi lagate, kyunki ye asli Binance load nahi hai.
function trackBn(endpoint: string, opts: { resp?: Response; symbol?: string | null; cacheHit?: boolean; params?: URLSearchParams } = {}): void {
  const t = nowMs();
  const cacheHit = opts.cacheHit ?? false;
  bnLog.push({
    ts: t,
    endpoint,
    symbol: opts.symbol ?? null,
    cacheHit,
    weight: cacheHit ? 0 : estimateWeight(endpoint, opts.params),
  });
  if (bnLog.length > BN_LOG_MAX) bnLog.splice(0, bnLog.length - BN_LOG_MAX);
  if (opts.resp) {
    const w = opts.resp.headers.get("x-mbx-used-weight-1m");
    if (w != null) {
      const n = parseInt(w, 10);
      if (Number.isFinite(n)) { bnLastUsedWeight = n; bnLastUsedWeightAt = t; }
    }
    if (opts.resp.status === 418) {
      if (!bnIpBanned) {
        bnIpBanned = true;
        bnIpBanSince = t;
        captureFailSnapshot(`IP ban (418) shuru hua — endpoint: ${endpoint}`);
      }
    } else if (opts.resp.ok) {
      // Koi bhi successful Binance call ban flag clear kar sakti hai — probe-loop
      // bhi isi rasta se apna khud ka check confirm karta hai (neeche dekho).
      bnIpBanned = false;
    }
  }
}

// Legacy shim — purane call-sites jo sirf resp pass karte the, wo ab bhi
// chalte hain (endpoint tag ke bina), naye call-sites trackBn() seedha use karein.
function trackBnRequest(resp?: Response): void {
  trackBn("untagged", { resp });
}

function pruneBnLog(sinceMs: number): void {
  const cutoff = nowMs() - sinceMs;
  while (bnLog.length && bnLog[0].ts < cutoff) bnLog.shift();
}

function bnRateStats() {
  const t = nowMs();
  pruneBnLog(10 * 60_000); // 10 min se purana kabhi kaam ka nahi, permanently trim

  function windowCalls(ms: number) {
    const cutoff = t - ms;
    return bnLog.filter((c) => c.ts >= cutoff);
  }

  const last60s = windowCalls(60_000);
  const last10s = windowCalls(10_000);
  const last5s = windowCalls(5_000);

  const misses60s = last60s.filter((c) => !c.cacheHit);
  const hits60s = last60s.length - misses60s.length;

  // ── per-endpoint breakdown (last 60s) — seen (proxy ko mili) vs sent (Binance tak gayi) ──
  const byEndpoint = new Map<string, { seen: number; sent: number; cacheHits: number; weight: number }>();
  for (const c of last60s) {
    const e = byEndpoint.get(c.endpoint) ?? { seen: 0, sent: 0, cacheHits: 0, weight: 0 };
    e.seen++;
    if (c.cacheHit) e.cacheHits++;
    else { e.sent++; e.weight += c.weight; }
    byEndpoint.set(c.endpoint, e);
  }
  const endpoints = [...byEndpoint.entries()]
    .map(([endpoint, v]) => ({ endpoint, ...v }))
    .sort((a, b) => b.sent - a.sent || b.seen - a.seen);

  // ── depth: per-symbol breakdown (last 5 min), sirf actual Binance-bound calls ──
  const depthWindow = windowCalls(5 * 60_000).filter((c) => c.endpoint === "/eapi/v1/depth" && !c.cacheHit);
  const bySymbol = new Map<string, number>();
  for (const c of depthWindow) {
    const s = c.symbol ?? "(unknown)";
    bySymbol.set(s, (bySymbol.get(s) ?? 0) + 1);
  }
  const depth_top_symbols_5m = [...bySymbol.entries()]
    .map(([symbol, sent]) => ({ symbol, sent }))
    .sort((a, b) => b.sent - a.sent)
    .slice(0, 8);

  return {
    requests_last_60s: misses60s.length,          // backward-compatible field — sirf asli Binance-bound calls
    requests_seen_last_60s: last60s.length,        // proxy ko total kitni requests mili (cache hit + miss)
    cache_hits_last_60s: hits60s,
    burst_last_10s: last10s.filter((c) => !c.cacheHit).length,
    burst_last_5s: last5s.filter((c) => !c.cacheHit).length,
    estimated_weight_last_60s: misses60s.reduce((s, c) => s + c.weight, 0),
    binance_used_weight_1m: bnLastUsedWeight,
    binance_used_weight_age_ms: bnLastUsedWeight != null ? (t - bnLastUsedWeightAt) : null,
    weight_limit_1m: 6000,
    ip_banned: bnIpBanned,
    ip_ban_age_ms: bnIpBanned ? (t - bnIpBanSince) : null,
    ip_ban_next_probe_in_ms: bnIpBanned ? Math.max(0, bnNextProbeAt - t) : null,
    endpoints,
    depth_top_symbols_5m,
  };
}

// ── Frozen fail/ban snapshot — "kahan fail ho raha tha" freeze-frame ───────
// Live /ratelimit wala data har 4s refresh mein latest 60s window dikhata hai
// — matlab agar ban lagne ke turant baad traffic khud hi kam ho jaaye (jaisa
// hota hai, kyunki fail hone par retries kam hoti hain), to dashboard pe wo
// "culprit" moment 4s ke andar hi wipe ho jaata hai. Isko fix karne ke liye
// jis pal 418-ban shuru hota hai YA koi REST endpoint "failing" state mein
// (pehli baar) enter karta hai, usi pal ka bnRateStats() snapshot yahan
// FREEZE kar dete hain. Ye snapshot tab tak screen (aur agli fail-email) par
// wahi ka wahi dikhta rehta hai jab tak agla naya fail/ban episode na aaye —
// beech mein normal 4s live-refresh isko overwrite nahi karta.
let lastFailSnapshot: { capturedAt: number; reason: string; stats: ReturnType<typeof bnRateStats> } | null = null;

function captureFailSnapshot(reason: string): void {
  lastFailSnapshot = { capturedAt: nowMs(), reason, stats: bnRateStats() };
  console.log(`[fail-snapshot] frozen — reason: ${reason}`);
}

function lastFailSnapshotReport(): null | {
  captured_at_ms: number; age_ms: number; reason: string; stats: ReturnType<typeof bnRateStats>;
} {
  if (!lastFailSnapshot) return null;
  return {
    captured_at_ms: lastFailSnapshot.capturedAt,
    age_ms: nowMs() - lastFailSnapshot.capturedAt,
    reason: lastFailSnapshot.reason,
    stats: lastFailSnapshot.stats,
  };
}

// Plain-text form — dashboard aur email dono isi ek function se banate hain,
// taaki dono jagah EXACT same breakdown dikhe (do alag formatting maintain
// nahi karni padti).
function fmtFailSnapshotText(snap: ReturnType<typeof lastFailSnapshotReport>): string {
  if (!snap) return "   (abhi tak koi fail/ban episode nahi hua — snapshot khaali hai)";
  const s = snap.stats;
  const epLines = s.endpoints.length
    ? s.endpoints.slice(0, 10)
        .map((e) => `   ${e.endpoint} — seen:${e.seen} sent:${e.sent} cacheHits:${e.cacheHits} weight:${e.weight}`)
        .join("\n")
    : "   (us waqt koi request nahi thi)";
  return (
    `   Reason: ${snap.reason}\n` +
    `   Frozen ${Math.round(snap.age_ms / 1000)}s pehle (us waqt ka 60s window hai, live nahi):\n` +
    `   requests seen: ${s.requests_seen_last_60s} | sent: ${s.requests_last_60s} | cacheHits: ${s.cache_hits_last_60s}\n` +
    `   burst 10s/5s: ${s.burst_last_10s}/${s.burst_last_5s} | est weight: ${s.estimated_weight_last_60s} | binance used_weight_1m: ${s.binance_used_weight_1m ?? "n/a"}\n` +
    `   Per-endpoint us waqt (last 60s):\n${epLines}`
  );
}

// fmtFailSnapshotText() jaisa hi, par HTML mein: ek chhota "summary" table (reason,
// frozen-age, seen/sent/cacheHits, burst, weight) + ek per-endpoint table.
function fmtFailSnapshotHtml(snap: ReturnType<typeof lastFailSnapshotReport>): string {
  if (!snap) {
    return `<p style="margin:4px 0 16px;color:#64748b;font-size:13px;">(abhi tak koi fail/ban episode nahi hua)</p>`;
  }
  const s = snap.stats;
  const summaryRows = [[
    escHtml(snap.reason),
    `${Math.round(snap.age_ms / 1000)}s pehle`,
    `${s.requests_seen_last_60s} / ${s.requests_last_60s} / ${s.cache_hits_last_60s}`,
    `${s.burst_last_10s} / ${s.burst_last_5s}`,
    String(s.estimated_weight_last_60s),
    String(s.binance_used_weight_1m ?? "n/a"),
  ]];
  const summary = tableHtml(
    ["Reason", "Frozen", "Seen/Sent/CacheHits", "Burst 10s/5s", "Est. Weight", "BN used_weight_1m"],
    summaryRows,
    "#dc2626",
  );
  const epRows = s.endpoints.slice(0, 10).map((e) => [
    escHtml(e.endpoint), String(e.seen), String(e.sent), String(e.cacheHits), String(e.weight),
  ]);
  const epTable = tableHtml(["Endpoint", "Seen", "Sent", "CacheHits", "Weight"], epRows, "#64748b");
  return summary + epTable;
}

// ── IP-ban recovery probe — app se poori tarah INDEPENDENT ─────────────────
// Jaise hi koi bhi Binance call 418 dekhti hai (trackBn() ke andar), ye loop
// har BN_BAN_PROBE_INTERVAL_MS (default 5 min) par SIRF EK chhoti REST call
// (/eapi/v1/time — sabse halka public endpoint, weight ~1) bhejta hai taaki
// pata chale ban khatam hui ya nahi. Jab tak bnIpBanned false hai, ye loop
// koi Binance call nahi karta — bilkul idle (sirf har 5 min ek baar jaagta
// hai, flag check karta hai, phir so jaata hai). App khuli ho ya band, iska
// koi farak nahi padta — ye rulesMonitorLoop jaisa hi standalone hai.
const BN_BAN_PROBE_INTERVAL_MS = 5 * 60_000;
let bnNextProbeAt = 0;
let bnBanProbeRunning = false;

async function bnBanProbeLoop(): Promise<void> {
  if (bnBanProbeRunning) return;
  bnBanProbeRunning = true;
  while (true) {
    bnNextProbeAt = nowMs() + BN_BAN_PROBE_INTERVAL_MS;
    await sleep(BN_BAN_PROBE_INTERVAL_MS);
    if (!bnIpBanned) continue;   // ban nahi hai → is cycle mein Binance ko bilkul mat chhedo
    try {
      const r = await fetch(`${EAPI}/eapi/v1/time`, { signal: AbortSignal.timeout(8000) });
      trackBn("/eapi/v1/time (ban-probe)", { resp: r });   // ye hi bnIpBanned ko clear karega agar 200 aaya
      if (r.ok) {
        console.log("[ban-probe] Binance ban khatam ho gayi — proxy normal");
        restOk("ip-ban-probe");
      } else {
        restFail("ip-ban-probe", `HTTP ${r.status} — ban abhi bhi active`);
        console.log(`[ban-probe] abhi bhi ${r.status} — agla check ${BN_BAN_PROBE_INTERVAL_MS / 60000}min baad`);
      }
    } catch (e) {
      // Network-level fail (timeout/DNS) — ban-state nahi badalte, agli cycle phir try karega
      console.log(`[ban-probe] fetch fail: ${String(e).slice(0, 150)}`);
    }
  }
}

function num(x: unknown): number {
  const n = typeof x === "number" ? x : parseFloat(String(x ?? ""));
  return Number.isFinite(n) ? n : 0;
}
function present(v: unknown): boolean {
  return v !== undefined && v !== null && v !== "";
}

async function respondText(
  req: Request,
  text: string,
  status = 200,
  contentType = "application/json",
): Promise<Response> {
  const headers = new Headers({
    "Content-Type": contentType,
    "Access-Control-Allow-Origin": "*",
    "Cache-Control": "no-store",
  });
  const acceptsGzip = /\bgzip\b/i.test(req.headers.get("accept-encoding") ?? "");
  if (acceptsGzip && text.length > 1024) {
    const gz = new Blob([text]).stream().pipeThrough(new CompressionStream("gzip"));
    const buf = await new Response(gz).arrayBuffer();
    headers.set("Content-Encoding", "gzip");
    headers.set("Vary", "Accept-Encoding");
    return new Response(buf, { status, headers });
  }
  return new Response(text, { status, headers });
}
const respondJson = (req: Request, body: unknown, status = 200) =>
  respondText(req, JSON.stringify(body), status);

// ── Live store (memory mein) ──────────────────────────────────────────────
// Field names bilkul wahi jo app.py ke _BN_LIVE_QUOTES rows mein use hote hain.
interface Quote {
  mark?: string; bid?: string; ask?: string; last?: string;
  delta?: string; gamma?: string; theta?: string; vega?: string;
  iv?: string; buy_iv?: string; sell_iv?: string;
  oi?: number; chg?: number; chgp?: number; volume?: number; vol_cum?: number;
  mark_ts?: number; last_ts?: number; ts: number;
}
type StrField =
  | "mark" | "bid" | "ask" | "last" | "delta" | "gamma"
  | "theta" | "vega" | "iv" | "buy_iv" | "sell_iv";

const quotes = new Map<string, Quote>();
const symInfo = new Map<string, { exp: string; strike: number }>();
const spot: { price: number | null; ts: number } = { price: null, ts: 0 };

function getQuote(sym: unknown): Quote | null {
  if (typeof sym !== "string" || !sym.startsWith(SYMBOL_PREFIX)) return null;
  let q = quotes.get(sym);
  if (!q) {
    const p = sym.split("-"); // BTC-YYMMDD-STRIKE-C|P
    const strike = Number(p[2]);
    if (p.length < 4 || !Number.isFinite(strike)) return null;
    q = { ts: nowMs() };
    quotes.set(sym, q);
    symInfo.set(sym, { exp: p[1], strike });
  }
  return q;
}

// Mark-stream keys → app.py ke row keys (app.py _BN_MARK_FIELD_MAP jaisa hi)
const MARK_FIELD_MAP: Array<[StrField, string]> = [
  ["last", "c"], ["bid", "bo"], ["ask", "ao"], ["mark", "mp"],
  ["delta", "d"], ["gamma", "g"], ["theta", "t"], ["vega", "v"],
  ["iv", "vo"], ["buy_iv", "b"], ["sell_iv", "a"],
];

// deno-lint-ignore no-explicit-any
function unwrap(data: any): any[] {
  const payload = data && typeof data === "object" && "data" in data ? data.data : data;
  if (Array.isArray(payload)) return payload;
  return payload && typeof payload === "object" ? [payload] : [];
}

// deno-lint-ignore no-explicit-any
function applyMark(msg: any) {
  const q = getQuote(msg?.s);
  if (!q) return;
  const t = nowMs();
  for (const [target, key] of MARK_FIELD_MAP) {
    if (present(msg[key])) {
      q[target] = msg[key];
      if (target === "mark") q.mark_ts = t;
    }
  }
  q.ts = t;
}

// deno-lint-ignore no-explicit-any
function applyTrade(msg: any) {
  const q = getQuote(msg?.s);
  if (!q) return;
  const t = nowMs();
  if (present(msg.p)) {
    q.last = msg.p;
    q.last_ts = t;
  }
  if (present(msg.q)) q.vol_cum = (q.vol_cum ?? 0) + num(msg.q);
  q.ts = t;
}

// deno-lint-ignore no-explicit-any
function applySpot(msg: any) {
  const m = msg && typeof msg === "object" && msg.data ? msg.data : msg;
  const p = parseFloat(m?.p);
  if (Number.isFinite(p)) {
    spot.price = p;
    spot.ts = nowMs();
  }
}

// ── Upstream Binance WebSocket feed (auto-reconnect + backoff + watchdog) ──
class Feed {
  ws: WebSocket | null = null;
  connected = false;
  stopped = true;
  lastMsg = 0;
  msgCount = 0;
  failCount = 0;
  // deno-lint-ignore no-explicit-any
  retryTimer: any = null;
  // deno-lint-ignore no-explicit-any
  constructor(public name: string, public url: string, public onData: (d: any) => void, public staleMs: number) {}

  start() {
    if (!this.stopped) return;
    this.stopped = false;
    this.failCount = 0;
    this.connect();
  }
  stop() {
    this.stopped = true;
    clearTimeout(this.retryTimer);
    const ws = this.ws;
    this.ws = null;
    this.connected = false;
    try { ws?.close(); } catch { /* ignore */ }
  }
  private connect() {
    if (this.stopped) return;
    let ws: WebSocket;
    try {
      ws = new WebSocket(this.url);
    } catch {
      this.scheduleRetry();
      return;
    }
    this.ws = ws;
    ws.onopen = () => {
      if (this.ws !== ws) return;
      this.connected = true;
      this.failCount = 0;
      this.lastMsg = nowMs();
    };
    ws.onmessage = (e: MessageEvent) => {
      if (this.ws !== ws) return;
      this.lastMsg = nowMs();
      this.msgCount++;
      try {
        this.onData(JSON.parse(typeof e.data === "string" ? e.data : ""));
      } catch { /* bad frame ignore */ }
    };
    ws.onerror = () => { try { ws.close(); } catch { /* ignore */ } };
    ws.onclose = () => {
      if (this.ws !== ws) return; // stop() ya watchdog ne already handle kiya
      this.ws = null;
      this.connected = false;
      this.scheduleRetry();
    };
  }
  private scheduleRetry() {
    if (this.stopped) return;
    this.failCount++;
    const delay = Math.min(1000 * 2 ** (this.failCount - 1), 30000) + Math.random() * 500;
    this.retryTimer = setTimeout(() => this.connect(), delay);
  }
  // Half-open TCP: socket "open" hai par data nahi aa raha → force reconnect
  watchdog() {
    if (this.stopped || !this.ws || this.staleMs <= 0) return;
    if (nowMs() - this.lastMsg > this.staleMs) {
      try { this.ws.close(); } catch { /* ignore */ }
    }
  }
  status(t: number) {
    return { connected: this.connected, age_ms: this.lastMsg ? t - this.lastMsg : null };
  }
}

const feeds = {
  mark: new Feed("mark", BN_WS.mark, (d) => { for (const it of unwrap(d)) applyMark(it); }, 30_000),
  trade: new Feed("trade", BN_WS.trade, (d) => { for (const it of unwrap(d)) applyTrade(it); }, 0), // trade sparse hoti hai
  spot: new Feed("spot", BN_WS.spot, applySpot, 30_000),
};
let feedsStopped = true;
let lastDemand = 0;

function ensureFeeds(): boolean {
  lastDemand = nowMs();
  const wasCold = feedsStopped;
  if (feedsStopped) {
    feedsStopped = false;
    for (const f of Object.values(feeds)) f.start();
    startSession();   // naya "app session" shuru — HF app ne pehli baar (ya idle-stop ke baad phir se) maanga
  }
  return wasCold;
}

function stopFeedsAndClear() {
  for (const f of Object.values(feeds)) f.stop();
  feedsStopped = true;
  quotes.clear();
  symInfo.clear();
  spot.price = null;
  spot.ts = 0;
  tickerTs = 0;
  endSession();   // session khatam — IDLE_STOP_SEC se zyada der koi demand nahi aayi
}

setInterval(() => {
  const t = nowMs();
  if (!feedsStopped && t - lastDemand > IDLE_STOP_MS) {
    stopFeedsAndClear();
    return;
  }
  if (!feedsStopped) {
    for (const f of Object.values(feeds)) f.watchdog();
    for (const [sym, q] of quotes) {
      if (t - q.ts > QUOTE_PRUNE_MS) {
        quotes.delete(sym);
        symInfo.delete(sym);
      }
    }
  }
}, 5000);

// ── 24h ticker (OI / volume / change%) — WS par nahi aata, isliye REST ─────
let tickerTs = 0;
let tickerErr: string | null = null;
let tickerInflight: Promise<void> | null = null;
// FIX (2026-09-25): pehle tickerTs sirf SUCCESS par update hota tha, isliye
// fail hone par agli hi /snapshot request turant retry kar deti thi (TTL
// gate fail ke baad bhi "expired" dikhta tha) — 429 ke waqt ye retry-loop
// Binance ko aur zyada hammer karta tha. Ab har attempt (pass ya fail) ke
// baad ek chhota backoff set hota hai; 429/418 par zyada lamba backoff.
let tickerNextTryMs = 0;

function refreshTicker(force = false): Promise<void> {
  if (tickerInflight) return tickerInflight;
  const t0 = nowMs();
  if (!force && (t0 - tickerTs < TICKER_TTL_MS || t0 < tickerNextTryMs)) return Promise.resolve();
  tickerInflight = (async () => {
    try {
      const r = await fetch("https://eapi.binance.com/eapi/v1/ticker");
      trackBn("/eapi/v1/ticker", { resp: r });
      if (!r.ok) {
        tickerErr = `ticker HTTP ${r.status}`;
        restFail("24h-ticker (refreshTicker)", tickerErr);
        // 429 (rate-limit) / 418 (ban) → lamba backoff; Retry-After header ho to usi ko maano
        if (r.status === 429 || r.status === 418) {
          const retryAfterSec = parseInt(r.headers.get("retry-after") ?? "", 10);
          const backoffMs = Number.isFinite(retryAfterSec) && retryAfterSec > 0
            ? retryAfterSec * 1000
            : Math.max(TICKER_TTL_MS * 4, 20_000);
          tickerNextTryMs = nowMs() + backoffMs;
        } else {
          tickerNextTryMs = nowMs() + TICKER_TTL_MS;   // normal fail → sirf ek TTL cycle skip
        }
        return;
      }
      const arr = await r.json();
      if (!Array.isArray(arr)) {
        tickerErr = "ticker: unexpected response";
        restFail("24h-ticker (refreshTicker)", tickerErr);
        tickerNextTryMs = nowMs() + TICKER_TTL_MS;
        return;
      }
      const t = nowMs();
      for (const x of arr) {
        const q = getQuote(x?.symbol);
        if (!q) continue;
        q.oi = num(x.openInterest);
        q.chg = num(x.priceChange);
        q.chgp = num(x.priceChangePercent);
        q.volume = num(x.volume);
        // WS ne abhi tak kuch na bheja ho to REST se ek baar fallback fill (app.py jaisa)
        if (!present(q.bid) && present(x.bidPrice)) q.bid = String(x.bidPrice);
        if (!present(q.ask) && present(x.askPrice)) q.ask = String(x.askPrice);
        if (!present(q.last) && present(x.lastPrice)) q.last = String(x.lastPrice);
        if (!present(q.mark) && present(x.lastPrice)) q.mark = String(x.lastPrice);
        q.ts = t;
      }
      tickerTs = t;
      tickerErr = null;
      tickerNextTryMs = 0;
      restOk("24h-ticker (refreshTicker)");
    } catch (e) {
      tickerErr = `ticker fetch failed: ${e}`;
      restFail("24h-ticker (refreshTicker)", tickerErr);
      tickerNextTryMs = nowMs() + TICKER_TTL_MS;
    } finally {
      tickerInflight = null;
    }
  })();
  return tickerInflight;
}

async function fetchSpotRest() {
  try {
    const r = await fetch("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT");
    trackBn("/api/v3/ticker/price", { resp: r, symbol: "BTCUSDT" });
    if (!r.ok) { restFail("spot-price (fetchSpotRest)", `HTTP ${r.status}`); return; }
    const j = await r.json();
    const p = parseFloat(j?.price);
    if (Number.isFinite(p)) {
      spot.price = p;
      spot.ts = nowMs();
      restOk("spot-price (fetchSpotRest)");
    } else {
      restFail("spot-price (fetchSpotRest)", "invalid price in response");
    }
  } catch (e) {
    restFail("spot-price (fetchSpotRest)", String(e));
  }
}

// Cold start: pehla mark tick + spot + ticker aane tak (max maxMs) ruko
async function warmup(maxMs: number) {
  const t0 = nowMs();
  const wait = (async () => {
    while (nowMs() - t0 < maxMs) {
      if (feeds.mark.msgCount > 0 && spot.price !== null) return;
      await sleep(100);
    }
  })();
  await Promise.all([refreshTicker(true), wait]);
  if (spot.price === null) await fetchSpotRest();
}

// ── Snapshot builder ──────────────────────────────────────────────────────
// Row = [symbol, ...SNAP_KEYS ki values]. Keys response mein ek baar jaati hain.
const SNAP_KEYS = [
  "mark", "bid", "ask", "last", "delta", "gamma", "theta", "vega",
  "iv", "buy_iv", "sell_iv", "oi", "chg", "chgp", "volume", "vol_cum",
  "mark_age_ms", "last_age_ms",
] as const;

function rowFor(sym: string, q: Quote, t: number): unknown[] {
  const row: unknown[] = [sym];
  for (const k of SNAP_KEYS) {
    if (k === "mark_age_ms") row.push(q.mark_ts ? t - q.mark_ts : null);
    else if (k === "last_age_ms") row.push(q.last_ts ? t - q.last_ts : null);
    else row.push(q[k] ?? null);
  }
  return row;
}

function buildSnapshot(strikeWindow: number, maxExpiries: number) {
  const t = nowMs();
  const byExp = new Map<string, Map<number, string[]>>();
  for (const [sym, info] of symInfo) {
    if (!quotes.has(sym)) continue;
    let sm = byExp.get(info.exp);
    if (!sm) byExp.set(info.exp, (sm = new Map()));
    const arr = sm.get(info.strike);
    if (arr) arr.push(sym);
    else sm.set(info.strike, [sym]);
  }

  const rows: unknown[][] = [];
  const price = spot.price;
  if (price !== null) {
    let exps = [...byExp.keys()].sort(); // YYMMDD → lexicographic = chronological
    if (maxExpiries > 0) exps = exps.slice(0, maxExpiries);
    for (const exp of exps) {
      const sm = byExp.get(exp)!;
      const strikes = [...sm.keys()].sort((a, b) => a - b);
      let atmIdx = 0;
      let best = Infinity;
      strikes.forEach((s, i) => {
        const d = Math.abs(s - price);
        if (d < best) { best = d; atmIdx = i; }
      });
      const lo = Math.max(0, atmIdx - strikeWindow);
      const hi = Math.min(strikes.length, atmIdx + strikeWindow + 1);
      for (let i = lo; i < hi; i++) {
        for (const sym of sm.get(strikes[i])!) {
          const q = quotes.get(sym);
          if (q) rows.push(rowFor(sym, q, t));
        }
      }
    }
  }

  return {
    v: 1,
    now_ms: t,
    spot: price,
    spot_age_ms: spot.ts ? t - spot.ts : null,
    feeds: { mark: feeds.mark.status(t), trade: feeds.trade.status(t), spot: feeds.spot.status(t) },
    ticker_age_ms: tickerTs ? t - tickerTs : null,
    ticker_error: tickerErr,
    total_symbols: quotes.size,
    keys: SNAP_KEYS,
    rows,
  };
}

async function handleSnapshot(req: Request, url: URL): Promise<Response> {
  const sw = parseInt(url.searchParams.get("strikes") ?? "", 10);
  const strikeWindow = Number.isFinite(sw) && sw >= 0 ? Math.min(sw, 1000) : DEFAULT_STRIKES;
  const me = parseInt(url.searchParams.get("expiries") ?? "", 10);
  const maxExpiries = Number.isFinite(me) && me > 0 ? me : 0; // 0 = saari expiries

  const cold = ensureFeeds();
  if (cold) await warmup(WARMUP_MS);
  else void refreshTicker(false);

  return respondJson(req, buildSnapshot(strikeWindow, maxExpiries));
}

// (2026-09-25) /orderbook route HATA DIYA — dead code tha, koi caller nahi
// tha (chart-1.html aur app.py dono depth ke liye generic /eapi/v1/depth
// REST-forward use karte hain, is dedicated route ko kabhi hit hi nahi
// karte the). Depth-specific tracking (per-symbol breakdown) ab generic
// REST-forward path mein hi hoti hai — dekho bnRateStats() ka
// depth_top_symbols_5m aur REST forward handler mein trackBn() call.

// ── Filtered + cached REST (sirf bina-symbol wale bade calls) ──────────────
const REST_TTL_MS: Record<string, number> = {
  "/eapi/v1/exchangeInfo": 300_000, // strikes/expiries rarely badalte hain
  "/eapi/v1/ticker": 3_000,
  "/eapi/v1/mark": 2_000,
  // (2026-09-25) FIX: /eapi/v1/depth pehle yahan nahi tha, isliye har order-book
  // poll (browser se 1.5s interval par, chart.html _ocPollInlineBookViaProxy)
  // seedha Binance tak uncached jaata tha — is chhoti si file mein jitne bhi
  // clients/strikes ek saath khule hon, sab Binance ko alag-alag hit karte the.
  // Isi se ek din Binance ne "Way too many requests" bolkar IP 418-ban kar diya.
  // 1.2s TTL browser ke 1.5s poll se chhota hai (fresh-enough), lekin ek hi
  // symbol ke overlapping/parallel requests ko dedupe kar deta hai.
  "/eapi/v1/depth": 1_200,
};
const restCache = new Map<string, { ts: number; status: number; text: string; ct: string }>();

// ── Public REST forward LOCK (Step 1) ──────────────────────────────────────
// Pehle /api/ aur /eapi/ ke SAARE paths (signed bhi) bina auth forward hote the. Ab sirf
// neeche ke public market-data GET paths jaate hain. Signed/private calls (account, order,
// position...) sirf token-protected /trade/... se hoti hain. Naya public path chahiye to
// Render env EXTRA_PUBLIC_PATHS mein comma-separated daalo (e.g. /eapi/v1/openInterest).
const PUBLIC_REST_PATHS = new Set<string>([
  "/api/v3/ping", "/api/v3/time", "/api/v3/exchangeInfo", "/api/v3/klines",
  "/api/v3/ticker/price", "/api/v3/ticker/24hr", "/api/v3/ticker/bookTicker",
  "/api/v3/depth", "/api/v3/trades", "/api/v3/avgPrice",
  "/eapi/v1/ping", "/eapi/v1/time", "/eapi/v1/exchangeInfo", "/eapi/v1/ticker",
  "/eapi/v1/mark", "/eapi/v1/depth", "/eapi/v1/klines", "/eapi/v1/trades", "/eapi/v1/index",
]);
for (const p of (Deno.env.get("EXTRA_PUBLIC_PATHS") ?? "").split(",")) {
  const t = p.trim();
  if (t.startsWith("/api/") || t.startsWith("/eapi/")) PUBLIC_REST_PATHS.add(t);
}

function forwardBlockReason(req: Request, url: URL): string | null {
  if (req.method !== "GET" && req.method !== "HEAD") return "Public forward par sirf GET allowed hai";
  for (const k of url.searchParams.keys()) {
    const kl = k.toLowerCase();
    if (kl === "signature" || kl === "timestamp" || kl === "recvwindow") return "Signed requests yahan block hain (use /trade/...)";
  }
  if (!PUBLIC_REST_PATHS.has(url.pathname)) return `Path public allowlist mein nahi hai: ${url.pathname}`;
  return null;
}

// deno-lint-ignore no-explicit-any
function filterRest(pathname: string, data: any): any {
  if (pathname === "/eapi/v1/exchangeInfo" && data && Array.isArray(data.optionSymbols)) {
    return {
      serverTime: data.serverTime,
      optionSymbols: data.optionSymbols
        // deno-lint-ignore no-explicit-any
        .filter((s: any) => s?.underlying === "BTCUSDT" || String(s?.symbol ?? "").startsWith(SYMBOL_PREFIX))
        // deno-lint-ignore no-explicit-any
        .map((s: any) => ({
          symbol: s.symbol,
          underlying: s.underlying,
          strikePrice: s.strikePrice,
          expiryDate: s.expiryDate,
          side: s.side,
        })),
    };
  }
  if ((pathname === "/eapi/v1/ticker" || pathname === "/eapi/v1/mark") && Array.isArray(data)) {
    // deno-lint-ignore no-explicit-any
    return data.filter((x: any) => String(x?.symbol ?? "").startsWith(SYMBOL_PREFIX));
  }
  return data;
}

// ── TRADE MODULE (Binance Options, eapi) ──────────────────────────────────
// Phase 1: read-only endpoints + guarded order/cancel.
// Secrets sirf Render env mein: OPT_API_KEY, OPT_SECRET_KEY, TRADE_TOKEN.
// Ye endpoints (/trade/...) sirf header `X-Trade-Token` sahi hone par chalte hain.
// Koi CORS header nahi — browser se nahi, sirf server-to-server (HF Python) use ke liye.
//
// Env vars:
//   OPT_API_KEY / OPT_SECRET_KEY   Binance Options key (Withdrawal OFF rakho)
//   TRADE_TOKEN                    lamba random secret (HF ke paas bhi wahi hoga)
//   TRADING_ENABLED                "true" ho tabhi naye orders jayenge (default false)
//   MAX_ORDER_QTY                  default 0.01   — ek order ki max quantity
//   MAX_ORDER_USDT                 default 3      — BUY order ka max premium (price*qty)
//   MAX_PRICE_DEV_PCT              default 50     — order price mark se itne % se zyada door nahi
//   MAX_ORDERS_PER_MIN             default 6      — rate limit
//
// Rules: shuru mein sirf BUY. SELL sirf reduceOnly=true (position band karne ke liye).
// Cancel / cancel-all hamesha allowed hain (kill-switch se bhi nahi rukte).

const EAPI = "https://eapi.binance.com";
const OPT_API_KEY = (Deno.env.get("OPT_API_KEY") ?? "").trim();
const OPT_SECRET_KEY = (Deno.env.get("OPT_SECRET_KEY") ?? "").trim();
const TRADE_TOKEN = (Deno.env.get("TRADE_TOKEN") ?? "").trim();
const TRADING_ENABLED = (Deno.env.get("TRADING_ENABLED") ?? "false").trim().toLowerCase() === "true";
const MAX_ORDER_QTY = envNum("MAX_ORDER_QTY", 0.01);
const MAX_ORDER_USDT = envNum("MAX_ORDER_USDT", 3);
const MAX_PRICE_DEV_PCT = envNum("MAX_PRICE_DEV_PCT", 50);
const MAX_ORDERS_PER_MIN = envNum("MAX_ORDERS_PER_MIN", 6);

function envNum(name: string, def: number): number {
  const v = parseFloat(Deno.env.get(name) ?? "");
  return Number.isFinite(v) && v > 0 ? v : def;
}

// ── EMAIL ALERTS (Brevo HTTPS API) ─────────────────────────────────────────
// app.py ke send_alert() jaisa hi, sirf email. Render → Environment mein ye
// 3 env vars daalo (HF wale secrets ki same values):
//   BREVO_API_KEY       = xkeysib-...
//   ALERT_EMAIL_TO      = jis email par alert chahiye
//   BREVO_SENDER_EMAIL  = Brevo mein VERIFIED sender (optional — na ho to ALERT_EMAIL_TO)
//   ALERT_FAIL_COOLDOWN_SEC = (optional, default 3600) ek hi rule ka "close fail"
//                         alert itne second mein max 1 baar (loop har 2s retry karta hai)
//   ALERT_DAILY_MAX     = (optional, default 30) ek din (UTC) mein max itni mails —
//                         Brevo free ki 300/day limit bachane ke liye. Ye cap sirf
//                         FAIL alerts par lagta hai; SL/Target/Trailing "HIT" mail
//                         (compulsory) cap se nahi rukti.
// Ye kabhi throw nahi karta aur trade/exit flow ko block nahi karta (caller
// isse `void` karke chalata hai). Env vars set na hon to chupchaap skip.
const BREVO_API_KEY = (Deno.env.get("BREVO_API_KEY") ?? "").trim();
const ALERT_EMAIL_TO = (Deno.env.get("ALERT_EMAIL_TO") ?? "").trim();
const BREVO_SENDER_EMAIL = (Deno.env.get("BREVO_SENDER_EMAIL") ?? "").trim() || ALERT_EMAIL_TO;
const ALERT_FAIL_COOLDOWN_MS = envNum("ALERT_FAIL_COOLDOWN_SEC", 3600) * 1000;
const ALERT_DAILY_MAX = envNum("ALERT_DAILY_MAX", 30);
const alertLastSent = new Map<string, number>();   // {key: last_success_ts}
let alertDay = "";        // UTC date (YYYY-MM-DD) jiska count neeche chal raha hai
let alertDayCount = 0;    // us din ab tak ki successful mails

// NOTE: escHtml() pehle se file mein neeche (daily-summary email section) defined
// hai — usi ko reuse karte hain (function declarations hoist hoti hain, isliye
// yahan upar bhi available hai). Dobara define nahi kiya, taaki duplicate na ho.

// Generic styled HTML table builder — email tables (health, snapshot, session, log)
// sab isi ek function se banti hain taaki look consistent rahe. `accent` header row
// ka background color hai; rows ke andar har cell already-safe HTML ho sakta hai
// (escaping caller ki zimmedaari hai, taaki ❌/✅ jaisे icons ya <b> allowed rahein).
function tableHtml(headers: string[], rows: string[][], accent = "#334155"): string {
  if (!rows.length) {
    return `<p style="margin:4px 0 16px;color:#64748b;font-size:13px;">(koi data nahi)</p>`;
  }
  const thStyle = `padding:8px 10px;text-align:left;font-size:12px;font-weight:600;color:#ffffff;background:${accent};border:1px solid #e2e8f0;`;
  const tdStyle = `padding:7px 10px;font-size:12.5px;color:#1e293b;border:1px solid #e2e8f0;`;
  const head = headers.map((h) => `<th style="${thStyle}">${escHtml(h)}</th>`).join("");
  const body = rows
    .map((r, i) => {
      const bg = i % 2 === 0 ? "#ffffff" : "#f8fafc";
      const cells = r.map((c) => `<td style="${tdStyle}background:${bg};">${c}</td>`).join("");
      return `<tr>${cells}</tr>`;
    })
    .join("");
  return `<table style="width:100%;border-collapse:collapse;margin:6px 0 18px;font-family:Segoe UI,Roboto,Arial,sans-serif;">
    <thead><tr>${head}</tr></thead>
    <tbody>${body}</tbody>
  </table>`;
}

// critical=true → daily cap ignore (sirf trade-HIT jaisi compulsory mails ke liye)
// Plain text alert message ko ek basic par saaf-suthri HTML card mein wrap karta hai
// (jab kisi call-site ne apna khud ka HTML nahi diya). Monospace block + subject-jaisi
// heading, taaki har mail (trigger/fail alerts) bhi Gmail mein professional dikhe.
function autoHtmlFromText(subject: string, message: string): string {
  const esc = escHtml;
  const accent = /FAIL|⚠️/.test(subject) ? "#dc2626" : /HIT/.test(subject) ? "#16a34a" : "#334155";
  return `<!doctype html><html><body style="margin:0;padding:24px;background:#f1f5f9;font-family:Segoe UI,Roboto,Arial,sans-serif;">
  <div style="max-width:600px;margin:0 auto;background:#ffffff;border-radius:12px;overflow:hidden;border:1px solid #e2e8f0;">
    <div style="background:${accent};padding:16px 20px;">
      <span style="color:#ffffff;font-size:15px;font-weight:600;">${esc(subject)}</span>
    </div>
    <div style="padding:20px;">
      <pre style="margin:0;white-space:pre-wrap;font-family:'Consolas','Menlo',monospace;font-size:13px;line-height:1.6;color:#1e293b;">${esc(message)}</pre>
    </div>
  </div>
</body></html>`;
}

async function sendEmailAlert(
  key: string, subject: string, message: string, cooldownMs: number, critical = false,
  html?: string,
): Promise<boolean> {
  const last = alertLastSent.get(key) ?? 0;
  try {
    if (!BREVO_API_KEY || !ALERT_EMAIL_TO || !BREVO_SENDER_EMAIL) {
      console.log(`[alert] skip (${key}): BREVO_API_KEY / ALERT_EMAIL_TO / BREVO_SENDER_EMAIL set nahi`);
      return false;
    }
    const now = nowMs();
    if (cooldownMs > 0 && now - last < cooldownMs) return false;
    const day = new Date(now).toISOString().slice(0, 10);
    if (day !== alertDay) { alertDay = day; alertDayCount = 0; }
    if (!critical && alertDayCount >= ALERT_DAILY_MAX) {
      console.log(`[alert] daily cap ${ALERT_DAILY_MAX} poora — skip (${key})`);
      return false;
    }
    alertLastSent.set(key, now);   // race se bachne ke liye pehle mark; fail hua to neeche wapas
    const r = await fetch("https://api.brevo.com/v3/smtp/email", {
      method: "POST",
      headers: { "api-key": BREVO_API_KEY, "accept": "application/json", "content-type": "application/json" },
      body: JSON.stringify({
        sender: { name: "Trading Alerts", email: BREVO_SENDER_EMAIL },
        to: [{ email: ALERT_EMAIL_TO }],
        subject,
        textContent: message,
        htmlContent: html ?? autoHtmlFromText(subject, message),
      }),
      signal: AbortSignal.timeout(12000),
    });
    if (r.status === 200 || r.status === 201 || r.status === 202) {
      alertDayCount++;
      console.log(`[alert] sent (${key}) — aaj ${alertDayCount} mail`);
      return true;
    }
    alertLastSent.set(key, last);  // fail → agli baar phir try hoga
    console.log(`[alert] FAILED (${key}) HTTP ${r.status} ${(await r.text()).slice(0, 200)}`);
    return false;
  } catch (e) {
    alertLastSent.set(key, last);
    console.log(`[alert] FAILED (${key}): ${String(e).slice(0, 200)}`);
    return false;
  }
}

// ── REST health tracker (email alert jab REST calls baar-baar fail hon) ────
// Rule: kisi bhi tracked REST call mein REST_FAIL_THRESHOLD CONSECUTIVE fails → email alert.
// Cap: alert mail max 1 baar/ghanta (ALERT_FAIL_COOLDOWN_MS, jo upar sendEmailAlert
// ke liye already default 3600s hai — isi ko yahan reuse karte hain).
// Mail mein saare tracked endpoints ka combined health report jaata hai:
// FAILING wale top par (critical), OK wale niche (taaki "baaki sab theek chal
// raha hai" bhi pata chale). Jab koi failing endpoint wapas OK ho jaye, ek
// chhoti "recovered" mail alag se jaati hai (cap ke bina, kyunki good-news hai).
const REST_FAIL_THRESHOLD = envNum("REST_FAIL_THRESHOLD", 2);

interface RestHealth {
  fails: number;
  lastError: string;
  lastOkAt: number;
  alerting: boolean;   // true = abhi is endpoint ke liye "failing" state alert ja chuki hai
}
const restHealth = new Map<string, RestHealth>();

// ── Rolling request log (in-memory, fixed-size ring buffer) ────────────────
// Render ka 512MB RAM — LOG_MAX entries chhote objects hi hain (~200 bytes
// har entry), isliye chahe LOG_MAX 300 ho ya 1000, memory par asar negligible
// hai. Fixed-size rakhne se buffer kabhi grow nahi hota — process restart
// (redeploy / idle sleep) par ye log khaali ho jaata hai, persistent nahi hai.
const LOG_MAX = envNum("HEALTH_LOG_MAX", 300);
interface ReqLogEntry { ts: number; name: string; ok: boolean; detail: string }
const reqLog: ReqLogEntry[] = [];
function pushLog(name: string, ok: boolean, detail: string): void {
  reqLog.push({ ts: nowMs(), name, ok, detail: detail.slice(0, 200) });
  if (reqLog.length > LOG_MAX) reqLog.shift();
}

// ── App-session tracker ─────────────────────────────────────────────────
// "Session" = jab se HF app ne pehli baar /snapshot maanga (feeds cold se
// start hue) jab tak IDLE_STOP_SEC (default 60s) se zyada der koi demand
// nahi aayi aur feeds band ho gaye. Isi window ke andar restOk/restFail se
// aane wala HAR call yahan cumulative record hota hai (consecutive-fail
// counter jaisa reset nahi hota — poore session ka total rehta hai).
// Session khatam hone par bhi ye data yahin frozen rehta hai (agla cold
// start hi clear karega) — taaki "session abhi-abhi khatam hui, uska poora
// record dikhao" bhi possible ho.
let sessionStartedAt: number | null = null;
let sessionEndedAt: number | null = null;
interface SessionEndpointStat { sent: number; fails: number; lastAt: number; lastError: string }
const sessionStats = new Map<string, SessionEndpointStat>();

function startSession(): void {
  sessionStartedAt = nowMs();
  sessionEndedAt = null;
  sessionStats.clear();
}
function endSession(): void {
  sessionEndedAt = nowMs();
}
function sessionNote(name: string, ok: boolean, err: string): void {
  if (sessionStartedAt == null) return;   // koi session active nahi (feeds cold) — is call ka session se lena-dena nahi
  const s = sessionStats.get(name) ?? { sent: 0, fails: 0, lastAt: 0, lastError: "" };
  s.sent++;
  s.lastAt = nowMs();
  if (!ok) { s.fails++; s.lastError = err.slice(0, 200); }
  sessionStats.set(name, s);
}

function sessionReport() {
  const t = nowMs();
  const endpoints = [...sessionStats.entries()]
    .map(([name, s]) => ({
      name, sent: s.sent, fails: s.fails,
      last_ago_ms: t - s.lastAt,
      last_error: s.lastError || null,
    }))
    .sort((a, b) => b.fails - a.fails || b.sent - a.sent);
  const totalSent = endpoints.reduce((n, e) => n + e.sent, 0);
  const totalFails = endpoints.reduce((n, e) => n + e.fails, 0);
  return {
    active: sessionStartedAt != null && sessionEndedAt == null,
    started_at_ms: sessionStartedAt,
    ended_at_ms: sessionEndedAt,
    duration_ms: sessionStartedAt != null ? (sessionEndedAt ?? t) - sessionStartedAt : null,
    total_requests: totalSent,
    total_fails: totalFails,
    endpoints,
  };
}

function restOk(name: string): void {
  const h = restHealth.get(name);
  const wasAlerting = h?.alerting === true;
  restHealth.set(name, { fails: 0, lastError: "", lastOkAt: nowMs(), alerting: false });
  pushLog(name, true, "ok");
  sessionNote(name, true, "");
  if (wasAlerting) void sendRestRecoveryAlert(name);
}

function restFail(name: string, err: string): void {
  const h = restHealth.get(name) ?? { fails: 0, lastError: "", lastOkAt: 0, alerting: false };
  h.fails++;
  h.lastError = err.slice(0, 200);
  // "naya" fail-episode sirf tab, jab pehle se alerting nahi thi — isse
  // snapshot sirf har NAYE episode ki shuruaat par freeze hota hai, har
  // consecutive fail par baar-baar overwrite nahi hota.
  const enteringNewFailEpisode = h.fails >= REST_FAIL_THRESHOLD && !h.alerting;
  restHealth.set(name, h);
  pushLog(name, false, err);
  sessionNote(name, false, err);
  if (enteringNewFailEpisode) captureFailSnapshot(`${name} failing — ${err}`);
  if (h.fails >= REST_FAIL_THRESHOLD) {
    h.alerting = true;
    void sendRestFailAlert();
  }
}

function restHealthReport(): { failing: string[]; ok: string[] } {
  const t = nowMs();
  const ago = (ts: number) => ts ? `${Math.round((t - ts) / 1000)}s pehle` : "abhi tak nahi mila";
  const failing: string[] = [];
  const ok: string[] = [];
  for (const [name, h] of restHealth) {
    if (h.fails >= REST_FAIL_THRESHOLD) {
      failing.push(`❌ ${name} — ${h.fails} consecutive fails | last success: ${ago(h.lastOkAt)}\n   Error: ${h.lastError}`);
    } else {
      ok.push(`✅ ${name} — last success: ${ago(h.lastOkAt)}`);
    }
  }
  return { failing, ok };
}

// Same data as restHealthReport() par table-row (HTML <td>) form mein — email ke
// "FAILING endpoints" aur "OK endpoints" table ke liye.
function restHealthReportHtmlRows(): { failingRows: string[][]; okRows: string[][] } {
  const t = nowMs();
  const ago = (ts: number) => (ts ? `${Math.round((t - ts) / 1000)}s pehle` : "abhi tak nahi mila");
  const failingRows: string[][] = [];
  const okRows: string[][] = [];
  for (const [name, h] of restHealth) {
    if (h.fails >= REST_FAIL_THRESHOLD) {
      failingRows.push([escHtml(name), String(h.fails), ago(h.lastOkAt), escHtml(h.lastError)]);
    } else {
      okRows.push([escHtml(name), ago(h.lastOkAt)]);
    }
  }
  return { failingRows, okRows };
}

// Alert ke waqt "kyun fail hua" diagnose karne ke liye ek chhota snapshot
// bnRateStats() se + reqLog se recent entries — taaki mail dekhte hi pata
// chale us waqt total kitni requests aa rahi thin, kaunse endpoint sabse
// zyada poll ho rahe the (poll-storm), aur exact us waqt kya OK/FAIL ho raha tha.
function alertDiagnosticSnapshot(): string {
  const stats = bnRateStats();
  const epLines = stats.endpoints.length
    ? stats.endpoints
        .slice(0, 10)
        .map((e) => `   ${e.endpoint} — seen:${e.seen} sent:${e.sent} cacheHits:${e.cacheHits} weight:${e.weight}`)
        .join("\n")
    : "   (koi request nahi last 60s mein)";

  const t = nowMs();
  const recentLines = reqLog
    .slice(-15)
    .reverse()
    .map((e) => `   [${Math.round((t - e.ts) / 1000)}s pehle] ${e.ok ? "OK " : "FAIL"} ${e.name}${e.ok ? "" : ` — ${e.detail}`}`)
    .join("\n");

  const sess = sessionReport();
  const sessLines = sess.endpoints.length
    ? sess.endpoints.map((e) => `   ${e.name} — sent:${e.sent} fails:${e.fails}${e.last_error ? ` | last error: ${e.last_error}` : ""}`).join("\n")
    : "   (session mein abhi koi request nahi)";
  const sessDurationStr = sess.duration_ms != null ? `${Math.round(sess.duration_ms / 1000)}s` : "n/a";

  return (
    `\n🗂️ App session (jab se HF app ne demand shuru ki):\n` +
    `   status: ${sess.active ? "ACTIVE" : "ended"} | duration: ${sessDurationStr}\n` +
    `   total requests: ${sess.total_requests} | total fails: ${sess.total_fails}\n` +
    `   Per-endpoint (poore session ka total):\n${sessLines}\n\n` +
    `📊 Us waqt ka snapshot (last 60s):\n` +
    `   requests seen (proxy ko mili): ${stats.requests_seen_last_60s}\n` +
    `   requests sent (Binance tak gayi): ${stats.requests_last_60s}\n` +
    `   cache hits: ${stats.cache_hits_last_60s}\n` +
    `   burst last 10s / 5s: ${stats.burst_last_10s} / ${stats.burst_last_5s}\n` +
    `   estimated weight (60s): ${stats.estimated_weight_last_60s} | binance used_weight_1m: ${stats.binance_used_weight_1m ?? "n/a"}\n\n` +
    `   Per-endpoint (last 60s):\n${epLines}\n\n` +
    `📜 Recent request-log (last 15):\n${recentLines || "   (khaali)"}\n`
  );
}

// alertDiagnosticSnapshot() jaisa hi data, par HTML mein 4 separate tables:
// (1) session summary, (2) session per-endpoint, (3) live 60s per-endpoint,
// (4) recent request-log.
function alertDiagnosticSnapshotHtml(): string {
  const stats = bnRateStats();
  const t = nowMs();
  const sess = sessionReport();
  const sessDurationStr = sess.duration_ms != null ? `${Math.round(sess.duration_ms / 1000)}s` : "n/a";

  let html = `<h3 style="margin:18px 0 4px;font-size:14px;color:#334155;">🗂️ App session</h3>`;
  html += tableHtml(
    ["Status", "Duration", "Total requests", "Total fails"],
    [[sess.active ? "ACTIVE" : "ended", sessDurationStr, String(sess.total_requests), String(sess.total_fails)]],
    "#334155",
  );
  const sessRows = sess.endpoints.map((e) => [
    escHtml(e.name), String(e.sent), String(e.fails), e.last_error ? escHtml(e.last_error) : "—",
  ]);
  html += tableHtml(["Endpoint", "Sent", "Fails", "Last error"], sessRows, "#64748b");

  html += `<h3 style="margin:18px 0 4px;font-size:14px;color:#334155;">📊 Live snapshot (last 60s)</h3>`;
  html += tableHtml(
    ["Seen", "Sent", "CacheHits", "Burst 10s/5s", "Est. Weight", "BN used_weight_1m"],
    [[
      String(stats.requests_seen_last_60s), String(stats.requests_last_60s), String(stats.cache_hits_last_60s),
      `${stats.burst_last_10s} / ${stats.burst_last_5s}`, String(stats.estimated_weight_last_60s),
      String(stats.binance_used_weight_1m ?? "n/a"),
    ]],
    "#334155",
  );
  const liveEpRows = stats.endpoints.slice(0, 10).map((e) => [
    escHtml(e.endpoint), String(e.seen), String(e.sent), String(e.cacheHits), String(e.weight),
  ]);
  html += tableHtml(["Endpoint", "Seen", "Sent", "CacheHits", "Weight"], liveEpRows, "#64748b");

  html += `<h3 style="margin:18px 0 4px;font-size:14px;color:#334155;">📜 Recent request-log (last 15)</h3>`;
  const logRows = reqLog.slice(-15).reverse().map((e) => [
    `${Math.round((t - e.ts) / 1000)}s pehle`,
    e.ok ? `<span style="color:#16a34a;font-weight:600;">OK</span>` : `<span style="color:#dc2626;font-weight:600;">FAIL</span>`,
    escHtml(e.name),
    e.ok ? "—" : escHtml(e.detail),
  ]);
  html += tableHtml(["Age", "Result", "Name", "Detail"], logRows, "#64748b");

  return html;
}

async function sendRestFailAlert(): Promise<void> {
  const { failing, ok } = restHealthReport();
  if (!failing.length) return;
  const subject = "[Alert] REST call(s) failing — Binance proxy";

  // Plain-text version — textContent fallback ke liye (kuch mail clients HTML
  // nahi dikhate), format bilkul pehle jaisa hi rehta hai.
  const body =
    `🚨 REST calls fail ho rahi hain (${REST_FAIL_THRESHOLD}+ consecutive fails)\n\n` +
    `❌ FAILING:\n${failing.join("\n\n")}\n\n` +
    `✅ OK:\n${ok.length ? ok.join("\n") : "  (koi doosra tracked endpoint nahi)"}\n\n` +
    `🔒 Fail/ban shuru hone ke waqt ka FROZEN 60s snapshot (isi episode ka, live nahi):\n` +
    fmtFailSnapshotText(lastFailSnapshotReport()) + `\n` +
    alertDiagnosticSnapshot();

  // HTML version — sab kuch column-wise tables mein (Gmail/Outlook mein khulega).
  const { failingRows, okRows } = restHealthReportHtmlRows();
  let html = `<!doctype html><html><body style="margin:0;padding:24px;background:#f1f5f9;font-family:Segoe UI,Roboto,Arial,sans-serif;">
  <div style="max-width:680px;margin:0 auto;background:#ffffff;border-radius:12px;overflow:hidden;border:1px solid #e2e8f0;">
    <div style="background:#dc2626;padding:16px 20px;">
      <span style="color:#ffffff;font-size:15px;font-weight:600;">🚨 REST calls fail ho rahi hain (${REST_FAIL_THRESHOLD}+ consecutive fails)</span>
    </div>
    <div style="padding:20px;">`;

  html += `<h3 style="margin:0 0 4px;font-size:14px;color:#dc2626;">❌ FAILING</h3>`;
  html += tableHtml(["Endpoint", "Consecutive fails", "Last success", "Last error"], failingRows, "#dc2626");

  html += `<h3 style="margin:0 0 4px;font-size:14px;color:#16a34a;">✅ OK</h3>`;
  html += tableHtml(["Endpoint", "Last success"], okRows, "#16a34a");

  html += `<h3 style="margin:0 0 4px;font-size:14px;color:#334155;">🔒 Frozen 60s snapshot (episode ki shuruaat, live nahi)</h3>`;
  html += fmtFailSnapshotHtml(lastFailSnapshotReport());

  html += alertDiagnosticSnapshotHtml();

  html += `</div></div></body></html>`;

  await sendEmailAlert("rest-health-fail", subject, body, ALERT_FAIL_COOLDOWN_MS, false, html);
}

async function sendRestRecoveryAlert(name: string): Promise<void> {
  const body = `✅ ${name} wapas normal chal raha hai (fails ruk gaye).`;
  await sendEmailAlert(`rest-health-recover:${name}`, `[Recovered] ${name} theek ho gaya`, body, 0, false);
}

// ── Rules engine (SL / Target / Trailing) — Supabase-backed ────────────────
// Secrets naam jaanbujh kar app.py ke REPLAY/TEST Supabase project jaise hi
// rakhe hain (TEST_SUPABASE_URL / TEST_SUPABASE_ANON_KEY) — user ke ask par.
// Ye anon-key hai isliye Supabase mein "trade_rules" table par RLS OFF honi
// chahiye (ya anon ko explicit grants), warna reads/writes 401/403 denge.
const SUPABASE_URL = (Deno.env.get("TEST_SUPABASE_URL") ?? "").trim().replace(/\/+$/, "");
const SUPABASE_ANON_KEY = (Deno.env.get("TEST_SUPABASE_ANON_KEY") ?? "").trim();

const OPTION_SYMBOL_RE = /^BTC-\d{6}-\d{3,7}-[CP]$/;
const CLIENT_ID_RE = /^[A-Za-z0-9_-]{1,36}$/;
const textEnc = new TextEncoder();

function tradeJson(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", "Cache-Control": "no-store" },
  });
}

function tradeRaw(status: number, bodyText: string): Response {
  return new Response(bodyText, {
    status,
    headers: { "Content-Type": "application/json", "Cache-Control": "no-store" },
  });
}

async function sha256Bytes(s: string): Promise<Uint8Array> {
  return new Uint8Array(await crypto.subtle.digest("SHA-256", textEnc.encode(s)));
}

async function hmacHex(secret: string, msg: string): Promise<string> {
  const key = await crypto.subtle.importKey(
    "raw", textEnc.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"],
  );
  const sig = new Uint8Array(await crypto.subtle.sign("HMAC", key, textEnc.encode(msg)));
  return [...sig].map((b) => b.toString(16).padStart(2, "0")).join("");
}

// Token compare (constant-time, hash ke through)
async function tokenOk(provided: string): Promise<boolean> {
  if (!TRADE_TOKEN || !provided) return false;
  const [a, b] = await Promise.all([sha256Bytes(provided), sha256Bytes(TRADE_TOKEN)]);
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a[i] ^ b[i];
  return diff === 0;
}

// Galat token bar-bar aaye to us IP ko thodi der ke liye rok do
const authFails = new Map<string, { n: number; first: number; lockUntil: number }>();
function clientIp(req: Request): string {
  return (req.headers.get("x-forwarded-for") ?? "unknown").split(",")[0].trim();
}
function isLocked(ip: string): boolean {
  const f = authFails.get(ip);
  return !!f && f.lockUntil > nowMs();
}
function noteAuthFail(ip: string) {
  const t = nowMs();
  let f = authFails.get(ip);
  if (!f || t - f.first > 10 * 60_000) f = { n: 0, first: t, lockUntil: 0 };
  f.n++;
  if (f.n >= 10) f.lockUntil = t + 10 * 60_000;
  authFails.set(ip, f);
}

// Binance server time offset (timestamp galat na jaye)
let timeOffsetMs = 0;
let timeSyncedAt = 0;
async function syncTime() {
  if (nowMs() - timeSyncedAt < 10 * 60_000) return;
  try {
    const r = await fetch(`${EAPI}/eapi/v1/time`, { signal: AbortSignal.timeout(5000) });
    trackBn("/eapi/v1/time", { resp: r });
    if (r.ok) {
      const j = await r.json();
      if (Number.isFinite(Number(j?.serverTime))) {
        timeOffsetMs = Number(j.serverTime) - nowMs();
        timeSyncedAt = nowMs();
        restOk("time-sync (syncTime)");
      } else {
        restFail("time-sync (syncTime)", "invalid serverTime in response");
      }
    } else {
      restFail("time-sync (syncTime)", `HTTP ${r.status}`);
    }
  } catch (e) {
    restFail("time-sync (syncTime)", String(e));   // purana offset chalega
  }
}

type Params = Record<string, string | number | boolean | undefined>;

function buildQuery(p: Params): string {
  return Object.entries(p)
    .filter(([, v]) => v !== undefined && v !== "")
    .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`)
    .join("&");
}

// Binance rate-limit (429) / IP-ban (418) ke baad hum khud bhi kuch der ruk jaate hain
let bnBackoffUntil = 0;

interface CallOpts { bypassBackoff?: boolean }

async function signedCall(
  method: "GET" | "POST" | "DELETE", path: string, params: Params, opts: CallOpts = {},
): Promise<{ status: number; body: string }> {
  if (!opts.bypassBackoff && nowMs() < bnBackoffUntil) {
    const s = Math.ceil((bnBackoffUntil - nowMs()) / 1000);
    return { status: 429, body: JSON.stringify({ error: `Binance rate-limit backoff chal raha hai — ${s}s baad try karo` }) };
  }
  const maxAttempts = method === "GET" ? 3 : 1;   // sirf GET (idempotent) network error par retry hota hai
  let resynced = false;
  for (let attempt = 1; ; attempt++) {
    await syncTime();
    const qs = buildQuery({ ...params, recvWindow: 5000, timestamp: nowMs() + timeOffsetMs });
    const sig = await hmacHex(OPT_SECRET_KEY, qs);
    let r: Response;
    try {
      r = await fetch(`${EAPI}${path}?${qs}&signature=${sig}`, {
        method,
        headers: { "X-MBX-APIKEY": OPT_API_KEY },
        signal: AbortSignal.timeout(15000),
      });
    } catch (e) {
      if (attempt < maxAttempts) { await sleep(400 * attempt); continue; }
      throw e;   // POST/DELETE: outcome unknown — caller ko batana hai, blind retry nahi
    }
    trackBn(path, { resp: r, symbol: params.symbol != null ? String(params.symbol) : null });
    const text = await r.text();
    // Binance ka jawab bina parse/stringify kiye seedha aage jaata hai — 19-digit
    // orderId jaise bade numbers JS mein precision kho dete hain.
    let body: string;
    let code: number | null = null;
    try {
      const j = JSON.parse(text);
      body = text;
      if (j && typeof j === "object" && !Array.isArray(j) && Number.isFinite(Number(j.code))) code = Number(j.code);
    } catch { body = JSON.stringify({ raw: text.slice(0, 500) }); }

    if (r.status === 429 || r.status === 418) {
      const ra = parseInt(r.headers.get("retry-after") ?? "", 10);
      const wait = Number.isFinite(ra) && ra > 0 ? Math.min(ra, 1800) : (r.status === 418 ? 120 : 10);
      bnBackoffUntil = Math.max(bnBackoffUntil, nowMs() + wait * 1000);
      console.log(`[trade] Binance HTTP ${r.status} — backoff ${wait}s`);
      return { status: r.status, body };
    }
    // -1021: timestamp recvWindow ke bahar — request process hi nahi hui, isliye retry safe hai (sab methods)
    if (code === -1021 && !resynced) {
      resynced = true;
      timeSyncedAt = 0;
      await syncTime();
      continue;
    }
    return { status: r.status, body };
  }
}

// URL se sirf allowed query params uthao
function pick(url: URL, names: string[]): Params {
  const out: Params = {};
  for (const n of names) {
    const v = url.searchParams.get(n);
    if (v !== null && v !== "") out[n] = v;
  }
  return out;
}

async function readBody(req: Request): Promise<Record<string, unknown> | null> {
  const text = await req.text();
  if (text.length > 2000) return null;
  try {
    const j = JSON.parse(text);
    return j && typeof j === "object" && !Array.isArray(j) ? j : null;
  } catch {
    return null;
  }
}

// ── Tick size / qty step (exchangeInfo filters) ────────────────────────────
// Public REST forward exchangeInfo se filters hata deta hai, isliye yahan Render seedha
// Binance se leta hai (10 min cache). Agar filters na milein to check skip hota hai
// (source: "unknown") — order tab bhi Binance khud reject/accept karega.
interface SymRules { tick: number | null; step: number | null; minQty: number | null; source: "exchangeInfo" | "unknown" }
let symRulesCache: { ts: number; map: Map<string, SymRules> } = { ts: 0, map: new Map() };

function posNum(x: unknown): number | null {
  const n = parseFloat(String(x ?? ""));
  return Number.isFinite(n) && n > 0 ? n : null;
}

async function loadSymRules(): Promise<void> {
  if (symRulesCache.map.size && nowMs() - symRulesCache.ts < 10 * 60_000) return;
  try {
    const r = await fetch(`${EAPI}/eapi/v1/exchangeInfo`, { signal: AbortSignal.timeout(12000) });
    trackBn("/eapi/v1/exchangeInfo", { resp: r });
    if (!r.ok) return;
    const j = await r.json();
    const m = new Map<string, SymRules>();
    // deno-lint-ignore no-explicit-any
    for (const s of ((j?.optionSymbols ?? []) as any[])) {
      const sym = String(s?.symbol ?? "");
      if (!sym.startsWith(SYMBOL_PREFIX)) continue;
      let tick: number | null = null;
      let step: number | null = null;
      let minQty: number | null = posNum(s?.minQty);
      // deno-lint-ignore no-explicit-any
      for (const f of ((s?.filters ?? []) as any[])) {
        if (f?.filterType === "PRICE_FILTER") tick = posNum(f.tickSize);
        if (f?.filterType === "LOT_SIZE") { step = posNum(f.stepSize); minQty = posNum(f.minQty) ?? minQty; }
      }
      m.set(sym, { tick, step, minQty, source: "exchangeInfo" });
    }
    if (m.size) symRulesCache = { ts: nowMs(), map: m };
  } catch { /* purana cache chalega */ }
}

async function getSymRules(symbol: string): Promise<SymRules> {
  await loadSymRules();
  return symRulesCache.map.get(symbol) ?? { tick: null, step: null, minQty: null, source: "unknown" };
}

function isMultipleOf(x: number, unit: number): boolean {
  const q = x / unit;
  return Math.abs(q - Math.round(q)) < 1e-6;
}

function roundToTick(x: number, tick: number, mode: "down" | "up"): number {
  const q = x / tick;
  const r = mode === "down" ? Math.floor(q + 1e-9) : Math.ceil(q - 1e-9);
  return Number((r * tick).toFixed(8));
}

// Order safety state
const recentClientIds = new Map<string, number>();
const orderTimes: number[] = [];

function newClientId(): string {
  return `rn${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`;
}

async function getMarkPrice(symbol: string): Promise<number | null> {
  try {
    const r = await fetch(`${EAPI}/eapi/v1/mark?symbol=${encodeURIComponent(symbol)}`, {
      signal: AbortSignal.timeout(8000),
    });
    trackBn("/eapi/v1/mark", { resp: r, symbol });
    if (!r.ok) return null;
    const j = await r.json();
    const row = Array.isArray(j) ? j[0] : j;
    const p = parseFloat(row?.markPrice);
    return Number.isFinite(p) && p > 0 ? p : null;
  } catch {
    return null;
  }
}

async function handlePlaceOrder(req: Request): Promise<Response> {
  if (!TRADING_ENABLED) return tradeJson(403, { error: "Trading disabled (TRADING_ENABLED != true)" });

  const b = await readBody(req);
  if (!b) return tradeJson(400, { error: "Bad JSON body" });

  const symbol = String(b.symbol ?? "");
  const side = String(b.side ?? "").toUpperCase();
  const qty = Number(b.quantity);
  const price = Number(b.price);
  const tif = String(b.timeInForce ?? "GTC").toUpperCase();
  const reduceOnly = b.reduceOnly === true;
  const postOnly = b.postOnly === true;
  const clientOrderId = b.clientOrderId ? String(b.clientOrderId) : newClientId();

  if (!OPTION_SYMBOL_RE.test(symbol)) return tradeJson(400, { error: "Symbol invalid (sirf BTC-YYMMDD-STRIKE-C|P)" });
  if (side !== "BUY" && side !== "SELL") return tradeJson(400, { error: "side BUY ya SELL" });
  if (side === "SELL" && !reduceOnly) return tradeJson(403, { error: "SELL sirf reduceOnly=true ke saath allowed hai" });
  if (!Number.isFinite(qty) || qty <= 0) return tradeJson(400, { error: "quantity invalid" });
  if (qty > MAX_ORDER_QTY) return tradeJson(403, { error: `quantity limit se zyada (max ${MAX_ORDER_QTY})` });
  if (!Number.isFinite(price) || price <= 0) return tradeJson(400, { error: "price invalid (LIMIT order ke liye zaroori)" });
  if (!["GTC", "IOC", "FOK"].includes(tif)) return tradeJson(400, { error: "timeInForce GTC/IOC/FOK" });
  if (!CLIENT_ID_RE.test(clientOrderId)) return tradeJson(400, { error: "clientOrderId invalid" });
  if (side === "BUY" && price * qty > MAX_ORDER_USDT) {
    return tradeJson(403, { error: `premium limit se zyada (price*qty > ${MAX_ORDER_USDT} USDT)` });
  }

  // Duplicate + rate limit (sab await se pehle, taaki race na ho)
  const t = nowMs();
  for (const [id, ts] of recentClientIds) if (t - ts > 10 * 60_000) recentClientIds.delete(id);
  if (recentClientIds.has(clientOrderId)) return tradeJson(409, { error: "Duplicate clientOrderId" });
  while (orderTimes.length && t - orderTimes[0] > 60_000) orderTimes.shift();
  if (orderTimes.length >= MAX_ORDERS_PER_MIN) return tradeJson(429, { error: "Order rate limit (per minute) cross" });
  recentClientIds.set(clientOrderId, t);
  orderTimes.push(t);

  // Fat-finger check: price mark se bahut door na ho
  const mark = await getMarkPrice(symbol);
  if (mark === null) {
    recentClientIds.delete(clientOrderId);
    return tradeJson(502, { error: "Mark price nahi mila — order roka gaya" });
  }
  const dev = MAX_PRICE_DEV_PCT / 100;
  if ((side === "BUY" && price > mark * (1 + dev)) || (side === "SELL" && price < mark * (1 - dev))) {
    recentClientIds.delete(clientOrderId);
    return tradeJson(403, { error: `price mark (${mark}) se ${MAX_PRICE_DEV_PCT}% se zyada door hai` });
  }

  // Tick size / qty step check (Binance ke asli filters se) — reject hone se pehle saaf error
  const rules = await getSymRules(symbol);
  if (rules.tick !== null && !isMultipleOf(price, rules.tick)) {
    recentClientIds.delete(clientOrderId);
    return tradeJson(400, { error: `price tick size (${rules.tick}) ke multiple mein hona chahiye`, tick: rules.tick });
  }
  if (rules.step !== null && !isMultipleOf(qty, rules.step)) {
    recentClientIds.delete(clientOrderId);
    return tradeJson(400, { error: `quantity step (${rules.step}) ke multiple mein hona chahiye`, step: rules.step });
  }
  if (rules.minQty !== null && qty < rules.minQty - 1e-12) {
    recentClientIds.delete(clientOrderId);
    return tradeJson(400, { error: `quantity min (${rules.minQty}) se kam hai`, minQty: rules.minQty });
  }

  const res = await signedCall("POST", "/eapi/v1/order", {
    symbol, side, type: "LIMIT", quantity: qty, price, timeInForce: tif,
    reduceOnly: reduceOnly ? "true" : undefined,
    postOnly: postOnly ? "true" : undefined,
    clientOrderId, newOrderRespType: "RESULT",
  });
  console.log(`[trade] order ${side} ${symbol} q=${qty} p=${price} cid=${clientOrderId} -> HTTP ${res.status}`);
  return tradeRaw(res.status, res.body);
}

async function handleCancel(req: Request): Promise<Response> {
  const b = await readBody(req);
  if (!b) return tradeJson(400, { error: "Bad JSON body" });
  const symbol = String(b.symbol ?? "");
  if (!OPTION_SYMBOL_RE.test(symbol)) return tradeJson(400, { error: "Symbol invalid" });
  if (!b.orderId && !b.clientOrderId) return tradeJson(400, { error: "orderId ya clientOrderId do" });
  const res = await signedCall("DELETE", "/eapi/v1/order", {
    symbol,
    orderId: b.orderId ? String(b.orderId) : undefined,
    clientOrderId: b.clientOrderId ? String(b.clientOrderId) : undefined,
  }, { bypassBackoff: true });
  console.log(`[trade] cancel ${symbol} -> HTTP ${res.status}`);
  return tradeRaw(res.status, res.body);
}

// ── PANIC: saare open orders cancel + saari open positions reduceOnly close ──
// Ye risk GHATAATA hai (naya risk nahi banata), isliye TRADING_ENABLED=false hone par bhi chalta hai
// aur MAX_ORDER_* limits / rate-limit / backoff bypass karta hai. Ek waqt mein ek hi panic chalta hai.
let panicRunning = false;

function pickOrderId(body: string): string | null {
  const m = body.match(/"orderId"\s*:\s*"?(\d+)"?/);   // regex — 19-digit id string mein
  return m ? m[1] : null;
}

async function publicRow(path: string, symbol: string): Promise<Record<string, unknown> | null> {
  try {
    const r = await fetch(`${EAPI}${path}?symbol=${encodeURIComponent(symbol)}`, { signal: AbortSignal.timeout(8000) });
    trackBn(path, { resp: r, symbol });
    if (!r.ok) return null;
    const j = await r.json();
    const row = Array.isArray(j) ? j[0] : j;
    return row && typeof row === "object" ? row as Record<string, unknown> : null;
  } catch { return null; }
}

async function handlePanic(): Promise<Response> {
  if (panicRunning) return tradeJson(429, { error: "Panic pehle se chal raha hai — result ka wait karo" });
  panicRunning = true;
  try {
    const notes: string[] = [];
    // 1) Sab open orders cancel (pehle — taaki reduceOnly close orders ke saath conflict na ho)
    const c = await signedCall("DELETE", "/eapi/v1/allOpenOrdersByUnderlying", { underlying: "BTCUSDT" }, { bypassBackoff: true });
    console.log(`[trade] PANIC cancel-all -> HTTP ${c.status}`);
    const cancelOk = c.status >= 200 && c.status < 300;
    if (!cancelOk) notes.push(`cancel-all fail (HTTP ${c.status}): ${c.body.slice(0, 200)}`);

    // 2) Positions
    const p = await signedCall("GET", "/eapi/v1/position", {}, { bypassBackoff: true });
    let positions: unknown = null;
    try { positions = JSON.parse(p.body); } catch { /* neeche handle */ }
    if (p.status < 200 || p.status >= 300 || !Array.isArray(positions)) {
      notes.push(`positions nahi mile (HTTP ${p.status}) — positions MANUALLY check/close karo`);
      return tradeJson(200, { ok: false, cancel_ok: cancelOk, closes: [], notes });
    }

    // 3) Har open position ke liye reduceOnly aggressive LIMIT close
    const closes: Array<Record<string, unknown>> = [];
    let i = 0;
    // deno-lint-ignore no-explicit-any
    for (const pos of (positions as any[])) {
      const symbol = String(pos?.symbol ?? "");
      const q = parseFloat(String(pos?.quantity ?? pos?.qty ?? "0"));
      if (!OPTION_SYMBOL_RE.test(symbol) || !Number.isFinite(q) || q === 0) continue;
      const isShort = String(pos?.side ?? "").toUpperCase() === "SHORT" || q < 0;
      const qty = Math.abs(q);
      const entry: Record<string, unknown> = { symbol, side: isShort ? "BUY" : "SELL", quantity: qty };
      try {
        const [tk, mk, rules] = await Promise.all([
          publicRow("/eapi/v1/ticker", symbol), publicRow("/eapi/v1/mark", symbol), getSymRules(symbol),
        ]);
        const bid = parseFloat(String(tk?.bidPrice ?? "0"));
        const ask = parseFloat(String(tk?.askPrice ?? "0"));
        const mark = parseFloat(String(mk?.markPrice ?? "0"));
        const low = parseFloat(String(mk?.lowPriceLimit ?? "0"));
        const high = parseFloat(String(mk?.highPriceLimit ?? "0"));
        const tick = rules.tick ?? 5;   // filters na mile to purana 5 USDT andaza
        // SELL (long close): price bid se NEECHE (neeche round) — taaki bids se turant match ho.
        // BUY (short close): price ask se UPAR (upar round). Binance price-limit band ke andar clamp.
        let price: number;
        if (!isShort) {
          const base = bid > 0 ? bid * 0.97 : (mark > 0 ? mark * 0.7 : 0);
          if (base <= 0) throw new Error("bid/mark price nahi mila");
          price = roundToTick(base, tick, "down");
          if (low > 0 && price < low) price = roundToTick(low, tick, "up");
          if (price < tick) price = tick;
        } else {
          const base = ask > 0 ? ask * 1.03 : (mark > 0 ? mark * 1.3 : 0);
          if (base <= 0) throw new Error("ask/mark price nahi mila");
          price = roundToTick(base, tick, "up");
          if (high > 0 && price > high) price = roundToTick(high, tick, "down");
          if (price < tick) price = tick;
        }
        entry.price = price;
        const cid = `pn${Date.now().toString(36)}${i++}${Math.random().toString(36).slice(2, 5)}`;
        const res = await signedCall("POST", "/eapi/v1/order", {
          symbol, side: isShort ? "BUY" : "SELL", type: "LIMIT", quantity: qty, price,
          timeInForce: "GTC", reduceOnly: "true", clientOrderId: cid, newOrderRespType: "RESULT",
        }, { bypassBackoff: true });
        entry.http = res.status;
        entry.ok = res.status >= 200 && res.status < 300;
        entry.orderId = pickOrderId(res.body);
        if (!entry.ok) entry.error = res.body.slice(0, 200);
        console.log(`[trade] PANIC close ${symbol} ${entry.side} q=${qty} p=${price} -> HTTP ${res.status}`);
      } catch (e) {
        entry.ok = false;
        entry.error = String(e).slice(0, 200);
      }
      closes.push(entry);
    }
    if (!closes.length) notes.push("Koi open position nahi mili");
    const allOk = cancelOk && closes.every((x) => x.ok === true);
    notes.push("Close orders GTC limit hain — fill hue ya nahi, Positions/Orders tab mein dekho");
    return tradeJson(200, { ok: allOk, cancel_ok: cancelOk, closes, notes });
  } finally {
    panicRunning = false;
  }
}

// ── RULES ENGINE (SL / Target / Trailing) ───────────────────────────────────
// Rules Supabase table "trade_rules" mein store hote hain (HF POST /rules/create
// karta hai jab entry fill ho). Render yahan background loop mein khud har
// active rule ko monitor karta hai — HF session so jaaye/crash ho to bhi ye
// chalta rehta hai (jab tak Render instance khud zinda hai — dekho neeche
// "keep-alive" note).
//
// Rules: LONG (BUY se entry) → exit hamesha reduceOnly SELL, trigger BID par.
//        SHORT (SELL se entry) → exit hamesha reduceOnly BUY, trigger ASK par.
// sl_price / target_price / trailing_pct teeno optional, kam se kam ek zaroori.

interface TradeRule {
  id: number;
  symbol: string;
  side: "LONG" | "SHORT";
  entry_qty: number;
  entry_price: number;
  sl_price: number | null;
  target_price: number | null;
  trailing_points: number | null;   // absolute USDT offset (chart.html "trail" field), % nahi
  trail_high_water: number | null;
  status: "active" | "triggered" | "closed" | "cancelled";
  close_order_id: string | null;
}

function supaHeaders(extra: Record<string, string> = {}): Record<string, string> {
  return {
    apikey: SUPABASE_ANON_KEY,
    Authorization: `Bearer ${SUPABASE_ANON_KEY}`,
    "Content-Type": "application/json",
    ...extra,
  };
}

function supaReady(): boolean {
  return !!(SUPABASE_URL && SUPABASE_ANON_KEY);
}

async function supaSelectActiveRules(): Promise<TradeRule[]> {
  if (!supaReady()) return [];
  try {
    const r = await fetch(`${SUPABASE_URL}/rest/v1/trade_rules?status=eq.active&select=*`, {
      headers: supaHeaders(), signal: AbortSignal.timeout(10000),
    });
    if (!r.ok) return [];
    const j = await r.json();
    return Array.isArray(j) ? (j as TradeRule[]) : [];
  } catch { return []; }
}

async function supaInsertRule(row: Record<string, unknown>): Promise<TradeRule | null> {
  if (!supaReady()) return null;
  try {
    const r = await fetch(`${SUPABASE_URL}/rest/v1/trade_rules`, {
      method: "POST",
      headers: supaHeaders({ Prefer: "return=representation" }),
      body: JSON.stringify(row),
      signal: AbortSignal.timeout(10000),
    });
    if (!r.ok) return null;
    const j = await r.json();
    return Array.isArray(j) && j[0] ? (j[0] as TradeRule) : null;
  } catch { return null; }
}

async function supaUpdateRule(id: number, patch: Record<string, unknown>): Promise<boolean> {
  if (!supaReady()) return false;
  try {
    const r = await fetch(`${SUPABASE_URL}/rest/v1/trade_rules?id=eq.${id}`, {
      method: "PATCH",
      headers: supaHeaders({ Prefer: "return=minimal" }),
      body: JSON.stringify(patch),
      signal: AbortSignal.timeout(10000),
    });
    return r.ok;
  } catch { return false; }
}

const rulesCache = new Map<number, TradeRule>();
let rulesLoadedAt = 0;
const RULES_REFRESH_MS = 4000;

async function refreshRulesCache(): Promise<void> {
  const rows = await supaSelectActiveRules();
  const ids = new Set(rows.map((r) => r.id));
  for (const id of rulesCache.keys()) if (!ids.has(id)) rulesCache.delete(id);
  for (const row of rows) rulesCache.set(row.id, row);
  rulesLoadedAt = nowMs();
}

// Panic-close jaisa hi per-symbol lock — taaki SL/target/trailing aur manual
// panic ek hi symbol par ek saath do reduceOnly close orders na bhej dein.
const closingSymbols = new Set<string>();

async function closeRuleReduceOnly(rule: TradeRule, reason: string): Promise<void> {
  const symbol = rule.symbol;
  if (closingSymbols.has(symbol) || panicRunning) return;
  closingSymbols.add(symbol);
  try {
    const [tk, mk, rules] = await Promise.all([
      publicRow("/eapi/v1/ticker", symbol), publicRow("/eapi/v1/mark", symbol), getSymRules(symbol),
    ]);
    const bid = parseFloat(String(tk?.bidPrice ?? "0"));
    const ask = parseFloat(String(tk?.askPrice ?? "0"));
    const mark = parseFloat(String(mk?.markPrice ?? "0"));
    const low = parseFloat(String(mk?.lowPriceLimit ?? "0"));
    const high = parseFloat(String(mk?.highPriceLimit ?? "0"));
    const tick = rules.tick ?? 5;
    const isShort = rule.side === "SHORT";
    let price: number;
    if (!isShort) {
      // LONG close = SELL, bid se neeche (turant match ho)
      const base = bid > 0 ? bid * 0.97 : (mark > 0 ? mark * 0.7 : 0);
      if (base <= 0) throw new Error("bid/mark price nahi mila");
      price = roundToTick(base, tick, "down");
      if (low > 0 && price < low) price = roundToTick(low, tick, "up");
      if (price < tick) price = tick;
    } else {
      // SHORT close = BUY, ask se upar
      const base = ask > 0 ? ask * 1.03 : (mark > 0 ? mark * 1.3 : 0);
      if (base <= 0) throw new Error("ask/mark price nahi mila");
      price = roundToTick(base, tick, "up");
      if (high > 0 && price > high) price = roundToTick(high, tick, "down");
      if (price < tick) price = tick;
    }
    const cid = `rl${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`;
    const res = await signedCall("POST", "/eapi/v1/order", {
      symbol, side: isShort ? "BUY" : "SELL", type: "LIMIT", quantity: rule.entry_qty, price,
      timeInForce: "GTC", reduceOnly: "true", clientOrderId: cid, newOrderRespType: "RESULT",
    }, { bypassBackoff: true });
    const ok = res.status >= 200 && res.status < 300;
    console.log(`[rules] ${reason} close ${symbol} ${isShort ? "BUY" : "SELL"} q=${rule.entry_qty} p=${price} -> HTTP ${res.status}`);
    const ruleInfo =
      `Symbol: ${symbol}\nSide: ${rule.side}\nQty: ${rule.entry_qty}\nEntry: ${rule.entry_price}\n` +
      `SL: ${rule.sl_price ?? "-"} | Target: ${rule.target_price ?? "-"} | Trail pts: ${rule.trailing_points ?? "-"}\n` +
      `Close order: ${isShort ? "BUY" : "SELL"} LIMIT reduceOnly @ ${price}`;
    if (ok) {
      const orderId = pickOrderId(res.body);
      const dbOk = await supaUpdateRule(rule.id, { status: "triggered", close_order_id: orderId });
      rulesCache.delete(rule.id);
      // Trigger alert (background — exit flow ko block nahi karta). Per-rule key + 10 min
      // cooldown: agar DB update fail ho gaya aur rule dobara trigger ho, to duplicate mail na aaye.
      void sendEmailAlert(
        `trigger:${rule.id}`,
        `[Trade] ${reason} HIT — ${symbol} ${rule.side}`,
        `${reason} trigger hua, close order bhej diya gaya.\n\n${ruleInfo}\nOrder ID: ${orderId ?? "?"}\n` +
          (dbOk ? "" : "\n⚠️ Supabase mein rule status update NAHI hua — rule dobara trigger ho sakta hai, check karo.\n") +
          `\nTime (UTC): ${new Date().toISOString()}`,
        10 * 60 * 1000,
        true,   // critical: daily cap se nahi rukegi
      );
    } else {
      // fail ho to rule "active" hi rehta hai — agla loop-pass phir try karega
      // (isliye alert per-rule cooldown ke saath, warna har 2s mein mail jaati)
      void sendEmailAlert(
        `closefail:${rule.id}`,
        `[Trade] ⚠️ ${reason} close order FAIL — ${symbol}`,
        `${reason} trigger hua par close order REJECT/FAIL hua (HTTP ${res.status}). ` +
          `Engine har ~2s retry kar raha hai — par manual check karo!\n\n${ruleInfo}\n\n` +
          `Binance response: ${String(res.body).slice(0, 300)}\n\nTime (UTC): ${new Date().toISOString()}`,
        ALERT_FAIL_COOLDOWN_MS,
      );
    }
  } catch (e) {
    console.log(`[rules] close FAILED ${symbol}: ${String(e).slice(0, 200)}`);
    void sendEmailAlert(
      `closefail:${rule.id}`,
      `[Trade] ⚠️ ${reason} close order FAIL — ${symbol}`,
      `${reason} trigger hua par close order bhejte waqt error aaya. Engine retry kar raha hai — par manual check karo!\n\n` +
        `Symbol: ${symbol}\nSide: ${rule.side}\nQty: ${rule.entry_qty}\nEntry: ${rule.entry_price}\n` +
        `SL: ${rule.sl_price ?? "-"} | Target: ${rule.target_price ?? "-"} | Trail pts: ${rule.trailing_points ?? "-"}\n\n` +
        `Error: ${String(e).slice(0, 300)}\n\nTime (UTC): ${new Date().toISOString()}`,
      ALERT_FAIL_COOLDOWN_MS,
    );
  } finally {
    closingSymbols.delete(symbol);
  }
}

async function checkRule(rule: TradeRule): Promise<void> {
  if (closingSymbols.has(rule.symbol)) return;
  const tk = await publicRow("/eapi/v1/ticker", rule.symbol);
  if (!tk) return;
  const bid = parseFloat(String(tk.bidPrice ?? "0"));
  const ask = parseFloat(String(tk.askPrice ?? "0"));
  const isLong = rule.side === "LONG";
  const exitRef = isLong ? bid : ask;   // jis price par exit hoga, usi par check karo
  if (!(exitRef > 0)) return;

  // Trailing high-water-mark update (favorable direction mein hi)
  if (rule.trailing_points && rule.trailing_points > 0) {
    const prevHwm = rule.trail_high_water ?? rule.entry_price;
    const improved = isLong ? exitRef > prevHwm : exitRef < prevHwm;
    if (improved) {
      rule.trail_high_water = exitRef;
      await supaUpdateRule(rule.id, { trail_high_water: exitRef });
    }
  }

  let trigger: string | null = null;
  if (trigger === null && rule.sl_price !== null && rule.sl_price !== undefined) {
    if ((isLong && exitRef <= rule.sl_price) || (!isLong && exitRef >= rule.sl_price)) trigger = "SL";
  }
  if (trigger === null && rule.target_price !== null && rule.target_price !== undefined) {
    if ((isLong && exitRef >= rule.target_price) || (!isLong && exitRef <= rule.target_price)) trigger = "TARGET";
  }
  if (trigger === null && rule.trailing_points && rule.trail_high_water) {
    // Points-offset (jaisa purana HF-side engine karta tha): highest_bid - trail
    const trailStop = isLong
      ? rule.trail_high_water - rule.trailing_points
      : rule.trail_high_water + rule.trailing_points;
    if ((isLong && exitRef <= trailStop) || (!isLong && exitRef >= trailStop)) trigger = "TRAILING";
  }

  if (trigger) await closeRuleReduceOnly(rule, trigger);
}

// Idle hone par bhi ye loop hamesha chalta rehta hai (WS Feeds ki tarah
// IDLE_STOP se gated nahi) — jab tak koi active rule nahi, Binance ko
// har-cycle hit nahi karta (sirf Supabase check karta hai, halka).
// LIMITATION: agar Render (free tier) khud hi HTTP-inactivity se so jaaye,
// to ye loop bhi ruk jaayega — active rule hote waqt instance ko jagaye
// rakhne ke liye external keep-alive ping (UptimeRobot/cron-job.org, har
// ~4-5 min GET /status) lagana zaroori hai, ya paid always-on plan.
let rulesLoopRunning = false;
async function rulesMonitorLoop(): Promise<void> {
  if (rulesLoopRunning) return;
  rulesLoopRunning = true;
  while (true) {
    try {
      if (nowMs() - rulesLoadedAt > RULES_REFRESH_MS) await refreshRulesCache();
      for (const rule of [...rulesCache.values()]) await checkRule(rule);
    } catch (e) {
      console.log(`[rules] loop error: ${String(e).slice(0, 200)}`);
    }
    await sleep(rulesCache.size ? 2000 : 5000);
  }
}

async function handleRules(req: Request, url: URL): Promise<Response> {
  const ip = clientIp(req);
  if (isLocked(ip)) return tradeJson(429, { error: "Bahut galat attempts — thodi der baad try karo" });
  if (!TRADE_TOKEN) return tradeJson(503, { error: "TRADE_TOKEN set nahi hai" });
  if (!(await tokenOk(req.headers.get("x-trade-token") ?? ""))) {
    noteAuthFail(ip);
    return tradeJson(401, { error: "Unauthorized" });
  }
  if (!supaReady()) return tradeJson(503, { error: "TEST_SUPABASE_URL / TEST_SUPABASE_ANON_KEY set nahi hain" });

  const path = url.pathname;
  const m = req.method;

  if (path === "/rules/list" && m === "GET") {
    await refreshRulesCache();
    return tradeJson(200, { rules: [...rulesCache.values()] });
  }

  if (path === "/rules/create" && m === "POST") {
    const b = await readBody(req);
    if (!b) return tradeJson(400, { error: "Bad JSON body" });
    const symbol = String(b.symbol ?? "");
    const side = String(b.side ?? "").toUpperCase();
    const entry_qty = Number(b.entry_qty);
    const entry_price = Number(b.entry_price);
    const sl_price = b.sl_price !== undefined && b.sl_price !== null ? Number(b.sl_price) : null;
    const target_price = b.target_price !== undefined && b.target_price !== null ? Number(b.target_price) : null;
    const trailing_points = b.trailing_points !== undefined && b.trailing_points !== null ? Number(b.trailing_points) : null;

    if (!OPTION_SYMBOL_RE.test(symbol)) return tradeJson(400, { error: "Symbol invalid" });
    if (side !== "LONG" && side !== "SHORT") return tradeJson(400, { error: "side LONG ya SHORT" });
    if (!Number.isFinite(entry_qty) || entry_qty <= 0) return tradeJson(400, { error: "entry_qty invalid" });
    if (!Number.isFinite(entry_price) || entry_price <= 0) return tradeJson(400, { error: "entry_price invalid" });
    if (sl_price === null && target_price === null && !trailing_points) {
      return tradeJson(400, { error: "kam se kam SL, target ya trailing_points mein se ek do" });
    }

    const row = await supaInsertRule({
      symbol, side, entry_qty, entry_price,
      sl_price, target_price, trailing_points,
      trail_high_water: entry_price, status: "active",
    });
    if (!row) return tradeJson(502, { error: "Supabase insert fail — table/permissions check karo" });
    rulesCache.set(row.id, row);
    return tradeJson(200, { ok: true, rule: row });
  }

  if (path === "/rules/update" && m === "POST") {
    const b = await readBody(req);
    const id = b ? Number(b.id) : NaN;
    if (!b || !Number.isFinite(id)) return tradeJson(400, { error: "id chahiye" });
    const patch: Record<string, unknown> = {};
    if (b.sl_price !== undefined) patch.sl_price = b.sl_price === null ? null : Number(b.sl_price);
    if (b.target_price !== undefined) patch.target_price = b.target_price === null ? null : Number(b.target_price);
    if (b.trailing_points !== undefined) patch.trailing_points = b.trailing_points === null ? null : Number(b.trailing_points);
    if (!Object.keys(patch).length) return tradeJson(400, { error: "kuch update karne ko nahi bheja" });
    const ok = await supaUpdateRule(id, patch);
    if (!ok) return tradeJson(502, { error: "Supabase update fail" });
    const cur = rulesCache.get(id);
    if (cur) rulesCache.set(id, { ...cur, ...patch } as TradeRule);
    return tradeJson(200, { ok: true });
  }

  if (path === "/rules/cancel" && m === "POST") {
    const b = await readBody(req);
    const id = b ? Number(b.id) : NaN;
    if (!b || !Number.isFinite(id)) return tradeJson(400, { error: "id chahiye" });
    const ok = await supaUpdateRule(id, { status: "cancelled" });
    if (!ok) return tradeJson(502, { error: "Supabase update fail" });
    rulesCache.delete(id);
    return tradeJson(200, { ok: true });
  }

  return tradeJson(404, { error: "Unknown /rules path or method" });
}

async function handleTrade(req: Request, url: URL): Promise<Response> {
  const ip = clientIp(req);
  if (isLocked(ip)) return tradeJson(429, { error: "Bahut galat attempts — thodi der baad try karo" });
  if (!TRADE_TOKEN) return tradeJson(503, { error: "TRADE_TOKEN set nahi hai" });
  if (!(await tokenOk(req.headers.get("x-trade-token") ?? ""))) {
    noteAuthFail(ip);
    return tradeJson(401, { error: "Unauthorized" });
  }
  if (!OPT_API_KEY || !OPT_SECRET_KEY) return tradeJson(503, { error: "OPT_API_KEY / OPT_SECRET_KEY set nahi hain" });

  const path = url.pathname;
  const m = req.method;
  try {
    if (path === "/trade/status" && m === "GET") {
      return tradeJson(200, {
        trading_enabled: TRADING_ENABLED,
        keys_configured: true,
        limits: {
          max_order_qty: MAX_ORDER_QTY,
          max_order_usdt: MAX_ORDER_USDT,
          max_price_dev_pct: MAX_PRICE_DEV_PCT,
          max_orders_per_min: MAX_ORDERS_PER_MIN,
        },
        rules: "BUY + reduceOnly SELL only, LIMIT only",
        tick_rules_loaded: symRulesCache.map.size,
        backoff_sec: Math.max(0, Math.ceil((bnBackoffUntil - nowMs()) / 1000)),
        panic: "POST /trade/panic (TRADING_ENABLED se independent, sirf risk kam karta hai)",
      });
    }
    if (path === "/trade/ticksize" && m === "GET") {
      const sym = url.searchParams.get("symbol") ?? "";
      if (!OPTION_SYMBOL_RE.test(sym)) return tradeJson(400, { error: "Symbol invalid" });
      return tradeJson(200, { symbol: sym, ...(await getSymRules(sym)) });
    }
    if (path === "/trade/panic" && m === "POST") return await handlePanic();
    if (path === "/trade/account" && m === "GET") {
      const r = await signedCall("GET", "/eapi/v1/marginAccount", {});
      return tradeRaw(r.status, r.body);
    }
    if (path === "/trade/positions" && m === "GET") {
      const r = await signedCall("GET", "/eapi/v1/position", pick(url, ["symbol"]));
      return tradeRaw(r.status, r.body);
    }
    if (path === "/trade/orders/open" && m === "GET") {
      const r = await signedCall("GET", "/eapi/v1/openOrders", pick(url, ["symbol", "orderId", "limit"]));
      return tradeRaw(r.status, r.body);
    }
    if (path === "/trade/orders/history" && m === "GET") {
      const r = await signedCall("GET", "/eapi/v1/historyOrders", pick(url, ["symbol", "orderId", "startTime", "endTime", "limit"]));
      return tradeRaw(r.status, r.body);
    }
    if (path === "/trade/fills" && m === "GET") {
      const r = await signedCall("GET", "/eapi/v1/userTrades", pick(url, ["symbol", "fromId", "startTime", "endTime", "limit"]));
      return tradeRaw(r.status, r.body);
    }
    // Account bills: fees, contract (premium/settlement) flows, transfers — read-only
    if (path === "/trade/bill" && m === "GET") {
      const params = pick(url, ["currency", "recordId", "startTime", "endTime", "limit"]);
      if (!params.currency) params.currency = "USDT";
      const r = await signedCall("GET", "/eapi/v1/bill", params);
      return tradeRaw(r.status, r.body);
    }
    // Exercise / expiry (settlement) records — read-only
    if (path === "/trade/exercise" && m === "GET") {
      const r = await signedCall("GET", "/eapi/v1/exerciseRecord", pick(url, ["symbol", "startTime", "endTime", "limit"]));
      return tradeRaw(r.status, r.body);
    }
    if (path === "/trade/order" && m === "POST") return await handlePlaceOrder(req);
    if (path === "/trade/cancel" && m === "POST") return await handleCancel(req);
    if (path === "/trade/cancel-all" && m === "POST") {
      const r = await signedCall("DELETE", "/eapi/v1/allOpenOrdersByUnderlying", { underlying: "BTCUSDT" }, { bypassBackoff: true });
      console.log(`[trade] cancel-all -> HTTP ${r.status}`);
      return tradeRaw(r.status, r.body);
    }
    return tradeJson(404, { error: "Unknown /trade path or method" });
  } catch (e) {
    // Error message mein secret nahi hota (key sirf header mein jaati hai)
    const unknown = m === "POST" && (path === "/trade/order" || path === "/trade/panic")
      ? " — RESULT UNKNOWN: order gaya ho sakta hai, Orders/Positions tab check karo, blind retry mat karo" : "";
    return tradeJson(502, { error: `Trade call failed: ${String(e).slice(0, 200)}${unknown}` });
  }
}


// ── DAILY P&L SUMMARY EMAIL ─────────────────────────────────────────────────
// Roz ek baar (IST) ek mail: aaj ke fills se realized P&L + fees, symbol-wise
// breakup, aur abhi ke open positions ka unrealized P&L. Data Binance
// /eapi/v1/userTrades + /eapi/v1/position se aata hai (koi extra table nahi).
// Trade na ho tab bhi mail JAATI hai — sab columns 0 dikhte hain, balance change
// "No change", aur neeche roz ek nayi motivation line (har mail mein).
// Env vars (optional):
//   DAILY_SUMMARY_TIME_IST     default "23:55"  — roz is IST time ke baad bhejo; "off" = band
// Din = IST 00:00 se ab tak. Roz max 1 mail (Brevo 300/day par asar nahi).
// LIMITATION: Render instance us waqt so raha ho to us din ka summary nahi jaata
// (external keep-alive ping ya paid plan se theek hota hai). Bhejne ke baad
// isi din process restart ho to wahi mail dobara ja sakti hai (flag memory mein hai).
const SUMMARY_TIME_RAW = (Deno.env.get("DAILY_SUMMARY_TIME_IST") ?? "23:55").trim().toLowerCase();

// Roz ki motivation line — date ke hisaab se rotate (30 din tak repeat nahi hoti).
const MOTIVATION_LINES: string[] = [
  "Aaj trade nahi li? Koi baat nahi — capital bachana bhi ek jeet hai.",
  "Har trade ka size chhota, har rule ka respect bada. Yahi long game hai.",
  "Stop-loss haar nahi hai, ye aapki insurance hai.",
  "Market kal bhi khulega. Aaj ka loss kal ki galti sudharne ka mauka hai.",
  "Jaldi ameer banne wale aksar jaldi khatam ho jaate hain. Dheere par tikke raho.",
  "Plan ke bina trade sirf jua hai. Plan ke saath trade business hai.",
  "Ek achhi trade ke liye kabhi kabhi 10 mauke chhodne padte hain.",
  "Loss ko personal mat lo — data samjho, agla setup dhundho.",
  "Discipline wo hai jo aap tab karte ho jab koi dekh nahi raha.",
  "Profit ka lalach aur loss ka darr — dono ko rules se control karo.",
  "Revenge trade ka koi fayda nahi. Ek saans lo, screen se hato.",
  "Consistency bade ek-do wins se nahi, hazaar chhote sahi faislon se banti hai.",
  "Aaj jo seekha wahi aaj ki asli kamai hai.",
  "Risk pehle sochte hain, reward baad me. Ulta karoge to market sikha dega.",
  "Sabse achha trader wo hai jo apna capital zinda rakhta hai.",
  "Overtrading se broker kamata hai, patience se aap.",
  "Har din green hona zaroori nahi. Har din rule me rehna zaroori hai.",
  "Achha setup aayega. Jab tak nahi aata, wait karna bhi ek position hai.",
  "Journal likho. Jo naapa nahi jaata wo sudhaara nahi jaata.",
  "Bade loss ek galti se nahi, ek galti ko na maanne se hote hain.",
  "Emotion se nahi, edge se trade karo.",
  "Chhota profit lena galat nahi, bada loss rokna sahi hai.",
  "Market ko predict nahi karna — uske hisaab se react karna hai.",
  "Aaj ki thakaan kal ki clarity banegi. Aaram bhi trading ka hissa hai.",
  "Jitna sabr, utna sasta entry.",
  "Ek mahine ka result nahi, ek saal ka process dekho.",
  "Position size wahi rakho jisme neend aaye.",
  "Rules banana aasan hai, todna aasan hai, nibhana asli kaam hai.",
  "Har loss ek fees hai jo aap market ko sikhne ke liye dete ho. Value nikaalo.",
  "Kal fir ek naya din, naya chart, naya mauka. Bas apna plan saath rakhna.",
];
function motivationForDay(day: string): string {
  const dayNum = Math.floor(Date.parse(`${day}T00:00:00Z`) / 86400000);
  return MOTIVATION_LINES[((dayNum % MOTIVATION_LINES.length) + MOTIVATION_LINES.length) % MOTIVATION_LINES.length];
}
const IST_OFFSET_MS = 330 * 60 * 1000;
const SUMMARY_MINUTE_OF_DAY: number | null = (() => {
  if (SUMMARY_TIME_RAW === "off" || SUMMARY_TIME_RAW === "false") return null;
  const m = /^(\d{1,2}):(\d{2})$/.exec(SUMMARY_TIME_RAW);
  const h = m ? Number(m[1]) : NaN;
  const mi = m ? Number(m[2]) : NaN;
  if (m && h >= 0 && h <= 23 && mi >= 0 && mi <= 59) return h * 60 + mi;
  console.log(`[summary] DAILY_SUMMARY_TIME_IST "${SUMMARY_TIME_RAW}" samajh nahi aaya — default 23:55 use ho raha hai`);
  return 23 * 60 + 55;
})();

function istDayInfo(ms: number): { day: string; minuteOfDay: number } {
  const d = new Date(ms + IST_OFFSET_MS);
  return { day: d.toISOString().slice(0, 10), minuteOfDay: d.getUTCHours() * 60 + d.getUTCMinutes() };
}
const sgn2 = (v: number): string => (v >= 0 ? "+" : "") + v.toFixed(2);

// ── Daily summary ka HTML email template ────────────────────────────────────
// Plain-text jaisa hi data, bas Gmail mein ek proper card/table ki tarah dikhta
// hai: profit green, loss red, header colored net ke hisaab se.
function escHtml(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
function pnlColor(v: number): string {
  return v > 0 ? "#16a34a" : v < 0 ? "#dc2626" : "#64748b";
}
function buildSummaryHtml(p: {
  day: string; endMs: number; noTrade: boolean;
  realized: number; fees: number; net: number; balChange: string;
  fillsCount: number; wins: number; losses: number; capped: boolean;
  shownRows: [string, { fills: number; realized: number; fee: number }][];
  extraSymbolCount: number;
  posOpen: { symbol: string; side: string; qty: number; entry: number; mark: number; uPnl: number }[];
  totalUnreal: number; currentBalance: number | null;
}): string {
  const headerColor = p.net > 0 ? "#16a34a" : p.net < 0 ? "#dc2626" : "#475569";
  const windowStr = `00:00 – ${hhmmIst(p.endMs)} IST`;

  const symbolRows = p.shownRows.length
    ? p.shownRows.map(([sym, r]) => `
      <tr>
        <td style="padding:8px 10px;border-bottom:1px solid #e2e8f0;font-weight:600;color:#1e293b;">${escHtml(sym)}</td>
        <td style="padding:8px 10px;border-bottom:1px solid #e2e8f0;text-align:center;color:#475569;">${r.fills}</td>
        <td style="padding:8px 10px;border-bottom:1px solid #e2e8f0;text-align:right;font-weight:600;color:${pnlColor(r.realized)};">${sgn2(r.realized)}</td>
        <td style="padding:8px 10px;border-bottom:1px solid #e2e8f0;text-align:right;color:#475569;">${r.fee.toFixed(2)}</td>
      </tr>`).join("")
    : `<tr><td colspan="4" style="padding:12px 10px;text-align:center;color:#94a3b8;">Koi trade nahi — sab 0</td></tr>`;
  const extraRow = p.extraSymbolCount > 0
    ? `<tr><td colspan="4" style="padding:8px 10px;text-align:center;color:#94a3b8;font-style:italic;">... +${p.extraSymbolCount} aur symbols</td></tr>` : "";

  const posRows = p.posOpen.length
    ? p.posOpen.map((x) => `
      <tr>
        <td style="padding:8px 10px;border-bottom:1px solid #e2e8f0;font-weight:600;color:#1e293b;">${escHtml(x.symbol)}</td>
        <td style="padding:8px 10px;border-bottom:1px solid #e2e8f0;text-align:center;color:#475569;">${escHtml(x.side)}</td>
        <td style="padding:8px 10px;border-bottom:1px solid #e2e8f0;text-align:right;color:#475569;">${x.qty}</td>
        <td style="padding:8px 10px;border-bottom:1px solid #e2e8f0;text-align:right;color:#475569;">${x.entry}</td>
        <td style="padding:8px 10px;border-bottom:1px solid #e2e8f0;text-align:right;color:#475569;">${x.mark}</td>
        <td style="padding:8px 10px;border-bottom:1px solid #e2e8f0;text-align:right;font-weight:600;color:${pnlColor(x.uPnl)};">${sgn2(x.uPnl)}</td>
      </tr>`).join("")
    : `<tr><td colspan="6" style="padding:12px 10px;text-align:center;color:#94a3b8;">Koi open position nahi</td></tr>`;

  const statRow = (label: string, value: string, color = "#1e293b") => `
    <tr>
      <td style="padding:7px 0;color:#64748b;font-size:13px;">${label}</td>
      <td style="padding:7px 0;text-align:right;font-weight:600;color:${color};font-size:13px;">${value}</td>
    </tr>`;

  return `<!doctype html><html><body style="margin:0;padding:24px;background:#f1f5f9;font-family:Segoe UI,Roboto,Arial,sans-serif;">
  <div style="max-width:600px;margin:0 auto;background:#ffffff;border-radius:12px;overflow:hidden;border:1px solid #e2e8f0;">

    <div style="background:${headerColor};padding:20px 24px;">
      <div style="color:#ffffff;font-size:17px;font-weight:700;">📊 Daily P&amp;L Summary — ${escHtml(p.day)}</div>
      <div style="color:rgba(255,255,255,0.85);font-size:12px;margin-top:4px;">${windowStr}</div>
    </div>

    ${p.noTrade ? `<div style="margin:16px 24px 0;padding:10px 14px;background:#eff6ff;border-left:3px solid #3b82f6;border-radius:6px;color:#1e40af;font-size:13px;">ℹ️ Aaj koi trade nahi hui — sab values 0.</div>` : ""}

    <div style="padding:20px 24px 8px;">
      <table style="width:100%;border-collapse:collapse;">
        ${statRow("Realized P&amp;L", sgn2(p.realized) + " USDT", pnlColor(p.realized))}
        ${statRow("Fees", (p.fees > 0 ? "-" : "") + p.fees.toFixed(2) + " USDT", "#dc2626")}
        ${statRow("Net (approx)", sgn2(p.net) + " USDT", pnlColor(p.net))}
        ${statRow("Balance change", escHtml(p.balChange))}
        ${p.currentBalance !== null ? statRow("Current balance", p.currentBalance.toFixed(2) + " USDT") : ""}
        ${statRow("Fills / Closing trades", `${p.fillsCount} / ${p.wins + p.losses} (W${p.wins} L${p.losses})`)}
      </table>
      ${p.capped ? `<div style="margin-top:8px;font-size:12px;color:#b45309;">⚠️ 1000+ fills — list kat gayi, total kam dikh sakta hai</div>` : ""}
    </div>

    <div style="padding:8px 24px 4px;">
      <div style="font-size:13px;font-weight:700;color:#334155;margin-bottom:6px;">Symbol-wise</div>
      <table style="width:100%;border-collapse:collapse;font-size:13px;">
        <thead>
          <tr style="background:#f8fafc;">
            <th style="padding:8px 10px;text-align:left;color:#64748b;font-size:11px;text-transform:uppercase;">Symbol</th>
            <th style="padding:8px 10px;text-align:center;color:#64748b;font-size:11px;text-transform:uppercase;">Fills</th>
            <th style="padding:8px 10px;text-align:right;color:#64748b;font-size:11px;text-transform:uppercase;">Realized</th>
            <th style="padding:8px 10px;text-align:right;color:#64748b;font-size:11px;text-transform:uppercase;">Fee</th>
          </tr>
        </thead>
        <tbody>${symbolRows}${extraRow}</tbody>
      </table>
    </div>

    <div style="padding:16px 24px 4px;">
      <div style="font-size:13px;font-weight:700;color:#334155;margin-bottom:6px;">Open Positions</div>
      <table style="width:100%;border-collapse:collapse;font-size:13px;">
        <thead>
          <tr style="background:#f8fafc;">
            <th style="padding:8px 10px;text-align:left;color:#64748b;font-size:11px;text-transform:uppercase;">Symbol</th>
            <th style="padding:8px 10px;text-align:center;color:#64748b;font-size:11px;text-transform:uppercase;">Side</th>
            <th style="padding:8px 10px;text-align:right;color:#64748b;font-size:11px;text-transform:uppercase;">Qty</th>
            <th style="padding:8px 10px;text-align:right;color:#64748b;font-size:11px;text-transform:uppercase;">Entry</th>
            <th style="padding:8px 10px;text-align:right;color:#64748b;font-size:11px;text-transform:uppercase;">Mark</th>
            <th style="padding:8px 10px;text-align:right;color:#64748b;font-size:11px;text-transform:uppercase;">uPnL</th>
          </tr>
        </thead>
        <tbody>${posRows}</tbody>
      </table>
      ${p.posOpen.length ? `<div style="text-align:right;margin-top:6px;font-size:12px;color:#64748b;">Total unrealized: <span style="font-weight:700;color:${pnlColor(p.totalUnreal)};">${sgn2(p.totalUnreal)} USDT</span></div>` : ""}
    </div>

    <div style="padding:18px 24px 22px;">
      <div style="border-top:1px solid #e2e8f0;padding-top:14px;font-size:13px;color:#475569;font-style:italic;">💬 ${escHtml(motivationForDay(p.day))}</div>
    </div>

  </div>
</body></html>`;
}

const hhmmIst = (ms: number): string => {
  const i = istDayInfo(ms);
  return `${String(Math.floor(i.minuteOfDay / 60)).padStart(2, "0")}:${String(i.minuteOfDay % 60).padStart(2, "0")}`;
};

// true = kaam ho gaya (mail gayi ya jaanbujh kar skip), false = phir try karo
async function sendDailySummary(day: string, startMs: number, endMs: number): Promise<boolean> {
  const tr = await signedCall("GET", "/eapi/v1/userTrades", { startTime: startMs, endTime: endMs, limit: 1000 });
  if (tr.status < 200 || tr.status >= 300) {
    console.log(`[summary] userTrades HTTP ${tr.status} ${tr.body.slice(0, 150)}`);
    return false;
  }
  let trades: Record<string, unknown>[];
  try {
    const j = JSON.parse(tr.body);
    if (!Array.isArray(j)) { console.log("[summary] userTrades array nahi mila"); return false; }
    trades = j as Record<string, unknown>[];
  } catch { console.log("[summary] userTrades parse fail"); return false; }

  // Trade na ho tab bhi mail jaati hai — loops khaali rahenge, isliye sab values 0 aayengi.

  const per = new Map<string, { fills: number; realized: number; fee: number }>();
  let realized = 0, fees = 0, wins = 0, losses = 0;
  for (const t of trades) {
    const sym = String(t.symbol ?? "?");
    const rp = num(t.realizedProfit);
    const fee = Math.abs(num(t.fee));
    const row = per.get(sym) ?? { fills: 0, realized: 0, fee: 0 };
    row.fills++; row.realized += rp; row.fee += fee;
    per.set(sym, row);
    realized += rp; fees += fee;
    if (rp > 0) wins++; else if (rp < 0) losses++;
  }

  // Open positions (fail ho to summary phir bhi jaati hai, bas ye hissa chhoot jaata hai)
  let posText = "  (positions fetch nahi ho paye)";
  let totalUnreal = 0;
  let openPositionsForHtml: { symbol: string; side: string; qty: number; entry: number; mark: number; uPnl: number }[] = [];
  try {
    const ps = await signedCall("GET", "/eapi/v1/position", {});
    if (ps.status >= 200 && ps.status < 300) {
      const arr = JSON.parse(ps.body);
      const open = (Array.isArray(arr) ? arr : []).filter((x: Record<string, unknown>) => num(x.quantity) !== 0);
      totalUnreal = open.reduce((a: number, x: Record<string, unknown>) => a + num(x.unrealizedPNL), 0);
      openPositionsForHtml = open.map((x: Record<string, unknown>) => ({
        symbol: String(x.symbol ?? "?"), side: String(x.side ?? ""), qty: num(x.quantity),
        entry: num(x.entryPrice), mark: num(x.markPrice), uPnl: num(x.unrealizedPNL),
      }));
      posText = open.length
        ? open.map((x: Record<string, unknown>) =>
            `  ${x.symbol} ${x.side ?? ""} qty ${num(x.quantity)} entry ${num(x.entryPrice)} mark ${num(x.markPrice)} uPnL ${sgn2(num(x.unrealizedPNL))}`
          ).join("\n") + `\n  Total unrealized: ${sgn2(totalUnreal)} USDT`
        : "  Koi open position nahi\n  Total unrealized: +0.00 USDT";
    }
  } catch { /* posText default rahega */ }

  // Current balance (best effort — na mile to ye line chhoot jaati hai)
  let balLine = "";
  let currentBalance: number | null = null;
  try {
    const ac = await signedCall("GET", "/eapi/v1/marginAccount", {});
    if (ac.status >= 200 && ac.status < 300) {
      const j = JSON.parse(ac.body) as { asset?: Record<string, unknown>[] };
      const a = (Array.isArray(j?.asset) ? j.asset : []).find((x) => String(x.asset) === "USDT");
      const eq = a ? (a.equity ?? a.marginBalance) : undefined;
      if (eq !== undefined) { currentBalance = num(eq); balLine = `Current balance: ${currentBalance.toFixed(2)} USDT\n`; }
    }
  } catch { /* balLine khaali rahegi */ }

  const rows = [...per.entries()].sort((a, b) => Math.abs(b[1].realized) - Math.abs(a[1].realized));
  const shown = rows.slice(0, 15).map(([sym, r]) =>
    `  ${sym}  fills ${r.fills}  realized ${sgn2(r.realized)}  fee ${r.fee.toFixed(2)}`);
  if (rows.length > 15) shown.push(`  ... +${rows.length - 15} aur symbols`);

  const net = Math.round((realized - fees) * 100) / 100;
  const balChange = net > 0 ? `📈 Increased ${sgn2(net)} USDT`
    : net < 0 ? `📉 Decreased ${sgn2(net)} USDT`
    : "➖ No change (0.00 USDT)";
  const noTrade = trades.length === 0;

  const body =
    `📊 Daily P&L Summary — ${day} (IST)\n` +
    `Window: 00:00 – ${hhmmIst(endMs)} IST\n` +
    (noTrade ? "ℹ️ Aaj koi trade nahi hui — sab values 0.\n" : "") +
    `\nRealized P&L  : ${sgn2(realized)} USDT\n` +
    `Fees          : ${fees > 0 ? "-" : ""}${fees.toFixed(2)} USDT\n` +
    `Net (approx)  : ${sgn2(net)} USDT\n` +
    `Balance change: ${balChange}\n` +
    balLine +
    `Fills: ${trades.length} | Closing trades: ${wins + losses} (Win ${wins} / Loss ${losses})\n` +
    (trades.length >= 1000 ? "⚠️ 1000+ fills — list kat gayi, total kam dikh sakta hai\n" : "") +
    `\nSymbol-wise:\n${shown.length ? shown.join("\n") : "  (koi trade nahi — sab 0)"}\n` +
    `\nOpen positions:\n${posText}\n` +
    `\n💬 Aaj ki line: ${motivationForDay(day)}\n`;

  const html = buildSummaryHtml({
    day, endMs, noTrade, realized, fees, net, balChange,
    fillsCount: trades.length, wins, losses, capped: trades.length >= 1000,
    shownRows: rows.slice(0, 15), extraSymbolCount: Math.max(0, rows.length - 15),
    posOpen: openPositionsForHtml, totalUnreal, currentBalance,
  });

  return await sendEmailAlert(
    `summary:${day}`,
    `[Trade] Daily P&L ${day}: ${sgn2(net)} USDT (${trades.length} fills${noTrade ? " — aaj trade nahi" : ""})`,
    body,
    0,
    true,   // critical: fail-alert daily cap se nahi rukegi
    html,
  );
}

let summarySentDay = "";
let summaryTriesDay = "";
let summaryTries = 0;
let summaryNextTryMs = 0;
async function dailySummaryLoop(): Promise<void> {
  if (SUMMARY_MINUTE_OF_DAY === null) { console.log("[summary] band (DAILY_SUMMARY_TIME_IST=off)"); return; }
  console.log(`[summary] daily P&L mail chalu — roz ${String(Math.floor(SUMMARY_MINUTE_OF_DAY / 60)).padStart(2, "0")}:${String(SUMMARY_MINUTE_OF_DAY % 60).padStart(2, "0")} IST ke baad`);
  while (true) {
    try {
      const now = nowMs();
      const { day, minuteOfDay } = istDayInfo(now);
      if (day !== summaryTriesDay) { summaryTriesDay = day; summaryTries = 0; }
      if (minuteOfDay >= SUMMARY_MINUTE_OF_DAY && summarySentDay !== day && summaryTries < 3 && now >= summaryNextTryMs) {
        summaryTries++;
        const startMs = Date.parse(`${day}T00:00:00+05:30`);
        const ok = await sendDailySummary(day, startMs, now);
        if (ok) summarySentDay = day; else summaryNextTryMs = nowMs() + 10 * 60 * 1000;
      }
    } catch (e) {
      console.log(`[summary] error: ${String(e).slice(0, 200)}`);
      summaryNextTryMs = nowMs() + 10 * 60 * 1000;
    }
    await sleep(60_000);
  }
}

// ── Rules engine startup — background, IDLE_STOP se independent ────────────
rulesMonitorLoop();
dailySummaryLoop();
bnBanProbeLoop();


// ── Live status dashboard (same-origin auto-connect) ───────────────────────
// Ye render.html file ka hi content hai, seedha yahan se serve hota hai taaki
// /status.html khulte hi khud apne origin se /health, /status, /ratelimit fetch
// kar le — koi manual URL nahi daalna padta. Same file kahin aur bhi (local
// disk, doosra host) khol sakte ho — tab manual URL input dikh jaata hai.
const DASHBOARD_HTML = `<!doctype html>
<html lang="hi">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Binance Proxy — Live Status</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 20px; background: #0f172a; color: #e2e8f0;
    font-family: Segoe UI, Roboto, Arial, sans-serif;
  }
  h1 { font-size: 19px; margin: 0 0 4px; }
  .sub { color: #94a3b8; font-size: 12px; margin-bottom: 16px; }
  .urlbar {
    display: flex; gap: 8px; margin-bottom: 18px; flex-wrap: wrap; align-items: center;
  }
  .urlbar input {
    flex: 1; min-width: 220px; background: #111827; border: 1px solid #1e293b; color: #e2e8f0;
    padding: 8px 10px; border-radius: 8px; font-size: 13px;
  }
  .urlbar button {
    background: #1d4ed8; color: #fff; border: none; padding: 8px 14px; border-radius: 8px;
    font-size: 13px; cursor: pointer;
  }
  .urlbar button:hover { background: #1e40af; }
  .pill { font-size: 11px; padding: 3px 8px; border-radius: 999px; }
  .pill.live { background: #14532d; color: #86efac; }
  .pill.down { background: #7f1d1d; color: #fca5a5; }
  .toprow { display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 18px; }
  .stat {
    background: #111827; border: 1px solid #1e293b; border-radius: 10px;
    padding: 10px 14px; min-width: 130px;
  }
  .stat .label { font-size: 11px; color: #94a3b8; }
  .stat .value { font-size: 15px; font-weight: 600; margin-top: 2px; }
  h2 { font-size: 14px; color: #cbd5e1; margin: 22px 0 10px; }
  .cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(230px,1fr)); gap: 10px; margin-bottom: 22px; }
  .card { border-radius: 10px; padding: 12px 14px; border: 1px solid #1e293b; background: #111827; }
  .card.ok { border-color: #14532d; }
  .card.warning { border-color: #78350f; }
  .card.failing { border-color: #7f1d1d; background: #1f1315; }
  .name { font-size: 13px; font-weight: 600; margin-bottom: 6px; word-break: break-word; }
  .dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; }
  .ok .dot { background: #22c55e; } .warning .dot { background: #f59e0b; } .failing .dot { background: #ef4444; }
  .meta { font-size: 11px; color: #94a3b8; line-height: 1.6; }
  .err { color: #fca5a5; font-size: 11px; margin-top: 4px; word-break: break-word; }
  table { width: 100%; border-collapse: collapse; font-size: 12px; }
  th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid #1e293b; }
  th { color: #94a3b8; font-weight: 500; position: sticky; top: 0; background: #0f172a; }
  .row-ok { color: #86efac; } .row-fail { color: #fca5a5; }
  .badge { display: inline-block; padding: 1px 6px; border-radius: 4px; font-size: 10px; }
  .badge.ok { background: #14532d; color: #86efac; } .badge.fail { background: #7f1d1d; color: #fca5a5; }
  .tablewrap { max-height: 50vh; overflow: auto; border: 1px solid #1e293b; border-radius: 10px; }
  .empty { color: #64748b; padding: 16px; text-align: center; }
  .errbox {
    background: #1f1315; border: 1px solid #7f1d1d; color: #fca5a5; padding: 10px 14px;
    border-radius: 8px; font-size: 12px; margin-bottom: 16px; display: none;
  }
</style>
</head>
<body>

  <h1>Binance Proxy — Live Status</h1>
  <div class="sub">
    Render service ka URL neeche daalo (ek baar save hoga, phir har baar yaad rahega).
    Ye page kisi bhi jagah se khol sakte ho — sirf browser file hai, kahin deploy karne ki zaroorat nahi.
  </div>

  <div class="urlbar" id="urlbar">
    <input id="baseUrl" type="text" placeholder="https://your-service.onrender.com" />
    <button id="saveBtn">Save &amp; Connect</button>
    <span id="livePill" class="pill down">disconnected</span>
  </div>
  <div class="sub" id="originNote" style="display:none;"></div>

  <div class="errbox" id="errBox"></div>

  <div class="toprow" id="topStats"></div>

  <h2>🔒 Binance Request Load — Last Fail/Ban Snapshot (frozen, wipe nahi hota)</h2>
  <div class="sub" id="freezeMeta">Abhi tak koi fail/ban episode nahi hua.</div>
  <div class="tablewrap">
    <table>
      <thead><tr><th>Endpoint</th><th>Seen (proxy)</th><th>Sent (Binance)</th><th>Cache hits</th><th>Est. weight</th></tr></thead>
      <tbody id="freezeRows"><tr><td colspan="5" class="empty">—</td></tr></tbody>
    </table>
  </div>

  <h2>Binance Request Breakdown (last 60s) — kaunsi request se load h</h2>
  <div class="tablewrap">
    <table>
      <thead><tr><th>Endpoint</th><th>Seen (proxy)</th><th>Sent (Binance)</th><th>Cache hits</th><th>Est. weight</th></tr></thead>
      <tbody id="epBreakdownRows"></tbody>
    </table>
  </div>

  <h2>Depth — Top Symbols (last 5 min, Binance-bound only)</h2>
  <div class="tablewrap">
    <table>
      <thead><tr><th>Symbol</th><th>Requests sent</th></tr></thead>
      <tbody id="depthSymRows"></tbody>
    </table>
  </div>

  <h2>Endpoint Health (3+ consecutive fails → failing)</h2>
  <div class="cards" id="cards"></div>

  <h2>Recent Calls (live log)</h2>
  <div class="tablewrap">
    <table>
      <thead><tr><th>Ago</th><th>Endpoint</th><th>Status</th><th>Detail</th></tr></thead>
      <tbody id="rows"></tbody>
    </table>
  </div>

<script>
const LS_KEY = "binance_proxy_base_url";
const urlInput = document.getElementById("baseUrl");
const saveBtn = document.getElementById("saveBtn");
const livePill = document.getElementById("livePill");
const errBox = document.getElementById("errBox");
const topStats = document.getElementById("topStats");
const cardsEl = document.getElementById("cards");
const rowsEl = document.getElementById("rows");
const epBreakdownRows = document.getElementById("epBreakdownRows");
const depthSymRows = document.getElementById("depthSymRows");
const freezeMeta = document.getElementById("freezeMeta");
const freezeRows = document.getElementById("freezeRows");
const urlbarEl = document.querySelector(".urlbar");
const originNote = document.getElementById("originNote");

let pollTimer = null;

function loadSavedUrl() {
  try { return localStorage.getItem(LS_KEY) || ""; } catch { return ""; }
}
function saveUrl(u) {
  try { localStorage.setItem(LS_KEY, u); } catch { /* ignore */ }
}

function normalizeBase(u) {
  u = (u || "").trim();
  if (!u) return "";
  if (!/^https?:\\/\\//i.test(u)) u = "https://" + u;
  return u.replace(/\\/+$/, "");
}

function fmtAgo(ms) {
  if (ms == null) return "—";
  const s = Math.round(ms / 1000);
  if (s < 60) return s + "s";
  const m = Math.floor(s / 60);
  if (m < 60) return m + "m " + (s % 60) + "s";
  const h = Math.floor(m / 60);
  return h + "h " + (m % 60) + "m";
}

async function fetchJson(base, path) {
  const r = await fetch(base + path, { cache: "no-store" });
  if (!r.ok) throw new Error(path + " → HTTP " + r.status);
  return await r.json();
}

async function pollOnce() {
  const base = normalizeBase(urlInput.value);
  if (!base) return;
  try {
    const [health, status, ratelimit] = await Promise.all([
      fetchJson(base, "/health"),
      fetchJson(base, "/status"),
      fetchJson(base, "/ratelimit"),
    ]);

    livePill.textContent = "live";
    livePill.className = "pill live";
    errBox.style.display = "none";

    // ── Top stats row (from /status + /ratelimit) ──
    const stats = [
      ["Feeds running", status.feeds_running ? "✅ Yes" : "❌ No"],
      ["Symbols tracked", status.symbols],
      ["Spot price", status.spot != null ? status.spot : "—"],
      ["Ticker age", fmtAgo(status.ticker_age_ms)],
      ["Sent to Binance / 60s", ratelimit.requests_last_60s],
      ["Seen by proxy / 60s", ratelimit.requests_seen_last_60s],
      ["Cache hits / 60s", ratelimit.cache_hits_last_60s],
      ["Burst (last 5s / 10s)", ratelimit.burst_last_5s + " / " + ratelimit.burst_last_10s],
      ["Est. weight / 60s", ratelimit.estimated_weight_last_60s],
      ["Binance used weight", ratelimit.binance_used_weight_1m != null ? ratelimit.binance_used_weight_1m + " / " + ratelimit.weight_limit_1m : "—"],
      ["IP ban (418)", ratelimit.ip_banned ? ("🔴 Active — " + fmtAgo(ratelimit.ip_ban_age_ms) + " se") : "🟢 Nahi"],
      ["Agla auto-check", ratelimit.ip_banned ? ("~" + Math.ceil(ratelimit.ip_ban_next_probe_in_ms / 1000) + "s baad") : "—"],
    ];
    topStats.innerHTML = stats.map(([label, value]) =>
      '<div class="stat"><div class="label">' + label + '</div><div class="value">' + value + '</div></div>'
    ).join("");

    // ── Frozen fail/ban snapshot — sirf tab update hota hai jab backend mein
    // NAYA episode capture ho (capturedAt badal jaata hai); beech mein 4s
    // live-refresh isko chhedta nahi, isliye "wipe" nahi hota. ──
    const snap = ratelimit.last_fail_snapshot;
    if (freezeMeta) {
      freezeMeta.textContent = snap
        ? "Reason: " + snap.reason + " — frozen " + fmtAgo(snap.age_ms) + " pehle (us waqt ka 60s window hai, live nahi)"
        : "Abhi tak koi fail/ban episode nahi hua.";
    }
    if (freezeRows) {
      freezeRows.innerHTML = snap && snap.stats.endpoints.length
        ? snap.stats.endpoints.map((e) =>
            '<tr><td>' + e.endpoint + '</td><td>' + e.seen + '</td><td>' + e.sent +
            '</td><td>' + e.cacheHits + '</td><td>' + e.weight + '</td></tr>'
          ).join("")
        : '<tr><td colspan="5" class="empty">' + (snap ? "us waqt koi Binance call nahi thi" : "—") + '</td></tr>';
    }

    // ── Per-endpoint breakdown table ──
    if (epBreakdownRows) {
      epBreakdownRows.innerHTML = ratelimit.endpoints && ratelimit.endpoints.length
        ? ratelimit.endpoints.map((e) =>
            '<tr><td>' + e.endpoint + '</td><td>' + e.seen + '</td><td>' + e.sent +
            '</td><td>' + e.cacheHits + '</td><td>' + e.weight + '</td></tr>'
          ).join("")
        : '<tr><td colspan="5" class="empty">Abhi koi Binance call nahi hui (60s window)</td></tr>';
    }

    // ── Depth per-symbol breakdown table ──
    if (depthSymRows) {
      depthSymRows.innerHTML = ratelimit.depth_top_symbols_5m && ratelimit.depth_top_symbols_5m.length
        ? ratelimit.depth_top_symbols_5m.map((s) =>
            '<tr><td>' + s.symbol + '</td><td>' + s.sent + '</td></tr>'
          ).join("")
        : '<tr><td colspan="2" class="empty">Last 5 min mein koi depth call Binance tak nahi gayi</td></tr>';
    }

    // ── Endpoint health cards ──
    cardsEl.innerHTML = health.endpoints.length ? "" :
      '<div class="empty">Abhi koi endpoint track nahi hua (pehli REST call ka wait)</div>';
    for (const ep of health.endpoints) {
      const div = document.createElement("div");
      div.className = "card " + ep.status;
      div.innerHTML =
        '<div class="name"><span class="dot"></span>' + ep.name + '</div>' +
        '<div class="meta">Status: ' + ep.status.toUpperCase() +
        ' &middot; Consecutive fails: ' + ep.consecutive_fails +
        '<br>Last success: ' + fmtAgo(ep.last_ok_age_ms) + ' pehle</div>' +
        (ep.last_error ? '<div class="err">' + ep.last_error + '</div>' : '');
      cardsEl.appendChild(div);
    }

    // ── Recent calls table ──
    rowsEl.innerHTML = "";
    if (!health.recent.length) {
      rowsEl.innerHTML = '<tr><td colspan="4" class="empty">Koi recent call log nahi hai</td></tr>';
    }
    for (const e of health.recent) {
      const tr = document.createElement("tr");
      tr.className = e.ok ? "row-ok" : "row-fail";
      tr.innerHTML =
        '<td>' + fmtAgo(e.age_ms) + '</td>' +
        '<td>' + e.name + '</td>' +
        '<td><span class="badge ' + (e.ok ? "ok" : "fail") + '">' + (e.ok ? "PASS" : "FAIL") + '</span></td>' +
        '<td>' + (e.detail || "") + '</td>';
      rowsEl.appendChild(tr);
    }
  } catch (e) {
    livePill.textContent = "disconnected";
    livePill.className = "pill down";
    errBox.style.display = "block";
    errBox.textContent = "Connect nahi ho paya: " + e.message + " — URL check karo (CORS/typo/service down).";
  }
}

function startPolling() {
  if (pollTimer) clearInterval(pollTimer);
  pollOnce();
  pollTimer = setInterval(pollOnce, 4000);
}

// ── Auto-detect: agar ye page khud Render service se serve ho rahi hai
// (jaise https://your-service.onrender.com/status.html), to /health usi
// origin se turant try karo — sahi hua to URL-box hide karke seedha connect
// ho jao. File://  ya kisi aur jagah se khola ho to manual input dikhao.
async function tryAutoDetect() {
  if (location.protocol === "file:") return false;
  try {
    const r = await fetch(location.origin + "/health", { cache: "no-store" });
    if (!r.ok) return false;
    await r.json();  // confirm ye wahi API hai (JSON parse ho raha hai)
    urlInput.value = location.origin;
    urlbarEl.style.display = "none";
    originNote.style.display = "block";
    originNote.textContent = "🔗 Auto-connected: " + location.origin + " (isi service se ye page serve ho rahi hai)";
    startPolling();
    return true;
  } catch { return false; }
}

saveBtn.addEventListener("click", () => {
  const base = normalizeBase(urlInput.value);
  if (!base) return;
  urlInput.value = base;
  saveUrl(base);
  startPolling();
});
urlInput.addEventListener("keydown", (e) => { if (e.key === "Enter") saveBtn.click(); });

(async () => {
  const auto = await tryAutoDetect();
  if (auto) return;
  // Auto-detect fail hua (local file ya doosri jagah se khula) → saved URL try karo
  const saved = loadSavedUrl();
  if (saved) {
    urlInput.value = saved;
    startPolling();
  }
})();

</script>
</body>
</html>
`;

// ── Server ────────────────────────────────────────────────────────────────
Deno.serve({ port: PORT }, async (req: Request) => {
  const url = new URL(req.url);

  try {
    // Health-check
    if (url.pathname === "/") {
      return new Response("Binance proxy is running ✅ (on-demand /snapshot enabled)", { status: 200 });
    }

    // Diagnostics — koi secret nahi
    if (url.pathname === "/status") {
      const t = nowMs();
      return respondJson(req, {
        feeds_running: !feedsStopped,
        last_snapshot_request_age_ms: lastDemand ? t - lastDemand : null,
        symbols: quotes.size,
        spot: spot.price,
        ticker_age_ms: tickerTs ? t - tickerTs : null,
        ticker_error: tickerErr,
        feeds: { mark: feeds.mark.status(t), trade: feeds.trade.status(t), spot: feeds.spot.status(t) },
        rest_filter: FILTER_REST,
      });
    }

    // Rate-limit stats — koi secret nahi, browser seedha yahan se poll kar sakta hai
    // (jaise /status hai). Header icon (baad mein banega) isko use karega.
    if (url.pathname === "/ratelimit") {
      return respondJson(req, { ...bnRateStats(), last_fail_snapshot: lastFailSnapshotReport() });
    }

    // REST call health — snapshot (per-endpoint status) + rolling recent-log.
    // /status.html (neeche) isi data ko poll karke dashboard dikhata hai
    // (CORS already open hai — respondJson "Access-Control-Allow-Origin: *" bhejta hai).
    if (url.pathname === "/health") {
      const t = nowMs();
      const endpoints = [...restHealth.entries()].map(([name, h]) => ({
        name,
        status: h.fails >= REST_FAIL_THRESHOLD ? "failing" : (h.fails > 0 ? "warning" : "ok"),
        consecutive_fails: h.fails,
        last_error: h.lastError || null,
        last_ok_age_ms: h.lastOkAt ? t - h.lastOkAt : null,
      }));
      return respondJson(req, {
        threshold: REST_FAIL_THRESHOLD,
        endpoints,
        // naya sabse upar (log already chronological — bas reverse kar do)
        recent: reqLog.slice().reverse().map((e) => ({
          age_ms: t - e.ts, name: e.name, ok: e.ok, detail: e.detail,
        })),
        log_capacity: LOG_MAX,
        // App-session (HF app ki pehli demand se leke IDLE_STOP_SEC idle hone tak) —
        // isme pura session ka cumulative total hota hai, consecutive-fail counter
        // ki tarah reset nahi hota beech mein.
        session: sessionReport(),
      });
    }

    // Live status dashboard — Render URL khud kholte hi (same-origin) auto-connect ho jaata hai.
    if (url.pathname === "/status.html") {
      return respondText(req, DASHBOARD_HTML, 200, "text/html");
    }

    // ── On-demand snapshot ──
    if (url.pathname === "/snapshot") {
      return await handleSnapshot(req, url);
    }

    // ── Trade (Options) — token-protected ──
    if (url.pathname.startsWith("/trade/")) {
      return await handleTrade(req, url);
    }

    // ── SL / Target / Trailing rules — token-protected ──
    if (url.pathname.startsWith("/rules/")) {
      return await handleRules(req, url);
    }

    // ── REST forward ──
    const prefix = Object.keys(UPSTREAM_MAP).find((p) => url.pathname.startsWith(p));
    if (!prefix) {
      return new Response("Not found — /snapshot, /api/... ya /eapi/... use karo", { status: 404 });
    }

    const blocked = forwardBlockReason(req, url);
    if (blocked) {
      return new Response(JSON.stringify({ error: blocked }), {
        status: 403,
        headers: { "Content-Type": "application/json", "Access-Control-Allow-Origin": "*", "Cache-Control": "no-store" },
      });
    }

    const upstreamUrl = UPSTREAM_MAP[prefix] + url.pathname.slice(prefix.length) + url.search;

    const ttl = REST_TTL_MS[url.pathname];
    // (2026-09-25) FIX: cache key ab pathname+search hai (pehle sirf pathname
    // tha, isliye alag symbol/limit wali depth requests ek hi cache-slot
    // clobber kar detin). "symbol" param hone par bhi ab cache chalta hai —
    // depth ko isi ki zaroorat thi (exchangeInfo/ticker/mark bina symbol ke
    // hi call hote hain, isliye unke liye behavior same rehta hai).
    const cacheKey = url.pathname + url.search;
    const filterable = FILTER_REST && req.method === "GET" && ttl !== undefined;
    // Is REST-forward path se hi depth (aur mark/ticker/exchangeInfo) jaate hain —
    // isliye symbol yahin se nikal lete hain (agar query mein ho), taaki depth ka
    // per-symbol breakdown ban sake ("kaunsa strike zyada poll ho raha").
    const reqSymbol = url.searchParams.get("symbol");
    if (filterable) {
      const hit = restCache.get(cacheKey);
      if (hit && nowMs() - hit.ts < ttl) {
        // Cache-HIT — Binance ko is call ki wajah se koi request nahi gayi, par
        // "proxy ko kitni baar maanga gaya" gin-na zaroori hai (poll-frequency
        // diagnose karne ke liye — jaise browser 2s poll kar raha ho par cache
        // 1.2s TTL ho to zyadatar MISS honi chahiye; agar HIT-ratio ekdum kam
        // dikhe to iska matlab cache TTL ya poll-interval mismatch hai).
        trackBn(url.pathname, { symbol: reqSymbol, cacheHit: true, params: url.searchParams });
        return respondText(req, hit.text, hit.status, hit.ct);
      }
    }

    // Health-tracker key: prefix ke hisaab se ("/api/" → spot forward, "/eapi/" → options forward)
    const healthKey = `rest-forward ${prefix}`;

    let upstreamResp: Response;
    try {
      upstreamResp = await fetch(upstreamUrl, {
        method: req.method,
        headers: {
          "Content-Type": req.headers.get("Content-Type") ?? "application/json",
        },
        body: req.method === "GET" || req.method === "HEAD" ? undefined : await req.text(),
        signal: AbortSignal.timeout(10000),
      });
    } catch (e) {
      restFail(healthKey, String(e));
      trackBn(url.pathname, { symbol: reqSymbol, cacheHit: false, params: url.searchParams });
      throw e;   // bahar wala catch abhi bhi 502 wapas karega, behavior same
    }
    // ASLI Binance-bound call — endpoint + symbol tagged, weight estimate lagta hai.
    trackBn(url.pathname, { resp: upstreamResp, symbol: reqSymbol, params: url.searchParams });

    // Network-level fetch to gaya, par Binance-side issue ho sakta hai:
    // 429 (rate-limit), 418 (IP ban), 5xx (Binance down) → in sabko "fail" maano.
    // 4xx (400/404 etc — jaisa galat symbol client ne bheja) fail nahi, wo normal hai.
    if (upstreamResp.status === 429 || upstreamResp.status === 418 || upstreamResp.status >= 500) {
      restFail(healthKey, `HTTP ${upstreamResp.status}`);
    } else {
      restOk(healthKey);
    }

    let respBody = await upstreamResp.text();
    const ct = upstreamResp.headers.get("Content-Type") ?? "application/json";

    if (filterable && upstreamResp.status === 200) {
      try {
        respBody = JSON.stringify(filterRest(url.pathname, JSON.parse(respBody)));
        restCache.set(cacheKey, { ts: nowMs(), status: 200, text: respBody, ct });
      } catch { /* parse fail → original body as-is */ }
    }

    return respondText(req, respBody, upstreamResp.status, ct);
  } catch (err) {
    return new Response(JSON.stringify({ error: `Proxy failed: ${err}` }), {
      status: 502,
      headers: { "Content-Type": "application/json", "Access-Control-Allow-Origin": "*" },
    });
  }
});
