"""Claude Motion engine: Claude designs and animates the whole video as code.

Like Anthropic's Claude Motion, the video is built as an animation in code
(kinetic type, diagrams, charts, shapes, transitions) — here with HyperFrames
(HeyGen, Apache-2.0: HTML + GSAP → MP4 via headless Chrome).

A Claude agent (Agent SDK) runs HyperFrames' `faceless-explainer` workflow
autonomously inside the project folder, using the HyperFrames skills. The app
supplies the brief, brand assets, context files and the narration (ElevenLabs
or edge-tts) through an in-process `voiceover` tool, then post-processes the
render (brand intro/outro) and keeps every render as a version.

HyperFrames' skill installer writes to the user's global agent folders, so we
always run it with an app-owned HOME and copy skills into each project.
"""
import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from claude_agent_sdk import (
    AgentDefinition, AssistantMessage, ClaudeAgentOptions, PermissionResultAllow, PermissionResultDeny, ResultMessage,
    TextBlock, ToolUseBlock, create_sdk_mcp_server, query, tool,
)

from . import activity, brands, claude, context, render, settings, tts, usage
from .config import DATA_DIR

log = logging.getLogger("motion")

HF_VERSION = "0.8.159"
HF_HOME = DATA_DIR / "hyperframes"  # isolated HOME for HyperFrames' skill installer
SKILLS_DIR = HF_HOME / ".claude" / "skills"
WORKFLOW = "faceless-explainer"
RESOLUTIONS = {"shorts": "portrait", "tiktok": "portrait", "youtube": "landscape", "square": "square"}
ASPECTS = {"portrait": "1080x1920", "landscape": "1920x1080", "square": "1080x1080"}
TIMEOUT = 60 * 60  # one agent run

# Shell commands the agent must never run (installs, network, privilege, VCS).
DENY = re.compile(
    r"(skills\s+(update|add)|npx\s+(-y\s+)?skills\b|\bcurl\b|\bwget\b|\bsudo\b|\bssh\b|\bscp\b|\bgit\s+(push|clone)"
    r"|\bpip3?\s+install|\bbrew\b|\bnpm\s+(i|install)\s+-g|\bheygen\b|hyperframes(@[\d.]+)?\s+(auth|publish|cloud|lambda|cloudrun)"
    r"|rm\s+-rf?\s+[/~])"
)


# ------------------------------------------------------------------ toolchain

def _node_version() -> tuple[int, ...] | None:
    node = shutil.which("node")
    if not node:
        return None
    try:
        out = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=10).stdout.strip()
        return tuple(int(x) for x in out.lstrip("v").split(".")[:3])
    except Exception:
        return None


def _skills_ready() -> bool:
    return (SKILLS_DIR / WORKFLOW / "SKILL.md").exists() and (SKILLS_DIR / "hyperframes-core" / "SKILL.md").exists()


def status() -> dict:
    node = _node_version()
    if not node or not shutil.which("npx"):
        return {"ok": False, "code": "node", "detail": "Node.js 22+ is required — https://nodejs.org"}
    if node[0] < 22:
        return {"ok": False, "code": "node", "detail": f"Node.js {'.'.join(map(str, node))} found — 22 or newer is required"}
    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        return {"ok": False, "code": "ffmpeg", "detail": "FFmpeg (with ffprobe) is required — e.g. brew install ffmpeg"}
    if not _skills_ready():
        return {"ok": False, "code": "install", "detail": "Ready to install (one-time download of HyperFrames)"}
    return {"ok": True, "code": "ready", "detail": f"HyperFrames {HF_VERSION} · Node {'.'.join(map(str, node))}"}


def _env(extra: dict | None = None) -> dict:
    env = {**os.environ, "HYPERFRAMES_SKIP_SKILLS": "1", "DO_NOT_TRACK": "1", **(extra or {})}
    return env


async def _run(cmd: list[str], cwd: Path, env: dict | None = None, timeout: int = 1800) -> str:
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=cwd, env=env or _env(), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError(f"{' '.join(cmd[:4])} timed out")
    except asyncio.CancelledError:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        raise
    text = out.decode(errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:4])} failed: {text[-800:]}")
    return text


_install_lock = asyncio.Lock()


async def install() -> dict:
    """Download HyperFrames, its agent skills (into the app's own HOME) and Chrome."""
    async with _install_lock:
        st = status()
        if st["code"] in ("node", "ffmpeg"):
            raise RuntimeError(st["detail"])
        HF_HOME.mkdir(parents=True, exist_ok=True)
        npx = shutil.which("npx")
        # HOME points at the app folder so the installer can't touch ~/.claude, ~/.agents, ~/.cursor...
        env = _env({"HOME": str(HF_HOME), "npm_config_cache": str(Path.home() / ".npm")})
        env.pop("HYPERFRAMES_SKIP_SKILLS", None)
        await _run([npx, "-y", f"hyperframes@{HF_VERSION}", "skills", "update", WORKFLOW], HF_HOME, env)
        await _run([npx, "-y", f"hyperframes@{HF_VERSION}", "browser", "ensure"], HF_HOME)
        if not _skills_ready():
            raise RuntimeError("HyperFrames skills did not install")
        return status()


# ------------------------------------------------------------------ project setup

def project_root(pid: str) -> Path:
    return render.project_dir(pid) / "motion"


def _brief(req: dict, brand: dict | None, resolution: str) -> str:
    lang = tts.LANGUAGE_CODES.get(req.get("language", "English"), "en")
    topic = req["topic"].strip()
    message = topic.splitlines()[0][:160].replace('"', "'")
    lines = [
        "---", f"workflow: {WORKFLOW}", "flow: automation", "storyboard: no", f'message: "{message}"',
        "destination: shorts" if resolution == "portrait" else "destination: youtube",
        f"aspect: {ASPECTS[resolution]}", f"language: {lang}", f"length: {int(req.get('duration', 45))}s",
        "angle: concept", "---", "", "## Intent", "", topic,
    ]
    if req.get("style"):
        lines += ["", f"Tone and style: {req['style']}"]
    if req.get("notes"):
        lines += ["", req["notes"]]
    lines += ["", "## Notes", "",
              f"- Narration and all on-screen text are in {req.get('language', 'English')}.",
              "- Fully autonomous run inside AutoClip Studio: no questions, no review pauses.",
              "- Narration comes from the host's `voiceover` tool (see the run instructions), not audio.mjs.",
              "- No sound effects (no SFX catalog is available)."]
    if req.get("context"):
        lines.append("- Source material from the user is in `source/` — base the facts on it.")
    if brand:
        c = brand["colors"]
        lines.append(f"- Brand: {brand['name']}. Colors — primary {c['primary']}, accent {c['accent']}, text {c['text']}.")
        if brand["font"]:
            lines.append(f"- Brand font: `public/brand/{brand['font']['file']}` (family \"{brand['font']['family']}\") — use it for display type.")
        if brand["logo"]:
            lines.append(f"- Brand logo: `public/brand/{brand['logo']['file']}` — show it on the opening and closing frames.")
        if brand["clips"]:
            lines.append("- Brand footage in `public/brand/` may be used where it fits: "
                         + "; ".join(f"{x['file']} ({x['description']})" for x in brand["clips"]))
        site = brand.get("site") or {}
        if site.get("fonts") and not brand["font"]:
            lines.append(f"- The brand's website ({site.get('domain')}) uses the fonts {', '.join(site['fonts'])} — load them "
                         "from Google Fonts (they must cover Vietnamese when the narration is Vietnamese).")
        if site.get("images"):
            lines.append("- Real product visuals captured from the website are in `public/brand/site/` — feature them "
                         "(device frames, zooms, reveals) where the story talks about the product: "
                         + "; ".join(f"{Path(i['file']).name} ({i['description']})" for i in site["images"]))
        guide = brands.prompt_context({**brand, "clips": []})
        if guide:
            lines += ["", "## Customizations", "", guide]
    return "\n".join(lines) + "\n"


async def prepare(pid: str, req: dict, brand: dict | None) -> Path:
    root = project_root(pid)
    resolution = RESOLUTIONS.get(req.get("format", "shorts"), "portrait")
    if not (root / "hyperframes.json").exists():
        if root.exists():
            shutil.rmtree(root)
        await _run([shutil.which("npx"), "-y", f"hyperframes@{HF_VERSION}", "init", str(root), "--non-interactive",
                    "--example=blank", f"--skill={WORKFLOW}", f"--resolution={resolution}"], root.parent)
    skills = root / ".claude" / "skills"
    skills.mkdir(parents=True, exist_ok=True)
    for src in SKILLS_DIR.iterdir():
        dest = skills / src.name
        if src.is_dir() and not dest.exists():
            dest.symlink_to(src.resolve(), target_is_directory=True)
    if not (root / "BRIEF.md").exists():
        (root / "BRIEF.md").write_text(_brief(req, brand, resolution), encoding="utf-8")
    if req.get("context"):
        (root / "source").mkdir(exist_ok=True)
        for m in req["context"]:
            (root / "source" / f"{Path(m['name']).stem}.md").write_text(context.text(m["id"]), encoding="utf-8")
    if brand:
        dest = root / "public" / "brand"
        dest.mkdir(parents=True, exist_ok=True)
        for item in [brand["logo"], brand["font"], *brand["clips"]]:
            if item and (p := brands.asset_path(brand["id"], item["file"])).exists():
                shutil.copyfile(p, dest / item["file"])
        for item in (brand.get("site") or {}).get("images", []):
            if (p := brands.asset_path(brand["id"], item["file"])).exists():
                (dest / "site").mkdir(exist_ok=True)
                shutil.copyfile(p, dest / "site" / p.name)
    return root


# ------------------------------------------------------------------ narration tool

def _voiceover_server(pid: str, root: Path, req: dict, brand: dict | None):
    language = req.get("language", "English")
    voice = req.get("voice") or ""  # the video's pick (edge name or "eleven:<id>"); empty = default
    if not voice and brand and brand.get("voice_name") and brand.get("language") in ("", None, language):
        voice = brand["voice_name"]
    voice = re.sub(r"-(Male|Female)$", "", voice)
    rate = float(req.get("voice_rate", 1.0))

    @tool(
        "voiceover",
        "Record the narration for the video. Pass every SCRIPT.md line (frame number + exact spoken text). "
        "Writes assets/voice/NN.wav per frame and audio_meta.json (voices with frame-relative word timings, plus "
        "the brand music bed when the brand kit has one). Call again with only the changed frames after an edit.",
        {"type": "object", "required": ["lines"], "properties": {"lines": {"type": "array", "items": {
            "type": "object", "required": ["frame", "text"],
            "properties": {"frame": {"type": "integer"}, "text": {"type": "string"}}}}}},
    )
    async def voiceover(args):
        lines = args.get("lines") or []
        meta_path = root / "audio_meta.json"
        meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
        voices = {v["frame"]: v for v in meta.get("voices", [])}
        (root / "assets" / "voice").mkdir(parents=True, exist_ok=True)
        done = []
        for line in lines:
            frame, text = int(line["frame"]), line["text"].strip()
            if not text:
                continue
            mp3 = root / "assets" / "voice" / f"{frame:02d}.mp3"
            wav = mp3.with_suffix(".wav")
            words = await tts.synthesize(text, language, voice, rate, mp3)
            await render._ffmpeg("-i", mp3, "-ac", "1", "-ar", "44100", wav)
            mp3.unlink(missing_ok=True)
            dur = await render.media_duration(wav)
            voices[frame] = {"frame": frame, "path": f"assets/voice/{frame:02d}.wav", "duration_s": round(dur, 3),
                             "words": [{"id": f"w{frame:02d}_{i}", "text": w, "start": round(s, 3), "end": round(e, 3)}
                                       for i, (s, e, w) in enumerate(words)], "text": text}
            done.append(f"frame {frame}: {dur:.2f}s, {len(words)} words")
            activity.add(pid, f"Scene {frame}: “{text[:140]}”", "say")
        bgm = meta.get("bgm")
        tracks = [m for m in (brand or {}).get("music", []) if brands.asset_path(brand["id"], m["file"]).exists()]
        if tracks and not bgm:
            m = tracks[int(render._hash(pid), 16) % len(tracks)]
            (root / "assets" / "music").mkdir(parents=True, exist_ok=True)
            shutil.copyfile(brands.asset_path(brand["id"], m["file"]), root / "assets" / "music" / m["file"])
            bgm = {"path": f"assets/music/{m['file']}", "source": "host", "volume": float(m.get("volume", 0.12)),
                   "query": None, "duration_s": round(await render.media_duration(root / "assets" / "music" / m["file"]), 3)}
        meta = {"bgm": bgm, "bgm_pending": False, "voices": sorted(voices.values(), key=lambda v: v["frame"]),
                "sfx": meta.get("sfx", [])}
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2))
        summary = "\n".join(done) or "nothing to record"
        music = f"music bed: {bgm['path']} (host)" if bgm else "no music bed (set `music: none` in STORYBOARD.md)"
        return {"content": [{"type": "text", "text": f"✓ voiceover → audio_meta.json\n{summary}\n{music}"}]}

    return create_sdk_mcp_server("autoclip", tools=[voiceover])


# ------------------------------------------------------------------ the agent

RULES = """You are running inside AutoClip Studio as a fully autonomous video maker. There is no human to ask.
Quality is the point of this run: the user chose Claude Motion over a quick slideshow. Do the whole workflow — no shortcuts.

Hard rules:
- Work only inside the current project folder.
- Never run `skills update`, `npx skills`, `npm install -g`, `pip install`, `curl`, `wget`, `heygen` or `hyperframes auth/publish/cloud`.
- Narration: once SCRIPT.md is final, DO NOT run `scripts/audio.mjs` generate. Call the `mcp__autoclip__voiceover` tool
  with every narration line (frame number + exact spoken text). It writes the voice files and audio_meta.json in the
  shape the workflow expects. Then continue with `audio.mjs sync-durations`. Skip `fetch-sfx` (no SFX catalog).
- Skip sign-in and preference recording steps (`hyperframes auth status`, `prefs.mjs`) — not available here.
- Do Step 4 (time-coded shot sequence per frame) properly before building.
- Step 5: dispatch ONE `frame-designer` sub-agent PER FRAME (Agent tool, subagent_type "frame-designer") with `_role.md`
  + that frame's packet, as the workflow says. Send ALL of them in a single message so they run in parallel, in the
  foreground: never run them in the background, never sleep, poll or schedule wake-ups while they work — the tool call
  returns when they're done. Never generate frames with a shell script, heredoc template or loop — every frame is
  designed individually.
- Length: keep the total narration within ±15% of the brief's `length` (Vietnamese ≈ 2.6 spoken words per second,
  English ≈ 2.5). Cut lines rather than overrun.
- Visual QA is mandatory (Step 6): run `npx hyperframes snapshot` at every frame's midpoint and OPEN the contact sheet
  image with the Read tool. Fix what you see, re-snapshot, and repeat (max 2 rounds) until none of these remain:
  text or shapes cut off by the canvas edge; content crammed into a corner with the rest of the frame empty;
  overlapping text; unreadable contrast; a frame that is blank or nearly blank; captions colliding with content.
  Keep a safe margin of at least 6% of the canvas on every side.
- Vietnamese (and any language with stacked diacritics): use a font with full Vietnamese coverage (e.g. Be Vietnam Pro,
  Inter, Montserrat via Google Fonts), line-height ≥ 1.2 for display text (≥ 1.3 for all-caps), no negative
  letter-spacing below -0.02em, and never clip text containers vertically — diacritics like Ổ, Ỗ, Ầ must be fully visible.
- Before the final render run `npx hyperframes lint` and `npx hyperframes check` and fix real problems.
- Final render: `npx hyperframes render --quality delivery -o renders/video.mp4`. You are done when that file exists.
- Fewer turns: every turn re-reads your whole context, so do as much as possible per turn. Chain related shell steps
  in ONE Bash call (`a && b && c`), read several files in one call (`cat a b c`, or parallel Read calls), and send
  independent tool calls together in one message. Don't spend a turn just to look around or confirm a step worked
  when the next command would show it anyway.
- Keep your context small: pipe long command output through `| tail -n 40` (or grep for errors), read files with
  offset/limit when you need part of them, and don't re-read a skill or file you've already read. Edit files in
  place instead of printing them.
- Keep the final message short: what the video shows, frame by frame, in one line each."""

DESIGNER = """You design and animate ONE HyperFrames frame for AutoClip Studio, from the `_role.md` and frame packet you
are given. Follow them exactly (look, timing, Vietnamese typography rules, safe margins). Write only your frame's
composition file, inside the project folder. Run `npx hyperframes lint` on it when done and fix real problems. Keep
command output short (`| tail -n 40`) and don't re-read files. Reply with one line: the file you wrote and what it shows."""

# Only the tools the workflow uses: every tool's definition is re-sent on every turn.
ORCHESTRATOR_TOOLS = ["Bash", "Read", "Write", "Edit", "MultiEdit", "Glob", "Grep", "TodoWrite", "Agent", "Task", "Skill"]
DESIGNER_TOOLS = ["Bash", "Read", "Write", "Edit", "MultiEdit", "Glob", "Grep"]

# Waiting tools would make the orchestrator wake up again and again, re-reading its whole context each time.
IDLE_TOOLS = ["ScheduleWakeup", "CronCreate", "CronDelete", "CronList", "Monitor", "RemoteTrigger", "SendMessage",
              "WebFetch", "WebSearch"]


def _bash_ok(cmd: str) -> str | None:
    if DENY.search(cmd):
        return "This command isn't allowed in AutoClip Studio's sandbox."
    return None


def _make_permission(root: Path):
    root_real = root.resolve()

    async def can_use_tool(name, tool_input, _ctx):
        if name == "Bash":
            why = _bash_ok(tool_input.get("command", ""))
            return PermissionResultDeny(message=why) if why else PermissionResultAllow()
        if name in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
            target = Path(tool_input.get("file_path") or tool_input.get("notebook_path") or "")
            target = (root_real / target).resolve() if not target.is_absolute() else target.resolve()
            if root_real != target and root_real not in target.parents:
                return PermissionResultDeny(message="Only files inside the project folder can be changed.")
            return PermissionResultAllow()
        if name in ("WebFetch", "WebSearch"):
            return PermissionResultDeny(message="No web access in this run.")
        return PermissionResultAllow()

    return can_use_tool


class Progress:
    """Turns the agent's tool calls into a stage + percentage for the UI."""

    STAGES = [  # (pattern on tool/input, progress, label)
        (r"build-frame\.mjs|frame\.md", 12, "Choosing the look"),
        (r"STORYBOARD\.md", 20, "Writing the storyboard"),
        (r"voiceover", 32, "Recording the voice-over"),
        (r"sync-durations", 36, "Timing the scenes"),
        (r"^(Task|Agent) |\"file_path\": \"[^\"]*compositions/frames/", 40, "Animating the scenes"),
        (r"captions\.mjs", 72, "Adding captions"),
        (r"assemble-index", 76, "Assembling the video"),
        (r"transitions\.mjs", 79, "Adding transitions"),
        (r"hyperframes(@[\d.]+)?\s+(lint|check|snapshot)", 83, "Checking every frame"),
        (r"hyperframes(@[\d.]+)?\s+render", 88, "Rendering"),
    ]

    def __init__(self, root: Path, report):
        self.root, self.report, self.value, self.label = root, report, 3, "Reading the brief"

    def see(self, block: ToolUseBlock):
        text = f"{block.name} {json.dumps(block.input, ensure_ascii=False)[:500]}"
        for pattern, value, label in self.STAGES:
            if re.search(pattern, text) and value >= self.value:
                self.value, self.label = value, label
        if self.label == "Animating the scenes":
            frames = len(list((self.root / "compositions" / "frames").glob("*.html")))
            planned = max(frames, len(re.findall(r"(?m)^##\s+Frame", self._read("STORYBOARD.md"))),
                          len(list((self.root / ".hyperframes" / "frame-packets").glob("[0-9]*.md"))), 1)
            self.value = max(self.value, min(70, 40 + int(30 * frames / planned)))
            self.label = f"Animating scene {min(frames + 1, planned)} of {planned}"
        self.report(self.value, self.label)

    def _read(self, name):
        p = self.root / name
        return p.read_text(errors="replace") if p.exists() else ""


async def _agent(pid: str, root: Path, req: dict, brand: dict | None, task: str, report, kind: str = "motion",
                 model: str | None = None, effort: str | None = None) -> str:
    log_path = root / "agent.log"
    model, effort = usage.choose("motion", model or req.get("model"), effort or req.get("effort"))
    d_model, d_effort = usage.choose("designer")
    meter = usage.Meter(kind, pid, model, effort)
    options = ClaudeAgentOptions(
        cwd=str(root),
        cli_path=claude.cli_path(),
        env={**claude._env(), "HYPERFRAMES_SKIP_SKILLS": "1", "DO_NOT_TRACK": "1"},
        model=model,
        effort=effort,
        setting_sources=["project"],  # project CLAUDE.md + the HyperFrames skills linked into .claude/skills
        skills="all",
        system_prompt={"type": "preset", "preset": "claude_code", "append": RULES},
        tools=ORCHESTRATOR_TOOLS,
        allowed_tools=["Read", "Glob", "Grep", "TodoWrite", "Task", "Agent", "mcp__autoclip__voiceover"],
        disallowed_tools=IDLE_TOOLS,
        agents={"frame-designer": AgentDefinition(
            description="Designs and animates one video frame from its packet (Step 5). Use one per frame.",
            prompt=DESIGNER, model=d_model, effort=d_effort if d_effort else None, background=False,
            tools=DESIGNER_TOOLS,
            disallowedTools=IDLE_TOOLS + ["Agent", "Task"], maxTurns=60)},
        can_use_tool=_make_permission(root),
        mcp_servers={"autoclip": _voiceover_server(pid, root, req, brand)},
        max_turns=400,
        max_buffer_size=64 * 1024 * 1024,  # snapshots / contact sheets Claude reads arrive as base64
    )

    async def prompt():
        yield {"type": "user", "message": {"role": "user", "content": task}}

    progress = Progress(root, report)
    final = ""
    started = time.time()
    with open(log_path, "a", encoding="utf-8") as logf:
        logf.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} · {model} · effort {effort or 'default'} =====\n{task}\n")

        async def consume():
            nonlocal final
            async for msg in query(prompt=prompt(), options=options):
                meter.see(msg)
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, ToolUseBlock):
                            logf.write(f"[tool] {block.name} {json.dumps(block.input, ensure_ascii=False)[:400]}\n")
                            progress.see(block)
                            if text := activity.describe_tool(block.name, block.input or {}):
                                activity.add(pid, text)
                            img = (block.input or {}).get("file_path", "")
                            if block.name == "Read" and re.search(r"\.(jpe?g|png|webp)$", img, re.I):
                                activity.preview(pid, Path(img))
                        elif isinstance(block, TextBlock) and block.text.strip():
                            logf.write(f"[claude] {block.text.strip()[:2000]}\n")
                            if not getattr(msg, "parent_tool_use_id", None):
                                activity.add(pid, block.text.strip()[:300], "say")
                    logf.flush()
                elif isinstance(msg, ResultMessage):
                    final = msg.result or ""
                    logf.write(f"[result] error={msg.is_error} turns={getattr(msg, 'num_turns', '?')} "
                               f"cost=${msg.total_cost_usd or 0:.4f} secs={time.time() - started:.0f}\n{final[:2000]}\n")
                    if msg.is_error:
                        raise RuntimeError(f"Claude stopped: {final[:300]}")

        try:
            await asyncio.wait_for(consume(), TIMEOUT)
        except BaseException:
            meter.save(ok=False)
            raise
    meter.save()
    return final


# ------------------------------------------------------------------ public API

def _frames(root: Path) -> list[dict]:
    """Scenes for the editor: narration per frame with start/end from audio_meta.json."""
    meta_path = root / "audio_meta.json"
    if not meta_path.exists():
        return []
    voices = sorted(json.loads(meta_path.read_text()).get("voices", []), key=lambda v: v["frame"])
    out, t = [], 0.0
    for v in voices:
        out.append({"id": f"f{v['frame']}", "frame": v["frame"], "text": v.get("text", ""),
                    "start": round(t, 2), "end": round(t + v["duration_s"], 2), "search": "", "clip": None})
        t += v["duration_s"]
    return out


async def _finish(pid: str, root: Path, brand: dict | None, out: Path) -> dict:
    src = root / "renders" / "video.mp4"
    if not src.exists():
        raise RuntimeError("Claude finished without rendering renders/video.mp4 — see motion/agent.log")
    bumpers = [p for p in ((brands.asset_path(brand["id"], brand[k]["file"]) if brand and brand[k] else None)
                           for k in ("intro", "outro")) if p and p.exists()]
    if not bumpers:
        shutil.copyfile(src, out)
    else:
        await _with_bumpers(pid, src, brand, out)
    return {"engine": "motion", "language": None, "scenes": _frames(root)}


async def _with_bumpers(pid: str, src: Path, brand: dict, out: Path):
    """Brand intro/outro around the motion render (re-encoded to one stream)."""
    probe = await render._ffmpeg_probe_size(src)
    parts = []
    if brand["intro"]:
        parts.append(brands.asset_path(brand["id"], brand["intro"]["file"]))
    parts.append(src)
    if brand["outro"]:
        parts.append(brands.asset_path(brand["id"], brand["outro"]["file"]))
    w, h = probe
    inputs, chains = [], []
    for i, p in enumerate(parts):
        inputs += ["-i", p]
        has_audio = await render._has_audio(p)
        chains.append(f"[{i}:v]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},fps=30,setsar=1,format=yuv420p[v{i}]")
        if has_audio:
            chains.append(f"[{i}:a]aformat=sample_rates=44100:channel_layouts=stereo[a{i}]")
        else:
            d = await render.media_duration(p)
            chains.append(f"anullsrc=r=44100:cl=stereo,atrim=0:{d:.3f}[a{i}]")
    graph = ";".join(chains) + ";" + "".join(f"[v{i}][a{i}]" for i in range(len(parts))) + f"concat=n={len(parts)}:v=1:a=1[v][a]"
    await render._ffmpeg(*inputs, "-filter_complex", graph, "-map", "[v]", "-map", "[a]", "-c:v", "libx264",
                         "-preset", "veryfast", "-crf", "18", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", out)


async def create(pid: str, req: dict, out: Path, report, model: str | None = None, effort: str | None = None) -> dict:
    if not status()["ok"]:
        await install()
    brand = brands.get(req.get("brand_id"))
    root = await prepare(pid, req, brand)
    report(2, "Reading the brief")
    task = (f"Make this video with the /{WORKFLOW} workflow, fully autonomous. BRIEF.md is confirmed — start at Step 0 "
            f"with the project already initialized (do not run init again). Follow the run instructions in your system "
            f"prompt for narration, checks and the final render.")
    await _agent(pid, root, req, brand, task, report, "motion", model, effort)
    return await _finish(pid, root, brand, out)


async def edit(pid: str, req: dict, instruction: str, out: Path, report, model: str | None = None,
               effort: str | None = None) -> dict:
    brand = brands.get(req.get("brand_id"))
    root = project_root(pid)
    if not (root / "STORYBOARD.md").exists():
        raise RuntimeError("This video has no motion project to edit — retry it first")
    (root / "renders" / "video.mp4").unlink(missing_ok=True)
    report(5, "Reading your request")
    task = (f"Edit this existing HyperFrames project (an explainer made with /{WORKFLOW}). Change only what this asks, "
            f"keep everything else:\n\n{instruction}\n\nIf narration changes, update SCRIPT.md and call the voiceover tool "
            f"for the changed frames only, then sync durations and reassemble. Re-run lint/check, then render to "
            f"renders/video.mp4.")
    await _agent(pid, root, req, brand, task, report, "motion-edit", model, effort)
    return await _finish(pid, root, brand, out)
