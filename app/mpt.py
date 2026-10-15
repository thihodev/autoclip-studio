"""Client for the MoneyPrinterTurbo REST API (https://github.com/harry0703/MoneyPrinterTurbo).

We hand MPT a finished script + search terms written by Claude, so MPT only does
stock footage (Pexels/Pixabay), TTS (edge-tts etc.), subtitles and compositing.
"""
import asyncio
from pathlib import Path

import httpx

from .config import MPT_API_KEY, MPT_URL

STATE_COMPLETE = 1
STATE_FAILED = -1


def _headers():
    return {"x-api-key": MPT_API_KEY} if MPT_API_KEY else {}


async def health() -> dict:
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(f"{MPT_URL}/api/v1/musics", headers=_headers())
        return {"ok": r.status_code == 200, "detail": f"{MPT_URL} → HTTP {r.status_code}"}
    except Exception as e:
        return {"ok": False, "detail": f"{MPT_URL} unreachable ({e.__class__.__name__})"}


async def create_task(params: dict) -> str:
    try:
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(f"{MPT_URL}/api/v1/videos", json=params, headers=_headers())
    except httpx.ConnectError:
        raise RuntimeError(f"MoneyPrinterTurbo is not running at {MPT_URL} (run ./scripts/setup-engines.sh)")
    r.raise_for_status()
    body = r.json()
    if body.get("status") != 200:
        raise RuntimeError(f"MoneyPrinterTurbo error: {body.get('message')}")
    return body["data"]["task_id"]


async def get_task(task_id: str) -> dict:
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(f"{MPT_URL}/api/v1/tasks/{task_id}", headers=_headers())
    r.raise_for_status()
    return r.json()["data"]


async def wait_for_task(task_id: str, on_progress=None, timeout: int = 3600) -> dict:
    elapsed = 0
    while elapsed < timeout:
        data = await get_task(task_id)
        if on_progress:
            on_progress(int(data.get("progress") or 0))
        state = data.get("state")
        if state == STATE_COMPLETE:
            return data
        if state == STATE_FAILED:
            raise RuntimeError(f"Render failed at {data.get('failed_stage')}: {data.get('error')}")
        await asyncio.sleep(5)
        elapsed += 5
    raise TimeoutError("Render timed out")


async def download(url: str, dest: Path) -> Path:
    if url.startswith("/"):
        url = MPT_URL + url
    async with httpx.AsyncClient(timeout=None) as c:
        async with c.stream("GET", url, headers=_headers()) as r:
            r.raise_for_status()
            with open(dest, "wb") as f:
                async for chunk in r.aiter_bytes():
                    f.write(chunk)
    return dest
