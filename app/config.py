"""Process-level config. Everything user-facing (accounts, API keys, app
credentials) is configured in the web UI and stored in the database; see
settings.py. Environment variables here are optional."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DATA_DIR = Path(os.getenv("DATA_DIR", ROOT / "data")).resolve()
VIDEOS_DIR = DATA_DIR / "videos"
WORK_DIR = DATA_DIR / "work"
CLAUDE_CONFIG_DIR = DATA_DIR / "claude"  # Claude login made from the web UI
DB_PATH = DATA_DIR / "autoclip.db"
FONT_PATH = Path(__file__).resolve().parent / "assets" / "fonts" / "BeVietnamPro-Bold.ttf"

for d in (VIDEOS_DIR, WORK_DIR, CLAUDE_CONFIG_DIR):
    d.mkdir(parents=True, exist_ok=True)

HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "8000"))

# AutoClip Connect (services/connect): one-click TikTok without a developer app. AUTOCLIP_CONNECT_URL overrides it
# (a self-hosted Worker, `wrangler dev` while testing, or "" to turn it off).
CONNECT_URL = os.getenv("AUTOCLIP_CONNECT_URL", "https://autoclip-connect.thihodev.workers.dev").rstrip("/")
