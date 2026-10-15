"""Claude via the Claude Agent SDK.

The SDK drives the Claude Code CLI it bundles, so it authenticates the way
Claude Code does. Nothing is used until the user picks one of three
sources in the Connections tab:

- "app":     a login made from the web UI, stored in DATA_DIR/claude
             (separate from any login in your terminal)
- "system":  this computer's existing Claude Code login (~/.claude)
- "api_key": an Anthropic API key (pay-as-you-go)
"""
import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

import claude_agent_sdk
from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

from . import settings, usage
from .config import CLAUDE_CONFIG_DIR, DATA_DIR

SCRIPT_SYSTEM = """You are a senior short-form video producer for YouTube Shorts, TikTok and Facebook Reels.
You write voice-over scripts that hook in the first 2 seconds, keep a fast pace and end with a call to action.
Reply with ONE JSON object only, no markdown fences, no commentary."""

SCRIPT_PROMPT = """Create a faceless short video.

Topic: {topic}
Language of the voice-over and all captions: {language}
Target length: about {duration} seconds of narration (~{words} words in total)
Tone / style: {style}
Extra instructions: {notes}
{brand}{context}
Split the voice-over into {scenes} scenes. Each scene is 1-2 sentences shown over ONE stock clip,
so pick a concrete, filmable ENGLISH search phrase for it (e.g. "woman reading phone at night").

Return JSON with exactly these keys:
{{
  "title": "catchy title, max 90 characters, in {language}",
  "scenes": [{{"text": "voice-over for this scene, plain sentences, no emojis, in {language}", "search": "english stock footage phrase", "asset": "brand footage id or null"}}],
  "description": "2-3 sentence post caption in {language}",
  "hashtags": ["6-10 hashtags without #"],
  "youtube_tags": ["5-10 SEO keywords"]
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
                       effort: str | None = None) -> dict:
    words = int(duration * 2.5)
    scenes = max(3, min(12, round(duration / 5)))
    data = await ask_json(
        SCRIPT_PROMPT.format(
            topic=topic, language=language, duration=duration, words=words, scenes=scenes,
            style=style or "engaging, informative", notes=notes or "none",
            brand=f"\n{brand_context}\n" if brand_context else "",
            context=f"\n{source}\n" if source else "",
        ),
        kind="script", project_id=project_id, model=model, effort=effort,
    )
    if not data.get("title") or not data.get("scenes"):
        raise ValueError("Claude response is missing title or scenes")
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
