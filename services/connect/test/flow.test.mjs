// The sign-in flow against a mocked TikTok: node --test test/
import assert from "node:assert/strict";
import { test } from "node:test";
import worker from "../src/index.js";

const env = { TIKTOK_CLIENT_KEY: "ck_test", TIKTOK_CLIENT_SECRET: "cs_test", OPERATOR_NAME: "Test" };
// The one-time public key AutoClip would send (the round-trip test passes a real one from Python).
const KEY = process.env.TEST_PUBLIC_KEY || await (async () => {
  const k = await crypto.subtle.generateKey({ name: "ECDH", namedCurve: "P-256" }, true, ["deriveBits"]);
  return Buffer.from(await crypto.subtle.exportKey("raw", k.publicKey)).toString("base64url");
})();
const calls = [];
globalThis.fetch = async (url, init) => {
  calls.push({ url: String(url), body: String(init.body) });
  const p = new URLSearchParams(String(init.body));
  if (p.get("grant_type") === "authorization_code" && p.get("code") === "good")
    return new Response(JSON.stringify({ access_token: "act.1", refresh_token: "rft.1", open_id: "u1", expires_in: 86400 }));
  if (p.get("grant_type") === "refresh_token") return new Response(JSON.stringify({ access_token: "act.2", expires_in: 86400 }));
  return new Response(JSON.stringify({ error: "invalid_grant" }), { status: 400 });
};
const get = (u) => worker.fetch(new Request(u), env);

test("start sends a local AutoClip to TikTok's consent screen", async () => {
  const r = await get(`https://c.test/tiktok/start?return=${encodeURIComponent("http://localhost:8000/oauth/tiktok/callback")}&key=${KEY}&state=abcdefgh`);
  assert.equal(r.status, 302);
  const to = new URL(r.headers.get("Location"));
  assert.equal(to.origin + to.pathname, "https://www.tiktok.com/v2/auth/authorize/");
  assert.equal(to.searchParams.get("client_key"), "ck_test");
  assert.equal(to.searchParams.get("redirect_uri"), "https://c.test/tiktok/callback");
});

test("a public return address asks for confirmation first", async () => {
  const r = await get(`https://c.test/tiktok/start?return=${encodeURIComponent("https://evil.example/cb")}&key=${KEY}&state=abcdefgh`);
  assert.equal(r.status, 200);
  assert.match(await r.text(), /Only continue if/);
});

test("bad input is rejected", async () => {
  assert.equal((await get(`https://c.test/tiktok/start?return=x&key=${KEY}&state=abcdefgh`)).status, 400);
  assert.equal((await get(`https://c.test/tiktok/start?return=http://localhost/cb&key=short&state=abcdefgh`)).status, 400);
});

test("callback seals the tokens for the caller and sends them back", async () => {
  const start = await get(`https://c.test/tiktok/start?return=${encodeURIComponent("http://192.168.1.5:8000/oauth/tiktok/callback")}&key=${KEY}&state=st123456`);
  const state = new URL(start.headers.get("Location")).searchParams.get("state");
  const r = await get(`https://c.test/tiktok/callback?code=good&state=${state}`);
  const back = new URL(r.headers.get("Location"));
  assert.equal(back.origin + back.pathname, "http://192.168.1.5:8000/oauth/tiktok/callback");
  assert.equal(back.searchParams.get("state"), "st123456");
  const sealed = back.searchParams.get("sealed");
  assert.ok(sealed && !sealed.includes("act.1"));
  assert.match(calls.at(-1).body, /client_secret=cs_test/);
  if (process.env.SEALED_OUT) (await import("node:fs")).writeFileSync(process.env.SEALED_OUT, sealed);
});

test("a TikTok error goes back to AutoClip", async () => {
  const start = await get(`https://c.test/tiktok/start?return=${encodeURIComponent("http://localhost:8000/cb")}&key=${KEY}&state=st123456`);
  const state = new URL(start.headers.get("Location")).searchParams.get("state");
  const back = new URL((await get(`https://c.test/tiktok/callback?error=access_denied&state=${state}`)).headers.get("Location"));
  assert.equal(back.searchParams.get("error"), "access_denied");
});

test("refresh renews with the client secret", async () => {
  const r = await worker.fetch(new Request("https://c.test/tiktok/refresh", { method: "POST", body: JSON.stringify({ refresh_token: "rft.1" }) }), env);
  assert.equal((await r.json()).access_token, "act.2");
  assert.equal((await get("https://c.test/tiktok/refresh")).status, 405);
});

test("pages for TikTok's review exist", async () => {
  for (const p of ["/", "/privacy", "/terms"]) assert.equal((await get("https://c.test" + p)).status, 200);
  assert.deepEqual(await (await get("https://c.test/health")).json(), { ok: true, tiktok: true });
});
