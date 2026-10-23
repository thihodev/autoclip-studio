"""Claude via the Claude Agent SDK.

The SDK drives the Claude Code CLI it bundles, so it authenticates the way
Claude Code does. Nothing is used until the user picks one of three
sources in the Connections tab:

- "app":     a login made from the web UI, stored in DATA_DIR/claude
             (separate from any login in your terminal)
- "system":  this computer's existing Claude Code login (~/.claude)
- "api_key": an Anthropic API key (pay-as-you-go)
"""
import asyncio
import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

import anthropic
import claude_agent_sdk
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, ResultMessage, query

from . import settings, usage
from .config import CLAUDE_CONFIG_DIR, DATA_DIR

SCRIPT_SYSTEM = """You are a senior short-form video producer for YouTube Shorts, TikTok and Facebook Reels.
You write voice-over scripts that hook in the first 2 seconds, keep a fast pace and end with a call to action.
Reply with ONE JSON object only, no markdown fences, no commentary."""

EPISODE_SYSTEM = """You are a senior writer of faceless video series for YouTube and TikTok.
You write episodes, not ads: the episode's question or tension in the first seconds, clear chapters that build to a
payoff, consistency with the earlier episodes, and an ending that teases the next one. No sales pitch.
Reply with ONE JSON object only, no markdown fences, no commentary."""

SCRIPT_PROMPT = """Create a faceless {kind}.

Topic: {topic}
Language of the voice-over and all captions: {language}
Target length: about {duration} seconds of narration (~{words} words in total)
Tone / style: {style}
Extra instructions: {notes}
{brand}{context}
Split the voice-over into {scenes} scenes. Each scene is 1-2 sentences shown over ONE stock clip,
so pick a concrete, filmable ENGLISH search phrase for it (e.g. "woman reading phone at night").{chapters_rule}

Return JSON with exactly these keys:
{{
  "title": "catchy title, max 90 characters, in {language}",
  "scenes": [{{"text": "voice-over for this scene, plain sentences, no emojis, in {language}", "search": "english stock footage phrase", "asset": "brand footage id or null"}}],{chapters_key}
  "description": "2-3 sentence post caption in {language}",
  "hashtags": ["6-10 hashtags without #"],
  "youtube_tags": ["5-10 SEO keywords"],
  "music": "background music that fits this video, in English: genre, energy, tempo (e.g. 'upbeat electronic, energetic, 120 bpm')"
}}"""

EDIT_SYSTEM = """You are a video editor working on a short vertical video made of scenes.
Each scene is a voice-over line over one stock clip. You apply the requested changes by
returning the edited timeline. Keep everything the request doesn't ask to change.
Reply with ONE JSON object only, no markdown fences, no commentary."""

EDIT_PROMPT = """Current video (language: {language}):
{timeline}
{brand}{context}
Caption presets available: bold (big uppercase, highlighted word), box (text on dark box), minimal, none.
Caption position is 0-1 from the top of the frame (0.68 = lower third).

Requested changes:
{instruction}

Return JSON:
{{
  "summary": "one line describing what you changed",
  "title": "updated title (or the same)",
  "scenes": [{{"id": "keep existing id, or null for a new scene", "text": "...", "search": "english stock phrase",
              "new_footage": true if this scene needs a different clip, else false,
              "asset": "brand footage id to use for this scene, or null"}}],
  "captions": {{"preset": "...", "position": 0.68, "color": "#FFFFFF", "highlight_color": "#FFE14D"}},
  "voice_rate": 1.0,
  "motion": true
}}"""

LONG_VIDEO = 180  # seconds; from here videos get chapters and a slower cut

IDEAS_PROMPT = """Suggest {count} short-video ideas for the niche "{niche}" that are likely to go viral
on TikTok / YouTube Shorts right now. Language: {language}.
Return JSON: {{"ideas": [{{"topic": "...", "hook": "first sentence of the video", "why": "why it performs"}}]}}"""


def cli_path() -> str | None:
    bundled = Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude"
    if bundled.exists():
        return str(bundled)
    return shutil.which("claude")


def _env(source: str | None = None) -> dict:
    """Environment for the Claude CLI for the given credential source."""
    cfg = settings.get("claude")
    source = source or cfg["source"]
    if source == "api_key" and cfg["api_key"]:
        return {"ANTHROPIC_API_KEY": cfg["api_key"]}
    if source == "app":
        return {"CLAUDE_CONFIG_DIR": str(CLAUDE_CONFIG_DIR)}
    return {}


def _extract_json(text: str) -> dict:
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fenced:
        text = fenced.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"Claude did not return JSON: {text[:300]}")
    return json.loads(text[start : end + 1])


def transcript_dirs(folder: Path) -> list[Path]:
    """Claude Code keeps session transcripts per working directory: the ones for runs inside `folder`."""
    bases = {re.sub(r"[^A-Za-z0-9]", "-", str(p)) for p in (folder, folder.resolve())}
    out = []
    for root in (CLAUDE_CONFIG_DIR / "projects", Path.home() / ".claude" / "projects"):
        if root.is_dir():
            out += [d for d in root.iterdir()
                    if d.is_dir() and any(d.name == b or d.name.startswith(b + "-") for b in bases)]
    return out


async def ask_json(prompt: str, system: str = SCRIPT_SYSTEM, *, kind: str = "script", project_id: str | None = None,
                   model: str | None = None, effort: str | None = None, tools: list[str] | None = None,
                   cwd: str | None = None, max_turns: int = 1) -> dict:
    if not settings.get("claude")["source"]:
        raise RuntimeError("Claude is not connected — open Connections and choose how to sign in")
    model, effort = usage.choose("script", model, effort)
    meter = usage.Meter(kind, project_id, model, effort)
    if cwd is None and project_id:
        # Run inside the video's folder so its transcripts can be deleted with it.
        cwd = DATA_DIR / "projects" / project_id
        cwd.mkdir(parents=True, exist_ok=True)
    options = ClaudeAgentOptions(
        system_prompt=system,
        model=model,
        effort=effort,
        max_turns=max_turns,
        allowed_tools=tools or [],
        tools=tools or [],  # read-only helpers at most (e.g. Read to look at images)
        cwd=cwd,
        max_buffer_size=64 * 1024 * 1024,  # images Claude reads travel through the stream as base64
        setting_sources=[],  # don't load the user's CLAUDE.md / project settings
        env=_env(),
        cli_path=cli_path(),
    )
    result = None
    try:
        async for message in query(prompt=prompt, options=options):
            meter.see(message)
            if isinstance(message, ResultMessage):
                if message.is_error:
                    raise RuntimeError(f"Claude error: {message.result}")
                result = message.result
    except Exception:
        meter.save(ok=False)
        raise
    meter.save()
    if not result:
        raise RuntimeError("Claude returned no result")
    return _extract_json(result)


async def write_script(topic: str, language: str, duration: int, style: str, notes: str, brand_context: str = "",
                       source: str = "", project_id: str | None = None, model: str | None = None,
                       effort: str | None = None, episode: bool = False) -> dict:
    words = int(duration * 2.5)
    long = duration >= LONG_VIDEO
    # Short videos cut every ~5 s; long ones every ~8 s (a 20-minute episode has about 150 scenes).
    scenes = max(8, min(150, round(duration / 8))) if long else max(3, min(12, round(duration / 5)))
    chapters = max(3, min(12, round(duration / 120)))
    data = await ask_json(
        SCRIPT_PROMPT.format(
            kind=("video series episode" if episode else "video") if long else ("short video series episode" if episode else "short video"),
            chapters_rule=(f"\nGroup the scenes into {chapters} chapters that build on each other, each with a short title "
                           f"(the first one can be the opening)." if long else ""),
            chapters_key=(f'\n  "chapters": [{{"title": "chapter title in {language}, max 40 characters", '
                          f'"first_scene": "number of its first scene, from 1"}}],' if long else ""),
            topic=topic, language=language, duration=duration, words=words, scenes=scenes,
            style=style or "engaging, informative", notes=notes or "none",
            brand=f"\n{brand_context}\n" if brand_context else "",
            context=f"\n{source}\n" if source else "",
        ),
        EPISODE_SYSTEM if episode else SCRIPT_SYSTEM, kind="script", project_id=project_id, model=model, effort=effort,
    )
    if not data.get("title") or not data.get("scenes"):
        raise ValueError("Claude response is missing title or scenes")
    for c in data.get("chapters") or []:  # the chapter title goes on its first scene (a title card in the render)
        try:
            n = int(c["first_scene"]) - 1
        except (KeyError, TypeError, ValueError):
            continue
        if 0 <= n < len(data["scenes"]) and str(c.get("title") or "").strip():
            data["scenes"][n]["chapter"] = str(c["title"]).strip()[:60]
    data["script"] = " ".join(sc["text"] for sc in data["scenes"])
    return data


async def edit_timeline(timeline: dict, meta: dict, language: str, instruction: str, brand_context: str = "",
                        source: str = "", project_id: str | None = None, model: str | None = None,
                        effort: str | None = None) -> dict:
    view = {
        "title": meta.get("title"),
        "captions": timeline["captions"],
        "voice_rate": timeline["voice_rate"],
        "motion": timeline.get("motion", True),
        "scenes": [
            {"id": s["id"], "start": s.get("start"), "end": s.get("end"), "text": s["text"], "search": s["search"]}
            for s in timeline["scenes"]
        ],
    }
    return await ask_json(
        EDIT_PROMPT.format(language=language, timeline=json.dumps(view, ensure_ascii=False, indent=1),
                           instruction=instruction, brand=f"\n{brand_context}\n" if brand_context else "",
                           context=f"\n{source}\n" if source else ""),
        system=EDIT_SYSTEM, kind="edit", project_id=project_id, model=model, effort=effort,
    )


HOOKS_PROMPT = """Write 3 different opening hooks for a short video (YouTube Shorts / TikTok / Reels).

Topic: {topic}
Language of the hooks: {language}
{extra}
A hook is the first spoken line (max 14 words), said in the first 2 seconds. Make the 3 take clearly different angles,
e.g. a surprising fact, a bold claim, a question, a mistake to avoid, a result first, a relatable pain.
No clickbait that the video can't deliver.

Reply with JSON: {{"hooks": [{{"text": "the hook in {language}", "angle": "2-4 word label in English"}}]}}"""

POST_PROMPT = """Write the post copy for this short video, to publish on YouTube, TikTok and Facebook.

Language: {language}
Video title so far: {title}
What the video says (its narration):
{narration}
{extra}
Reply with JSON:
{{"title": "catchy, honest title, max 90 characters, in {language}",
  "description": "2-4 sentence caption in {language}: the promise, one key detail, a call to action",
  "hashtags": ["6-10 relevant hashtags without #, mixing broad and niche"],
  "youtube_tags": ["8-12 SEO keywords people would search"],
  "thumbnail_text": "2-5 punchy words for the cover image, in {language}"}}"""


CONCEPT_PROMPT = """Plan a short video before it is made, so the person can approve or change the idea first.

What they asked for: {topic}
Language: {language} · length: about {duration} seconds · format: {format}
{extra}
Find the strongest angle for this topic: the one promise the video makes, who it is for, the opening line that stops
the scroll in the first second, and the beats that deliver on it (one per scene, at most {scenes}). Stay true to the
request and to any source material; don't invent facts.{avoid}

Reply with JSON:
{{"title": "working title in {language}, max 80 characters",
  "angle": "one sentence in {language}: the promise of the video and why it's worth watching",
  "audience": "who it is for, in {language}, a few words",
  "hook": "the first spoken line in {language}, max 14 words",
  "outline": ["scene by scene, what is shown and said, one short line each in {language}"]}}"""


COMPOSE_SYSTEM = """You compose instrumental background music for short videos, as Tone.js code that is rendered offline.
Reply with ONE JSON object only: {"code": "<the whole score.js>"} — no markdown fences, no commentary."""


async def compose_score(brief: str, project_id: str | None = None, model: str | None = None,
                        effort: str | None = None) -> str:
    """Quick engine: the score for a video, in one call (Claude Motion composes inside its own run)."""
    data = await ask_json(brief, COMPOSE_SYSTEM, kind="music", project_id=project_id, model=model, effort=effort)
    code = str(data.get("code") or "")
    if "function compose" not in code:
        raise RuntimeError("Claude didn't write a score")
    return code


async def plan_video(topic: str, language: str, duration: int, format: str, extra: str = "", avoid: list[str] | None = None,
                     model: str | None = None) -> dict:
    """A concept to approve before the video is made: title, angle, audience, hook and a scene outline."""
    avoid_text = ("\nIt must be a clearly different idea from these, already proposed: " + "; ".join(avoid[:5])) if avoid else ""
    data = await ask_json(CONCEPT_PROMPT.format(topic=topic, language=language, duration=duration, format=format, extra=extra,
                                                scenes=max(3, min(10, round(duration / 6))), avoid=avoid_text),
                          kind="concept", model=model, effort="low")
    outline = [str(x).strip() for x in data.get("outline") or [] if str(x).strip()][:12]
    if not data.get("title") or not outline:
        raise RuntimeError("Claude didn't return a plan — try again")
    return {"title": str(data["title"])[:120], "angle": str(data.get("angle") or "")[:400],
            "audience": str(data.get("audience") or "")[:120], "hook": str(data.get("hook") or "")[:200], "outline": outline}


SERIES_PROMPT = """You plan the next videos of a series of short videos.

Series: {name}
Language: {language} · {formats}
{extra}
Videos already made or planned in this series (don't repeat them; continue where they leave off):
{done}
{hint}
Suggest the {count} best next videos for this series: what a viewer of the earlier ones would want next, in a sensible
order, each one its own clear topic. Stay accurate; don't invent facts, rules or numbers.

Reply with JSON:
{{"ideas": [{{"title": "working title in {language}, max 80 characters",
             "pillar": "the content pillar it belongs to, exactly as named above; empty when none are named",
             "format": "{format_keys}",
             "angle": "one sentence in {language}: what this video teaches or shows and why it's worth watching",
             "hook": "the first spoken line in {language}, max 14 words",
             "outline": ["scene by scene, what is shown and said, one short line each in {language}"]}}]}}"""


CHANNEL_PROMPT = """You are a strategist who builds faceless short-video channels on YouTube and TikTok.
Plan a new channel around this topic, so the person can approve or change it before it is set up.

Topic: {topic}
Platforms: {platforms} · language of the channel: {language}
Formats: {formats}
{extra}{competitors}
Find a clear position: who the channel is for, the promise that makes them follow, and how it differs from what is
already out there. Plan content pillars (recurring kinds of video) that a faceless channel made of short animated
videos can keep up for months, and the first videos that would make a newcomer subscribe. Be specific to the topic;
don't invent facts, numbers or claims about competitors that the list above doesn't show.{avoid}

Reply with JSON:
{{"names": ["3 channel name options in {language} or neutral, short and easy to say, the best first"],
  "handles": ["3 matching @handles without @, lowercase, letters, digits, dots or underscores"],
  "tagline": "what the channel gives in under 8 words, in {language}",
  "audience": "who it is for, in {language}, one sentence",
  "positioning": "why follow this channel rather than others, in {language}, 1-2 sentences",
  "voice": "voice and tone of the videos in {language}, a few words",
  "cta": "the call to action that ends the videos, in {language}",
  "pillars": [{{"name": "pillar name in {language}", "about": "what these videos do, in {language}, one sentence",
               "examples": ["2 example video titles in {language}"]}}],
  "cadence": "how often to post on each platform and when, in {language}, one sentence",
  "bio_youtube": "the YouTube channel description in {language}, 2-4 sentences, max 900 characters",
  "bio_tiktok": "the TikTok bio in {language}, max 80 characters",
  "hashtags": ["8-12 hashtags without # that the channel keeps using"],
  "colors": {{"primary": "#hex brand color that fits the topic", "accent": "#hex contrasting highlight", "text": "#hex"}},
  "lengths": {{{lengths}}},
  "learned": ["what works for the channels listed above and what they leave out, short lines in {language}; [] when none are listed"],
  "instructions": "instructions Claude follows for every video of this channel, in English: audience, tone, structure, what to always or never do",
  "first_videos": [{{"title": "working title in {language}, max 80 characters", "pillar": "its pillar name",
                    "format": "{format_keys}",
                    "angle": "one sentence in {language}: what this video shows and why it's worth watching",
                    "hook": "the first spoken line in {language}, max 14 words",
                    "outline": ["scene by scene, one short line each in {language}"]}}]}}
Give 3-5 pillars and exactly {first} first videos from different pillars{mix}."""


NICHE_QUERIES_PROMPT = """Someone wants to find directions that already work for faceless short-video channels on
YouTube. Their interest: {interest}

Write 3 different English YouTube search queries that surface the most successful channels around this interest:
each 2-5 words, the way viewers search, covering different angles of it (e.g. a broad one, a specific sub-niche, a format).

Reply with JSON: {{"queries": ["...", "...", "..."]}}"""

NICHE_PROMPT = """You help someone pick the direction of a new faceless short-video channel (animated videos, no presenter)
by finding what already works on YouTube. Their interest: {interest}

Channels found on YouTube for it, with their numbers:
{channels}

A high "median views / followers" means the channel's videos reach far beyond its followers: a format that travels.
Small channels with that are the best sign that a direction works now, not just a famous creator.

Propose the {count} most successful directions. For each: what the format is, the evidence from the numbers above (name
the channels), what a new channel could do better or differently to stand out, how well it fits short animated videos
made by AI, and the risks (saturation, facts that need care, rules of the platforms). Never suggest copying a channel's
videos, characters, names or words — only the format and the need it meets.

Reply with JSON (the person reads {language}):
{{"summary": "2-3 sentences in {language}: what works around this interest and where the openings are",
  "directions": [{{"title": "short name of the direction in {language}",
                  "format": "what each video is, in {language}, one sentence",
                  "evidence": "why it works, citing the channels and numbers, in {language}",
                  "edge": "how a new channel can stand out, in {language}",
                  "fit": "high | medium | low — how well it suits short animated AI videos",
                  "risks": "in {language}, one sentence",
                  "channels": ["names of the channels above to learn from, at most 3"],
                  "topic": "the topic to give the channel planner, in {language}, one sentence",
                  "examples": ["3 example video titles in {language}"]}}]}}"""


async def niche_queries(interest: str) -> list[str]:
    data = await ask_json(NICHE_QUERIES_PROMPT.format(interest=interest), kind="niche", effort="low")
    return [str(q).strip()[:80] for q in data.get("queries") or [] if str(q).strip()][:3]


async def niche_directions(interest: str, language: str, channels: str, count: int = 4) -> dict:
    """Directions for a new channel, from channels abroad that do well (one call)."""
    data = await ask_json(NICHE_PROMPT.format(interest=interest, language=language, channels=channels,
                                              count=count), kind="niche", effort="medium")
    out = []
    for d in data.get("directions") or []:
        if not isinstance(d, dict) or not d.get("title"):
            continue
        out.append({**{k: str(d.get(k) or "").strip()[:600] for k in ("title", "format", "evidence", "edge", "risks", "topic")},
                    "fit": d.get("fit") if d.get("fit") in ("high", "medium", "low") else "medium",
                    "channels": [str(c)[:80] for c in d.get("channels") or []][:3],
                    "examples": [str(c)[:120] for c in d.get("examples") or []][:3]})
    if not out:
        raise RuntimeError("Claude didn't return any direction — try again")
    return {"summary": str(data.get("summary") or "")[:800], "directions": out}


def _hex(c, default: str) -> str:
    return c if isinstance(c, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", c) else default


async def channel_plan(topic: str, language: str, formats: list[str], platforms: list[str], competitors: str = "",
                       extra: str = "", avoid: list[str] | None = None, first: int = 3) -> dict:
    """A channel to approve before it is set up: names, position, pillars, bios, colors and its first videos."""
    avoid_text = ("\nPropose a clearly different channel from these, already proposed: " + "; ".join(avoid[:5])) if avoid else ""
    data = await ask_json(CHANNEL_PROMPT.format(topic=topic, platforms=" and ".join(platforms) or "YouTube and TikTok",
                                                language=language, extra=extra, avoid=avoid_text, first=first,
                                                formats="; ".join(f"{f} = {FORMAT_NAMES.get(f, f)}" for f in formats),
                                                lengths=", ".join(f'"{f}": seconds per video in this format (10-1200), '
                                                                  "fitting the topic and the platform" for f in formats),
                                                format_keys=" | ".join(formats), mix=" and covering every format" if len(formats) > 1 else "",
                                                competitors=f"\n{competitors}\n" if competitors else ""),
                          kind="channel", effort="medium")
    strs = lambda v, n, cap: [str(x).strip()[:cap] for x in v or [] if str(x).strip()][:n]  # noqa: E731
    names = strs(data.get("names"), 3, 60)
    pillars = [{"name": str(p["name"])[:60], "about": str(p.get("about") or "")[:300], "examples": strs(p.get("examples"), 3, 120)}
               for p in data.get("pillars") or [] if isinstance(p, dict) and p.get("name")][:6]
    if not names or not pillars:
        raise RuntimeError("Claude didn't return a channel plan — try again")
    colors = data.get("colors") if isinstance(data.get("colors"), dict) else {}
    videos = []
    for x in data.get("first_videos") or []:
        if (item := _plan_item(x)):
            videos.append({**item, "pillar": str(x.get("pillar") or "")[:60],
                           "format": x.get("format") if x.get("format") in formats else formats[0]})
    raw = data.get("lengths") if isinstance(data.get("lengths"), dict) else {}
    lengths = {}
    for f in formats:
        try:
            lengths[f] = max(10, min(1200, int(raw.get(f))))
        except (TypeError, ValueError):
            lengths[f] = 480 if f == "youtube" else 45
    return {"names": names, "name": names[0],
            "handles": [re.sub(r"[^a-z0-9._]", "", h.lower().lstrip("@"))[:30] for h in strs(data.get("handles"), 3, 40)],
            **{k: str(data.get(k) or "").strip()[:n] for k, n in
               (("tagline", 80), ("audience", 300), ("positioning", 500), ("voice", 200), ("cta", 200), ("cadence", 300),
                ("bio_youtube", 1000), ("bio_tiktok", 80), ("instructions", 2000))},
            "pillars": pillars,
            "hashtags": [h.lstrip("#").replace(" ", "") for h in strs(data.get("hashtags"), 12, 40)],
            "colors": {"primary": _hex(colors.get("primary"), "#E8385A"), "accent": _hex(colors.get("accent"), "#FFE14D"),
                       "text": _hex(colors.get("text"), "#FFFFFF")},
            "lengths": lengths, "learned": strs(data.get("learned"), 8, 300), "first_videos": videos[:first]}


def _plan_item(x: dict) -> dict | None:
    outline = [str(o).strip() for o in x.get("outline") or [] if str(o).strip()][:12] if isinstance(x, dict) else []
    if not isinstance(x, dict) or not x.get("title"):
        return None
    return {"title": str(x["title"])[:120], "angle": str(x.get("angle") or "")[:400], "hook": str(x.get("hook") or "")[:200],
            "outline": outline}


FORMAT_NAMES = {"shorts": "9:16 vertical (TikTok, YouTube Shorts, Reels)", "youtube": "16:9 landscape (YouTube videos)",
                "square": "1:1 square"}


def formats_line(lengths: dict[str, int]) -> str:
    """{format: seconds} as the prompts state it."""
    if len(lengths) == 1:
        (f, sec), = lengths.items()
        return f"each video about {sec} seconds · format: {f} = {FORMAT_NAMES.get(f, f)}"
    return "the channel makes several formats: " + "; ".join(f"{f} = {FORMAT_NAMES.get(f, f)}, about {sec} seconds"
                                                              for f, sec in lengths.items())


async def series_ideas(name: str, language: str, lengths: dict[str, int], done: list[str], count: int = 3,
                       extra: str = "", hint: str = "") -> list[dict]:
    """The next videos of a series (a project), different from the ones already made or planned. `lengths` is
    {format: seconds}; with several formats each idea says which one it is."""
    keys = " | ".join(lengths)
    data = await ask_json(SERIES_PROMPT.format(name=name, language=language, formats=formats_line(lengths), extra=extra,
                                               format_keys=keys + (" — the format that suits this video" if len(lengths) > 1 else ""),
                                               done="\n".join(f"- {d}" for d in done[:60]) or "- none yet",
                                               hint=f"Wanted next: {hint.strip()}" if hint.strip() else "", count=count),
                          kind="concept", effort="low")
    first = next(iter(lengths))
    return [{**i, "pillar": str(x.get("pillar") or "")[:60], "format": x.get("format") if x.get("format") in lengths else first}
            for x in data.get("ideas") or [] if (i := _plan_item(x))][:count]


async def suggest_hooks(topic: str, language: str, extra: str = "", model: str | None = None) -> list[dict]:
    data = await ask_json(HOOKS_PROMPT.format(topic=topic, language=language, extra=extra), kind="hooks",
                          model=model, effort="low")
    return [h for h in data.get("hooks", []) if isinstance(h, dict) and h.get("text")][:3]


async def write_post(title: str, narration: str, language: str, extra: str = "", project_id: str | None = None,
                     model: str | None = None) -> dict:
    return await ask_json(POST_PROMPT.format(title=title, narration=narration[:6000], language=language, extra=extra),
                          kind="post", project_id=project_id, model=model, effort="low")


async def suggest_ideas(niche: str, language: str, count: int = 8) -> list[dict]:
    data = await ask_json(IDEAS_PROMPT.format(niche=niche, language=language, count=count), kind="ideas")
    return data.get("ideas", [])


# ---------- auth status ----------

def _cli_status(env: dict) -> dict:
    cli = cli_path()
    if not cli:
        return {"ok": False, "detail": "Claude CLI not found (reinstall claude-agent-sdk)"}
    try:
        out = subprocess.run(
            [cli, "auth", "status"], capture_output=True, text=True, timeout=20,
            env={**os.environ, **env},
        )
        info = json.loads(out.stdout or "{}")
    except Exception as e:
        return {"ok": False, "detail": f"Could not check login ({e.__class__.__name__})"}
    if not info.get("loggedIn"):
        return {"ok": False, "detail": "Not signed in"}
    plan = info.get("subscriptionType") or info.get("authMethod") or ""
    return {"ok": True, "detail": f"{info.get('email') or 'signed in'} · {plan}".strip(" ·"), "email": info.get("email")}


def auth_status() -> dict:
    cfg = settings.get("claude")
    source = cfg["source"]
    if not source:
        res = {"ok": False, "detail": "Not connected"}
    elif source == "api_key":
        key = cfg["api_key"]
        res = {"ok": bool(key), "detail": f"API key …{key[-4:]}" if key else "API key not set"}
    else:
        res = _cli_status(_env(source))
    return {**res, "source": source}


def app_status() -> dict:
    """The login made from the web UI, whether or not it's the one in use."""
    return _cli_status(_env("app"))


async def check_api_key(key: str) -> str | None:
    """None when the key works, else an error message. Listing models costs nothing."""
    try:
        async with anthropic.AsyncAnthropic(api_key=key, max_retries=1, timeout=20) as client:
            await client.models.list(limit=1)
    except anthropic.AuthenticationError:
        return "Anthropic rejected this API key"
    except anthropic.PermissionDeniedError:
        return "This API key isn't allowed to use the Claude API"
    except anthropic.APIConnectionError:
        return "Could not reach the Claude API"
    except anthropic.APIStatusError as e:
        return f"Claude API error {e.status_code}"
    return None


# ---------- plan usage: the windows Claude Code's /usage shows ----------

_plan_cache: dict[str, dict] = {}
_plan_lock = asyncio.Lock()


def _money(m) -> float | None:
    return m["amount_minor"] / 10 ** m.get("exponent", 2) if isinstance(m, dict) and "amount_minor" in m else None


def _plan_view(raw: dict) -> dict:
    rl = raw.get("rate_limits") or {}
    limits = [{
        "kind": x.get("kind"), "group": x.get("group"), "percent": x.get("percent"), "resets_at": x.get("resets_at"),
        "model": (((x.get("scope") or {}).get("model") or {}).get("display_name")),
        "severity": x.get("severity"), "active": x.get("is_active"),
    } for x in rl.get("limits") or []]
    if not limits:  # older answers only have the named windows
        for key, group, model in [("five_hour", "session", None), ("seven_day", "weekly", None),
                                  ("seven_day_opus", "weekly", "Opus"), ("seven_day_sonnet", "weekly", "Sonnet")]:
            if (w := rl.get(key)) and w.get("utilization") is not None:
                limits.append({"kind": key, "group": group, "percent": w["utilization"], "resets_at": w.get("resets_at"),
                               "model": model, "severity": None, "active": None})
        for w in rl.get("model_scoped") or []:
            limits.append({"kind": "weekly_scoped", "group": "weekly", "percent": w.get("utilization"),
                           "resets_at": w.get("resets_at"), "model": w.get("display_name"), "severity": None, "active": None})
    spend = rl.get("spend") or {}
    extra = None
    if spend.get("enabled"):
        extra = {"used": _money(spend.get("used")), "limit": _money(spend.get("limit")),
                 "currency": (spend.get("used") or {}).get("currency"), "percent": spend.get("percent")}
    return {"plan": raw.get("subscription_type"), "available": bool(raw.get("rate_limits_available")),
            "limits": limits, "extra": extra}


async def plan_usage(refresh: bool = False) -> dict:
    """How much of the plan's 5-hour session and weekly limits is used, and when they reset.

    Asks the Claude CLI itself (the `get_usage` control request behind /usage, still marked
    experimental). No message goes to Claude, so checking uses nothing. API keys have no plan limits.
    """
    source = settings.get("claude")["source"]
    if source not in ("app", "system"):
        return {"source": source, "applies": False}
    async with _plan_lock:
        hit = _plan_cache.get(source)
        if hit and not refresh and time.time() - hit["at"] < 60:
            return hit
        options = ClaudeAgentOptions(setting_sources=[], tools=[], allowed_tools=[], max_turns=1, cwd=str(DATA_DIR),
                                     env=_env(source), cli_path=cli_path())
        try:
            async with ClaudeSDKClient(options=options) as client:
                # The Python SDK has no public wrapper for this request yet.
                raw = await client._query._send_control_request({"subtype": "get_usage", "skip_behaviors": True},
                                                                timeout=45)
            res = _plan_view(raw)
        except Exception as e:
            res = {"error": f"Could not read your plan usage: {str(e)[:200] or e.__class__.__name__}"}
        res.update(source=source, applies=True, at=time.time())
        _plan_cache[source] = res
        return res


def system_status() -> dict:
    """Is there a usable Claude Code login on this computer (outside the app)?"""
    return _cli_status({})


def logout_app():
    cli = cli_path()
    if cli:
        subprocess.run([cli, "auth", "logout"], capture_output=True, timeout=20,
                       env={**os.environ, **_env("app")})


# ---------- web sign-in: drives `claude auth login` in a pseudo-terminal ----------

class LoginSession:
    """One interactive `claude auth login` run.

    The CLI prints an authorize URL and waits for the code shown after the
    user approves in their browser. We relay the URL to the web UI and write
    the pasted code back to the CLI.
    """

    URL_RE = re.compile(r"https://(?:claude\.ai|claude\.com)/[^\s\x07\x1b]+")

    def __init__(self):
        import pty  # POSIX only

        self.state = "starting"  # starting | waiting_code | verifying | done | error
        self.url = None
        self.error = None
        self.output = ""
        self.started = time.time()
        master, slave = pty.openpty()
        self.fd = master
        env = {**os.environ, **_env("app"), "BROWSER": "true", "NO_COLOR": "1"}
        env.pop("ANTHROPIC_API_KEY", None)
        self.proc = subprocess.Popen(
            [cli_path(), "auth", "login", "--claudeai"],
            stdin=slave, stdout=slave, stderr=slave, env=env, close_fds=True,
        )
        os.close(slave)
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        while True:
            try:
                chunk = os.read(self.fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            self.output += chunk.decode(errors="replace")
            if not self.url and (m := self.URL_RE.search(self.output)):
                self.url = m.group(0)
                self.state = "waiting_code"
            if self.state == "verifying" and re.search(r"(?i)press enter", self.output):
                self._write("\r")
        code = self.proc.wait()
        if code == 0 and _cli_status(_env("app"))["ok"]:
            self.state = "done"
        elif self.state != "done":
            self.state = "error"
            tail = re.sub(r"\x1b\][^\x07]*\x07|\x1b\[[0-9;?]*[A-Za-z]", "", self.output)[-400:]
            self.error = tail.strip() or f"claude auth login exited with {code}"

    def _write(self, text: str):
        try:
            os.write(self.fd, text.encode())
        except OSError:
            pass

    def submit_code(self, code: str):
        self.state = "verifying"
        self._write(code.strip() + "\r")

    def cancel(self):
        if self.proc.poll() is None:
            self.proc.kill()

    def public(self) -> dict:
        if self.state in ("starting", "waiting_code", "verifying") and time.time() - self.started > 900:
            self.cancel()
            self.state, self.error = "error", "Sign-in timed out"
        return {"state": self.state, "url": self.url, "error": self.error}


_login: LoginSession | None = None


def login_start() -> dict:
    global _login
    if _login:
        _login.cancel()
    _login = LoginSession()
    for _ in range(100):  # wait up to ~10s for the URL
        if _login.url or _login.state == "error":
            break
        time.sleep(0.1)
    return _login.public()


def login_submit(code: str) -> dict:
    if not _login:
        raise RuntimeError("No sign-in in progress")
    _login.submit_code(code)
    for _ in range(300):  # wait up to ~30s for the CLI to finish
        if _login.state in ("done", "error"):
            break
        time.sleep(0.1)
    if _login.state == "done":
        settings.put("claude", {**settings.get("claude"), "source": "app"})
    return _login.public()


def login_cancel():
    global _login
    if _login:
        _login.cancel()
        _login = None


def login_state() -> dict:
    return _login.public() if _login else {"state": "idle", "url": None, "error": None}
