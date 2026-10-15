"""FrameJam (https://github.com/moritzkremb/framejam, MIT) review loop.

FrameJam is a local review app: you click the frame that's wrong and leave a
comment. We talk to its MCP endpoint over plain JSON-RPC (streamable HTTP):
open_review / add_version to send renders, get_feedback to pull comments.
Start it with `npx -y framejam start` (Node 22+).
"""
import asyncio
import json
import logging
import shutil

import httpx

from . import settings

HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def base_url() -> str:
    return (settings.get("framejam")["url"] or "http://localhost:2400").rstrip("/")


def _parse(r: httpx.Response):
    if "event-stream" in r.headers.get("content-type", ""):
        for line in r.text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:])
        return None
    return r.json() if r.text else None


async def call(tool: str, args: dict, timeout: float = 60) -> dict:
    url = f"{base_url()}/mcp"
    async with httpx.AsyncClient(timeout=timeout) as c:
        r = await c.post(url, headers=HEADERS, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                       "clientInfo": {"name": "autoclip-studio", "version": "0.2"}},
        })
        r.raise_for_status()
        init = _parse(r)
        h = dict(HEADERS)
        if sid := r.headers.get("mcp-session-id"):
            h["Mcp-Session-Id"] = sid
        h["MCP-Protocol-Version"] = init["result"]["protocolVersion"]
        await c.post(url, headers=h, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        r = await c.post(url, headers=h, json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": tool, "arguments": args}})
        r.raise_for_status()
        res = _parse(r) or {}
    if "error" in res:
        raise RuntimeError(f"FrameJam {tool}: {res['error'].get('message')}")
    result = res.get("result", {})
    texts = [c["text"] for c in result.get("content", []) if c.get("type") == "text"]
    if result.get("isError"):
        raise RuntimeError(f"FrameJam {tool}: {' '.join(texts)[:300]}")
    joined = "\n".join(texts).strip()
    try:
        # FrameJam replies with a JSON object, sometimes followed by a markdown summary.
        data, end = json.JSONDecoder().raw_decode(joined)
        if isinstance(data, dict):
            if joined[end:].strip():
                data["markdown"] = joined[end:].strip()
            return data
    except json.JSONDecodeError:
        pass
    return {"text": joined}


async def health() -> dict:
    try:
        data = await call("list_reviews", {}, timeout=5)
        return {"ok": True, "detail": f"{base_url()} · {data.get('count', 0)} review(s)"}
    except Exception as e:
        return {"ok": False, "detail": f"{base_url()} not reachable — run: npx -y framejam start ({e.__class__.__name__})"}


_starting = asyncio.Lock()


async def start() -> dict:
    """Start FrameJam in the background (`npx -y framejam start`) if it isn't running."""
    async with _starting:
        if (h := await health())["ok"]:
            return h
        npx = shutil.which("npx")
        if not npx:
            return {"ok": False, "detail": "Node.js is needed to run FrameJam"}
        try:
            proc = await asyncio.create_subprocess_exec(
                npx, "-y", "framejam", "start", stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), 180)
        except Exception:
            logging.getLogger("framejam").warning("could not start FrameJam", exc_info=True)
        for _ in range(20):
            if (h := await health())["ok"]:
                return h
            await asyncio.sleep(1)
        return await health()


def auto() -> bool:
    return bool(settings.get("framejam").get("auto", True))


async def send_version(video_path: str, title: str, review_id: str | None, note: str) -> dict:
    """→ {reviewId, url, version}"""
    if review_id:
        try:
            return await call("add_version", {"reviewId": review_id, "videoPath": video_path, "note": note})
        except RuntimeError:
            pass  # review deleted in FrameJam → open a new one
    return await call("open_review", {"videoPath": video_path, "title": title, "note": note})


async def get_feedback(review_id: str) -> dict:
    return await call("get_feedback", {"reviewId": review_id}, timeout=30)
