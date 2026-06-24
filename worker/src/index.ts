/**
 * =============================================================
 *  EUR/USD Morning Brief – Cloudflare Worker (TypeScript)
 *  Version: 4.6
 *
 *  Cron Trigger:  0 5 * * *   → 05:00 UTC = 07:00 CEST
 *  wrangler.toml: [triggers] crons = ["0 5 * * *"]
 *
 *  Umgebungsvariablen (Cloudflare Secrets):
 *    TELEGRAM_BOT_TOKEN   wrangler secret put TELEGRAM_BOT_TOKEN
 *    TELEGRAM_CHAT_ID     wrangler secret put TELEGRAM_CHAT_ID
 *    FRED_API_KEY         wrangler secret put FRED_API_KEY
 *    GH_PAT               wrangler secret put GH_PAT
 *                         (repo scope: workflow + contents write auf gh-pages)
 *
 *  HTTP-Endpunkte:
 *    POST /       → dispatch update-data.yml (aktualisiert data.json)
 *    GET  /events → events.json von gh-pages lesen
 *    PUT  /events → events.json auf gh-pages schreiben
 *    GET  /run    → Morning Brief sofort senden (Telegram)
 *
 *  FIX-LOG v4.6
 *  ─────────────────────────────────────────────────────────────
 *  #14 CORS-Header auf allen Antworten (Browser-Zugriff von gh-pages)
 *  #15 POST / dispatcht update-data.yml via GitHub API (GH_PAT)
 *  #16 GET/PUT /events liest/schreibt events.json auf gh-pages
 * =============================================================
 */

export interface Env {
  TELEGRAM_BOT_TOKEN: string;
  TELEGRAM_CHAT_ID: string;
  FRED_API_KEY: string;
  GH_PAT?: string;
}

// ─────────────────────────────────────────────────────────────
//  CORS
// ─────────────────────────────────────────────────────────────

const CORS: Record<string, string> = {
  "Access-Control-Allow-Origin":  "*",
  "Access-Control-Allow-Methods": "GET, POST, PUT, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type",
};

function jsonResp(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...CORS },
  });
}

function textResp(body: string, status = 200): Response {
  return new Response(body, {
    status,
    headers: { "Content-Type": "text/plain", ...CORS },
  });
}

// ─────────────────────────────────────────────────────────────
//  Typen
// ─────────────────────────────────────────────────────────────

interface CotData {
  date: string;
  net: number;
  delta_net: number;
  long_pct: number;
  short_pct: number;
  net_pct: number;
  oi: number;
  bias: string;
  source: string;
}

interface RetailData {
  long_pct: number;
  short_pct: number;
  long_pos: number;
  short_pos: number;
  bias: string;
  icon: string;
  source: string;
}

interface Meetings {
  fomc_date: string; fomc_days: number | null;
  ecb_date: string;  ecb_days: number | null;
  nfp_date: string;  nfp_days: number | null;
  cpi_date: string;  cpi_days: number | null;
  ppi_date: string;  ppi_days: number | null;
}

interface Signals {
  rate_diff: number | null;
  rate_bias: string;
  rate_icon: string;
  yield_spread: number | null;
  yield_bias: string;
  yield_icon: string;
  cot_bias: string;
  cot_icon: string;
}

// ─────────────────────────────────────────────────────────────
//  Konstanten
// ─────────────────────────────────────────────────────────────

const ECB_BASE       = "https://data-api.ecb.europa.eu/service/data";
const CFTC_URL       = "https://publicreporting.cftc.gov/resource/gpe5-46if.json";
const MARKETMILK_URL = "https://marketmilk.babypips.com/api/sentiment.json";
const OANDA_URL      = "https://www.oanda.com/oanda_fx_sentiment/data/getdata.json";
const EUR_CODES      = ["099741", "99741"];
const WEEKDAY_DE     = ["So", "Mo", "Di", "Mi", "Do", "Fr", "Sa"];

const GH_REPO        = "noahhwagnerr-wq/eurusd-morning-brief";
const GH_API         = "https://api.github.com";

const ECB_KNOWN_DECISIONS = [
  { dec: "2026-06-11", dfr: 2.25, eff: "2026-06-17" },
];

const FOMC_2026 = ["2026-01-29","2026-03-18","2026-04-29","2026-06-17","2026-07-29","2026-09-16","2026-10-28","2026-12-09"];
const ECB_2026  = ["2026-01-30","2026-03-19","2026-04-30","2026-06-11","2026-07-23","2026-09-10","2026-10-29","2026-12-03"];
const NFP_2026  = ["2026-02-11","2026-03-06","2026-04-03","2026-05-08","2026-06-05","2026-07-02","2026-08-07","2026-09-04","2026-10-02","2026-11-06","2026-12-04"];
const CPI_2026  = ["2026-01-13","2026-02-11","2026-03-11","2026-04-10","2026-05-12","2026-06-10","2026-07-14","2026-08-12","2026-09-11","2026-10-14","2026-11-10","2026-12-10"];
const PPI_2026  = ["2026-01-14","2026-02-12","2026-03-18","2026-04-14","2026-05-13","2026-06-11","2026-07-15","2026-08-13","2026-09-12","2026-10-15","2026-11-12","2026-12-11"];

// ─────────────────────────────────────────────────────────────
//  GitHub-Hilfsfunktionen
// ─────────────────────────────────────────────────────────────

function ghHeaders(pat: string): Record<string, string> {
  return {
    "Authorization": `token ${pat}`,
    "Accept":        "application/vnd.github+json",
    "Content-Type":  "application/json",
    "User-Agent":    "eurusd-morning-brief-worker/4.6",
  };
}

function b64Encode(str: string): string {
  const bytes = new TextEncoder().encode(str);
  let binary  = "";
  for (const b of bytes) binary += String.fromCharCode(b);
  return btoa(binary);
}

function b64Decode(b64: string): string {
  const binary = atob(b64.replace(/\n/g, ""));
  const bytes  = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return new TextDecoder().decode(bytes);
}

// ─────────────────────────────────────────────────────────────
//  HTTP-Handler: POST / → update-data.yml dispatchen
// ─────────────────────────────────────────────────────────────

async function handleDispatch(pat: string): Promise<Response> {
  const url = `${GH_API}/repos/${GH_REPO}/actions/workflows/update-data.yml/dispatches`;
  const r = await fetch(url, {
    method:  "POST",
    headers: ghHeaders(pat),
    body:    JSON.stringify({ ref: "main", inputs: { reason: "App-Button" } }),
  });

  if (r.status === 204) return jsonResp({ ok: true });

  const body = await r.text().catch(() => "");
  const status = r.status === 422 ? 422 : 500;
  return jsonResp({ ok: false, error: `GitHub ${r.status}: ${body}` }, status);
}

// ─────────────────────────────────────────────────────────────
//  HTTP-Handler: GET /events
// ─────────────────────────────────────────────────────────────

async function handleEventsGet(pat: string): Promise<Response> {
  const url = `${GH_API}/repos/${GH_REPO}/contents/events.json?ref=gh-pages`;
  const r = await fetch(url, { headers: ghHeaders(pat) });

  if (!r.ok) {
    const errText = await r.text().catch(() => "");
    console.warn(`[WARN] events GET: GitHub ${r.status} ${errText}`);
    return jsonResp({ ok: false, error: `GitHub ${r.status}` });
  }

  const meta   = await r.json() as { sha: string; content: string };
  const parsed = JSON.parse(b64Decode(meta.content));
  return jsonResp({ ok: true, sha: meta.sha, data: parsed });
}

// ─────────────────────────────────────────────────────────────
//  HTTP-Handler: PUT /events
// ─────────────────────────────────────────────────────────────

async function handleEventsPut(pat: string, request: Request): Promise<Response> {
  const body = await request.json() as { events: unknown[]; sha?: string };

  const payload = {
    events:       body.events,
    last_updated: new Date().toISOString(),
  };

  const url = `${GH_API}/repos/${GH_REPO}/contents/events.json`;
  const r = await fetch(url, {
    method:  "PUT",
    headers: ghHeaders(pat),
    body: JSON.stringify({
      message: `events: update via app ${new Date().toISOString().slice(0, 10)}`,
      content: b64Encode(JSON.stringify(payload, null, 2)),
      sha:     body.sha,
      branch:  "gh-pages",
    }),
  });

  if (!r.ok) {
    const errText = await r.text().catch(() => "");
    console.warn(`[WARN] events PUT: GitHub ${r.status} ${errText}`);
    return jsonResp({ ok: false, error: `GitHub ${r.status}: ${errText}` }, 500);
  }

  const res = await r.json() as { content?: { sha: string } };
  return jsonResp({ ok: true, sha: res.content?.sha });
}

// ─────────────────────────────────────────────────────────────
//  Hilfsfunktionen (Morning Brief)
// ─────────────────────────────────────────────────────────────

async function safeGet(url: string, params: Record<string, string> = {}, extraHeaders: Record<string, string> = {}): Promise<unknown> {
  const u = new URL(url);
  Object.entries(params).forEach(([k, v]) => u.searchParams.set(k, v));
  try {
    const r = await fetch(u.toString(), {
      headers: { "User-Agent": "eurusd-morning-brief/4.6", "Accept": "application/json", ...extraHeaders },
    });
    if (!r.ok) { console.warn(`[WARN] ${url} → HTTP ${r.status}`); return null; }
    return await r.json();
  } catch (e) {
    console.warn(`[WARN] ${url}: ${e}`);
    return null;
  }
}

function fmt(v: number | null, decimals = 2, suffix = "%"): string {
  if (v === null || v === undefined) return "N/A";
  return v.toFixed(decimals) + suffix;
}

function esc(text: string): string {
  return String(text).replace(/([_*\[\]()~`>#+=|{}.!\-])/g, "\\$1");
}

function toISO(d: Date): string {
  return d.toISOString().slice(0, 10);
}

function utcMidnight(d: Date): Date {
  return new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate()));
}

function daysBetween(a: Date, b: Date): number {
  return Math.trunc((utcMidnight(b).getTime() - utcMidnight(a).getTime()) / 86_400_000);
}

function parseYM(s: string): Date {
  if (s.length === 7) return new Date(s + "-01");
  if (s.length >= 10) return new Date(s.slice(0, 10));
  return new Date(0);
}

async function ecbLastObs(seriesPath: string, extra: Record<string, string> = {}): Promise<[number | null, string]> {
  const params: Record<string, string> = { format: "jsondata", lastNObservations: "1", detail: "dataonly", ...extra };
  const data = await safeGet(`${ECB_BASE}/${seriesPath}`, params) as any;
  if (!data) return [null, "N/A"];
  try {
    const sm    = data.dataSets[0].series;
    const key   = Object.keys(sm)[0];
    const obs   = sm[key].observations;
    const lastK = Object.keys(obs).sort((a, b) => +a - +b).pop()!;
    const val   = parseFloat(obs[lastK][0]);
    const periods = data.structure.dimensions.observation[0].values;
    let rawP = periods[+lastK].id as string;
    if (rawP.length === 7) rawP += "-01";
    else if (rawP.length === 4) rawP += "-01-01";
    return [val, rawP];
  } catch (e) {
    console.warn(`[WARN] ECB parse ${seriesPath}: ${e}`);
    return [null, "N/A"];
  }
}

async function fredObs(seriesId: string, limit = 2, sort = "desc", apiKey: string, units?: string): Promise<Array<[string, string]>> {
  const params: Record<string, string> = {
    series_id: seriesId, api_key: apiKey, file_type: "json", sort_order: sort, limit: String(limit),
  };
  if (units) params.units = units;
  const data = await safeGet("https://api.stlouisfed.org/fred/series/observations", params) as any;
  if (data?.observations) {
    return (data.observations as any[])
      .filter((o: any) => o.value !== ".")
      .map((o: any) => [o.value, o.date]);
  }
  return [];
}

// ─────────────────────────────────────────────────────────────
//  Datenabruf (Morning Brief)
// ─────────────────────────────────────────────────────────────

async function getEcbDfr(today: Date): Promise<{ val: number | null; period: string; hinweis: string | null }> {
  let val: number | null = null;
  let period = "N/A";
  for (const key of ["FM/B.U2.EUR.4F.KR.DFR.LEV", "FM/D.U2.EUR.4F.KR.DFR.LEV"]) {
    const [v, p] = await ecbLastObs(key, { endPeriod: toISO(today), lastNObservations: "1" });
    if (v !== null) { val = v; period = p; break; }
  }
  let hinweis: string | null = null;
  for (const d of ECB_KNOWN_DECISIONS) {
    const eff = new Date(d.eff); const dec = new Date(d.dec);
    if (dec <= today && today < eff && (val === null || Math.abs(val - d.dfr) > 0.001)) {
      val = d.dfr; period = d.dec; hinweis = `in Kraft ab ${d.eff}`;
    }
  }
  return { val, period, hinweis };
}

async function getEcbHicp(): Promise<[number | null, string]> {
  const eurostatBase = "https://ec.europa.eu/eurostat/api/dissemination/sdmx/2.1/data";
  for (const params of [
    { format: "JSON", geo: "EA", unit: "RCH_A", coicop: "CP00" },
    { format: "JSON", geo: "EA", unit: "RCH_A", coicop: "CP00", startPeriod: "2025-06" },
  ]) {
    const data = await safeGet(`${eurostatBase}/prc_hicp_manr`, params) as any;
    if (data) {
      try {
        const sm   = data.dataSets[0].series;
        const k    = Object.keys(sm)[0];
        const obs  = sm[k].observations;
        const meta = data.structure.dimensions.observation[0].values;
        const candidates: Array<[Date, number, string]> = [];
        for (const [ok, ov] of Object.entries(obs) as any) {
          if (ov[0] !== null) {
            const pStr = meta[+ok].id;
            const v = parseFloat(ov[0]);
            if (Math.abs(v) > 0 && Math.abs(v) <= 25) candidates.push([parseYM(pStr), v, pStr]);
          }
        }
        if (candidates.length > 0) {
          candidates.sort((a, b) => b[0].getTime() - a[0].getTime());
          console.log(`[OK] HICP Eurostat: ${candidates[0][1]}% (${candidates[0][2]})`);
          return [candidates[0][1], candidates[0][2]];
        }
      } catch (e) { console.warn(`[WARN] Eurostat HICP: ${e}`); }
    }
  }
  const [val, period] = await ecbLastObs("ICP/M.U2.N.000000.4.ANR");
  if (val !== null && Math.abs(val) > 0 && Math.abs(val) <= 25) return [val, period];
  return [null, "N/A"];
}

async function getDe2y(): Promise<[number | null, string]> {
  return await ecbLastObs("YC/B.U2.EUR.4F.G_N_A.SV_C_YM.SR_2Y");
}

async function getFedEffr(apiKey: string): Promise<[number | null, string]> {
  const obs = await fredObs("DFF", 1, "desc", apiKey);
  return obs.length > 0 ? [parseFloat(obs[0][0]), obs[0][1]] : [null, "N/A"];
}

async function getUsCpi(apiKey: string): Promise<[number | null, string]> {
  for (const sid of ["CPIAUCSL", "CPIAUCNS"]) {
    const obs = await fredObs(sid, 1, "desc", apiKey, "pc1");
    if (obs.length > 0) {
      try {
        const yoy = Math.round(parseFloat(obs[0][0]) * 10) / 10;
        console.log(`[OK] US CPI YoY (FRED pc1, ${sid}): ${yoy}% (${obs[0][1]})`);
        return [yoy, obs[0][1]];
      } catch { continue; }
    }
  }
  for (const sid of ["CPIAUCSL", "CPIAUCNS"]) {
    const obs = await fredObs(sid, 14, "desc", apiKey);
    if (obs.length < 13) continue;
    try {
      const now = parseFloat(obs[0][0]);
      const prev = parseFloat(obs[12][0]);
      const yoy = Math.round((now - prev) / prev * 1000) / 10;
      return [yoy, obs[0][1]];
    } catch { continue; }
  }
  return [null, "N/A"];
}

async function getUs2y(apiKey: string): Promise<[number | null, string]> {
  const obs = await fredObs("DGS2", 1, "desc", apiKey);
  return obs.length > 0 ? [parseFloat(obs[0][0]), obs[0][1]] : [null, "N/A"];
}

async function getCotEur(): Promise<CotData | null> {
  for (const code of EUR_CODES) {
    const data = await safeGet(CFTC_URL, {
      "$where": `cftc_contract_market_code='${code}'`,
      "$order": "report_date_as_yyyy_mm_dd DESC",
      "$limit": "2",
    }) as any[];
    if (data && data.length > 0) return parseCot(data[0], data[1] ?? {});
  }
  return null;
}

function toF(rec: Record<string, unknown>, ...keys: string[]): number {
  for (const k of keys) {
    const v = rec[k];
    if (v !== undefined && v !== null) {
      const f = parseFloat(String(v));
      if (!isNaN(f) && f > 0) return f;
    }
  }
  return 0;
}

function parseCot(cur: Record<string, unknown>, prev: Record<string, unknown>): CotData | null {
  let longC = toF(cur, "lev_money_positions_long");
  let shortC = toF(cur, "lev_money_positions_short");
  let longP = toF(prev, "lev_money_positions_long");
  let shortP = toF(prev, "lev_money_positions_short");
  let source = "Disaggregated/Leveraged Money";
  if (longC === 0 && shortC === 0) {
    longC = toF(cur, "asset_mgr_positions_long");
    shortC = toF(cur, "asset_mgr_positions_short");
    longP = toF(prev, "asset_mgr_positions_long");
    shortP = toF(prev, "asset_mgr_positions_short");
    source = "Disaggregated/Asset Manager";
  }
  if (longC === 0 && shortC === 0) return null;
  const oi = toF(cur, "open_interest_all") || longC + shortC;
  const net = longC - shortC;
  const netPrev = (longP || shortP) ? longP - shortP : 0;
  const netPct = oi > 0 ? Math.round(net / oi * 1000) / 10 : 0;
  const longPct = toF(cur, "pct_of_oi_lev_money_long", "pct_of_oi_asset_mgr_long") || (oi > 0 ? Math.round(longC / oi * 1000) / 10 : 0);
  const shortPct = toF(cur, "pct_of_oi_lev_money_short", "pct_of_oi_asset_mgr_short") || (oi > 0 ? Math.round(shortC / oi * 1000) / 10 : 0);
  const bias = netPct > 5 ? "NET-LONG" : netPct < -5 ? "NET-SHORT" : "NEUTRAL";
  const rawDate = String(cur.report_date_as_yyyy_mm_dd ?? cur.report_date_as_mm_dd_yyyy ?? "N/A").slice(0, 10);
  return { date: rawDate, net: Math.round(net), delta_net: Math.round(net - netPrev), long_pct: longPct, short_pct: shortPct, net_pct: netPct, oi: Math.round(oi), bias, source };
}

function buildRetail(longPct: number, shortPct: number, longPos: number, shortPos: number, source: string): RetailData {
  let bias: string; let icon: string;
  if (shortPct >= 60)      { bias = "CONTRARIAN BULLISCH"; icon = "🟢"; }
  else if (longPct >= 60)  { bias = "CONTRARIAN BÄRISCH";  icon = "🔴"; }
  else if (shortPct >= 55) { bias = "LEICHT CONTRARIAN BULLISCH"; icon = "🟡"; }
  else if (longPct >= 55)  { bias = "LEICHT CONTRARIAN BÄRISCH";  icon = "🟡"; }
  else                     { bias = "NEUTRAL"; icon = "⚪"; }
  return { long_pct: longPct, short_pct: shortPct, long_pos: longPos, short_pos: shortPos, bias, icon, source };
}

async function getRetailSentiment(): Promise<RetailData | null> {
  const mmData = await safeGet(MARKETMILK_URL, { pair: "EURUSD" }) as any;
  if (mmData && !mmData.error) {
    try {
      const lp = parseFloat(mmData.long_percentage ?? mmData.longPercentage ?? 0);
      const sp = parseFloat(mmData.short_percentage ?? mmData.shortPercentage ?? 0);
      if (lp + sp > 0) {
        const lpos = parseInt(mmData.long_positions ?? mmData.longPositions ?? 0);
        const spos = parseInt(mmData.short_positions ?? mmData.shortPositions ?? 0);
        console.log(`[OK] Retail (MarketMilk): Long=${lp.toFixed(1)}% Short=${sp.toFixed(1)}%`);
        return buildRetail(lp, sp, lpos, spos, "MarketMilk/BabyPips");
      }
    } catch (e) { console.warn(`[WARN] MarketMilk parse: ${e}`); }
  }
  const oandaData = await safeGet(OANDA_URL, { instrument: "EUR_USD" }) as any;
  if (oandaData) {
    try {
      const items: any[] = oandaData.data ?? oandaData.orderBook ?? [];
      if (items.length > 0) {
        const longVol  = items.reduce((s: number, i: any) => s + parseFloat(i.longCountPercent  ?? 0), 0);
        const shortVol = items.reduce((s: number, i: any) => s + parseFloat(i.shortCountPercent ?? 0), 0);
        const total = longVol + shortVol;
        if (total > 0) {
          const lp = Math.round(longVol  / total * 1000) / 10;
          const sp = Math.round(shortVol / total * 1000) / 10;
          console.log(`[OK] Retail (Oanda): Long=${lp.toFixed(1)}% Short=${sp.toFixed(1)}%`);
          return buildRetail(lp, sp, 0, 0, "Oanda Orderbook");
        }
      }
    } catch (e) { console.warn(`[WARN] Oanda parse: ${e}`); }
  }
  console.warn("[WARN] Retail Sentiment: alle Quellen nicht erreichbar");
  return null;
}

// ─────────────────────────────────────────────────────────────
//  Event-Kalender
// ─────────────────────────────────────────────────────────────

function getNextMeetings(today: Date): Meetings {
  const todayMid = utcMidnight(today);
  const next = (dates: string[]): Date | null => {
    const fut = dates
      .map(d => new Date(d))
      .filter(d => utcMidnight(d) >= todayMid)
      .sort((a, b) => +a - +b);
    return fut[0] ?? null;
  };
  const fmtD = (d: Date | null) =>
    d ? d.toLocaleDateString("de-DE", { day: "2-digit", month: "2-digit", year: "numeric" }) : "N/A";
  const days = (d: Date | null) => d !== null ? daysBetween(today, d) : null;
  const nf = next(FOMC_2026), ne = next(ECB_2026), nn = next(NFP_2026), nc = next(CPI_2026), np = next(PPI_2026);
  return {
    fomc_date: fmtD(nf), fomc_days: days(nf),
    ecb_date:  fmtD(ne), ecb_days:  days(ne),
    nfp_date:  fmtD(nn), nfp_days:  days(nn),
    cpi_date:  fmtD(nc), cpi_days:  days(nc),
    ppi_date:  fmtD(np), ppi_days:  days(np),
  };
}

// ─────────────────────────────────────────────────────────────
//  Signale
// ─────────────────────────────────────────────────────────────

function computeSignals(ecbDfr: number | null, fedEffr: number | null, us2y: number | null, de2y: number | null, cot: CotData | null): Signals {
  const s: Partial<Signals> = {};
  if (fedEffr !== null && ecbDfr !== null) {
    const diff = Math.round((fedEffr - ecbDfr) * 100) / 100;
    s.rate_diff = diff; s.rate_bias = diff > 0 ? "BÄRISCH" : "BULLISCH"; s.rate_icon = diff > 0 ? "🔴" : "🟢";
  } else { s.rate_diff = null; s.rate_bias = "N/A"; s.rate_icon = "⚪"; }
  if (us2y !== null && de2y !== null) {
    const sp = Math.round((us2y - de2y) * 100) / 100;
    s.yield_spread = sp; s.yield_bias = sp > 0 ? "USD‑Vorteil" : "EUR‑Vorteil"; s.yield_icon = sp > 0 ? "🔴" : "🟢";
  } else { s.yield_spread = null; s.yield_bias = "N/A"; s.yield_icon = "⚪"; }
  if (cot) {
    const np = cot.net_pct;
    s.cot_bias = np > 5 ? "NET‑LONG" : np < -5 ? "NET‑SHORT" : "NEUTRAL";
    s.cot_icon = np > 5 ? "🟢" : np < -5 ? "🔴" : "⚪";
  } else { s.cot_bias = "N/A"; s.cot_icon = "⚪"; }
  return s as Signals;
}

// ─────────────────────────────────────────────────────────────
//  Event-Zeile
// ─────────────────────────────────────────────────────────────

function eventLine(label: string, days: number | null, dateStr: string): string {
  if (days === null) return `  ${esc(label).padEnd(14)} \`N/A\``;
  let icon: string; let cd: string;
  if (days === 0)      { icon = "🔴"; cd = "HEUTE"; }
  else if (days === 1) { icon = "🟠"; cd = "morgen"; }
  else if (days <= 5)  { icon = "🟡"; cd = `in ${days}T`; }
  else                 { icon = "⚫"; cd = `in ${days}T`; }
  return `  ${icon} ${esc(label).padEnd(10)} \`${esc(dateStr)}\` _${esc(cd)}_`;
}

// ─────────────────────────────────────────────────────────────
//  Nachricht bauen
// ─────────────────────────────────────────────────────────────

function buildMessage(
  ecbDfr: number | null, ecbDfrDate: string, ecbDfrHinweis: string | null,
  ecbHicp: number | null, ecbHicpDate: string,
  fedEffr: number | null, fedEffrDate: string,
  usCpi: number | null, usCpiDate: string,
  us2y: number | null, _us2yDate: string, de2y: number | null, _de2yDate: string,
  cot: CotData | null, meetings: Meetings, signals: Signals,
  retail: RetailData | null, today: Date
): string {
  const wd = WEEKDAY_DE[today.getDay()];
  const dd = today.toLocaleDateString("de-DE", { day: "2-digit", month: "2-digit", year: "numeric" });
  const dateStr = esc(`${wd}, ${dd}`);

  const rd = signals.rate_diff;
  const rdStr = esc(rd !== null ? (rd >= 0 ? `+${rd.toFixed(2)}pp` : `${rd.toFixed(2)}pp`) : "N/A");
  const ys = signals.yield_spread;
  const ysStr = esc(ys !== null ? (ys >= 0 ? `+${ys.toFixed(2)}pp` : `${ys.toFixed(2)}pp`) : "N/A");

  const netVal   = cot?.net ?? 0;
  const delta    = cot?.delta_net ?? 0;
  const deltaSym = delta >= 0 ? "▲" : "▼";
  const netStr   = cot ? esc(`${netVal >= 0 ? "+" : ""}${netVal.toLocaleString("de-DE")}`) : esc("N/A");
  const deltaStr = cot ? esc(`${deltaSym} ${Math.abs(delta).toLocaleString("de-DE")}`) : esc("N/A");
  const oiStr    = cot ? esc(cot.oi.toLocaleString("de-DE")) : esc("N/A");
  const lp       = cot ? esc(`${cot.long_pct.toFixed(1)}%`) : esc("N/A");
  const sp       = cot ? esc(`${cot.short_pct.toFixed(1)}%`) : esc("N/A");
  const npPct    = cot ? esc(`${cot.net_pct.toFixed(1)}%`) : esc("N/A");
  const cotDate  = esc(cot?.date ?? "N/A");
  const cotSrc   = esc(cot?.source ?? "N/A");

  const eDfr      = esc(fmt(ecbDfr));
  const eDfrDate  = esc(ecbDfrDate.slice(0, 10));
  const eDfrNote  = ecbDfrHinweis ? ` _\\(${esc(ecbDfrHinweis)}\\)_` : "";
  const eHicp     = esc(fmt(ecbHicp));
  const eHicpDate = esc(ecbHicpDate.slice(0, 7));
  const eEffr     = esc(fmt(fedEffr));
  const eEffrDate = esc(fedEffrDate.slice(0, 10));
  const eCpi      = esc(fmt(usCpi, 1));
  const eCpiDate  = esc(usCpiDate.slice(0, 7));
  const eUs2y     = esc(fmt(us2y));
  const eDe2y     = esc(fmt(de2y));

  const ri = signals.rate_icon;
  const yi = signals.yield_icon;
  const ci = signals.cot_icon;

  const retailBlock = retail ? [
    "",
    "━━━━━━━━━━━━━━━━━━━━━━",
    "👥 *Retail Sentiment · EUR/USD*",
    `_${esc(retail.source)}_`,
    "",
    `  Long  \`${esc(retail.long_pct.toFixed(1))}%\` \\(${retail.long_pos > 0 ? esc(retail.long_pos.toLocaleString("de-DE")) : esc("–")} Positionen\\)`,
    `  Short \`${esc(retail.short_pct.toFixed(1))}%\` \\(${retail.short_pos > 0 ? esc(retail.short_pos.toLocaleString("de-DE")) : esc("–")} Positionen\\)`,
    `  Contrarian\\-Bias: ${retail.icon} \`${esc(retail.bias)}\``,
  ] : [
    "",
    "━━━━━━━━━━━━━━━━━━━━━━",
    "👥 *Retail Sentiment · EUR/USD*",
    "",
    "  `N/A` – alle Quellen nicht erreichbar",
  ];

  return [
    "📊 *EUR/USD · Morning Brief*",
    `_${dateStr}_`,
    "",
    "━━━━━━━━━━━━━━━━━━━━━━",
    "🇪🇺 *EZB*",
    `Leitzins \\(DFR\\)  \`${eDfr}\`  \\| Stand \`${eDfrDate}\`${eDfrNote}`,
    `Inflation HVPI   \`${eHicp}\` \\| Stand \`${eHicpDate}\``,
    "",
    "🇺🇸 *Federal Reserve*",
    `Leitzins \\(EFFR\\) \`${eEffr}\`  \\| Stand \`${eEffrDate}\``,
    `Inflation CPI    \`${eCpi}\` \\| Stand \`${eCpiDate}\``,
    "",
    "━━━━━━━━━━━━━━━━━━━━━━",
    "📈 *Kapitalfluss · Zinsdifferenz*",
    "",
    `  EFFR vs\\. DFR  \`${rdStr}\`  ${ri} _${esc(signals.rate_bias)}_`,
    `  US 2Y \`${eUs2y}\` · DE 2Y \`${eDe2y}\``,
    `  2Y\\-Spread   \`${ysStr}\`  ${yi} _${esc(signals.yield_bias)}_`,
    "",
    "━━━━━━━━━━━━━━━━━━━━━━",
    "📋 *COT · CME EUR Futures 6E*",
    `_Stand: ${cotDate} · Quelle: ${cotSrc}_`,
    "",
    `  Net\\-Position  \`${netStr}\` Kontrakte`,
    `  Δ Vorwoche     \`${deltaStr}\` Kontrakte`,
    `  Long \`${lp}\` · Short \`${sp}\` · Net\\-OI \`${npPct}\``,
    `  Open Interest  \`${oiStr}\` Kontrakte`,
    `  Bias: ${ci} \`${esc(cot?.bias ?? "N/A")}\``,
    ...retailBlock,
    "",
    "━━━━━━━━━━━━━━━━━━━━━━",
    "🗓 *Nächste High\\-Impact Events*",
    "",
    eventLine("FOMC", meetings.fomc_days, meetings.fomc_date),
    eventLine("EZB",  meetings.ecb_days,  meetings.ecb_date),
    eventLine("NFP",  meetings.nfp_days,  meetings.nfp_date),
    eventLine("CPI",  meetings.cpi_days,  meetings.cpi_date),
    eventLine("PPI",  meetings.ppi_days,  meetings.ppi_date),
    "",
    "━━━━━━━━━━━━━━━━━━━━━━",
    "🎯 *Gesamt\\-Bias EUR/USD*",
    "",
    `  Zinsdiff\\. ${ri} \`${esc(signals.rate_bias)}\``,
    `  2Y\\-Spread ${yi} \`${esc(signals.yield_bias)}\``,
    `  COT        ${ci} \`${esc(signals.cot_bias)}\``,
    `  Retail     ${retail?.icon ?? "⚪"} \`${esc(retail?.bias ?? "N/A")}\``,
    "",
    "_ECB SDMX · Eurostat · FRED · CFTC · MarketMilk_",
  ].join("\n");
}

// ─────────────────────────────────────────────────────────────
//  Telegram
// ─────────────────────────────────────────────────────────────

async function sendTelegram(token: string, chatId: string, message: string): Promise<void> {
  const r = await fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ chat_id: chatId, text: message, parse_mode: "MarkdownV2" }),
  });
  if (!r.ok) {
    const body = await r.text();
    throw new Error(`Telegram HTTP ${r.status}: ${body}`);
  }
  const res = await r.json() as any;
  console.log(`[OK] Telegram msg_id=${res.result?.message_id}`);
}

// ─────────────────────────────────────────────────────────────
//  Morning Brief ausführen
// ─────────────────────────────────────────────────────────────

async function run(env: Env): Promise<void> {
  const today = new Date();
  console.log(`[${today.toISOString()}] EUR/USD Morning Brief v4.6 (Worker)`);

  const [ecbDfrResult, [ecbHicp, ecbHicpDate], [fedEffr, fedEffrDate], [usCpi, usCpiDate], [us2y, us2yDate], [de2y, de2yDate], cot, retail] =
    await Promise.all([
      getEcbDfr(today),
      getEcbHicp(),
      getFedEffr(env.FRED_API_KEY),
      getUsCpi(env.FRED_API_KEY),
      getUs2y(env.FRED_API_KEY),
      getDe2y(),
      getCotEur(),
      getRetailSentiment(),
    ]);

  const { val: ecbDfr, period: ecbDfrDate, hinweis: ecbDfrHinweis } = ecbDfrResult;
  const meetings = getNextMeetings(today);
  const signals  = computeSignals(ecbDfr, fedEffr, us2y, de2y, cot);

  const message = buildMessage(
    ecbDfr, ecbDfrDate, ecbDfrHinweis,
    ecbHicp, ecbHicpDate,
    fedEffr, fedEffrDate,
    usCpi, usCpiDate,
    us2y, us2yDate, de2y, de2yDate,
    cot, meetings, signals, retail, today
  );

  await sendTelegram(env.TELEGRAM_BOT_TOKEN, env.TELEGRAM_CHAT_ID, message);
}

// ─────────────────────────────────────────────────────────────
//  Exports (Cron + HTTP)
// ─────────────────────────────────────────────────────────────

export default {
  async scheduled(_event: ScheduledEvent, env: Env, ctx: ExecutionContext): Promise<void> {
    ctx.waitUntil(run(env));
  },

  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    const url    = new URL(request.url);
    const method = request.method.toUpperCase();

    // CORS preflight
    if (method === "OPTIONS") {
      return new Response(null, { status: 204, headers: CORS });
    }

    // GET /run → Morning Brief sofort senden
    if (method === "GET" && url.pathname === "/run") {
      ctx.waitUntil(run(env));
      return textResp("✅ Morning Brief gestartet");
    }

    // POST / → update-data.yml dispatchen (aktualisiert data.json auf gh-pages)
    if (method === "POST" && (url.pathname === "/" || url.pathname === "")) {
      if (!env.GH_PAT) {
        return jsonResp({ ok: false, error: "GH_PAT nicht konfiguriert" }, 422);
      }
      return handleDispatch(env.GH_PAT);
    }

    // GET /events → events.json von gh-pages lesen
    if (method === "GET" && url.pathname === "/events") {
      if (!env.GH_PAT) {
        return jsonResp({ ok: false, error: "GH_PAT nicht konfiguriert" }, 422);
      }
      return handleEventsGet(env.GH_PAT);
    }

    // PUT /events → events.json auf gh-pages schreiben
    if (method === "PUT" && url.pathname === "/events") {
      if (!env.GH_PAT) {
        return jsonResp({ ok: false, error: "GH_PAT nicht konfiguriert" }, 422);
      }
      return handleEventsPut(env.GH_PAT, request);
    }

    return textResp("EUR/USD Morning Brief Worker v4.6\nPOST / → data.json aktualisieren\nGET /run → Brief sofort senden\nGET /events · PUT /events → Event-Daten");
  },
};
