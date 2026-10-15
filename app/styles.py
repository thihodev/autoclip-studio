"""Visual styles for Claude Motion videos, gathered from open-source projects.

- FrameJam presets (MIT, https://github.com/moritzkremb/framejam): bundled in
  app/assets/styles/framejam — a detailed style.json (palette, fonts, easing,
  transitions, text animations, pacing), a poster and a short example.
- HyperFrames frame presets (Apache-2.0, https://github.com/heygen-com/hyperframes):
  read from the skills Claude Motion installs; the agent applies them with
  the workflow's own `build-frame.mjs --preset`.

A style sets the look (layout, type, motion, pacing); a brand kit, when chosen,
keeps its colors, logo and font.
"""
import json
import re
import shutil
from pathlib import Path

ASSETS = Path(__file__).resolve().parent / "assets" / "styles"
FRAMEJAM = ASSETS / "framejam"


def _hf_presets() -> Path:
    from .motion import SKILLS_DIR

    return SKILLS_DIR / "hyperframes-creative" / "frame-presets"


def _framejam() -> list[dict]:
    out = []
    for d in sorted(FRAMEJAM.iterdir()) if FRAMEJAM.is_dir() else []:
        f = d / "style.json"
        if not f.exists():
            continue
        s = json.loads(f.read_text())
        fonts = s.get("fonts") or {}
        out.append({
            "id": f"framejam:{s.get('id') or d.name}", "name": s.get("name") or d.name, "source": "FrameJam",
            "license": "MIT", "tagline": s.get("tagline") or "", "description": s.get("description") or "",
            "mood": s.get("mood") or [], "pacing": s.get("pacing") or "",
            "palette": [v for v in (s.get("palette") or {}).values() if isinstance(v, str) and v.startswith("#")][:6],
            "fonts": [v.get("family") for v in fonts.values() if isinstance(v, dict) and v.get("family")],
            "poster": f"/style-assets/framejam/{d.name}/poster.jpg" if (d / "poster.jpg").exists() else None,
        })
    return out


def _front_matter(text: str) -> str:
    m = re.match(r"---\n(.*?)\n---", text, re.S)
    return m.group(1) if m else ""


def _hf_tagline(description: str) -> str:
    """The look in one line: the "Atoms are identical and sacred — …" list, without the boilerplate around it."""
    m = re.search(r"sacred\s+—\s+(.+?)(?:\.\s|$)", description)
    text = m.group(1) if m else description
    text = text[0].upper() + text[1:] if text else text
    return text if len(text) <= 170 else text[:167].rsplit(" ", 1)[0] + "…"


def _hyperframes() -> list[dict]:
    root = _hf_presets()
    out = []
    for d in sorted(root.iterdir()) if root.is_dir() else []:
        f = d / "FRAME.md"
        if not f.exists():
            continue
        fm = _front_matter(f.read_text(errors="replace"))
        name = re.search(r"^name:\s*(.+)$", fm, re.M)
        desc = re.search(r"^description:\s*>?\s*\n((?:[ \t]+.+\n?)+)", fm, re.M)
        colors = re.search(r"^colors:\s*\n((?:[ \t]+.+\n?)+)", fm, re.M)
        description = " ".join(desc.group(1).split()) if desc else ""
        out.append({
            "id": f"hyperframes:{d.name}", "source": "HyperFrames", "license": "Apache-2.0",
            "name": re.sub(r"\s+—.*$", "", name.group(1)).strip() if name else d.name,
            "tagline": _hf_tagline(description),
            "description": description, "mood": [], "pacing": "",
            "palette": re.findall(r'"(#[0-9A-Fa-f]{6})"', colors.group(1))[:6] if colors else [],
            "fonts": sorted(set(re.findall(r'fontFamily:\s*"([^"]+)"', fm)))[:3],
            "poster": None,
        })
    return out


def list_all() -> list[dict]:
    return _framejam() + _hyperframes()


def get(style_id: str | None) -> dict | None:
    return next((s for s in list_all() if s["id"] == style_id), None) if style_id else None


def brief_lines(style_id: str | None, root: Path, has_brand: bool) -> list[str]:
    """Put the style's files in the Claude Motion project and say how to use them."""
    style = get(style_id)
    if not style:
        return []
    source, key = style_id.split(":", 1)
    lines = ["", f"## Visual style: {style['name']}", style["tagline"]]
    if source == "framejam":
        dest = root / "style"
        dest.mkdir(exist_ok=True)
        for name in ("style.json", "poster.jpg", "example.html"):
            if (FRAMEJAM / key / name).exists():
                shutil.copyfile(FRAMEJAM / key / name, dest / name)
        lines.append("- Follow `style/style.json` for palette, fonts, easing, transitions, text animations and pacing. "
                     "`style/poster.jpg` shows the look; `style/example.html` is a short example built in it. In the "
                     "design step, write frame.md from style.json instead of picking a stock preset.")
        lines.append("- A style is a look, not a starting video: build THIS video's subject in it, adapted to the "
                     "canvas — don't copy the example's subject, props or layout.")
    else:
        lines.append(f"- In the design step use the HyperFrames frame preset `{key}` "
                     f"(`build-frame.mjs --preset {key}`) instead of choosing one.")
    if has_brand:
        lines.append("- The brand kit wins on identity: keep its colors, logo and font; take layout, type scale, "
                     "motion and pacing from the style.")
    return lines
