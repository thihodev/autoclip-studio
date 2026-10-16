"""The timeline editor: CapCut-style edits on top of any rendered version (Quick or Claude Motion).

An edit is a small JSON document kept on the project (extra["edit"]) and applied to a *base* version with
ffmpeg, so editing is non-destructive and every export becomes a new version:

    clips  — pieces of the base video in order: in/out (seconds in the base), speed, volume, look (filter preset
             + brightness/contrast/saturation)
    texts  — titles over the video: text, start/end (timeline seconds), position (0–1), size (fraction of the
             height), color, style (plain/outline/box/shadow), animation (none/fade/pop/slide/type)
    music  — a library track: where it starts on the timeline, where it starts in the track, length, volume, fades
    audio  — the original sound's volume

The browser preview applies the same looks with CSS filters, so color math here follows the CSS filter
definitions (Filter Effects spec), and titles use the same font, size, position and animation timing.
"""
import math
import re
import uuid
from pathlib import Path

from PIL import ImageFont

from . import audio, render
from .config import FONT_PATH

# Looks: CSS filter functions in order, mirrored in ffmpeg. Names are UI keys.
LOOKS = {
    "none": [],
    "vivid": [("saturate", 1.35), ("contrast", 1.08)],
    "warm": [("sepia", 0.22), ("saturate", 1.15)],
    "fade": [("contrast", 0.85), ("saturate", 0.8), ("brightness", 1.08)],
    "film": [("sepia", 0.12), ("contrast", 1.1), ("saturate", 0.9)],
    "mono": [("grayscale", 1.0), ("contrast", 1.1)],
    "noir": [("grayscale", 1.0), ("contrast", 1.35), ("brightness", 0.95)],
}
ANIMS = {"none", "fade", "pop", "slide", "type"}
TEXT_STYLES = {"plain", "outline", "box", "shadow"}
MAX_CLIPS, MAX_TEXTS = 200, 100


def _num(v, lo, hi, default):
    try:
        return max(lo, min(hi, float(v)))
    except (TypeError, ValueError):
        return default


def clean(edit: dict, base_duration: float) -> dict:
    """Validate and clamp an edit coming from the browser."""
    clips = []
    for c in (edit.get("clips") or [])[:MAX_CLIPS]:
        a = _num(c.get("in"), 0, base_duration, 0)
        b = _num(c.get("out"), 0, base_duration, base_duration)
        if b - a < 0.05:
            continue
        adj = c.get("adjust") or {}
        clips.append({
            "id": str(c.get("id") or uuid.uuid4().hex[:8])[:16], "in": round(a, 3), "out": round(b, 3),
            "speed": _num(c.get("speed"), 0.25, 4, 1), "volume": _num(c.get("volume"), 0, 2, 1),
            "look": c.get("look") if c.get("look") in LOOKS else "none",
            "adjust": {"brightness": _num(adj.get("brightness"), 0.5, 1.5, 1), "contrast": _num(adj.get("contrast"), 0.5, 1.5, 1),
                       "saturation": _num(adj.get("saturation"), 0, 2, 1)},
        })
    total = duration({"clips": clips})
    texts = []
    for t in (edit.get("texts") or [])[:MAX_TEXTS]:
        body = str(t.get("text") or "").strip()[:300]
        start = _num(t.get("start"), 0, total, 0)
        end = _num(t.get("end"), 0, total, min(total, start + 2))
        if not body or end - start < 0.1:
            continue
        color = t.get("color") if re.fullmatch(r"#[0-9a-fA-F]{6}", str(t.get("color", ""))) else "#ffffff"
        texts.append({
            "id": str(t.get("id") or uuid.uuid4().hex[:8])[:16], "text": body, "start": round(start, 3), "end": round(end, 3),
            "x": _num(t.get("x"), 0, 1, 0.5), "y": _num(t.get("y"), 0, 1, 0.5), "size": _num(t.get("size"), 0.02, 0.2, 0.06),
            "color": color, "style": t.get("style") if t.get("style") in TEXT_STYLES else "outline",
            "anim": t.get("anim") if t.get("anim") in ANIMS else "pop",
        })
    music = None
    m = edit.get("music") or None
    if m and m.get("file") and (audio.MUSIC_DIR / Path(m["file"]).name).exists():
        music = {"file": Path(m["file"]).name, "name": str(m.get("name") or "")[:80],
                 "start": _num(m.get("start"), 0, total, 0), "in": _num(m.get("in"), 0, 3600, 0),
                 "length": _num(m.get("length"), 0, 3600, 0) or None, "volume": _num(m.get("volume"), 0, 2, 0.35),
                 "fade_in": _num(m.get("fade_in"), 0, 10, 0.5), "fade_out": _num(m.get("fade_out"), 0, 10, 1.5)}
    return {"clips": clips, "texts": texts, "music": music,
            "audio": {"original": _num((edit.get("audio") or {}).get("original"), 0, 2, 1)}}


def duration(edit: dict) -> float:
    return sum((c["out"] - c["in"]) / c["speed"] for c in edit.get("clips") or [])


def fresh(base_duration: float) -> dict:
    """A new edit: the whole base video as one clip."""
    return {"clips": [{"id": uuid.uuid4().hex[:8], "in": 0, "out": round(base_duration, 3), "speed": 1, "volume": 1,
                       "look": "none", "adjust": {"brightness": 1, "contrast": 1, "saturation": 1}}],
            "texts": [], "music": None, "audio": {"original": 1}}


# ------------------------------------------------------------------ looks (CSS filter math)

def _saturate(s):
    return [[0.213 + 0.787 * s, 0.715 - 0.715 * s, 0.072 - 0.072 * s],
            [0.213 - 0.213 * s, 0.715 + 0.285 * s, 0.072 - 0.072 * s],
            [0.213 - 0.213 * s, 0.715 - 0.715 * s, 0.072 + 0.928 * s]]


def _sepia(a):
    k = 1 - a
    return [[0.393 + 0.607 * k, 0.769 - 0.769 * k, 0.189 - 0.189 * k],
            [0.349 - 0.349 * k, 0.686 + 0.314 * k, 0.168 - 0.168 * k],
            [0.272 - 0.272 * k, 0.534 - 0.534 * k, 0.131 + 0.869 * k]]


def _mixer(m) -> str:
    keys = [["rr", "rg", "rb"], ["gr", "gg", "gb"], ["br", "bg", "bb"]]
    return "colorchannelmixer=" + ":".join(f"{keys[i][j]}={m[i][j]:.5f}" for i in range(3) for j in range(3))


def look_filters(clip: dict) -> list[str]:
    """ffmpeg filters for a clip's look, step by step like CSS (each step clamps)."""
    adj = clip.get("adjust") or {}
    steps = LOOKS.get(clip.get("look", "none"), []) + [
        ("brightness", adj.get("brightness", 1)), ("contrast", adj.get("contrast", 1)), ("saturate", adj.get("saturation", 1))]
    out = []
    for name, v in steps:
        if abs(v - (0 if name in ("sepia", "grayscale") else 1)) < 1e-3:
            continue
        if name == "brightness":
            out.append(_mixer([[v, 0, 0], [0, v, 0], [0, 0, v]]))
        elif name == "contrast":
            e = f"clip((val-127.5)*{v:.4f}+127.5\\,0\\,255)"
            out.append(f"lutrgb=r='{e}':g='{e}':b='{e}'")
        elif name == "saturate":
            out.append(_mixer(_saturate(v)))
        elif name == "grayscale":
            out.append(_mixer(_saturate(1 - v)))
        elif name == "sepia":
            out.append(_mixer(_sepia(v)))
    return (["format=gbrp"] + out) if out else []


# ------------------------------------------------------------------ titles (ASS)

def _font_ratio(font: Path) -> float:
    """libass sizes fonts by ascent + descent; CSS by the em. Ratio for this font."""
    asc, desc = ImageFont.truetype(str(font), 1000).getmetrics()
    return (asc + desc) / 1000


def _ass_color(hexcolor: str, alpha: int = 0) -> str:
    r, g, b = hexcolor[1:3], hexcolor[3:5], hexcolor[5:7]
    return f"&H{alpha:02X}{b}{g}{r}".upper()


def _ass_time(t: float) -> str:
    cs = int(round(max(0.0, t) * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def titles_ass(texts: list[dict], size: tuple[int, int], family: str, font: Path) -> str:
    w, h = size
    ratio = _font_ratio(font)
    styles, events = [], []
    for i, t in enumerate(texts):
        px = t["size"] * h  # CSS font-size in px
        fs = px * ratio
        pad = max(2, round(px * 0.18))
        # Same as the preview: outline = stroke 7% of the size, box = 65% black with 18% padding,
        # shadow = 60% black offset 6% down-right.
        border_style, outline, shadow, out_col, back = 1, 0, 0, "&H00000000", "&H00000000"
        if t["style"] == "outline":
            outline = max(1, round(px * 0.07))
        elif t["style"] == "box":
            border_style, outline, out_col = 3, pad, "&H59000000"
        elif t["style"] == "shadow":
            shadow, back = max(1, round(px * 0.06)), "&H66000000"
        name = f"T{i}"
        primary = _ass_color(t["color"])
        secondary = "&HFF000000" if t["anim"] == "type" else primary
        styles.append(f"Style: {name},{family},{fs:.1f},{primary},{secondary},{out_col},{back},-1,0,0,0,100,100,0,0,"
                      f"{border_style},{outline},{shadow},5,0,0,0,1")
        x, y = round(t["x"] * w), round(t["y"] * h)
        tags = [f"\\an5\\pos({x},{y})"]
        body = t["text"].replace("\\", "/").replace("{", "(").replace("}", ")")
        lines = body.split("\n")
        if t["anim"] == "fade":
            tags.append("\\fad(250,200)")
        elif t["anim"] == "pop":
            tags.append("\\fad(120,150)\\fscx40\\fscy40\\t(0,180,\\fscx108\\fscy108)\\t(180,260,\\fscx100\\fscy100)")
        elif t["anim"] == "slide":
            tags = [f"\\an5\\move({x},{y + round(0.04 * h)},{x},{y},0,280)\\fad(200,150)"]
        if t["anim"] == "type":
            chars = list("\n".join(lines))
            per = max(1, round(min(1.0, 0.04 * len(chars)) * 100 / max(1, len(chars))))  # centiseconds per char
            # \ko: each character (fill and outline) appears when its turn comes
            text = "".join(("\\N" if c == "\n" else f"{{\\ko{per}}}{c}") for c in chars)
        else:
            text = "\\N".join(lines)
        events.append(f"Dialogue: 0,{_ass_time(t['start'])},{_ass_time(t['end'])},{name},,0,0,0,,{{{''.join(tags)}}}{text}")
    return "\n".join([
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {w}", f"PlayResY: {h}", "WrapStyle: 2", "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
        "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
        "MarginR, MarginV, Encoding",
        *styles, "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text", *events, ""])


# ------------------------------------------------------------------ render

def _atempo(speed: float) -> list[str]:
    out, s = [], speed
    while s < 0.5:
        out.append("atempo=0.5"); s /= 0.5  # noqa: E702
    while s > 2.0:
        out.append("atempo=2.0"); s /= 2.0  # noqa: E702
    if abs(s - 1) > 1e-3:
        out.append(f"atempo={s:.5f}")
    return out


async def export(base: Path, edit: dict, out: Path, work: Path, family: str | None = None, font: Path | None = None) -> Path:
    """Apply an edit to the base video → out (H.264/AAC, the base's size, 30 fps)."""
    work.mkdir(parents=True, exist_ok=True)
    font = font or FONT_PATH
    family = family or render.FONT_NAME
    w, h = await render._ffmpeg_probe_size(base)
    has_audio = await render._has_audio(base)
    total = duration(edit)
    if total <= 0:
        raise ValueError("The timeline is empty")
    args = ["-i", base]
    graph = []
    for i, c in enumerate(edit["clips"]):
        v = [f"trim=start={c['in']:.3f}:end={c['out']:.3f}", f"setpts=(PTS-STARTPTS)/{c['speed']:.5f}",
             f"scale={w}:{h}", "setsar=1", "fps=30", *look_filters(c), "format=yuv420p"]
        graph.append(f"[0:v]{','.join(v)}[v{i}]")
        seg = (c["out"] - c["in"]) / c["speed"]
        if has_audio:
            a = [f"atrim=start={c['in']:.3f}:end={c['out']:.3f}", "asetpts=PTS-STARTPTS", *_atempo(c["speed"]),
                 f"volume={c['volume'] * edit['audio']['original']:.3f}", "aformat=sample_rates=44100:channel_layouts=stereo",
                 f"apad=whole_dur={seg:.3f}", f"atrim=end={seg:.3f}"]
            graph.append(f"[0:a]{','.join(a)}[a{i}]")
        else:
            graph.append(f"anullsrc=r=44100:cl=stereo,atrim=end={seg:.3f}[a{i}]")
    n = len(edit["clips"])
    graph.append("".join(f"[v{i}][a{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=1[vc][ac]")
    vlast, alast = "vc", "ac"
    if edit.get("texts"):
        fonts = work / "fonts"
        fonts.mkdir(exist_ok=True)
        (fonts / font.name).write_bytes(font.read_bytes())
        (work / "titles.ass").write_text(titles_ass(edit["texts"], (w, h), family, font), encoding="utf-8")
        graph.append(f"[{vlast}]ass=titles.ass:fontsdir=fonts[vt]")
        vlast = "vt"
    if (m := edit.get("music")):
        track = audio.MUSIC_DIR / m["file"]
        args += ["-i", track]
        avail = max(0.1, await render.media_duration(track) - m["in"])
        length = min(m.get("length") or avail, avail, max(0.1, total - m["start"]))
        ms = int(m["start"] * 1000)
        chain = [f"atrim=start={m['in']:.3f}:end={m['in'] + length:.3f}", "asetpts=PTS-STARTPTS",
                 "aformat=sample_rates=44100:channel_layouts=stereo", f"volume={m['volume']:.3f}"]
        if m["fade_in"] > 0:
            chain.append(f"afade=t=in:st=0:d={min(m['fade_in'], length):.2f}")
        if m["fade_out"] > 0:
            chain.append(f"afade=t=out:st={max(0, length - m['fade_out']):.2f}:d={min(m['fade_out'], length):.2f}")
        chain.append(f"adelay={ms}|{ms}")
        graph.append(f"[1:a]{','.join(chain)}[mu]")
        graph.append(f"[{alast}][mu]amix=inputs=2:duration=first:normalize=0[am]")
        alast = "am"
    await render._ffmpeg(*args, "-filter_complex", ";".join(graph), "-map", f"[{vlast}]", "-map", f"[{alast}]",
                         "-c:v", "libx264", "-preset", "veryfast", "-crf", "19", "-pix_fmt", "yuv420p",
                         "-c:a", "aac", "-b:a", "160k", "-t", f"{total:.3f}", "-movflags", "+faststart", out.resolve(), cwd=work)
    return out
