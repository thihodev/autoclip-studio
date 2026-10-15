import asyncio
import logging
import secrets
import shutil
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import claude, db, framejam, pipeline, platforms, postiz, render, settings
from .config import ROOT, VIDEOS_DIR

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    task = asyncio.create_task(pipeline.autopilot_loop())
    yield
    task.cancel()


app = FastAPI(title="AutoClip Studio", lifespan=lifespan)


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


class TimelineIn(BaseModel):
    timeline: dict
    title: str | None = None


class InstructionIn(BaseModel):
    instruction: str = Field(min_length=2)


class CodeIn(BaseModel):
    code: str


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
def put_settings(section: str, value=Body(...)):
    if section not in settings.DEFAULTS:
        raise HTTPException(404)
    settings.put(section, value)
    return {"ok": True}


# ------------------------------------------------------------------ Claude connection

@app.get("/api/claude")
async def claude_info():
    current, system = await asyncio.gather(
        asyncio.to_thread(claude.auth_status), asyncio.to_thread(claude.system_status)
    )
    return {"current": current, "system": system, "login": claude.login_state()}


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
    settings.put("claude", {**settings.get("claude"), "source": body.source})
    return {"ok": True}


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
        return RedirectResponse(f"/#connections?error={platform}+app+credentials+missing")
    state = secrets.token_urlsafe(24)
    redirect_uri = f"{_public_base(request)}/oauth/{platform}/callback"
    _oauth_states[state] = {"platform": platform, "redirect_uri": redirect_uri, "at": time.time()}
    return RedirectResponse(p.auth_url(redirect_uri, state))


@app.get("/oauth/{platform}/callback")
async def oauth_callback(platform: str, request: Request):
    q = request.query_params
    st = _oauth_states.pop(q.get("state", ""), None)
    if not st or st["platform"] != platform or time.time() - st["at"] > 900:
        return RedirectResponse("/#connections?error=Sign-in+expired,+try+again")
    if q.get("error"):
        return RedirectResponse(f"/#connections?error={q.get('error_description') or q.get('error')}")
    try:
        accounts = await platforms.PLATFORMS[platform].exchange(q.get("code", ""), st["redirect_uri"])
        for a in accounts:
            db.upsert_account(platform, a["external_id"], a["name"], a.get("avatar"), a["data"])
    except Exception as e:
        logging.exception("oauth %s failed", platform)
        return RedirectResponse(f"/#connections?error={str(e)[:200]}")
    return RedirectResponse(f"/#connections?connected={platform}")


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


@app.get("/api/projects")
def projects():
    return db.list_projects()


@app.post("/api/projects")
async def create_project(body: ProjectIn):
    p = db.create_project(body.model_dump())
    pipeline.start(p["id"])
    return p


@app.get("/api/projects/{pid}")
def get_project(pid: str):
    return _project(pid)


@app.delete("/api/projects/{pid}")
def delete_project(pid: str):
    for f in VIDEOS_DIR.glob(f"{pid}_v*.mp4"):
        f.unlink(missing_ok=True)
    render.clear_cache(pid)
    db.delete_project(pid)
    return {"ok": True}


@app.post("/api/projects/{pid}/retry")
async def retry(pid: str):
    _project(pid)
    db.update_project(pid, status="queued", progress=0, error=None)
    pipeline.start(pid)
    return {"ok": True}


# ------------------------------------------------------------------ editor

@app.put("/api/projects/{pid}/timeline")
async def save_timeline(pid: str, body: TimelineIn):
    p = _project(pid)
    if not p.get("timeline"):
        raise HTTPException(400, "Video has no timeline yet")
    _bg(pipeline.manual_edit(pid, body.timeline, body.title))
    return {"ok": True}


@app.post("/api/projects/{pid}/ai-edit")
async def ai_edit(pid: str, body: InstructionIn):
    p = _project(pid)
    if not p.get("timeline"):
        raise HTTPException(400, "Video has no timeline yet")
    _bg(pipeline.ai_edit(pid, body.instruction))
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
    return db.get_setting("autopilot", AutopilotIn().model_dump())


@app.put("/api/autopilot")
def put_autopilot(body: AutopilotIn):
    db.set_setting("autopilot", body.model_dump())
    return body


# ------------------------------------------------------------------ files

@app.get("/videos/{name}")
def video(name: str):
    path = VIDEOS_DIR / Path(name).name
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type="video/mp4")


@app.get("/media/{pid}/{name}")
def media(pid: str, name: str):
    path = render.project_dir(Path(pid).name) / "media" / Path(name).name
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path)


app.mount("/", StaticFiles(directory=ROOT / "static", html=True), name="static")
