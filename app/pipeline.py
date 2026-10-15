"""Topic → Claude scenes → timeline → render → edit loop → publish."""
import asyncio
import json
import logging
import random
import time
import uuid
from pathlib import Path

from . import claude, db, framejam, platforms, postiz, render
from .config import VIDEOS_DIR

log = logging.getLogger("pipeline")

# One render at a time; ffmpeg already uses all cores.
_render_lock = asyncio.Semaphore(1)


def _extra(p: dict) -> dict:
    return p.get("extra") or {"versions": [], "history": [], "framejam": {}}


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
                      status="ready", progress=100, error=None)
    if extra["framejam"].get("review_id"):
        try:
            await send_to_framejam(pid, note)
        except Exception:
            log.warning("could not push v%s to FrameJam", version, exc_info=True)
    return db.get_project(pid)


async def run_project(pid: str):
    p = db.get_project(pid)
    req = p["request"]
    try:
        if not p.get("timeline"):
            db.update_project(pid, status="scripting", progress=5)
            meta = await claude.write_script(
                topic=req["topic"], language=req.get("language", "English"),
                duration=int(req.get("duration", 45)), style=req.get("style", ""), notes=req.get("notes", ""),
            )
            db.update_project(pid, script=meta, timeline=render.new_timeline(meta, req))
        await _render_version(pid, "First cut")
        if req.get("auto_publish") and req.get("integrations"):
            await publish_project(pid)
    except Exception as e:
        _fail(pid, e)


def start(pid: str):
    asyncio.get_running_loop().create_task(run_project(pid))


# ---------- editing ----------

def apply_edit(tl: dict, meta: dict, edit: dict) -> tuple[dict, dict]:
    old = {s["id"]: s for s in tl["scenes"]}
    scenes = []
    for e in edit.get("scenes") or []:
        prev = old.get(e.get("id") or "")
        text = (e.get("text") or (prev or {}).get("text") or "").strip()
        if not text:
            continue
        search = e.get("search") or (prev or {}).get("search", "")
        keep_clip = prev and not e.get("new_footage") and search == prev["search"]
        scenes.append({
            "id": prev["id"] if prev else f"s{uuid.uuid4().hex[:6]}",
            "text": text, "search": search,
            "clip": prev.get("clip") if keep_clip else None,
            "clip_start": prev.get("clip_start", 0) if keep_clip else 0,
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


async def ai_edit(pid: str, instruction: str, source: str = "prompt"):
    p = db.get_project(pid)
    try:
        db.update_project(pid, status="editing", progress=0, error=None)
        edit = await claude.edit_timeline(
            p["timeline"], p["script"], p["request"].get("language", "English"), instruction
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
    old = {s["id"]: s for s in p["timeline"]["scenes"]}
    for s in timeline["scenes"]:
        s.setdefault("id", f"s{uuid.uuid4().hex[:6]}")
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


# ---------- FrameJam review loop ----------

async def send_to_framejam(pid: str, note: str = "") -> dict:
    p = db.get_project(pid)
    extra = _extra(p)
    fj = extra.get("framejam", {})
    res = await framejam.send_version(
        str(Path(p["video_path"]).resolve()), p["script"].get("title") or p["request"]["topic"][:60],
        fj.get("review_id"), note or f"Version {p['version']}",
    )
    extra["framejam"] = {"review_id": res.get("reviewId") or fj.get("review_id"),
                         "url": res.get("url") or fj.get("url"), "version": res.get("version")}
    db.update_project(pid, extra=extra)
    return extra["framejam"]


async def apply_framejam_feedback(pid: str) -> dict:
    p = db.get_project(pid)
    review_id = _extra(p).get("framejam", {}).get("review_id")
    if not review_id:
        raise ValueError("Open this video in FrameJam first")
    feedback = await framejam.get_feedback(review_id)
    if feedback.get("status") == "empty" or not feedback.get("comments"):
        return {"applied": False, "detail": "No comments in FrameJam yet — comment on the review page first"}
    comments = feedback.get("markdown") or json.dumps(feedback["comments"], ensure_ascii=False)
    instruction = (
        "Apply this reviewer feedback from FrameJam. Each comment has a timestamp (seconds) or range; "
        "map it to the scene whose start/end contains it.\n" + comments[:12000]
    )
    asyncio.get_running_loop().create_task(ai_edit(pid, instruction, source="framejam"))
    return {"applied": True}


# ---------- publishing ----------

async def publish_project(pid: str, channels: list[dict] | None = None, opts: dict | None = None):
    """channels: [{id, provider, name, via: native|postiz}]"""
    p = db.get_project(pid)
    req = p["request"]
    channels = channels or req.get("integrations") or []
    opts = {"format": p["timeline"]["format"], **req.get("publish_options", {}), **(opts or {})}
    if not channels:
        raise ValueError("No channels selected")
    db.update_project(pid, status="publishing")
    video = Path(p["video_path"])
    results, errors = [], []
    for ch in [c for c in channels if c.get("via", "native") == "native"]:
        account = db.get_account(ch["id"])
        try:
            if not account:
                raise RuntimeError("account was disconnected")
            res = await platforms.publish(account, video, p["script"], opts)
            results.append({"channel": ch, "ok": True, "result": res})
        except Exception as e:
            log.exception("publish to %s failed", ch)
            errors.append(f"{ch.get('provider')} {ch.get('name')}: {e}")
            results.append({"channel": ch, "ok": False, "error": str(e)[:500]})
    via_postiz = [c for c in channels if c.get("via") == "postiz"]
    if via_postiz:
        try:
            res = await postiz.publish(video, p["script"], via_postiz, opts)
            results.append({"channel": {"via": "postiz"}, "ok": True, "result": res})
        except Exception as e:
            errors.append(f"Postiz: {e}")
            results.append({"channel": {"via": "postiz"}, "ok": False, "error": str(e)[:500]})
    ok = any(r["ok"] for r in results)
    db.update_project(pid, status="published" if ok else "ready", publish_result=results,
                      error="; ".join(errors)[:1000] or None)
    if not ok:
        raise RuntimeError("; ".join(errors))


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
