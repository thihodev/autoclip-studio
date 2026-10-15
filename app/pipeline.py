"""Topic → Claude scenes → timeline → render → edit loop → publish."""
import asyncio
import json
import logging
import random
import time
import uuid
from pathlib import Path

from . import activity, brands, claude, context, db, framejam, motion, platforms, postiz, render, settings, usage
from .config import VIDEOS_DIR

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


def _save_version(pid: str, out: Path, tl: dict, note: str, model: str | None = None, effort: str | None = None) -> dict:
    p = db.get_project(pid)
    extra = _extra(p)
    version = (p.get("version") or 0) + 1
    extra["versions"].append({"version": version, "file": out.name, "note": note, "at": time.time(),
                              "model": model, "effort": effort})
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
    saved = _save_version(pid, out, tl, note, model, effort)
    if _wants_framejam(saved):
        try:
            await send_to_framejam(pid, note)
            activity.add(pid, "Sent to FrameJam for review", "done")
        except Exception:
            log.warning("could not push to FrameJam", exc_info=True)
    return saved


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


def is_motion(p: dict) -> bool:
    return (p.get("timeline") or {}).get("engine") == "motion" or p["request"].get("engine") == "motion"


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
                      status="ready", progress=100, error=None, publish_result=None)
    if _wants_framejam(db.get_project(pid)):
        try:
            await send_to_framejam(pid, note)
            activity.add(pid, "Sent to FrameJam for review", "done")
        except Exception:
            log.warning("could not push v%s to FrameJam", version, exc_info=True)
    return db.get_project(pid)


async def run_project(pid: str):
    activity.clear(pid)
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
            if req.get("auto_publish") and req.get("integrations"):
                await publish_project(pid)
        except Exception as e:
            _fail(pid, e, status="ready" if db.get_project(pid).get("video_path") else "failed")
        return
    try:
        if not p.get("timeline"):
            db.update_project(pid, status="scripting", progress=5)
            activity.add(pid, "Claude is writing the script")
            brand = brands.get(req.get("brand_id"))
            meta = await claude.write_script(
                topic=req["topic"], language=req.get("language", "English"),
                duration=int(req.get("duration", 45)), style=req.get("style", ""), notes=req.get("notes", ""),
                brand_context=brands.prompt_context(brand),
                source=source_material(req), project_id=pid, model=req.get("model"), effort=req.get("effort"),
            )
            db.update_project(pid, script=meta, timeline=render.new_timeline(meta, req, brand))
            activity.add(pid, f"Script ready: “{meta['title']}” — {len(meta['scenes'])} scenes", "say")
        await _render_version(pid, "First cut")
        if req.get("auto_publish") and req.get("integrations"):
            await publish_project(pid)
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
    if is_motion(p):
        try:
            extra = _extra(p)
            extra["history"].append({"at": time.time(), "source": source, "instruction": instruction[:2000],
                                     "summary": instruction.splitlines()[0][:160] if source == "prompt" else "Applied FrameJam comments"})
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


# ---------- FrameJam review loop ----------

def _wants_framejam(p: dict) -> bool:
    """Send this render for review? Once a video has a review, every version goes there;
    otherwise the choice made when creating it, else the Connections default."""
    if _extra(p)["framejam"].get("review_id"):
        return True
    choice = p["request"].get("framejam")
    return framejam.auto() if choice is None else bool(choice)


async def send_to_framejam(pid: str, note: str = "") -> dict:
    if not (await framejam.health())["ok"]:
        await framejam.start()
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
    spawn(pid, ai_edit(pid, instruction, source="framejam"))
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
            "auto_publish": cfg.get("auto_publish", False),
            "integrations": cfg.get("integrations", []),
            "publish_options": cfg.get("publish_options", {}),
            "brand_id": cfg.get("brand_id"),
            "engine": cfg.get("engine") or default_engine(),
            "source": "autopilot",
        })
        await asyncio.wait([spawn(p["id"], run_project(p["id"]))])  # a delete may cancel it
    db.set_setting("autopilot_used_topics", list(used)[-500:])
