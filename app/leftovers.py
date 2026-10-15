"""Leftovers of deleted videos.

Earlier versions removed a video's database row, renders and work folder,
but left its thumbnails and Claude's session logs on disk (and a late preview
request could re-create an empty work folder). This finds files that belong
to a video id no longer in the database, so they can be reviewed and removed.
"""
import json
import re
import shutil
from pathlib import Path

from . import db
from .config import CLAUDE_CONFIG_DIR, DATA_DIR, VIDEOS_DIR

PID = re.compile(r"[0-9a-f]{12}")
PROJECTS = DATA_DIR / "projects"


def _slug(p: Path) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", str(p))


def _size(p: Path) -> int:
    if p.is_file():
        return p.stat().st_size
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file() and not f.is_symlink())


def _mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0


def _scan() -> dict[str, dict]:
    known = db.project_ids()
    found: dict[str, dict] = {}

    def item(pid: str) -> dict:
        return found.setdefault(pid, {"renders": [], "thumbs": [], "folder": None, "logs": []})

    for f in VIDEOS_DIR.iterdir() if VIDEOS_DIR.is_dir() else []:
        m = re.fullmatch(r"([0-9a-f]{12})_v\d+(?:_strip)?\.(mp4|jpg)", f.name)
        if m and m[1] not in known:
            item(m[1])["renders" if m[2] == "mp4" else "thumbs"].append(f)
    for d in PROJECTS.iterdir() if PROJECTS.is_dir() else []:
        if d.is_dir() and PID.fullmatch(d.name) and d.name not in known:
            item(d.name)["folder"] = d
    # Claude Code names its transcript folder after the working directory of each run.
    bases = {_slug(PROJECTS) + "-", _slug(PROJECTS.resolve()) + "-"}
    for root in (CLAUDE_CONFIG_DIR / "projects", Path.home() / ".claude" / "projects"):
        for d in root.iterdir() if root.is_dir() else []:
            for b in bases:
                pid, rest = d.name[len(b):len(b) + 12], d.name[len(b) + 12:]
                if (d.name.startswith(b) and PID.fullmatch(pid) and (not rest or rest.startswith("-"))
                        and pid not in known and d.is_dir()):
                    item(pid)["logs"].append(d)
    return found


def _title(it: dict) -> str | None:
    """Best guess at what the video was about, from what's left of it."""
    folder = it["folder"]
    if folder:
        brief = folder / "motion" / "BRIEF.md"
        if brief.exists():
            m = re.search(r'^message:\s*"(.+)"\s*$', brief.read_text(errors="replace"), re.M)
            if m:
                return m[1]
        for ass in sorted((folder / "cache").glob("cap_*.ass")):
            lines = re.findall(r"^Dialogue:(?:[^,]*,){9}(.*)$", ass.read_text(errors="replace"), re.M)
            text = " ".join(re.sub(r"\{[^}]*\}", "", t).replace("\\N", " ") for t in lines[:6]).strip()
            if text:
                return text[:120] + ("…" if len(text) > 120 else "")
    # Transcripts: the Claude Motion brief ("## Intent") or the script prompt ("Topic: ...") we wrote.
    for d in it["logs"]:
        for f in d.glob("*.jsonl"):
            with open(f, errors="replace") as fh:
                head = fh.read(8_000_000)
            m = re.search(r"## Intent\\n\\n(.+?)\\n\\n## Notes", head) or re.search(r"\\nTopic: (.+?)\\n", head)
            if m:
                try:
                    return json.loads(f'"{m[1]}"')[:200]
                except ValueError:
                    return m[1][:200]
    return None


def _thumb(pid: str, it: dict) -> str | None:
    if it["thumbs"]:
        return f"/videos/{max(it['thumbs'], key=_mtime).name}"
    if it["folder"]:
        for f in sorted((it["folder"] / "media").glob("*")):
            if f.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"):
                return f"/media/{pid}/{f.name}"
    return None


def _paths(it: dict) -> list[Path]:
    return it["renders"] + it["thumbs"] + ([it["folder"]] if it["folder"] else []) + it["logs"]


def list_all() -> list[dict]:
    out = []
    for pid, it in _scan().items():
        paths = _paths(it)
        out.append({
            "id": pid, "title": _title(it), "thumb": _thumb(pid, it),
            "size": sum(_size(p) for p in paths), "modified": max(map(_mtime, paths)),
            "renders": len(it["renders"]), "thumbnails": len(it["thumbs"]), "work_folder": bool(it["folder"]),
            "claude_sessions": sum(len(list(d.glob("*.jsonl"))) for d in it["logs"]),
        })
    return sorted(out, key=lambda x: -x["modified"])


def remove(pids: list[str] | None = None) -> int:
    """Delete the leftovers of these deleted videos (all when None). Live videos are never touched."""
    found = _scan()
    n = 0
    for pid in pids if pids is not None else list(found):
        it = found.get(pid)
        if not it:
            continue
        for p in _paths(it):
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink(missing_ok=True)
        db.detach_usage(pid)
        n += 1
    return n
