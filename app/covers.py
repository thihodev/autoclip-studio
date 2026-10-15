"""Cover images (thumbnails): good frames of the render with big, readable text on them.

Three candidates come from different moments of the video. The text uses the brand kit's font and accent color
when there is one. The chosen cover becomes the YouTube thumbnail (landscape videos) and its moment the TikTok cover.
"""
import textwrap
import uuid
from pathlib import Path

from . import brands, render
from .config import DATA_DIR, FONT_PATH, VIDEOS_DIR

MOMENTS = (0.06, 0.32, 0.62)  # fractions of the video


def _wrap(text: str, width: int) -> str:
    """At most 3 lines of about equal length (no lone word on the last line)."""
    text = " ".join(text.split())
    n = max(1, min(3, -(-len(text) // width)))
    for w in range(-(-len(text) // n), width + 1):
        lines = textwrap.wrap(text, width=w, break_long_words=False)
        if len(lines) <= n:
            return "\n".join(lines)
    return "\n".join(textwrap.wrap(text, width=width, break_long_words=False)[:3])


async def make(video: Path, text: str, brand: dict | None) -> list[dict]:
    dur = await render.media_duration(video)
    w, h = await render._ffmpeg_probe_size(video)
    portrait = h > w
    out_w, out_h = (1080, 1920) if portrait else (1080, 1080) if w == h else (1280, 720)
    font = FONT_PATH
    accent = "#e8385a"
    if brand:
        accent = brand["colors"].get("accent") or brand["colors"].get("primary") or accent
        if brand.get("font") and (p := brands.asset_path(brand["id"], brand["font"]["file"])).exists():
            font = p
    size = int(out_w * (0.10 if portrait else 0.075))
    tmp = DATA_DIR / "work" / f"cover_{uuid.uuid4().hex[:8]}.txt"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(_wrap(text.upper(), 13 if portrait else 20), encoding="utf-8")
    covers = []
    try:
        for i, f in enumerate(MOMENTS):
            t = round(max(0.3, min(dur - 0.2, dur * f)), 2)
            out = VIDEOS_DIR / f"{video.stem}_cover{i}.jpg"
            vf = (f"scale={out_w}:{out_h}:force_original_aspect_ratio=increase,crop={out_w}:{out_h},"
                  f"drawtext=fontfile='{font}':textfile='{tmp}':fontsize={size}:fontcolor=white:line_spacing={size // 5}:"
                  f"box=1:boxcolor={accent}@0.92:boxborderw={size // 3}:x=(w-text_w)/2:y=h*{0.68 if portrait else 0.62}-text_h/2")
            await render._ffmpeg("-ss", f"{t:.2f}", "-i", video, "-frames:v", "1", "-vf", vf, "-q:v", "3", out)
            covers.append({"file": out.name, "t": t})
    finally:
        tmp.unlink(missing_ok=True)
    return covers
