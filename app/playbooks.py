"""Video types ("playbooks"): the structure of a video — hook, beats, pacing, ending.

A playbook complements a visual style (the look). Sources:
- FrameJam playbooks (MIT, https://github.com/moritzkremb/framejam): the ones that work without filmed footage,
  bundled in app/assets/playbooks/framejam (playbook.json, poster, SKILL.md and its references).
- AutoClip's own short-form playbooks in app/assets/playbooks/autoclip.

Claude Motion gets the playbook's files in its project; the Quick engine gets a summary in the script prompt.
"""
import json
import re
import shutil
from pathlib import Path

ASSETS = Path(__file__).resolve().parent / "assets" / "playbooks"
SOURCES = {"framejam": "FrameJam", "autoclip": "AutoClip"}


def _load(source: str, d: Path) -> dict | None:
    f = d / "playbook.json"
    if not f.exists():
        return None
    p = json.loads(f.read_text())
    return {
        "id": f"{source}:{p.get('id') or d.name}", "name": p.get("name") or d.name, "source": SOURCES[source],
        "license": p.get("license") or "MIT", "tagline": p.get("tagline") or "", "description": p.get("description") or "",
        "tags": p.get("tags") or [], "format": p.get("format") or "any", "steps": p.get("steps") or [],
        "poster": f"/playbook-assets/{source}/{d.name}/poster.jpg" if (d / "poster.jpg").exists() else None,
    }


def list_all() -> list[dict]:
    out = []
    for source in ("autoclip", "framejam"):
        root = ASSETS / source
        for d in sorted(root.iterdir()) if root.is_dir() else []:
            if d.is_dir() and (p := _load(source, d)):
                out.append(p)
    return out


def get(playbook_id: str | None) -> dict | None:
    return next((p for p in list_all() if p["id"] == playbook_id), None) if playbook_id else None


def _dir(playbook_id: str) -> Path:
    source, key = playbook_id.split(":", 1)
    return ASSETS / source / Path(key).name


def brief_lines(playbook_id: str | None, root: Path) -> list[str]:
    """Claude Motion: copy the playbook into the project and say how to use it."""
    p = get(playbook_id)
    if not p:
        return []
    dest = root / "playbook"
    shutil.copytree(_dir(playbook_id), dest, dirs_exist_ok=True, ignore=shutil.ignore_patterns("poster.jpg"))
    return [
        "", f"## Video type: {p['name']}", p["tagline"],
        "- Read `playbook/SKILL.md` (and only the `playbook/references/` files it points you to for story, visuals and "
        "sound) and follow its structure: hook, beats, pacing, how pictures land on words, the ending. Use it inside "
        "the faceless-explainer workflow you're running.",
        "- Ignore its tooling and review steps (its scripts, TTS, FrameJam, storyboard approval, questions to the user): "
        "AutoClip records the voice with the voiceover tool, and there is no one to ask. Its default length and aspect "
        "ratio give way to the brief's.",
    ]


def script_notes(playbook_id: str | None) -> str:
    """Quick engine: the playbook's structure as notes for the script writer."""
    p = get(playbook_id)
    if not p:
        return ""
    skill = (_dir(playbook_id) / "SKILL.md").read_text(errors="replace")
    if p["source"] == "AutoClip":  # short recipes: hand them over whole
        guide = re.sub(r"^#.*\n", "", skill).strip()
    else:
        guide = p["description"] + ("\nSteps: " + "; ".join(p["steps"]) if p["steps"] else "")
    return f"Video type: {p['name']} — {p['tagline']}\n{guide}"[:4000]
