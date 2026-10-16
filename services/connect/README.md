# AutoClip Connect

The one part of AutoClip that runs on a server. It holds a TikTok developer app so AutoClip users can
connect TikTok with one click instead of creating their own developer app.

It only completes the sign-in and renews tokens. Tokens go back to the user's AutoClip encrypted for it
(ECDH P-256 + AES-GCM with a one-time key AutoClip generates), nothing is stored, and videos upload from
the user's computer straight to TikTok. It runs free on Cloudflare Workers.

## Endpoints

| | |
|---|---|
| `GET /tiktok/start?return=…&key=…&state=…` | AutoClip sends the browser here; redirects to TikTok's consent screen. A `return` outside the user's computer/network asks for confirmation first. |
| `GET /tiktok/callback` | TikTok's redirect URI. Exchanges the code and sends the sealed tokens back to `return?state=…&sealed=…`. |
| `POST /tiktok/refresh` `{refresh_token}` | Renews an access token (TikTok access tokens last 24 hours). |
| `GET /`, `/privacy`, `/terms`, `/health` | Pages TikTok's app review asks for, and a health check. |

## Deploy (operator)

1. `npx wrangler login`, then `npx wrangler deploy` in this folder. Note the URL, e.g. `https://autoclip-connect.<you>.workers.dev`.
2. On [developers.tiktok.com](https://developers.tiktok.com): create an app, add **Login Kit** and **Content Posting API**,
   scopes `user.info.basic`, `video.upload`, `video.publish`; redirect URI `https://<worker>/tiktok/callback`;
   website, terms and privacy URLs `https://<worker>/`, `/terms`, `/privacy`.
3. `npx wrangler secret put TIKTOK_CLIENT_KEY` and `TIKTOK_CLIENT_SECRET` with the app's keys. Set `CONTACT_EMAIL` in `wrangler.toml` and redeploy.
4. Put the Worker URL in `app/config.py` (`CONNECT_URL`) so every AutoClip uses it.
5. Until TikTok audits the app, posts can only be private (`SELF_ONLY`) and the app works for the test users you add.
   Submit the app for review (a short screen recording of connecting and posting from AutoClip) to lift that.

## Test

`node --test test/flow.test.mjs` runs the flow against a mocked TikTok, and `../../.venv/bin/python test/roundtrip.py` checks that
AutoClip can open what the Worker seals.
