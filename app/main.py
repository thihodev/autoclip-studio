import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import claude, db, mpt, pipeline, postiz
from .config import ROOT, VIDEOS_DIR

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    task = asyncio.create_task(pipeline.autopilot_loop())
    yield
    task.cancel()


app = FastAPI(title="AutoClip Studio", lifespan=lifespan)


class Channel(BaseModel):
    id: str
    provider: str
    name: str | None = None


class ProjectIn(BaseModel):
    topic: str = Field(min_length=3)
    language: str = "English"
    duration: int = Field(45, ge=10, le=600)
    format: str = "shorts"  # shorts | tiktok | youtube | square
    style: str = ""
    notes: str = ""
    voice: str = ""
    voice_rate: float = 1.0
    video_source: str = "pexels"
    subtitle_mode: str = "word_by_word"
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


@app.get("/api/status")
async def status():
    mpt_s, postiz_s = await asyncio.gather(mpt.health(), postiz.health())
    claude_s = await asyncio.to_thread(claude.auth_status)
    return {"claude": claude_s, "mpt": mpt_s, "postiz": postiz_s}


@app.get("/api/integrations")
async def integrations():
    if not postiz.configured():
        return []
    try:
        return await postiz.list_integrations()
    except Exception as e:
        raise HTTPException(502, f"Postiz: {e}")


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
    p = db.get_project(pid)
    if not p:
        raise HTTPException(404)
    return p


@app.delete("/api/projects/{pid}")
def delete_project(pid: str):
    p = db.get_project(pid)
    if p and p.get("video_path"):
        Path(p["video_path"]).unlink(missing_ok=True)
    db.delete_project(pid)
    return {"ok": True}


@app.post("/api/projects/{pid}/retry")
async def retry(pid: str):
    p = db.get_project(pid)
    if not p:
        raise HTTPException(404)
    db.update_project(pid, status="queued", progress=0, error=None)
    pipeline.start(pid)
    return {"ok": True}


@app.post("/api/projects/{pid}/publish")
async def publish(pid: str, body: PublishIn):
    p = db.get_project(pid)
    if not p or not p.get("video_path"):
        raise HTTPException(400, "Video not ready")
    try:
        await pipeline.publish_project(
            pid, [c.model_dump() for c in body.integrations], body.publish_options
        )
    except Exception as e:
        raise HTTPException(502, str(e))
    return db.get_project(pid)


@app.get("/api/autopilot")
def get_autopilot():
    return db.get_setting("autopilot", AutopilotIn().model_dump())


@app.put("/api/autopilot")
def put_autopilot(body: AutopilotIn):
    db.set_setting("autopilot", body.model_dump())
    return body


@app.get("/videos/{pid}.mp4")
def video(pid: str):
    path = VIDEOS_DIR / f"{pid}.mp4"
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type="video/mp4")


app.mount("/", StaticFiles(directory=ROOT / "static", html=True), name="static")
