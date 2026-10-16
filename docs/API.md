# AutoClip Studio API

The web UI uses this JSON API; you can script it too. Everything runs on the local server (default `http://127.0.0.1:8000`) with no authentication of its own — keep it local or put it behind your own auth.

## Setup and connections

| Method | Path | |
|---|---|---|
| GET | `/api/status` | Claude / footage / voice / Claude Motion / accounts / Postiz status |
| GET, PUT | `/api/settings`, `/api/settings/{section}` | Connections settings (secrets are masked on read) |
| GET, POST | `/api/claude`, `/api/claude/login/{start,code,cancel}`, `/api/claude/{source,logout}` | Claude connection |
| POST, DELETE | `/api/claude/api-key` | Check and save an Anthropic API key `{key, use}` / remove it |
| GET | `/api/claude/plan-usage?refresh=` | Plan limits: 5-hour session and weekly windows with reset times (cached 60 s) |
| GET | `/api/models` | Models, effort levels and the defaults (scripts, Claude Motion, scene designers) |
| GET | `/oauth/{youtube,tiktok,facebook}/start` | Connect a social account |
| GET | `/api/channels` | Connected accounts + Postiz channels |
| DELETE | `/api/accounts/{id}` | Disconnect a social account |
| GET | `/api/voice`, `/api/voice/elevenlabs` | Voice-over status · the ElevenLabs account's voices |
| GET, POST, DELETE | `/api/github`, `/api/github/repos`, `/api/github/refs?repo=` | GitHub connection, repositories, branches and tags |
| GET, POST | `/api/motion`, `/api/motion/install` | Claude Motion status / one-click install |

## Making videos

| Method | Path | |
|---|---|---|
| GET, POST | `/api/projects` | List / create `{topic, language, duration, format, engine, model, effort, hook, playbook_id, style_id, music, sfx, github_repo, style, voice, voice_rate, brand_id, context_ids, auto_publish, integrations, workspace_id}` |
| GET, PUT | `/api/composer` | The composer's remembered choices |
| POST | `/api/hooks` | 3 opening hooks `{topic, language, playbook_id?, brand_id?}` |
| GET | `/api/playbooks` | Video types (structure and pacing); posters under `/playbook-assets/` |
| GET | `/api/styles` | Visual styles (FrameJam + HyperFrames presets); posters under `/style-assets/` |
| GET | `/api/voices?language=` | Voices to pick: the ElevenLabs account's (`eleven:<id>`) when connected, else edge-tts |
| POST | `/api/voice/preview` | A short sample `{text, language, voice?, rate?}` → audio |
| POST, DELETE | `/api/context`, `/api/context/{id}` | Upload a context file (multipart `file`) before creating a video; pass the ids as `context_ids` |
| POST | `/api/ideas` | `{niche, language, count}` |
| GET, PUT | `/api/autopilot` | Autopilot settings |

## Music and sound effects

| Method | Path | |
|---|---|---|
| GET | `/api/music` | The music library |
| POST | `/api/music` | Upload a track (multipart `file`, `name`, `mood`) |
| POST | `/api/music/generate` | Generate a track with ElevenLabs Music `{prompt, seconds}` |
| GET, DELETE | `/api/music/{file}/audio`, `/api/music/{file}` | Play / remove a track |
| GET | `/api/sfx`, `/api/sfx/{name}.wav` | The built-in sound effects |

## A video

| Method | Path | |
|---|---|---|
| GET, DELETE | `/api/projects/{id}` | Read / delete completely (stops its job, removes every version, work files, Claude session logs and context files only it uses) |
| POST | `/api/projects/{id}/retry` | Run a failed video again |
| PUT | `/api/projects/{id}/timeline` | Save a manual edit and re-render |
| POST | `/api/projects/{id}/ai-edit` | `{instruction, model?, effort?}` |
| GET | `/api/projects/{id}/footage?q=` · POST `/api/projects/{id}/upload` | Search stock footage · upload your own clip |
| POST, DELETE | `/api/projects/{id}/context`, `/api/projects/{id}/context/{cid}` | Context files of a video |
| GET, POST, DELETE | `/api/projects/{id}/review`, `/api/projects/{id}/review/{cid}` | Review comments on the current version `{text, t?, t2?, x?, y?}` |
| POST | `/api/projects/{id}/review/apply` | Send the round to Claude `{model?, effort?}` |
| GET | `/api/projects/{id}/filmstrip?file=` | Frame strip of the current version (or of one of its version files) |
| GET, PUT | `/api/projects/{id}/edit` | The timeline edit `{edit: {clips, texts, music, audio}}` on its base version, with the base's size and length |
| POST | `/api/projects/{id}/edit/reset`, `/api/projects/{id}/edit/export` | Start over from the latest version / render the edit as a new version |
| POST | `/api/projects/{id}/layers` | Export the latest version as layers (ZIP: scenes, transparent captions, audio stems, SRT, FCPXML) → `extra.layers.file` under `/videos/` |
| GET | `/api/projects/{id}/activity` | Live steps and latest preview while a video is made |
| GET | `/api/projects/{id}/usage` | Claude usage of this video |
| POST, PUT | `/api/projects/{id}/post-copy` | Write (Claude) / save title, caption, hashtags, tags |
| POST, PUT | `/api/projects/{id}/covers`, `/api/projects/{id}/covers/{i}` | Make 3 covers `{text?}` / choose one |
| POST | `/api/projects/{id}/publish` | `{integrations, publish_options}` |

`voice`: an edge-tts voice name, `eleven:<voice_id>`, `none` (no voice-over: captions timed to reading speed, music turned up) or empty for the default.

`publish_options`: `youtube_visibility` (`public|unlisted|private`), `tiktok_method` (`DIRECT_POST|UPLOAD`), `tiktok_privacy`, `schedule_at` (Postiz only).

## Brand kits and projects

| Method | Path | |
|---|---|---|
| GET, POST | `/api/brands` | List / create brand kits |
| GET, PUT, DELETE | `/api/brands/{id}` | Read / update / delete a kit |
| POST, DELETE | `/api/brands/{id}/assets`, `/api/brands/{id}/assets/{kind}` | Upload (multipart `kind`, `file`) or remove logo, font, intro, outro, music, clip, context |
| POST | `/api/brands/import`, `/api/brands/{id}/sync` | Import / re-sync from GitHub `{url, ref}` |
| POST | `/api/brands/import-website` | Build a brand kit from a website `{url}` |
| GET, POST, PUT, DELETE | `/api/workspaces`, `/api/workspaces/{id}`, `/api/workspaces/{id}/context` | Projects: settings, instructions and context files |

## Usage and housekeeping

| Method | Path | |
|---|---|---|
| GET | `/api/usage?days=30` | Usage for the way Claude is connected now (plan: tokens and time; API key: cost) |
| GET, DELETE | `/api/leftovers`, `/api/leftovers?id=` | Files left by videos deleted with older versions |
