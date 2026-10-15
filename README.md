# AutoClip Studio

An open-source tool that creates short-form videos (YouTube Shorts, TikTok, Facebook Reels, 16:9 YouTube), lets you edit them by hand or with AI, and publishes them to your accounts. Everything is set up from the web UI: no `.env`, no Docker, no system ffmpeg.

```
 Topic / Niche ─► Claude ─► scenes ─► edge-tts voice + stock clips ─► ffmpeg render ─► edit loop ─► YouTube / TikTok / Facebook
                 (Agent SDK)          (word timings)   (Pexels/Pixabay)  (captions burned in)  │
                                                                                         manual edits,
                                                                                         "edit with AI",
                                                                                         FrameJam frame comments
```

## Quick start

```bash
git clone https://github.com/thihodev/autoclip-studio.git && cd autoclip-studio
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m app            # opens http://localhost:8000
```

The app opens on **Connections** until two things are set:

1. **Claude**: click **Sign in with Claude**, approve in the browser and paste the code back, or pick *Use this computer's Claude Code login*, or paste an Anthropic API key. The app never uses an account until you choose one.
2. **Stock footage**: paste a free [Pexels](https://www.pexels.com/api/) (or [Pixabay](https://pixabay.com/api/docs/)) API key.

Social accounts, FrameJam and Postiz are optional and can be connected at any time from the same tab.

## Built on

| Component | Project | Role |
|---|---|---|
| LLM | [Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk-python) | Ideas, scene-by-scene scripts, AI edits. Bundles the Claude Code CLI, so it signs in with your Claude subscription. |
| Voice | [edge-tts](https://github.com/rany2/edge-tts) | Natural voices in many languages, with word timings for captions. |
| Footage | Pexels / Pixabay APIs | Royalty-free stock clips per scene, swappable in the editor. |
| Render | ffmpeg via [imageio-ffmpeg](https://github.com/imageio/imageio-ffmpeg) | Crop, slow camera drift, word-by-word captions (libass), concat. Each scene is cached, so edits re-encode only what changed. |
| Review | [FrameJam](https://github.com/moritzkremb/framejam) (MIT) | Comment on exact frames; Claude applies the comments and renders the next version. |
| Publishing | YouTube Data API, TikTok Content Posting API, Facebook Graph API | Native OAuth from the UI. Optional [Postiz](https://github.com/gitroomhq/postiz-app) for more networks. |
| Captions font | [Be Vietnam Pro](https://github.com/bettergui/BeVietnamPro) (OFL) | Covers Latin scripts including Vietnamese. |
| Web UI / API | FastAPI + Alpine.js | |

## Features

- **Create**: topic → Claude splits the voice-over into scenes and picks a footage search for each → render.
- **Editor** (per project):
  - edit each scene's text and footage search, reorder, add or delete scenes;
  - **swap footage** from search results or **upload your own clip**;
  - caption style (bold with a highlighted word, box, minimal, none), colors, position, voice and speed, camera drift;
  - **Edit with AI**: describe the change ("punchier hook, shorten scene 3, captions at the top") and Claude edits the timeline;
  - **FrameJam review**: send the render to FrameJam, click frames and comment, then **Apply feedback**;
  - every render is kept as a version (v1, v2, …) you can preview and compare.
- **Ideas**: Claude suggests topics for a niche, each with an opening hook.
- **Autopilot**: every N hours, generate ideas → render → optionally auto-publish.
- **Publish** to YouTube, TikTok, Facebook Pages (and Postiz channels) in one click.

## Connecting Claude

| Option | When to use |
|---|---|
| This computer's Claude Code login | One click if `claude` is already logged in on this machine (the button shows which account). |
| **Sign in with Claude** (in the UI) | A separate login stored in `data/claude/`, so it can be a different account from your terminal. |
| Anthropic API key | Pay-as-you-go instead of a subscription. |

> ⚠️ Use your subscription for **yourself only**. Since June 15, 2026, Agent SDK usage on subscription plans draws from a monthly "Agent SDK credit", separate from your chat limits. Don't share your login with other people; for multi-user deployments, each user should connect their own account or use an API key. See the [authentication docs](https://code.claude.com/docs/en/authentication).

Web sign-in drives `claude auth login` in a pseudo-terminal, so it works on macOS and Linux (on Windows, use this computer's login or an API key).

## Connecting social accounts

Each platform requires your own developer app. The Connections tab shows, for each platform, the exact **redirect URI** to register and where to paste the client ID and secret. Then click **Connect** and approve on the platform's own consent screen.

| Platform | What to create | Notes |
|---|---|---|
| YouTube | Google Cloud project → YouTube Data API v3 → OAuth client (Web) | Unverified apps upload as private until Google audits the app; new projects have a small daily quota. |
| TikTok | developers.tiktok.com app with Login Kit + Content Posting API | Needs an **HTTPS** redirect URI: set *Public URL* to a tunnel (e.g. `cloudflared tunnel --url http://localhost:8000`). Unaudited apps can post only privately (`SELF_ONLY`); "Send to inbox" mode drops the video into TikTok drafts. |
| Facebook | Meta app (Business) with Facebook Login for Business | Permissions `pages_show_list`, `pages_read_engagement`, `pages_manage_posts`. Every Page you manage becomes an account; vertical videos are posted as Reels. |
| Others | [Postiz](https://github.com/gitroomhq/postiz-app) URL + API key | Instagram, LinkedIn, X, Threads, Pinterest… |

AI-generated content is labeled where the platform supports it (`containsSyntheticMedia` on YouTube, `is_aigc` on TikTok).

## FrameJam review loop

```bash
npx -y framejam start     # Node 22+, runs at http://localhost:2400
```

In the editor click **Open in FrameJam**, comment on frames in the review page, then **Apply feedback**. Claude maps each comment's timestamp to a scene, edits the timeline and renders a new version, which is pushed to the same FrameJam review automatically.

## Configuration

Everything user-facing lives in the UI and is stored in `data/autoclip.db`. Optional environment variables (see `.env.example`): `HOST`, `PORT`, `DATA_DIR`. Run with `python -m app --no-browser` on a server.

> The app has no login of its own. Keep it on `127.0.0.1` (the default), or put it behind your own authentication before exposing it.

## API

| Method | Path | |
|---|---|---|
| GET | `/api/status` | Claude / footage / accounts / FrameJam / Postiz status |
| GET, PUT | `/api/settings`, `/api/settings/{section}` | Connections settings (secrets are masked on read) |
| GET, POST | `/api/claude`, `/api/claude/login/{start,code,cancel}`, `/api/claude/{source,logout}` | Claude connection |
| GET | `/oauth/{youtube,tiktok,facebook}/start` | Connect a social account |
| GET | `/api/channels` | Connected accounts + Postiz channels |
| POST | `/api/ideas` | `{niche, language, count}` |
| GET, POST | `/api/projects` | List / create `{topic, language, duration, format, style, voice, voice_rate, auto_publish, integrations}` |
| PUT | `/api/projects/{id}/timeline` | Save a manual edit and re-render |
| POST | `/api/projects/{id}/ai-edit` | `{instruction}` |
| GET | `/api/projects/{id}/footage?q=` | Search stock footage |
| POST | `/api/projects/{id}/upload` | Upload your own clip (multipart `file`) |
| POST | `/api/projects/{id}/framejam`, `/api/projects/{id}/framejam/apply` | FrameJam review loop |
| POST | `/api/projects/{id}/publish` | `{integrations, publish_options}` |
| GET, PUT | `/api/autopilot` | Autopilot settings |

`publish_options`: `youtube_visibility` (`public|unlisted|private`), `tiktok_method` (`DIRECT_POST|UPLOAD`), `tiktok_privacy`, `schedule_at` (Postiz only).

## Roadmap ideas

- Background music library and ducking.
- More voices: [Kokoro TTS](https://github.com/hexgrad/kokoro), [F5-TTS](https://github.com/SWivid/F5-TTS) (voice cloning).
- AI-generated shots instead of stock footage: ComfyUI + Wan 2.x / LTX-Video.
- Clip long videos into shorts with [faster-whisper](https://github.com/SYSTRAN/faster-whisper) + Claude picking the best moments.
- Fonts for non-Latin scripts (CJK, Thai, Devanagari).

## License

[MIT](LICENSE). The bundled Be Vietnam Pro font is under the [SIL Open Font License](app/assets/fonts/OFL.txt). FrameJam and Postiz are separate projects used over their APIs and keep their own licenses.
