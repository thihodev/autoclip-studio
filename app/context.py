"""Context files: documents Claude reads when writing or editing videos.

Users can attach articles, notes, product docs or transcripts to a video, or
add them to a brand kit as "knowledge". Text is extracted once on upload and
injected into Claude's prompts within a size budget.
"""
import html
import json
import re
import shutil
import time
import uuid
import zipfile
from pathlib import Path

from .config import DATA_DIR

CONTEXT_DIR = DATA_DIR / "context"
TEXT_EXT = {".txt", ".md", ".markdown", ".csv", ".json", ".srt", ".vtt", ".yaml", ".yml"}
DOC_EXT = {".pdf", ".docx", ".html", ".htm"}
ALLOWED = TEXT_EXT | DOC_EXT
MAX_BYTES = 20 * 1024 * 1024
MAX_CHARS = 60_000  # stored per file


def _clean(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


def _strip_tags(markup: str) -> str:
    markup = re.sub(r"<(script|style|noscript)[^>]*>.*?</\1>", " ", markup, flags=re.S | re.I)
    markup = re.sub(r"<(br|/p|/div|/h[1-6]|/li|/tr)[^>]*>", "\n", markup, flags=re.I)
    return html.unescape(re.sub(r"<[^>]+>", " ", markup))


def extract_text(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in TEXT_EXT:
        text = path.read_text(errors="replace")
    elif ext in (".html", ".htm"):
        text = _strip_tags(path.read_text(errors="replace"))
    elif ext == ".docx":
        with zipfile.ZipFile(path) as z:
            xml = z.read("word/document.xml").decode("utf-8", errors="replace")
        xml = re.sub(r"</w:p>", "\n", xml)
        xml = re.sub(r"<w:tab/>", "\t", xml)
        text = html.unescape(re.sub(r"<[^>]+>", "", xml))
    elif ext == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        text = "\n\n".join((page.extract_text() or "") for page in reader.pages[:300])
    else:
        raise ValueError(f"Unsupported file type {ext}")
    return _clean(text)[:MAX_CHARS]


def save(src: Path, name: str) -> dict:
    """Store a context file and its extracted text → {id, name, chars}."""
    ext = Path(name).suffix.lower()
    if ext not in ALLOWED:
        raise ValueError(f"'{name}' isn't supported. Use TXT, MD, PDF, DOCX, HTML, CSV, JSON or subtitles (SRT/VTT).")
    if src.stat().st_size > MAX_BYTES:
        raise ValueError(f"'{name}' is larger than 20 MB")
    cid = uuid.uuid4().hex[:12]
    d = CONTEXT_DIR / cid
    d.mkdir(parents=True)
    safe = re.sub(r"[^\w.\- ]+", "_", Path(name).name)[:120] or f"file{ext}"
    shutil.copyfile(src, d / safe)
    try:
        text = extract_text(d / safe)
    except Exception as e:
        shutil.rmtree(d, ignore_errors=True)
        raise ValueError(f"Could not read '{name}' ({e.__class__.__name__})")
    if not text.strip():
        shutil.rmtree(d, ignore_errors=True)
        raise ValueError(f"No readable text in '{name}' (scanned PDFs and images aren't supported)")
    (d / "text.txt").write_text(text)
    meta = {"id": cid, "name": Path(name).name, "chars": len(text), "at": time.time()}
    (d / "meta.json").write_text(json.dumps(meta))
    return meta


def get(cid: str) -> dict | None:
    p = CONTEXT_DIR / Path(cid).name / "meta.json"
    return json.loads(p.read_text()) if p.exists() else None


def text(cid: str) -> str:
    p = CONTEXT_DIR / Path(cid).name / "text.txt"
    return p.read_text() if p.exists() else ""


def remove(cid: str):
    shutil.rmtree(CONTEXT_DIR / Path(cid).name, ignore_errors=True)


def prompt_block(ids: list[str], title: str, budget: int = 30_000) -> str:
    """The files' text for a prompt, within `budget` characters."""
    parts, left = [], budget
    for cid in ids or []:
        meta, body = get(cid), text(cid)
        if not meta or not body or left <= 0:
            continue
        chunk = body[:left]
        cut = " [truncated]" if len(chunk) < len(body) else ""
        parts.append(f'<file name="{meta["name"]}"{cut}>\n{chunk}\n</file>')
        left -= len(chunk)
    if not parts:
        return ""
    return f"{title}:\n" + "\n".join(parts)


def cleanup(referenced: set[str], older_than: float = 86400):
    """Delete uploads that no video or brand kit uses (e.g. removed before creating)."""
    if not CONTEXT_DIR.exists():
        return
    now = time.time()
    for d in CONTEXT_DIR.iterdir():
        if d.is_dir() and d.name not in referenced and now - d.stat().st_mtime > older_than:
            shutil.rmtree(d, ignore_errors=True)
