"""Built-in renderer: scene-based, ffmpeg only, no Docker.

Each scene = one voice-over line (edge-tts, with word timings) over one stock
clip (Pexels/Pixabay or an upload). Every scene is encoded once with its
captions burned in (libass) and cached by content hash, so an edit only
re-encodes the scenes it touched. Scenes are then concatenated without
re-encoding and muxed with the voice track.

ffmpeg comes from imageio-ffmpeg (bundled binary with libass/freetype).
"""
import asyncio
import hashlib
import json
import logging
import re
import shutil
from pathlib import Path

import edge_tts
import httpx
import imageio_ffmpeg

from . import activity, brands, settings, tts
from .config import DATA_DIR, FONT_PATH

log = logging.getLogger("render")

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
FONT_NAME = "Be Vietnam Pro"

SIZES = {"shorts": (1080, 1920), "tiktok": (1080, 1920), "youtube": (1920, 1080), "square": (1080, 1080)}

# Languages written in Latin script (the bundled caption font covers them, incl. Vietnamese).
DEFAULT_VOICES = {
    "English": "en-US-AndrewMultilingualNeural",
    "Vietnamese": "vi-VN-HoaiMyNeural",
    "Spanish": "es-MX-JorgeNeural",
    "Portuguese": "pt-BR-AntonioNeural",
    "French": "fr-FR-HenriNeural",
    "German": "de-DE-ConradNeural",
    "Italian": "it-IT-DiegoNeural",
    "Indonesian": "id-ID-ArdiNeural",
}

LANGUAGE_LOCALES = {
    "English": "en", "Vietnamese": "vi", "Spanish": "es", "Portuguese": "pt",
    "French": "fr", "German": "de", "Italian": "it", "Indonesian": "id",
}

CAPTION_PRESETS = {
    # name: font scale, uppercase, outline, box, highlight word
    "bold": {"scale": 0.095, "upper": True, "outline": 0.09, "box": False, "highlight": True},
    "box": {"scale": 0.072, "upper": False, "outline": 0.22, "box": True, "highlight": False},
    "minimal": {"scale": 0.062, "upper": False, "outline": 0.05, "box": False, "highlight": False},
    "none": None,
}

DEFAULT_CAPTIONS = {"preset": "bold", "position": 0.68, "color": "#FFFFFF", "highlight_color": "#FFE14D"}


def project_dir(pid: str) -> Path:
    d = DATA_DIR / "projects" / pid
    (d / "media").mkdir(parents=True, exist_ok=True)
    (d / "cache").mkdir(parents=True, exist_ok=True)
    return d


def _hash(*parts) -> str:
    return hashlib.sha1(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:16]


_running: set = set()  # ffmpeg processes, killed on shutdown


def kill_running():
    for proc in list(_running):
        if proc.returncode is None:
            proc.kill()


async def _ffmpeg(*args, cwd: Path | None = None) -> str:
    proc = await asyncio.create_subprocess_exec(
        FFMPEG, "-hide_banner", "-loglevel", "error", "-y", *map(str, args),
        cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    _running.add(proc)
    try:
        _, err = await proc.communicate()
    except asyncio.CancelledError:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        raise
    finally:
        _running.discard(proc)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {err.decode(errors='replace')[-800:]}")
    return err.decode(errors="replace")


async def media_duration(path: Path) -> float:
    proc = await asyncio.create_subprocess_exec(
        FFMPEG, "-hide_banner", "-i", str(path), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", err.decode(errors="replace"))
    if not m:
        raise RuntimeError(f"Cannot read duration of {path.name}")
    h, mnt, s = m.groups()
    return int(h) * 3600 + int(mnt) * 60 + float(s)


# ---------- footage ----------

def footage_status() -> dict:
    cfg = settings.get("footage")
    provider = cfg["provider"]
    key = cfg[f"{provider}_api_key"]
    return {"ok": bool(key), "detail": f"{provider.title()} key set" if key else "Not set — scenes use motion backgrounds"}


# Animated gradient backgrounds: used when a scene has no footage (no stock key,
# no results, or picked by the user). Pairs are dark enough for white captions.
MOTION_PALETTES = [
    ("#1E3C72", "#2A5298"), ("#42275A", "#734B6D"), ("#0F2027", "#2C5364"), ("#B2153A", "#E2531F"),
    ("#134E5E", "#2F7D5B"), ("#3A1C71", "#B5456A"), ("#000428", "#004E92"), ("#232526", "#4B4F55"),
]


def _shade(hex_color: str, f: float) -> str:
    h = hex_color.lstrip("#")
    rgb = [int(h[i:i + 2], 16) for i in (0, 2, 4)]
    return "#" + "".join(f"{max(0, min(255, int(c * f))):02X}" for c in rgb)


def motion_clip(index: int, brand: dict | None = None) -> dict:
    if brand:
        p = brand["colors"]["primary"]
        c0, c1 = [(_shade(p, 0.35), _shade(p, 0.85)), (_shade(p, 0.2), _shade(p, 0.6))][index % 2]
    else:
        c0, c1 = MOTION_PALETTES[index % len(MOTION_PALETTES)]
    return {"id": f"motion:{c0}{c1}:{index}", "provider": "motion", "colors": [c0, c1], "seed": index + 1}


def _gradient_src(clip: dict, size, duration: float | None = None) -> str:
    w, h = size
    c0, c1 = (c.lstrip("#") for c in clip["colors"])
    src = f"gradients=s={w}x{h}:c0=0x{c0}:c1=0x{c1}:nb_colors=2:type=linear:speed=0.012:seed={clip.get('seed', 1)}:rate=30"
    return src + (f":d={duration:.3f}" if duration else "")


async def motion_thumbnail(pid: str, clip: dict) -> str:
    dest = project_dir(pid) / "media" / f"motion_{_hash(clip['colors'], clip.get('seed'))}.jpg"
    if not dest.exists():
        await _ffmpeg("-f", "lavfi", "-i", _gradient_src(clip, (270, 480)), "-frames:v", "1", dest)
    return str(dest)


async def test_footage_key(provider: str, key: str) -> str | None:
    """→ None if the key works, else an error message."""
    if not key:
        return None
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            if provider == "pexels":
                r = await c.get("https://api.pexels.com/videos/search", params={"query": "city", "per_page": 1},
                                headers={"Authorization": key})
            else:
                r = await c.get("https://pixabay.com/api/videos/", params={"key": key, "q": "city", "per_page": 3})
    except httpx.HTTPError as e:
        return f"Could not reach {provider.title()} ({e.__class__.__name__})"
    if r.status_code in (400, 401, 403):
        return f"{provider.title()} rejected this API key"
    if r.status_code >= 400:
        return f"{provider.title()} error {r.status_code}"
    return None


def _orientation(size):
    w, h = size
    return "portrait" if h > w else "landscape" if w > h else "square"


def _pick_file(files: list[dict], size) -> dict:
    w, h = size
    big_enough = [f for f in files if f["width"] >= w * 0.6 and f["height"] >= h * 0.6]
    if big_enough:
        return min(big_enough, key=lambda f: f["width"] * f["height"])
    return max(files, key=lambda f: f["width"] * f["height"])


def _footage_error(r: httpx.Response, provider: str):
    if r.status_code in (400, 401, 403):
        raise RuntimeError(f"{provider.title()} rejected the API key — update it in Connections → Stock footage")
    if r.status_code == 429:
        raise RuntimeError(f"{provider.title()} rate limit reached — try again in a few minutes")
    r.raise_for_status()


async def search_footage(term: str, size, per_page: int = 8) -> list[dict]:
    """→ [{id, url, thumb, duration, width, height, provider}]"""
    cfg = settings.get("footage")
    provider = cfg["provider"]
    key = cfg[f"{provider}_api_key"]
    if not key:
        raise RuntimeError(f"Add a {provider.title()} API key in Connections → Stock footage")
    out = []
    async with httpx.AsyncClient(timeout=30) as c:
        if provider == "pexels":
            r = await c.get(
                "https://api.pexels.com/videos/search",
                params={"query": term, "per_page": per_page, "orientation": _orientation(size), "size": "medium"},
                headers={"Authorization": key},
            )
            _footage_error(r, provider)
            for v in r.json().get("videos", []):
                files = [
                    {"url": f["link"], "width": f["width"], "height": f["height"]}
                    for f in v.get("video_files", []) if f.get("file_type") == "video/mp4" and f.get("height")
                ]
                if files:
                    f = _pick_file(files, size)
                    out.append({"id": f"pexels:{v['id']}", "url": f["url"], "thumb": v.get("image"),
                                "duration": v.get("duration", 0), "width": f["width"], "height": f["height"],
                                "provider": "pexels"})
        else:
            r = await c.get("https://pixabay.com/api/videos/", params={"key": key, "q": term, "per_page": max(3, per_page)})
            _footage_error(r, provider)
            for v in r.json().get("hits", []):
                vids = v.get("videos") or {}
                files = [{"url": f["url"], "width": f["width"], "height": f["height"]}
                         for f in vids.values() if f.get("url") and f.get("height")]
                if files:
                    f = _pick_file(files, size)
                    thumb = next((x.get("thumbnail") for x in vids.values() if x.get("thumbnail")), None)
                    out.append({"id": f"pixabay:{v['id']}", "url": f["url"], "thumb": thumb,
                                "duration": v.get("duration", 0), "width": f["width"], "height": f["height"],
                                "provider": "pixabay"})
    return out


async def ensure_clip_file(pid: str, clip: dict) -> Path:
    """Download a remote clip into the project's media folder (once)."""
    if clip.get("path"):
        return Path(clip["path"])
    dest = project_dir(pid) / "media" / f"{_hash(clip['url'])}.mp4"
    if not dest.exists():
        tmp = dest.with_suffix(".part")
        async with httpx.AsyncClient(timeout=None, follow_redirects=True) as c:
            async with c.stream("GET", clip["url"]) as r:
                r.raise_for_status()
                with open(tmp, "wb") as f:
                    async for chunk in r.aiter_bytes():
                        f.write(chunk)
        tmp.rename(dest)
    clip["path"] = str(dest)
    return dest


async def thumbnail(video: Path) -> Path:
    thumb = video.with_suffix(".jpg")
    if not thumb.exists():
        await _ffmpeg("-ss", "0.5", "-i", video, "-frames:v", "1", "-vf", "scale=270:-2", thumb)
    return thumb


# ---------- voice ----------

async def synthesize(text: str, voice: str, rate: float, dest: Path) -> list[list]:
    pct = int(round((rate - 1.0) * 100))
    comm = edge_tts.Communicate(text, voice, rate=f"{pct:+d}%", boundary="WordBoundary")
    words = []
    with open(dest, "wb") as f:
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                start = chunk["offset"] / 1e7
                words.append([start, start + chunk["duration"] / 1e7, chunk["text"]])
    return words


async def scene_voice(pid: str, text: str, voice: str, rate: float, language: str = "English") -> tuple[Path, list, float]:
    """Cached TTS for one scene → (mp3, words, duration)."""
    cache = project_dir(pid) / "cache"
    key = _hash("tts", text, voice, rate, language, tts.cache_key())
    mp3, meta = cache / f"tts_{key}.mp3", cache / f"tts_{key}.json"
    if not (mp3.exists() and meta.exists()):
        words = await tts.synthesize(text, language, voice, rate, mp3)
        meta.write_text(json.dumps({"words": words, "duration": await media_duration(mp3)}))
    data = json.loads(meta.read_text())
    return mp3, data["words"], data["duration"]


# ---------- captions ----------

def _norm(s: str) -> str:
    return re.sub(r"[^\w]", "", s.lower())


def attach_punctuation(words: list, text: str) -> list:
    """edge-tts drops punctuation; recover it from the original text."""
    tokens, ptr, out = text.split(), 0, []
    for start, end, w in words:
        shown = w
        for j in range(ptr, min(ptr + 4, len(tokens))):
            if _norm(tokens[j]) and (_norm(tokens[j]) == _norm(w) or _norm(tokens[j]).startswith(_norm(w))):
                shown, ptr = tokens[j], j + 1
                break
        out.append([start, end, shown])
    return out


def caption_chunks(words: list, scene_end: float, max_words=3, max_chars=18) -> list:
    """→ [(start, end, [[w_start, w_end, word], ...]), ...]"""
    groups, cur = [], []
    for w in words:
        cur.append(w)
        text = " ".join(x[2] for x in cur)
        if len(cur) >= max_words or len(text) >= max_chars or re.search(r"[.!?,;:…]$", w[2]):
            groups.append(cur)
            cur = []
    if cur:
        groups.append(cur)
    out = []
    for i, g in enumerate(groups):
        end = groups[i + 1][0][0] if i + 1 < len(groups) else scene_end
        out.append((g[0][0], max(end, g[-1][1]), g))
    return out


def _ass_color(hex_color: str, alpha: str = "00") -> str:
    h = hex_color.lstrip("#")
    return f"&H{alpha}{h[4:6]}{h[2:4]}{h[0:2]}".upper()


def _ass_time(t: float) -> str:
    t = max(0, t)
    return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"


def build_ass(words: list, text: str, duration: float, size, captions: dict, font_family: str = FONT_NAME) -> str | None:
    preset = CAPTION_PRESETS.get(captions.get("preset", "bold"))
    if not preset or not words:
        return None
    w, h = size
    fs = int(min(w, h) * preset["scale"])
    outline = max(1, int(fs * preset["outline"]))
    margin_v = int(h * (1 - float(captions.get("position", 0.68))))
    color = _ass_color(captions.get("color", "#FFFFFF"))
    hl = _ass_color(captions.get("highlight_color", "#FFE14D"))
    border_style, back = (3, "&H66000000") if preset["box"] else (1, "&H80000000")
    out_color = "&H66000000" if preset["box"] else "&H00000000"
    lines = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {w}", f"PlayResY: {h}", "WrapStyle: 0",
        "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
        "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
        "MarginR, MarginV, Encoding",
        f"Style: Default,{font_family.replace(',', ' ')},{fs},{color},{color},{out_color},{back},-1,0,0,0,100,100,0,0,"
        f"{border_style},{outline},0,2,{int(w * 0.07)},{int(w * 0.07)},{margin_v},1",
        "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]

    def esc(s):
        s = s.replace("\\", "").replace("{", "(").replace("}", ")")
        return s.upper() if preset["upper"] else s

    for start, end, group in caption_chunks(attach_punctuation(words, text), duration):
        if preset["highlight"]:
            # One event per spoken word, with that word highlighted.
            for i, (ws, _we, _) in enumerate(group):
                we = group[i + 1][0] if i + 1 < len(group) else end
                txt = " ".join(
                    f"{{\\c{hl}}}{esc(x[2])}{{\\c{color}}}" if k == i else esc(x[2]) for k, x in enumerate(group)
                )
                lines.append(f"Dialogue: 0,{_ass_time(ws if i else start)},{_ass_time(we)},Default,,0,0,0,,{txt}")
        else:
            txt = " ".join(esc(x[2]) for x in group)
            lines.append(f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Default,,0,0,0,,{txt}")
    return "\n".join(lines) + "\n"


# ---------- timeline → video ----------

BRAND_ELEMENTS = {"logo": True, "intro": True, "outro": True, "music": True}


def apply_brand_style(tl: dict, brand: dict | None):
    """Caption look and voice from a brand kit (used when a kit is picked)."""
    tl["brand_id"] = brand["id"] if brand else None
    tl.setdefault("brand_elements", dict(BRAND_ELEMENTS))
    if brand:
        tl["captions"] = {**tl.get("captions", DEFAULT_CAPTIONS), **brand["captions"],
                          "color": brand["colors"]["text"], "highlight_color": brand["colors"]["accent"]}


def new_timeline(meta: dict, req: dict, brand: dict | None = None) -> dict:
    scenes = meta.get("scenes") or [{"text": meta["script"], "search": (meta.get("search_terms") or ["nature"])[0]}]
    language = req.get("language") or "English"
    voice = req.get("voice")
    if not voice and brand and brand.get("voice_name") and brand.get("language") in ("", None, language):
        voice = brand["voice_name"]
    tl = {
        "engine": "classic",
        "language": language,
        "format": req.get("format", "shorts"),
        "voice": re.sub(r"-(Male|Female)$", "", voice or DEFAULT_VOICES.get(language, DEFAULT_VOICES["English"])),
        "voice_rate": float(req.get("voice_rate", 1.0)),
        "music": req.get("music") or "",      # "" auto · "none" · a library file
        "sfx": req.get("sfx", True) is not False,  # a whoosh on every cut
        "motion": True,
        "captions": dict(DEFAULT_CAPTIONS),
        "scenes": [
            {"id": f"s{i + 1}", "text": s["text"].strip(), "search": s.get("search", ""),
             "clip": brands.clip_for_scene(brand, s.get("asset"))}
            for i, s in enumerate(scenes) if s.get("text", "").strip()
        ],
    }
    apply_brand_style(tl, brand)
    if not footage_status()["ok"] and not brand:
        tl["captions"]["position"] = 0.5  # text-centric look on motion backgrounds
    return tl


def _style(pid: str, tl: dict, brand: dict | None) -> dict:
    """Font, logo, bumpers and music for this render."""
    el = {**BRAND_ELEMENTS, **(tl.get("brand_elements") or {})}
    cache = project_dir(pid) / "cache"
    font_file, family = FONT_PATH, FONT_NAME
    if brand and brand["font"]:
        f = brands.asset_path(brand["id"], brand["font"]["file"])
        if f.exists():
            font_file, family = f, brand["font"]["family"]
    # libass gets a relative fonts dir (cwd=cache) so paths never need escaping.
    fonts_dir = cache / f"fonts_{_hash(str(font_file))[:8]}"
    if not fonts_dir.exists():
        fonts_dir.mkdir()
        shutil.copyfile(font_file, fonts_dir / font_file.name)
    style = {"fonts_dir": fonts_dir.name, "family": family, "logo": None, "intro": None, "outro": None, "music": None}
    if brand:
        def path(item):
            p = brands.asset_path(brand["id"], item["file"]) if item else None
            return p if p and p.exists() else None
        if el["logo"] and (p := path(brand["logo"])):
            style["logo"] = {**brand["logo"], "path": str(p), "mtime": p.stat().st_mtime}
        for k in ("intro", "outro"):
            if el[k] and (p := path(brand[k])):
                style[k] = p
    if el["music"]:
        from . import audio  # audio imports render

        if (m := audio.pick(tl.get("music") or "", brand, pid)):
            style["music"] = {"path": m["path"], "volume": m["volume"]}
    return style


def _logo_xy(position: str, margin: int) -> tuple[str, str]:
    x = f"main_w-overlay_w-{margin}" if "right" in position else str(margin)
    y = f"main_h-overlay_h-{margin}" if "bottom" in position else str(margin)
    return x, y


async def _encode_scene(pid: str, scene: dict, clip_path: Path, voice_dur: float, words: list, tl: dict, style: dict) -> Path:
    size = SIZES.get(tl["format"], SIZES["shorts"])
    cache = project_dir(pid) / "cache"
    logo = style["logo"]
    is_motion = (scene.get("clip") or {}).get("provider") == "motion"
    key = _hash("scene", str(clip_path), voice_dur, words, scene["text"], tl["captions"], tl["motion"], size,
                scene.get("clip_start", 0), style["family"], style["fonts_dir"], logo,
                scene["clip"].get("colors") if is_motion else None)
    out = cache / f"scene_{key}.mp4"
    if out.exists():
        return out
    w, h = size
    vf = [f"scale={w}:{h}:force_original_aspect_ratio=increase"]
    if is_motion:
        vf = ["noise=alls=5:allf=t", "vignette=angle=PI/5"]  # subtle grain keeps gradients from banding
    elif tl.get("motion"):
        # Slow drift across a slightly larger frame (cheap, smooth Ken Burns).
        vf += ["scale=iw*1.12:-2", f"crop={w}:{h}:x='(iw-{w})*(0.2+0.6*t/{voice_dur:.3f})':y='(ih-{h})/2'"]
    else:
        vf += [f"crop={w}:{h}"]
    vf += ["fps=30", "setsar=1", "format=yuv420p"]
    ass = build_ass(words, scene["text"], voice_dur, size, tl["captions"], style["family"])
    if ass:
        (cache / f"cap_{key}.ass").write_text(ass, encoding="utf-8")
        vf.append(f"ass=cap_{key}.ass:fontsdir={style['fonts_dir']}")
    if is_motion:
        inputs = ["-f", "lavfi", "-i", _gradient_src(scene["clip"], size, voice_dur)]
    else:
        inputs = ["-stream_loop", "-1", "-ss", scene.get("clip_start", 0), "-i", clip_path]
    graph = f"[0:v]{','.join(vf)}[base]"
    out_label = "[base]"
    if logo:
        inputs += ["-i", logo["path"]]
        lw = max(16, int(w * float(logo.get("size", 0.16))) // 2 * 2)
        x, y = _logo_xy(logo.get("position", "top-right"), int(min(w, h) * 0.045))
        graph += (f";[1:v]scale={lw}:-1,format=rgba,colorchannelmixer=aa={float(logo.get('opacity', 0.9)):.2f}[lg]"
                  f";[base][lg]overlay={x}:{y}:format=auto,format=yuv420p[vout]")
        out_label = "[vout]"
    tmp = out.with_name(out.stem + ".tmp.mp4")
    await _ffmpeg(
        *inputs, "-filter_complex", graph, "-map", out_label, "-t", f"{voice_dur:.3f}", "-an",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-r", "30", tmp,
        cwd=cache,
    )
    tmp.rename(out)
    return out


async def _ffmpeg_probe_size(path: Path) -> tuple[int, int]:
    proc = await asyncio.create_subprocess_exec(
        FFMPEG, "-hide_banner", "-i", str(path), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    m = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", err.decode(errors="replace"))
    if not m:
        raise RuntimeError(f"Cannot read the size of {path.name}")
    return int(m.group(1)), int(m.group(2))


async def _has_audio(path: Path) -> bool:
    proc = await asyncio.create_subprocess_exec(
        FFMPEG, "-hide_banner", "-i", str(path), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    return "Audio:" in err.decode(errors="replace")


async def _to_wav(src: Path, cache: Path, duration: float | None = None) -> Path:
    """Normalize audio to 44.1 kHz stereo PCM so every part concatenates cleanly."""
    dest = cache / f"wav_{_hash(str(src), src.stat().st_mtime, duration)}.wav"
    if not dest.exists():
        if await _has_audio(src):
            args = ["-i", src, "-vn", "-ac", "2", "-ar", "44100"]
        else:
            args = ["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo"]
        if duration:
            args += ["-af", f"apad,atrim=0:{duration:.3f}"]
        await _ffmpeg(*args, "-c:a", "pcm_s16le", dest.with_suffix(".tmp.wav"))
        dest.with_suffix(".tmp.wav").rename(dest)
    return dest


async def _encode_bumper(pid: str, src: Path, size) -> tuple[Path, Path, float]:
    """Intro/outro → (video part, wav, duration), conformed to the project format."""
    cache = project_dir(pid) / "cache"
    w, h = size
    dur = await media_duration(src)
    video = cache / f"bumper_{_hash(str(src), src.stat().st_mtime, size)}.mp4"
    if not video.exists():
        await _ffmpeg(
            "-i", src, "-an", "-t", f"{dur:.3f}",
            "-vf", f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},fps=30,setsar=1,format=yuv420p",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-r", "30", video.with_suffix(".tmp.mp4"),
        )
        video.with_suffix(".tmp.mp4").rename(video)
    return video, await _to_wav(src, cache, dur), dur


async def _auto_clip(scene: dict, i: int, scenes: list, size, brand: dict | None) -> dict:
    """Stock footage when a key is set, otherwise (or on failure) a motion background."""
    if footage_status()["ok"]:
        try:
            results = await search_footage(scene["search"] or scene["text"][:60], size)
            used = {x["clip"]["id"] for x in scenes if x.get("clip") and x["clip"].get("id")}
            fresh = [r for r in results if r["id"] not in used] or results
            if fresh:
                return fresh[0]
        except Exception as e:
            log.warning("stock search failed for scene %s, using a motion background: %s", i + 1, e)
    return motion_clip(i, brand)


async def render_timeline(pid: str, tl: dict, out: Path, on_progress) -> dict:
    """Render the timeline to `out`. Mutates tl: fills clips and timings."""
    size = SIZES.get(tl["format"], SIZES["shorts"])
    scenes = tl["scenes"]
    if not scenes:
        raise ValueError("Timeline has no scenes")
    n = len(scenes)
    brand = brands.get(tl.get("brand_id"))
    style = _style(pid, tl, brand)
    cache = project_dir(pid) / "cache"

    voices = []
    activity.add(pid, f"Recording the voice-over ({tts.provider()})")
    for i, s in enumerate(scenes):
        voices.append(await scene_voice(pid, s["text"], tl["voice"], tl["voice_rate"], tl.get("language", "English")))
        on_progress(0.15 * (i + 1) / n)

    for i, s in enumerate(scenes):
        if s.get("clip") and s["clip"].get("path") and not Path(s["clip"]["path"]).exists():
            s["clip"] = None  # brand clip or upload was removed
        if not s.get("clip"):
            s["clip"] = await _auto_clip(s, i, scenes, size, brand)
        if s["clip"].get("provider") == "motion":
            s["clip"].setdefault("colors", motion_clip(i, brand)["colors"])
            if not s["clip"].get("thumb_local"):
                s["clip"]["thumb_local"] = await motion_thumbnail(pid, s["clip"])
            activity.add(pid, f"Scene {i + 1}: animated background in {'brand' if brand else 'theme'} colors")
            activity.preview(pid, Path(s["clip"]["thumb_local"]))
            on_progress(0.15 + 0.2 * (i + 1) / n)
            continue
        clip_path = await ensure_clip_file(pid, s["clip"])
        if not s["clip"].get("thumb_local") and not s["clip"].get("thumb_url"):
            s["clip"]["thumb_local"] = str(await thumbnail(clip_path))
        activity.add(pid, f"Scene {i + 1}: footage from {s['clip'].get('provider', 'your library')}")
        if s["clip"].get("thumb_local"):
            activity.preview(pid, Path(s["clip"]["thumb_local"]))
        on_progress(0.15 + 0.2 * (i + 1) / n)

    video_parts, audio_parts, t = [], [], 0.0
    if style["intro"]:
        v, a, d = await _encode_bumper(pid, style["intro"], size)
        video_parts.append(v); audio_parts.append(a); t += d  # noqa: E702
    for i, (s, (mp3, words, dur)) in enumerate(zip(scenes, voices)):
        activity.add(pid, f"Rendering scene {i + 1} of {n} with captions")
        video_parts.append(await _encode_scene(pid, s, Path(s["clip"].get("path") or "motion"), dur, words, tl, style))
        audio_parts.append(await _to_wav(mp3, cache, dur))
        s["start"], s["end"] = round(t, 2), round(t + dur, 2)
        t += dur
        on_progress(0.35 + 0.55 * (i + 1) / n)
    if style["outro"]:
        v, a, d = await _encode_bumper(pid, style["outro"], size)
        video_parts.append(v); audio_parts.append(a); t += d  # noqa: E702

    (cache / "video.txt").write_text("".join(f"file '{p.name}'\n" for p in video_parts))
    (cache / "audio.txt").write_text("".join(f"file '{p.name}'\n" for p in audio_parts))
    args = ["-f", "concat", "-safe", "0", "-i", "video.txt", "-f", "concat", "-safe", "0", "-i", "audio.txt"]
    sfx = None
    if tl.get("sfx", True) is not False:
        from . import audio

        intro = (scenes[0].get("start") or 0) if scenes else 0
        cues = [max(0.0, s["start"] - 0.25) for s in scenes[1:]] + ([max(0.0, intro - 0.25)] if intro else [])
        sfx = await audio.sfx_track(cues, t, cache / f"sfx_{_hash(cues, t)}.wav")
    graph, mix = [], ["[vo]"]
    if style["music"]:
        m = style["music"]
        fade = max(0.0, t - 2.0)
        args += ["-stream_loop", "-1", "-i", str(m["path"])]
        # Music ducks under the voice (sidechain) and fades out at the end.
        graph += [f"[1:a]asplit=2[vo][sc]",
                  f"[2:a]aformat=sample_rates=44100:channel_layouts=stereo,volume={m['volume']:.3f},"
                  f"afade=t=out:st={fade:.2f}:d=2[mu]",
                  "[mu][sc]sidechaincompress=threshold=0.03:ratio=6:attack=20:release=400[md]"]
        mix.append("[md]")
    else:
        graph.append("[1:a]anull[vo]")
    if sfx:
        args += ["-i", sfx]
        graph.append(f"[{len(mix) + 1}:a]aformat=sample_rates=44100:channel_layouts=stereo[sx]")
        mix.append("[sx]")
    if len(mix) > 1:
        graph.append("".join(mix) + f"amix=inputs={len(mix)}:duration=first:normalize=0[a]")
        args += ["-filter_complex", ";".join(graph), "-map", "0:v", "-map", "[a]"]
    else:
        args += ["-map", "0:v", "-map", "1:a"]
    await _ffmpeg(*args, "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", "-t", f"{t:.3f}",
                  "-movflags", "+faststart", out.resolve(), cwd=cache)
    on_progress(1.0)
    return tl


def clear_cache(pid: str):
    shutil.rmtree(DATA_DIR / "projects" / pid, ignore_errors=True)
