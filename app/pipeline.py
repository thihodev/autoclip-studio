"""Topic → Claude script → MoneyPrinterTurbo render → (optional) Postiz publish."""
import asyncio
import logging
import random
import time
from pathlib import Path

from . import claude, db, mpt, postiz
from .config import VIDEOS_DIR

log = logging.getLogger("pipeline")

# Render one video at a time; MPT is CPU-heavy.
_render_lock = asyncio.Semaphore(1)

ASPECTS = {"youtube": "16:9", "shorts": "9:16", "tiktok": "9:16", "square": "1:1"}

# edge-tts voices per language (MoneyPrinterTurbo format: "<voice>-<Gender>")
DEFAULT_VOICES = {
    "Vietnamese": "vi-VN-HoaiMyNeural-Female",
    "English": "en-US-AndrewMultilingualNeural-Male",
}


async def run_project(pid: str):
    p = db.get_project(pid)
    req = p["request"]
    try:
        # 1. Script with Claude
        db.update_project(pid, status="scripting", progress=5)
        meta = await claude.write_script(
            topic=req["topic"], language=req.get("language", "English"),
            duration=int(req.get("duration", 45)), style=req.get("style", ""),
            notes=req.get("notes", ""),
        )
        db.update_project(pid, script=meta, progress=15)

        # 2. Render with MoneyPrinterTurbo
        async with _render_lock:
            db.update_project(pid, status="rendering", progress=20)
            language = req.get("language", "English")
            params = {
                "video_subject": req["topic"],
                "video_script": meta["script"],
                "video_terms": meta["search_terms"],
                "video_aspect": ASPECTS.get(req.get("format", "shorts"), "9:16"),
                "video_language": "",
                "voice_name": req.get("voice") or DEFAULT_VOICES.get(language, DEFAULT_VOICES["English"]),
                "voice_rate": float(req.get("voice_rate", 1.0)),
                "video_source": req.get("video_source", "pexels"),
                "video_concat_mode": "sequential",
                "match_materials_to_script": True,
                "video_clip_duration": 4,
                "video_count": 1,
                "bgm_type": "random",
                "bgm_volume": 0.15,
                "subtitle_enabled": True,
                "subtitle_position": "custom",
                "custom_position": 70.0,
                "subtitle_display_mode": req.get("subtitle_mode", "word_by_word"),
                "font_size": 64,
                "stroke_width": 2.0,
                "n_threads": 4,
            }
            if req.get("font_name"):
                params["font_name"] = req["font_name"]
            task_id = await mpt.create_task(params)
            db.update_project(pid, mpt_task_id=task_id)
            result = await mpt.wait_for_task(
                task_id, on_progress=lambda pr: db.update_project(pid, progress=20 + int(pr * 0.7))
            )
            videos = result.get("videos") or result.get("combined_videos") or []
            if not videos:
                raise RuntimeError("Render finished but returned no video")
            dest = await mpt.download(videos[0], VIDEOS_DIR / f"{pid}.mp4")
        db.update_project(pid, status="ready", progress=100, video_path=str(dest))

        # 3. Auto publish
        if req.get("auto_publish") and req.get("integrations"):
            await publish_project(pid)
    except Exception as e:
        log.exception("project %s failed", pid)
        db.update_project(pid, status="failed", error=f"{e.__class__.__name__}: {e}"[:1000])


async def publish_project(pid: str, integrations: list[dict] | None = None, opts: dict | None = None):
    p = db.get_project(pid)
    req = p["request"]
    integrations = integrations or req.get("integrations") or []
    opts = {**req.get("publish_options", {}), **(opts or {})}
    if not integrations:
        raise ValueError("No channels selected")
    db.update_project(pid, status="publishing")
    try:
        res = await postiz.publish(Path(p["video_path"]), p["script"], integrations, opts)
        db.update_project(pid, status="published", publish_result=res, error=None)
    except Exception as e:
        log.exception("publish %s failed", pid)
        db.update_project(pid, status="ready", error=f"Publish failed: {e}"[:1000])
        raise


def start(pid: str):
    asyncio.get_running_loop().create_task(run_project(pid))


# ---------- Autopilot: niche → ideas → videos on a schedule ----------

async def autopilot_loop():
    while True:
        try:
            cfg = db.get_setting("autopilot", {})
            if cfg.get("enabled"):
                interval = max(1, float(cfg.get("every_hours", 24))) * 3600
                last = float(db.get_setting("autopilot_last_run", 0))
                if time.time() - last >= interval:
                    db.set_setting("autopilot_last_run", time.time())
                    await _autopilot_run(cfg)
        except Exception:
            log.exception("autopilot tick failed")
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
            "auto_publish": cfg.get("auto_publish", False),
            "integrations": cfg.get("integrations", []),
            "publish_options": cfg.get("publish_options", {}),
            "source": "autopilot",
        })
        await run_project(p["id"])
    db.set_setting("autopilot_used_topics", list(used)[-500:])
