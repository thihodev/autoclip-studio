"""Local voice: F5-TTS with the Vietnamese model (hynt/F5-TTS-Vietnamese-ViVoice), run on this computer.

One voice for a whole series, with no characters to run out of: the person uploads a short clip of the voice (with
its exact words) and every line is read in that voice. Everything lives in DATA_DIR/localvoice: its own Python
(3.12, fetched by uv), F5-TTS with PyTorch, the model and the voices. Installing is one click in Connections and
downloads about 8 GB once (the published checkpoint is a 5.4 GB training file; only its 1.3 GB of weights are kept).

The model is licensed CC-BY-NC-SA-4.0 (non-commercial). It speaks Vietnamese only.
"""
from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import platform
import re
import shutil
import sys
import time
import uuid
from pathlib import Path

from .config import DATA_DIR

log = logging.getLogger("localvoice")

ROOT = DATA_DIR / "localvoice"
MODEL_DIR = ROOT / "model"
HF_DIR = ROOT / "hf"
VOICES_DIR = ROOT / "voices"
READY = ROOT / "ready.json"
WORKER = Path(__file__).resolve().parent / "assets" / "f5_worker.py"
REPO = "hynt/F5-TTS-Vietnamese-ViVoice"
PREFIX = "local:"  # a voice picked for a video: "local:<id>"
MIN_REF, MAX_REF = 3.0, 12.0  # seconds of the voice sample (F5-TTS clips anything longer)
IDLE = 600  # the model leaves memory after this many seconds without work

_state = {"installing": False, "step": "", "error": None}


def _bin(venv: Path, name: str) -> Path:
    return venv / ("Scripts" if os.name == "nt" else "bin") / (name + (".exe" if os.name == "nt" else ""))


def _env() -> dict:
    # Every download and cache stays inside DATA_DIR.
    return {**os.environ, "UV_CACHE_DIR": str(ROOT / "uv-cache"), "UV_PYTHON_INSTALL_DIR": str(ROOT / "python"),
            "HF_HOME": str(HF_DIR), "PIP_CACHE_DIR": str(ROOT / "pip-cache"), "PYTORCH_ENABLE_MPS_FALLBACK": "1",
            "PYTHONUNBUFFERED": "1", "TOKENIZERS_PARALLELISM": "false"}


def supported() -> str | None:
    """None when this computer can run it, else why not."""
    if sys.platform == "darwin" and platform.machine() == "x86_64":
        return "Needs a Mac with Apple Silicon (M1 or newer): PyTorch no longer supports Intel Macs"
    return None


def installed() -> bool:
    return READY.exists() and _bin(ROOT / "venv", "python").exists() and (MODEL_DIR / "model.safetensors").exists()


def status() -> dict:
    return {"supported": supported() is None, "unsupported": supported(), "installed": installed(),
            "installing": _state["installing"], "step": _state["step"], "error": _state["error"],
            "device": (json.loads(READY.read_text()).get("device") if READY.exists() else None), "voices": voices()}


# ------------------------------------------------------------------ install

async def _run(*args, step: str, timeout: float = 3600):
    _state["step"] = step
    proc = await asyncio.create_subprocess_exec(*map(str, args), stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.STDOUT, env=_env(), cwd=str(ROOT))
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError(f"{step}: took too long")
    if proc.returncode:
        tail = out.decode(errors="replace").strip()[-600:]
        raise RuntimeError(f"{step} failed: {tail}")
    return out.decode(errors="replace")


FETCH = r'''
import os, shutil, sys
from huggingface_hub import hf_hub_download
model_dir, cache = sys.argv[1], sys.argv[2]
os.makedirs(model_dir, exist_ok=True)
for f in ("config.yaml", "pytorch_model.bin"):  # the vocoder, so the worker never needs the network
    hf_hub_download("charactr/vocos-mel-24khz", f, cache_dir=cache)
vocab = hf_hub_download("%(repo)s", "config.json", cache_dir=cache)  # this repo's config.json is the vocabulary
shutil.copyfile(vocab, os.path.join(model_dir, "vocab.txt"))
ckpt = hf_hub_download("%(repo)s", "model_last.pt", cache_dir=cache)
import torch
from safetensors.torch import save_file
data = torch.load(ckpt, map_location="cpu", weights_only=True)
weights = data.get("ema_model_state_dict")
if weights is None:  # no EMA weights: keep the model's under the names the loader expects
    weights = {"ema_model." + k: v for k, v in data["model_state_dict"].items()}
save_file({k: v.contiguous() for k, v in weights.items() if hasattr(v, "contiguous")}, os.path.join(model_dir, "model.safetensors"))
del data, weights
shutil.rmtree(os.path.join(cache, "models--" + "%(repo)s".replace("/", "--")), ignore_errors=True)  # the 5.4 GB file
print("ok")
''' % {"repo": REPO}


async def install():
    if _state["installing"]:
        return
    if (why := supported()):
        _state["error"] = why
        return
    _state.update(installing=True, error=None)
    try:
        ROOT.mkdir(parents=True, exist_ok=True)
        tools = ROOT / "tools"
        if not _bin(tools, "uv").exists():
            await _run(sys.executable, "-m", "venv", tools, step="Preparing the installer")
            await _run(_bin(tools, "python"), "-m", "pip", "install", "--no-cache-dir", "uv", step="Preparing the installer")
        uv, venv = _bin(tools, "uv"), ROOT / "venv"
        if not _bin(venv, "python").exists():
            await _run(uv, "venv", "--python", "3.12", venv, step="Downloading Python 3.12")
        await _run(uv, "pip", "install", "--python", _bin(venv, "python"), "f5-tts",
                   step="Installing F5-TTS and PyTorch (about 2 GB)")
        if not (MODEL_DIR / "model.safetensors").exists():
            await _run(_bin(venv, "python"), "-c", FETCH, MODEL_DIR, HF_DIR,
                       step="Downloading the Vietnamese voice model (5.4 GB, kept as 1.3 GB)")
        _state["step"] = "Loading the model once"
        READY.write_text(json.dumps({"at": time.time()}))
        device = await _worker.start()
        READY.write_text(json.dumps({"at": time.time(), "device": device}))
    except Exception as e:
        log.exception("local voice install failed")
        READY.unlink(missing_ok=True)
        _state["error"] = str(e)[:600]
    finally:
        _state.update(installing=False, step="")


def remove():
    """Uninstall: the model, Python and libraries go; the voice samples stay."""
    _worker.stop()
    for name in ("venv", "tools", "python", "model", "hf", "uv-cache", "pip-cache"):
        shutil.rmtree(ROOT / name, ignore_errors=True)
    READY.unlink(missing_ok=True)


# ------------------------------------------------------------------ voices (samples)

def _voices_file() -> Path:
    return VOICES_DIR / "voices.json"


def voices() -> list[dict]:
    f = _voices_file()
    return [v for v in (json.loads(f.read_text()) if f.exists() else []) if (VOICES_DIR / f"{v['id']}.wav").exists()]


def get_voice(vid: str) -> dict | None:
    return next((v for v in voices() if v["id"] == vid), None)


def sample(vid: str) -> Path | None:
    p = VOICES_DIR / f"{Path(vid).name}.wav"
    return p if p.exists() else None


async def add_voice(data: bytes, filename: str, name: str, text: str) -> dict:
    from . import render

    text = " ".join(text.split())
    if len(text) < 5:
        raise ValueError("Type exactly what the clip says")
    VOICES_DIR.mkdir(parents=True, exist_ok=True)
    vid = uuid.uuid4().hex[:10]
    src = VOICES_DIR / f"{vid}.src{Path(filename).suffix.lower()[:6]}"
    src.write_bytes(data)
    out = VOICES_DIR / f"{vid}.wav"
    try:
        await render._ffmpeg("-i", src, "-ac", "1", "-ar", "24000", "-af", "silenceremove=start_periods=1:start_threshold=-50dB,"
                             "areverse,silenceremove=start_periods=1:start_threshold=-50dB,areverse", out)
        seconds = await render.media_duration(out)
    except Exception:
        out.unlink(missing_ok=True)
        raise ValueError("That file doesn't look like audio")
    finally:
        src.unlink(missing_ok=True)
    if not MIN_REF <= seconds <= MAX_REF:
        out.unlink(missing_ok=True)
        raise ValueError(f"Use a clip of {MIN_REF:.0f}–{MAX_REF:.0f} seconds (this one is {seconds:.1f} s): "
                         "one or two clear sentences, no music")
    v = {"id": vid, "name": (name or "My voice").strip()[:60], "text": text[:500], "seconds": round(seconds, 1),
         "at": time.time()}
    _voices_file().write_text(json.dumps(voices() + [v], ensure_ascii=False, indent=1))
    return v


def remove_voice(vid: str):
    _voices_file().write_text(json.dumps([v for v in voices() if v["id"] != vid], ensure_ascii=False, indent=1))
    (VOICES_DIR / f"{Path(vid).name}.wav").unlink(missing_ok=True)


# ------------------------------------------------------------------ Vietnamese text for the model

DIGITS = "không một hai ba bốn năm sáu bảy tám chín".split()


def _triple(n: int, full: bool) -> str:
    """0–999 in words; `full` when it isn't the leading group (keeps "không trăm", "lẻ")."""
    h, t, u = n // 100, n // 10 % 10, n % 10
    out = []
    if h or full:
        out += [DIGITS[h], "trăm"]
    if t == 0:
        if u and (h or full):
            out.append("lẻ")
    elif t == 1:
        out.append("mười")
    else:
        out += [DIGITS[t], "mươi"]
    if u:
        out.append("mốt" if u == 1 and t >= 2 else "lăm" if u == 5 and t >= 1 else DIGITS[u])
    return " ".join(out)


def number_words(n: int) -> str:
    if n == 0:
        return "không"
    groups = []
    while n:
        groups.append(n % 1000)
        n //= 1000
    units = ["", "nghìn", "triệu", "tỷ", "nghìn tỷ", "triệu tỷ"]
    out = []
    for i in range(len(groups) - 1, -1, -1):
        g = groups[i]
        if g:
            out.append(_triple(g, i != len(groups) - 1) + (" " + units[i] if units[i] else ""))
    return " ".join(out)


def normalize(text: str) -> str:
    """How the model reads best: lowercase (as it was trained) with numbers spelled out."""
    t = re.sub(r"(\d)%", r"\1 phần trăm", text)
    t = re.sub(r"\b\d{1,3}(?:\.\d{3})+\b", lambda m: m.group(0).replace(".", ""), t)  # 1.000.000
    t = re.sub(r"\b(\d+),(\d+)\b", lambda m: f"{number_words(int(m.group(1)))} phẩy {' '.join(DIGITS[int(d)] for d in m.group(2))}", t)
    t = re.sub(r"\b\d+\b", lambda m: number_words(int(m.group(0))) if len(m.group(0)) <= 15 else " ".join(DIGITS[int(d)] for d in m.group(0)), t)
    return re.sub(r"\s+", " ", t).strip().lower()


# ------------------------------------------------------------------ the worker (model in memory)

class _Worker:
    def __init__(self):
        self.proc: asyncio.subprocess.Process | None = None
        self.lock = asyncio.Lock()
        self.errors = collections.deque(maxlen=40)
        self.idle: asyncio.TimerHandle | None = None

    async def _drain(self, stream):
        async for line in stream:
            self.errors.append(line.decode(errors="replace").rstrip())

    async def start(self) -> str:
        if self.proc and self.proc.returncode is None:
            return "ready"
        self.proc = await asyncio.create_subprocess_exec(
            str(_bin(ROOT / "venv", "python")), str(WORKER), str(MODEL_DIR), str(HF_DIR),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env={**_env(), "HF_HUB_OFFLINE": "1"}, limit=1 << 20)
        asyncio.get_running_loop().create_task(self._drain(self.proc.stderr))
        hello = await self._read(300)
        if not hello.get("ok"):
            self.stop()
            raise RuntimeError(hello.get("error") or "The voice model didn't start")
        return hello.get("device", "")

    async def _read(self, timeout: float) -> dict:
        try:
            line = await asyncio.wait_for(self.proc.stdout.readline(), timeout)
        except asyncio.TimeoutError:
            self.stop()
            raise RuntimeError("The voice model stopped answering")
        if not line:
            tail = "\n".join(list(self.errors)[-6:])
            self.stop()
            raise RuntimeError(f"The voice model quit{': ' + tail[-400:] if tail else ''}")
        return json.loads(line)

    async def run(self, job: dict) -> None:
        async with self.lock:
            if self.idle:
                self.idle.cancel()
            await self.start()
            self.proc.stdin.write((json.dumps(job, ensure_ascii=False) + "\n").encode())
            await self.proc.stdin.drain()
            res = await self._read(900)
            self.idle = asyncio.get_running_loop().call_later(IDLE, self.stop)
        if not res.get("ok"):
            raise RuntimeError(f"Local voice: {res.get('error')}")

    def stop(self):
        if self.proc and self.proc.returncode is None:
            self.proc.kill()
        self.proc = None


_worker = _Worker()


async def synthesize(text: str, vid: str, rate: float, dest: Path) -> list[list]:
    """Read `text` in the local voice `vid` into `dest` (mp3); word timings are spread over the audio by length."""
    from . import render

    if not installed():
        raise RuntimeError("The local voice isn't installed — open Connections → Voice-over")
    v = get_voice(vid)
    if not v:
        raise RuntimeError("That local voice was removed — pick another voice")
    wav = dest.with_suffix(".local.wav")
    await _worker.run({"ref": str(VOICES_DIR / f"{v['id']}.wav"), "ref_text": normalize(v["text"]),
                       "text": normalize(text), "out": str(wav), "speed": max(0.7, min(1.4, rate))})
    try:
        await render._ffmpeg("-i", wav, "-ac", "1", "-ar", "44100", "-b:a", "160k", dest)
        dur = await render.media_duration(dest)
    finally:
        wav.unlink(missing_ok=True)
    words = text.split()
    weights = [len(normalize(w)) + 1 + (3 if re.search(r"[.!?…]$", w) else 1.5 if re.search(r"[,;:]$", w) else 0) for w in words]
    start, span, out = 0.08, max(0.2, dur - 0.16), []
    for w, k in zip(words, weights):
        d = span * k / sum(weights)
        out.append([round(start, 3), round(start + d * 0.92, 3), w])
        start += d
    return out
