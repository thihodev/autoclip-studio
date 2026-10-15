"""Client for the Postiz public API (https://docs.postiz.com/public-api).

Postiz holds the OAuth connections to TikTok, YouTube, Facebook, Instagram,
LinkedIn, X, Threads, Pinterest... so this app never stores platform tokens.
"""
from datetime import datetime, timezone
from pathlib import Path

import httpx

from .config import POSTIZ_API_KEY, POSTIZ_URL

BASE = f"{POSTIZ_URL}/public/v1"


def _headers():
    return {"Authorization": POSTIZ_API_KEY}


def configured() -> bool:
    return bool(POSTIZ_API_KEY)


async def list_integrations() -> list[dict]:
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(f"{BASE}/integrations", headers=_headers())
    r.raise_for_status()
    items = r.json()
    return [
        {
            "id": i["id"],
            "name": i.get("name"),
            "provider": i.get("identifier") or i.get("providerIdentifier"),
            "picture": i.get("picture"),
            "disabled": i.get("disabled", False),
        }
        for i in items
    ]


async def health() -> dict:
    if not configured():
        return {"ok": False, "detail": "POSTIZ_API_KEY not set"}
    try:
        items = await list_integrations()
        return {"ok": True, "detail": f"{len(items)} channel(s) connected"}
    except Exception as e:
        return {"ok": False, "detail": f"{BASE} → {e.__class__.__name__}: {e}"[:300]}


async def upload(path: Path) -> dict:
    async with httpx.AsyncClient(timeout=None) as c:
        with open(path, "rb") as f:
            r = await c.post(
                f"{BASE}/upload", headers=_headers(), files={"file": (path.name, f, "video/mp4")}
            )
    r.raise_for_status()
    data = r.json()
    return {"id": data["id"], "path": data["path"]}


def provider_settings(provider: str, meta: dict, opts: dict) -> dict:
    title = meta.get("title", "")
    if provider == "youtube":
        return {
            "__type": "youtube",
            "title": title[:100] or "Untitled",
            "type": opts.get("youtube_visibility", "public"),
            "selfDeclaredMadeForKids": "no",
            "thumbnail": None,
            "tags": [{"value": t, "label": t} for t in meta.get("youtube_tags", [])[:15]],
        }
    if provider == "tiktok":
        return {
            "__type": "tiktok",
            "title": title[:90],
            "privacy_level": opts.get("tiktok_privacy", "PUBLIC_TO_EVERYONE"),
            "duet": False,
            "stitch": False,
            "comment": True,
            "autoAddMusic": "no",
            "brand_content_toggle": False,
            "brand_organic_toggle": False,
            "video_made_with_ai": True,
            "content_posting_method": opts.get("tiktok_method", "DIRECT_POST"),
        }
    if provider == "instagram" or provider == "instagram-standalone":
        return {"__type": provider, "post_type": "post", "collaborators": []}
    return {"__type": provider}


async def publish(video: Path, meta: dict, integrations: list[dict], opts: dict) -> dict:
    """integrations: [{id, provider}] as returned by list_integrations()."""
    media = await upload(video)
    caption = meta.get("description", "")
    tags = " ".join(f"#{h.lstrip('#')}" for h in meta.get("hashtags", []))
    content = f"{caption}\n\n{tags}".strip()
    when = opts.get("schedule_at")  # ISO string or None for now
    body = {
        "type": "schedule" if when else "now",
        "date": when or datetime.now(timezone.utc).isoformat(),
        "shortLink": False,
        "tags": [],
        "posts": [
            {
                "integration": {"id": i["id"]},
                "value": [{"content": content, "image": [media]}],
                "settings": provider_settings(i["provider"], meta, opts),
            }
            for i in integrations
        ],
    }
    async with httpx.AsyncClient(timeout=120) as c:
        r = await c.post(f"{BASE}/posts", headers=_headers(), json=body)
    if r.status_code >= 400:
        raise RuntimeError(f"Postiz {r.status_code}: {r.text[:500]}")
    return {"media": media, "response": r.json()}
