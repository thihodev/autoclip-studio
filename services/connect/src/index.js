// AutoClip Connect — the one part of AutoClip that has to run on a server.
//
// Posting to TikTok needs a developer app whose client secret must stay secret, and a fixed HTTPS
// redirect URI. This Worker holds that app so AutoClip users only click "Connect TikTok":
//
//   AutoClip (on the user's computer)  →  /tiktok/start  →  TikTok consent  →  /tiktok/callback
//   → the tokens, encrypted for that AutoClip only (ECDH P-256 + AES-GCM), go back to the user's computer.
//
// Nothing is stored here: no tokens, no videos, no accounts. Videos upload straight from the user's
// computer to TikTok with the user's own token; AutoClip comes back only to refresh that token.

const TIKTOK_AUTH = "https://www.tiktok.com/v2/auth/authorize/";
const TIKTOK_TOKEN = "https://open.tiktokapis.com/v2/oauth/token/";
const TIKTOK_SCOPES = "user.info.basic,video.publish,video.upload";
const SEAL_INFO = "autoclip-connect v1";

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    try {
      switch (url.pathname) {
        case "/": return page("AutoClip Connect", home(env));
        case "/privacy": return page("Privacy policy · AutoClip Connect", privacy(env));
        case "/terms": return page("Terms of service · AutoClip Connect", terms(env));
        case "/health": return json({ ok: true, tiktok: Boolean(env.TIKTOK_CLIENT_KEY && env.TIKTOK_CLIENT_SECRET) });
        case "/tiktok/start": return tiktokStart(url, env);
        case "/tiktok/callback": return await tiktokCallback(url, env);
        case "/tiktok/refresh":
          if (request.method !== "POST") return json({ error: "POST only" }, 405);
          return await tiktokRefresh(request, env);
        default: {
          // TikTok's URL-prefix verification: /tiktok<code>.txt for each code in TIKTOK_SITE_VERIFICATION.
          const m = url.pathname.match(/^\/tiktok([A-Za-z0-9]+)\.txt$/);
          const codes = String(env.TIKTOK_SITE_VERIFICATION || "").split(",").map((c) => c.trim());
          if (m && codes.includes(m[1])) {
            return new Response(`tiktok-developers-site-verification=${m[1]}`, { headers: { "Content-Type": "text/plain" } });
          }
          return new Response("Not found", { status: 404 });
        }
      }
    } catch (e) {
      return page("Something went wrong", `<h1>Something went wrong</h1><p>${esc(e.message || e)}</p>`, 500);
    }
  },
};

// ------------------------------------------------------------------ TikTok

function tiktokStart(url, env) {
  if (!env.TIKTOK_CLIENT_KEY) return page("Not set up", "<h1>TikTok isn't set up on this server yet.</h1>", 503);
  const ret = url.searchParams.get("return") || "";
  const key = url.searchParams.get("key") || "";
  const state = url.searchParams.get("state") || "";
  let target;
  try { target = new URL(ret); } catch { return bad("Missing or invalid return address."); }
  if (!/^https?:$/.test(target.protocol) || target.search || target.hash) return bad("Invalid return address.");
  if (!/^[A-Za-z0-9_-]{80,100}$/.test(key) || !/^[A-Za-z0-9_-]{8,128}$/.test(state)) return bad("Invalid request.");
  // The connection is sent back to `return`. On the user's own computer or network that's safe; anywhere else,
  // ask first, so a link someone else crafted can't quietly send your TikTok connection to their server.
  if (!isPrivateHost(target.hostname) && url.searchParams.get("confirm") !== "1") {
    const next = new URL(url); next.searchParams.set("confirm", "1");
    return page("Connect TikTok · AutoClip", `<h1>Connect TikTok to AutoClip at ${esc(target.host)}?</h1>
      <p>Only continue if <b>${esc(target.host)}</b> is your own AutoClip Studio. Your TikTok connection will be sent there.</p>
      <p><a class="btn" href="${esc(next)}">Continue</a></p>`);
  }
  const packed = b64u(enc(JSON.stringify({ r: ret, k: key, s: state })));
  return Response.redirect(TIKTOK_AUTH + "?" + new URLSearchParams({
    client_key: env.TIKTOK_CLIENT_KEY, scope: TIKTOK_SCOPES, response_type: "code",
    redirect_uri: `${url.origin}/tiktok/callback`, state: packed,
  }), 302);
}

async function tiktokCallback(url, env) {
  let st;
  try { st = JSON.parse(dec(b64d(url.searchParams.get("state") || ""))); } catch { return bad("This sign-in link is invalid. Start again from AutoClip."); }
  const back = (params) => Response.redirect(st.r + "?" + new URLSearchParams({ state: st.s, ...params }), 302);
  if (url.searchParams.get("error")) {
    return back({ error: url.searchParams.get("error_description") || url.searchParams.get("error") });
  }
  const tok = await tiktokToken(env, {
    grant_type: "authorization_code", code: url.searchParams.get("code") || "", redirect_uri: `${url.origin}/tiktok/callback`,
  });
  if (!tok.access_token) return back({ error: tok.error_description || tok.error || "TikTok didn't return a token" });
  return back({ sealed: await seal(tok, st.k) });
}

async function tiktokRefresh(request, env) {
  let body;
  try { body = await request.json(); } catch { return json({ error: "JSON body expected" }, 400); }
  if (!body.refresh_token) return json({ error: "refresh_token is required" }, 400);
  const tok = await tiktokToken(env, { grant_type: "refresh_token", refresh_token: String(body.refresh_token) });
  return json(tok, tok.access_token ? 200 : 400);
}

async function tiktokToken(env, params) {
  const r = await fetch(TIKTOK_TOKEN, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded", "Cache-Control": "no-cache" },
    body: new URLSearchParams({ client_key: env.TIKTOK_CLIENT_KEY, client_secret: env.TIKTOK_CLIENT_SECRET, ...params }),
  });
  return r.json().catch(() => ({ error: `TikTok answered ${r.status}` }));
}

// ------------------------------------------------------------------ sealing

// Encrypt for the AutoClip that started the sign-in: ECDH (P-256) with its one-time public key,
// HKDF-SHA256 → AES-256-GCM. Output: ephemeral public key (65 bytes) | IV (12) | ciphertext.
export async function seal(obj, peerKeyB64u) {
  const peer = await crypto.subtle.importKey("raw", b64d(peerKeyB64u), { name: "ECDH", namedCurve: "P-256" }, false, []);
  const eph = await crypto.subtle.generateKey({ name: "ECDH", namedCurve: "P-256" }, true, ["deriveBits"]);
  const ephRaw = new Uint8Array(await crypto.subtle.exportKey("raw", eph.publicKey));
  const shared = await crypto.subtle.deriveBits({ name: "ECDH", public: peer }, eph.privateKey, 256);
  const hkdf = await crypto.subtle.importKey("raw", shared, "HKDF", false, ["deriveKey"]);
  const key = await crypto.subtle.deriveKey({ name: "HKDF", hash: "SHA-256", salt: ephRaw, info: enc(SEAL_INFO) },
    hkdf, { name: "AES-GCM", length: 256 }, false, ["encrypt"]);
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const ct = new Uint8Array(await crypto.subtle.encrypt({ name: "AES-GCM", iv }, key, enc(JSON.stringify(obj))));
  const out = new Uint8Array(ephRaw.length + iv.length + ct.length);
  out.set(ephRaw); out.set(iv, ephRaw.length); out.set(ct, ephRaw.length + iv.length);
  return b64u(out);
}

// ------------------------------------------------------------------ helpers

function isPrivateHost(h) {
  h = h.replace(/^\[|\]$/g, "").toLowerCase();
  if (h === "localhost" || h.endsWith(".localhost") || h.endsWith(".local") || h === "::1") return true;
  const m = h.match(/^(\d+)\.(\d+)\.\d+\.\d+$/);
  if (!m) return false;
  const [a, b] = [Number(m[1]), Number(m[2])];
  return a === 127 || a === 10 || (a === 192 && b === 168) || (a === 172 && b >= 16 && b <= 31) || (a === 100 && b >= 64 && b <= 127);
}

const enc = (s) => new TextEncoder().encode(s);
const dec = (b) => new TextDecoder().decode(b);
function b64u(bytes) { let s = ""; for (const x of bytes) s += String.fromCharCode(x); return btoa(s).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, ""); }
function b64d(s) { const t = atob(s.replace(/-/g, "+").replace(/_/g, "/") + "===".slice((s.length + 3) % 4)); return Uint8Array.from(t, (c) => c.charCodeAt(0)); }
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const json = (o, status = 200) => new Response(JSON.stringify(o), { status, headers: { "Content-Type": "application/json", "Cache-Control": "no-store" } });
const bad = (msg) => page("Can't connect", `<h1>Can't connect</h1><p>${esc(msg)}</p>`, 400);

function page(title, body, status = 200) {
  return new Response(`<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>${esc(title)}</title><link rel="icon" href="/favicon.png">
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=Inter+Tight:wght@600;700&family=Instrument+Serif:ital@1&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<link rel="stylesheet" href="/site.css"></head><body>
<header class="nav scrolled"><div class="wrap"><a class="logo" href="/"><span class="mark">AC</span><span>Auto<b>Clip</b> Studio</span></a>
<div class="right"><a class="btn ghost sm" href="/">Home</a></div></div></header>
<main class="wrap"><div class="legal">${body}</div></main>
<footer style="margin-top:60px"><div class="wrap"><a class="logo" href="/"><span class="mark">AC</span><span>Auto<b>Clip</b> Studio</span></a>
<nav><a href="/">Home</a><a href="/privacy">Privacy</a><a href="/terms">Terms</a><a href="https://github.com/thihodev/autoclip-studio">GitHub</a></nav>
<span>© 2026 Ho Van Thi (@thihodev) · MIT</span></div></footer></body></html>`,
    { status, headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store" } });
}

const operator = (env) => esc(env.OPERATOR_NAME || "the operator of this service");
const contact = (env) => env.CONTACT_EMAIL ? `<a href="mailto:${esc(env.CONTACT_EMAIL)}">${esc(env.CONTACT_EMAIL)}</a>` : "the contact listed on GitHub";

function home(env) {
  return `<h1>AutoClip Connect</h1>
<p>AutoClip Studio is an open-source app that turns a topic into a short video with Claude. It runs on your own computer.
AutoClip Connect lets it publish to TikTok without you creating a developer app: you sign in to TikTok, approve,
and your videos post from your own computer with your own account.</p>
<p>This server only completes the sign-in and renews your access. It keeps no tokens, videos or personal data.</p>
<p class="muted">Operated by ${operator(env)}.</p>`;
}

function privacy(env) {
  return `<div class="eyebrow">AutoClip Connect</div><h1>Privacy policy</h1>
<p class="meta">AutoClip Connect, operated by ${operator(env)}. Contact: ${contact(env)}. Last updated October 16, 2026.</p>
<h2>What this service does</h2>
<p>When you connect TikTok in AutoClip Studio, your browser visits this service, which sends you to TikTok to sign in
and approve access. TikTok returns an authorization code here; the service exchanges it for access and refresh tokens,
encrypts them so only your AutoClip Studio can read them, and sends them back to it. Later, your AutoClip Studio asks
this service to renew the access token.</p>
<h2>What we store</h2>
<p>Nothing. Tokens are passed through in memory and are not saved, logged or shared. Videos never pass through this
service: AutoClip Studio uploads them from your computer directly to TikTok. Our hosting provider (Cloudflare) may keep
standard request logs such as IP addresses and timestamps for security.</p>
<h2>TikTok data</h2>
<p>With your permission, AutoClip Studio reads your TikTok profile name and picture (to show which account is connected)
and publishes the videos you choose. Those tokens and that data stay on your own computer.</p>
<h2>Removing access</h2>
<p>Disconnect the account in AutoClip Studio (Connections) and revoke AutoClip in TikTok's settings
(Settings and privacy → Security → Manage app permissions). Since we keep nothing, there is nothing to delete on our side.</p>
<h2>Changes</h2><p>Changes to this policy are published on this page and in the source code on GitHub.</p>`;
}

function terms(env) {
  return `<div class="eyebrow">AutoClip Connect</div><h1>Terms of service</h1>
<p class="meta">AutoClip Connect, operated by ${operator(env)}. Contact: ${contact(env)}. Last updated October 16, 2026.</p>
<p>AutoClip Connect is a free service, provided as is, without warranty, that connects AutoClip Studio to TikTok.</p>
<ul>
<li>You are responsible for what you publish and must follow TikTok's Terms of Service and Community Guidelines,
including labeling AI-generated content.</li>
<li>Don't use the service for spam, automated mass posting, or accounts you don't own or manage.</li>
<li>We may limit or stop the service, or block abuse, at any time.</li>
<li>AutoClip Studio is open source under the MIT License; this service's code is part of it.</li>
</ul>`;
}
