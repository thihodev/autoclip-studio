# AutoClip Studio

An open-source tool that automatically creates short-form videos (YouTube Shorts, TikTok, Facebook Reels, 16:9 YouTube) and publishes them to social networks. The AI runs on **the Claude account you already use for Claude Code**.

```
 Topic / Niche ──► Claude (Agent SDK) ──► MoneyPrinterTurbo ──► Postiz ──► TikTok / YouTube / Facebook / Instagram / ...
                   ideas, script,          stock footage,         channel OAuth,
                   search terms, caption,  TTS, subtitles,        post now / schedule
                   hashtags, SEO tags      music, compositing
```

## Built on

| Component | Project | Role |
|---|---|---|
| LLM | [Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk-python) | Writes scripts and suggests ideas. Reuses your Claude Code login (Pro/Max/Team). |
| Rendering | [MoneyPrinterTurbo](https://github.com/harry0703/MoneyPrinterTurbo) (MIT) | Pexels/Pixabay footage, edge-tts voice-over, subtitles, background music, MoviePy/FFmpeg compositing. |
| Publishing | [Postiz](https://github.com/gitroomhq/postiz-app) (AGPL-3.0) | Connects 20+ networks: TikTok, YouTube, Facebook, Instagram, Threads, X, LinkedIn... |
| Web UI / API | FastAPI + Alpine.js | Create videos, browse ideas, run autopilot. |

## Setup

Requirements: Python 3.11+, Docker, and [Claude Code](https://claude.com/claude-code) logged in.

```bash
# 1. Render + publishing engines (Docker)
./scripts/setup-engines.sh
#    - Edit vendor/MoneyPrinterTurbo/config.toml: pexels_api_keys = ["..."]  (free)
#    - Open http://localhost:4007, create a Postiz account, connect TikTok/YouTube/Facebook
#      (each platform needs its own developer app — see https://docs.postiz.com/providers)
#    - Postiz → Settings → Public API → copy the key

# 2. App
cp .env.example .env        # paste POSTIZ_API_KEY
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --port 8000
```

Open http://localhost:8000 — the three status dots in the header (Claude / MoneyPrinterTurbo / Postiz) should be green.

## Connecting your Claude subscription

The app calls Claude through the **Claude Agent SDK**, which runs the Claude Code CLI under the hood, so it authenticates exactly like Claude Code:

1. **Personal machine**: if you have run `claude` and logged in, there is nothing to configure.
2. **Server / Docker / no browser**: run `claude setup-token` on your machine and paste the token into `CLAUDE_CODE_OAUTH_TOKEN` in `.env`.
3. **API key instead of a subscription**: set `ANTHROPIC_API_KEY`.

**Using a different Claude account than the one logged in to your terminal:**

- Separate config directory: run `CLAUDE_CONFIG_DIR=~/.claude-autoclip claude`, log in with the second account, then set `CLAUDE_CONFIG_DIR=/Users/<you>/.claude-autoclip` in `.env`.
- Or run `claude setup-token`, approve with the second account in the browser (use a private window), and paste the result into `CLAUDE_CODE_OAUTH_TOKEN`. This does not overwrite your existing login.

Precedence: `ANTHROPIC_API_KEY` > `CLAUDE_CODE_OAUTH_TOKEN` > the login in `CLAUDE_CONFIG_DIR` (default `~/.claude`). Hover the **Claude** dot in the UI to see which account is in use.

> ⚠️ Use your subscription for **yourself only**. Since June 15, 2026, Agent SDK usage on subscription plans draws from a monthly "Agent SDK credit", separate from your chat limits. Don't share your login or token with other people; if you deploy this for several users, each one should use their own account or an API key. See the [authentication docs](https://code.claude.com/docs/en/authentication).

## Features

- **Create video**: enter a topic → Claude writes the script, caption and hashtags → render → preview → publish.
- **Ideas**: enter a niche and Claude suggests topics likely to go viral, each with an opening hook.
- **Autopilot**: every N hours, generate ideas → render → auto-publish to the selected channels.
- **Multi-channel publishing** in one call through Postiz; TikTok supports `DIRECT_POST` or `UPLOAD` (sent to the inbox for manual review).

## API

| Method | Path | |
|---|---|---|
| GET | `/api/status` | Claude / MPT / Postiz status |
| GET | `/api/integrations` | Channels connected in Postiz |
| POST | `/api/ideas` | `{niche, language, count}` |
| POST | `/api/projects` | `{topic, language, duration, format, style, voice, auto_publish, integrations}` |
| POST | `/api/projects/{id}/publish` | `{integrations, publish_options}` |
| GET/PUT | `/api/autopilot` | Autopilot settings |

`publish_options`: `youtube_visibility` (`public|unlisted|private`), `tiktok_privacy`, `tiktok_method` (`DIRECT_POST|UPLOAD`), `schedule_at` (ISO 8601).

## Platform notes

- **TikTok**: unaudited apps can only post as `SELF_ONLY` (private). To post publicly, submit your app for Content Posting API review.
- **YouTube**: new Google Cloud projects have a limited quota (~6 uploads/day), and videos uploaded by unverified apps are locked to private until the app passes an audit.
- **Facebook/Instagram**: requires a Meta app with `pages_manage_posts` / `instagram_content_publish` permissions.
- Label AI-generated content according to each platform's policy (the TikTok settings already set `video_made_with_ai`).

## Roadmap ideas

- More natural voices: [Kokoro TTS](https://github.com/hexgrad/kokoro), [F5-TTS](https://github.com/SWivid/F5-TTS) (voice cloning).
- AI-generated video instead of stock footage: ComfyUI + Wan 2.x / LTX-Video.
- Motion-graphics templates: [Remotion](https://github.com/remotion-dev/remotion) or Hyperframes.
- Clip long videos into shorts: [faster-whisper](https://github.com/SYSTRAN/faster-whisper) + Claude picking the best moments.

## License

[MIT](LICENSE). AutoClip Studio only talks to MoneyPrinterTurbo (MIT) and Postiz (AGPL-3.0) over their APIs and does not bundle their code. `scripts/setup-engines.sh` clones them into `vendor/` (not committed); each project keeps its own license.
