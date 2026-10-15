import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DATA_DIR = Path(os.getenv("DATA_DIR", ROOT / "data"))
VIDEOS_DIR = DATA_DIR / "videos"
DB_PATH = DATA_DIR / "autoclip.db"
VIDEOS_DIR.mkdir(parents=True, exist_ok=True)

# Claude: by default the Agent SDK reuses the local Claude Code login (Pro/Max).
# Set CLAUDE_CODE_OAUTH_TOKEN (from `claude setup-token`) for headless servers,
# or ANTHROPIC_API_KEY to bill the API instead.
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "sonnet")

# MoneyPrinterTurbo API (video rendering engine)
MPT_URL = os.getenv("MPT_URL", "http://127.0.0.1:8080").rstrip("/")
MPT_API_KEY = os.getenv("MPT_API_KEY", "")

# Postiz public API (publishing to TikTok, YouTube, Facebook, Instagram, ...)
POSTIZ_URL = os.getenv("POSTIZ_URL", "http://localhost:4007/api").rstrip("/")
POSTIZ_API_KEY = os.getenv("POSTIZ_API_KEY", "")
