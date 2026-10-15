"""Voice-over providers.

- ElevenLabs: natural voices in 70+ languages (incl. Vietnamese with v3/v4
  and Flash v2.5); /with-timestamps returns character timings we turn into
  word timings for captions.
- edge-tts: free fallback (Microsoft neural voices) with word boundaries.

synthesize() always returns word timings as [[start, end, word], ...].
"""
import base64
import re
from pathlib import Path

import edge_tts
import httpx

from . import settings

API = "https://api.elevenlabs.io/v1"
# Newest first; we fall back when an account can't use a model.
ELEVEN_MODELS = ["eleven_v4", "eleven_v3", "eleven_flash_v2_5"]
LANGUAGE_CODES = {"English": "en", "Vietnamese": "vi", "Spanish": "es", "Portuguese": "pt", "French": "fr",
                  "German": "de", "Italian": "it", "Indonesian": "id"}


def cfg() -> dict:
    return settings.get("voice")


def provider() -> str:
    return "elevenlabs" if cfg()["elevenlabs_api_key"] else "edge"


def status() -> dict:
    c = cfg()
    if c["elevenlabs_api_key"]:
        return {"ok": True, "provider": "elevenlabs", "detail": f"ElevenLabs · {c.get('elevenlabs_voice_name') or 'default voice'}"}
    return {"ok": False, "provider": "edge", "detail": "Free Microsoft voices (edge-tts)"}


def _headers(key: str | None = None) -> dict:
    return {"xi-api-key": key or cfg()["elevenlabs_api_key"]}


async def eleven_check(key: str) -> str | None:
    """None when the key works, else an error message."""
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(f"{API}/voices", headers=_headers(key), params={"page_size": 1})
    except httpx.HTTPError as e:
        return f"Could not reach ElevenLabs ({e.__class__.__name__})"
    if r.status_code in (401, 403):
        return "ElevenLabs rejected this API key"
    return None if r.status_code < 400 else f"ElevenLabs error {r.status_code}"


async def eleven_voices() -> list[dict]:
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(f"{API}/voices", headers=_headers())
    r.raise_for_status()
    out = []
    for v in r.json().get("voices", []):
        labels = v.get("labels") or {}
        out.append({"id": v["voice_id"], "name": v.get("name"), "category": v.get("category"),
                    "gender": labels.get("gender"), "accent": labels.get("accent"),
                    "description": labels.get("description") or labels.get("descriptive"),
                    "preview": v.get("preview_url")})
    return out


def _words_from_alignment(al: dict) -> list[list]:
    words, cur, start, end = [], "", None, None
    for ch, s, e in zip(al["characters"], al["character_start_times_seconds"], al["character_end_times_seconds"]):
        if ch.isspace():
            if cur:
                words.append([start, end, cur])
            cur, start = "", None
            continue
        if start is None:
            start = s
        cur += ch
        end = e
    if cur:
        words.append([start, end, cur])
    return words


async def _eleven(text: str, language: str, rate: float, dest: Path) -> list[list]:
    c = cfg()
    voice_id = c["elevenlabs_voice_id"]
    if not voice_id:
        voices = await eleven_voices()
        if not voices:
            raise RuntimeError("Your ElevenLabs account has no voices — add one in the ElevenLabs Voice Library")
        voice_id = voices[0]["id"]
    models = [c["elevenlabs_model"]] if c.get("elevenlabs_model") else ELEVEN_MODELS
    last = None
    async with httpx.AsyncClient(timeout=120) as h:
        for model in models:
            body = {"text": text, "model_id": model,
                    "voice_settings": {"stability": 0.45, "similarity_boost": 0.8, "style": 0.25,
                                       "speed": max(0.7, min(1.2, rate))}}
            if (code := LANGUAGE_CODES.get(language)) and model != "eleven_multilingual_v2":
                body["language_code"] = code
            r = await h.post(f"{API}/text-to-speech/{voice_id}/with-timestamps",
                             params={"output_format": "mp3_44100_128"}, headers=_headers(), json=body)
            if r.status_code == 200:
                data = r.json()
                dest.write_bytes(base64.b64decode(data["audio_base64"]))
                al = data.get("alignment") or data.get("normalized_alignment")
                return _words_from_alignment(al) if al else []
            last = f"ElevenLabs {r.status_code}: {r.text[:300]}"
            if r.status_code in (401, 403) and "model" not in r.text.lower():
                break  # bad key or quota — another model won't help
    raise RuntimeError(last or "ElevenLabs failed")


async def _edge(text: str, voice: str, rate: float, dest: Path) -> list[list]:
    pct = int(round((rate - 1.0) * 100))
    comm = edge_tts.Communicate(text, voice, rate=f"{pct:+d}%", boundary="WordBoundary")
    words = []
    with open(dest, "wb") as f:
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                start = chunk["offset"] / 1e7
                words.append([start, start + chunk["duration"] / 1e7, chunk["text"]])
    return words


async def synthesize(text: str, language: str, edge_voice: str, rate: float, dest: Path) -> list[list]:
    """Write an mp3 to `dest`; return word timings."""
    text = re.sub(r"\s+", " ", text).strip()
    if provider() == "elevenlabs":
        return await _eleven(text, language, rate, dest)
    return await _edge(text, edge_voice, rate, dest)


def cache_key() -> tuple:
    c = cfg()
    return (provider(), c.get("elevenlabs_voice_id"), c.get("elevenlabs_model"))
