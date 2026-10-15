# AutoClip Studio

An open-source tool that creates short-form videos (YouTube Shorts, TikTok, Facebook Reels, 16:9 YouTube), lets you edit them by hand or with AI, and publishes them to your accounts. Everything is set up from the web UI: no `.env`, no Docker.

By default videos are made with **Claude Motion**: like Anthropic's [Claude Motion](https://support.claude.com/en/articles/17454997-get-started-with-claude-motion), Claude designs and animates every scene as code — kinetic typography, diagrams, charts, shapes and transitions — here rendered locally with [HyperFrames](https://github.com/heygen-com/hyperframes). A **Quick** engine (voice-over and captions over stock clips or animated backgrounds) is there when you need a video in a minute.

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
./start.sh          # Windows: start.cmd
```

That's the only command. The first run creates `.venv` and installs dependencies (with [uv](https://github.com/astral-sh/uv) if it's installed, otherwise pip), then opens http://localhost:8000. After `git pull`, run `./start.sh` again — it reinstalls only when `requirements.txt` changed. Use `./start.sh --no-browser` on a server. Requires Python 3.11+.

The only required step is **Claude** (Connections → Claude): click **Sign in with Claude**, approve in the browser and paste the code back, or pick *Use this computer's Claude Code login*, or paste an Anthropic API key. The app never uses an account until you choose one.

Everything else is optional and can be connected at any time from the same tab:

- **Voice-over**: an [ElevenLabs](https://elevenlabs.io/app/settings/api-keys) API key for natural voices (pick the voice and listen in Connections). Without it, free Microsoft voices are used.
- **Claude Motion**: click **Install** once (downloads HyperFrames, its skills and Chrome). Requires Node.js 22+ and FFmpeg.
- **Stock footage**: a free [Pixabay](https://pixabay.com/api/docs/) key (or an existing Pexels key — Pexels has paused new keys) adds real video clips behind each scene. Without one, scenes use animated gradient backgrounds in your brand colors, and you can still upload your own clips or use brand b-roll.
- Social accounts, GitHub, FrameJam and Postiz.

## Engines

| | Claude Motion (default) | Quick |
|---|---|---|
| How | A Claude agent runs HyperFrames' `faceless-explainer` workflow: picks a design preset, writes the storyboard and script, records the voice-over, animates each scene in its own sub-agent (HTML + GSAP), checks every frame, renders | Claude writes scenes; voice-over + word-by-word captions over stock clips, brand b-roll or animated backgrounds; ffmpeg |
| Time | ~10–20 min per video | ~1 min |
| Editing | Describe changes ("make scene 3 a bar chart"); Claude re-animates; FrameJam comments | Scene-by-scene editor, footage swap, caption styles, AI edits |
| Needs | Node.js 22+, FFmpeg with ffprobe; HyperFrames + Chrome are installed from Connections (one click) | Nothing extra |

Claude Motion uses noticeably more of your Claude plan per video than the Quick engine.

HyperFrames' own skill installer writes into your global agent folders (`~/.claude/skills`, `~/.agents/skills`, …). AutoClip never lets it: it installs the skills into `data/hyperframes/` with an app-owned `HOME` and links them into each video project, and the agent runs with a command allow/deny policy, file writes limited to the project folder, and no web access.

## Built on

| Component | Project | Role |
|---|---|---|
| LLM | [Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk-python) | Ideas, scene-by-scene scripts, AI edits. Bundles the Claude Code CLI, so it signs in with your Claude subscription. |
| Voice | [ElevenLabs](https://elevenlabs.io) (optional) · [edge-tts](https://github.com/rany2/edge-tts) | ElevenLabs v4/v3 voices in 70+ languages including Vietnamese, with character timestamps for captions; free Microsoft voices otherwise. |
| Motion graphics | [HyperFrames](https://github.com/heygen-com/hyperframes) (Apache-2.0) | HTML + GSAP compositions rendered to MP4 in headless Chrome; agent skills for design presets, animation blueprints and checks. |
| Footage | Pixabay / Pexels APIs (optional) | Royalty-free stock clips per scene, swappable in the editor. Falls back to animated gradient backgrounds rendered by ffmpeg. |
| Render | ffmpeg via [imageio-ffmpeg](https://github.com/imageio/imageio-ffmpeg) | Crop, slow camera drift, word-by-word captions (libass), concat. Each scene is cached, so edits re-encode only what changed. |
| Review | [FrameJam](https://github.com/moritzkremb/framejam) (MIT) | Comment on exact frames; Claude applies the comments and renders the next version. |
| Publishing | YouTube Data API, TikTok Content Posting API, Facebook Graph API | Native OAuth from the UI. Optional [Postiz](https://github.com/gitroomhq/postiz-app) for more networks. |
| Captions font | [Be Vietnam Pro](https://github.com/bettergui/BeVietnamPro) (OFL) | Default caption font; covers Latin scripts including Vietnamese. Brand kits can bring their own. |
| Web UI / API | FastAPI + Alpine.js | |

## Features

- **Interface in English or Vietnamese**, light or dark theme (switch in the sidebar; follows your browser and system by default). Translations live in `static/locales/<lang>.json` — add a file to contribute a language.

- **Create**: topic → Claude splits the voice-over into scenes and picks a footage search for each → render.
- **Editor** (per project):
  - edit each scene's text and footage search, reorder, add or delete scenes;
  - **swap footage** from search results or **upload your own clip**;
  - caption style (bold with a highlighted word, box, minimal, none), colors, position, voice and speed, camera drift;
  - **Edit with AI**: describe the change ("punchier hook, shorten scene 3, captions at the top") and Claude edits the timeline;
  - **FrameJam review**: send the render to FrameJam, click frames and comment, then **Apply feedback**;
  - every render is kept as a version (v1, v2, …) you can preview and compare.
- **Context files** (optional): attach articles, notes, product docs or transcripts (TXT, MD, PDF, DOCX, HTML, CSV, JSON, SRT/VTT) when creating a video, or later in the editor. Claude uses them as source material for the script and for AI edits.
- **Brand kits**: a design system for videos — see below.
- **Ideas**: Claude suggests topics for a niche, each with an opening hook.
- **Autopilot**: every N hours, generate ideas → render → optionally auto-publish.
- **Publish** to YouTube, TikTok, Facebook Pages (and Postiz channels) in one click.

## Brand kits (a design system for videos)

A brand kit bundles everything that makes videos look and sound like yours:

| Part | Used for |
|---|---|
| Description, voice & tone, audience, call to action | Claude follows them when writing and editing scripts. |
| Knowledge files | Product facts, FAQs and docs Claude stays consistent with in every video for the brand. |
| Colors, caption style and position, font (TTF/OTF) | Burned-in captions; the highlight color marks the spoken word. |
| Logo (PNG/JPG/WebP) | Overlaid on every scene, with corner, size and opacity settings. |
| Intro / outro (≤ 30 s) | Played before and after every video, conformed to the video format. |
| Music | Mixed under the voice, ducked automatically while someone speaks, faded out at the end. |
| B-roll library | Your own clips with descriptions. Claude picks them for matching scenes before stock footage; you can also pick them in the editor. |

Pick a kit when creating a video (Studio), in the editor (Style → Brand kit, with per-video toggles for logo, intro, outro and music) or for Autopilot.

### Import from GitHub

**Brand kits → Import from GitHub** accepts `owner/repo` or a GitHub URL (optionally a branch or tag). Public repos work as they are; for private repos, connect GitHub in Connections with a fine-grained token that has read-only *Contents* access.

- If the repo contains **`autoclip.brand.json`** (it can also list `context` documents as brand knowledge) (at the root, `.autoclip/brand.json` or `brand/autoclip.brand.json`), it is applied exactly. See [`examples/autoclip.brand.json`](examples/autoclip.brand.json); asset paths are relative to the repo root.
- Otherwise Claude reads the README, `package.json`, Tailwind config, CSS variables and design-token files, and picks the logo, font, music and clips from the repo's files. The README is added as brand knowledge.

**Sync from GitHub** on a kit re-imports it from the same repo and ref. SVG logos and WOFF fonts can't be rendered by ffmpeg; the import warns you to upload a PNG or a TTF/OTF instead.

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

Everything user-facing lives in the UI and is stored in `data/autoclip.db`. Optional environment variables (see `.env.example`): `HOST`, `PORT`, `DATA_DIR`. Run with `./start.sh --no-browser` on a server.

> The app has no login of its own. Keep it on `127.0.0.1` (the default), or put it behind your own authentication before exposing it.

## API

| Method | Path | |
|---|---|---|
| GET | `/api/status` | Claude / footage / accounts / FrameJam / Postiz status |
| GET, PUT | `/api/settings`, `/api/settings/{section}` | Connections settings (secrets are masked on read) |
| GET, POST | `/api/claude`, `/api/claude/login/{start,code,cancel}`, `/api/claude/{source,logout}` | Claude connection |
| GET | `/oauth/{youtube,tiktok,facebook}/start` | Connect a social account |
| GET | `/api/channels` | Connected accounts + Postiz channels |
| GET | `/api/voices?language=` | edge-tts voices for a language |
| POST, DELETE | `/api/context`, `/api/context/{id}` | Upload a context file (multipart `file`) before creating a video; pass the ids as `context_ids` |
| POST, DELETE | `/api/projects/{id}/context`, `/api/projects/{id}/context/{cid}` | Add / remove context files of a video |
| POST | `/api/ideas` | `{niche, language, count}` |
| GET, POST | `/api/projects` | List / create `{topic, language, duration, format, style, voice, voice_rate, brand_id, context_ids, auto_publish, integrations}` |
| PUT | `/api/projects/{id}/timeline` | Save a manual edit and re-render |
| POST | `/api/projects/{id}/ai-edit` | `{instruction}` |
| GET | `/api/projects/{id}/footage?q=` | Search stock footage |
| POST | `/api/projects/{id}/upload` | Upload your own clip (multipart `file`) |
| POST | `/api/projects/{id}/framejam`, `/api/projects/{id}/framejam/apply` | FrameJam review loop |
| POST | `/api/projects/{id}/publish` | `{integrations, publish_options}` |
| GET, POST | `/api/brands` | List / create brand kits |
| GET, PUT, DELETE | `/api/brands/{id}` | Read / update / delete a kit |
| POST, DELETE | `/api/brands/{id}/assets`, `/api/brands/{id}/assets/{kind}` | Upload (multipart `kind`, `file`) or remove logo, font, intro, outro, music, clip, context |
| POST | `/api/brands/import`, `/api/brands/{id}/sync` | Import / re-sync from GitHub `{url, ref}` |
| GET, POST, DELETE | `/api/github`, `/api/github/repos` | GitHub connection and repo list |
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
