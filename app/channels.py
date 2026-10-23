"""Channels: a project built around one topic for YouTube and TikTok.

Claude plans the channel once (one call): names, position, content pillars, bios, colors and its first videos. It can
look at channels already in the niche first: their video list (titles and views, read with yt-dlp — no download, no
Claude) goes into that call. Approving the plan makes an ordinary project with a brand kit, instructions and an idea
board. Profile pictures and banners are drawn here from the brand kit, without Claude.
"""
from __future__ import annotations

import re
import shutil
import statistics
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from . import brands
from .config import DATA_DIR, FONT_PATH

CHANNELS_DIR = DATA_DIR / "channels"
MAX_VIDEOS = 60      # the newest videos listed per channel
TOP = 12             # the most viewed of them that Claude reads
AVATAR = (800, 800)
BANNER = (2560, 1440)
BANNER_SAFE = (1546, 423)  # what YouTube shows on every device, centered
LOOKS = ("color", "dark", "light")


class _Quiet:
    """yt-dlp prints expected misses (a channel without a Shorts tab) as errors; they're handled here."""
    def debug(self, msg): pass
    warning = error = debug


# ------------------------------------------------------------------ channels already in the niche

def _listing_urls(url: str) -> list[str]:
    """A YouTube channel link lists its Shorts first, then its videos; other sites as given."""
    url = url.strip().split("?")[0].rstrip("/")
    m = re.match(r"^https?://(?:www\.|m\.)?youtube\.com/(@[^/]+|channel/[^/]+|c/[^/]+|user/[^/]+)(/.*)?$", url)
    if m and not m.group(2):
        base = f"https://www.youtube.com/{m.group(1)}"
        return [f"{base}/shorts", f"{base}/videos"]
    return [url]


def study(url: str) -> dict:
    """What a channel posts and what does best there: its newest videos with their views. Slow (network): run in a thread."""
    import yt_dlp  # only needed here and for reference links

    if not re.match(r"^https?://\S+$", url.strip()) or len(url) > 500:
        raise ValueError("Paste the link of a channel (it starts with https://)")
    info, error = None, None
    for u in _listing_urls(url):
        try:
            with yt_dlp.YoutubeDL({"extract_flat": "in_playlist", "playlistend": MAX_VIDEOS, "quiet": True,
                                   "no_warnings": True, "logger": _Quiet(), "socket_timeout": 30}) as ydl:
                info = ydl.extract_info(u, download=False)
        except Exception as e:
            error = str(e)
            continue
        if info and info.get("entries"):
            break
    entries = [e for e in (info or {}).get("entries") or [] if e and e.get("title")]
    if not entries:
        raise ValueError(f"Couldn't list the videos of {url.strip()}" + (f" ({error[:160]})" if error else "")
                         + " — check the link, or leave it out")
    videos = [{"title": str(e["title"])[:150], "views": e.get("view_count"), "duration": e.get("duration"),
               "url": e.get("url") or e.get("webpage_url")} for e in entries]
    views = [v["views"] for v in videos if v["views"]]
    name = info.get("channel") or info.get("uploader") or re.sub(r" - (Shorts|Videos)$", "", info.get("title") or "")
    return {"url": url.strip(), "name": (name or url.strip())[:80], "site": "TikTok" if "TikTok" in (info.get("extractor_key") or "") else
            "YouTube" if "Youtube" in (info.get("extractor_key") or "") else (info.get("extractor_key") or ""),
            "followers": info.get("channel_follower_count"), "about": (info.get("description") or "")[:400],
            "count": len(videos), "median_views": int(statistics.median(views)) if views else None,
            "top": sorted(videos, key=lambda v: -(v["views"] or 0))[:TOP] if views else videos[:TOP],
            "recent": [v["title"] for v in videos[:6]]}


def search(queries: list[str], per_query: int = 40, studied: int = 8) -> list[dict]:
    """Channels abroad that do well on these YouTube searches: the most viewed results grouped by channel, then the
    best of them studied (followers, median views, what does best there). Slow (network): run in a thread."""
    import yt_dlp
    from concurrent.futures import ThreadPoolExecutor
    from urllib.parse import quote_plus

    found: dict[str, dict] = {}
    # Sorted by views finds the proven channels; by relevance (YouTube favors recent hits) the small ones rising now.
    for q, sort in [(q, sort) for q in queries[:3] for sort in ("&sp=CAM%3D", "")]:
        url = f"https://www.youtube.com/results?search_query={quote_plus(q)}{sort}"
        try:
            with yt_dlp.YoutubeDL({"extract_flat": "in_playlist", "playlistend": per_query, "quiet": True,
                                   "no_warnings": True, "logger": _Quiet(), "socket_timeout": 30}) as ydl:
                entries = ydl.extract_info(url, download=False).get("entries") or []
        except Exception:
            continue
        for e in entries:
            cid, link = e.get("channel_id"), e.get("uploader_url") or e.get("channel_url")
            if not cid or not link or not e.get("title"):
                continue
            c = found.setdefault(cid, {"name": e.get("channel") or e.get("uploader") or "", "url": link, "hits": 0,
                                       "views": 0, "videos": [], "queries": set(), "by_views": False})
            c["by_views"] |= bool(sort)
            c["hits"] += 1
            c["views"] += e.get("view_count") or 0
            c["queries"].add(q)
            c["videos"].append({"title": str(e["title"])[:150], "views": e.get("view_count"), "duration": e.get("duration")})
    if not found:
        raise ValueError("YouTube returned nothing for these searches — try other words")
    rank = lambda c: -(c["views"] * (1 + 0.3 * (c["hits"] - 1)))  # noqa: E731
    best = sorted((c for c in found.values() if c["by_views"]), key=rank)[:studied - 3]
    best += sorted((c for c in found.values() if not c["by_views"]), key=rank)[:studied - len(best)]

    def look(c: dict) -> dict:
        try:
            s = study(c["url"])
        except Exception:
            s = {"followers": None, "median_views": None, "top": [], "recent": [], "count": 0, "about": "", "site": "YouTube"}
        ratio = round(s["median_views"] / s["followers"], 2) if s.get("median_views") and s.get("followers") else None
        return {**s, "name": c["name"] or s.get("name"), "url": c["url"], "site": "YouTube", "ratio": ratio,
                "search_hits": c["hits"], "search_views": c["views"], "queries": sorted(c["queries"]),
                "search_top": sorted(c["videos"], key=lambda v: -(v["views"] or 0))[:3]}

    with ThreadPoolExecutor(4) as pool:
        return list(pool.map(look, best))


def discovery_block(found: list[dict]) -> str:
    """Channels found by searching, as Claude reads them."""
    parts = []
    for n, c in enumerate(found, 1):
        lines = [f"{n}. {c['name']} — {_num(c.get('followers'))} followers, median {_num(c.get('median_views'))} views"
                 + (f" (median views / followers = {c['ratio']})" if c.get("ratio") else "")
                 + f", {c.get('count') or 0} recent videos listed"]
        if c.get("about"):
            lines.append(f"   About: {c['about'][:200]}")
        lines += [f"   - {v['title']} — {_num(v.get('views'))} views" + (f", {round(v['duration'])}s" if v.get("duration") else "")
                  for v in (c.get("top") or c.get("search_top") or [])[:5]]
        parts.append("\n".join(lines))
    return "\n".join(parts)


def _num(n) -> str:
    if not n:
        return "?"
    for size, unit in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if n >= size:
            return f"{n / size:.1f}".rstrip("0").rstrip(".") + unit
    return str(n)


def prompt_block(found: list[dict]) -> str:
    """Channels already in the niche, as Claude reads them: what they are and what does best there."""
    parts = []
    for c in found[:3]:
        head = f"{c['name']} ({c.get('site') or 'channel'}, {_num(c.get('followers'))} followers, median {_num(c.get('median_views'))} views)"
        lines = [head] + ([f"About: {c['about'][:300]}"] if c.get("about") else []) + ["Most viewed of its latest videos:"]
        lines += [f"- {v['title']} — {_num(v.get('views'))} views" + (f", {round(v['duration'])}s" if v.get("duration") else "")
                  for v in c.get("top") or []]
        if c.get("recent"):
            lines.append("Latest: " + " | ".join(c["recent"][:5]))
        parts.append("\n".join(lines))
    return ("Channels already in this niche (to learn what works and find a gap, never to copy):\n\n" + "\n\n".join(parts)) if parts else ""


# ------------------------------------------------------------------ profile picture and banner

def art_dir(wid: str) -> Path:
    return CHANNELS_DIR / Path(wid).name


def remove(wid: str):
    shutil.rmtree(art_dir(wid), ignore_errors=True)


def _rgb(c: str) -> tuple[int, int, int]:
    c = (c or "#000000").lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4)) if len(c) == 6 else (0, 0, 0)


def _lum(rgb) -> float:
    return (0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]) / 255


def _mix(a, b, t: float) -> tuple[int, int, int]:
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def _gradient(size: tuple[int, int], top, bottom) -> Image.Image:
    w, h = size
    col = Image.new("RGB", (1, h))
    for y in range(h):
        col.putpixel((0, y), _mix(top, bottom, y / max(1, h - 1)))
    return col.resize(size)


def _palette(colors: dict, look: str) -> tuple:
    """Background top, bottom, the text and the accent of a look."""
    primary, accent = _rgb(colors.get("primary")), _rgb(colors.get("accent"))
    if look == "dark":
        bg = (16, 17, 22)
        return bg, _mix(bg, primary, 0.35), (255, 255, 255), accent if _lum(accent) > 0.35 else primary
    if look == "light":
        bg = (250, 248, 244)
        ink = primary if _lum(primary) < 0.55 else (24, 24, 28)
        return bg, _mix(bg, primary, 0.12), ink, primary
    ink = (255, 255, 255) if _lum(primary) < 0.6 else (20, 20, 24)
    return _mix(primary, (255, 255, 255), 0.08), _mix(primary, (0, 0, 0), 0.35), ink, accent


def _font(path: Path, size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(str(path), size)
    except OSError:
        return ImageFont.truetype(str(FONT_PATH), size)


def _fit(draw: ImageDraw.ImageDraw, text: str, path: Path, max_w: int, size: int, min_size: int = 20):
    while size > min_size and draw.textlength(text, font=_font(path, size)) > max_w:
        size = int(size * 0.92)
    return _font(path, size)


def _initials(name: str) -> str:
    words = [w for w in re.split(r"[\s._\-]+", name) if w]
    if not words:
        return "?"
    return (words[0][0] + (words[1][0] if len(words) > 1 else "")).upper()


def _avatar(size: tuple[int, int], name: str, colors: dict, look: str, font: Path, logo: Path | None) -> Image.Image:
    top, bottom, ink, accent = _palette(colors, look)
    img = _gradient(size, top, bottom)
    d = ImageDraw.Draw(img)
    w, h = size
    d.ellipse((w * 0.08, h * 0.08, w * 0.92, h * 0.92), outline=accent, width=max(6, w // 60))  # inside the circle crop
    if logo:
        mark = Image.open(logo).convert("RGBA")
        mark.thumbnail((int(w * 0.5), int(h * 0.5)))
        img.paste(mark, ((w - mark.width) // 2, (h - mark.height) // 2), mark)
        return img
    text = _initials(name)
    f = _fit(d, text, font, int(w * 0.56), int(h * 0.42))
    d.text((w / 2, h / 2), text, font=f, fill=ink, anchor="mm")
    return img


def _banner(size: tuple[int, int], name: str, tagline: str, colors: dict, look: str, font: Path) -> Image.Image:
    top, bottom, ink, accent = _palette(colors, look)
    img = _gradient(size, top, bottom)
    d = ImageDraw.Draw(img)
    w, h = size
    sw, sh = BANNER_SAFE
    # Shapes at the edges (seen on TVs and wide screens only), the words inside the safe area.
    r = h * 0.55
    d.ellipse((-r * 0.6, -r * 0.7, r * 0.9, r * 0.8), fill=_mix(top, accent, 0.18))
    d.ellipse((w - r * 0.8, h - r * 0.9, w + r * 0.7, h + r * 0.6), fill=_mix(bottom, accent, 0.22))
    title = _fit(d, name, font, int(sw * 0.9), int(sh * 0.42))
    if not tagline:
        d.text((w / 2, h / 2), name, font=title, fill=ink, anchor="mm")
        return img
    sub = _fit(d, tagline, font, int(sw * 0.86), int(sh * 0.16))
    gap = sh * 0.07
    th = d.textbbox((0, 0), name, font=title, anchor="ls")
    bh = d.textbbox((0, 0), tagline, font=sub, anchor="ls")
    total = (th[3] - th[1]) + gap + 10 + gap + (bh[3] - bh[1])
    y = (h - total) / 2 - th[1]  # the title's baseline
    d.text((w / 2, y), name, font=title, fill=ink, anchor="ms")
    bar = y + th[3] + gap
    d.rounded_rectangle((w / 2 - sw * 0.05, bar, w / 2 + sw * 0.05, bar + 10), radius=5, fill=accent)
    d.text((w / 2, bar + 10 + gap - bh[1]), tagline, font=sub, fill=_mix(ink, top, 0.15), anchor="ms")
    return img


def make_art(wid: str, name: str, tagline: str, brand: dict | None, colors: dict | None = None) -> list[dict]:
    """Three looks of a profile picture (800×800, safe inside the circle crop) and a YouTube banner (2560×1440, the words
    inside the area every device shows), in the brand kit's colors and font."""
    colors = (brand or {}).get("colors") or colors or {}
    font, logo = FONT_PATH, None
    if brand and brand.get("font") and (p := brands.asset_path(brand["id"], brand["font"]["file"])).exists():
        font = p
    if brand and brand.get("logo") and (p := brands.asset_path(brand["id"], brand["logo"]["file"])).exists():
        logo = p
    d = art_dir(wid)
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    out = []
    for look in LOOKS:
        _avatar(AVATAR, name, colors, look, font, logo).save(d / f"avatar-{look}.png", optimize=True)
        _banner(BANNER, name, tagline, colors, look, font).save(d / f"banner-{look}.png", optimize=True)
        out.append({"look": look, "avatar": f"avatar-{look}.png", "banner": f"banner-{look}.png"})
    return out


def art_file(wid: str, name: str) -> Path | None:
    if not re.fullmatch(r"(avatar|banner)-(color|dark|light)\.png", name):
        return None
    p = art_dir(wid) / name
    return p if p.is_file() else None
