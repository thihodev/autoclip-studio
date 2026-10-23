import asyncio
import json
import logging
import re
import secrets
import shutil
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlencode

from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import edge_tts

from . import activity, audio, brands, channels, claude, compose, context, covers, db, design, editor, leftovers, localvoice, playbooks, styles, motion, pipeline, references, render, settings, templates, tts, usage
from .config import DATA_DIR, ROOT, VIDEOS_DIR

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    settings.migrate()
    context.cleanup(_context_in_use())
    references.interrupted()
    templates.interrupted()
    task = asyncio.create_task(pipeline.autopilot_loop())
    yield
    task.cancel()
    render.kill_running()
    claude.login_cancel()


app = FastAPI(title="AutoClip Studio", lifespan=lifespan)


def _context_in_use() -> set[str]:
    ids = {m["id"] for p in db.list_projects(limit=100000) for m in (p["request"].get("context") or [])}
    ids |= {m["id"] for w in db.list_workspaces() for m in (w["data"].get("context") or [])}
    return ids | {k["id"] for b in brands.list_all() for k in b.get("knowledge", [])}


def _bg(coro):
    asyncio.get_running_loop().create_task(coro)


log = logging.getLogger("autoclip")
BUSY = ("queued", "scripting", "designing", "editing", "rendering", "publishing")


def _project(pid: str) -> dict:
    p = db.get_project(pid)
    if not p:
        raise HTTPException(404, "Project not found")
    return p


# ------------------------------------------------------------------ models

class ProjectIn(BaseModel):
    topic: str = Field(min_length=3)
    language: str = "English"
    duration: int = Field(45, ge=10, le=1200)  # up to 20 minutes; the UI warns before a long Claude Motion run
    format: str = "shorts"  # shorts | youtube | square
    style: str = ""
    notes: str = ""
    voice: str = ""
    voice_rate: float = 1.0
    brand_id: str | None = None
    context_ids: list[str] = []
    engine: str | None = None  # motion | classic (default: motion when available)
    workspace_id: str | None = None
    model: str | None = None   # Claude model for this video (default: Connections setting for the engine)
    effort: str | None = None  # low | medium | high | xhigh | max (default: Connections setting)
    music: str = ""  # "" auto (brand music, else the library) · "none" · a library file
    sfx: bool = True  # sound effects (whooshes on cuts; Claude Motion places its own cues)
    hook: str | None = None  # the first spoken line, picked from Claude's suggestions
    playbook_id: str | None = None  # video type: structure, beats, pacing ("framejam:<id>" or "autoclip:<id>")
    style_id: str | None = None  # visual style for Claude Motion ("framejam:<id>" or "hyperframes:<id>")
    github_repo: str | None = None  # owner/repo: Claude bases the video on it (README, description, latest release)
    reference_id: str | None = None  # a video studied for its format (references.py): same structure and look, own content
    make_template: bool = False  # Claude Motion: save the first version as a template when it's ready


class IdeasIn(BaseModel):
    niche: str
    language: str = "English"
    count: int = Field(8, ge=1, le=20)


class AutopilotIn(BaseModel):
    enabled: bool = False
    niche: str = ""
    language: str = "English"
    every_hours: float = 24
    videos_per_run: int = Field(1, ge=1, le=10)
    duration: int = 45
    format: str = "shorts"
    style: str = ""
    voice: str = ""
    brand_id: str | None = None


class TimelineIn(BaseModel):
    timeline: dict
    title: str | None = None


class InstructionIn(BaseModel):
    instruction: str = Field(min_length=2)
    model: str | None = None
    effort: str | None = None


class CodeIn(BaseModel):
    code: str


class BrandIn(BaseModel):
    name: str = Field(min_length=1)


class ImportIn(BaseModel):
    url: str
    ref: str | None = None


class TokenIn(BaseModel):
    token: str


class ApiKeyIn(BaseModel):
    key: str
    use: bool = True


class SourceIn(BaseModel):
    source: str  # "" (disconnected) | app | system | api_key


# ------------------------------------------------------------------ status & settings

@app.get("/api/status")
async def status():
    claude_s = await asyncio.to_thread(claude.auth_status)
    return {
        "claude": claude_s,
        "footage": render.footage_status(),
        "voice": tts.status(),
        "motion": motion.status(),
    }


@app.get("/api/settings")
def get_settings():
    return settings.public()


@app.put("/api/settings/{section}")
async def put_settings(section: str, value=Body(...)):
    if section not in settings.DEFAULTS:
        raise HTTPException(404)
    if section == "voice" and isinstance(value, dict):
        key = (value.get("elevenlabs_api_key") or "").strip()
        if key and not key.startswith(settings.MASK):
            if err := await tts.eleven_check(key):
                raise HTTPException(400, err)
            value["elevenlabs_api_key"] = key
    if section == "footage" and isinstance(value, dict):
        # Validate keys that actually changed (masked values mean "unchanged").
        for provider in ("pexels", "pixabay"):
            key = value.get(f"{provider}_api_key") or ""
            if key and not key.startswith(settings.MASK):
                if err := await render.test_footage_key(provider, key.strip()):
                    raise HTTPException(400, err)
                value[f"{provider}_api_key"] = key.strip()
    settings.put(section, value)
    return {"ok": True}



# ------------------------------------------------------------------ Claude connection

@app.get("/api/claude")
async def claude_info():
    current, system, app_login = await asyncio.gather(
        asyncio.to_thread(claude.auth_status), asyncio.to_thread(claude.system_status),
        asyncio.to_thread(claude.app_status),
    )
    state = "connected" if current["ok"] else ("expired" if current["source"] else "disconnected")
    key = settings.get("claude")["api_key"]
    return {"state": state, "current": current, "system": system, "app": app_login, "login": claude.login_state(),
            "api_key": f"…{key[-4:]}" if key else None, "model": settings.get("claude")["model"]}


@app.post("/api/claude/login/start")
async def claude_login_start():
    try:
        return await asyncio.to_thread(claude.login_start)
    except Exception as e:
        raise HTTPException(500, f"Could not start sign-in: {e}")


@app.post("/api/claude/login/code")
async def claude_login_code(body: CodeIn):
    try:
        return await asyncio.to_thread(claude.login_submit, body.code)
    except Exception as e:
        raise HTTPException(400, str(e))


@app.post("/api/claude/login/cancel")
def claude_login_cancel():
    claude.login_cancel()
    return {"ok": True}


@app.post("/api/claude/source")
def claude_source(body: SourceIn):
    if body.source not in ("", "app", "system", "api_key"):
        raise HTTPException(400, "Unknown source")
    if body.source == "api_key" and not settings.get("claude")["api_key"]:
        raise HTTPException(400, "Add an API key first")
    settings.put("claude", {**settings.get("claude"), "source": body.source})
    return {"ok": True}


@app.post("/api/claude/api-key")
async def claude_api_key(body: ApiKeyIn):
    """Save (after checking) an Anthropic API key and use it."""
    key = body.key.strip()
    if err := await claude.check_api_key(key):
        raise HTTPException(400, err)
    settings.put("claude", {**settings.get("claude"), "api_key": key, "source": "api_key" if body.use else
                            settings.get("claude")["source"]})
    return {"ok": True}


@app.delete("/api/claude/api-key")
def claude_api_key_remove():
    cfg = settings.get("claude")
    settings.put("claude", {**cfg, "api_key": "", "source": "" if cfg["source"] == "api_key" else cfg["source"]})
    return {"ok": True}


@app.get("/api/claude/plan-usage")
async def claude_plan_usage(refresh: bool = False):
    return await claude.plan_usage(refresh)


@app.post("/api/claude/logout")
async def claude_logout():
    await asyncio.to_thread(claude.logout_app)
    settings.put("claude", {**settings.get("claude"), "source": ""})
    return {"ok": True}


# ------------------------------------------------------------------ timeline editor & layers

class EditIn(BaseModel):
    edit: dict


async def _edit_state(p: dict) -> dict:
    base = pipeline.edit_base(p)
    if not base:
        raise HTTPException(400, "Render the video first")
    path = VIDEOS_DIR / base["file"]
    dur = await render.media_duration(path)
    w, h = await render._ffmpeg_probe_size(path)
    saved = pipeline._extra(p).get("edit")
    newer = [v for v in pipeline._extra(p)["versions"] if v["version"] > base["version"] and not v.get("edited")]
    brand = brands.get((p.get("timeline") or {}).get("brand_id"))
    font = {"family": brand["font"]["family"], "url": f"/brand-media/{brand['id']}/{brand['font']['file']}"} \
        if brand and brand.get("font") else {"family": render.FONT_NAME, "url": "/app-fonts/" + render.FONT_PATH.name}
    return {"edit": saved or editor.fresh(dur), "saved": bool(saved), "base": {**base, "duration": dur, "width": w, "height": h},
            "newer": newer[-1]["version"] if newer else None, "font": font, "looks": editor.LOOKS}


@app.get("/api/projects/{pid}/edit")
async def get_edit(pid: str):
    return await _edit_state(_project(pid))


@app.put("/api/projects/{pid}/edit")
async def put_edit(pid: str, body: EditIn):
    """Save the timeline edit (the browser autosaves)."""
    p = _project(pid)
    base = pipeline.edit_base(p)
    if not base:
        raise HTTPException(400, "Render the video first")
    clean = editor.clean(body.edit, await render.media_duration(VIDEOS_DIR / base["file"]))
    extra = pipeline._extra(p)
    extra["edit"] = {**clean, "base": base, "at": time.time()}
    db.update_project(pid, extra=extra)
    return {"edit": clean, "duration": editor.duration(clean)}


@app.post("/api/projects/{pid}/edit/reset")
async def reset_edit(pid: str):
    """Start over from the latest version (drops the saved edit)."""
    p = _project(pid)
    extra = pipeline._extra(p)
    extra.pop("edit", None)
    db.update_project(pid, extra=extra)
    return await _edit_state(db.get_project(pid))


@app.post("/api/projects/{pid}/edit/export")
async def export_edit(pid: str):
    p = _project(pid)
    if p["status"] in BUSY:
        raise HTTPException(409, "This video is busy")
    if not (pipeline._extra(p).get("edit") or {}).get("clips"):
        raise HTTPException(400, "Nothing to export yet")

    async def job():
        try:
            await pipeline.export_edit(pid)
        except Exception as e:
            pipeline._fail(pid, e, status="ready")
    pipeline.spawn(pid, job())
    return db.get_project(pid)


@app.post("/api/projects/{pid}/layers")
async def export_layers(pid: str):
    """Export the video as separate layers (scenes, transparent captions, audio stems, SRT, FCPXML) in a ZIP."""
    p = _project(pid)
    if p["status"] in BUSY:
        raise HTTPException(409, "This video is busy")
    if not pipeline.layers_base(p):
        raise HTTPException(400, "Render the video first")
    if pipeline.is_motion(p) and design.dirty(motion.project_root(pid)):
        raise HTTPException(409, "Render or discard your Design changes first — layers are made from the rendered video")

    async def job():
        try:
            await pipeline.export_layers(pid)
        except Exception as e:
            pipeline._fail(pid, e, status="ready")
    pipeline.spawn(pid, job())
    return db.get_project(pid)


# ------------------------------------------------------------------ design: direct edits to a Claude Motion video

class DesignTextIn(BaseModel):
    scene: str
    index: int
    text: str = Field(max_length=500)


class DesignColorIn(BaseModel):
    old: str = Field(pattern=r"^#?[0-9a-fA-F]{3}([0-9a-fA-F]{3})?$")
    new: str = Field(pattern=r"^#?[0-9a-fA-F]{6}$")


class DesignHoldIn(BaseModel):
    scene: str
    hold: float = Field(ge=0, le=design.MAX_HOLD)


class DesignFitIn(BaseModel):
    scene: str
    index: int
    size: float = Field(gt=0, le=1000)


class DesignCaptionIn(BaseModel):
    id: str
    text: str = Field(max_length=300)


class DesignFontIn(BaseModel):
    old: str = Field(max_length=80)
    new: str = Field(max_length=80)
    weights: dict[str, int] | None = None


def _design_root(pid: str, editing: bool = False) -> Path:
    p = _project(pid)
    root = motion.project_root(pid)
    if not pipeline.is_motion(p) or not (root / "index.html").exists():
        raise HTTPException(400, "Only Claude Motion videos can be edited here")
    if editing and p["status"] in BUSY:
        raise HTTPException(409, "This video is busy")
    return root


def _design_state(root: Path) -> dict:
    return {**design.state(root),
            "player": f"https://cdn.jsdelivr.net/npm/@hyperframes/player@{motion.HF_VERSION}/dist/hyperframes-player.global.js"}


_baselines: dict[str, asyncio.Task] = {}


def _design(fn, *args) -> dict:
    root = args[0]
    try:
        fn(*args)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if design.dirty(root) and not (root / design.BASELINE).exists():
        _baseline(root)
    return _design_state(root)


def _baseline(root: Path) -> asyncio.Task:
    """Check the version the edits started from once (in the background), to tell new problems from old ones."""
    key = str(root)
    if key in _baselines and not _baselines[key].done():
        return _baselines[key]

    async def job():
        copy = root / ".autoclip" / "baseline-project"
        try:
            design.baseline_copy(root, copy)
            found = design.issues(await motion.check(copy))
            if (root / ".autoclip").exists():
                (root / design.BASELINE).write_text(json.dumps(found))
        finally:
            shutil.rmtree(copy, ignore_errors=True)
    _baselines[key] = asyncio.get_running_loop().create_task(job())
    return _baselines[key]


@app.get("/api/projects/{pid}/design")
async def get_design(pid: str):
    return _design_state(_design_root(pid))


@app.put("/api/projects/{pid}/design/text")
async def design_text(pid: str, body: DesignTextIn):
    return _design(design.edit_text, _design_root(pid, True), body.scene, body.index, body.text)


@app.put("/api/projects/{pid}/design/color")
async def design_color(pid: str, body: DesignColorIn):
    return _design(design.edit_color, _design_root(pid, True), body.old, body.new)


@app.put("/api/projects/{pid}/design/hold")
async def design_hold(pid: str, body: DesignHoldIn):
    return _design(design.edit_hold, _design_root(pid, True), body.scene, body.hold)


@app.put("/api/projects/{pid}/design/fit")
async def design_fit(pid: str, body: DesignFitIn):
    return _design(design.fit_text, _design_root(pid, True), body.scene, body.index, body.size)


@app.put("/api/projects/{pid}/design/caption")
async def design_caption(pid: str, body: DesignCaptionIn):
    return _design(design.edit_caption, _design_root(pid, True), body.id, body.text)


@app.put("/api/projects/{pid}/design/font")
async def design_font(pid: str, body: DesignFontIn):
    return _design(design.edit_font, _design_root(pid, True), body.old, body.new, body.weights)


@app.put("/api/projects/{pid}/design/media")
async def design_media(pid: str, scene: str = Form(...), index: int = Form(...), fit: str = Form("keep"),
                       everywhere: bool = Form(False), file: UploadFile = File(...)):
    """Replace an image or video in a scene with an uploaded file (up to 200 MB)."""
    root = _design_root(pid, True)
    data = await file.read(200 * 1024 * 1024 + 1)
    if len(data) > 200 * 1024 * 1024:
        raise HTTPException(413, "That file is too big (200 MB at most)")
    return _design(design.replace_media, root, scene, index, data, file.filename or "upload", fit, everywhere)


@app.post("/api/projects/{pid}/design/undo")
async def design_undo(pid: str):
    return _design(design.undo, _design_root(pid, True))


@app.post("/api/projects/{pid}/design/check")
async def design_check(pid: str):
    """Check the edited video with HyperFrames (layout, contrast, errors): the problems the edits brought in."""
    root = _design_root(pid, True)
    compared = True
    try:
        await _baseline(root) if not (root / design.BASELINE).exists() else None
    except Exception:
        log.warning("design baseline check failed for %s", pid, exc_info=True)
        compared = False
    try:
        report = await motion.check(root)
    except RuntimeError as e:
        raise HTTPException(500, str(e))
    return {"issues": design.new_issues(root, report), "compared": compared and (root / design.BASELINE).exists()}


@app.post("/api/projects/{pid}/design/discard")
async def design_discard(pid: str):
    return _design(design.discard, _design_root(pid, True))


@app.post("/api/projects/{pid}/design/render")
async def design_render(pid: str):
    """Render the direct edits as a new version with HyperFrames (no Claude run)."""
    root = _design_root(pid, True)
    if not design.dirty(root):
        raise HTTPException(400, "Nothing changed yet")

    async def job():
        try:
            await pipeline.render_design(pid)
        except Exception as e:
            pipeline._fail(pid, e, status="ready")
    pipeline.spawn(pid, job())
    return db.get_project(pid)


@app.get("/api/projects/{pid}/design/files/{path:path}")
async def design_file(pid: str, path: str):
    """The HyperFrames project for the live preview; frames come with their texts marked for clicking."""
    root = _design_root(pid).resolve()
    f = (root / path).resolve()
    if not f.is_relative_to(root) or not f.is_file() or f.relative_to(root).parts[0] in (".autoclip", ".claude", "renders"):
        raise HTTPException(404)
    if f.suffix == ".html":
        src = f.read_text(encoding="utf-8")
        rel = f.relative_to(root).as_posix()
        if rel.startswith("compositions/frames/"):
            scene = next((s["id"] for s in design.scenes(root) if s["src"] == rel), None)
            if scene:
                src = design.annotate(src, scene)
        return Response(src, media_type="text/html", headers={"Cache-Control": "no-store"})
    return FileResponse(f)


# ------------------------------------------------------------------ templates: a Motion video as a series format

class TemplateIn(BaseModel):
    project_id: str
    name: str = Field("", max_length=80)


class TemplatePatch(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class EpisodesIn(BaseModel):
    episodes: list[str] = Field(min_length=1, max_length=templates.MAX_EPISODES)
    mode: str = Field("fill", pattern="^(fill|redraw)$")
    workspace_id: str | None = None


class EpisodeIdeasIn(BaseModel):
    count: int = Field(5, ge=1, le=10)
    hint: str = Field("", max_length=300)
    workspace_id: str | None = None


class PlanIn(BaseModel):
    topic: str = Field(min_length=3, max_length=4000)
    language: str = "English"
    duration: int = Field(45, ge=10, le=1200)
    format: str = "shorts"
    playbook_id: str | None = None
    brand_id: str | None = None
    workspace_id: str | None = None
    context_ids: list[str] = []
    avoid: list[str] = Field([], max_length=5)
    reference_id: str | None = None
    voice: str = ""


class MoveIn(BaseModel):
    workspace_id: str | None = None


def _template(tid: str) -> dict:
    t = templates.get(tid)
    if not t:
        raise HTTPException(404, "Template not found")
    return t


def _episodes(tid: str) -> list[dict]:
    keep = ("id", "status", "progress", "version", "video_path", "error", "created_at")
    out = []
    for p in db.template_projects(tid):
        q = (pipeline._extra(p).get("quality") or {})
        out.append({**{k: p.get(k) for k in keep}, "title": (p.get("script") or {}).get("title") or p["request"]["topic"],
                    "topic": p["request"]["topic"], "stage": pipeline.STAGES.get(p["id"]),
                    "issues": len(q.get("issues") or []) if q.get("version") == p.get("version") else None})
    return out


def _with_counts(t: dict, counts: dict | None = None) -> dict:
    return {**t, "episodes": (counts if counts is not None else db.template_counts()).get(t["id"], 0)}


@app.get("/api/templates")
def list_templates():
    counts = db.template_counts()
    return [_with_counts(t, counts) for t in templates.list_all()]


@app.post("/api/templates")
async def create_template(body: TemplateIn):
    """Save a Claude Motion video as a template; Claude reads its scenes in the background (status "analyzing")."""
    _require_setup()
    p = _project(body.project_id)
    if p["status"] in BUSY:
        raise HTTPException(409, "This video is busy")
    if not pipeline.is_motion(p):
        raise HTTPException(400, "Only Claude Motion videos can become templates")
    try:
        t = templates.start(p, body.name)
    except ValueError as e:
        raise HTTPException(400, str(e))

    async def job():
        try:
            await templates.analyze(t["id"])
        except Exception as e:
            log.warning("template analysis failed for %s", t["id"], exc_info=True)
            if (cur := templates.get(t["id"])):
                cur.update(status="failed", error=f"{e.__class__.__name__}: {e}"[:500])
                templates._save(cur)
    _bg(job())
    return _with_counts(t)


@app.get("/api/templates/{tid}")
def get_template(tid: str):
    return {**_with_counts(_template(tid)), "list": _episodes(tid)}


@app.patch("/api/templates/{tid}")
def rename_template(tid: str, body: TemplatePatch):
    _template(tid)
    return _with_counts(templates.update(tid, body.name))


@app.post("/api/templates/{tid}/retry")
async def retry_template(tid: str):
    t = _template(tid)
    if t["status"] != "failed":
        raise HTTPException(409, "This template isn't stuck")
    t.update(status="analyzing", error=None)
    templates._save(t)

    async def job():
        try:
            await templates.analyze(tid)
        except Exception as e:
            if (cur := templates.get(tid)):
                cur.update(status="failed", error=f"{e.__class__.__name__}: {e}"[:500])
                templates._save(cur)
    _bg(job())
    return _with_counts(t)


@app.delete("/api/templates/{tid}")
def delete_template(tid: str):
    """Delete a template. Its episodes stay: each has its own copy of the project."""
    _template(tid)
    templates.delete(tid)
    return {"ok": True}


@app.get("/api/templates/{tid}/preview.mp4")
def template_preview(tid: str):
    _template(tid)
    return FileResponse(templates.preview(tid), media_type="video/mp4")


@app.get("/api/templates/{tid}/cover.jpg")
def template_cover(tid: str):
    _template(tid)
    f = templates.cover(tid)
    if not f.exists():
        raise HTTPException(404)
    return FileResponse(f, media_type="image/jpeg")


@app.post("/api/templates/{tid}/episodes")
async def make_episodes(tid: str, body: EpisodesIn):
    """New episodes from a template: one video per line, made one after another."""
    _require_setup()
    t = _template(tid)
    if t["status"] == "failed":
        raise HTTPException(409, "This template couldn't be prepared — try again first")
    briefs = [" ".join(b.split())[:500] for b in body.episodes if b.strip()]
    if not briefs:
        raise HTTPException(400, "Write what at least one episode is about")
    if body.workspace_id and not db.get_workspace(body.workspace_id):
        raise HTTPException(400, "That project no longer exists")
    made = []
    for brief in briefs:
        req = {"topic": brief, "engine": "motion", "template_id": tid, "template_mode": body.mode,
               "format": t["format"], "language": t["language"], "voice": t["voice"], "voice_rate": t["voice_rate"],
               "brand_id": t["brand_id"] if t["brand_id"] and brands.get(t["brand_id"]) else None,
               "music": t["music"], "sfx": t["sfx"], "duration": round(t["duration"])}
        p = db.create_project(req, body.workspace_id)
        pipeline.start(p["id"])
        made.append(p)
    return made


@app.post("/api/templates/{tid}/ideas")
async def episode_ideas(tid: str, body: EpisodeIdeasIn):
    """Claude suggests the next episodes of a series, different from the ones already made."""
    _require_setup()
    t = _template(tid)
    if t["status"] != "ready":
        raise HTTPException(409, "The template is still being prepared")
    done = [(p.get("script") or {}).get("title") or p["request"]["topic"] for p in db.template_projects(tid)]
    ws = db.get_workspace(body.workspace_id) if body.workspace_id else None
    try:
        return {"ideas": await templates.suggest_episodes(t, [t["example"], *done], body.count, body.hint,
                                                          (ws or {}).get("data", {}).get("notes", "") if ws else "")}
    except Exception as e:
        raise HTTPException(500, str(e)[:300])


# ------------------------------------------------------------------ references: other videos studied for their format

@app.get("/api/references")
def list_references():
    return references.list_all()


@app.post("/api/references")
async def add_reference(url: str = Form(""), file: UploadFile | None = File(None)):
    """Study a video from a link (TikTok, YouTube, Instagram, Facebook…) or a file; Claude reads it in the background."""
    _require_setup()
    try:
        if file is not None and file.filename:
            data = await file.read(references.MAX_BYTES + 1)
            if len(data) > references.MAX_BYTES:
                raise HTTPException(413, "That file is too big (500 MB at most)")
            r = references.from_file(data, file.filename)
        else:
            r = references.from_url(url)
    except ValueError as e:
        raise HTTPException(400, str(e))
    _bg(references.run(r["id"]))
    return r


@app.get("/api/references/{rid}")
def get_reference(rid: str):
    r = references.get(rid)
    if not r:
        raise HTTPException(404, "Reference not found")
    return r


class RefSoundIn(BaseModel):
    voice: str | None = None
    music: str | None = None


@app.put("/api/references/{rid}/sound")
def set_reference_sound(rid: str, body: RefSoundIn):
    """Correct what Claude heard: whether anyone speaks, and the music."""
    if not references.get(rid):
        raise HTTPException(404, "Reference not found")
    try:
        return references.set_sound(rid, body.voice, body.music)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/references/{rid}/retry")
async def retry_reference(rid: str):
    r = references.get(rid)
    if not r:
        raise HTTPException(404, "Reference not found")
    if r["status"] in ("queued", "working"):
        raise HTTPException(409, "It's still being studied")
    references._set(rid, status="queued", error=None)
    _bg(references.run(rid))
    return references.get(rid)


@app.delete("/api/references/{rid}")
def delete_reference(rid: str):
    references.delete(rid)
    return {"ok": True}


@app.get("/api/references/{rid}/{name:path}")
def reference_file(rid: str, name: str):
    f = references.file(rid, name) if references.get(rid) else None
    if not f:
        raise HTTPException(404)
    return FileResponse(f)


# ------------------------------------------------------------------ planning: the idea before the video

class WsIdeasIn(BaseModel):
    count: int = Field(3, ge=1, le=6)
    hint: str = Field("", max_length=300)


class IdeaIn(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    angle: str = Field("", max_length=400)
    hook: str = Field("", max_length=200)
    outline: list[str] = Field([], max_length=12)


class MakeIdeaIn(IdeaIn):
    mode: str = Field("redraw", pattern="^(fill|redraw)$")


def _channel_lengths(d: dict) -> dict[str, int] | None:
    """A channel's formats with their length in seconds, e.g. {"youtube": 600, "shorts": 45}."""
    ch = d.get("channel") or {}
    lengths = (ch.get("plan") or {}).get("lengths") or {}
    return {f: max(10, min(1200, int(lengths.get(f) or (480 if f == "youtube" else 45)))) for f in ch.get("formats") or []} or None


def _ws_or_404(wid: str) -> dict:
    w = db.get_workspace(wid)
    if not w:
        raise HTTPException(404, "Project not found")
    return w


def _idea_brief(i: dict) -> str:
    """An approved idea as the brief Claude works from."""
    parts = [i["title"].strip()]
    if i.get("angle", "").strip():
        parts.append(i["angle"].strip())
    if outline := [o.strip() for o in i.get("outline") or [] if o.strip()]:
        parts.append("Scenes:\n" + "\n".join(f"{n}. {o}" for n, o in enumerate(outline, 1)))
    return "\n\n".join(parts)


@app.post("/api/workspaces/{wid}/ideas")
async def workspace_ideas(wid: str, body: WsIdeasIn):
    """Claude suggests the next videos of this project, knowing the videos and ideas it already has."""
    _require_setup()
    w = _ws_or_404(wid)
    d = w["data"]
    composer = db.get_setting("composer", {}) or {}
    t = templates.get(d.get("template_id")) if d.get("template_id") else None
    done = [(p.get("script") or {}).get("title") or p["request"]["topic"].split("\n")[0][:120]
            for p in db.list_projects(limit=200, workspace_id=wid)]
    done += [i["title"] for i in d.get("ideas") or [] if i.get("status") != "made"]
    extra = []
    if d.get("notes"):
        extra.append(f"Project instructions: {d['notes'][:1500]}")
    if t:
        extra.append(f"Every video is an episode of the format \"{t['name']}\": {t.get('summary', '')}")
    if (plan := (d.get("channel") or {}).get("plan")):
        extra.append(channel_context(plan))
    if (brand := brands.get(d.get("brand_id"))):
        extra.append(brands.prompt_context(brand)[:1200])
    if d.get("context"):
        extra.append(context.prompt_block([m["id"] for m in d["context"]], "Source material for the series", budget=10_000))
    language = (t or {}).get("language") or d.get("language") or composer.get("language") or "English"
    duration = round((t or {}).get("duration") or composer.get("duration") or 45)
    lengths = (None if t else _channel_lengths(d)) or {(t or {}).get("format") or d.get("format") or composer.get("format") or "shorts": duration}
    try:
        found = await claude.series_ideas(w["name"], language, lengths, done, body.count, "\n".join(extra), body.hint)
    except Exception as e:
        raise HTTPException(500, str(e)[:300])
    w = _ws_or_404(wid)  # re-read: Claude took a while
    ideas = [{"id": uuid.uuid4().hex[:10], **i, "status": "new", "at": time.time()} for i in found]
    db.save_workspace(wid, w["name"], {**w["data"], "ideas": ideas + list(w["data"].get("ideas") or [])}, w["created_at"])
    return _workspace(wid)


def _set_idea(wid: str, iid: str, **fields) -> dict:
    w = _ws_or_404(wid)
    ideas = list(w["data"].get("ideas") or [])
    for i, idea in enumerate(ideas):
        if idea["id"] == iid:
            ideas[i] = {**idea, **fields}
            db.save_workspace(wid, w["name"], {**w["data"], "ideas": ideas}, w["created_at"])
            return ideas[i]
    raise HTTPException(404, "That idea is no longer there")


@app.put("/api/workspaces/{wid}/ideas/{iid}")
def edit_idea(wid: str, iid: str, body: IdeaIn):
    _set_idea(wid, iid, **body.model_dump())
    return _workspace(wid)


@app.delete("/api/workspaces/{wid}/ideas/{iid}")
def dismiss_idea(wid: str, iid: str):
    _set_idea(wid, iid, status="dismissed")
    return _workspace(wid)


@app.post("/api/workspaces/{wid}/ideas/{iid}/make")
async def make_idea(wid: str, iid: str, body: MakeIdeaIn):
    """Approve an idea: the video is made from it (an episode of the project's template, or a new video)."""
    _require_setup()
    w = _ws_or_404(wid)
    idea = _set_idea(wid, iid, **body.model_dump(exclude={"mode"}))
    if idea.get("project_id") and db.get_project(idea["project_id"]):
        raise HTTPException(409, "A video is already being made from this idea")
    t = templates.get(w["data"].get("template_id")) if w["data"].get("template_id") else None
    brief = _idea_brief(idea)
    if t:
        made = await make_episodes(t["id"], EpisodesIn(episodes=[brief], mode=body.mode, workspace_id=wid))
        p = made[0]
    else:
        composer = {k: v for k, v in (db.get_setting("composer", {}) or {}).items() if k in ProjectIn.model_fields}
        fmt = {}
        if idea.get("format") and (lengths := _channel_lengths(w["data"]) or {}).get(idea["format"]):
            fmt = {"format": idea["format"], "duration": lengths[idea["format"]]}
        p = await create_project(ProjectIn(**{**composer, **fmt,
                                              "topic": brief, "hook": idea.get("hook") or None, "workspace_id": wid}))
    _set_idea(wid, iid, status="made", project_id=p["id"])
    return {"project": p, "workspace": _workspace(wid)}

@app.post("/api/plan")
async def plan(body: PlanIn):
    """A concept to approve before making the video: title, angle, audience, opening line and scene outline."""
    _require_setup()
    extra = []
    if (ws := db.get_workspace(body.workspace_id) if body.workspace_id else None):
        if ws["data"].get("notes"):
            extra.append(f"Project instructions: {ws['data']['notes'][:1500]}")
    if (pb := playbooks.get(body.playbook_id)):
        extra.append(f"Video type: {pb['name']} — {pb['tagline']}")
    if (brand := brands.get(body.brand_id or ((ws or {}).get("data") or {}).get("brand_id"))):
        extra.append(brands.prompt_context(brand)[:1500])
    ids = [*body.context_ids, *[m["id"] for m in ((ws or {}).get("data") or {}).get("context") or []]]
    if ids:
        extra.append(context.prompt_block(ids, "Source material", budget=12_000))
    if body.reference_id and (notes := references.script_notes(body.reference_id, body.duration, body.voice)):
        extra.append(notes)
    try:
        return await claude.plan_video(body.topic.strip(), body.language, body.duration, body.format, "\n".join(extra),
                                       body.avoid)
    except Exception as e:
        raise HTTPException(500, str(e)[:300])


@app.put("/api/projects/{pid}/workspace")
def move_project(pid: str, body: MoveIn):
    """Move a video into a project (or out of every project)."""
    _project(pid)
    if body.workspace_id and not db.get_workspace(body.workspace_id):
        raise HTTPException(400, "That project no longer exists")
    db.update_project(pid, workspace_id=body.workspace_id)
    return db.get_project(pid)


# ------------------------------------------------------------------ ideas & projects

@app.post("/api/ideas")
async def ideas(body: IdeasIn):
    try:
        return await claude.suggest_ideas(body.niche, body.language, body.count)
    except Exception as e:
        raise HTTPException(500, str(e))


def _with_stage(p: dict) -> dict:
    p["stage"] = pipeline.STAGES.get(p["id"])
    return p


@app.get("/api/projects")
def projects(workspace: str | None = None):
    return [_with_stage(p) for p in db.list_projects(workspace_id=workspace)]


@app.get("/api/projects/{pid}/activity")
def project_activity(pid: str):
    _project(pid)
    return activity.get(pid)


def _require_setup():
    if not settings.get("claude")["source"]:
        raise HTTPException(400, "Connect Claude first (Connections → Claude)")


# The composer remembers its last choices (not the topic or files) for the next video.
COMPOSER_KEYS = {"language", "format", "duration", "style", "style_id", "playbook_id", "music", "sfx", "voice", "voice_rate", "engine", "model", "effort",
                 "brand_id", "plan"}


# ------------------------------------------------------------------ hooks, post copy, covers

class HooksIn(BaseModel):
    topic: str = Field(min_length=3)
    language: str = "English"
    playbook_id: str | None = None
    brand_id: str | None = None


@app.post("/api/hooks")
async def hooks(body: HooksIn):
    _require_setup()
    extra = []
    if (pb := playbooks.get(body.playbook_id)):
        extra.append(f"Video type: {pb['name']} — {pb['tagline']}")
    if (brand := brands.get(body.brand_id)):
        extra.append(brands.prompt_context(brand)[:1500])
    try:
        return await claude.suggest_hooks(body.topic, body.language, "\n".join(extra))
    except Exception as e:
        raise HTTPException(500, str(e)[:300])


def _narration(p: dict) -> str:
    return " ".join(s.get("text", "") for s in (p.get("timeline") or {}).get("scenes") or []).strip()


class MusicIn(BaseModel):
    instruction: str = Field("", max_length=1000)
    volume: float | None = Field(None, ge=0, le=1)


@app.get("/api/projects/{pid}/music")
def get_music(pid: str):
    """The video's current music bed, for the Music card."""
    info = pipeline.music_info(_project(pid))
    return {**{k: v for k, v in info.items() if k != "file"}, "has_music": bool(info["file"])}


@app.get("/api/projects/{pid}/music/audio")
def music_audio(pid: str):
    f = pipeline.music_info(_project(pid))["file"]
    if not f or not Path(f).is_file():
        raise HTTPException(404, "No music")
    return FileResponse(f)


@app.post("/api/projects/{pid}/music")
async def change_music(pid: str, body: MusicIn):
    """Change only the music of a finished video: Claude rewrites the score from the note, and/or a new volume."""
    p = _project(pid)
    if not p.get("video_path") or p["status"] not in ("ready", "failed"):
        raise HTTPException(409, "Wait until the video is ready")
    if not body.instruction.strip() and body.volume is None:
        raise HTTPException(400, "Say what to change, or set the volume")
    if body.instruction.strip():
        _require_setup()
        if not compose.chrome():
            raise HTTPException(400, "Composing music needs Claude Motion installed (Connections → Claude Motion)")
    pipeline.spawn(pid, pipeline.change_music(pid, body.instruction.strip(), body.volume))
    db.update_project(pid, status="rendering", progress=1)
    return db.get_project(pid)


def _music_file(p: dict) -> str | None:
    """The library track the latest version plays: Claude Motion's audio_meta.json bed, or the Quick timeline's."""
    if (p.get("timeline") or {}).get("engine") == "classic":
        return (p.get("timeline") or {}).get("music") or None
    meta = motion.project_root(p["id"]) / "audio_meta.json"
    try:
        return Path((json.loads(meta.read_text()).get("bgm") or {}).get("path") or "").name or None
    except (OSError, ValueError):
        return None


@app.post("/api/projects/{pid}/post-copy")
async def write_post_copy(pid: str):
    """Title, caption, hashtags, tags and cover text, written by Claude from what the video says."""
    p = _project(pid)
    _require_setup()
    req = p["request"]
    brand = brands.get((p.get("timeline") or {}).get("brand_id") or req.get("brand_id"))
    try:
        post = await claude.write_post((p.get("script") or {}).get("title") or req["topic"][:90],
                                       _narration(p) or req["topic"], req.get("language", "English"),
                                       brands.prompt_context(brand)[:1500] if brand else "", project_id=pid)
    except Exception as e:
        raise HTTPException(500, str(e)[:300])
    keep = {k: post[k] for k in ("title", "description", "hashtags", "youtube_tags", "thumbnail_text") if post.get(k)}
    if (c := audio.credit(_music_file(p))) and c not in keep.get("description", ""):
        keep["description"] = f"{keep.get('description', '')}\n\n{c}".strip()  # CC BY music must be credited
    db.update_project(pid, script={**(p.get("script") or {}), **keep})
    return db.get_project(pid)["script"]


class PostCopyIn(BaseModel):
    title: str = Field("", max_length=100)
    description: str = Field("", max_length=5000)
    hashtags: list[str] = []
    youtube_tags: list[str] = []


@app.put("/api/projects/{pid}/post-copy")
def save_post_copy(pid: str, body: PostCopyIn):
    p = _project(pid)
    clean = lambda xs: [x.strip().lstrip("#") for x in xs if x.strip()][:30]  # noqa: E731
    script = {**(p.get("script") or {}), "title": body.title.strip(), "description": body.description.strip(),
              "hashtags": clean(body.hashtags), "youtube_tags": clean(body.youtube_tags)}
    db.update_project(pid, script=script)
    return script


class CoversIn(BaseModel):
    text: str = Field("", max_length=80)
    version: int | None = None  # default: the latest


@app.post("/api/projects/{pid}/covers")
async def make_covers(pid: str, body: CoversIn):
    p = _project(pid)
    v = next((v for v in pipeline._extra(p)["versions"] if v["version"] == body.version), None) if body.version else None
    video = VIDEOS_DIR / v["file"] if v else Path(p.get("video_path") or "")
    if not p.get("video_path") or not video.is_file():
        raise HTTPException(400, "Render the video first")
    script = p.get("script") or {}
    text = body.text.strip() or script.get("thumbnail_text") or script.get("title") or p["request"]["topic"][:40]
    brand = brands.get((p.get("timeline") or {}).get("brand_id") or p["request"].get("brand_id"))
    try:
        items = await covers.make(video, text, brand)
    except Exception as e:
        raise HTTPException(500, f"Could not make covers: {str(e)[:300]}")
    extra = pipeline._extra(p)
    extra["covers"] = {"version": v["version"] if v else p.get("version"), "text": text, "items": items, "chosen": 0, "at": int(time.time())}
    db.update_project(pid, extra=extra)
    return extra["covers"]


@app.put("/api/projects/{pid}/covers/{index}")
def choose_cover(pid: str, index: int):
    p = _project(pid)
    extra = pipeline._extra(p)
    cv = extra.get("covers") or {}
    if not 0 <= index < len(cv.get("items") or []):
        raise HTTPException(404)
    cv["chosen"] = index
    db.update_project(pid, extra=extra)
    return cv


# ------------------------------------------------------------------ music & sound effects

@app.get("/api/sfx")
async def list_sfx():
    await audio.sfx_pack()
    return audio.sfx_list()


@app.get("/api/sfx/{name}.wav")
async def sfx_file(name: str):
    if name not in audio.SFX:
        raise HTTPException(404)
    return FileResponse((await audio.sfx_pack()) / f"{name}.wav", media_type="audio/wav")


@app.get("/api/music")
def list_music():
    return audio.music_list()


@app.post("/api/music")
async def add_music(file: UploadFile = File(...), name: str = Form(""), mood: str = Form("")):
    tmp = render.project_dir("_uploads") / f"{uuid.uuid4().hex}{Path(file.filename or '').suffix}"
    with open(tmp, "wb") as f:
        shutil.copyfileobj(file.file, f)
    try:
        return await audio.music_add(tmp, name or Path(file.filename or "track").stem, mood)
    except ValueError as e:
        raise HTTPException(400, str(e))
    finally:
        tmp.unlink(missing_ok=True)


class MusicGenIn(BaseModel):
    prompt: str = Field(min_length=3, max_length=500)
    seconds: int = Field(60, ge=10, le=300)


@app.post("/api/music/generate")
async def generate_music(body: MusicGenIn):
    try:
        return await audio.music_generate(body.prompt, body.seconds)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/music/{file}/audio")
def music_file(file: str):
    p = audio.MUSIC_DIR / Path(file).name
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(p)


@app.delete("/api/music/{file}")
def delete_music(file: str):
    audio.music_remove(file)
    return audio.music_list()


@app.get("/api/playbooks")
def list_playbooks():
    """Video types (structure and pacing): AutoClip's own and FrameJam's faceless playbooks."""
    return playbooks.list_all()


@app.get("/api/styles")
def list_styles():
    """Visual styles for Claude Motion, from open-source projects (FrameJam, HyperFrames)."""
    return styles.list_all()


@app.get("/api/composer")
def get_composer():
    return db.get_setting("composer", {}) or {}


@app.put("/api/composer")
def put_composer(value: dict = Body(...)):
    db.set_setting("composer", {k: v for k, v in value.items() if k in COMPOSER_KEYS})
    return {"ok": True}


@app.post("/api/projects")
async def create_project(body: ProjectIn):
    _require_setup()
    if body.brand_id and not brands.get(body.brand_id):
        raise HTTPException(400, "That brand kit no longer exists")
    if body.workspace_id and not db.get_workspace(body.workspace_id):
        raise HTTPException(400, "That project no longer exists")
    req = body.model_dump(exclude={"context_ids", "workspace_id"})
    if body.github_repo:
        try:
            req["github_repo"] = brands.parse_repo(body.github_repo)
        except ValueError as e:
            raise HTTPException(400, str(e))
    if body.reference_id and (references.get(body.reference_id) or {}).get("status") != "ready":
        raise HTTPException(400, "That reference video isn't ready")
    req["context"] = [m for cid in body.context_ids if (m := context.get(cid))]
    req["engine"] = body.engine or pipeline.default_engine()
    if req["engine"] == "motion" and (st := motion.status())["code"] in ("node", "ffmpeg"):
        raise HTTPException(400, f"Claude Motion needs setup: {st['detail']}")
    p = db.create_project(req, body.workspace_id)
    pipeline.start(p["id"])
    return p


@app.get("/api/projects/{pid}")
def get_project(pid: str):
    return _with_stage(_project(pid))


@app.delete("/api/projects/{pid}")
async def delete_project(pid: str):
    """Delete a video completely: stop its jobs, then remove every file and record it owns."""
    p = _project(pid)
    await pipeline.cancel(pid)
    for f in VIDEOS_DIR.glob(f"{pid}_v*"):  # every version + its thumbnail
        f.unlink(missing_ok=True)
    for d in claude.transcript_dirs(DATA_DIR / "projects" / pid):  # Claude's session transcripts for this video
        shutil.rmtree(d, ignore_errors=True)
    render.clear_cache(pid)  # scenes, footage, voice, captions, the Claude Motion project
    activity.clear(pid)
    own = [m["id"] for m in p["request"].get("context") or []]
    db.delete_project(pid)
    db.detach_usage(pid)  # usage history is account consumption; it stays, without the link
    in_use = _context_in_use()  # files shared with the project (workspace) or other videos stay
    for cid in own:
        if cid not in in_use:
            context.remove(cid)
    return {"ok": True}

@app.get("/api/leftovers")
def list_leftovers():
    """Files still on disk from videos deleted by earlier versions."""
    return leftovers.list_all()


@app.delete("/api/leftovers")
def delete_leftovers(id: str | None = None):
    return {"removed": leftovers.remove([id] if id else None)}


@app.post("/api/projects/{pid}/retry")
async def retry(pid: str):
    _project(pid)
    _require_setup()
    db.update_project(pid, status="queued", progress=0, error=None)
    pipeline.start(pid)
    return {"ok": True}


# ------------------------------------------------------------------ context files

async def _save_upload(file: UploadFile) -> dict:
    tmp = render.project_dir("_uploads") / f"{uuid.uuid4().hex}{Path(file.filename or '').suffix}"
    with open(tmp, "wb") as f:
        shutil.copyfileobj(file.file, f)
    try:
        return await asyncio.to_thread(context.save, tmp, file.filename or tmp.name)
    except ValueError as e:
        raise HTTPException(400, str(e))
    finally:
        tmp.unlink(missing_ok=True)


@app.post("/api/context")
async def upload_context(file: UploadFile = File(...)):
    return await _save_upload(file)


@app.delete("/api/context/{cid}")
def delete_context(cid: str):
    if cid in _context_in_use():
        raise HTTPException(400, "This file is used by a video or brand kit")
    context.remove(cid)
    return {"ok": True}


@app.post("/api/projects/{pid}/context")
async def add_project_context(pid: str, file: UploadFile = File(...)):
    p = _project(pid)
    meta = await _save_upload(file)
    req = {**p["request"], "context": [*(p["request"].get("context") or []), meta]}
    db.update_project(pid, request=req)
    return db.get_project(pid)


@app.delete("/api/projects/{pid}/context/{cid}")
def remove_project_context(pid: str, cid: str):
    p = _project(pid)
    req = {**p["request"], "context": [m for m in p["request"].get("context") or [] if m["id"] != cid]}
    db.update_project(pid, request=req)
    context.remove(cid)
    return db.get_project(pid)


# ------------------------------------------------------------------ editor

@app.put("/api/projects/{pid}/timeline")
async def save_timeline(pid: str, body: TimelineIn):
    p = _project(pid)
    if not p.get("timeline"):
        raise HTTPException(400, "Video has no timeline yet")
    if pipeline.is_motion(p):
        raise HTTPException(400, "Claude Motion videos are edited with Claude (AI & review)")
    pipeline.spawn(pid, pipeline.manual_edit(pid, body.timeline, body.title))
    return {"ok": True}


@app.post("/api/projects/{pid}/ai-edit")
async def ai_edit(pid: str, body: InstructionIn):
    p = _project(pid)
    if not p.get("timeline"):
        raise HTTPException(400, "Video has no timeline yet")
    pipeline.spawn(pid, pipeline.ai_edit(pid, body.instruction, model=body.model or None, effort=body.effort or None))
    return {"ok": True}


@app.get("/api/projects/{pid}/footage")
async def footage(pid: str, q: str):
    p = _project(pid)
    size = render.SIZES.get((p.get("timeline") or {}).get("format", "shorts"), render.SIZES["shorts"])
    try:
        return await render.search_footage(q, size, per_page=12)
    except Exception as e:
        raise HTTPException(400, str(e))


@app.post("/api/projects/{pid}/upload")
async def upload_clip(pid: str, file: UploadFile = File(...)):
    _project(pid)
    ext = Path(file.filename or "clip.mp4").suffix.lower() or ".mp4"
    if ext not in (".mp4", ".mov", ".webm", ".mkv", ".m4v"):
        raise HTTPException(400, "Upload a video file (mp4, mov, webm, mkv)")
    dest = render.project_dir(pid) / "media" / f"upload_{uuid.uuid4().hex[:8]}{ext}"
    with open(dest, "wb") as f:
        shutil.copyfileobj(file.file, f)
    thumb = await render.thumbnail(dest)
    return {"id": f"upload:{dest.name}", "path": str(dest), "thumb_local": str(thumb), "provider": "upload",
            "duration": await render.media_duration(dest)}


# ------------------------------------------------------------------ review (comments on the render)

class CommentIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)
    t: float | None = None   # seconds; None = the whole video
    t2: float | None = None  # end of a range
    x: float | None = Field(None, ge=0, le=1)  # a spot pinned on the frame, as a fraction of width/height
    y: float | None = Field(None, ge=0, le=1)


def _review_view(p: dict, version: int | None = None) -> dict:
    extra = p.get("extra") or {}
    return {"version": version or p.get("version") or 0, "comments": pipeline.review_comments(p, version),
            "rounds": {v: len(c) for v, c in (extra.get("reviews") or {}).items()}}


@app.get("/api/projects/{pid}/review")
def get_review(pid: str, version: int | None = None):
    return _review_view(_project(pid), version)


@app.post("/api/projects/{pid}/review")
def add_comment(pid: str, body: CommentIn):
    p = _project(pid)
    if not p.get("video_path"):
        raise HTTPException(400, "Render the video first")
    extra = pipeline._extra(p)
    c = {"id": uuid.uuid4().hex[:8], "text": body.text.strip(), "at": time.time(),
         "t": round(body.t, 2) if body.t is not None else None,
         "t2": round(body.t2, 2) if body.t is not None and body.t2 is not None and body.t2 > body.t else None,
         "x": body.x if body.t is not None and body.x is not None and body.y is not None else None,
         "y": body.y if body.t is not None and body.x is not None and body.y is not None else None}
    extra["reviews"].setdefault(str(p.get("version") or 0), []).append(c)
    db.update_project(pid, extra=extra)
    return _review_view(db.get_project(pid))


@app.delete("/api/projects/{pid}/review/{cid}")
def delete_comment(pid: str, cid: str):
    p = _project(pid)
    extra = pipeline._extra(p)
    key = str(p.get("version") or 0)
    extra["reviews"][key] = [c for c in extra["reviews"].get(key, []) if c["id"] != cid or c.get("sent")]
    db.update_project(pid, extra=extra)
    return _review_view(db.get_project(pid))


@app.post("/api/projects/{pid}/review/apply")
async def apply_review(pid: str, body: dict = Body(default={})):
    p = _project(pid)
    if p["status"] not in ("ready", "failed", "published"):
        raise HTTPException(409, "Wait for the current job to finish")
    _require_setup()
    try:
        n = await pipeline.apply_review(pid, body.get("model") or None, body.get("effort") or None)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"sent": n}


@app.get("/api/projects/{pid}/filmstrip")
async def filmstrip(pid: str, file: str | None = None):
    """A strip of evenly spaced frames of the current version (or of `file`, one of its versions), for the review
    timeline and the editor's clip thumbnails."""
    p = _project(pid)
    if file:
        if Path(file).name not in {v["file"] for v in pipeline._extra(p)["versions"]} or not (VIDEOS_DIR / Path(file).name).exists():
            raise HTTPException(404)
        src = VIDEOS_DIR / Path(file).name
    elif not p.get("video_path") or not Path(p["video_path"]).exists():
        raise HTTPException(404)
    else:
        src = Path(p["video_path"])
    out = VIDEOS_DIR / f"{src.stem}_strip.jpg"
    dur = await render.media_duration(src)
    count = max(8, min(60, round(dur)))
    if not out.exists():
        await render._ffmpeg("-i", src, "-vf", f"fps={count / dur:.5f},scale=-2:90,tile={count}x1", "-frames:v", "1",
                             "-q:v", "5", out)
    return {"url": f"/videos/{out.name}", "count": count, "duration": dur}


# ------------------------------------------------------------------ autopilot

@app.get("/api/autopilot")
def get_autopilot():
    cfg = db.get_setting("autopilot", AutopilotIn().model_dump())
    last = db.get_setting("autopilot_last_run", 0)
    every = max(1, float(cfg.get("every_hours", 24))) * 3600
    return {**cfg, "last_run": last or None, "next_run": (last + every if last else time.time()) if cfg.get("enabled") else None,
            "last_error": db.get_setting("autopilot_last_error")}


@app.put("/api/autopilot")
def put_autopilot(body: AutopilotIn):
    if body.enabled:
        if not body.niche.strip():
            raise HTTPException(400, "Enter a niche before enabling autopilot")
        _require_setup()
    db.set_setting("autopilot", body.model_dump())
    return get_autopilot()


# ------------------------------------------------------------------ files

@app.get("/videos/{name}")
def video(name: str):
    path = VIDEOS_DIR / Path(name).name
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type="video/mp4" if path.suffix == ".mp4" else None)


@app.get("/media/{pid}/{name}")
def media(pid: str, name: str):
    path = DATA_DIR / "projects" / Path(pid).name / "media" / Path(name).name  # never re-create a deleted video's folder
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path)


# ------------------------------------------------------------------ projects (workspaces)

WS_FIELDS = ("notes", "website", "brand_id", "language", "format", "template_id", "series")


def _workspace(wid: str) -> dict:
    w = db.get_workspace(wid)
    if not w:
        raise HTTPException(404, "Project not found")
    return {"id": w["id"], "name": w["name"], "created_at": w["created_at"],
            **{k: w["data"].get(k, "") for k in WS_FIELDS}, "context": w["data"].get("context", []),
            "ideas": [i for i in w["data"].get("ideas") or [] if i.get("status") != "dismissed"],
            "channel": w["data"].get("channel"), "videos": len(db.list_projects(limit=100000, workspace_id=wid))}


class WorkspaceIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    notes: str = ""
    website: str = ""
    brand_id: str | None = None
    language: str = ""
    format: str = ""
    template_id: str | None = None  # the series this project makes: its composer makes episodes of it
    series: bool | None = None  # its videos are numbered episodes of a series (None: unchanged)


@app.get("/api/workspaces")
def list_workspaces():
    return [_workspace(w["id"]) for w in db.list_workspaces()]


@app.post("/api/workspaces")
def create_workspace(body: WorkspaceIn):
    wid = uuid.uuid4().hex[:10]
    db.save_workspace(wid, body.name.strip(), {**body.model_dump(exclude={"name"}), "context": []})
    return _workspace(wid)


@app.get("/api/workspaces/{wid}")
def get_workspace(wid: str):
    return _workspace(wid)


@app.put("/api/workspaces/{wid}")
def update_workspace(wid: str, body: WorkspaceIn):
    w = db.get_workspace(wid)
    if not w:
        raise HTTPException(404, "Project not found")
    if body.brand_id and not brands.get(body.brand_id):
        raise HTTPException(400, "That brand kit no longer exists")
    if body.template_id and not templates.get(body.template_id):
        raise HTTPException(400, "That template no longer exists")
    db.save_workspace(wid, body.name.strip(), {**w["data"], **body.model_dump(exclude={"name"} | ({"series"} if body.series is None else set()))},
                      w["created_at"])
    return _workspace(wid)


@app.delete("/api/workspaces/{wid}")
def delete_workspace(wid: str):
    w = db.get_workspace(wid)
    if w:
        db.delete_workspace(wid)
        channels.remove(wid)
        in_use = _context_in_use()
        for m in w["data"].get("context") or []:
            if m["id"] not in in_use:
                context.remove(m["id"])
    return {"ok": True}


@app.post("/api/workspaces/{wid}/context")
async def add_workspace_context(wid: str, file: UploadFile = File(...)):
    w = db.get_workspace(wid)
    if not w:
        raise HTTPException(404, "Project not found")
    meta = await _save_upload(file)
    db.save_workspace(wid, w["name"], {**w["data"], "context": [*(w["data"].get("context") or []), meta]}, w["created_at"])
    return _workspace(wid)


@app.delete("/api/workspaces/{wid}/context/{cid}")
def remove_workspace_context(wid: str, cid: str):
    w = db.get_workspace(wid)
    if not w:
        raise HTTPException(404, "Project not found")
    db.save_workspace(wid, w["name"], {**w["data"], "context": [m for m in w["data"].get("context") or [] if m["id"] != cid]},
                      w["created_at"])
    if cid not in _context_in_use():
        context.remove(cid)
    return _workspace(wid)



# ------------------------------------------------------------------ channels: a project built around one topic

class ChannelStudyIn(BaseModel):
    url: str = Field(min_length=8, max_length=500)


class ChannelPillar(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    about: str = Field("", max_length=300)
    examples: list[str] = Field([], max_length=3)


class ChannelPlan(BaseModel):
    """The channel as approved (and edited) by the person; the first videos go to the idea board."""
    name: str = Field(min_length=1, max_length=60)
    names: list[str] = Field([], max_length=3)
    handles: list[str] = Field([], max_length=3)
    tagline: str = Field("", max_length=80)
    audience: str = Field("", max_length=300)
    positioning: str = Field("", max_length=500)
    voice: str = Field("", max_length=200)
    cta: str = Field("", max_length=200)
    pillars: list[ChannelPillar] = Field([], max_length=6)
    cadence: str = Field("", max_length=300)
    bio_youtube: str = Field("", max_length=1000)
    bio_tiktok: str = Field("", max_length=80)
    hashtags: list[str] = Field([], max_length=12)
    colors: dict[str, str] = {}
    learned: list[str] = Field([], max_length=8)
    instructions: str = Field("", max_length=2000)
    lengths: dict[str, int] = {}  # seconds per video in each format
    first_videos: list[dict] = Field([], max_length=5)


FORMATS = ("shorts", "youtube", "square")


class ChannelPlanIn(BaseModel):
    topic: str = Field(min_length=3, max_length=2000)
    language: str = "English"
    formats: list[str] = Field(["shorts"], min_length=1, max_length=3)  # e.g. long 16:9 on YouTube and 9:16 for TikTok
    platforms: list[str] = Field(["YouTube", "TikTok"], max_length=2)
    competitors: list[dict] = Field([], max_length=3)  # what /api/channels/study returned
    avoid: list[str] = Field([], max_length=5)


class ChannelIn(ChannelPlanIn):
    plan: ChannelPlan


def _formats(formats: list[str]) -> list[str]:
    return [f for f in FORMATS if f in formats] or ["shorts"]


def channel_context(plan: dict) -> str:
    """What every idea of the channel keeps to."""
    lines = [f"This project is the channel \"{plan['name']}\"" + (f": {plan['tagline']}" if plan.get("tagline") else "")]
    if plan.get("audience"):
        lines.append(f"Audience: {plan['audience']}")
    if plan.get("pillars"):
        lines.append("Content pillars (rotate between them; favor the ones with fewer videos so far):\n"
                     + "\n".join(f"- {p['name']}: {p.get('about', '')}" for p in plan["pillars"]))
    return "\n".join(lines)


def _competitors(found: list[dict]) -> list[dict]:
    """Keep what the plan and the channel page use from a study (it comes back from the browser)."""
    keep = ("url", "name", "site", "followers", "median_views", "count", "about", "recent")
    return [{**{k: c.get(k) for k in keep}, "top": [{k: v.get(k) for k in ("title", "views", "duration", "url")}
                                                   for v in (c.get("top") or [])[:channels.TOP] if isinstance(v, dict)]}
            for c in found[:3] if isinstance(c, dict) and c.get("name")]


@app.post("/api/channels/study")
async def study_channel(body: ChannelStudyIn):
    """A channel already in the niche: its latest videos and which did best (yt-dlp, no Claude)."""
    try:
        return await asyncio.to_thread(channels.study, body.url)
    except ValueError as e:
        raise HTTPException(400, str(e))


class DiscoverIn(BaseModel):
    interest: str = Field(min_length=2, max_length=300)
    language: str = "Vietnamese"
    queries: list[str] = Field([], max_length=3)
    channels: list[dict] = Field([], max_length=10)  # what /api/channels/discover/search returned


@app.get("/api/channels/discover")
def last_discovery():
    """The last niche search, kept so closing the dialog loses nothing."""
    return db.get_setting("channel_discovery")


@app.post("/api/channels/discover/queries")
async def discover_queries(body: DiscoverIn):
    """Step 1: English YouTube searches for an interest (one small Claude call)."""
    _require_setup()
    try:
        return {"queries": await claude.niche_queries(body.interest.strip())}
    except Exception as e:
        raise HTTPException(500, str(e)[:300])


@app.post("/api/channels/discover/search")
async def discover_search(body: DiscoverIn):
    """Step 2: the channels abroad that do best on those searches, with their numbers (yt-dlp, no Claude)."""
    queries = [q.strip() for q in body.queries if q.strip()] or [body.interest.strip()]
    try:
        return {"channels": await asyncio.to_thread(channels.search, queries)}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/channels/discover/directions")
async def discover_directions(body: DiscoverIn):
    """Step 3: Claude proposes the directions that work, from those channels (one call); the result is kept."""
    _require_setup()
    if not body.channels:
        raise HTTPException(400, "Search for channels first")
    found = [{k: c.get(k) for k in ("name", "url", "followers", "median_views", "ratio", "count", "about", "top", "search_top")}
             for c in body.channels if isinstance(c, dict) and c.get("name")]
    try:
        res = await claude.niche_directions(body.interest.strip(), body.language, channels.discovery_block(found))
    except Exception as e:
        raise HTTPException(500, str(e)[:300])
    keep = {**body.model_dump(), "channels": body.channels, **res, "at": time.time()}
    db.set_setting("channel_discovery", keep)
    return keep


@app.post("/api/channels/plan")
async def plan_channel(body: ChannelPlanIn):
    """Claude plans a channel around a topic (one call): to approve before anything is set up."""
    _require_setup()
    try:
        return await claude.channel_plan(body.topic.strip(), body.language, _formats(body.formats), body.platforms,
                                         channels.prompt_block(_competitors(body.competitors)), avoid=body.avoid)
    except Exception as e:
        raise HTTPException(500, str(e)[:300])


@app.post("/api/channels")
def create_channel(body: ChannelIn):
    """Set up an approved channel: a brand kit, a project with its instructions, its first ideas, a profile picture and
    a banner."""
    plan = body.plan.model_dump()
    colors = {k: v for k, v in plan["colors"].items() if k in ("primary", "accent", "text") and re.fullmatch(r"#[0-9a-fA-F]{6}", v)}
    brand = brands.create(plan["name"], {
        "description": " ".join(x for x in (plan["tagline"], plan["positioning"]) if x),
        "voice": plan["voice"], "audience": plan["audience"], "cta": plan["cta"], "language": body.language,
        **({"colors": colors} if colors else {})})
    formats = _formats(body.formats)
    ideas = [{"id": uuid.uuid4().hex[:10], **i, "pillar": str(v.get("pillar") or "")[:60],
              "format": v.get("format") if v.get("format") in formats else formats[0], "status": "new", "at": time.time()}
             for v in plan.pop("first_videos") if (i := claude._plan_item(v))]
    wid = uuid.uuid4().hex[:10]
    channel = {"topic": body.topic.strip(), "platforms": body.platforms, "formats": formats, "plan": plan, "competitors": _competitors(body.competitors),
               "art": channels.make_art(wid, plan["name"], plan["tagline"], brand)}
    db.save_workspace(wid, plan["name"], {"notes": plan["instructions"], "website": "", "brand_id": brand["id"], "language": body.language,
                                          "format": formats[0] if len(formats) == 1 else "", "template_id": None, "series": True, "context": [], "ideas": ideas,
                                          "channel": channel})
    return _workspace(wid)


@app.put("/api/workspaces/{wid}/channel")
def update_channel(wid: str, body: ChannelPlan):
    """Edit the channel's plan (names, bios, pillars…); the profile art is redrawn when the name or tagline changed."""
    w = _ws_or_404(wid)
    ch = w["data"].get("channel")
    if not ch:
        raise HTTPException(400, "This project isn't a channel")
    plan = {**body.model_dump(exclude={"first_videos"}), "colors": ch["plan"].get("colors", {})}
    if (plan["name"], plan["tagline"]) != (ch["plan"]["name"], ch["plan"].get("tagline")):
        ch["art"] = channels.make_art(wid, plan["name"], plan["tagline"], brands.get(w["data"].get("brand_id")), plan["colors"])
    db.save_workspace(wid, w["name"], {**w["data"], "channel": {**ch, "plan": plan}}, w["created_at"])
    return _workspace(wid)


@app.post("/api/workspaces/{wid}/art")
def redraw_art(wid: str):
    """Draw the profile pictures and banners again, e.g. after the brand kit's colors, font or logo changed."""
    w = _ws_or_404(wid)
    ch = w["data"].get("channel")
    if not ch:
        raise HTTPException(400, "This project isn't a channel")
    ch["art"] = channels.make_art(wid, ch["plan"]["name"], ch["plan"].get("tagline", ""),
                                  brands.get(w["data"].get("brand_id")), ch["plan"].get("colors"))
    db.save_workspace(wid, w["name"], {**w["data"], "channel": ch}, w["created_at"])
    return _workspace(wid)


@app.get("/api/workspaces/{wid}/art/{name}")
def art_file(wid: str, name: str):
    path = channels.art_file(wid, name)
    if not path:
        raise HTTPException(404)
    return FileResponse(path, headers={"Cache-Control": "no-cache"})

# ------------------------------------------------------------------ brand kits

def _brand(bid: str) -> dict:
    b = brands.get(bid)
    if not b:
        raise HTTPException(404, "Brand kit not found")
    return b


@app.get("/api/brands")
def list_brands():
    return brands.list_all()


@app.post("/api/brands")
def create_brand(body: BrandIn):
    return brands.create(body.name)


@app.get("/api/brands/{bid}")
def get_brand(bid: str):
    return _brand(bid)


@app.put("/api/brands/{bid}")
def update_brand(bid: str, patch: dict = Body(...)):
    _brand(bid)
    return brands.update(bid, patch)


@app.delete("/api/brands/{bid}")
def delete_brand(bid: str):
    _brand(bid)
    brands.delete(bid)
    return {"ok": True}


@app.post("/api/brands/{bid}/assets")
async def add_brand_asset(bid: str, kind: str = Form(...), file: UploadFile = File(...), description: str = Form("")):
    _brand(bid)
    if kind not in brands.KIND_EXT:
        raise HTTPException(400, "Unknown asset kind")
    tmp = render.project_dir("_uploads") / f"{uuid.uuid4().hex}{Path(file.filename or '').suffix}"
    with open(tmp, "wb") as f:
        shutil.copyfileobj(file.file, f)
    try:
        return await brands.add_asset(bid, kind, tmp, file.filename or kind, description)
    except ValueError as e:
        raise HTTPException(400, str(e))
    finally:
        tmp.unlink(missing_ok=True)


@app.delete("/api/brands/{bid}/assets/{kind}")
def remove_brand_asset(bid: str, kind: str, file: str | None = None):
    _brand(bid)
    return brands.remove_asset(bid, kind, file)


@app.post("/api/brands/import")
async def import_brand(body: ImportIn):
    try:
        return await brands.import_github(body.url, body.ref or None)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        logging.exception("GitHub import failed")
        raise HTTPException(502, f"Import failed: {e.__class__.__name__}: {e}"[:300])


class WebsiteIn(BaseModel):
    url: str = Field(min_length=3)


@app.post("/api/brands/import-website")
async def import_brand_website(body: WebsiteIn):
    if not brands.find_domain(body.url):
        raise HTTPException(400, "Enter a website like example.com")
    try:
        return await brands.import_website(body.url)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        logging.exception("website import failed")
        raise HTTPException(502, f"Import failed: {e.__class__.__name__}: {e}"[:300])


@app.post("/api/brands/{bid}/sync")
async def sync_brand(bid: str):
    b = _brand(bid)
    src = b["source"]
    if src.get("type") == "website":
        try:
            return await brands.import_website(src["url"], bid=bid)
        except Exception as e:
            raise HTTPException(502, f"Sync failed: {e}"[:300])
    if src.get("type") != "github":
        raise HTTPException(400, "This brand kit wasn't imported from GitHub or a website")
    try:
        return await brands.import_github(src["repo"], src.get("ref"), bid=bid)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/brand-media/{bid}/site/{name}")
def brand_site_media(bid: str, name: str):
    path = brands.asset_path(bid, f"site/{name}")
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path)


@app.get("/brand-media/{bid}/{name}")
def brand_media(bid: str, name: str):
    path = brands.asset_path(bid, name)
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path)


# ------------------------------------------------------------------ GitHub connection

@app.get("/api/github")
def github_info():
    cfg = settings.get("github")
    return {"connected": bool(cfg["token"]), "login": cfg["login"]}


@app.post("/api/github")
async def github_connect(body: TokenIn):
    try:
        user = await brands.github_user(body.token.strip())
    except ValueError as e:
        raise HTTPException(400, str(e))
    settings.put("github", {"token": body.token.strip(), "login": user["login"]})
    return {"connected": True, "login": user["login"]}


@app.delete("/api/github")
def github_disconnect():
    settings.put("github", {"token": "", "login": ""})
    return {"connected": False, "login": ""}


@app.get("/api/github/refs")
async def github_refs(repo: str):
    try:
        return await brands.github_refs(brands.parse_repo(repo))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(502, f"GitHub: {e}")


@app.get("/api/github/repos")
async def github_repos():
    try:
        return await brands.github_repos()
    except Exception as e:
        raise HTTPException(502, f"GitHub: {e}")


# ------------------------------------------------------------------ models & usage

@app.get("/api/models")
def models():
    cfg = settings.get("claude")
    return {"models": usage.MODELS, "efforts": usage.EFFORTS,
            "defaults": {"script": {"model": cfg["model"], "effort": cfg["effort"]},
                         "motion": {"model": cfg["motion_model"], "effort": cfg["motion_effort"]},
                         "designer": {"model": cfg["designer_model"], "effort": cfg["designer_effort"]}}}


@app.get("/api/usage")
def usage_summary(days: int = 30):
    return usage.summary(max(1, min(365, days)))


@app.get("/api/projects/{pid}/usage")
def project_usage(pid: str):
    _project(pid)
    return usage.project(pid)


# ------------------------------------------------------------------ Claude Motion engine

_install = {"running": False, "error": None}


@app.get("/api/motion")
def motion_status():
    return {**motion.status(), "installing": _install["running"], "install_error": _install["error"],
            "default_engine": pipeline.default_engine()}




@app.post("/api/motion/install")
async def motion_install():
    if _install["running"]:
        return motion_status()

    async def run():
        _install.update(running=True, error=None)
        try:
            await motion.install()
        except Exception as e:
            logging.exception("motion install failed")
            _install["error"] = str(e)[:500]
        finally:
            _install["running"] = False

    _bg(run())
    await asyncio.sleep(0.1)
    return motion_status()


# ------------------------------------------------------------------ voice-over (ElevenLabs)

class VoiceTestIn(BaseModel):
    text: str = Field(min_length=1, max_length=400)
    language: str = "English"
    voice: str = ""  # edge-tts name or "eleven:<id>"; empty = the default for the language / account
    rate: float = Field(1.0, ge=0.5, le=2.0)


@app.get("/api/voice")
def voice_status():
    return tts.status()


@app.get("/api/voice/elevenlabs")
async def eleven_voices():
    if not settings.get("voice")["elevenlabs_api_key"]:
        return []
    try:
        return await tts.eleven_voices()
    except Exception as e:
        raise HTTPException(502, f"ElevenLabs: {e}")


@app.post("/api/voice/preview")
async def voice_preview(body: VoiceTestIn):
    out = render.project_dir("_uploads") / f"preview_{uuid.uuid4().hex[:8]}.mp3"
    voice = re.sub(r"-(Male|Female)$", "", body.voice)
    try:
        await tts.synthesize(body.text, body.language, voice, body.rate, out)
    except Exception as e:
        raise HTTPException(502, str(e)[:300])
    data = out.read_bytes()
    out.unlink(missing_ok=True)
    from fastapi import Response
    return Response(data, media_type="audio/mpeg")


# ------------------------------------------------------------------ local voice (F5-TTS, on this computer)

@app.get("/api/localvoice")
def localvoice_status():
    return localvoice.status()


@app.post("/api/localvoice/install")
async def localvoice_install():
    if localvoice.supported():
        raise HTTPException(400, localvoice.supported())
    _bg(localvoice.install())
    await asyncio.sleep(0.1)
    return localvoice.status()


@app.delete("/api/localvoice")
def localvoice_remove():
    localvoice.remove()
    return localvoice.status()


@app.post("/api/localvoice/voices")
async def localvoice_add(file: UploadFile = File(...), name: str = Form(""), text: str = Form("")):
    data = await file.read(30 * 1024 * 1024 + 1)
    if len(data) > 30 * 1024 * 1024:
        raise HTTPException(413, "That file is too big; use a clip of a few seconds")
    try:
        await localvoice.add_voice(data, file.filename or "voice.wav", name, text)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return localvoice.status()


@app.delete("/api/localvoice/voices/{vid}")
def localvoice_remove_voice(vid: str):
    localvoice.remove_voice(vid)
    return localvoice.status()


@app.get("/api/localvoice/voices/{vid}/sample.wav")
def localvoice_sample(vid: str):
    if not (p := localvoice.sample(vid)):
        raise HTTPException(404)
    return FileResponse(p, media_type="audio/wav")


# ------------------------------------------------------------------ voices

_voices_cache: list | None = None


def _local_voices() -> list[dict]:
    """The voices on this computer, offered first (the model speaks Vietnamese)."""
    if not localvoice.installed():
        return []
    return [{"name": localvoice.PREFIX + v["id"], "provider": "local", "label": v["name"]} for v in localvoice.voices()]


@app.get("/api/voices")
async def voices(language: str = "English"):
    """Voices to pick for a video: the ElevenLabs account's voices when connected, else free edge-tts voices."""
    local = _local_voices() if language == "Vietnamese" else []
    if tts.provider() == "elevenlabs":
        try:
            return local + [{"name": tts.ELEVEN_PREFIX + v["id"], "provider": "elevenlabs", "label": v["name"],
                     "gender": v.get("gender"), "accent": v.get("accent")} for v in await tts.eleven_voices()]
        except Exception:
            logging.warning("could not list ElevenLabs voices; offering free voices", exc_info=True)
    global _voices_cache
    if _voices_cache is None:
        try:
            _voices_cache = await edge_tts.list_voices()
        except Exception as e:
            raise HTTPException(502, f"Could not load voices ({e.__class__.__name__})")
    prefix = render.LANGUAGE_LOCALES.get(language, "en")
    return local + [
        {"name": v["ShortName"], "gender": v.get("Gender"), "locale": v.get("Locale")}
        for v in _voices_cache if v.get("Locale", "").split("-")[0] == prefix
    ]


# ------------------------------------------------------------------ pages (client-side routes)

INDEX = ROOT / "static" / "index.html"


@app.get("/", include_in_schema=False)
@app.get("/ideas", include_in_schema=False)
@app.get("/autopilot", include_in_schema=False)
@app.get("/connections", include_in_schema=False)
@app.get("/usage", include_in_schema=False)
@app.get("/w/{wid}", include_in_schema=False)
@app.get("/brands", include_in_schema=False)
@app.get("/brands/{bid}", include_in_schema=False)
@app.get("/templates", include_in_schema=False)
@app.get("/templates/{tid}", include_in_schema=False)
@app.get("/projects/{pid}", include_in_schema=False)
@app.get("/projects/{pid}/{section}", include_in_schema=False)
def page(pid: str | None = None, section: str | None = None, bid: str | None = None, wid: str | None = None,
         tid: str | None = None):
    return FileResponse(INDEX, headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
app.mount("/app-fonts", StaticFiles(directory=render.FONT_PATH.parent), name="app-fonts")  # titles in the editor preview
app.mount("/style-assets", StaticFiles(directory=styles.ASSETS), name="style-assets")
app.mount("/playbook-assets", StaticFiles(directory=playbooks.ASSETS), name="playbook-assets")
