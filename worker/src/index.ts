/**
 * =============================================================
 *  EUR/USD Morning Brief – Cloudflare Worker (TypeScript)
 *  Version: 5.0
 *
 *  HTTP Endpoints:
 *    POST /          → Triggert GitHub Actions workflow_dispatch (update-data.yml)
 *    GET  /events    → KV-Event-Daten lesen
 *    PUT  /events    → KV-Event-Daten schreiben
 *
 *  Cloudflare Secrets (wrangler secret put):
 *    GH_PAT          → GitHub Personal Access Token (repo + workflow scope)
 *    FRED_API_KEY    → nicht mehr benötigt, kann entfernt werden
 *
 *  KV Namespace:
 *    EVENTS_KV       → wrangler.toml binding
 * =============================================================
 */

export interface Env {
  GH_PAT: string;
  EVENTS_KV: KVNamespace;
}

const GH_OWNER    = "noahhwagnerr-wq";
const GH_REPO     = "eurusd-morning-brief";
const GH_WORKFLOW = "update-data.yml";
const GH_REF      = "main";

// ─────────────────────────────────────────────────────────────
//  GitHub Actions Trigger
// ─────────────────────────────────────────────────────────────

async function triggerGitHubWorkflow(pat: string): Promise<{ ok: boolean; error?: string }> {
  const url = `https://api.github.com/repos/${GH_OWNER}/${GH_REPO}/actions/workflows/${GH_WORKFLOW}/dispatches`;
  const r = await fetch(url, {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${pat}`,
      "Accept": "application/vnd.github+json",
      "Content-Type": "application/json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "eurusd-morning-brief-worker/5.0",
    },
    body: JSON.stringify({ ref: GH_REF }),
  });
  if (r.status === 204) return { ok: true };
  const text = await r.text();
  console.error(`[ERR] GitHub dispatch HTTP ${r.status}: ${text}`);
  return { ok: false, error: `GitHub API ${r.status}: ${text}` };
}

// ─────────────────────────────────────────────────────────────
//  CORS
// ─────────────────────────────────────────────────────────────

function corsHeaders(): Record<string, string> {
  return {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, PUT, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
  };
}

function jsonResponse(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json", ...corsHeaders() },
  });
}

// ─────────────────────────────────────────────────────────────
//  fetch Handler
// ─────────────────────────────────────────────────────────────

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);

    // OPTIONS preflight
    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: corsHeaders() });
    }

    // POST / → GitHub Actions workflow_dispatch
    if (request.method === "POST" && (url.pathname === "/" || url.pathname === "")) {
      if (!env.GH_PAT) {
        return jsonResponse({ ok: false, error: "GH_PAT not configured" }, 500);
      }
      const result = await triggerGitHubWorkflow(env.GH_PAT);
      if (result.ok) {
        console.log("[OK] GitHub Actions workflow_dispatch ausgelöst");
        return jsonResponse({ ok: true });
      } else {
        return jsonResponse({ ok: false, error: result.error }, 502);
      }
    }

    // GET /events → KV lesen
    if (request.method === "GET" && url.pathname === "/events") {
      try {
        const { value, metadata } = await (env.EVENTS_KV as any).getWithMetadata("events", { type: "json" });
        return jsonResponse({ ok: true, data: value ?? { events: [] }, sha: (metadata as any)?.sha ?? null });
      } catch {
        return jsonResponse({ ok: true, data: { events: [] }, sha: null });
      }
    }

    // PUT /events → KV schreiben
    if (request.method === "PUT" && url.pathname === "/events") {
      try {
        const body = await request.json() as any;
        const sha = Date.now().toString(36);
        await (env.EVENTS_KV as any).put("events", JSON.stringify(body), { metadata: { sha } });
        return jsonResponse({ ok: true, sha });
      } catch (e) {
        return jsonResponse({ ok: false, error: String(e) }, 500);
      }
    }

    // Default
    return new Response(
      "EUR/USD Morning Brief Worker v5.0\nPOST / → GitHub Actions triggern\nGET  /events → Event-Daten lesen\nPUT  /events → Event-Daten schreiben",
      { status: 200, headers: corsHeaders() }
    );
  },
};
