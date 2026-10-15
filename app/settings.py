"""User settings, edited from the Connections tab and stored in SQLite."""
from . import db

DEFAULTS = {
    "claude": {"source": "", "api_key": "", "model": "sonnet", "motion_model": "opus"},  # source: "" until the user picks
    "footage": {"provider": "pixabay", "pexels_api_key": "", "pixabay_api_key": ""},
    "framejam": {"url": "http://localhost:2400"},
    "voice": {"elevenlabs_api_key": "", "elevenlabs_voice_id": "", "elevenlabs_voice_name": "", "elevenlabs_model": ""},
    "postiz": {"url": "", "api_key": ""},
    "github": {"token": "", "login": ""},
    "apps": {
        "youtube": {"client_id": "", "client_secret": ""},
        "tiktok": {"client_key": "", "client_secret": ""},
        "facebook": {"app_id": "", "app_secret": ""},
    },
    "public_url": "",
}

SECRET_KEYS = {"api_key", "pexels_api_key", "pixabay_api_key", "client_secret", "app_secret", "token", "elevenlabs_api_key"}
MASK = "••••"


def _merge(default, value):
    if isinstance(default, dict):
        value = value if isinstance(value, dict) else {}
        return {k: _merge(v, value.get(k)) for k, v in default.items()}
    return default if value is None else value


def get(section: str):
    return _merge(DEFAULTS[section], db.get_setting(f"cfg.{section}"))


def put(section: str, value):
    current = get(section)
    db.set_setting(f"cfg.{section}", _unmask(current, value))


def _unmask(current, new):
    """Masked secrets coming back from the UI mean "unchanged"."""
    if isinstance(current, dict) and isinstance(new, dict):
        return {k: _unmask(current.get(k), new.get(k, current.get(k))) for k in current}
    if isinstance(new, str) and new.startswith(MASK):
        return current
    return new


def _mask(value, key=""):
    if isinstance(value, dict):
        return {k: _mask(v, k) for k, v in value.items()}
    if key in SECRET_KEYS and value:
        return MASK + str(value)[-4:]
    return value


def public() -> dict:
    """All settings with secrets masked, for the UI."""
    return {section: _mask(get(section)) for section in DEFAULTS}
