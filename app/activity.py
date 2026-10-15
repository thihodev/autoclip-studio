"""Live activity per video: what Claude is doing right now, for the UI.

Kept in memory (recent events + the latest preview image) — it's a window
into a running job, not history. Versions and usage keep the durable record.
"""
import re
import shutil
import time
from collections import deque
from pathlib import Path

_events: dict[str, deque] = {}
_preview: dict[str, str] = {}


def add(pid: str, text: str, kind: str = "step"):
    """kind: step (an action) | say (Claude's own words) | done | warn."""
    text = re.sub(r"\s+", " ", text or "").strip()
    if not text:
        return
    q = _events.setdefault(pid, deque(maxlen=60))
    if q and q[-1]["text"] == text:
        return
    q.append({"at": time.time(), "kind": kind, "text": text[:400]})


def preview(pid: str, image: Path):
    """Show this image (a snapshot / contact sheet / footage frame) as the live preview."""
    from . import render

    if not image.exists():
        return
    dest = render.project_dir(pid) / "media" / f"live_{int(time.time() * 1000)}{image.suffix.lower()}"
    shutil.copyfile(image, dest)
    old = _preview.get(pid)
    _preview[pid] = dest.name
    if old and old != dest.name:
        (render.project_dir(pid) / "media" / old).unlink(missing_ok=True)


def get(pid: str) -> dict:
    return {"events": list(_events.get(pid, [])), "preview": _preview.get(pid)}


def clear(pid: str):
    _events.pop(pid, None)
    _preview.pop(pid, None)


def describe_tool(name: str, inp: dict) -> str | None:
    """A short human sentence for an agent tool call (None = not worth showing)."""
    if name in ("Agent", "Task"):
        return f"Started a designer for: {inp.get('description', 'a scene')}"
    if name == "mcp__autoclip__voiceover":
        return f"Recording the voice-over for {len(inp.get('lines') or [])} scene(s)"
    if name in ("Write", "Edit", "MultiEdit"):
        f = Path(inp.get("file_path", "")).name
        labels = {"STORYBOARD.md": "the storyboard", "SCRIPT.md": "the script", "BRIEF.md": "the brief",
                  "frame.md": "the design system", "index.html": "the video timeline"}
        what = labels.get(f) or (f"scene file {f}" if f.endswith(".html") else f)
        return f"{'Writing' if name == 'Write' else 'Editing'} {what}"
    if name == "Bash":
        cmd = inp.get("command", "")
        for pat, text in [
            (r"build-frame\.mjs.*--preset\s+(\S+)", "Applying the “{0}” design preset"),
            (r"hyperframes(@[\d.]+)?\s+render", "Rendering the final video"),
            (r"hyperframes(@[\d.]+)?\s+snapshot", "Taking snapshots to review every frame"),
            (r"hyperframes(@[\d.]+)?\s+check", "Checking layout, contrast and timing"),
            (r"hyperframes(@[\d.]+)?\s+lint", "Linting the composition"),
            (r"assemble-index", "Assembling the scenes into one timeline"),
            (r"transitions\.mjs", "Adding transitions between scenes"),
            (r"captions\.mjs", "Building the captions"),
            (r"sync-durations", "Timing each scene to the voice-over"),
            (r"frame-packets", "Preparing a brief for each scene"),
            (r"hyperframes(@[\d.]+)?\s+catalog", "Searching the motion block catalog"),
            (r"hyperframes(@[\d.]+)?\s+add\s+(\S+)", "Installing the “{0}” motion block"),
        ]:
            m = re.search(pat, cmd)
            if m:
                groups = [g for g in m.groups() if g and not g.startswith("@")]
                return text.format(*(groups or [""]), *([""] * 2))
        return None
    if name == "Read":
        f = inp.get("file_path", "")
        if re.search(r"\.(jpe?g|png|webp)$", f, re.I):
            return "Looking at the frames" if "contact" in f else "Looking at a frame"
        if "/skills/" in f:
            return None
        return None
    return None
