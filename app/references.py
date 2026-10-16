"""References: someone else's short video, from a link (TikTok, YouTube, Instagram, Facebook… via yt-dlp) or a file,
studied for its format so a new video can follow it with your own content.

AutoClip measures what it can (length, size, every cut, the colors), takes one frame per shot, and Claude reads the
frames once (one call) to describe the structure, hook, pacing and look. A video made "like this" gets that
description and the frames as a reference: same format and rhythm, nothing copied — not the footage, characters,
logos, words or music.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import time
import uuid
from pathlib import Path

from . import claude, render
from .config import DATA_DIR

log = logging.getLogger("references")

REF_DIR = DATA_DIR / "references"
MAX_SECONDS = 180  # only the first 3 minutes are studied (short-form)
MAX_BYTES = 500 * 1024 * 1024
MAX_FRAMES = 14
VIDEO_EXT = (".mp4", ".mov", ".webm", ".mkv", ".m4v", ".avi")


def _dir(rid: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{12}", rid or ""):
        raise ValueError("Unknown reference")
    return REF_DIR / rid


def get(rid: str) -> dict | None:
    try:
        path = _dir(rid) / "reference.json"
    except ValueError:
        return None
    return json.loads(path.read_text()) if path.exists() else None


def _save(r: dict):
    path = _dir(r["id"]) / "reference.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(r, ensure_ascii=False, indent=2))
    tmp.replace(path)


def _set(rid: str, **fields) -> dict:
    r = get(rid)
    r.update(fields)
    _save(r)
    return r


def list_all() -> list[dict]:
    if not REF_DIR.exists():
        return []
    return sorted((r for d in REF_DIR.iterdir() if (r := get(d.name))), key=lambda r: -r["created_at"])


def delete(rid: str):
    shutil.rmtree(_dir(rid), ignore_errors=True)


def file(rid: str, name: str) -> Path | None:
    """A file of the reference the UI may show: the video, the contact sheet or one frame."""
    root = _dir(rid).resolve()
    f = (root / name).resolve()
    if not f.is_relative_to(root) or not f.is_file() or not re.fullmatch(r"video\.mp4|sheet\.jpg|frames/[\d.]+s\.jpg", name):
        return None
    return f


def _new(source: dict) -> dict:
    rid = uuid.uuid4().hex[:12]
    _dir(rid).mkdir(parents=True)
    r = {"id": rid, "created_at": time.time(), "status": "queued", "step": None, "error": None, "source": source,
         "name": source.get("title") or source.get("url") or source.get("file") or "Reference", "analysis": None}
    _save(r)
    return r


def from_url(url: str) -> dict:
    url = url.strip()
    if not re.match(r"^https?://\S+$", url) or len(url) > 2000:
        raise ValueError("Paste the link of a video (it starts with https://)")
    return _new({"url": url})


def from_file(data: bytes, filename: str) -> dict:
    ext = Path(filename).suffix.lower()
    if ext not in VIDEO_EXT:
        raise ValueError("Use a video file (MP4, MOV, WebM…)")
    r = _new({"file": Path(filename).name[:120]})
    (_dir(r["id"]) / f"source{ext}").write_bytes(data)
    return r


# ------------------------------------------------------------------ studying it

async def run(rid: str):
    """Download (for a link), measure, take the frames and have Claude read them. Errors land in the reference."""
    try:
        await _run(rid)
    except Exception as e:
        log.warning("reference %s failed", rid, exc_info=True)
        if get(rid):
            _set(rid, status="failed", step=None, error=f"{e.__class__.__name__}: {e}"[:500])


async def _run(rid: str):
    d = _dir(rid)
    r = get(rid)
    if r["source"].get("url") and not any(d.glob("source.*")):
        _set(rid, status="working", step="Downloading the video")
        meta = await asyncio.to_thread(_download, r["source"]["url"], d)
        r = _set(rid, source={**r["source"], **meta}, name=meta.get("title") or r["name"])
    src = next((p for p in d.glob("source.*") if p.suffix.lower() in VIDEO_EXT), None)
    if not src:
        raise RuntimeError("The video couldn't be downloaded from that link — save it and upload the file instead")
    _set(rid, status="working", step="Measuring the cuts")
    video = d / "video.mp4"  # bounded and playable everywhere
    await render._ffmpeg("-i", src, "-t", str(MAX_SECONDS), "-vf", "scale='min(720,iw)':-2", "-c:v", "libx264",
                         "-preset", "veryfast", "-crf", "26", "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", video)
    duration = await render.media_duration(video)
    w, h = await render._ffmpeg_probe_size(video)
    cuts = await _cuts(video)
    shots = [0.0, *cuts]
    ends = [*cuts, duration]
    lengths = [round(b - a, 2) for a, b in zip(shots, ends)]
    stats = {"duration": round(duration, 2), "width": w, "height": h, "shots": len(shots),
             "avg_shot": round(duration / max(1, len(shots)), 2), "cuts": [round(c, 2) for c in cuts], "lengths": lengths,
             "format": "shorts" if h > w * 1.2 else "square" if abs(h - w) <= w * 0.2 else "youtube"}
    _set(rid, step="Taking a frame per shot", stats=stats)
    frames = await _frames(video, shots, ends, d / "frames")
    _sheet(frames, d / "sheet.jpg")
    palette = _palette(frames)
    _set(rid, step="Claude is watching it", palette=palette, frames=[f.name for f in frames])
    analysis = await _analyze(get(rid), d)
    _set(rid, status="ready", step=None, analysis=analysis, name=analysis.get("name") or get(rid)["name"])
    src.unlink(missing_ok=True)  # video.mp4 is kept for the preview


def _download(url: str, d: Path) -> dict:
    import yt_dlp  # only needed for links

    opts = {"outtmpl": str(d / "source.%(ext)s"), "format": "bv*[height<=1080]+ba/b[height<=1080]/b",
            "merge_output_format": "mp4", "noplaylist": True, "quiet": True, "no_warnings": True, "noprogress": True,
            "max_filesize": MAX_BYTES, "ffmpeg_location": render.FFMPEG, "writesubtitles": True,
            "writeautomaticsub": True, "subtitleslangs": ["vi", "en", "en-.*"], "subtitlesformat": "vtt",
            "socket_timeout": 30}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except Exception as e:
        raise RuntimeError(f"Couldn't download that link ({str(e)[:200]}) — save the video and upload the file instead")
    if info.get("duration") and info["duration"] > 20 * 60:
        raise RuntimeError("That video is longer than 20 minutes; use a short one (only the first 3 minutes are studied)")
    captions = ""
    for vtt in sorted(d.glob("source*.vtt")):
        captions = _vtt_text(vtt.read_text(errors="replace"))
        vtt.unlink(missing_ok=True)
        if captions:
            break
    return {"title": (info.get("title") or "")[:200], "uploader": (info.get("uploader") or info.get("channel") or "")[:100],
            "site": info.get("extractor_key") or "", "description": (info.get("description") or "")[:1500],
            "captions": captions[:4000], "webpage_url": info.get("webpage_url") or url}


def _vtt_text(vtt: str) -> str:
    lines, last = [], None
    for line in vtt.splitlines():
        line = re.sub(r"<[^>]+>", "", line).strip()
        if not line or "-->" in line or line.startswith(("WEBVTT", "Kind:", "Language:", "NOTE")) or line.isdigit():
            continue
        if line != last:
            lines.append(line)
        last = line
    return " ".join(lines)


async def _cuts(video: Path) -> list[float]:
    """Where the picture cuts, from ffmpeg's scene score (a change bigger than 30%)."""
    proc = await asyncio.create_subprocess_exec(
        render.FFMPEG, "-hide_banner", "-i", str(video), "-vf", "select='gt(scene,0.3)',showinfo", "-an", "-f", "null", "-",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    _, err = await proc.communicate()
    times = [float(t) for t in re.findall(r"pts_time:([\d.]+)", err.decode(errors="replace"))]
    out = []
    for t in times:  # a flash or a fast push can score twice in a row: keep cuts at least 0.25 s apart
        if t > 0.2 and (not out or t - out[-1] >= 0.25):
            out.append(t)
    return out


def frame_times(starts: list[float], ends: list[float], duration: float) -> list[float]:
    """When to take frames: the hook (0.3 s) and the middle of each shot. A video with few hard cuts (smooth
    transitions, one long take) also gets frames every few seconds, so every beat is seen. At most MAX_FRAMES."""
    times = [0.3, *((a + b) / 2 for a, b in zip(starts[1:], ends[1:]))]
    if len(starts) < duration / 4:
        step = max(2.0, duration / MAX_FRAMES)
        times += [step * k for k in range(1, int(duration / step) + 1) if step * k < duration - 0.3]
    times = sorted(times)
    kept = []
    for t in times:
        if not kept or t - kept[-1] >= 0.8:
            kept.append(t)
    if len(kept) > MAX_FRAMES:
        step = (len(kept) - 1) / (MAX_FRAMES - 1)
        kept = [kept[round(i * step)] for i in range(MAX_FRAMES)]
    return kept


async def _frames(video: Path, starts: list[float], ends: list[float], dest: Path) -> list[Path]:
    dest.mkdir(exist_ok=True)
    out = []
    for t in frame_times(starts, ends, ends[-1] if ends else 0):
        f = dest / f"{t:.1f}s.jpg"
        await render._ffmpeg("-ss", f"{t:.2f}", "-i", video, "-frames:v", "1", "-vf", "scale=540:-2", "-q:v", "4", f)
        if f.exists():
            out.append(f)
    return out


def _sheet(frames: list[Path], dest: Path):
    from PIL import Image, ImageDraw

    if not frames:
        return
    ims = [Image.open(f).convert("RGB") for f in frames]
    w = 180
    ims = [im.resize((w, round(im.height * w / im.width))) for im in ims]
    h = max(im.height for im in ims)
    cols = min(7, len(ims))
    rows = (len(ims) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * (w + 6) + 6, rows * (h + 24) + 6), (18, 18, 22))
    draw = ImageDraw.Draw(sheet)
    for n, (im, f) in enumerate(zip(ims, frames)):
        x, y = 6 + (n % cols) * (w + 6), 6 + (n // cols) * (h + 24)
        sheet.paste(im, (x, y))
        draw.text((x + 4, y + h + 5), f.stem, fill=(220, 220, 220))
    sheet.save(dest, quality=85)


def _palette(frames: list[Path], n: int = 6) -> list[str]:
    """The video's main colors, measured on its frames (most used first)."""
    from PIL import Image

    if not frames:
        return []
    strip = Image.new("RGB", (64 * len(frames), 64))
    for i, f in enumerate(frames):
        strip.paste(Image.open(f).convert("RGB").resize((64, 64)), (64 * i, 0))
    q = strip.quantize(colors=12, method=Image.Quantize.MEDIANCUT)
    pal = q.getpalette()[:36]
    out: list[tuple[int, int, int]] = []
    for _, i in sorted(q.getcolors(), reverse=True):  # most used first; near-duplicates merged
        c = tuple(pal[i * 3:i * 3 + 3])
        if len(c) == 3 and all(sum((a - b) ** 2 for a, b in zip(c, o)) > 40 ** 2 for o in out):
            out.append(c)
    return ["#%02x%02x%02x" % c for c in out[:n]]


ANALYZE_SYSTEM = """You study short videos made by others to learn their format: structure, hook, pacing and look. You
describe the format so a different video, with original content, can follow it. Reply with JSON only."""

ANALYZE_PROMPT = """Study this short video{who}. In this folder: `sheet.jpg` (every frame below, in order, with its
second) and `frames/<second>s.jpg` (one frame per shot). Read `sheet.jpg` first, then the frames you need to see
details (type, layout, captions) — at most 10.

Measured: {duration}s, {width}×{height}, {shots} shots (average {avg_shot}s per shot), hard cuts at {cuts}
(smooth transitions such as fades and slides aren't counted as cuts).
Main colors (measured): {palette}.
{title}{description}{captions}
Describe its format — what makes it work — as something a new video on another topic could follow. Be concrete
(sizes, positions, how text appears, what moves), and honest about what you can't tell from still frames.

Reply with JSON:
{{"name": "short name for this format, e.g. 'Bold quiz countdown'",
  "summary": "one or two sentences: what kind of video it is and why it holds attention",
  "structure": [{{"beat": "Hook", "start": 0, "end": 2.5, "what": "what happens on screen and in the words", "why": "what it does for the viewer"}}],
  "hook": "how the first 2 seconds stop the scroll",
  "pacing": "rhythm: cut rate, holds, where it speeds up or slows down",
  "look": {{"palette": "colors and their roles (use the measured hex values)", "type": "fonts (kind, weight, case, size)",
           "layout": "framing, safe areas, where text sits", "motion": "how things enter, move and transition",
           "captions": "caption style, or none", "imagery": "footage / illustration / UI / talking head…"}},
  "sound": "what the sound probably is (voice-over, trend audio, music mood), if you can tell",
  "recipe": ["5 to 8 short rules to make a video in this format"],
  "avoid": ["what belongs to this creator and must not be copied: characters, mascots, logos, names, exact lines"]}}"""


async def _analyze(r: dict, d: Path) -> dict:
    s, src = r["stats"], r["source"]
    prompt = ANALYZE_PROMPT.format(
        who=f" by {src['uploader']} on {src['site']}" if src.get("uploader") else "",
        duration=s["duration"], width=s["width"], height=s["height"], shots=s["shots"], avg_shot=s["avg_shot"],
        cuts=", ".join(f"{c}s" for c in s["cuts"][:60]) or "none", palette=", ".join(r.get("palette") or []) or "unknown",
        title=f"Title: {src['title']}\n" if src.get("title") else "",
        description=f"Description: {src['description'][:800]}\n" if src.get("description") else "",
        captions=f"Its captions/subtitles (what is said): {src['captions'][:3000]}\n" if src.get("captions") else
        "No transcript: the words come only from on-screen text.\n")
    data = await claude.ask_json(prompt, ANALYZE_SYSTEM, kind="reference", tools=["Read", "Glob"], cwd=str(d),
                                 max_turns=16, effort="low")
    if not data.get("structure") or not data.get("recipe"):
        raise RuntimeError("Claude couldn't describe this video — try again")
    data["structure"] = [b for b in data["structure"] if isinstance(b, dict)][:12]
    data["recipe"] = [str(x) for x in data.get("recipe") or []][:10]
    data["avoid"] = [str(x) for x in data.get("avoid") or []][:8]
    return data


# ------------------------------------------------------------------ using it

def _structure(a: dict, total: float, length: float) -> list[str]:
    k = length / total if total else 1
    return [f"  - {b.get('beat', '')} ({float(b.get('start') or 0) * k:.1f}–{float(b.get('end') or 0) * k:.1f}s): "
            f"{b.get('what', '')}" for b in a.get("structure") or []]


def brief_lines(rid: str | None, root: Path, length: float) -> list[str]:
    """Claude Motion: the reference's format (and its frames, copied into the project) for the brief."""
    r = get(rid) if rid else None
    if not r or r["status"] != "ready":
        return []
    a, s = r["analysis"], r["stats"]
    dest = root / "reference"
    dest.mkdir(exist_ok=True)
    if (_dir(rid) / "sheet.jpg").exists():
        shutil.copyfile(_dir(rid) / "sheet.jpg", dest / "sheet.jpg")
    shutil.copytree(_dir(rid) / "frames", dest / "frames", dirs_exist_ok=True)
    look = a.get("look") or {}
    lines = ["", "## Reference video: follow its format, make the content your own",
             f"{a.get('name', '')}: {a.get('summary', '')}",
             f"- Look at `reference/sheet.jpg` (a frame per shot, with its second) and `reference/frames/` before you design.",
             f"- Structure, scaled to this video's length:", *_structure(a, s["duration"], length),
             f"- Hook: {a.get('hook', '')}",
             f"- Pacing: {a.get('pacing', '')}" + (f" It cuts every {s['avg_shot']}s on average; keep a similar rhythm."
                                                  if s["shots"] > 1 else " It has no hard cuts; keep its rhythm of scene changes."),
             f"- Look: palette {look.get('palette', '')}; type {look.get('type', '')}; layout {look.get('layout', '')}; "
             f"motion {look.get('motion', '')}; captions {look.get('captions', '')}; imagery {look.get('imagery', '')}.",
             "- Recipe:", *[f"  - {x}" for x in a.get("recipe") or []],
             "- Make everything original. Never reproduce its footage, characters, mascots, logos, names, exact lines or "
             "music" + (": " + "; ".join(a["avoid"]) if a.get("avoid") else "") + ".",
             "- A brand kit, a chosen visual style or the brief's own instructions win over the reference's look; its "
             "structure and pacing still apply."]
    return lines


def script_notes(rid: str | None, length: float) -> str:
    """Quick engine: the reference's structure, hook and pacing for the script writer."""
    r = get(rid) if rid else None
    if not r or r["status"] != "ready":
        return ""
    a, s = r["analysis"], r["stats"]
    return "\n".join(["Follow the format of a reference video (original content, nothing copied):",
                      f"{a.get('name', '')}: {a.get('summary', '')}", "Structure:", *_structure(a, s["duration"], length),
                      f"Hook: {a.get('hook', '')}", f"Pacing: {a.get('pacing', '')}",
                      *[f"- {x}" for x in a.get("recipe") or []]])
