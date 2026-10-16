"""Direct publishing to YouTube, TikTok and Facebook Pages.

Each platform needs a developer app (client id/secret) entered in the
Connections tab; users then click "Connect" and go through the platform's
own OAuth consent screen. Tokens are stored per account in SQLite.
"""
import time
from pathlib import Path
from urllib.parse import urlencode

import httpx

from . import connect, db, settings

GRAPH = "https://graph.facebook.com/v25.0"


class Platform:
    key = ""
    label = ""
    cred_fields: list[tuple[str, str]] = []  # (settings key, label)
    docs = ""
    notes = ""

    def creds(self) -> dict:
        return settings.get("apps")[self.key]

    def configured(self) -> bool:
        return all(self.creds().get(k) for k, _ in self.cred_fields)

    def auth_url(self, redirect_uri: str, state: str) -> str: ...
    async def exchange(self, code: str, redirect_uri: str) -> list[dict]: ...
    async def publish(self, account: dict, video: Path, meta: dict, opts: dict) -> dict: ...

    def via_connect(self) -> bool:
        """Connect through AutoClip Connect (no developer app) — when it supports this platform and no own app is set."""
        return self.key in CONNECT_PLATFORMS and not self.configured() and connect.available(self.key)

    def info(self, redirect_uri: str) -> dict:
        return {
            "key": self.key, "label": self.label, "docs": self.docs, "notes": self.notes,
            "fields": [{"key": k, "label": label} for k, label in self.cred_fields],
            "configured": self.configured(), "redirect_uri": redirect_uri, "connect": self.via_connect(),
        }


CONNECT_PLATFORMS = {"tiktok"}  # what AutoClip Connect can sign in to


def _caption(meta: dict) -> str:
    tags = " ".join(f"#{h.lstrip('#')}" for h in meta.get("hashtags", []))
    return f"{meta.get('description', '')}\n\n{tags}".strip()


# ---------------------------------------------------------------- YouTube

class YouTube(Platform):
    key, label = "youtube", "YouTube"
    cred_fields = [("client_id", "OAuth client ID"), ("client_secret", "OAuth client secret")]
    docs = "https://developers.google.com/youtube/v3/guides/uploading_a_video"
    notes = ("Google Cloud Console → enable 'YouTube Data API v3' → OAuth consent screen → "
             "Credentials → OAuth client ID (Web application) → add the redirect URI below. "
             "Unverified apps upload as private until Google audits the app.")
    SCOPES = "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly"

    def auth_url(self, redirect_uri, state):
        return "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode({
            "client_id": self.creds()["client_id"], "redirect_uri": redirect_uri, "response_type": "code",
            "scope": self.SCOPES, "access_type": "offline", "prompt": "consent", "state": state,
        })

    async def _token(self, data: dict) -> dict:
        c = self.creds()
        async with httpx.AsyncClient(timeout=30) as h:
            r = await h.post("https://oauth2.googleapis.com/token",
                             data={**data, "client_id": c["client_id"], "client_secret": c["client_secret"]})
        if r.status_code >= 400:
            raise RuntimeError(f"Google token error: {r.text[:300]}")
        t = r.json()
        t["expires_at"] = time.time() + t.get("expires_in", 3600) - 60
        return t

    async def exchange(self, code, redirect_uri):
        tok = await self._token({"code": code, "grant_type": "authorization_code", "redirect_uri": redirect_uri})
        async with httpx.AsyncClient(timeout=30) as h:
            r = await h.get("https://www.googleapis.com/youtube/v3/channels",
                            params={"part": "snippet", "mine": "true"},
                            headers={"Authorization": f"Bearer {tok['access_token']}"})
        r.raise_for_status()
        items = r.json().get("items", [])
        if not items:
            raise RuntimeError("This Google account has no YouTube channel")
        ch = items[0]
        return [{"external_id": ch["id"], "name": ch["snippet"]["title"],
                 "avatar": ch["snippet"]["thumbnails"].get("default", {}).get("url"), "data": tok}]

    async def _access_token(self, account) -> str:
        data = account["data"]
        if time.time() >= data.get("expires_at", 0):
            fresh = await self._token({"refresh_token": data["refresh_token"], "grant_type": "refresh_token"})
            data = {**data, **fresh}
            db.update_account_data(account["id"], data)
        return data["access_token"]

    async def publish(self, account, video, meta, opts):
        token = await self._access_token(account)
        tags = meta.get("youtube_tags", [])[:15]
        title = (meta.get("title") or "Untitled")[:100]
        if opts.get("format", "shorts") != "youtube" and "#shorts" not in title.lower():
            title = title[:92] + " #Shorts"
        body = {
            "snippet": {"title": title, "description": _caption(meta)[:5000], "tags": tags, "categoryId": "22"},
            "status": {"privacyStatus": opts.get("youtube_visibility", "public"),
                       "selfDeclaredMadeForKids": False, "containsSyntheticMedia": True},
        }
        size = video.stat().st_size
        async with httpx.AsyncClient(timeout=None) as h:
            r = await h.post(
                "https://www.googleapis.com/upload/youtube/v3/videos",
                params={"uploadType": "resumable", "part": "snippet,status"},
                headers={"Authorization": f"Bearer {token}", "X-Upload-Content-Type": "video/mp4",
                         "X-Upload-Content-Length": str(size)},
                json=body,
            )
            if r.status_code >= 400:
                raise RuntimeError(f"YouTube: {r.text[:400]}")
            r = await h.put(r.headers["Location"], content=video.read_bytes(),
                            headers={"Authorization": f"Bearer {token}", "Content-Type": "video/mp4"})
        if r.status_code >= 400:
            raise RuntimeError(f"YouTube upload: {r.text[:400]}")
        vid = r.json()["id"]
        result = {"id": vid, "url": f"https://youtu.be/{vid}"}
        if opts.get("cover_path") and opts.get("format") == "youtube":
            # Custom thumbnails need a verified channel; the video is already up either way.
            async with httpx.AsyncClient(timeout=60) as h:
                t = await h.post("https://www.googleapis.com/upload/youtube/v3/thumbnails/set", params={"videoId": vid},
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "image/jpeg"},
                                 content=Path(opts["cover_path"]).read_bytes())
            if t.status_code >= 400:
                result["note"] = f"Thumbnail not set: {t.text[:200]}"
        return result


# ---------------------------------------------------------------- TikTok

class TikTok(Platform):
    key, label = "tiktok", "TikTok"
    cred_fields = [("client_key", "Client key"), ("client_secret", "Client secret")]
    docs = "https://developers.tiktok.com/doc/content-posting-api-get-started"
    notes = ("developers.tiktok.com → create an app → add Login Kit + Content Posting API (Direct Post) → "
             "scopes user.info.basic, video.publish, video.upload → add the redirect URI below. "
             "TikTok requires an HTTPS redirect URI: set a Public URL (e.g. a Cloudflare/ngrok tunnel). "
             "Unaudited apps can only post privately (SELF_ONLY).")
    API = "https://open.tiktokapis.com/v2"

    def auth_url(self, redirect_uri, state):
        return "https://www.tiktok.com/v2/auth/authorize/?" + urlencode({
            "client_key": self.creds()["client_key"], "redirect_uri": redirect_uri, "response_type": "code",
            "scope": "user.info.basic,video.publish,video.upload", "state": state,
        })

    async def _token(self, data: dict) -> dict:
        c = self.creds()
        async with httpx.AsyncClient(timeout=30) as h:
            r = await h.post(f"{self.API}/oauth/token/",
                             data={**data, "client_key": c["client_key"], "client_secret": c["client_secret"]},
                             headers={"Content-Type": "application/x-www-form-urlencoded"})
        t = r.json()
        if r.status_code >= 400 or "access_token" not in t:
            raise RuntimeError(f"TikTok token error: {str(t)[:300]}")
        t["expires_at"] = time.time() + t.get("expires_in", 86400) - 60
        return t

    async def exchange(self, code, redirect_uri):
        tok = await self._token({"code": code, "grant_type": "authorization_code", "redirect_uri": redirect_uri})
        return await self.accounts_for(tok)

    async def accounts_for(self, tok: dict) -> list[dict]:
        """The account a token belongs to (also for tokens that came through AutoClip Connect)."""
        tok.setdefault("expires_at", time.time() + tok.get("expires_in", 86400) - 60)
        async with httpx.AsyncClient(timeout=30) as h:
            r = await h.get(f"{self.API}/user/info/", params={"fields": "open_id,display_name,avatar_url"},
                            headers={"Authorization": f"Bearer {tok['access_token']}"})
        user = r.json().get("data", {}).get("user", {})
        return [{"external_id": tok.get("open_id") or user.get("open_id"),
                 "name": user.get("display_name") or "TikTok", "avatar": user.get("avatar_url"), "data": tok}]

    async def _access_token(self, account) -> str:
        data = account["data"]
        if time.time() >= data.get("expires_at", 0):
            if data.get("via") == "connect":  # signed in through AutoClip Connect: it holds the client secret
                fresh = await connect.refresh("tiktok", data["refresh_token"])
                fresh["expires_at"] = time.time() + fresh.get("expires_in", 86400) - 60
            else:
                fresh = await self._token({"refresh_token": data["refresh_token"], "grant_type": "refresh_token"})
            data = {**data, **fresh}
            db.update_account_data(account["id"], data)
        return data["access_token"]

    async def publish(self, account, video, meta, opts):
        token = await self._access_token(account)
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8"}
        size = video.stat().st_size
        # TikTok: chunks of 5-64 MB, the last one may be larger; small files go in one chunk.
        chunk = size if size <= 64 * 1024 * 1024 else 10 * 1024 * 1024
        count = max(1, size // chunk)
        source = {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk, "total_chunk_count": count}
        method = opts.get("tiktok_method", "UPLOAD")

        async with httpx.AsyncClient(timeout=None) as h:
            if method == "UPLOAD":
                r = await h.post(f"{self.API}/post/publish/inbox/video/init/", headers=headers,
                                 json={"source_info": source})
            else:
                info = (await h.post(f"{self.API}/post/publish/creator_info/query/", headers=headers)).json()
                allowed = info.get("data", {}).get("privacy_level_options") or ["SELF_ONLY"]
                privacy = opts.get("tiktok_privacy", "PUBLIC_TO_EVERYONE")
                if privacy not in allowed:
                    privacy = "SELF_ONLY" if "SELF_ONLY" in allowed else allowed[0]
                title = f"{meta.get('title', '')}\n{_caption(meta)}"[:2200]
                r = await h.post(f"{self.API}/post/publish/video/init/", headers=headers, json={
                    "post_info": {"title": title, "privacy_level": privacy, "disable_duet": False,
                                  "disable_comment": False, "disable_stitch": False, "is_aigc": True,
                                  **({"video_cover_timestamp_ms": int(opts["cover_t"] * 1000)}
                                     if opts.get("cover_t") is not None else {})},
                    "source_info": source,
                })
            body = r.json()
            if body.get("error", {}).get("code", "ok") != "ok":
                raise RuntimeError(f"TikTok: {body['error']}")
            upload_url, publish_id = body["data"]["upload_url"], body["data"]["publish_id"]

            with open(video, "rb") as f:
                for i in range(count):
                    start = i * chunk
                    end = size - 1 if i == count - 1 else start + chunk - 1
                    f.seek(start)
                    data = f.read(end - start + 1)
                    r = await h.put(upload_url, content=data, headers={
                        "Content-Type": "video/mp4", "Content-Length": str(len(data)),
                        "Content-Range": f"bytes {start}-{end}/{size}",
                    })
                    if r.status_code >= 400:
                        raise RuntimeError(f"TikTok upload chunk {i + 1}/{count}: {r.status_code} {r.text[:200]}")
        return {"publish_id": publish_id, "method": method}


# ---------------------------------------------------------------- Facebook Pages

class Facebook(Platform):
    key, label = "facebook", "Facebook Page"
    cred_fields = [("app_id", "App ID"), ("app_secret", "App secret")]
    docs = "https://developers.facebook.com/docs/video-api/guides/reels-publishing"
    notes = ("developers.facebook.com → create an app (type Business) → add Facebook Login for Business → "
             "add the redirect URI below to 'Valid OAuth Redirect URIs'. Permissions: pages_show_list, "
             "pages_read_engagement, pages_manage_posts. Every Page you manage is added as an account.")
    SCOPES = "pages_show_list,pages_read_engagement,pages_manage_posts"

    def auth_url(self, redirect_uri, state):
        return "https://www.facebook.com/v25.0/dialog/oauth?" + urlencode({
            "client_id": self.creds()["app_id"], "redirect_uri": redirect_uri, "state": state,
            "scope": self.SCOPES, "response_type": "code",
        })

    async def exchange(self, code, redirect_uri):
        c = self.creds()
        async with httpx.AsyncClient(timeout=30) as h:
            r = await h.get(f"{GRAPH}/oauth/access_token", params={
                "client_id": c["app_id"], "client_secret": c["app_secret"],
                "redirect_uri": redirect_uri, "code": code})
            short = r.json()
            if "access_token" not in short:
                raise RuntimeError(f"Facebook token error: {short}")
            r = await h.get(f"{GRAPH}/oauth/access_token", params={
                "grant_type": "fb_exchange_token", "client_id": c["app_id"], "client_secret": c["app_secret"],
                "fb_exchange_token": short["access_token"]})
            user_token = r.json().get("access_token", short["access_token"])
            r = await h.get(f"{GRAPH}/me/accounts", params={
                "fields": "id,name,access_token,picture{url}", "access_token": user_token})
            pages = r.json().get("data", [])
        if not pages:
            raise RuntimeError("No Facebook Pages found for this account (or permission not granted)")
        return [{"external_id": p["id"], "name": p["name"],
                 "avatar": (p.get("picture") or {}).get("data", {}).get("url"),
                 "data": {"page_token": p["access_token"]}} for p in pages]

    async def publish(self, account, video, meta, opts):
        page_id, token = account["external_id"], account["data"]["page_token"]
        size = video.stat().st_size
        async with httpx.AsyncClient(timeout=None) as h:
            if opts.get("format", "shorts") in ("shorts", "tiktok"):
                r = await h.post(f"{GRAPH}/{page_id}/video_reels",
                                 params={"upload_phase": "start", "access_token": token})
                start = r.json()
                if "video_id" not in start:
                    raise RuntimeError(f"Facebook: {start}")
                vid = start["video_id"]
                r = await h.post(f"https://rupload.facebook.com/video-upload/v25.0/{vid}",
                                 content=video.read_bytes(),
                                 headers={"Authorization": f"OAuth {token}", "offset": "0", "file_size": str(size)})
                if r.status_code >= 400:
                    raise RuntimeError(f"Facebook upload: {r.text[:300]}")
                r = await h.post(f"{GRAPH}/{page_id}/video_reels", params={
                    "upload_phase": "finish", "video_id": vid, "video_state": "PUBLISHED",
                    "description": _caption(meta), "access_token": token})
                if r.status_code >= 400 or not r.json().get("success", True):
                    raise RuntimeError(f"Facebook publish: {r.text[:300]}")
                return {"video_id": vid, "type": "reel"}
            with open(video, "rb") as f:
                r = await h.post(f"https://graph-video.facebook.com/v25.0/{page_id}/videos",
                                 data={"title": meta.get("title", ""), "description": _caption(meta),
                                       "access_token": token},
                                 files={"source": (video.name, f, "video/mp4")})
            body = r.json()
            if "id" not in body:
                raise RuntimeError(f"Facebook: {body}")
            return {"video_id": body["id"], "type": "video"}


PLATFORMS: dict[str, Platform] = {p.key: p for p in (YouTube(), TikTok(), Facebook())}


async def publish(account: dict, video: Path, meta: dict, opts: dict) -> dict:
    return await PLATFORMS[account["platform"]].publish(account, video, meta, opts)
