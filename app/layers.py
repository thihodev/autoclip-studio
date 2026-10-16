"""Export a video as separate layers, for finishing it in CapCut, Premiere, DaVinci Resolve or Final Cut.

No editor shares a layered project format with CapCut (its drafts are its own and encrypted), so the export is
the common denominator every editor imports, in one ZIP:

    scenes/NN-name.mp4   each scene as its own clip, without captions (Quick: also without the logo)
    captions.mov         captions (and the logo) on a transparent background — ProRes 4444 with alpha
    captions.srt         the same captions as text, to restyle them in the editor
    voice.wav · music.wav · sfx.wav   one audio stem each
    video-no-captions.mp4 (Claude Motion)  the whole video with its transitions, without captions
    timeline.fcpxml      the scenes in order with the layers above them, for DaVinci Resolve / Final Cut
    README.txt           how to put it together

Quick videos are rebuilt from their timeline with ffmpeg (seconds); Claude Motion videos are re-rendered from
their HyperFrames project, one composition per layer (a few minutes, no Claude).
"""
import html
import json
import re
import shutil
import zipfile
from pathlib import Path

from . import audio, brands, render
from .config import VIDEOS_DIR

FPS = 30


def _t(seconds: float) -> str:
    """FCPXML time on the 30 fps grid."""
    return f"{max(0, round(seconds * FPS))}/{FPS}s"


def _srt_time(t: float) -> str:
    ms = int(round(max(0.0, t) * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def _srt(cues: list[tuple[float, float, str]]) -> str:
    return "\n".join(f"{i}\n{_srt_time(a)} --> {_srt_time(b)}\n{text}\n" for i, (a, b, text) in enumerate(cues, 1))


def fcpxml(title: str, size: tuple[int, int], total: float, scenes: list[dict], overlays: list[dict], audios: list[dict]) -> str:
    """scenes: [{file, start, duration}] in order on the main track; overlays/audios: [{file, start, duration}]
    connected above (lane 1+) or below (lane -1…) the first clip. Paths are relative to the ZIP folder."""
    w, h = size
    assets, n = [], 0
    ids = {}
    for item in scenes + overlays + audios:
        if item["file"] in ids:
            continue
        n += 1
        ids[item["file"]] = f"r{n + 1}"
        is_audio = item["file"].endswith(".wav")
        assets.append(f'    <asset id="r{n + 1}" name="{html.escape(Path(item["file"]).stem)}" start="0s" duration="{_t(item["media"])}" '
                      f'hasVideo="{0 if is_audio else 1}" hasAudio="{1 if is_audio or item.get("audio") else 0}"'
                      f'{"" if is_audio else " format=" + chr(34) + "r1" + chr(34)}>'
                      f'<media-rep kind="original-media" src="./{html.escape(item["file"])}"/></asset>')
    connected = [f'          <asset-clip ref="{ids[o["file"]]}" lane="{i + 1}" offset="{_t(o["start"])}" duration="{_t(o["duration"])}" name="{html.escape(Path(o["file"]).stem)}"/>'
                 for i, o in enumerate(overlays)]
    connected += [f'          <asset-clip ref="{ids[a["file"]]}" lane="{-(i + 1)}" offset="{_t(a["start"])}" duration="{_t(a["duration"])}" name="{html.escape(Path(a["file"]).stem)}"/>'
                  for i, a in enumerate(audios)]
    spine = []
    for i, s in enumerate(scenes):
        inner = ("\n" + "\n".join(connected) + "\n        ") if i == 0 else ""
        spine.append(f'        <asset-clip ref="{ids[s["file"]]}" offset="{_t(s["start"])}" start="0s" duration="{_t(s["duration"])}" '
                     f'name="{html.escape(Path(s["file"]).stem)}" format="r1">{inner}</asset-clip>')
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE fcpxml>
<fcpxml version="1.10">
  <resources>
    <format id="r1" name="AutoClip {w}x{h} {FPS}p" frameDuration="1/{FPS}s" width="{w}" height="{h}"/>
{chr(10).join(assets)}
  </resources>
  <library>
    <event name="AutoClip Studio">
      <project name="{html.escape(title)}">
        <sequence format="r1" duration="{_t(total)}" tcStart="0s" tcFormat="NDF" audioLayout="stereo" audioRate="48k">
          <spine>
{chr(10).join(spine)}
          </spine>
        </sequence>
      </project>
    </event>
  </library>
</fcpxml>
'''


README = """AutoClip Studio — layers of "{title}" (version {version})

scenes/            each scene as its own clip, without captions. Put them in order on the main video track.
captions.mov       captions{logo} on a transparent background (ProRes 4444 with alpha). Put it on the track above.
captions.srt       the captions as text, if you'd rather restyle them in your editor.
voice.wav          the voice-over.        music.wav   the music bed (not ducked — duck it in your editor).
sfx.wav            the sound effects.{extra}
timeline.fcpxml    the whole layout for DaVinci Resolve (File → Import → Timeline) or Final Cut Pro.
                   Keep it next to these files; relink the media if your editor asks.

CapCut: drag the scenes onto the main track in order, captions.mov onto the track above, the WAV files onto
audio tracks (all start at 0:00), or import captions.srt instead of captions.mov to restyle the captions.
"""


async def _wav(src: Path, dest: Path, total: float):
    await render._ffmpeg("-i", src, "-ac", "2", "-ar", "48000", "-af", f"apad,atrim=0:{total:.3f}", dest)


async def _mix(parts: list[tuple[Path, float, float]], total: float, dest: Path):
    """[(file, start seconds, volume)] → one stereo WAV of `total` seconds."""
    if not parts:
        await render._ffmpeg("-f", "lavfi", "-i", f"anullsrc=r=48000:cl=stereo", "-t", f"{total:.3f}", dest)
        return
    args, chains = [], []
    for i, (f, start, vol) in enumerate(parts):
        args += ["-i", f]
        ms = int(start * 1000)
        chains.append(f"[{i}:a]aformat=sample_rates=48000:channel_layouts=stereo,volume={vol:.3f},adelay={ms}|{ms}[a{i}]")
    graph = ";".join(chains) + ";" + "".join(f"[a{i}]" for i in range(len(parts))) + \
        f"amix=inputs={len(parts)}:normalize=0:duration=longest,apad,atrim=0:{total:.3f}[o]"
    await render._ffmpeg(*args, "-filter_complex", graph, "-map", "[o]", "-ar", "48000", dest)


# ------------------------------------------------------------------ Quick

async def _quick(p: dict, work: Path, report) -> dict:
    pid, tl = p["id"], p["timeline"]
    size = render.SIZES.get(tl["format"], render.SIZES["shorts"])
    w, h = size
    brand = brands.get(tl.get("brand_id"))
    style = render._style(pid, tl, brand)
    cache = render.project_dir(pid) / "cache"
    (work / "scenes").mkdir(parents=True)
    scenes, voices, caps, cues, t = [], [], [], [], 0.0
    if style["intro"]:
        v, a, d = await render._encode_bumper(pid, style["intro"], size)
        shutil.copyfile(v, work / "scenes" / "00-intro.mp4")
        scenes.append({"file": "scenes/00-intro.mp4", "start": 0, "duration": d, "media": d})
        voices.append((a, 0, 1)); caps.append((None, d)); t += d  # noqa: E702
    n = len(tl["scenes"])
    for i, s in enumerate(tl["scenes"]):
        report(10 + 70 * i / max(1, n), f"Scene {i + 1} of {n}")
        mp3, words, dur = await render.scene_voice(pid, s["text"], tl["voice"], tl["voice_rate"], tl.get("language", "English"))
        if not s.get("clip"):
            raise RuntimeError("Render the video first")
        clip_path = Path(s["clip"].get("path") or "motion") if s["clip"].get("provider") == "motion" else await render.ensure_clip_file(pid, s["clip"])
        clean = await render._encode_scene(pid, s, clip_path, dur, words, tl, style, layers=True)
        name = f"scenes/{i + 1:02d}.mp4"
        shutil.copyfile(clean, work / name)
        scenes.append({"file": name, "start": t, "duration": dur, "media": dur})
        voices.append((await render._to_wav(mp3, cache, dur), t, 1))
        caps.append((await _caption_layer(pid, s, words, dur, tl, style, size), dur))
        for a, b, group in render.caption_chunks(render.attach_punctuation(words, s["text"]), dur):
            cues.append((t + a, t + b, " ".join(x[2] for x in group)))
        t += dur
    if style["outro"]:
        v, a, d = await render._encode_bumper(pid, style["outro"], size)
        shutil.copyfile(v, work / "scenes" / "99-outro.mp4")
        scenes.append({"file": "scenes/99-outro.mp4", "start": t, "duration": d, "media": d})
        voices.append((a, t, 1)); caps.append((None, d)); t += d  # noqa: E702
    report(82, "Captions layer")
    parts = []
    for i, (f, d) in enumerate(caps):
        if f is None:  # bumpers: a transparent gap
            f = work / f"blank_{i}.mov"
            await render._ffmpeg("-f", "lavfi", "-i", f"color=c=black@0.0:s={w}x{h}:r={FPS}:d={d:.3f},format=rgba",
                                 "-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuva444p10le", f)
        parts.append(f)
    (work / "caps.txt").write_text("".join(f"file '{Path(x).resolve()}'\n" for x in parts))
    await render._ffmpeg("-f", "concat", "-safe", "0", "-i", work / "caps.txt", "-c", "copy", work / "captions.mov")
    (work / "caps.txt").unlink()
    for f in work.glob("blank_*.mov"):
        f.unlink()
    report(88, "Audio stems")
    await _mix(voices, t, work / "voice.wav")
    m = style["music"]
    if m:
        fade = max(0.0, t - 2)
        await render._ffmpeg("-stream_loop", "-1", "-i", m["path"], "-t", f"{t:.3f}", "-ac", "2", "-ar", "48000",
                             "-af", f"volume={m['volume']:.3f},afade=t=out:st={fade:.2f}:d=2", work / "music.wav")
    if tl.get("sfx", True) is not False:
        intro = scenes[1]["start"] if style["intro"] and len(scenes) > 1 else 0
        cue_t = [max(0.0, s["start"] - 0.25) for s in scenes[1:] if s["file"] != "scenes/99-outro.mp4"] + ([max(0.0, intro - 0.25)] if intro else [])
        await audio.sfx_track(cue_t, t, work / "sfx.wav")
    (work / "captions.srt").write_text(_srt(cues), encoding="utf-8")
    return {"size": size, "total": t, "scenes": scenes, "logo": bool(style["logo"]), "extra": ""}


async def _caption_layer(pid: str, scene: dict, words: list, dur: float, tl: dict, style: dict, size) -> Path:
    """One scene's captions (and logo) on a transparent background, cached like the scenes."""
    cache = render.project_dir(pid) / "cache"
    w, h = size
    key = render._hash("caplayer2", words, scene["text"], tl["captions"], size, style["family"], style["fonts_dir"], style["logo"], dur)
    out = cache / f"caplayer_{key}.mov"
    if out.exists():
        return out
    ass = render.build_ass(words, scene["text"], dur, size, tl["captions"], style["family"])
    vf = [f"format=rgba"]
    if ass:
        (cache / f"caplayer_{key}.ass").write_text(ass, encoding="utf-8")
        vf.append(f"ass=caplayer_{key}.ass:fontsdir={style['fonts_dir']}:alpha=1")
    inputs = ["-f", "lavfi", "-i", f"color=c=black@0.0:s={w}x{h}:r={FPS}:d={dur:.3f},format=rgba"]  # keep the alpha from the start
    graph = f"[0:v]{','.join(vf)}[base]"
    label = "[base]"
    logo = style["logo"]
    if logo:
        inputs += ["-i", logo["path"]]
        lw = max(16, int(w * float(logo.get("size", 0.16))) // 2 * 2)
        x, y = render._logo_xy(logo.get("position", "top-right"), int(min(w, h) * 0.045), (w, h))
        graph += (f";[1:v]scale={lw}:-1,format=rgba,colorchannelmixer=aa={float(logo.get('opacity', 0.9)):.2f}[lg]"
                  f";[base][lg]overlay={x}:{y}:format=auto,format=rgba[vout]")
        label = "[vout]"
    tmp = out.with_name(out.stem + ".tmp.mov")
    await render._ffmpeg(*inputs, "-filter_complex", graph, "-map", label, "-t", f"{dur:.3f}",
                         "-c:v", "prores_ks", "-profile:v", "4444", "-pix_fmt", "yuva444p10le", tmp, cwd=cache)
    tmp.rename(out)
    return out


# ------------------------------------------------------------------ Claude Motion

_EL = re.compile(r"<(div|audio)\b([^>]*)>\s*</\1>", re.S)


def _attrs(s: str) -> dict:
    return dict(re.findall(r'([\w-]+)="([^"]*)"', s))


def _motion_page(index: str, keep: list[str], duration: float, transparent: bool = False, transitions: bool = False) -> str:
    """index.html reduced to the elements in `keep` (their raw HTML), on a timeline of `duration` seconds."""
    head = index[:index.index("<body>")]
    if transparent:
        head = head.replace("</head>", "<style>html,body,#root{background:transparent !important}</style>\n</head>")
    root = re.search(r"<div\s+id=\"root\"[^>]*>", index).group(0)
    root = re.sub(r'data-duration="[^"]*"', f'data-duration="{duration:.3f}"', root)
    script = ""
    if transitions:
        script = index[index.index("<script>", index.index("<body>")):index.rindex("</script>") + len("</script>")]
    else:
        script = ("<script>window.__timelines = window.__timelines || {}; window.__timelines[\"main\"] = gsap.timeline({ paused: true });"
                  f" window.__timelines[\"main\"].to({{}}, {{ duration: {duration:.3f} }}, 0);</script>")
    return f"{head}<body>\n{root}\n" + "\n".join(keep) + f"\n</div>\n{script}\n</body>\n</html>\n"


async def _hf_render(root: Path, page: str, name: str, out: Path, fmt: str):
    from . import motion  # motion imports a lot; keep it out of import time
    (root / name).write_text(page, encoding="utf-8")
    try:
        await motion._run([shutil.which("npx"), "-y", f"hyperframes@{motion.HF_VERSION}", "render", ".", "-c", name,
                           "--format", fmt, "-o", str(out.resolve()), "--quiet"], root, motion._env())
    finally:
        (root / name).unlink(missing_ok=True)


async def _motion(p: dict, work: Path, report) -> dict:
    from . import motion
    root = motion.project_root(p["id"])
    index = (root / "index.html").read_text(encoding="utf-8")
    els = [(m.group(0), m.group(1), _attrs(m.group(2))) for m in _EL.finditer(index)]
    frames = [(raw, a) for raw, tag, a in els if tag == "div" and "frames/" in a.get("data-composition-src", "")]
    caption = [raw for raw, tag, a in els if tag == "div" and a.get("data-composition-id") == "captions"]
    audios = [(raw, a) for raw, tag, a in els if tag == "audio"]
    w = int(re.search(r'data-width="(\d+)"', index).group(1))
    h = int(re.search(r'data-height="(\d+)"', index).group(1))
    total = float(re.search(r'data-composition-id="main"[^>]*data-duration="([\d.]+)"', index, re.S).group(1)) \
        if re.search(r'data-composition-id="main"[^>]*data-duration="([\d.]+)"', index, re.S) else \
        float(re.search(r'<div\s+id="root"[^>]*data-duration="([\d.]+)"', index, re.S).group(1))
    (work / "scenes").mkdir(parents=True)
    scenes = []
    starts = [float(a["data-start"]) for _, a in frames]
    for i, (raw, a) in enumerate(frames):
        report(5 + 60 * i / max(1, len(frames)), f"Scene {i + 1} of {len(frames)}")
        dur = float(a["data-duration"])
        one = re.sub(r'data-start="[^"]*"', 'data-start="0"', raw, count=1)
        name = f"scenes/{i + 1:02d}-{a['data-composition-id'].split('-', 1)[-1]}.mp4"
        await _hf_render(root, _motion_page(index, [one], dur), f"_layer_scene_{i + 1}.html", work / name, "mp4")
        cut = (starts[i + 1] if i + 1 < len(starts) else total) - starts[i]  # on the main track: cut to the next scene
        scenes.append({"file": name, "start": starts[i], "duration": min(cut, dur), "media": dur})
    report(68, "Captions layer")
    if caption:
        await _hf_render(root, _motion_page(index, caption, total, transparent=True), "_layer_captions.html", work / "captions.mov", "mov")
    report(80, "Video without captions")
    without = [raw for raw, tag, a in els if not (tag == "div" and a.get("data-composition-id") == "captions")]
    await _hf_render(root, _motion_page(index, without, total, transitions=True), "_layer_clean.html", work / "video-no-captions.mp4", "mp4")
    report(90, "Audio stems")
    voice = [(root / a["src"], float(a["data-start"]), float(a.get("data-volume", 1))) for _, a in audios if "/voice/" in a.get("src", "")]
    sfx = [(root / a["src"], float(a["data-start"]), float(a.get("data-volume", 1))) for _, a in audios if "/sfx/" in a.get("src", "")]
    music = [(root / a["src"], float(a["data-start"]), float(a.get("data-volume", 1))) for _, a in audios if "/music/" in a.get("src", "")]
    await _mix(voice, total, work / "voice.wav")
    if sfx:
        await _mix(sfx, total, work / "sfx.wav")
    if music:
        await _mix(music, total, work / "music.wav")
    cues = []
    groups = root / "caption_groups.json"
    if groups.exists():
        cues = [(g["start"], g["end"], g["text"]) for g in json.loads(groups.read_text()).get("groups", [])]
    (work / "captions.srt").write_text(_srt(cues), encoding="utf-8")
    return {"size": (w, h), "total": total, "scenes": scenes, "logo": False,
            "extra": "\nvideo-no-captions.mp4  the whole video with its transitions, without captions."}


# ------------------------------------------------------------------ export

async def export(p: dict, base: dict, report) -> Path:
    """→ VIDEOS_DIR/<pid>_v<N>_layers.zip for the base version."""
    from . import pipeline
    work = render.project_dir(p["id"]) / "layers"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    info = await (_motion(p, work, report) if pipeline.is_motion(p) else _quick(p, work, report))
    total, size = info["total"], info["size"]
    overlays = [{"file": "captions.mov", "start": 0, "duration": total, "media": total}] if (work / "captions.mov").exists() else []
    stems = [{"file": f, "start": 0, "duration": total, "media": total} for f in ("voice.wav", "music.wav", "sfx.wav") if (work / f).exists()]
    title = (p.get("script") or {}).get("title") or p["request"]["topic"][:60]
    (work / "timeline.fcpxml").write_text(fcpxml(title, size, total, info["scenes"], overlays, stems), encoding="utf-8")
    (work / "README.txt").write_text(README.format(title=title, version=base["version"], extra=info["extra"],
                                                   logo=" and the logo" if info["logo"] else ""), encoding="utf-8")
    out = VIDEOS_DIR / f"{p['id']}_v{base['version']}_layers.zip"
    folder = (re.sub(r"[^\w\- ]+", "", title).strip()[:40] or "autoclip") + " - layers"
    report(96, "Packing the ZIP")
    with zipfile.ZipFile(out.with_suffix(".tmp"), "w", zipfile.ZIP_STORED) as z:  # media is already compressed
        for f in sorted(work.rglob("*")):
            if f.is_file():
                z.write(f, f"{folder}/{f.relative_to(work)}")
    out.with_suffix(".tmp").rename(out)
    shutil.rmtree(work, ignore_errors=True)
    return out
