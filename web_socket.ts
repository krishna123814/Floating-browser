// main.ts — Deno Deploy par deploy karne wala standalone network-diagnostic
// script. Maqsad: sirf ye check karna ki Deno Deploy ke outbound IP se
// Binance ke REST + WebSocket endpoints (spot/futures/options-eoptions)
// reachable hain ya nahi — khaas kar `nbstream.binance.com/eoptions/ws/...`
// (jahan option-depth stream aata hai), jo humne pehle Cloudflare Worker
// se try kiya tha aur woh block ho gaya tha.
//
// Isme koi Render/Binance-proxy logic nahi hai — ye SIRF ek reachability
// test hai. Result dekhne ke baad hi depth-publisher wala asli kaam is
// platform par banayenge (agar test pass hua).
//
// Deploy: is file ko GitHub repo ke root mein "main.ts" naam se rakho aur
// Deno Deploy ko us repo se connect karo (entry point: main.ts).
// Test: deploy hone ke baad apne app ka URL browser mein kholo — turant
// report dikh jayegi (GET request par hi pura test chalta hai).

function nowMs(): number {
  return Date.now();
}

// ── REST reachability test ──────────────────────────────────────────────
async function restTest(url: string, timeoutMs = 10_000): Promise<{ ok: boolean; detail: string }> {
  const t0 = nowMs();
  try {
    const resp = await fetch(url, { signal: AbortSignal.timeout(timeoutMs) });
    const ms = nowMs() - t0;
    if (resp.status === 200) {
      return { ok: true, detail: `OK — HTTP 200 in ${ms}ms` };
    }
    const bodySnippet = (await resp.text().catch(() => "")).slice(0, 150);
    return { ok: false, detail: `HTTP ${resp.status} in ${ms}ms — ${bodySnippet}` };
  } catch (e) {
    const ms = nowMs() - t0;
    return { ok: false, detail: `FAILED after ${ms}ms — ${(e as Error).name}: ${(e as Error).message}` };
  }
}

// ── WebSocket reachability test — connect, wait for first message, close ─
function wsTest(url: string, timeoutMs = 8_000): Promise<{ ok: boolean; detail: string }> {
  return new Promise((resolve) => {
    const t0 = nowMs();
    let settled = false;
    let ws: WebSocket;
    try {
      ws = new WebSocket(url);
    } catch (e) {
      resolve({ ok: false, detail: `new WebSocket() threw — ${(e as Error).name}: ${(e as Error).message}` });
      return;
    }
    const finish = (ok: boolean, detail: string) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      try { ws.close(); } catch { /* ignore */ }
      resolve({ ok, detail });
    };
    const timer = setTimeout(() => {
      const msOpen = nowMs() - t0;
      finish(false, `TIMEOUT after ${msOpen}ms waiting for open/message (readyState=${ws.readyState})`);
    }, timeoutMs);
    ws.onopen = () => {
      const msOpen = nowMs() - t0;
      // onopen hi mil gaya matlab connection ban gaya — ab ek message ka wait
      // karte hain taaki confirm ho "feed live hai", sirf handshake nahi.
      (ws as WebSocket & { _msOpen?: number })._msOpen = msOpen;
    };
    ws.onmessage = (ev) => {
      const msOpen = (ws as WebSocket & { _msOpen?: number })._msOpen ?? (nowMs() - t0);
      const size = typeof ev.data === "string" ? ev.data.length : (ev.data as ArrayBuffer)?.byteLength ?? 0;
      finish(true, `OPEN ✅ in ${msOpen}ms — first message received (${size} bytes) — feed is live`);
    };
    ws.onerror = () => {
      // onerror ke baad onclose bhi aayega — real detail wahan milega, isliye
      // yahan turant finish nahi karte, thoda wait karte hain onclose ka.
    };
    ws.onclose = (ev) => {
      const msOpen = nowMs() - t0;
      finish(false, `Closed before any message — code ${ev.code}, reason: ${ev.reason || "(none)"}, after ${msOpen}ms`);
    };
  });
}

// ── Render relay se ek live, still-trading BTC option symbol nikalta hai ─
// (taaki fake/expired symbol se galat FAIL na aaye) — same Render endpoint
// jo already working hai, isliye ye hop khud kabhi issue nahi hoga.
async function fetchLiveOptionSymbol(): Promise<string> {
  try {
    const resp = await fetch("https://my-engine-au06.onrender.com/eapi/v1/exchangeInfo", {
      signal: AbortSignal.timeout(10_000),
    });
    const info = await resp.json();
    const nowMsVal = Date.now();
    const syms = (info?.optionSymbols ?? [])
      .filter((s: { underlying?: string; expiryDate?: number; status?: string }) =>
        s.underlying === "BTCUSDT" && (s.expiryDate ?? 0) > nowMsVal && (s.status ?? "TRADING") === "TRADING"
      )
      .sort((a: { expiryDate: number }, b: { expiryDate: number }) => a.expiryDate - b.expiryDate);
    return syms[0]?.symbol ?? "";
  } catch {
    return "";
  }
}

async function runDiagnostic(): Promise<string> {
  const lines: string[] = [];
  lines.push("=== Deno Deploy → Binance reachability diagnostic ===");
  lines.push(`Run at: ${new Date().toISOString()}\n`);

  lines.push("--- A: live option symbol (via Render relay, already-working path) ---");
  let sym = await fetchLiveOptionSymbol();
  if (sym) {
    lines.push(`Symbol auto-picked: ${sym}`);
  } else {
    sym = "BTC-260926-84000-C";
    lines.push(`Fetch failed — fallback (may be expired): ${sym}`);
  }
  lines.push("");

  lines.push("--- B: direct Binance WebSocket tests (from Deno Deploy) ---");
  const wsTests: Array<[string, string]> = [
    ["Spot WS (stream.binance.com)", "wss://stream.binance.com:9443/ws/btcusdt@trade"],
    ["Futures WS (fstream.binance.com)", "wss://fstream.binance.com/ws/btcusdt@markPrice"],
    [`Options WS REAL symbol (${sym})`, `wss://nbstream.binance.com/eoptions/ws/${sym}@depth@100ms`],
    ["Options WS WRONG symbol (control)", "wss://nbstream.binance.com/eoptions/ws/btcusdt@depth@100ms"],
  ];
  for (const [label, url] of wsTests) {
    const { ok, detail } = await wsTest(url);
    lines.push(`${ok ? "✅" : "❌"} ${label}: ${detail}`);
  }
  lines.push("");

  lines.push("--- C: direct Binance REST tests (from Deno Deploy) ---");
  const restTests: Array<[string, string]> = [
    ["Spot REST (api.binance.com)", "https://api.binance.com/api/v3/ping"],
    ["Options REST (eapi.binance.com)", "https://eapi.binance.com/eapi/v1/ping"],
  ];
  for (const [label, url] of restTests) {
    const { ok, detail } = await restTest(url);
    lines.push(`${ok ? "✅" : "❌"} ${label}: ${detail}`);
  }

  return lines.join("\n");
}

Deno.serve(async (_req: Request) => {
  const report = await runDiagnostic();
  return new Response(report, {
    headers: { "content-type": "text/plain; charset=utf-8" },
  });
});
