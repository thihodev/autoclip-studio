"""Music beds and sound effects.

- Sound effects are synthesized with ffmpeg on first use (no downloads, no licenses to track): whoosh, pop,
  click, tick, riser, impact, ding. The Quick engine puts a whoosh on every cut; Claude Motion gets the pack in
  its project and places cues itself.
- Music: the brand kit's tracks, a library of tracks you upload (with a mood), or tracks generated with
  ElevenLabs Music. A video uses "auto" (brand music, else a library track), "none", or a chosen library track.
"""
import asyncio
import json
import re
import shutil
import time
import uuid
from pathlib import Path

import httpx

from . import brands, db, render, tts
from .config import DATA_DIR

SFX_DIR = DATA_DIR / "sfx"
MUSIC_DIR = DATA_DIR / "music"
MUSIC_EXT = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}

# name: (what it's for, ffmpeg lavfi source)
SFX = {
    "whoosh": ("transitions and fast moves", "anoisesrc=d=0.7:c=pink:a=0.6,highpass=f=350,lowpass=f=4200,"
               "afade=t=in:d=0.3:curve=exp,afade=t=out:st=0.32:d=0.38"),
    "pop": ("an element popping in", "aevalsrc='0.8*sin(2*PI*(900-2600*t)*t)*exp(-28*t)':d=0.16"),
    "click": ("a UI tap or a cut", "aevalsrc='0.9*(random(0)*2-1)*exp(-180*t)':d=0.05,highpass=f=1500"),
    "tick": ("counters and small steps", "aevalsrc='0.55*sin(2*PI*2200*t)*exp(-70*t)':d=0.07"),
    "riser": ("building up to a reveal", "aevalsrc='(0.28*sin(2*PI*(180+260*t*t)*t)+0.18*(random(0)*2-1))*(t/1.6)^2':d=1.6,"
              "highpass=f=180,afade=t=out:st=1.5:d=0.1"),
    "impact": ("a big statement landing", "aevalsrc='0.9*sin(2*PI*52*t)*exp(-4.5*t)+0.35*(random(0)*2-1)*exp(-22*t)':d=1.0,"
               "lowpass=f=900"),
    "ding": ("a check, a win, a correct answer", "aevalsrc='0.45*sin(2*PI*1318*t)*exp(-4.5*t)+0.2*sin(2*PI*2637*t)*exp(-6*t)':d=1.1"),
}


async def _peak_db(path: Path) -> float:
    proc = await asyncio.create_subprocess_exec(render.FFMPEG, "-hide_banner", "-i", str(path), "-af", "volumedetect",
                                                "-f", "null", "-", stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE)
    _, err = await proc.communicate()
    m = re.search(r"max_volume: (-?[\d.]+) dB", err.decode(errors="replace"))
    return float(m.group(1)) if m else 0.0


async def sfx_pack() -> Path:
    """The sound effects as WAV files, peak-normalized to -3 dB (made once)."""
    SFX_DIR.mkdir(parents=True, exist_ok=True)
    for name, (_, src) in SFX.items():
        out = SFX_DIR / f"{name}.wav"
        if out.exists():
            continue
        raw = SFX_DIR / f"{name}.raw.wav"
        await render._ffmpeg("-f", "lavfi", "-i", src, "-ac", "2", "-ar", "44100", raw)
        gain = -3.0 - await _peak_db(raw)
        await render._ffmpeg("-i", raw, "-af", f"volume={gain:.1f}dB", out)
        raw.unlink(missing_ok=True)
    return SFX_DIR


def sfx_list() -> list[dict]:
    return [{"name": n, "use": use, "url": f"/api/sfx/{n}.wav"} for n, (use, _) in SFX.items()]


# ---------- music library ----------

def music_list() -> list[dict]:
    return [m for m in (db.get_setting("music_library", []) or []) if (MUSIC_DIR / m["file"]).exists()]


def _save_list(items: list[dict]):
    db.set_setting("music_library", items)


async def music_add(src: Path, name: str, mood: str = "", source: str = "upload") -> dict:
    ext = src.suffix.lower()
    if ext not in MUSIC_EXT:
        raise ValueError("Use an MP3, WAV, M4A, AAC, OGG or FLAC file")
    MUSIC_DIR.mkdir(parents=True, exist_ok=True)
    file = f"{uuid.uuid4().hex[:10]}{ext}"
    shutil.copyfile(src, MUSIC_DIR / file)
    try:
        dur = await render.media_duration(MUSIC_DIR / file)
    except Exception:
        (MUSIC_DIR / file).unlink(missing_ok=True)
        raise ValueError("That file doesn't look like audio")
    item = {"file": file, "name": (name or Path(src).stem)[:80], "mood": mood[:60], "duration": round(dur, 1),
            "source": source, "at": time.time()}
    _save_list(music_list() + [item])
    return item


def music_remove(file: str):
    _save_list([m for m in music_list() if m["file"] != file])
    (MUSIC_DIR / Path(file).name).unlink(missing_ok=True)


async def music_generate(prompt: str, seconds: int) -> dict:
    """A track from ElevenLabs Music (uses the ElevenLabs key from Connections)."""
    key = tts.cfg()["elevenlabs_api_key"]
    if not key:
        raise ValueError("Connect ElevenLabs in Connections → Voice-over to generate music")
    async with httpx.AsyncClient(timeout=300) as c:
        r = await c.post(f"{tts.API}/music", headers={"xi-api-key": key},
                         json={"prompt": prompt, "music_length_ms": max(10, min(300, seconds)) * 1000})
    if r.status_code >= 400:
        raise ValueError(tts.eleven_error(r))
    tmp = DATA_DIR / "work" / f"music_{uuid.uuid4().hex[:8]}.mp3"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_bytes(r.content)
    try:
        name = re.sub(r"\s+", " ", prompt).strip()[:60]
        return await music_add(tmp, name, mood=name, source="elevenlabs")
    finally:
        tmp.unlink(missing_ok=True)


def pick(choice: str | None, brand: dict | None, pid: str) -> dict | None:
    """The music bed for a video → {path, volume} or None.
    choice: "" / "auto" (brand music, else a library track), "none", or a library file name."""
    if choice == "none":
        return None
    if choice and choice != "auto":
        p = MUSIC_DIR / Path(choice).name
        return {"path": p, "volume": 0.12} if p.exists() else None
    if brand:
        tracks = [m for m in brand.get("music") or [] if brands.asset_path(brand["id"], m["file"]).exists()]
        if tracks:
            m = tracks[int(render._hash(pid), 16) % len(tracks)]
            return {"path": brands.asset_path(brand["id"], m["file"]), "volume": float(m.get("volume", 0.12))}
    lib = music_list()
    if lib:
        m = lib[int(render._hash(pid), 16) % len(lib)]
        return {"path": MUSIC_DIR / m["file"], "volume": 0.12}
    return None


async def sfx_track(cues: list[float], total: float, dest: Path, name: str = "whoosh", volume: float = 0.35) -> Path | None:
    """One stereo WAV with the effect at each cue time (seconds), for the Quick engine's mix."""
    cues = [c for c in cues if 0 <= c < total]
    if not cues:
        return None
    src = (await sfx_pack()) / f"{name}.wav"
    inputs, labels = [], []
    for i, c in enumerate(cues):
        inputs += ["-i", src]
        ms = int(c * 1000)
        labels.append(f"[{i}:a]adelay={ms}|{ms}[d{i}]")
    graph = ";".join(labels) + ";" + "".join(f"[d{i}]" for i in range(len(cues))) + \
        f"amix=inputs={len(cues)}:normalize=0,volume={volume},apad=whole_dur={total:.3f}[a]"
    await render._ffmpeg(*inputs, "-filter_complex", graph, "-map", "[a]", "-ac", "2", "-ar", "44100", dest)
    return dest


def motion_brief(root: Path) -> list[str]:
    """Claude Motion: the SFX pack in the project and how to cue it."""
    dest = root / "assets" / "sfx"
    dest.mkdir(parents=True, exist_ok=True)
    for name in SFX:
        if (SFX_DIR / f"{name}.wav").exists():
            shutil.copyfile(SFX_DIR / f"{name}.wav", dest / f"{name}.wav")
    return [
        "", "## Sound effects",
        "AutoClip's sound effects are in `assets/sfx/`: " + "; ".join(f"`{n}.wav` ({use})" for n, (use, _) in SFX.items()) + ".",
        "- After the voiceover, add cues to `audio_meta.json` → `sfx`: "
        '`{"frame": N, "file": "assets/sfx/whoosh.wav", "offset_s": seconds into that frame, "duration_s": 1, '
        '"volume": 0.3}` (0.25–0.45). Typical: a whoosh on each transition, a pop or tick when key elements appear, '
        "a riser before the big reveal, an impact on the boldest claim, a ding on a check or a result. 1–3 per frame — "
        "sound supports the picture, never covers the voice. Don't run `fetch-sfx`.",
    ]


def library_json() -> str:
    return json.dumps(music_list(), ensure_ascii=False)
