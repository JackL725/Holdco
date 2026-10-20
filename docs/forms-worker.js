/**
 * Holdco forms Worker: a template Cloudflare Worker that stores your site's form submissions and serves them to
 * Holdco's forms connector (Worker mode). Holdco polls it once an hour with a GET; nothing ever calls into your PC.
 * Use it when the site is not on Netlify (Netlify Forms needs no Worker: pick the netlify mode instead).
 *
 * Set up (Cloudflare dashboard, or wrangler with the same names):
 *   1. Workers & Pages > KV > Create a namespace, for example "holdco-forms".
 *   2. Create a Worker and paste this file. Under Settings > Bindings, bind the KV namespace as SUBMISSIONS.
 *   3. Under Settings > Variables and Secrets add a Secret named SHARED_SECRET: a long random string (32 characters
 *      or more). Put the same value in the forms card's "Shared secret" in Holdco.
 *   4. Optional plain variables: THANKS_URL (where a browser is sent after posting the form; default: back to the
 *      page it came from with ?submitted=1) and ALLOWED_ORIGINS (comma-separated origins such as
 *      https://pawplan.example that may post with fetch() and read the answer).
 *   5. Point the form at the Worker. The hidden form-name names the form (Holdco records the source as
 *      forms:<form-name>); a consent checkbox named consent records consent, and the hidden consent=no before it
 *      makes an unticked box arrive as "no" (the last value of a repeated name wins):
 *
 *        <form method="POST" action="https://holdco-forms.your-name.workers.dev/">
 *          <input type="hidden" name="form-name" value="signup">
 *          <input name="name" autocomplete="name">
 *          <input type="email" name="email" required autocomplete="email">
 *          <input type="hidden" name="consent" value="no">
 *          <label><input type="checkbox" name="consent" value="yes"> Email me product news</label>
 *          <input name="bot-field" tabindex="-1" autocomplete="off" style="display:none">
 *          <button>Sign up</button>
 *        </form>
 *
 *   6. In Holdco, on the company's forms card: mode worker, Worker address = the Worker's URL, shared secret =
 *      SHARED_SECRET. Press Test, submit the form once, and the lead appears in Contacts within the hour.
 *
 * What it answers:
 *   POST /   A form post (urlencoded, multipart or JSON, at most 20 KB). Stored as {id, form, created_at, data}.
 *            A browser form is redirected (303) to THANKS_URL or back to the page; a fetch() that accepts JSON
 *            gets {"ok": true}. A filled-in bot-field (the honeypot) is answered the same way and not stored.
 *   GET /?form=<name>&after=<id>&since=<ISO time>&limit=<1-1000>   with the header X-Holdco-Secret: <SHARED_SECRET>
 *            {"submissions": [{id, form, created_at, data}, ...] oldest first, after the id and since the time,
 *             "total": every stored submission, "last_7_days": those of the last 7 days, "more": true when the
 *             limit cut the list}; with form, only that form's submissions are listed and counted
 *            Without the right secret: 401. Any other method: 405.
 *
 * Ids are "<13-digit milliseconds>-<8 hex characters>", so sorting them sorts by arrival, which is the order KV
 * lists keys in. Submissions stay in KV until you delete them there; Holdco keeps its own copy as contacts.
 */

const PREFIX = "sub:";
const MAX_BODY = 20 * 1024;
const MAX_FIELDS = 50;
const MAX_VALUE = 2000;
const DAY_MS = 24 * 60 * 60 * 1000;
const SKIP_FIELDS = new Set(["form-name", "bot-field", "g-recaptcha-response"]);

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (request.method === "OPTIONS") return preflight(request, env);
    if (request.method === "GET") return list(request, env, url);
    if (request.method === "POST") return submit(request, env);
    return json({ error: "method not allowed" }, 405);
  },
};

// ------------------------------------------------------------------ GET: Holdco reads the submissions

async function list(request, env, url) {
  if (!env.SHARED_SECRET || !(await sameSecret(request.headers.get("X-Holdco-Secret") || "", env.SHARED_SECRET))) {
    return json({ error: "unauthorized" }, 401);
  }
  const after = url.searchParams.get("after") || "";
  const sinceText = url.searchParams.get("since") || "";
  const since = sinceText ? Date.parse(sinceText) : 0;
  const limit = Math.max(1, Math.min(parseInt(url.searchParams.get("limit") || "100", 10) || 100, 1000));
  const form = (url.searchParams.get("form") || "").toLowerCase();

  const keys = (await allKeys(env)).filter((k) => !form || k.form === form);
  const weekAgo = Date.now() - 7 * DAY_MS;
  let lastWeek = 0;
  const wanted = [];
  for (const { name } of keys) {
    const id = name.slice(PREFIX.length);
    const ms = parseInt(id.slice(0, 13), 10) || 0;
    if (ms >= weekAgo) lastWeek += 1;
    if (after && id <= after) continue;
    if (since && ms < since) continue;
    wanted.push(name);
  }
  const page = wanted.slice(0, limit);
  const submissions = [];
  for (const name of page) {
    const value = await env.SUBMISSIONS.get(name, "json");
    if (value) submissions.push(value);
  }
  return json({ submissions, total: keys.length, last_7_days: lastWeek, more: wanted.length > page.length });
}

async function allKeys(env) {
  // Every stored key with its form (kept in the key's metadata, so counting needs no reads).
  const keys = [];
  let cursor;
  do {
    const page = await env.SUBMISSIONS.list({ prefix: PREFIX, cursor });
    for (const k of page.keys) keys.push({ name: k.name, form: String((k.metadata || {}).form || "") });
    cursor = page.list_complete ? undefined : page.cursor;
  } while (cursor);
  return keys.sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0));
}

async function sameSecret(given, expected) {
  // Compares digests, so the time taken does not depend on how much of the secret was right.
  const enc = new TextEncoder();
  const [a, b] = await Promise.all([crypto.subtle.digest("SHA-256", enc.encode(given)),
                                    crypto.subtle.digest("SHA-256", enc.encode(expected))]);
  const x = new Uint8Array(a), y = new Uint8Array(b);
  let diff = 0;
  for (let i = 0; i < x.length; i++) diff |= x[i] ^ y[i];
  return diff === 0;
}

// ------------------------------------------------------------------ POST: the site's form

async function submit(request, env) {
  const length = parseInt(request.headers.get("Content-Length") || "0", 10);
  if (length > MAX_BODY) return json({ error: "too large" }, 413);
  const buf = await request.arrayBuffer();
  if (buf.byteLength > MAX_BODY) return json({ error: "too large" }, 413);

  let fields;
  try {
    fields = await parse(request, buf);
  } catch (e) {
    return json({ error: "could not read the form" }, 400);
  }
  const accepted = done(request, env);
  if ((fields["bot-field"] || "").trim()) return accepted;   // the honeypot: say thanks, keep nothing

  const data = {};
  let count = 0;
  for (const [k, v] of Object.entries(fields)) {
    if (SKIP_FIELDS.has(k) || count >= MAX_FIELDS) continue;
    data[String(k).slice(0, 100)] = String(v).slice(0, MAX_VALUE);
    count += 1;
  }
  if (!count) return json({ error: "empty form" }, 400);

  const now = Date.now();
  const rand = crypto.getRandomValues(new Uint8Array(4));
  const id = String(now).padStart(13, "0") + "-" + [...rand].map((b) => b.toString(16).padStart(2, "0")).join("");
  const form = String(fields["form-name"] || "form").slice(0, 60);
  const record = { id, form, created_at: new Date(now).toISOString(), data };
  await env.SUBMISSIONS.put(PREFIX + id, JSON.stringify(record), { metadata: { form: form.toLowerCase() } });
  return accepted;
}

async function parse(request, buf) {
  const type = (request.headers.get("Content-Type") || "").toLowerCase();
  const raw = new TextDecoder().decode(buf);
  const out = {};
  if (type.includes("application/json")) {
    const body = JSON.parse(raw || "{}");
    if (body && typeof body === "object" && !Array.isArray(body)) {
      for (const [k, v] of Object.entries(body)) out[k] = Array.isArray(v) ? v.join(", ") : String(v ?? "");
    }
    return out;
  }
  if (type.includes("multipart/form-data")) {
    const form = await new Request(request.url, { method: "POST", headers: request.headers, body: buf }).formData();
    for (const [k, v] of form.entries()) if (typeof v === "string") out[k] = v;   // files are ignored
    return out;
  }
  for (const [k, v] of new URLSearchParams(raw)) out[k] = v;   // a repeated name keeps its last value
  return out;
}

function done(request, env) {
  const origin = request.headers.get("Origin") || "";
  if ((request.headers.get("Accept") || "").includes("application/json")) {
    return json({ ok: true }, 200, cors(origin, env));
  }
  let target = env.THANKS_URL || "";
  if (!target) {
    const back = request.headers.get("Referer") || "";
    try {
      const u = new URL(back);
      u.searchParams.set("submitted", "1");
      target = u.toString();
    } catch (e) {
      return new Response("Thank you, we got it.", { status: 200, headers: { "Content-Type": "text/plain" } });
    }
  }
  return Response.redirect(target, 303);
}

// ------------------------------------------------------------------ CORS for fetch() from the site

function allowed(origin, env) {
  const list = String(env.ALLOWED_ORIGINS || "").split(",").map((s) => s.trim()).filter(Boolean);
  return origin && list.includes(origin);
}

function cors(origin, env) {
  return allowed(origin, env) ? { "Access-Control-Allow-Origin": origin, "Vary": "Origin" } : {};
}

function preflight(request, env) {
  const origin = request.headers.get("Origin") || "";
  if (!allowed(origin, env)) return new Response(null, { status: 403 });
  return new Response(null, {
    status: 204,
    headers: { ...cors(origin, env), "Access-Control-Allow-Methods": "POST", "Access-Control-Allow-Headers":
      "Content-Type, Accept", "Access-Control-Max-Age": "86400" },
  });
}

function json(obj, status = 200, extra = {}) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "Content-Type": "application/json", "Cache-Control": "no-store", ...extra },
  });
}
