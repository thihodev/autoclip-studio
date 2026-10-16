"""Direct edits to a Claude Motion video, without Claude: its texts, its colors and the pause after each scene's
narration, made in the HyperFrames project itself, previewed live in the browser and rendered with HyperFrames.

The files Claude wrote stay the source of truth. Before the first change a copy of them is kept in
`.autoclip/base/`, so the changes can be discarded until they are rendered.
"""
from __future__ import annotations

import html
import json
import re
import shutil
from pathlib import Path

EPS = 1e-3
MAX_HOLD = 5.0
_BASE = Path(".autoclip") / "base"
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
    """The preview copy of a frame: each tag holding text says which text it is (data-ac-t="scene:i")."""
    marks: dict[int, tuple[int, list[int]]] = {}
    for i, s in enumerate(_slots(src)):
        at, _, length = s["tag"]
        marks.setdefault(at, (length, []))[1].append(i)
    for at in sorted(marks, reverse=True):
        length, ids = marks[at]
        src = src[:at + length] + f' data-ac-t="{scene}:{ids[0]}"' + src[at + length:]
    return src


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
                    "texts": texts(src.read_text(encoding="utf-8")) if src.exists() else []})
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
    return {"scenes": scenes(root), "colors": colors(root), "duration": _total(index), "dirty": dirty(root),
            "width": int(w.group(1)) if w else 1080, "height": int(h.group(1)) if h else 1920}


# ------------------------------------------------------------------ keep / discard

def dirty(root: Path) -> bool:
    return (root / _BASE).exists()


def _snapshot(root: Path):
    base = root / _BASE
    if base.exists():
        return
    tmp = base.with_name("base.tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    for name in _KEEP:
        p = root / name
        if p.is_dir():
            shutil.copytree(p, tmp / name)
        elif p.exists():
            shutil.copy2(p, tmp / name)
    tmp.rename(base)


def discard(root: Path):
    base = root / _BASE
    if not base.exists():
        return
    for name in _KEEP:
        src, dst = base / name, root / name
        if src.is_dir():
            shutil.rmtree(dst, ignore_errors=True)
            shutil.copytree(src, dst)
        elif src.exists():
            shutil.copy2(src, dst)
    shutil.rmtree(base)


def keep(root: Path):
    """The changes are part of the video now (rendered, or handed to Claude): forget the copy."""
    shutil.rmtree(root / _BASE, ignore_errors=True)


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
        _snapshot(root)
        path.write_text(new, encoding="utf-8")


def edit_color(root: Path, old: str, new: str):
    old, new = _norm(old), _norm(new)
    if not re.fullmatch(r"#[0-9a-f]{6}", new) or old == new:
        return
    _snapshot(root)
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
    _snapshot(root)
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
