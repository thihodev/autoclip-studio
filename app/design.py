"""Direct edits to a Claude Motion video, without Claude: texts, images and videos, colors, fonts, captions and the
pause after each scene's narration, made in the HyperFrames project itself, previewed live in the browser and
rendered with HyperFrames.

The files Claude wrote stay the source of truth. Before the first change a copy of them is kept in
`.autoclip/base/` (discard), and before every change one in `.autoclip/undo/<n>/` (undo).
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import shutil
from pathlib import Path

EPS = 1e-3
MAX_HOLD = 5.0
_BASE = Path(".autoclip") / "base"
_UNDO = Path(".autoclip") / "undo"
MAX_UNDO = 40
_KEEP = ("index.html", "compositions", "caption_groups.json", "audio_meta.json")

# A frame's markup as tokens: scripts, styles and comments whole, then tags and the text between them.
_TOKENS = re.compile(r"<script\b.*?</script\s*>|<style\b.*?</style\s*>|<title\b.*?</title\s*>|<!--.*?-->|<[^>]*>|[^<]+",
                     re.S | re.I)
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr",
         "path", "circle", "ellipse", "line", "rect", "polygon", "polyline", "use", "stop"}
_HEX = re.compile(r"#([0-9a-fA-F]{6}|[0-9a-fA-F]{3})(?![\w-])")
_EL = re.compile(r"<(div|audio)\b([^>]*)>\s*</\1>", re.S)


def _attrs(s: str) -> dict:
    return dict(re.findall(r'([\w-]+)="([^"]*)"', s))


def _num(x: float) -> str:
    return f"{x:.3f}".rstrip("0").rstrip(".") or "0"


# ------------------------------------------------------------------ texts

def _slots(src: str) -> list[dict]:
    """Every piece of visible text in a frame: its place in the file, its text and the tag that holds it."""
    out, stack = [], []
    for m in _TOKENS.finditer(src):
        tok = m.group(0)
        if tok.startswith("<"):
            if tok.startswith(("<!--", "<script", "<style", "<title", "<!")) or tok.endswith("/>"):
                continue
            if tok.startswith("</"):
                name = tok[2:].strip(" >").lower()
                while stack and stack.pop()[1] != name:
                    pass
                continue
            name = re.match(r"<([\w-]+)", tok)
            if name and name.group(1).lower() not in _VOID:
                stack.append((m.start(), name.group(1).lower(), len(name.group(0))))
            continue
        text = html.unescape(tok).strip()
        if not text or not stack or stack[-1][1] in ("template", "svg", "defs", "head", "html", "body"):
            continue
        out.append({"start": m.start(), "end": m.end(), "text": text, "tag": stack[-1]})
    return out


def texts(src: str) -> list[str]:
    return [s["text"] for s in _slots(src)]


def set_text(src: str, i: int, text: str) -> str:
    slots = _slots(src)
    if not 0 <= i < len(slots):
        raise ValueError("That text is no longer in the scene — reload the editor")
    s = slots[i]
    old = src[s["start"]:s["end"]]
    lead, trail = old[:len(old) - len(old.lstrip())], old[len(old.rstrip()):]
    return src[:s["start"]] + lead + html.escape(text.strip(), quote=False) + trail + src[s["end"]:]


def annotate(src: str, scene: str) -> str:
    """The preview copy of a frame: each tag holding text says which text it is (data-ac-t="scene:i"), each image
    or video which one it is (data-ac-m="scene:i")."""
    marks: dict[int, tuple[int, str]] = {}
    for i, s in enumerate(_slots(src)):
        at, _, length = s["tag"]
        marks.setdefault(at, (length, f' data-ac-t="{scene}:{i}"'))
    for i, m in enumerate(_media(src)):
        marks[m["start"]] = (len(m["tag"]), f' data-ac-m="{scene}:{i}"')
    for at in sorted(marks, reverse=True):
        length, attr = marks[at]
        src = src[:at + length] + attr + src[at + length:]
    return src


def _tag_style(tag: str, prop: str, value: str | None) -> str:
    """The opening tag `tag` with one inline style property set (or removed when value is None)."""
    m = re.search(r'\sstyle="([^"]*)"', tag)
    rules = [r for r in (m.group(1).split(";") if m else []) if r.strip() and r.split(":")[0].strip().lower() != prop]
    if value is not None:
        rules.append(f"{prop}: {value}")
    style = "; ".join(r.strip() for r in rules)
    if m:
        return tag[:m.start()] + (f' style="{style}"' if style else "") + tag[m.end():]
    if not style:
        return tag
    name = re.match(r"<[\w-]+", tag)
    return tag[:name.end()] + f' style="{style}"' + tag[name.end():]


def fit_text(root: Path, scene_id: str, i: int, size: float):
    """Make one text smaller so it fits its box again: an inline font-size on the tag that holds it."""
    path = root / _scene(root, scene_id)["src"]
    src = path.read_text(encoding="utf-8")
    slots = _slots(src)
    if not 0 <= i < len(slots):
        raise ValueError("That text is no longer in the scene — reload the editor")
    at, _, _ = slots[i]["tag"]
    end = src.index(">", at) + 1
    _before(root)
    path.write_text(src[:at] + _tag_style(src[at:end], "font-size", f"{max(8.0, size):.1f}px") + src[end:], encoding="utf-8")


# ------------------------------------------------------------------ images and videos

_MEDIA_TAGS = {"img": "image", "image": "image", "video": "video"}
_MEDIA_EXT = {"image": (".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".avif"), "video": (".mp4", ".webm", ".mov")}


def _media(src: str) -> list[dict]:
    """Every <img>, SVG <image> and <video> in a frame, with the attribute that names its file."""
    out = []
    for m in _TOKENS.finditer(src):
        tok = m.group(0)
        name = re.match(r"<(img|image|video)\b", tok, re.I)
        if not name:
            continue
        attr = re.search(r'\s((?:xlink:)?href|src)="([^"]*)"', tok)
        if attr:
            out.append({"start": m.start(), "end": m.end(), "tag": tok[:name.end()], "raw": tok,
                        "kind": _MEDIA_TAGS[name.group(1).lower()], "attr": attr.group(1), "value": attr.group(2)})
    return out


def media(src: str) -> list[dict]:
    return [{"kind": m["kind"], "src": m["value"], "path": _root_path(m["value"])} for m in _media(src)]


def _root_path(value: str) -> str | None:
    """A frame's file reference as a path from the project root (frames resolve against index.html)."""
    if re.match(r"^(https?:|data:|blob:|//)", value):
        return None
    return re.sub(r"^(\.\./)+", "", value.split("?")[0].split("#")[0]) or None


def replace_media(root: Path, scene_id: str, i: int, data: bytes, filename: str, fit: str, everywhere: bool):
    """Put an uploaded file in place of one image or video (or of every use of the same file)."""
    path = root / _scene(root, scene_id)["src"]
    src = path.read_text(encoding="utf-8")
    items = _media(src)
    if not 0 <= i < len(items):
        raise ValueError("That image is no longer in the scene — reload the editor")
    item = items[i]
    ext = Path(filename).suffix.lower()
    if ext not in _MEDIA_EXT[item["kind"]]:
        raise ValueError(f"Use {' / '.join(e.lstrip('.').upper() for e in _MEDIA_EXT[item['kind']])} for this {item['kind']}")
    name = hashlib.sha1(data).hexdigest()[:12] + ext
    (root / "assets" / "edits").mkdir(parents=True, exist_ok=True)
    (root / "assets" / "edits" / name).write_bytes(data)
    new = ("../../" if item["value"].startswith("../") else "") + f"assets/edits/{name}"
    old = item["value"]
    _before(root)
    for f in (_files(root) if everywhere else [path]):
        text = f.read_text(encoding="utf-8")
        done = []
        for k, m in reversed(list(enumerate(_media(text)))):
            if m["value"] != old or (f == path and not everywhere and k != i):
                continue
            tag = re.sub(r'\s(?:src|(?:xlink:)?href)="[^"]*"', lambda x: x.group(0).split("=")[0] + f'="{new}"', m["raw"], count=1)
            tag = re.sub(r'\ssrcset="[^"]*"', "", tag)
            if fit in ("cover", "contain"):
                if m["kind"] == "image" and m["tag"].lower() == "<image":
                    tag = re.sub(r'\spreserveAspectRatio="[^"]*"', "", tag)
                    tag = tag[:len(m["tag"])] + f' preserveAspectRatio="xMidYMid {"slice" if fit == "cover" else "meet"}"' + tag[len(m["tag"]):]
                else:
                    tag = _tag_style(tag, "object-fit", fit)
            text = text[:m["start"]] + tag + text[m["end"]:]
            done.append(k)
        if done:
            f.write_text(text, encoding="utf-8")


# ------------------------------------------------------------------ captions

def _caption_files(root: Path) -> tuple[Path, Path]:
    return root / "caption_groups.json", root / "compositions" / "captions.html"


def captions(root: Path) -> list[dict]:
    groups, cap = _caption_files(root)
    if not groups.exists() or not cap.exists() or "var GROUPS" not in cap.read_text(encoding="utf-8"):
        return []
    return [{"id": g["id"], "frame": g.get("frame"), "start": g["start"], "end": g["end"], "text": g.get("text", "")}
            for g in json.loads(groups.read_text()).get("groups", [])]


def _retime(group: dict, words: list[str]) -> list[dict]:
    """New words for a caption group: same count keeps every word's timing, otherwise the group's span is shared
    out by word length."""
    old = group.get("words") or []
    if len(old) == len(words):
        return [{**w, "text": t} for w, t in zip(old, words)]
    t0 = old[0]["start"] if old else group["start"]
    t1 = old[-1]["end"] if old else group["end"]
    prefix = (old[0]["id"].rsplit("-", 1)[0] if old else group["id"].replace("group", "word"))
    weights = [len(w) + 1 for w in words]
    out, t = [], t0
    for k, (w, wt) in enumerate(zip(words, weights)):
        d = (t1 - t0) * wt / sum(weights)
        out.append({"id": f"{prefix}-{k}", "text": w, "start": round(t, 3), "end": round(t + d, 3)})
        t += d
    return out


def edit_caption(root: Path, gid: str, text: str):
    words = text.split()
    if not words:
        raise ValueError("A caption can't be empty")
    groups, cap = _caption_files(root)
    data = json.loads(groups.read_text())
    group = next((g for g in data.get("groups", []) if g["id"] == gid), None)
    if not group:
        raise ValueError("That caption is no longer there — reload the editor")
    if " ".join(words) == group.get("text"):
        return
    _before(root)
    group["words"], group["text"] = _retime(group, words), " ".join(words)
    groups.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    src = cap.read_text(encoding="utf-8")

    def js_groups(m: re.Match) -> str:
        items = json.loads(m.group(2))
        for g in items:
            if g.get("id") == gid:
                g["words"], g["text"] = group["words"], group["text"]
        return m.group(1) + json.dumps(items, ensure_ascii=False, separators=(",", ":")) + m.group(3)

    cap.write_text(re.sub(r"(var GROUPS\s*=\s*)(\[.*?\])(\s*;)", js_groups, src, count=1, flags=re.S), encoding="utf-8")


# ------------------------------------------------------------------ fonts

# Google Fonts that cover Vietnamese (checked against their subsets), by feel.
FONTS = {
    "Be Vietnam Pro": ("sans", "Be+Vietnam+Pro:wght@400;500;600;700;800;900"),
    "Inter": ("sans", "Inter:wght@400;500;600;700;800;900"),
    "Montserrat": ("sans", "Montserrat:wght@400;500;600;700;800;900"),
    "Plus Jakarta Sans": ("sans", "Plus+Jakarta+Sans:wght@400;500;600;700;800"),
    "Manrope": ("sans", "Manrope:wght@400;500;600;700;800"),
    "Lexend": ("sans", "Lexend:wght@400;500;600;700;800;900"),
    "Space Grotesk": ("sans", "Space+Grotesk:wght@400;500;600;700"),
    "Bricolage Grotesque": ("sans", "Bricolage+Grotesque:wght@400;500;600;700;800"),
    "Nunito": ("rounded", "Nunito:wght@400;500;600;700;800;900"),
    "Baloo 2": ("rounded", "Baloo+2:wght@400;500;600;700;800"),
    "Quicksand": ("rounded", "Quicksand:wght@400;500;600;700"),
    "Mali": ("rounded", "Mali:wght@400;500;600;700"),
    "Anton": ("display", "Anton"),
    "Oswald": ("display", "Oswald:wght@400;500;600;700"),
    "Barlow Condensed": ("display", "Barlow+Condensed:wght@400;500;600;700;800;900"),
    "Unbounded": ("display", "Unbounded:wght@400;500;600;700;800;900"),
    "Bungee": ("display", "Bungee"),
    "Chakra Petch": ("display", "Chakra+Petch:wght@400;500;600;700"),
    "Playfair Display": ("serif", "Playfair+Display:wght@400;500;600;700;800;900"),
    "Roboto Slab": ("serif", "Roboto+Slab:wght@400;500;600;700;800;900"),
    "Patrick Hand": ("hand", "Patrick+Hand"),
    "Pacifico": ("hand", "Pacifico"),
    "Lobster": ("hand", "Lobster"),
    "Dancing Script": ("hand", "Dancing+Script:wght@400;500;600;700"),
}
_GENERIC = {"serif", "sans-serif", "monospace", "cursive", "fantasy", "system-ui", "emoji", "math", "fangsong",
            "inherit", "initial", "unset", "revert", "-apple-system", "blinkmacsystemfont", "ui-sans-serif",
            "ui-serif", "ui-monospace", "ui-rounded"}
_FACE = re.compile(r"@font-face\s*\{[^}]*\}", re.I)
_FAMILY_DECL = re.compile(r"(font-family\s*:\s*|--[\w-]*font[\w-]*\s*:\s*|fontFamily\s*:\s*)([^;{}\n]+)", re.I)


def _masked(src: str) -> str:
    """The file with its @font-face rules blanked out (same length), so their names are left alone."""
    return _FACE.sub(lambda m: " " * len(m.group(0)), src)


def _families(value: str) -> list[str]:
    out = []
    for part in value.split(","):
        name = part.strip().strip("\"'` ").strip()
        if name and not name.lower().startswith("var(") and name.lower() not in _GENERIC and len(name) < 60:
            out.append(name)
    return out


def _weight(v: str | None) -> int:
    v = (v or "").strip().lower()
    return {"normal": 400, "bold": 700, "lighter": 300, "bolder": 800}.get(v) or (int(v) if v.isdigit() else 400)


def _rule(masked: str, at: int) -> tuple[int, int] | None:
    """The CSS rule block {…} around a declaration (None for inline styles and scripts)."""
    start, end = masked.rfind("{", 0, at), masked.find("}", at)
    if start < 0 or end < 0 or "}" in masked[start + 1:at] or "<" in masked[start:end]:
        return None
    return start, end


def fonts(root: Path) -> list[dict]:
    count: dict[str, int] = {}
    weights: dict[str, set] = {}
    for f in _files(root):
        masked = _masked(f.read_text(encoding="utf-8"))
        for m in _FAMILY_DECL.finditer(masked):
            for name in _families(m.group(2))[:1]:  # the font that shows; the rest are fallbacks
                count[name] = count.get(name, 0) + 1
                weights.setdefault(name, set())
                if m.group(1).lower().startswith("font-family"):  # the weights it's used at, for the swap
                    rule = _rule(masked, m.start())
                    w = re.search(r"font-weight\s*:\s*([^;}\n]+)", masked[rule[0]:rule[1]]) if rule else None
                    weights[name].add(_weight(w.group(1)) if w else 400)
    return [{"family": n, "count": c, "weights": sorted(weights.get(n) or {400}), "vietnamese": n in FONTS or None}
            for n, c in sorted(count.items(), key=lambda x: -x[1])]


def edit_font(root: Path, old: str, new: str, weights: dict | None = None):
    """Swap one font for another everywhere. `weights` maps the weights used with the old font to the new font's
    weight with the same visual thickness (measured in the browser), so a heavy one-weight display font doesn't
    turn into a thin regular."""
    if new not in FONTS:
        raise ValueError("Pick one of the listed fonts")
    if old == new:
        return
    link = f'<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family={FONTS[new][1]}&display=block" />'
    name = re.compile(r"([\"']?)" + re.escape(old) + r"\1(?=\s*(?:,|;|$|\"|'|\)|!))")
    wmap = {int(k): int(v) for k, v in (weights or {}).items() if str(k).isdigit() and str(v).isdigit()}
    changes = []
    for f in _files(root):
        src = f.read_text(encoding="utf-8")
        masked, edits = _masked(src), []
        for m in _FAMILY_DECL.finditer(masked):
            value = src[m.start(2):m.end(2)]
            js = "fontFamily" in m.group(1)
            quote = "" if js else '"'  # inside a JS string: no quotes of our own
            swapped = name.sub(lambda x: f"{x.group(1) or quote}{new}{x.group(1) or quote}", value)
            if swapped == value:
                continue
            edits.append((m.start(2), m.end(2), swapped))
            rule = None if js or not m.group(1).lower().startswith("font-family") or _families(value)[:1] != [old] else _rule(masked, m.start())
            if not rule or not wmap:
                continue
            w = re.search(r"(font-weight\s*:\s*)([^;}\n]+)", masked[rule[0]:rule[1]])
            was = _weight(w.group(2) if w else None)
            to = wmap.get(was) or wmap[min(wmap, key=lambda k: abs(k - was))]
            if to == was:
                continue
            if w:
                edits.append((rule[0] + w.start(2), rule[0] + w.end(2), str(to)))
            else:
                semi = masked.find(";", m.end(2) - 1, rule[1])
                at = semi + 1 if semi >= 0 else m.end(2)
                edits.append((at, at, f" font-weight: {to};" if semi >= 0 else f"; font-weight: {to}"))
        if not edits:
            continue
        text = src
        for start, end, rep in sorted(edits, reverse=True):
            text = text[:start] + rep + text[end:]
        if FONTS[new][1].split(":")[0] not in text:
            t = re.search(r"<template\b[^>]*>", text)
            text = (text[:t.end()] + "\n" + link + text[t.end():]) if t else \
                text.replace("</head>", link + "\n</head>", 1) if "</head>" in text else link + "\n" + text
        changes.append((f, text))
    if changes:
        _before(root)
        for f, text in changes:
            f.write_text(text, encoding="utf-8")


# ------------------------------------------------------------------ the project

def _files(root: Path) -> list[Path]:
    return [root / "index.html", *sorted((root / "compositions").rglob("*.html"))]


def scenes(root: Path) -> list[dict]:
    index = (root / "index.html").read_text(encoding="utf-8")
    els = [(tag, _attrs(a)) for tag, a in _EL.findall(index)]
    voices = {a["id"].removesuffix("-voice"): a for tag, a in els if tag == "audio" and a.get("id", "").endswith("-voice")}
    total = _total(index)
    frames = [a for tag, a in els if tag == "div" and "frames/" in a.get("data-composition-src", "")]
    out = []
    for k, a in enumerate(frames):
        start, dur = float(a.get("data-start", 0)), float(a.get("data-duration", 0))
        v = voices.get(a.get("id", ""))
        end = float(frames[k + 1]["data-start"]) if k + 1 < len(frames) else total
        spoken = float(v["data-start"]) + float(v["data-duration"]) if v else end
        src = root / a["data-composition-src"]
        out.append({"id": a["data-composition-id"], "src": a["data-composition-src"], "start": round(start, 3),
                    "end": round(end, 3), "duration": dur, "hold": round(max(0.0, end - spoken), 2),
                    "texts": texts(src.read_text(encoding="utf-8")) if src.exists() else [],
                    "media": media(src.read_text(encoding="utf-8")) if src.exists() else []})
    return out


def _total(index: str) -> float:
    m = re.search(r'<div\s+id="root"[^>]*?data-duration="([\d.]+)"', index, re.S)
    return float(m.group(1)) if m else 0.0


def colors(root: Path) -> list[dict]:
    """The video's palette, most used first: #rrggbb and how often it appears."""
    count: dict[str, int] = {}
    for f in _files(root):
        for m in _HEX.finditer(f.read_text(encoding="utf-8")):
            h = _norm(m.group(1))
            count[h] = count.get(h, 0) + 1
    return [{"hex": h, "count": n} for h, n in sorted(count.items(), key=lambda x: -x[1])][:24]


def _norm(h: str) -> str:
    h = h.lower().lstrip("#")
    return "#" + ("".join(c * 2 for c in h) if len(h) == 3 else h)


def state(root: Path) -> dict:
    index = (root / "index.html").read_text(encoding="utf-8")
    w = re.search(r'data-width="(\d+)"', index)
    h = re.search(r'data-height="(\d+)"', index)
    return {"scenes": scenes(root), "colors": colors(root), "fonts": fonts(root), "captions": captions(root),
            "font_choices": [{"family": k, "group": v[0], "css": v[1]} for k, v in FONTS.items()],
            "duration": _total(index), "dirty": dirty(root), "undo": len(_undo_steps(root)),
            "width": int(w.group(1)) if w else 1080, "height": int(h.group(1)) if h else 1920}


# ------------------------------------------------------------------ keep / discard

def dirty(root: Path) -> bool:
    return (root / _BASE).exists()


def _copy(root: Path, dest: Path):
    tmp = dest.with_name(dest.name + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    for name in _KEEP:
        p = root / name
        if p.is_dir():
            shutil.copytree(p, tmp / name)
        elif p.exists():
            shutil.copy2(p, tmp / name)
    tmp.rename(dest)


def _restore(root: Path, src: Path):
    for name in _KEEP:
        a, b = src / name, root / name
        if a.is_dir():
            shutil.rmtree(b, ignore_errors=True)
            shutil.copytree(a, b)
        elif a.exists():
            shutil.copy2(a, b)


def _undo_steps(root: Path) -> list[Path]:
    d = root / _UNDO
    return sorted((p for p in d.iterdir() if p.is_dir() and p.name.isdigit()), key=lambda p: int(p.name)) if d.exists() else []


def _before(root: Path):
    """Called before every change: the copy for discard (first change) and one undo step."""
    if not (root / _BASE).exists():
        _copy(root, root / _BASE)
    steps = _undo_steps(root)
    _copy(root, root / _UNDO / str(int(steps[-1].name) + 1 if steps else 1))
    for old in steps[:max(0, len(steps) + 1 - MAX_UNDO)]:
        shutil.rmtree(old, ignore_errors=True)


def undo(root: Path):
    steps = _undo_steps(root)
    if not steps:
        raise ValueError("Nothing to undo")
    _restore(root, steps[-1])
    shutil.rmtree(steps[-1])
    if len(steps) == 1:  # back where the edits started
        keep(root)


def discard(root: Path):
    base = root / _BASE
    if base.exists():
        _restore(root, base)
    keep(root)


def keep(root: Path):
    """The changes are part of the video now (rendered, or handed to Claude): forget the copies."""
    shutil.rmtree(root / ".autoclip", ignore_errors=True)


def _scene(root: Path, scene_id: str) -> dict:
    for s in scenes(root):
        if s["id"] == scene_id:
            return s
    raise ValueError("Unknown scene")


# ------------------------------------------------------------------ edits

def edit_text(root: Path, scene_id: str, i: int, text: str):
    if not text.strip():
        raise ValueError("Text can't be empty — ask Claude in AI & review to remove something")
    path = root / _scene(root, scene_id)["src"]
    src = path.read_text(encoding="utf-8")
    new = set_text(src, i, text)
    if new != src:
        _before(root)
        path.write_text(new, encoding="utf-8")


def edit_color(root: Path, old: str, new: str):
    old, new = _norm(old), _norm(new)
    if not re.fullmatch(r"#[0-9a-f]{6}", new) or old == new:
        return
    _before(root)
    for f in _files(root):
        src = f.read_text(encoding="utf-8")
        out = _HEX.sub(lambda m: new if _norm(m.group(1)) == old else m.group(0), src)
        if out != src:
            f.write_text(out, encoding="utf-8")


def edit_hold(root: Path, scene_id: str, hold: float):
    """Set the pause after a scene's narration: everything after the scene moves by the difference."""
    s = _scene(root, scene_id)
    delta = round(min(MAX_HOLD, max(0.0, hold)) - s["hold"], 3)
    if abs(delta) < 0.01:
        return
    _before(root)
    b, total = s["end"], _total((root / "index.html").read_text(encoding="utf-8"))
    _shift_index(root / "index.html", b, delta, total)
    _shift_captions(root, b, delta, total)
    frame = root / s["src"]
    old = s["duration"]
    frame.write_text(re.sub(r'data-duration="([\d.]+)"',
                            lambda m: f'data-duration="{_num(old + delta)}"' if abs(float(m.group(1)) - old) < EPS else m.group(0),
                            frame.read_text(encoding="utf-8")), encoding="utf-8")
    meta = root / "audio_meta.json"
    if meta.exists():
        data = json.loads(meta.read_text())
        n = [x["id"] for x in scenes(root)].index(s["id"]) + 1
        for v in data.get("voices", []):
            if v.get("frame") == n:
                v["hold_s"] = round(max(0.0, v.get("hold_s", 0) + delta), 3)
        meta.write_text(json.dumps(data, indent=2))


def _shift_index(path: Path, b: float, delta: float, total: float):
    index = path.read_text(encoding="utf-8")

    def element(m: re.Match) -> str:
        tag, raw = m.group(1), m.group(2)
        a = _attrs(raw)
        if "data-start" not in a:
            return m.group(0)
        start, dur = float(a["data-start"]), float(a.get("data-duration", 0))
        src = a.get("src", "")
        if start >= b - EPS:  # after the scene: moves
            raw = re.sub(r'data-start="[^"]*"', f'data-start="{_num(start + delta)}"', raw, count=1)
        elif start + dur > b + EPS and (tag == "div" or not re.search(r"/(voice|sfx)/", src)):  # spans it: stretches
            raw = re.sub(r'data-duration="[^"]*"', f'data-duration="{_num(dur + delta)}"', raw, count=1)
        whole, at = m.group(0), m.start()
        return whole[:m.start(2) - at] + raw + whole[m.end(2) - at:]

    index = _EL.sub(element, index)
    index = re.sub(r'(<div\s+id="root"[^>]*?data-duration=")([\d.]+)(")',
                   lambda m: m.group(1) + _num(float(m.group(2)) + delta) + m.group(3), index, count=1, flags=re.S)
    body = index.index("<body")
    head, rest = index[:body], index[body:]

    def script(m: re.Match) -> str:
        code = re.sub(r"(,\s*)(\d+(?:\.\d+)?)(\s*\)\s*;)",
                      lambda x: x.group(1) + (_num(float(x.group(2)) + delta) if float(x.group(2)) >= b - EPS else x.group(2)) + x.group(3),
                      m.group(0))
        return re.sub(r"(duration:\s*)(\d+(?:\.\d+)?)",
                      lambda x: x.group(1) + (_num(total + delta) if abs(float(x.group(2)) - total) < EPS else x.group(2)), code)

    rest = re.sub(r"<script>.*?</script>", script, rest, flags=re.S)
    path.write_text(head + rest, encoding="utf-8")


def _shift_captions(root: Path, b: float, delta: float, total: float):
    def move(g: dict):
        for k in ("start", "end"):
            if k in g and g[k] >= b - EPS:
                g[k] = round(g[k] + delta, 3)
        for w in g.get("words", []):
            move(w)

    groups = root / "caption_groups.json"
    if groups.exists():
        data = json.loads(groups.read_text())
        for g in data.get("groups", []):
            move(g)
        if "total_duration_s" in data:
            data["total_duration_s"] = round(data["total_duration_s"] + delta, 3)
        groups.write_text(json.dumps(data, indent=2))
    cap = root / "compositions" / "captions.html"
    if not cap.exists():
        return
    src = cap.read_text(encoding="utf-8")

    def js_groups(m: re.Match) -> str:
        data = json.loads(m.group(2))
        for g in data:
            move(g)
        return m.group(1) + json.dumps(data, ensure_ascii=False, separators=(",", ":")) + m.group(3)

    src = re.sub(r"(var GROUPS\s*=\s*)(\[.*?\])(\s*;)", js_groups, src, count=1, flags=re.S)
    src = re.sub(r"(var DURATION\s*=\s*)([\d.]+)", lambda m: m.group(1) + _num(float(m.group(2)) + delta), src, count=1)
    src = re.sub(r'data-duration="([\d.]+)"',
                 lambda m: f'data-duration="{_num(total + delta)}"' if abs(float(m.group(1)) - total) < EPS else m.group(0), src)
    cap.write_text(src, encoding="utf-8")


# ------------------------------------------------------------------ quality check

# HyperFrames' check (`hyperframes check --json`) samples the video in a browser. The edits are judged against the
# same check on the version they started from, so only problems the edits brought in are shown.
_LAYOUT = {"canvas_overflow": "Something runs off the edge of the video",
           "container_overflow": "Something sticks out of its box",
           "content_overlap": "Two texts overlap",
           "text_occluded": "A text is covered by something"}
BASELINE = Path(".autoclip") / "baseline.json"


def issues(report: dict) -> list[dict]:
    out = []
    for f in (report.get("contrast") or {}).get("findings") or []:
        if f.get("severity") in ("error", "warning"):
            out.append({"key": f"contrast:{f.get('text')}:{f.get('fg')}:{f.get('bg')}", "kind": "contrast",
                        "severity": f["severity"], "text": f.get("text"), "time": f.get("time"),
                        "ratio": f.get("ratio"), "required": f.get("requiredRatio"), "fg": f.get("fg"), "bg": f.get("bg")})
    for f in (report.get("layout") or {}).get("findings") or []:
        if f.get("code") in _LAYOUT and f.get("severity") in ("error", "warning") or \
                (f.get("code") in ("canvas_overflow", "text_occluded") and f.get("text")):
            out.append({"key": f"layout:{f['code']}:{f.get('text') or f.get('selector')}", "kind": "layout",
                        "severity": f.get("severity"), "message": _LAYOUT[f["code"]], "text": f.get("text"),
                        "time": f.get("time")})
    for f in (report.get("runtime") or {}).get("findings") or []:
        if f.get("severity") == "error":
            out.append({"key": f"runtime:{f.get('code')}:{f.get('message')}", "kind": "runtime", "severity": "error",
                        "message": f.get("message", "")[:300], "time": f.get("time")})
    return out


def new_issues(root: Path, report: dict) -> list[dict]:
    base = root / BASELINE
    known = {i["key"] for i in json.loads(base.read_text())} if base.exists() else set()
    return diff_issues(issues(report), known)


def diff_issues(found: list[dict], known: set[str]) -> list[dict]:
    """The issues whose key isn't in `known`, one per line of text (a finding per word is merged)."""
    seen, out = set(), []
    for i in found:
        if i["key"] in known or i["key"] in seen:
            continue
        seen.add(i["key"])
        same = next((o for o in out if o["kind"] == i["kind"] == "layout" and o["message"] == i["message"]
                     and abs((o["time"] or 0) - (i["time"] or 0)) < 1.5), None)
        if same and i.get("text"):  # one finding per word of the same line: one issue
            same["text"] = f"{same['text']} {i['text']}" if same.get("text") else i["text"]
        else:
            out.append(i)
    return out


def baseline_copy(root: Path, dest: Path):
    """A copy of the project as it was before the edits, to check it once."""
    shutil.rmtree(dest, ignore_errors=True)
    shutil.copytree(root, dest, ignore=shutil.ignore_patterns(".autoclip", "renders", "artifacts", "agent.log", "*.mp4.tmp"))
    if (root / _BASE).exists():
        _restore(dest, root / _BASE)
