"""Templates: a Claude Motion video turned into a format that makes new episodes without designing them again.

Making a template copies the video's HyperFrames project and asks Claude (one small call) what each scene does:

- `fixed`   the same in every episode (intro, call to action): visuals, texts and narration are kept;
- `fill`    the content lives in its texts: an episode gets new texts and narration, the design stays;
- `redraw`  the illustration belongs to the topic: an episode can have Claude redraw just those scenes.

An episode is an ordinary Claude Motion project made from a copy of that project. Claude writes its narration and
texts (one small call), the voice is recorded, and the workflow's own scripts re-time the video (captions,
assembly, transitions) — no agent run unless scenes need redrawing. Then it is rendered and checked against the
quality check of the template, so only problems the new content brought in are reported.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import time
import uuid
from pathlib import Path

from . import claude, design, motion

log = logging.getLogger("templates")
from .config import DATA_DIR

TEMPLATES_DIR = DATA_DIR / "templates"
KINDS = ("fixed", "fill", "redraw")
MAX_EPISODES = 30  # one batch; a longer series is several batches
MIN_LENGTH = 0.92  # an episode's scene keeps at least this share of the template's narration time
_COPY_IGNORE = shutil.ignore_patterns(".autoclip", "renders", "artifacts", "agent.log", "node_modules", "_layer_*")


def _dir(tid: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{12}", tid or ""):
        raise ValueError("Unknown template")
    return TEMPLATES_DIR / tid


def get(tid: str) -> dict | None:
    try:
        path = _dir(tid) / "template.json"
    except ValueError:
        return None
    return json.loads(path.read_text()) if path.exists() else None


def _save(t: dict):
    path = _dir(t["id"]) / "template.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(t, ensure_ascii=False, indent=2))
    tmp.replace(path)


def interrupted():
    """At startup: a template whose reading or check was cut short by the app stopping can be retried."""
    for t in list_all():
        if t["status"] in ("analyzing", "checking"):
            _save({**t, "status": "failed", "error": "Stopped when the app was closed — retry"})


def list_all() -> list[dict]:
    if not TEMPLATES_DIR.exists():
        return []
    out = [t for d in TEMPLATES_DIR.iterdir() if (t := get(d.name))]
    return sorted(out, key=lambda t: -t["created_at"])


def delete(tid: str):
    shutil.rmtree(_dir(tid), ignore_errors=True)


def project(tid: str) -> Path:
    return _dir(tid) / "project"


def update(tid: str, name: str | None = None) -> dict:
    t = get(tid)
    if not t:
        raise ValueError("Unknown template")
    if name is not None and name.strip():
        t["name"] = name.strip()[:80]
    _save(t)
    return t


# ------------------------------------------------------------------ making a template

def start(p: dict, name: str) -> dict:
    """Copy the video's project and register the template (status "analyzing"); [analyze] finishes it."""
    root = motion.project_root(p["id"])
    if not (root / "STORYBOARD.md").exists() or not (root / "index.html").exists():
        raise ValueError("Only Claude Motion videos can become templates")
    if design.dirty(root):
        raise ValueError("Render or discard your Design changes first")
    video = Path(p.get("video_path") or "")
    if not video.is_file():
        raise ValueError("Render the video first")
    tid = uuid.uuid4().hex[:12]
    dest = _dir(tid)
    dest.mkdir(parents=True)
    shutil.copytree(root, dest / "project", symlinks=True, ignore=_COPY_IGNORE)
    shutil.copyfile(video, dest / "preview.mp4")
    if video.with_suffix(".jpg").exists():
        shutil.copyfile(video.with_suffix(".jpg"), dest / "cover.jpg")
    req = p["request"]
    t = {"id": tid, "name": (name or (p.get("script") or {}).get("title") or req.get("topic", ""))[:80].strip() or "Template",
         "status": "analyzing", "error": None, "source": p["id"], "created_at": time.time(),
         "format": req.get("format", "shorts"), "language": req.get("language", "English"),
         "voice": req.get("voice") or "", "voice_rate": req.get("voice_rate", 1.0), "brand_id": req.get("brand_id"),
         "music": req.get("music") or "", "sfx": req.get("sfx", True), "duration": design.state(dest / "project")["duration"],
         "example": (p.get("script") or {}).get("title") or req.get("topic", ""), "scenes": [], "baseline": []}
    _save(t)
    return t


def _frames(root: Path) -> list[dict]:
    """Per scene of the copied project: frame number, texts, images, narration and its storyboard notes."""
    story = (root / "STORYBOARD.md").read_text(encoding="utf-8")
    meta = json.loads((root / "audio_meta.json").read_text()) if (root / "audio_meta.json").exists() else {}
    voices = {v["frame"]: v for v in meta.get("voices", [])}
    out = []
    for n, s in enumerate(design.scenes(root), 1):
        sec = _section(story, n)
        notes = "\n".join(line for line in sec.splitlines()
                          if re.match(r"\s*(-\s*(scene|type|focal|beat)\s*:|narrativeRole:|keyMessage:)", line))
        v = voices.get(n) or {}
        out.append({"id": s["id"], "frame": n, "src": s["src"], "texts": s["texts"], "media": s["media"],
                    "narration": v.get("text") or _field(sec, "voiceover"), "voice_s": v.get("duration_s"),
                    "notes": notes[:1200]})
    return out


def _section(story: str, n: int) -> str:
    m = re.search(rf"^## Frame {n}\b.*?(?=^## Frame {n + 1}\b|\Z)", story, re.S | re.M)
    return m.group(0) if m else ""


def _field(section: str, name: str) -> str:
    m = re.search(rf"^\s*-\s*{name}\s*:\s*\"?(.*?)\"?\s*$", section, re.M)
    return m.group(1) if m else ""


ANALYZE_SYSTEM = """You turn a finished short video into a reusable template for a series. You read what each scene
shows and says, and decide what changes from one episode to the next. Reply with JSON only."""

ANALYZE_PROMPT = """This {language} video is "{title}". Its scenes (in order), with every on-screen text by index,
the images in it, the narration and the storyboard notes:

{scenes}

Make it a template for a series of episodes on other topics in the same format. For each scene decide:
- "fixed": the same in every episode (brand intro, call to action, sign-off): keep visuals, texts and narration.
- "fill": the episode's content is carried by texts (and narration); the visuals work for any topic of the series.
- "redraw": the illustration only makes sense for this topic (a drawn brain for "your brain"); an episode needs it
  redrawn.
For "fill" and "redraw" scenes list the texts that change per episode ("slots", by index) with a short label in
{language}; decorative or brand texts (day letters, "?", logo words, numbering kept in every episode) are not
slots. Images that change per episode go in "media" by index.

Reply with JSON:
{{"name": "short template name in {language}, the format not this topic (e.g. 'Quick quiz · 3 tips')",
  "summary": "one sentence in {language}: what an episode of this format is",
  "episode_prompt": "one short example of what to type to make a new episode, in {language}",
  "scenes": [{{"id": "scene id", "kind": "fixed|fill|redraw", "purpose": "what this scene does in any episode, in English",
              "slots": [{{"index": 0, "label": "in {language}"}}], "media": [{{"index": 0, "label": "in {language}"}}]}}]}}"""


def _scene_block(f: dict) -> str:
    texts = "\n".join(f"    [{i}] {t}" for i, t in enumerate(f["texts"])) or "    (none)"
    media = "\n".join(f"    [{i}] {m['kind']}: {m['src'].split('/')[-1]}" for i, m in enumerate(f["media"])) or "    (none)"
    return (f"## {f['id']} (scene {f['frame']})\n  narration: {f['narration']}\n  texts:\n{texts}\n  images:\n{media}\n"
            f"  notes:\n{f['notes']}")


async def analyze(tid: str):
    """Claude reads the scenes; then the template's own quality check is run once as the episodes' baseline."""
    t = get(tid)
    root = project(tid)
    frames = _frames(root)
    data = await claude.ask_json(
        ANALYZE_PROMPT.format(language=t["language"], title=t["example"], scenes="\n\n".join(map(_scene_block, frames))),
        ANALYZE_SYSTEM, kind="template", project_id=t["source"], effort="low")
    picked = {s.get("id"): s for s in data.get("scenes", []) if isinstance(s, dict)}
    scenes = []
    for f in frames:
        s = picked.get(f["id"]) or {}
        kind = s.get("kind") if s.get("kind") in KINDS else "fill"
        slots = []
        for sl in s.get("slots") or []:
            i = sl.get("index") if isinstance(sl, dict) else None
            if isinstance(i, int) and 0 <= i < len(f["texts"]) and kind != "fixed" and i not in [x["index"] for x in slots]:
                orig = f["texts"][i]
                slots.append({"index": i, "label": str(sl.get("label") or orig)[:60], "original": orig,
                              "max_chars": max(len(orig) + 4, round(len(orig) * 1.25)), "case": _case(orig)})
        media = [{"index": m["index"], "label": str(m.get("label") or "")[:60]} for m in s.get("media") or []
                 if isinstance(m, dict) and isinstance(m.get("index"), int) and 0 <= m["index"] < len(f["media"])]
        scenes.append({"id": f["id"], "frame": f["frame"], "src": f["src"], "kind": kind,
                       "purpose": str(s.get("purpose") or "")[:300], "narration": f["narration"],
                       "words": len(f["narration"].split()), "voice_s": f["voice_s"], "slots": slots, "media": media})
    t = get(tid)
    t.update(name=t["name"] if t["name"] != t["example"][:80] else (str(data.get("name") or t["name"])[:80]),
             summary=str(data.get("summary") or "")[:300], episode_prompt=str(data.get("episode_prompt") or "")[:200],
             scenes=scenes, status="checking")
    _save(t)
    t["baseline"] = design.issues(await motion.check(root))
    t["status"] = "ready"
    _save(t)


def _case(text: str) -> str:
    letters = [c for c in text if c.isalpha()]
    if letters and all(c.isupper() for c in letters):
        return "upper"
    return "as-is"


# ------------------------------------------------------------------ an episode

EPISODE_SYSTEM = """You write a new episode of an existing short-video format. The design, layout and timing are
fixed; you only write what is said and the on-screen texts, sized to fit the slots exactly. Reply with JSON only."""

EPISODE_PROMPT = """Format: "{name}" — {summary}
Language: {language}. Earlier episode: "{example}".

New episode: {brief}

Write it scene by scene. For each scene below keep its purpose; the narration must stay close to the original
length (word count given, ±10%) so the animation timing still fits, and keep the same tone and rhythm. Each
on-screen text slot must fit its limit (characters, counting spaces) and follow its case.

{scenes}

Reply with JSON:
{{"title": "catchy, honest title for this episode in {language}, max 90 characters",
  "scenes": [{{"id": "scene id", "narration": "what is said in {language}",
              "texts": {{"<slot index>": "on-screen text"}}}}]}}
Only list the "fill" and "redraw" scenes."""


def _episode_block(s: dict) -> str:
    slots = "\n".join(f"    slot {sl['index']} ({sl['label']}): was \"{sl['original']}\" · max {sl['max_chars']} chars"
                      f"{' · UPPERCASE' if sl['case'] == 'upper' else ''}" for sl in s["slots"]) or "    (no text slots)"
    return (f"## {s['id']} — {s['kind']}: {s['purpose']}\n  narration was ({s['words']} words): {s['narration']}\n"
            f"  texts:\n{slots}")


def _check_episode(t: dict, data: dict) -> list[str]:
    """What the written episode gets wrong (a slot too long, a narration far off its length)."""
    got = {s.get("id"): s for s in data.get("scenes", []) if isinstance(s, dict)}
    problems = []
    for s in t["scenes"]:
        if s["kind"] == "fixed":
            continue
        e = got.get(s["id"])
        if not e or not str(e.get("narration") or "").strip():
            problems.append(f"{s['id']}: missing narration")
            continue
        n = len(str(e["narration"]).split())
        if s["words"] and not 0.75 * s["words"] <= n <= 1.3 * s["words"]:
            problems.append(f"{s['id']}: narration has {n} words, it needs about {s['words']}")
        texts = e.get("texts") if isinstance(e.get("texts"), dict) else {}
        for sl in s["slots"]:
            v = str(texts.get(str(sl["index"])) or "").strip()
            if not v:
                problems.append(f"{s['id']} slot {sl['index']}: missing")
            elif len(v) > sl["max_chars"]:
                problems.append(f"{s['id']} slot {sl['index']}: {len(v)} characters, max {sl['max_chars']}")
    return problems


async def write_episode(t: dict, brief: str, project_id: str, notes: str = "") -> dict:
    """Claude writes the episode; one more try with the problems listed when it doesn't fit. `notes`: the project's
    instructions (audience, call to action, words to avoid)."""
    prompt = EPISODE_PROMPT.format(name=t["name"], summary=t.get("summary", ""), language=t["language"],
                                   example=t["example"], brief=brief.strip(),
                                   scenes="\n\n".join(_episode_block(s) for s in t["scenes"] if s["kind"] != "fixed"))
    if notes.strip():
        prompt = prompt.replace("\nWrite it scene by scene.", f"\nProject instructions: {notes.strip()[:1500]}\n\nWrite it scene by scene.", 1)
    data = await claude.ask_json(prompt, EPISODE_SYSTEM, kind="episode", project_id=project_id, effort="low")
    problems = _check_episode(t, data)
    if problems:
        log.info("episode %s: first draft doesn't fit: %s", project_id, "; ".join(problems))
        retry = prompt + "\n\nYour last answer did not fit:\n- " + "\n- ".join(problems) + \
            "\nWrite it again so every scene fits."
        try:
            again = await claude.ask_json(retry, EPISODE_SYSTEM, kind="episode", project_id=project_id, effort="low")
        except RuntimeError:  # keep the first draft when it has every scene (lengths are fitted below)
            log.warning("episode %s: the second draft failed", project_id, exc_info=True)
            again = None
        if again is not None and len(_check_episode(t, again)) <= len(problems):
            data, problems = again, _check_episode(t, again)
    if any("missing" in p for p in problems):
        raise RuntimeError("Claude's episode didn't fill every scene: " + "; ".join(problems[:4]))
    return data


IDEAS_PROMPT = """Format: "{name}" — {summary}
Language: {language}. Its scenes: {scenes}
Episodes already made: {done}
{hint}
Suggest {count} new episodes for this series: topics people in its audience search for and share, each different from
the episodes already made, and each a good fit for the format's scenes. Write each as the short line someone would
type to ask for the episode, in {language} (like "{example}").

Reply with JSON: {{"ideas": ["..."]}}"""


async def suggest_episodes(t: dict, done: list[str], count: int = 5, hint: str = "", notes: str = "") -> list[str]:
    """Next episodes for a series, different from the ones already made (one small call)."""
    scenes = "; ".join(f"{s['kind']}: {s['purpose']}" for s in t["scenes"])[:1500]
    extra = "\n".join(x for x in [f"Wanted: {hint.strip()}" if hint.strip() else "",
                                    f"Project instructions: {notes.strip()[:1500]}" if notes.strip() else ""] if x)
    data = await claude.ask_json(IDEAS_PROMPT.format(name=t["name"], summary=t.get("summary", ""), language=t["language"],
                                                     scenes=scenes, done="; ".join(done[:40]) or "none yet", hint=extra,
                                                     count=count, example=t.get("example") or t.get("episode_prompt") or ""),
                                 EPISODE_SYSTEM, kind="episode-ideas", effort="low")
    return [" ".join(str(x).split())[:300] for x in data.get("ideas") or [] if str(x).strip()][:count]


def _fit(text: str, slot: dict) -> str:
    text = " ".join(text.split())
    if slot["case"] == "upper":
        text = text.upper()
    if len(text) > slot["max_chars"]:  # last resort: cut at a word, the check after the render still looks at it
        text = text[:slot["max_chars"]].rsplit(" ", 1)[0].rstrip(" ,.;:") or text[:slot["max_chars"]]
    return text


def _set_story(root: Path, durations: dict[int, float], narration: dict[int, str]):
    """STORYBOARD.md and SCRIPT.md follow the episode (durations, voice-over lines) for later Claude edits."""
    sb = root / "STORYBOARD.md"
    story = sb.read_text(encoding="utf-8")
    for n, dur in durations.items():
        sec = _section(story, n)
        if not sec:
            continue
        new = re.sub(r"^(\s*-\s*duration\s*:\s*).*$", lambda m: f"{m.group(1)}{dur:g}s", sec, count=1, flags=re.M)
        if n in narration:
            text = narration[n].replace('"', "'")
            new = re.sub(r"^(\s*-\s*voiceover\s*:\s*).*$", lambda m: f'{m.group(1)}"{text}"', new, count=1, flags=re.M)
        story = story.replace(sec, new, 1)
    sb.write_text(story, encoding="utf-8")
    sp = root / "SCRIPT.md"
    if sp.exists():
        script = sp.read_text(encoding="utf-8")
        for n, text in narration.items():
            m = re.search(rf"^## Line \d+ — .*\(Frame {n}\)\s*$.*?\n\n    (.+?)$", script, re.S | re.M)
            if m:
                script = script[:m.start(1)] + text + script[m.end(1):]
        sp.write_text(script, encoding="utf-8")


async def _node(root: Path, *args: str):
    scripts = motion.SKILLS_DIR / motion.WORKFLOW / "scripts"
    await motion._run([shutil.which("node"), str(scripts / args[0]), *args[1:]], root, motion._env(), timeout=600)


async def build(t: dict, pid: str, data: dict, req: dict, brand: dict | None, report) -> Path:
    """Copy the template's project into the episode, put in its texts and narration, record the voice and re-time
    the video with the workflow's scripts. Returns the episode's project folder (not rendered yet)."""
    root = motion.project_root(pid)
    shutil.rmtree(root, ignore_errors=True)
    root.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(project(t["id"]), root, symlinks=True)
    old = {s["id"]: s for s in design.scenes(root)}
    root_dur = {sid: _root_duration(root / s["src"], sid) for sid, s in old.items()}
    got = {s.get("id"): s for s in data.get("scenes", []) if isinstance(s, dict)}
    lines, narration = [], {}
    report(30, "Writing the texts")
    for s in t["scenes"]:
        e = got.get(s["id"])
        if s["kind"] == "fixed" or not e:
            continue
        path = root / s["src"]
        src = path.read_text(encoding="utf-8")
        texts = e.get("texts") if isinstance(e.get("texts"), dict) else {}
        for sl in sorted(s["slots"], key=lambda x: -x["index"]):  # later slots first: earlier offsets stay put
            if v := str(texts.get(str(sl["index"])) or "").strip():
                src = design.set_text(src, sl["index"], _fit(v, sl))
        path.write_text(src, encoding="utf-8")
        text = " ".join(str(e.get("narration") or "").split())
        if text:
            lines.append({"frame": s["frame"], "text": text})
            narration[s["frame"]] = text
    report(40, "Recording the voice-over")
    await motion.record_voice(pid, root, req, brand, lines)
    meta = json.loads((root / "audio_meta.json").read_text())
    voices = {v["frame"]: v for v in meta.get("voices", [])}
    durations = {}
    for s in t["scenes"]:
        v = voices.get(s["frame"])
        if v:
            # A shorter narration keeps most of the template's time, so the scene's animation still plays out.
            durations[s["frame"]] = round(max(v["duration_s"], (s.get("voice_s") or 0) * MIN_LENGTH), 2)
    _set_story(root, durations, narration)
    report(55, "Timing the scenes")
    await _node(root, "captions.mjs", "build", "--storyboard", "./STORYBOARD.md", "--audio-meta", "./audio_meta.json",
                "--hyperframes", ".", "--out", "./caption_groups.json")
    await _node(root, "assemble-index.mjs", "--storyboard", "./STORYBOARD.md", "--hyperframes", ".",
                "--audio-meta", "./audio_meta.json")
    await _node(root, "transitions.mjs", "inject", "--storyboard", "./STORYBOARD.md", "--hyperframes", ".")
    # The frames' own clips last as long as the template's scene did: stretch them to the episode's scene.
    for s in design.scenes(root):
        was = root_dur.get(s["id"])
        if was:
            _stretch(root / s["src"], was, s["duration"])
    return root


def _root_duration(path: Path, sid: str) -> float | None:
    m = re.search(rf'<[^>]*data-composition-id="{re.escape(sid)}"[^>]*data-duration="([\d.]+)"', path.read_text(encoding="utf-8"))
    return float(m.group(1)) if m else None


def _stretch(path: Path, was: float, now: float):
    src = path.read_text(encoding="utf-8")
    out = re.sub(r'data-duration="([\d.]+)"',
                 lambda m: f'data-duration="{design._num(now)}"' if abs(float(m.group(1)) - was) < 1e-3 else m.group(0), src)
    if out != src:
        path.write_text(out, encoding="utf-8")


def redraw_instruction(t: dict, data: dict) -> str | None:
    """The Claude Motion edit for an episode's "redraw" scenes, or None when it has none."""
    got = {s.get("id"): s for s in data.get("scenes", []) if isinstance(s, dict)}
    parts = [f"- {s['id']} ({s['purpose']}): now says “{got.get(s['id'], {}).get('narration', '')}”"
             for s in t["scenes"] if s["kind"] == "redraw"]
    if not parts:
        return None
    return ("This video is a new episode made from a template: its texts, narration, voice, captions and timing are "
            "already done for the new topic. These scenes still show the template's illustration, which belonged to "
            "the earlier topic:\n" + "\n".join(parts) + "\nRedraw only the illustrations of those scenes so they show "
            "what the narration now says, in the same style (cast, palette, line weight, layout, motion grammar). Keep "
            "every text, the narration, the voice files, the scene durations and the other scenes exactly as they "
            "are; do not call the voiceover tool and do not re-assemble the timing.")


def check_issues(t: dict, report: dict) -> list[dict]:
    """The episode's check findings that the template didn't have."""
    known = {i["key"] for i in t.get("baseline") or []}
    return design.diff_issues(design.issues(report), known)


async def wait_ready(tid: str, timeout: float = 600):
    """An episode asked for while its template is still being analyzed waits for it."""
    start = time.time()
    while (t := get(tid)) and t["status"] in ("analyzing", "checking"):
        if time.time() - start > timeout:
            raise RuntimeError("The template is still being prepared")
        await asyncio.sleep(2)
    if not t or t["status"] != "ready":
        raise RuntimeError("The template couldn't be prepared: " + ((t or {}).get("error") or "it was deleted"))
    return t


def preview(tid: str) -> Path:
    return _dir(tid) / "preview.mp4"


def cover(tid: str) -> Path:
    return _dir(tid) / "cover.jpg"

