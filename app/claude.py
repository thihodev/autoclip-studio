"""Claude via the Claude Agent SDK.

The SDK drives the local Claude Code CLI, so it authenticates the same way
Claude Code does: your existing `claude` login (Pro/Max subscription),
CLAUDE_CODE_OAUTH_TOKEN from `claude setup-token`, or ANTHROPIC_API_KEY.
"""
import json
import re
import shutil
import subprocess

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

from .config import CLAUDE_MODEL

SCRIPT_SYSTEM = """You are a senior short-form video producer for YouTube Shorts, TikTok and Facebook Reels.
You write voice-over scripts that hook in the first 2 seconds, keep a fast pace and end with a call to action.
Reply with ONE JSON object only, no markdown fences, no commentary."""

SCRIPT_PROMPT = """Create a faceless short video.

Topic: {topic}
Language of the voice-over and all captions: {language}
Target length: about {duration} seconds of narration (~{words} words)
Tone / style: {style}
Extra instructions: {notes}

Return JSON with exactly these keys:
{{
  "title": "catchy title, max 90 characters, in {language}",
  "script": "the full voice-over text, plain sentences, no stage directions, no emojis, in {language}",
  "search_terms": ["5-8 short ENGLISH stock-footage search phrases matching the script, in order"],
  "description": "2-3 sentence post caption in {language}",
  "hashtags": ["6-10 hashtags without #"],
  "youtube_tags": ["5-10 SEO keywords"]
}}"""

IDEAS_PROMPT = """Suggest {count} short-video ideas for the niche "{niche}" that are likely to go viral
on TikTok / YouTube Shorts right now. Language: {language}.
Return JSON: {{"ideas": [{{"topic": "...", "hook": "first sentence of the video", "why": "why it performs"}}]}}"""


def _extract_json(text: str) -> dict:
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fenced:
        text = fenced.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"Claude did not return JSON: {text[:300]}")
    return json.loads(text[start : end + 1])


async def ask_json(prompt: str, system: str = SCRIPT_SYSTEM) -> dict:
    options = ClaudeAgentOptions(
        system_prompt=system,
        model=CLAUDE_MODEL,
        max_turns=1,
        allowed_tools=[],
        setting_sources=[],  # don't load the user's CLAUDE.md / project settings
    )
    result = None
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, ResultMessage):
            if message.is_error:
                raise RuntimeError(f"Claude error: {message.result}")
            result = message.result
    if not result:
        raise RuntimeError("Claude returned no result")
    return _extract_json(result)


async def write_script(topic: str, language: str, duration: int, style: str, notes: str) -> dict:
    words = int(duration * 2.5)
    data = await ask_json(
        SCRIPT_PROMPT.format(
            topic=topic, language=language, duration=duration, words=words,
            style=style or "engaging, informative", notes=notes or "none",
        )
    )
    missing = {"title", "script", "search_terms"} - data.keys()
    if missing:
        raise ValueError(f"Claude response missing keys: {missing}")
    return data


async def suggest_ideas(niche: str, language: str, count: int = 8) -> list[dict]:
    data = await ask_json(IDEAS_PROMPT.format(niche=niche, language=language, count=count))
    return data.get("ideas", [])


def auth_status() -> dict:
    """Best-effort check of how the Claude Code CLI is authenticated."""
    cli = shutil.which("claude")
    if not cli:
        return {"ok": False, "detail": "Claude Code CLI not found. Install: npm i -g @anthropic-ai/claude-code"}
    try:
        out = subprocess.run([cli, "auth", "status"], capture_output=True, text=True, timeout=15)
        text = (out.stdout or out.stderr).strip()
        try:
            info = json.loads(text)
            return {
                "ok": bool(info.get("loggedIn")),
                "detail": f"{info.get('authMethod')} · {info.get('subscriptionType') or ''} · {info.get('email') or ''}",
            }
        except json.JSONDecodeError:
            return {"ok": out.returncode == 0, "detail": text[:500] or "unknown"}
    except Exception as e:  # older CLI without `auth status`
        return {"ok": True, "detail": f"Claude Code CLI found at {cli} ({e.__class__.__name__} checking auth)"}
