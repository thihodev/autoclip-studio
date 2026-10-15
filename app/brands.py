"""Brand kits: a design system for videos.

A brand kit holds identity (description, tone, audience, call to action),
look (colors, caption style, font, logo), bumpers (intro/outro), music and a
b-roll library. Kits are built by hand in the UI or imported from a GitHub
repository — either from an explicit `autoclip.brand.json` manifest, or by
letting Claude read the repo (README, CSS/Tailwind tokens) and pick assets.
"""
import json
import logging
import re
import shutil
import tempfile
import time
import uuid
import zipfile
from collections import Counter
from pathlib import Path

import httpx

from . import context, db, settings
from .config import DATA_DIR

log = logging.getLogger("brands")

BRANDS_DIR = DATA_DIR / "brands"
MANIFEST_NAMES = ("autoclip.brand.json", ".autoclip/brand.json", "brand/autoclip.brand.json")
SKIP_DIRS = {".git", "node_modules", "vendor", "dist", "build", ".next", "venv", ".venv", "__pycache__", "coverage"}

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp"}
FONT_EXT = {".ttf", ".otf"}
VIDEO_EXT = {".mp4", ".mov", ".webm", ".m4v"}
AUDIO_EXT = {".mp3", ".wav", ".m4a", ".ogg", ".aac"}
KIND_EXT = {"logo": IMAGE_EXT, "font": FONT_EXT, "intro": VIDEO_EXT, "outro": VIDEO_EXT,
            "music": AUDIO_EXT, "clip": VIDEO_EXT, "context": context.ALLOWED}

MAX_ZIP = 400 * 1024 * 1024
MAX_FILES = 20000

EMPTY = {
    "description": "", "voice": "", "audience": "", "cta": "",
    "language": "", "voice_name": "",
    "colors": {"primary": "#E8385A", "accent": "#FFE14D", "text": "#FFFFFF"},
    "captions": {"preset": "bold", "position": 0.68},
    "logo": None,      # {file, position, size, opacity}
    "font": None,      # {file, family}
    "intro": None,     # {file, duration, thumb}
    "outro": None,
    "music": [],       # [{file, name, volume}]
    "clips": [],       # [{id, file, description, duration, thumb}]
    "knowledge": [],   # context files: [{id, name, chars}]
    "site": None,      # website capture: {url, domain, title, fonts, images: [{file, description}]}
    "source": {"type": "manual"},
    "warnings": [],
}


# ---------------------------------------------------------------- storage

def brand_dir(bid: str) -> Path:
    d = BRANDS_DIR / Path(bid).name
    d.mkdir(parents=True, exist_ok=True)
    return d


def _merge(data: dict | None) -> dict:
    out = json.loads(json.dumps(EMPTY))
    for k, v in (data or {}).items():
        if isinstance(out.get(k), dict) and isinstance(v, dict):
            out[k].update(v)
        else:
            out[k] = v
    return out


def get(bid: str | None) -> dict | None:
    if not bid:
        return None
    row = db.get_brand(bid)
    if not row:
        return None
    return {"id": row["id"], "name": row["name"], "created_at": row["created_at"],
            "updated_at": row["updated_at"], **_merge(row["data"])}


def list_all() -> list[dict]:
    return [get(r["id"]) for r in db.list_brands()]


def create(name: str, data: dict | None = None) -> dict:
    bid = uuid.uuid4().hex[:10]
    db.save_brand(bid, name.strip() or "Untitled brand", _merge(data))
    brand_dir(bid)
    return get(bid)


def update(bid: str, patch: dict) -> dict:
    b = get(bid)
    if not b:
        raise KeyError(bid)
    name = (patch.get("name") or b["name"]).strip()
    editable = ("description", "voice", "audience", "cta", "language", "voice_name", "colors", "captions")
    data = {k: b[k] for k in EMPTY}
    for k in editable:
        if k in patch:
            data[k] = {**data[k], **patch[k]} if isinstance(data[k], dict) and isinstance(patch[k], dict) else patch[k]
    # asset settings (not the files themselves)
    if isinstance(patch.get("logo"), dict) and data["logo"]:
        data["logo"].update({k: patch["logo"][k] for k in ("position", "size", "opacity") if k in patch["logo"]})
    for i, m in enumerate(patch.get("music") or []):
        if i < len(data["music"]) and "volume" in m:
            data["music"][i]["volume"] = max(0.0, min(1.0, float(m["volume"])))
    by_id = {c["id"]: c for c in patch.get("clips") or [] if c.get("id")}
    for c in data["clips"]:
        if c["id"] in by_id and "description" in by_id[c["id"]]:
            c["description"] = by_id[c["id"]]["description"]
    db.save_brand(bid, name, data)
    return get(bid)


def delete(bid: str):
    b = get(bid)
    for k in (b or {}).get("knowledge", []):
        context.remove(k["id"])
    db.delete_brand(bid)
    shutil.rmtree(BRANDS_DIR / Path(bid).name, ignore_errors=True)


def asset_path(bid: str, file: str) -> Path:
    if file.startswith("site/"):  # website visuals live in a subfolder
        return brand_dir(bid) / "site" / Path(file).name
    return brand_dir(bid) / Path(file).name


def font_family(path: Path) -> str | None:
    try:
        from PIL import ImageFont

        return ImageFont.truetype(str(path), 20).getname()[0]
    except Exception:
        return None


async def add_asset(bid: str, kind: str, src: Path, original_name: str, description: str = "") -> dict:
    """Copy a file into the kit as `kind` (logo|font|intro|outro|music|clip)."""
    from . import render  # local import: render imports brands

    b = get(bid)
    if not b:
        raise KeyError(bid)
    if kind == "context":
        meta = context.save(src, original_name)
        data = {k: b[k] for k in EMPTY}
        data["knowledge"].append(meta)
        db.save_brand(bid, b["name"], data)
        return get(bid)
    ext = Path(original_name).suffix.lower()
    if ext not in KIND_EXT[kind]:
        hint = {"logo": "PNG, JPG or WebP (SVG isn't supported — export a PNG)", "font": "TTF or OTF",
                "music": "MP3, WAV, M4A or OGG"}.get(kind, "MP4, MOV or WebM")
        raise ValueError(f"Unsupported {kind} file '{original_name}'. Use {hint}.")
    data = {k: b[k] for k in EMPTY}
    stem = re.sub(r"[^A-Za-z0-9_-]+", "-", Path(original_name).stem)[:40] or kind
    name = f"{kind}_{stem}_{uuid.uuid4().hex[:6]}{ext}"
    dest = asset_path(bid, name)
    shutil.copyfile(src, dest)

    if kind == "logo":
        _remove_file(bid, data["logo"])
        data["logo"] = {"file": name, "position": (data["logo"] or {}).get("position", "top-right"),
                        "size": (data["logo"] or {}).get("size", 0.16), "opacity": (data["logo"] or {}).get("opacity", 0.9)}
    elif kind == "font":
        family = font_family(dest)
        if not family:
            dest.unlink(missing_ok=True)
            raise ValueError("Could not read this font file")
        _remove_file(bid, data["font"])
        data["font"] = {"file": name, "family": family}
    elif kind in ("intro", "outro"):
        dur = await render.media_duration(dest)
        if dur > 30:
            dest.unlink(missing_ok=True)
            raise ValueError(f"{kind.title()} must be 30 seconds or shorter")
        _remove_file(bid, data[kind])
        data[kind] = {"file": name, "duration": round(dur, 2), "thumb": (await render.thumbnail(dest)).name}
    elif kind == "music":
        data["music"].append({"file": name, "name": Path(original_name).stem[:60], "volume": 0.12})
    elif kind == "clip":
        dur = await render.media_duration(dest)
        data["clips"].append({"id": f"b{uuid.uuid4().hex[:6]}", "file": name,
                              "description": description or Path(original_name).stem.replace("-", " ").replace("_", " "),
                              "duration": round(dur, 2), "thumb": (await render.thumbnail(dest)).name})
    db.save_brand(bid, b["name"], data)
    return get(bid)


def _remove_file(bid: str, item: dict | None):
    if item and item.get("file"):
        asset_path(bid, item["file"]).unlink(missing_ok=True)
        if item.get("thumb"):
            asset_path(bid, item["thumb"]).unlink(missing_ok=True)


def remove_asset(bid: str, kind: str, file: str | None = None) -> dict:
    b = get(bid)
    if not b:
        raise KeyError(bid)
    data = {k: b[k] for k in EMPTY}
    if kind == "context":
        context.remove(file or "")
        data["knowledge"] = [k for k in data["knowledge"] if k["id"] != file]
    elif kind in ("logo", "font", "intro", "outro"):
        _remove_file(bid, data[kind])
        data[kind] = None
    elif kind in ("music", "clip"):
        key = "music" if kind == "music" else "clips"
        keep = []
        for item in data[key]:
            if item["file"] == file:
                _remove_file(bid, item)
            else:
                keep.append(item)
        data[key] = keep
    db.save_brand(bid, b["name"], data)
    return get(bid)


def clip_for_scene(brand: dict | None, clip_id: str | None) -> dict | None:
    """A brand b-roll clip as a timeline clip."""
    if not brand or not clip_id:
        return None
    for c in brand["clips"]:
        if c["id"] == clip_id:
            return {"id": f"brand:{brand['id']}:{c['id']}", "provider": "brand", "path": str(asset_path(brand["id"], c["file"])),
                    "thumb_url": f"/brand-media/{brand['id']}/{c['thumb']}", "duration": c.get("duration", 0),
                    "description": c.get("description", "")}
    return None


def prompt_context(brand: dict | None) -> str:
    """Brand guidelines for Claude."""
    if not brand:
        return ""
    lines = [f"Brand: {brand['name']}" + (f" — {brand['description']}" if brand["description"] else "")]
    if (brand.get("site") or {}).get("domain"):
        lines.append(f"Website: {brand['site']['domain']}")
    if brand["voice"]:
        lines.append(f"Voice & tone: {brand['voice']}")
    if brand["audience"]:
        lines.append(f"Audience: {brand['audience']}")
    if brand["cta"]:
        lines.append(f"End the video with this call to action (adapt it to the video language): {brand['cta']}")
    text = "Brand guidelines — follow them:\n- " + "\n- ".join(lines)
    if brand["clips"]:
        text += ("\n\nBrand footage you can use instead of stock footage. When a scene fits one, set its "
                 "\"asset\" to the id:\n" + "\n".join(f"- {c['id']}: {c['description']}" for c in brand["clips"]))
    knowledge = context.prompt_block([k["id"] for k in brand["knowledge"]],
                                     "Brand knowledge (facts about the product — stay consistent with them)", 15_000)
    if knowledge:
        text += "\n\n" + knowledge
    return text


# ---------------------------------------------------------------- GitHub import

def github_token() -> str:
    return settings.get("github")["token"]


def _gh_headers(token: str | None = None) -> dict:
    h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token := token if token is not None else github_token():
        h["Authorization"] = f"Bearer {token}"
    return h


async def github_user(token: str) -> dict:
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get("https://api.github.com/user", headers=_gh_headers(token))
    if r.status_code == 401:
        raise ValueError("GitHub rejected this token")
    r.raise_for_status()
    u = r.json()
    return {"login": u["login"], "name": u.get("name"), "avatar": u.get("avatar_url")}


async def github_repos() -> list[dict]:
    if not github_token():
        return []
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get("https://api.github.com/user/repos", params={"per_page": 100, "sort": "updated"},
                        headers=_gh_headers())
    r.raise_for_status()
    return [{"full_name": x["full_name"], "private": x["private"], "description": x.get("description"),
             "default_branch": x.get("default_branch")} for x in r.json()]


async def github_material(repo: str) -> dict:
    """What a video about a repository needs: description, homepage, topics, the README and the latest release."""
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
        r = await c.get(f"https://api.github.com/repos/{repo}", headers=_gh_headers())
        if r.status_code == 404:
            raise ValueError(f"Repository {repo} not found — for a private repo, connect GitHub in Connections first")
        r.raise_for_status()
        info = r.json()
        readme = await c.get(f"https://api.github.com/repos/{repo}/readme",
                             headers={**_gh_headers(), "Accept": "application/vnd.github.raw+json"})
        release = await c.get(f"https://api.github.com/repos/{repo}/releases/latest", headers=_gh_headers())
    lines = [f"# {info['full_name']}", "", f"GitHub: {info.get('html_url')}"]
    for label, value in [("Description", info.get("description")), ("Website", info.get("homepage")),
                         ("Topics", ", ".join(info.get("topics") or [])), ("Main language", info.get("language")),
                         ("License", (info.get("license") or {}).get("name")),
                         ("Stars", info.get("stargazers_count")), ("Forks", info.get("forks_count"))]:
        if value not in (None, ""):
            lines.append(f"{label}: {value}")
    if release.status_code == 200:
        rel = release.json()
        lines += ["", f"## Latest release: {rel.get('name') or rel.get('tag_name')} ({(rel.get('published_at') or '')[:10]})",
                  (rel.get("body") or "")[:6000]]
    if readme.status_code == 200 and readme.text.strip():
        lines += ["", "## README", readme.text[:40000]]
    return {"text": "\n".join(lines).strip(), "homepage": info.get("homepage") or "", "name": info.get("name") or repo}


def find_by_repo(repo: str) -> dict | None:
    """A brand kit already imported from this repository."""
    for b in list_all():
        src = b.get("source") or {}
        if src.get("type") == "github" and (src.get("repo") or "").lower() == repo.lower():
            return b
    return None


def parse_repo(url: str) -> str:
    url = url.strip().removesuffix(".git").rstrip("/")
    m = re.search(r"(?:github\.com[/:])?([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)$", url)
    if not m:
        raise ValueError("Enter a GitHub repository like owner/repo or https://github.com/owner/repo")
    return f"{m.group(1)}/{m.group(2)}"


async def _download_repo(repo: str, ref: str | None, dest: Path) -> dict:
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as c:
        r = await c.get(f"https://api.github.com/repos/{repo}", headers=_gh_headers())
        if r.status_code == 404:
            raise ValueError(f"Repository {repo} not found — for a private repo, connect GitHub in Connections first")
        r.raise_for_status()
        info = r.json()
        ref = ref or info.get("default_branch") or "main"
        zpath = dest / "repo.zip"
        size = 0
        async with c.stream("GET", f"https://api.github.com/repos/{repo}/zipball/{ref}", headers=_gh_headers()) as resp:
            if resp.status_code == 404:
                raise ValueError(f"Branch or tag '{ref}' not found in {repo}")
            resp.raise_for_status()
            with open(zpath, "wb") as f:
                async for chunk in resp.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_ZIP:
                        raise ValueError("Repository is too large to import (over 400 MB)")
                    f.write(chunk)
    root = dest / "src"
    with zipfile.ZipFile(zpath) as z:
        names = z.namelist()
        if len(names) > MAX_FILES:
            raise ValueError("Repository has too many files to import")
        for n in names:
            parts = Path(n).parts[1:]  # strip the "owner-repo-sha/" folder
            if not parts or n.endswith("/") or ".." in parts or any(p in SKIP_DIRS for p in parts):
                continue
            target = root.joinpath(*parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(n) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
    zpath.unlink()
    return {"root": root, "ref": ref, "description": info.get("description") or "", "name": info.get("name") or repo}


def _read(path: Path, limit: int) -> str:
    try:
        return path.read_text(errors="replace")[:limit]
    except Exception:
        return ""


def _scan(root: Path) -> dict:
    files = [p for p in root.rglob("*") if p.is_file()]
    rel = lambda p: str(p.relative_to(root))  # noqa: E731
    by_ext = lambda exts, limit, cond=lambda p: True: [rel(p) for p in files if p.suffix.lower() in exts and cond(p)][:limit]  # noqa: E731
    small = lambda mb: (lambda p: p.stat().st_size <= mb * 1024 * 1024)  # noqa: E731
    logo_words = re.compile(r"logo|brand|wordmark|logotype|mark|icon|favicon", re.I)
    cands = {
        "logos": by_ext(IMAGE_EXT, 40, lambda p: small(5)(p) and bool(logo_words.search(rel(p)))),
        "svg_logos": by_ext({".svg"}, 10, lambda p: bool(logo_words.search(rel(p)))),
        "fonts": by_ext(FONT_EXT, 20, small(10)),
        "web_fonts": by_ext({".woff", ".woff2"}, 5),
        "videos": by_ext(VIDEO_EXT, 30, small(300)),
        "audio": by_ext(AUDIO_EXT, 15, small(50)),
    }
    text = {}
    for name in ("README.md", "readme.md", "README.MD", "README"):
        if (root / name).exists():
            text["README"] = _read(root / name, 6000)
            break
    if (root / "package.json").exists():
        try:
            pkg = json.loads(_read(root / "package.json", 200000))
            text["package.json"] = json.dumps({k: pkg.get(k) for k in ("name", "description", "homepage", "keywords")})
        except Exception:
            pass
    styles = [p for p in files if p.name.startswith("tailwind.config")]
    styles += [p for p in files if p.suffix in (".css", ".scss") and small(1)(p) and re.search(r"--[\w-]*colou?r|:root", _read(p, 20000))][:4]
    styles += [p for p in files if re.search(r"tokens?|theme|design", p.name, re.I) and p.suffix == ".json" and small(1)(p)][:2]
    for p in styles[:6]:
        text[rel(p)] = _read(p, 3000)
    hexes = Counter(h.upper() for p in styles for h in re.findall(r"#[0-9a-fA-F]{6}\b", _read(p, 50000)))
    cands["colors_found"] = [h for h, _ in hexes.most_common(12)]
    return {"candidates": cands, "text": text}


ANALYZE_PROMPT = """You are setting up a video "brand kit" (a design system for short videos) from a code repository.

Repository: {repo}
Repository description: {description}

Files that may describe the brand:
{texts}

Candidate asset files (paths relative to the repo):
{candidates}

Return JSON with exactly these keys (use null when nothing fits; only choose paths from the candidates):
{{
  "name": "brand or product name",
  "description": "one or two sentences: what it is and for whom",
  "voice": "tone of voice guidance for video scripts",
  "audience": "who the videos are for",
  "cta": "short call to action for the end of videos (mention the website if known)",
  "colors": {{"primary": "#RRGGBB", "accent": "#RRGGBB (a bright highlight color)", "text": "#FFFFFF or another very light color for captions"}},
  "captions": {{"preset": "bold | box | minimal"}},
  "logo": "path to the best logo image (prefer a horizontal logo with transparency) or null",
  "font": "path to a bold display font file or null",
  "intro": "path to an intro video or null",
  "outro": "path to an outro video or null",
  "music": ["paths to background music tracks"],
  "clips": [{{"path": "path to a b-roll / demo video", "description": "what it shows"}}]
}}"""


async def _analyze_with_claude(repo: str, scan: dict, description: str) -> dict:
    from . import claude

    texts = "\n\n".join(f"--- {k} ---\n{v}" for k, v in scan["text"].items()) or "(none)"
    cands = json.dumps(scan["candidates"], indent=1)
    return await claude.ask_json(
        ANALYZE_PROMPT.format(repo=repo, description=description or "(none)", texts=texts, candidates=cands),
        system="You are a brand designer. Reply with ONE JSON object only, no markdown fences, no commentary.",
        kind="brand-import",
    )


def _heuristic(repo: str, scan: dict, description: str) -> dict:
    c = scan["candidates"]
    colors = c["colors_found"]
    light = [h for h in colors if sum(int(h[i:i + 2], 16) for i in (1, 3, 5)) > 600]
    vivid = [h for h in colors if h not in light]
    return {
        "name": repo.split("/")[1].replace("-", " ").title(), "description": description,
        "colors": {"primary": vivid[0] if vivid else "#E8385A", "accent": vivid[1] if len(vivid) > 1 else "#FFE14D",
                   "text": "#FFFFFF"},
        "logo": next((p for p in c["logos"] if p.lower().endswith(".png")), c["logos"][0] if c["logos"] else None),
        "font": c["fonts"][0] if c["fonts"] else None,
        "music": c["audio"][:1],
        "clips": [{"path": p, "description": Path(p).stem.replace("-", " ").replace("_", " ")} for p in c["videos"][:10]],
    }


def _load_manifest(root: Path) -> dict | None:
    for name in MANIFEST_NAMES:
        p = root / name
        if p.exists():
            try:
                return json.loads(p.read_text())
            except json.JSONDecodeError as e:
                raise ValueError(f"{name} is not valid JSON: {e}")
    return None


async def _apply_manifest(bid: str, root: Path, m: dict) -> list[str]:
    """Copy the manifest's assets into the kit. Returns warnings."""
    warnings = []

    def resolve(p):
        if not p:
            return None
        p = p["path"] if isinstance(p, dict) else p
        full = (root / p).resolve()
        if root.resolve() not in full.parents or not full.is_file():
            warnings.append(f"Asset not found in the repo: {p}")
            return None
        return full

    async def add(kind, ref, **kw):
        if (src := resolve(ref)) is None:
            return
        try:
            await add_asset(bid, kind, src, src.name, **kw)
        except ValueError as e:
            warnings.append(str(e))

    await add("logo", m.get("logo"))
    if isinstance(m.get("logo"), dict) and get(bid)["logo"]:
        update(bid, {"logo": m["logo"]})
    await add("font", m.get("font"))
    await add("intro", m.get("intro"))
    await add("outro", m.get("outro"))
    for track in m.get("music") or []:
        await add("music", track)
        if isinstance(track, dict) and "volume" in track:
            b = get(bid)
            update(bid, {"music": [*[{} for _ in b["music"][:-1]], {"volume": track["volume"]}]})
    for clip in m.get("clips") or []:
        await add("clip", clip, description=clip.get("description", "") if isinstance(clip, dict) else "")
    for doc in m.get("context") or []:
        await add("context", doc)
    return warnings


async def import_github(url: str, ref: str | None = None, bid: str | None = None) -> dict:
    """Create (or re-sync, when bid is given) a brand kit from a GitHub repo."""
    from . import claude

    repo = parse_repo(url)
    tmp = Path(tempfile.mkdtemp(dir=DATA_DIR))
    try:
        dl = await _download_repo(repo, ref, tmp)
        root = dl["root"]
        manifest = _load_manifest(root)
        mode = "manifest"
        warnings = []
        if manifest is None:
            scan = _scan(root)
            if settings.get("claude")["source"] and claude.auth_status()["ok"]:
                manifest, mode = await _analyze_with_claude(repo, scan, dl["description"]), "claude"
            else:
                manifest, mode = _heuristic(repo, scan, dl["description"]), "heuristic"
                warnings.append("Claude isn't connected, so the kit was guessed from file names — review it.")
            if scan["candidates"]["svg_logos"] and not scan["candidates"]["logos"]:
                warnings.append("The repo only has SVG logos — upload a PNG version of the logo.")
            if scan["candidates"]["web_fonts"] and not scan["candidates"]["fonts"]:
                warnings.append("Only web fonts (WOFF) were found — upload a TTF or OTF to use the brand font.")
            readme = next((n for n in ("README.md", "readme.md", "README.MD") if (root / n).exists()), None)
            if readme:
                manifest["context"] = [readme]  # the README becomes brand knowledge

        identity = {k: manifest.get(k) for k in ("description", "voice", "audience", "cta", "language", "voice_name")
                    if manifest.get(k)}
        if isinstance(manifest.get("colors"), dict):
            identity["colors"] = {k: v for k, v in manifest["colors"].items() if re.fullmatch(r"#[0-9A-Fa-f]{6}", str(v))}
        if isinstance(manifest.get("captions"), dict):
            identity["captions"] = manifest["captions"]
        name = manifest.get("name") or dl["name"]

        if bid:  # re-sync: drop old assets, keep the id
            b = get(bid)
            for kind in ("logo", "font", "intro", "outro"):
                remove_asset(bid, kind)
            for m in b["music"]:
                remove_asset(bid, "music", m["file"])
            for c in b["clips"]:
                remove_asset(bid, "clip", c["file"])
            for k in b["knowledge"]:
                remove_asset(bid, "context", k["id"])
            update(bid, {"name": name, **identity})
        else:
            bid = create(name, identity)["id"]

        warnings += await _apply_manifest(bid, root, manifest)
        b = get(bid)
        data = {k: b[k] for k in EMPTY}
        data["source"] = {"type": "github", "repo": repo, "ref": dl["ref"], "mode": mode, "imported_at": time.time()}
        data["warnings"] = warnings
        db.save_brand(bid, b["name"], data)
        return get(bid)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- website import

DOMAIN_RE = re.compile(
    r"(?<![@\w.-])(?:https?://)?((?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+(?:com\.vn|com|net|org|io|ai|app|co|dev|vn|edu|"
    r"gov|xyz|me|tv|store|shop|tech|so|gg|studio|design|cloud|site|online|info|biz|us|uk|de|fr|jp|sg|id|in))"
    r"(?:/[^\s,;)]*)?", re.I)


def find_domain(text: str) -> str | None:
    m = DOMAIN_RE.search(text or "")
    return m.group(1).lower().removeprefix("www.") if m else None


def find_by_website(domain: str) -> dict | None:
    for b in list_all():
        if (b.get("site") or {}).get("domain") == domain:
            return b
    return None


SITE_PROMPT = """You are setting up a video brand kit (a design system for short videos) from a captured website.

Website: {url}
Page title: {title}
Meta description: {description}

Design tokens found on the site (CSS variables, colors, fonts):
{tokens}

Visible text (excerpt):
{text}

Image files captured from the site (choose only from these):
{images}

Before answering, LOOK at the images with the Read tool: open the asset contact sheets
(`assets/contact-sheet-*.jpg`, which label each file) and `screenshots/contact-sheet.jpg`, and open single
files when unsure. Many sites show logos of partners, clubs, customers or sponsors — pick only the site's OWN
brand mark (it usually sits in the header and matches the favicon). If the own logo is only an SVG, pick the
favicon/app icon PNG of the same mark instead, or null.

Return JSON with exactly these keys:
{{
  "name": "brand or product name",
  "description": "one or two sentences: what it is and for whom",
  "voice": "tone of voice guidance for video scripts, matching the site's copy",
  "audience": "who the videos are for",
  "cta": "short call to action that mentions {domain}",
  "language": "the site's main language as an English name, e.g. Vietnamese or English",
  "colors": {{"primary": "#RRGGBB brand color", "accent": "#RRGGBB bright highlight", "text": "#FFFFFF or a very light color"}},
  "captions": {{"preset": "bold | box | minimal"}},
  "logo": "the best logo image file (prefer PNG with transparency) or null",
  "images": [{{"file": "a product screenshot or photo that shows the product well", "description": "what it shows"}}]
}}
Pick at most 8 images; skip icons, avatars and tiny decorations."""


async def import_website(url: str, bid: str | None = None, report=None) -> dict:
    """Capture a website with HyperFrames and turn it into a brand kit."""
    from . import claude, motion

    say = report or (lambda _t: None)
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    domain = find_domain(url) or url
    if not motion.status()["ok"]:
        say("Installing the website capture tool (one time)…")
        await motion.install()
    tmp = Path(tempfile.mkdtemp(dir=DATA_DIR))
    try:
        say(f"Opening {domain} in a headless browser…")
        out = await motion._run([shutil.which("npx"), "-y", f"hyperframes@{motion.HF_VERSION}", "capture", url, "-o", "site",
                                 "--skip-vision", "--max-screenshots", "8", "--json"], tmp, timeout=420)
        site = tmp / "site"
        try:
            meta = json.loads(out[out.index("{"):])
        except ValueError:
            meta = {}
        if not (site / "extracted").exists():
            raise ValueError(f"Could not capture {url}")
        tokens = json.loads(_read(site / "extracted" / "tokens.json", 200000) or "{}")
        text = _read(site / "extracted" / "visible-text.txt", 6000)
        marks = re.compile(r"logo|icon|favicon|brand", re.I)
        imgs = sorted([p for p in (site / "assets").glob("*") if p.suffix.lower() in IMAGE_EXT and "contact-sheet" not in p.name
                       and (p.stat().st_size > 4000 or marks.search(p.name))],
                      key=lambda p: (not marks.search(p.name), -p.stat().st_size))[:40]
        shots = [p for p in sorted((site / "screenshots").glob("scroll-*.png"))][:6]
        say(f"Found {len(imgs)} images, {len(tokens.get('colors') or [])} colors and fonts {', '.join(f['family'] for f in tokens.get('fonts', [])[:3])}")

        candidates = [p.name for p in imgs] + [f"screenshots/{p.name}" for p in shots]
        token_view = json.dumps({"cssVariables": tokens.get("cssVariables"), "colors": (tokens.get("colors") or [])[:24],
                                 "fonts": [f["family"] for f in tokens.get("fonts", [])]}, ensure_ascii=False)[:5000]
        if settings.get("claude")["source"]:
            say("Claude is reading the site to define the brand")
            plan = await claude.ask_json(
                SITE_PROMPT.format(url=url, domain=domain, title=tokens.get("title") or meta.get("title", ""),
                                   description=tokens.get("description", ""), tokens=token_view, text=text[:5000],
                                   images="\n".join(candidates)),
                system="You are a brand designer. Look at the images before choosing. "
                       "Your final message is ONE JSON object only, no markdown fences, no commentary.",
                kind="brand-import", tools=["Read", "Glob"], cwd=str(site), max_turns=16,
            )
        else:
            plan = {"name": tokens.get("title") or domain, "description": tokens.get("description", ""),
                    "logo": next((c for c in candidates if "logo" in c.lower() and c.lower().endswith(".png")), None),
                    "images": [{"file": f"screenshots/{p.name}", "description": "website screenshot"} for p in shots[:4]]}

        identity = {k: plan.get(k) for k in ("description", "voice", "audience", "cta", "language") if plan.get(k)}
        if isinstance(plan.get("colors"), dict):
            identity["colors"] = {k: v for k, v in plan["colors"].items() if re.fullmatch(r"#[0-9A-Fa-f]{6}", str(v))}
        if isinstance(plan.get("captions"), dict):
            identity["captions"] = plan["captions"]
        name = (plan.get("name") or domain)[:80]
        if bid:
            b = get(bid)
            for kind in ("logo", "font"):
                remove_asset(bid, kind)
            for k in b["knowledge"]:
                remove_asset(bid, "context", k["id"])
            update(bid, {"name": name, **identity})
        else:
            bid = create(name, identity)["id"]

        warnings = []
        resolve = lambda f: (site / "assets" / f) if not f.startswith("screenshots/") else (site / f)  # noqa: E731
        if plan.get("logo") and (p := resolve(Path(plan["logo"]).as_posix())).exists():
            try:
                await add_asset(bid, "logo", p, p.name)
            except ValueError as e:
                warnings.append(str(e))
        elif any(p.suffix.lower() == ".svg" and "logo" in p.name.lower() for p in (site / "assets").glob("*")):
            warnings.append("The site's logo is an SVG — upload a PNG version of the logo.")
        for f in sorted((site / "assets" / "fonts").glob("*")) if (site / "assets" / "fonts").exists() else []:
            if f.suffix.lower() in FONT_EXT:
                try:
                    await add_asset(bid, "font", f, f.name)
                    break
                except ValueError:
                    continue
        # The site's copy becomes brand knowledge.
        if text.strip():
            know = tmp / f"{domain}.md"
            know.write_text(f"# {tokens.get('title') or domain}\n\n{tokens.get('description', '')}\n\n{text}", encoding="utf-8")
            await add_asset(bid, "context", know, know.name)
        # Product visuals for Claude Motion.
        site_dir = brand_dir(bid) / "site"
        shutil.rmtree(site_dir, ignore_errors=True)
        site_dir.mkdir(parents=True)
        images = []
        for item in (plan.get("images") or [])[:8]:
            src = resolve(str(item.get("file", "")))
            if src.exists() and src.suffix.lower() in IMAGE_EXT:
                dest = site_dir / src.name
                shutil.copyfile(src, dest)
                images.append({"file": f"site/{src.name}", "description": item.get("description", "")[:200]})
        b = get(bid)
        data = {k: b[k] for k in EMPTY}
        data["site"] = {"url": url, "domain": domain, "title": tokens.get("title") or meta.get("title"),
                        "fonts": [f["family"] for f in tokens.get("fonts", []) if not f["family"].startswith("-")][:3],
                        "images": images}
        data["source"] = {"type": "website", "url": url, "domain": domain, "imported_at": time.time(),
                          "mode": "claude" if settings.get("claude")["source"] else "heuristic"}
        data["warnings"] = warnings
        db.save_brand(bid, b["name"], data)
        say(f"Brand kit ready: {b['name']}")
        return get(bid)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
