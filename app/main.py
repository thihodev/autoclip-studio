import asyncio
import logging
import secrets
import shutil
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlencode

from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import edge_tts

from . import activity, brands, claude, context, db, framejam, leftovers, motion, pipeline, platforms, postiz, render, settings, tts, usage
from .config import DATA_DIR, ROOT, VIDEOS_DIR

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    context.cleanup(_context_in_use())
    task = asyncio.create_task(pipeline.autopilot_loop())
    if framejam.auto() and shutil.which("npx"):
        asyncio.create_task(framejam.start())
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


def _project(pid: str) -> dict:
    p = db.get_project(pid)
    if not p:
        raise HTTPException(404, "Project not found")
    return p


# ------------------------------------------------------------------ models

class Channel(BaseModel):
    id: str
    provider: str
    name: str | None = None
    via: str = "native"  # native | postiz


class ProjectIn(BaseModel):
    topic: str = Field(min_length=3)
    language: str = "English"
    duration: int = Field(45, ge=10, le=600)
    format: str = "shorts"  # shorts | youtube | square
    style: str = ""
    notes: str = ""
    voice: str = ""
    voice_rate: float = 1.0
    auto_publish: bool = False
    integrations: list[Channel] = []
    publish_options: dict = {}
    brand_id: str | None = None
    context_ids: list[str] = []
    engine: str | None = None  # motion | classic (default: motion when available)
    workspace_id: str | None = None
    model: str | None = None   # Claude model for this video (default: Connections setting for the engine)
    effort: str | None = None  # low | medium | high | xhigh | max (default: Connections setting)
    framejam: bool | None = None  # send renders to FrameJam for review (default: Connections setting)
    github_repo: str | None = None  # owner/repo: Claude bases the video on it (README, description, latest release)


class PublishIn(BaseModel):
    integrations: list[Channel]
    publish_options: dict = {}


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
    auto_publish: bool = False
    integrations: list[Channel] = []
    publish_options: dict = {}
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

def _public_base(request: Request) -> str:
    return (settings.get("public_url") or str(request.base_url)).rstrip("/")


@app.get("/api/status")
async def status():
    claude_s, fj, pz = await asyncio.gather(
        asyncio.to_thread(claude.auth_status), framejam.health(), postiz.health()
    )
    accounts = db.list_accounts()
    return {
        "claude": claude_s,
        "footage": render.footage_status(),
        "voice": tts.status(),
        "motion": motion.status(),
        "framejam": fj,
        "postiz": pz,
        "accounts": {"ok": bool(accounts) or postiz.configured(), "detail": f"{len(accounts)} account(s)"},
    }


@app.get("/api/settings")
def get_settings(request: Request):
    base = _public_base(request)
    return {
        **settings.public(),
        "platforms": [p.info(f"{base}/oauth/{k}/callback") for k, p in platforms.PLATFORMS.items()],
    }


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


# ------------------------------------------------------------------ social accounts (OAuth)

_oauth_states: dict[str, dict] = {}


@app.get("/oauth/{platform}/start")
def oauth_start(platform: str, request: Request):
    p = platforms.PLATFORMS.get(platform)
    if not p:
        raise HTTPException(404)
    if not p.configured():
        return RedirectResponse("/connections?" + urlencode({"error": f"Add the {p.label} app credentials first"}))
    state = secrets.token_urlsafe(24)
    redirect_uri = f"{_public_base(request)}/oauth/{platform}/callback"
    _oauth_states[state] = {"platform": platform, "redirect_uri": redirect_uri, "at": time.time()}
    return RedirectResponse(p.auth_url(redirect_uri, state))


@app.get("/oauth/{platform}/callback")
async def oauth_callback(platform: str, request: Request):
    q = request.query_params
    st = _oauth_states.pop(q.get("state", ""), None)
    if not st or st["platform"] != platform or time.time() - st["at"] > 900:
        return RedirectResponse("/connections?" + urlencode({"error": "Sign-in expired, try again"}))
    if q.get("error"):
        return RedirectResponse("/connections?" + urlencode({"error": q.get("error_description") or q.get("error")}))
    try:
        accounts = await platforms.PLATFORMS[platform].exchange(q.get("code", ""), st["redirect_uri"])
        for a in accounts:
            db.upsert_account(platform, a["external_id"], a["name"], a.get("avatar"), a["data"])
    except Exception as e:
        logging.exception("oauth %s failed", platform)
        return RedirectResponse("/connections?" + urlencode({"error": str(e)[:200]}))
    return RedirectResponse("/connections?" + urlencode({"connected": platforms.PLATFORMS[platform].label}))


@app.get("/api/channels")
async def channels():
    out = [
        {"id": a["id"], "provider": a["platform"], "name": a["name"], "avatar": a.get("avatar"), "via": "native"}
        for a in db.list_accounts()
    ]
    if postiz.configured():
        try:
            out += [{**i, "via": "postiz"} for i in await postiz.list_integrations() if not i.get("disabled")]
        except Exception:
            logging.warning("Postiz unreachable", exc_info=True)
    return out


@app.delete("/api/accounts/{aid}")
def delete_account(aid: str):
    db.delete_account(aid)
    return {"ok": True}


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
COMPOSER_KEYS = {"language", "format", "duration", "style", "voice", "voice_rate", "engine", "model", "effort",
                 "framejam", "channelIds", "auto_publish", "brand_id"}


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


@app.post("/api/projects/{pid}/framejam")
async def framejam_open(pid: str):
    p = _project(pid)
    if not p.get("video_path"):
        raise HTTPException(400, "Render the video first")
    try:
        return await pipeline.send_to_framejam(pid)
    except Exception as e:
        raise HTTPException(502, f"FrameJam: {e}")


@app.post("/api/projects/{pid}/framejam/apply")
async def framejam_apply(pid: str):
    _project(pid)
    try:
        return await pipeline.apply_framejam_feedback(pid)
    except Exception as e:
        raise HTTPException(400, str(e))


@app.post("/api/projects/{pid}/publish")
async def publish(pid: str, body: PublishIn):
    p = _project(pid)
    if not p.get("video_path"):
        raise HTTPException(400, "Video not ready")
    try:
        await pipeline.publish_project(pid, [c.model_dump() for c in body.integrations], body.publish_options)
    except Exception as e:
        raise HTTPException(502, str(e))
    return db.get_project(pid)


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
        if body.auto_publish and not body.integrations:
            raise HTTPException(400, "Pick at least one account to auto-publish to")
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

WS_FIELDS = ("notes", "website", "brand_id", "language", "format")


def _workspace(wid: str) -> dict:
    w = db.get_workspace(wid)
    if not w:
        raise HTTPException(404, "Project not found")
    return {"id": w["id"], "name": w["name"], "created_at": w["created_at"],
            **{k: w["data"].get(k, "") for k in WS_FIELDS}, "context": w["data"].get("context", []),
            "videos": len(db.list_projects(limit=100000, workspace_id=wid))}


class WorkspaceIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    notes: str = ""
    website: str = ""
    brand_id: str | None = None
    language: str = ""
    format: str = ""


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
    db.save_workspace(wid, body.name.strip(), {**w["data"], **body.model_dump(exclude={"name"})}, w["created_at"])
    return _workspace(wid)


@app.delete("/api/workspaces/{wid}")
def delete_workspace(wid: str):
    w = db.get_workspace(wid)
    if w:
        db.delete_workspace(wid)
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
                         "motion": {"model": cfg["motion_model"], "effort": cfg["motion_effort"]}}}


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


@app.post("/api/framejam/start")
async def framejam_start():
    return await framejam.start()


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
    voice = render.DEFAULT_VOICES.get(body.language, render.DEFAULT_VOICES["English"])
    try:
        await tts.synthesize(body.text, body.language, voice, 1.0, out)
    except Exception as e:
        raise HTTPException(502, str(e)[:300])
    data = out.read_bytes()
    out.unlink(missing_ok=True)
    from fastapi import Response
    return Response(data, media_type="audio/mpeg")


# ------------------------------------------------------------------ voices

_voices_cache: list | None = None


@app.get("/api/voices")
async def voices(language: str = "English"):
    """Voices to pick for a video: the ElevenLabs account's voices when connected, else free edge-tts voices."""
    if tts.provider() == "elevenlabs":
        try:
            return [{"name": tts.ELEVEN_PREFIX + v["id"], "provider": "elevenlabs", "label": v["name"],
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
    return [
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
@app.get("/projects/{pid}", include_in_schema=False)
@app.get("/projects/{pid}/{section}", include_in_schema=False)
def page(pid: str | None = None, section: str | None = None, bid: str | None = None, wid: str | None = None):
    return FileResponse(INDEX, headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
