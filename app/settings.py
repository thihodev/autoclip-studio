"""User settings, edited from the Connections tab and stored in SQLite."""
from . import db

DEFAULTS = {
    # source: "" until the user picks. effort "" = the model's default.
    "claude": {"source": "", "api_key": "", "model": "sonnet", "effort": "", "motion_model": "opus", "motion_effort": "high",
               "designer_model": "opus", "designer_effort": "high"},
    "footage": {"provider": "pixabay", "pexels_api_key": "", "pixabay_api_key": ""},
    "voice": {"elevenlabs_api_key": "", "elevenlabs_voice_id": "", "elevenlabs_voice_name": "", "elevenlabs_model": ""},
    "github": {"token": "", "login": ""},
}

SECRET_KEYS = {"api_key", "pexels_api_key", "pixabay_api_key", "token", "elevenlabs_api_key"}
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


def migrate():
    """One-time changes to saved settings when a default changes for a reason that applies to everyone."""
    done = set(db.get_setting("migrations", []) or [])
    if "motion_effort_medium" not in done:
        # Claude Motion's orchestrator at medium effort: about as good, far fewer tokens than high.
        saved = db.get_setting("cfg.claude")
        if saved and saved.get("motion_effort") == "high":
            db.set_setting("cfg.claude", {**saved, "motion_effort": "medium"})
        done.add("motion_effort_medium")
    if "motion_quality_defaults" not in done:
        # Back to Opus at high effort for the orchestrator and the scene designers: the cheaper defaults
        # (medium effort, Sonnet designers) made visibly weaker videos. Only settings still on those defaults move.
        saved = db.get_setting("cfg.claude")
        if saved:
            fix = {}
            if saved.get("motion_effort") == "medium" and "motion_effort_medium" in done:
                fix["motion_effort"] = "high"
            if saved.get("designer_model", "sonnet") == "sonnet" and saved.get("designer_effort", "medium") == "medium":
                fix |= {"designer_model": "opus", "designer_effort": "high"}
            if fix:
                db.set_setting("cfg.claude", {**saved, **fix})
        done.add("motion_quality_defaults")
    if "drop_publishing" not in done:
        # AutoClip no longer connects to social networks: forget the stored tokens and app secrets.
        db.forget_publishing()
        done.add("drop_publishing")
    db.set_setting("migrations", sorted(done))
