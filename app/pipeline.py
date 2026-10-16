"""Topic → Claude scenes → timeline → render → edit loop → export."""
import asyncio
import json
import logging
import random
import shutil
import tempfile
import time
import uuid
from pathlib import Path

from . import activity, audio, brands, claude, context, db, design, editor, motion, references, playbooks, render, settings, tts, usage
from .config import DATA_DIR, VIDEOS_DIR

log = logging.getLogger("pipeline")

# One render at a time; ffmpeg already uses all cores. Claude Motion runs one at a time too.
_render_lock = asyncio.Semaphore(1)
_motion_lock = asyncio.Semaphore(1)

# Live stage label per project while Claude Motion works ("Animating scene 3 of 7").
STAGES: dict[str, str] = {}


def default_engine() -> str:
    return "motion" if motion.status()["code"] in ("ready", "install") else "classic"


def _reporter(pid: str):
    def report(value: int, label: str):
        STAGES[pid] = label
        db.update_project(pid, progress=int(value))
    return report


def _save_version(pid: str, out: Path, tl: dict, note: str, model: str | None = None, effort: str | None = None,
                  edited: bool = False) -> dict:
    p = db.get_project(pid)
    extra = _extra(p)
    version = (p.get("version") or 0) + 1
    extra["versions"].append({"version": version, "file": out.name, "note": note, "at": time.time(),
                              "model": model, "effort": effort, **({"edited": True} if edited else {})})
    db.update_project(pid, timeline=tl, video_path=str(out), version=version, extra=extra,
                      status="ready", progress=100, error=None, publish_result=None)
    return db.get_project(pid)


async def _motion_version(pid: str, note: str, instruction: str | None = None, model: str | None = None,
                          effort: str | None = None) -> dict:
    p = db.get_project(pid)
    model, effort = usage.choose("motion", model or p["request"].get("model"), effort or p["request"].get("effort"))
    out = VIDEOS_DIR / f"{pid}_v{(p.get('version') or 0) + 1}.mp4"
    db.update_project(pid, status="queued", progress=0)
    async with _motion_lock:
        activity.start(pid)
        db.update_project(pid, status="designing", progress=1)
        try:
            report = _reporter(pid)
            if instruction:
                tl = await motion.edit(pid, p["request"], instruction, out, report, model, effort)
            else:
                tl = await motion.create(pid, p["request"], out, report, model, effort)
        finally:
            STAGES.pop(pid, None)
    tl["format"] = p["request"].get("format", "shorts")
    tl["brand_id"] = p["request"].get("brand_id")
    await render.thumbnail(out)
    return _save_version(pid, out, tl, note, model, effort)


async def prepare_request(pid: str) -> dict:
    """Apply the video's project (workspace) settings and, when the topic or project names a website and no
    brand kit was chosen, build/reuse a brand kit from that website."""
    p = db.get_project(pid)
    req = dict(p["request"])
    changed = False
    ws = db.get_workspace(p["workspace_id"]) if p.get("workspace_id") else None
    if ws and not req.get("_workspace_applied"):
        d = ws["data"]
        if not req.get("brand_id") and d.get("brand_id") and brands.get(d["brand_id"]):
            req["brand_id"] = d["brand_id"]
        if d.get("notes"):
            req["notes"] = f"Project instructions: {d['notes']}\n{req.get('notes', '')}".strip()
        own = {m["id"] for m in req.get("context") or []}
        req["context"] = [m for m in d.get("context") or [] if m["id"] not in own] + list(req.get("context") or [])
        req["website"] = req.get("website") or d.get("website") or ""
        req["_workspace_applied"] = True
        changed = True
    if req.get("github_repo") and not req.get("_github_applied"):
        await _apply_github(pid, req)
        changed = True
    if not req.get("brand_id"):
        domain = brands.find_domain(req.get("website") or "") or brands.find_domain(req.get("topic", ""))
        if domain:
            brand = brands.find_by_website(domain)
            if brand:
                activity.add(pid, f"Using the {brand['name']} brand kit for {domain}")
            else:
                activity.add(pid, f"No brand kit for {domain} yet — building one from the website")
                STAGES[pid] = f"Reading {domain}"
                try:
                    brand = await brands.import_website(domain, report=lambda t: activity.add(pid, t))
                except Exception as e:
                    log.warning("website import failed for %s", domain, exc_info=True)
                    activity.add(pid, f"Couldn't read {domain} ({e.__class__.__name__}) — continuing without its design", "warn")
                    brand = None
            if brand:
                req["brand_id"] = brand["id"]
                changed = True
    if changed:
        db.update_project(pid, request=req)
    return req


async def _apply_github(pid: str, req: dict):
    """A repository chosen for the video: its README, description and latest release become source material,
    and (without a brand kit chosen) its kit is reused or built from the repository."""
    repo = req["github_repo"]
    req["_github_applied"] = True
    STAGES[pid] = f"Reading {repo}"
    activity.add(pid, f"Reading {repo} on GitHub")
    try:
        material = await brands.github_material(repo)
        tmp = Path(tempfile.mkdtemp(dir=DATA_DIR))
        try:
            path = tmp / f"{repo.replace('/', '-')}-github.md"
            path.write_text(material["text"])
            meta = context.save(path, path.name)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        req["context"] = [*(req.get("context") or []), meta]
        activity.add(pid, f"Using {repo}: description, README and latest release as source material", "done")
    except Exception as e:
        log.warning("github material failed for %s", repo, exc_info=True)
        activity.add(pid, f"Couldn't read {repo} ({str(e)[:120] or e.__class__.__name__}) — continuing without it", "warn")
        return
    if req.get("brand_id"):
        return
    brand = brands.find_by_repo(repo)
    if brand:
        activity.add(pid, f"Using the {brand['name']} brand kit from {repo}")
    else:
        activity.add(pid, f"No brand kit for {repo} yet — building one from the repository")
        STAGES[pid] = f"Building a brand kit from {repo}"
        try:
            brand = await brands.import_github(repo)
        except Exception as e:
            log.warning("github brand import failed for %s", repo, exc_info=True)
            activity.add(pid, f"Couldn't build a brand kit from {repo} ({e.__class__.__name__}) — continuing without one", "warn")
    if brand:
        req["brand_id"] = brand["id"]


def is_motion(p: dict) -> bool:
    return (p.get("timeline") or {}).get("engine") == "motion" or p["request"].get("engine") == "motion"


def _extra(p: dict) -> dict:
    extra = p.get("extra") or {}
    for k, v in (("versions", []), ("history", []), ("reviews", {})):
        extra.setdefault(k, v)
    return extra


def _fail(pid: str, e: Exception, status: str = "failed"):
    log.exception("project %s failed", pid)
    db.update_project(pid, status=status, error=f"{e.__class__.__name__}: {e}"[:1000])


async def _render_version(pid: str, note: str) -> dict:
    """Render the project's current timeline as the next version."""
    p = db.get_project(pid)
    version = (p.get("version") or 0) + 1
    out = VIDEOS_DIR / f"{pid}_v{version}.mp4"
    async with _render_lock:
        db.update_project(pid, status="rendering", progress=0)
        tl = await render.render_timeline(
            pid, p["timeline"], out, lambda f: db.update_project(pid, progress=int(f * 100))
        )
    extra = _extra(p)
    extra["versions"].append({"version": version, "file": out.name, "note": note, "at": time.time()})
    db.update_project(pid, timeline=tl, video_path=str(out), version=version, extra=extra,
                      status="ready", progress=100, error=None, publish_result=None)
    return db.get_project(pid)


async def run_project(pid: str):
    activity.clear(pid)
    activity.start(pid)
    if db.get_project(pid)["request"].get("template_id"):
        try:
            await make_episode(pid)
        except Exception as e:
            _fail(pid, e, status="ready" if db.get_project(pid).get("video_path") else "failed")
        finally:
            STAGES.pop(pid, None)
        return
    db.update_project(pid, status="designing" if db.get_project(pid)["request"].get("engine") == "motion" else "scripting",
                      progress=1)
    try:
        await prepare_request(pid)
    finally:
        STAGES.pop(pid, None)
    p = db.get_project(pid)
    req = p["request"]
    if req.get("engine") == "motion":
        try:
            await _motion_version(pid, "First cut")
        except Exception as e:
            _fail(pid, e, status="ready" if db.get_project(pid).get("video_path") else "failed")
            return
        if req.get("make_template"):
            await _save_as_template(pid)
        return
    try:
        if not p.get("timeline"):
            db.update_project(pid, status="scripting", progress=5)
            activity.add(pid, "Claude is writing the script")
            brand = brands.get(req.get("brand_id"))
            meta = await claude.write_script(
                topic=req["topic"], language=req.get("language", "English"),
                duration=int(req.get("duration", 45)), style=req.get("style", ""),
                notes="\n\n".join(filter(None, [
                    req.get("notes", ""), playbooks.script_notes(req.get("playbook_id")),
                    references.script_notes(req.get("reference_id"), float(req.get("duration", 45)), req.get("voice") or ""),
                    f'Open the voice-over with exactly this hook (first scene): "{req["hook"]}"' if req.get("hook") else "",
                    "No voice-over: viewers read each scene's text as on-screen captions, so write short, punchy "
                    "lines (at most 10 words each)." if req.get("voice") == tts.NO_VOICE else ""])),
                brand_context=brands.prompt_context(brand),
                source=source_material(req), project_id=pid, model=req.get("model"), effort=req.get("effort"),
            )
            tl = render.new_timeline(meta, req, brand)
            mood = str(meta.get("music") or "") or tl.get("music_mood") or req.get("style", "")
            if not tl["music"] and (m := await audio.pick_auto("", brand, pid, mood, float(req.get("duration", 45)))) \
                    and m.get("file"):  # a new track for this video: the timeline names it, so edits keep it
                tl["music"] = m["file"]
                if (c := audio.credit(m["file"])):
                    meta["description"] = f"{meta.get('description', '')}\n\n{c}".strip()
                activity.add(pid, f"Music: {next((x['name'] for x in audio.music_list() if x['file'] == m['file']), '')}"
                             " — added to the library", "done")
            db.update_project(pid, script=meta, timeline=tl)
            activity.add(pid, f"Script ready: “{meta['title']}” — {len(meta['scenes'])} scenes", "say")
        await _render_version(pid, "First cut")
    except Exception as e:
        _fail(pid, e)


def source_material(req: dict) -> str:
    return context.prompt_block(
        [m["id"] for m in req.get("context") or []],
        "Source material attached by the user (base the video on it and keep its facts accurate)",
    )


# Running jobs per video, so a delete can stop them.
_jobs: dict[str, set] = {}


def spawn(pid: str, coro):
    task = asyncio.get_running_loop().create_task(coro)
    _jobs.setdefault(pid, set()).add(task)
    task.add_done_callback(lambda t: _jobs.get(pid, set()).discard(t))
    return task


async def cancel(pid: str):
    """Stop everything running for this video (Claude agent, renders)."""
    tasks = list(_jobs.pop(pid, set()))
    for t in tasks:
        t.cancel()
    if tasks:
        await asyncio.wait(tasks, timeout=15)
    STAGES.pop(pid, None)


def start(pid: str):
    spawn(pid, run_project(pid))


# ---------- editing ----------

def apply_edit(tl: dict, meta: dict, edit: dict) -> tuple[dict, dict]:
    brand = brands.get(tl.get("brand_id"))
    old = {s["id"]: s for s in tl["scenes"]}
    scenes = []
    for e in edit.get("scenes") or []:
        prev = old.get(e.get("id") or "")
        text = (e.get("text") or (prev or {}).get("text") or "").strip()
        if not text:
            continue
        search = e.get("search") or (prev or {}).get("search", "")
        keep_clip = prev and not e.get("new_footage") and search == prev["search"]
        brand_clip = brands.clip_for_scene(brand, e.get("asset"))
        scenes.append({
            "id": prev["id"] if prev else f"s{uuid.uuid4().hex[:6]}",
            "text": text, "search": search,
            "clip": brand_clip or (prev.get("clip") if keep_clip else None),
            "clip_start": prev.get("clip_start", 0) if keep_clip and not brand_clip else 0,
        })
    if scenes:
        tl["scenes"] = scenes
    if isinstance(edit.get("captions"), dict):
        tl["captions"] = {**tl["captions"], **edit["captions"]}
    if edit.get("voice_rate"):
        tl["voice_rate"] = max(0.5, min(2.0, float(edit["voice_rate"])))
    if "motion" in edit:
        tl["motion"] = bool(edit["motion"])
    meta = dict(meta)
    if edit.get("title"):
        meta["title"] = edit["title"]
    meta["script"] = " ".join(s["text"] for s in tl["scenes"])
    return tl, meta


async def ai_edit(pid: str, instruction: str, source: str = "prompt", model: str | None = None,
                  effort: str | None = None):
    p = db.get_project(pid)
    activity.start(pid)
    if is_motion(p):
        try:
            extra = _extra(p)
            extra["history"].append({"at": time.time(), "source": source, "instruction": instruction[:2000],
                                     "summary": instruction.splitlines()[0][:160] if source == "prompt" else "Applied review comments"})
            db.update_project(pid, extra=extra, error=None)
            await _motion_version(pid, extra["history"][-1]["summary"], instruction, model, effort)
        except Exception as e:
            _fail(pid, e, status="ready" if p.get("video_path") else "failed")
        return
    try:
        db.update_project(pid, status="editing", progress=0, error=None)
        edit = await claude.edit_timeline(
            p["timeline"], p["script"], p["request"].get("language", "English"), instruction,
            brand_context=brands.prompt_context(brands.get(p["timeline"].get("brand_id"))),
            source=source_material(p["request"]), project_id=pid,
            model=model or p["request"].get("model"), effort=effort or p["request"].get("effort"),
        )
        tl, meta = apply_edit(p["timeline"], p["script"], edit)
        extra = _extra(p)
        summary = edit.get("summary") or instruction[:120]
        extra["history"].append({"at": time.time(), "source": source, "instruction": instruction[:2000],
                                 "summary": summary})
        db.update_project(pid, timeline=tl, script=meta, extra=extra)
        await _render_version(pid, summary)
    except Exception as e:
        _fail(pid, e, status="ready" if p.get("video_path") else "failed")


async def manual_edit(pid: str, timeline: dict, title: str | None = None):
    """Timeline edited by hand in the UI (scene text, clip swap, captions...)."""
    p = db.get_project(pid)
    activity.start(pid)
    old = {s["id"]: s for s in p["timeline"]["scenes"]}
    for s in timeline["scenes"]:
        s.setdefault("id", f"s{uuid.uuid4().hex[:6]}")
        if (s.get("clip") or {}).get("brand_clip"):  # picked from the brand library in the UI
            s["clip"] = brands.clip_for_scene(brands.get(s["clip"].get("brand_id")), s["clip"]["brand_clip"])
        prev = old.get(s["id"])
        if prev and s.get("search") != prev.get("search") and s.get("clip") == prev.get("clip"):
            s["clip"] = None  # new search phrase → fetch new footage
    meta = dict(p["script"])
    if title:
        meta["title"] = title
    meta["script"] = " ".join(s["text"] for s in timeline["scenes"])
    extra = _extra(p)
    extra["history"].append({"at": time.time(), "source": "manual", "summary": "Manual edit"})
    db.update_project(pid, timeline=timeline, script=meta, extra=extra)
    try:
        await _render_version(pid, "Manual edit")
    except Exception as e:
        _fail(pid, e, status="ready")


# ---------- templates: new episodes of a Claude Motion format (templates.py) ----------

async def _save_as_template(pid: str):
    """"Save it as a template when it's ready": the first version becomes a template (and its project's series
    template when the project has none)."""
    from . import templates
    p = db.get_project(pid)
    ref = references.get(p["request"].get("reference_id")) if p["request"].get("reference_id") else None
    try:
        t = templates.start(p, (ref or {}).get("name") or "")
        activity.add(pid, f"Saved as the template “{t['name']}”; Claude is reading its scenes", "done")
        if p.get("workspace_id") and (ws := db.get_workspace(p["workspace_id"])) and not ws["data"].get("template_id"):
            db.save_workspace(ws["id"], ws["name"], {**ws["data"], "template_id": t["id"]}, ws["created_at"])
        await templates.analyze(t["id"])
    except Exception as e:
        log.warning("saving %s as a template failed", pid, exc_info=True)
        activity.add(pid, f"Couldn't save it as a template: {e}", "warn")
        if "t" in locals() and (cur := templates.get(t["id"])):
            cur.update(status="failed", error=f"{e.__class__.__name__}: {e}"[:500])
            templates._save(cur)


_episode_writers = asyncio.Semaphore(2)  # Claude writes two episodes at a time; renders take turns (_motion_lock)


async def make_episode(pid: str):
    from . import templates
    p = db.get_project(pid)
    req = p["request"]
    db.update_project(pid, status="queued", progress=1)
    STAGES[pid] = "Preparing the template"
    t = await templates.wait_ready(req["template_id"])
    brand = brands.get(req.get("brand_id"))
    report = _reporter(pid)
    async with _episode_writers:
        db.update_project(pid, status="scripting", progress=5)
        STAGES[pid] = "Writing the episode"
        activity.add(pid, f"Claude is writing this episode of “{t['name']}”")
        ws = db.get_workspace(p["workspace_id"]) if p.get("workspace_id") else None
        data = await templates.write_episode(t, req["topic"], pid, (ws or {}).get("data", {}).get("notes", "") if ws else "")
    title = str(data.get("title") or req["topic"])[:100]
    script = {**(p.get("script") or {}), "title": title,
              "script": " ".join(str(s.get("narration") or "") for s in data.get("scenes", []) if isinstance(s, dict))}
    db.update_project(pid, script=script)
    activity.add(pid, f"Episode written: “{title}”", "say")
    out = VIDEOS_DIR / f"{pid}_v{(p.get('version') or 0) + 1}.mp4"
    instruction = templates.redraw_instruction(t, data) if req.get("template_mode") == "redraw" else None
    model = effort = None
    db.update_project(pid, status="queued", progress=20)
    async with _motion_lock:
        db.update_project(pid, status="rendering", progress=22)
        root = await templates.build(t, pid, data, req, brand, report)
        if instruction:
            db.update_project(pid, status="designing")
            activity.add(pid, "Claude is redrawing the scenes that belong to this topic")
            model, effort = usage.choose("motion", req.get("model"), req.get("effort"))
            tl = await motion.edit(pid, req, instruction, out, report, model, effort)
        else:
            tl = await motion.render_edits(pid, req, out, report, low=60)
        report(96, "Checking the video")
        try:
            found = templates.check_issues(t, await motion.check(root))
        except Exception:
            log.warning("episode check failed for %s", pid, exc_info=True)
            found = None
    tl["format"] = req.get("format", "shorts")
    tl["brand_id"] = req.get("brand_id")
    await render.thumbnail(out)
    extra = _extra(db.get_project(pid))
    extra["template"] = {"id": t["id"], "name": t["name"]}
    extra["quality"] = {"issues": found, "at": time.time(), "version": (p.get("version") or 0) + 1}
    db.update_project(pid, extra=extra)
    _save_version(pid, out, tl, f"From the template “{t['name']}”", model, effort)
    if found:
        activity.add(pid, f"The quality check found {len(found)} thing(s) to look at in the Design tab", "warn")
    activity.add(pid, "Episode ready", "done")


# ---------- design: direct edits to a Claude Motion video, rendered without Claude ----------

async def render_design(pid: str):
    p = db.get_project(pid)
    out = VIDEOS_DIR / f"{pid}_v{(p.get('version') or 0) + 1}.mp4"
    activity.start(pid)
    activity.add(pid, "Rendering your design changes — no Claude run")
    db.update_project(pid, status="rendering", progress=2)
    try:
        async with _motion_lock:
            tl = await motion.render_edits(pid, p["request"], out, _reporter(pid))
    finally:
        STAGES.pop(pid, None)
    design.keep(motion.project_root(pid))
    tl["format"] = p["request"].get("format", "shorts")
    tl["brand_id"] = p["request"].get("brand_id")
    await render.thumbnail(out)
    _save_version(pid, out, tl, "Design edits")
    activity.add(pid, "Version ready", "done")


# ---------- timeline editor: CapCut-style edits on top of a version ----------

def edit_base(p: dict) -> dict | None:
    """The version the editor works on: the one the saved edit started from, else the latest non-edited version."""
    vs = _extra(p)["versions"]
    saved = _extra(p).get("edit") or {}
    if saved.get("base") and (VIDEOS_DIR / saved["base"]["file"]).exists():
        return saved["base"]
    for v in reversed(vs):
        if not v.get("edited") and (VIDEOS_DIR / v["file"]).exists():
            return {"version": v["version"], "file": v["file"]}
    return None


async def export_edit(pid: str):
    p = db.get_project(pid)
    saved = _extra(p).get("edit") or {}
    base = edit_base(p)
    if not base or not saved.get("clips"):
        raise ValueError("Nothing to export")
    out = VIDEOS_DIR / f"{pid}_v{(p.get('version') or 0) + 1}.mp4"
    brand = brands.get((p.get("timeline") or {}).get("brand_id"))
    font = brands.asset_path(brand["id"], brand["font"]["file"]) if brand and brand.get("font") else None
    activity.start(pid)
    activity.add(pid, f"Exporting the edit of version {base['version']}")
    db.update_project(pid, status="rendering", progress=10)
    async with _render_lock:
        await editor.export(VIDEOS_DIR / base["file"], saved, out, render.project_dir(pid) / "edit",
                            family=brand["font"]["family"] if font and font.exists() else None,
                            font=font if font and font.exists() else None)
    await render.thumbnail(out)
    return _save_version(pid, out, p["timeline"], f"Edited (from v{base['version']})", edited=True)


def layers_base(p: dict) -> dict | None:
    """The version layers are made from: the latest one made by Claude or the Design tab (not an Edit-tab export),
    since the project's sources describe that one."""
    for v in reversed(_extra(p)["versions"]):
        if not v.get("edited") and (VIDEOS_DIR / v["file"]).exists():
            return {"version": v["version"], "file": v["file"]}
    return None


async def export_layers(pid: str):
    """The latest (non-edited) version as separate layers for other editors → extra["layers"]."""
    from . import layers
    p = db.get_project(pid)
    base = layers_base(p)
    if not base:
        raise ValueError("Render the video first")
    activity.start(pid)
    activity.add(pid, f"Exporting the layers of version {base['version']}")
    db.update_project(pid, status="rendering", progress=2)
    report = _reporter(pid)
    try:
        async with (_motion_lock if is_motion(p) else _render_lock):
            out = await layers.export(p, base, report)
    finally:
        STAGES.pop(pid, None)
    extra = _extra(db.get_project(pid))
    extra["layers"] = {"version": base["version"], "file": out.name, "size": out.stat().st_size, "at": time.time()}
    db.update_project(pid, extra=extra, status="ready", progress=100, error=None)
    activity.add(pid, "Layers ready", "done")


# ---------- review: comments on the render, applied by Claude ----------

def review_comments(p: dict, version: int | None = None) -> list[dict]:
    """Comments on one version (default: the current one). Each version keeps its own round of comments."""
    return _extra(p)["reviews"].get(str(version or p.get("version") or 0), [])


def _scene_at(scenes: list[dict], t: float) -> int | None:
    for i, s in enumerate(scenes, 1):
        if s.get("start") is not None and s["start"] <= t < (s.get("end") or 0) + 0.01:
            return i
    return len(scenes) if scenes and t >= (scenes[-1].get("end") or 0) else None


def _where(c: dict, scenes: list[dict]) -> str:
    if c.get("t") is None:
        return "Whole video"
    a = _scene_at(scenes, c["t"])
    if c.get("t2") is not None:
        b = _scene_at(scenes, c["t2"])
        sc = (f"Scene {a}" if a == b else f"Scenes {a}–{b}") if a else "Time"
        return f"{sc} · {c['t']:.1f}s–{c['t2']:.1f}s"
    return f"{'Scene ' + str(a) if a else 'Time'} · {c['t']:.1f}s"


async def apply_review(pid: str, model: str | None = None, effort: str | None = None) -> int:
    """Hand the current round's comments to Claude as one edit. Returns how many were sent."""
    p = db.get_project(pid)
    extra = _extra(p)
    round_ = extra["reviews"].get(str(p.get("version") or 0), [])
    todo = [c for c in round_ if not c.get("sent")]
    if not todo:
        raise ValueError("Add a comment first")
    scenes = (p.get("timeline") or {}).get("scenes") or []
    root = motion.project_root(pid) if is_motion(p) else None
    lines = []
    for i, c in enumerate(todo, 1):
        where = _where(c, scenes)
        if c.get("x") is not None:
            spot = f"pinned at {c['x'] * 100:.0f}% across, {c['y'] * 100:.0f}% down"
            if root and c.get("t") is not None:
                # Claude Motion's agent can look at the frame: draw the pin on it.
                img = root / "review" / f"{c['id']}.jpg"
                img.parent.mkdir(exist_ok=True)
                try:
                    await render._ffmpeg("-ss", f"{c['t']:.3f}", "-i", p["video_path"], "-frames:v", "1", "-vf",
                                         f"drawbox=x=iw*{c['x']:.4f}-28:y=ih*{c['y']:.4f}-28:w=56:h=56:color=red@0.95:t=6",
                                         img)
                    spot += f" — open review/{img.name} to see the spot (red square)"
                except Exception:
                    log.warning("could not extract review frame", exc_info=True)
            where += f", {spot}"
        lines.append(f"{i}. [{where}] {c['text']}")
    instruction = (f"Apply these review comments on version {p.get('version')}. Each says where in the video it "
                   "applies (scene number, seconds from the start). Change only what they ask; keep everything else.\n"
                   + "\n".join(lines))
    for c in todo:
        c["sent"] = True
    db.update_project(pid, extra=extra)
    spawn(pid, ai_edit(pid, instruction, source="review", model=model, effort=effort))
    return len(todo)


# ---------- Autopilot: niche → ideas → videos on a schedule ----------

async def autopilot_loop():
    while True:
        try:
            cfg = db.get_setting("autopilot", {})
            if cfg.get("enabled") and cfg.get("niche"):
                interval = max(1, float(cfg.get("every_hours", 24))) * 3600
                last = float(db.get_setting("autopilot_last_run", 0))
                if time.time() - last >= interval:
                    db.set_setting("autopilot_last_run", time.time())
                    if not settings.get("claude")["source"]:
                        db.set_setting("autopilot_last_error", "Skipped: Claude is not connected")
                    else:
                        db.set_setting("autopilot_last_error", None)
                        await _autopilot_run(cfg)
        except Exception as e:
            log.exception("autopilot tick failed")
            db.set_setting("autopilot_last_error", f"{e.__class__.__name__}: {e}"[:500])
        await asyncio.sleep(60)


async def _autopilot_run(cfg: dict):
    count = int(cfg.get("videos_per_run", 1))
    ideas = await claude.suggest_ideas(cfg["niche"], cfg.get("language", "English"), count=count + 3)
    used = set(db.get_setting("autopilot_used_topics", []))
    fresh = [i for i in ideas if i.get("topic") and i["topic"] not in used] or ideas
    random.shuffle(fresh)
    for idea in fresh[:count]:
        used.add(idea["topic"])
        p = db.create_project({
            "topic": f'{idea["topic"]} — hook: {idea.get("hook", "")}',
            "language": cfg.get("language", "English"),
            "duration": cfg.get("duration", 45),
            "format": cfg.get("format", "shorts"),
            "style": cfg.get("style", ""),
            "voice": cfg.get("voice", ""),
            "brand_id": cfg.get("brand_id"),
            "engine": cfg.get("engine") or default_engine(),
            "source": "autopilot",
        })
        await asyncio.wait([spawn(p["id"], run_project(p["id"]))])  # a delete may cancel it
    db.set_setting("autopilot_used_topics", list(used)[-500:])
