"""Claude usage: models, effort, tokens, API-equivalent cost and plan limits.

Every Claude run (script, ideas, edits, Claude Motion, brand import) is metered
from the Agent SDK's messages: the session's last ResultMessage carries
cumulative `total_cost_usd` and per-model token counts; turns add up per run.
RateLimitEvents report the subscription's rate-limit windows.
"""
import time

from claude_agent_sdk import RateLimitEvent, ResultMessage

from . import db, settings

MODELS = [
    {"id": "opus", "label": "Claude Opus 5.5", "note": "Best quality for most work", "price": [4, 20]},
    {"id": "sonnet", "label": "Claude Sonnet 5.5", "note": "Fast and capable", "price": [2, 10]},
    {"id": "haiku", "label": "Claude Haiku 5.5", "note": "Fastest, lightest", "price": [0.1, 0.5]},
    {"id": "claude-fable-5-1", "label": "Claude Fable 5.1", "note": "Most capable, uses the most", "price": [10, 50]},
]
EFFORTS = ["", "low", "medium", "high", "xhigh", "max"]  # "" = model default

# Defaults per task: (settings key for model, settings key for effort)
TASKS = {"script": ("model", "effort"), "motion": ("motion_model", "motion_effort")}


def choose(task: str, model: str | None = None, effort: str | None = None) -> tuple[str, str | None]:
    """Model and effort for a run: explicit per-video choice, else the Connections defaults."""
    cfg = settings.get("claude")
    mk, ek = TASKS[task]
    model = model or cfg.get(mk) or ("opus" if task == "motion" else "sonnet")
    effort = effort if effort in EFFORTS and effort else (cfg.get(ek) or None)
    return model, effort


class Meter:
    def __init__(self, kind: str, project_id: str | None, model: str, effort: str | None):
        self.kind, self.project_id, self.model, self.effort = kind, project_id, model, effort
        self.started = time.time()
        self.last: ResultMessage | None = None
        self.turns = 0
        self.saved = False

    def see(self, msg):
        if isinstance(msg, ResultMessage):
            self.last = msg
            self.turns += msg.num_turns or 0
        elif isinstance(msg, RateLimitEvent):
            record_rate_limit(msg)

    def save(self, ok: bool = True):
        if self.saved:
            return
        self.saved = True
        models = (self.last.model_usage if self.last else None) or {}
        tok = lambda k: sum(int(m.get(k) or 0) for m in models.values())  # noqa: E731
        db.add_usage({
            "at": time.time(), "project_id": self.project_id, "kind": self.kind, "model": self.model,
            "effort": self.effort or "", "source": settings.get("claude")["source"],
            "models": {name: {k: m.get(k) for k in ("inputTokens", "outputTokens", "cacheReadInputTokens",
                                                     "cacheCreationInputTokens", "costUSD")} for name, m in models.items()},
            "input_tokens": tok("inputTokens"), "output_tokens": tok("outputTokens"),
            "cache_read_tokens": tok("cacheReadInputTokens"), "cache_write_tokens": tok("cacheCreationInputTokens"),
            "cost_usd": float((self.last.total_cost_usd if self.last else 0) or 0),
            "turns": self.turns, "duration_s": round(time.time() - self.started, 1), "ok": 1 if ok else 0,
        })


def record_rate_limit(ev: RateLimitEvent):
    info = ev.rate_limit_info
    limits = db.get_setting("rate_limits", {}) or {}
    limits[info.rate_limit_type or "unknown"] = {
        "status": info.status, "utilization": info.utilization, "resets_at": info.resets_at,
        "overage_status": info.overage_status, "at": time.time(),
    }
    db.set_setting("rate_limits", limits)


def summary(days: int = 30) -> dict:
    now = time.time()
    since = now - days * 86400
    today = time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))
    return {
        "days": days,
        "today": db.usage_totals(today),
        "period": db.usage_totals(since),
        "by_day": db.usage_grouped(since, "day"),
        "by_model": db.usage_grouped(since, "model"),
        "by_kind": db.usage_grouped(since, "kind"),
        "recent": db.usage_recent(40),
        "rate_limits": db.get_setting("rate_limits", {}) or {},
        "source": settings.get("claude")["source"],
        "models": MODELS,
    }


def project(pid: str) -> dict:
    return {"total": db.usage_totals(0, pid), "runs": db.usage_recent(50, pid)}
