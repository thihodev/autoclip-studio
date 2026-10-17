"""Music composed by Claude: a Tone.js score (JavaScript) rendered offline to WAV in the headless Chrome that
Claude Motion already installs — nothing else to install, no samples or licenses to track.

The score defines `async function compose({ Tone, seconds, transport })`: it builds instruments and effects and
schedules notes on the timeline. AutoClip renders it faster than real time (Tone.Offline), normalizes the loudness,
and reports what Claude can't hear: errors, length, loudness, silence, clipping and a spectrogram to look at.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

from . import render

TONE_JS = Path(__file__).parent / "assets" / "tone" / "Tone.js"
SAMPLE_RATE = 44100
MAX_SECONDS = 300

HARNESS = """<!doctype html><meta charset="utf-8"><script src="Tone.js"></script>
<script>
window.__errors = [];
window.addEventListener("error", e => __errors.push(`${e.message} (line ${e.lineno})`));
</script>
<script src="score.js"></script>
<script>
function wav(buffer) {  // AudioBuffer → 16-bit PCM WAV, base64
  const ch = buffer.numberOfChannels, n = buffer.length, rate = buffer.sampleRate;
  const out = new DataView(new ArrayBuffer(44 + n * ch * 2));
  const str = (o, s) => [...s].forEach((c, i) => out.setUint8(o + i, c.charCodeAt(0)));
  str(0, "RIFF"); out.setUint32(4, 36 + n * ch * 2, true); str(8, "WAVE"); str(12, "fmt ");
  out.setUint32(16, 16, true); out.setUint16(20, 1, true); out.setUint16(22, ch, true);
  out.setUint32(24, rate, true); out.setUint32(28, rate * ch * 2, true); out.setUint16(32, ch * 2, true);
  out.setUint16(34, 16, true); str(36, "data"); out.setUint32(40, n * ch * 2, true);
  const data = [...Array(ch)].map((_, c) => buffer.getChannelData(c));
  let o = 44;
  for (let i = 0; i < n; i++) for (let c = 0; c < ch; c++) {
    const s = Math.max(-1, Math.min(1, data[c][i]));
    out.setInt16(o, s < 0 ? s * 0x8000 : s * 0x7fff, true); o += 2;
  }
  const bytes = new Uint8Array(out.buffer);
  let bin = "";
  for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  return btoa(bin);
}
window.__render = async (seconds) => {
  if (__errors.length) return { error: __errors.join("\\n") };
  if (typeof compose !== "function") return { error: "score.js must define `async function compose({ Tone, seconds, transport })`" };
  try {
    const buffer = await Tone.Offline(async ({ transport }) => {
      await compose({ Tone, seconds, transport });
      transport.start(0);
    }, seconds, 2, %d);
    return { wav: wav(buffer.get()) };
  } catch (e) {
    return { error: String(e && e.stack || e).split("\\n").slice(0, 4).join("\\n") };
  }
};
</script>""" % SAMPLE_RATE


# What Claude needs to write a good score; part of the Claude Motion brief and of Quick's one-shot request.
GUIDE = """Write the score as `music/score.js`: plain JavaScript (Tone.js 15 is loaded as `Tone`) defining
`async function compose({ Tone, seconds, transport })`. AutoClip renders it offline to WAV, faster than real time.

- Schedule with absolute times in seconds: `synth.triggerAttackRelease(note, duration, time)` (duration in seconds or
  "8n"), or `new Tone.Part/Sequence(...)` with `transport.bpm.value = …` and `.start(0)`. Everything must happen
  inside `seconds`. No samples or URLs: build every sound from Tone's synths (Synth, PolySynth, FMSynth, AMSynth,
  MonoSynth, PluckSynth, MembraneSynth, MetalSynth, NoiseSynth, Noise) and effects (Filter, Reverb — `await
  reverb.ready` —, FeedbackDelay, Chorus, Distortion, BitCrusher, AutoFilter, Tremolo, StereoWidener, EQ3,
  Compressor). Route everything through one bus: `const bus = new Tone.Compressor(-20, 3); bus.chain(new
  Tone.Limiter(-1), Tone.getDestination());` and keep each part's level with `volume` or `Tone.Volume` (−8 to −24 dB).
- Instrumental only. Make it sound produced, not like a ringtone: layered pads (fatsawtooth or detuned
  PolySynth, low-passed, with reverb), a real low end (sub/bass following the roots), drums designed from
  MembraneSynth (kick, toms, taiko), NoiseSynth through filters (snare, claps, hats, risers), MetalSynth (shakers,
  rides), humanize timing and velocity slightly, automate filters and volumes (`.rampTo`, `linearRampToValueAtTime`)
  so sections breathe, use reverb/delay sends for depth, and use sidechain-style ducking (ramp the pad bus down on
  each kick) for electronic styles.
- Arrange to the video: the first second already sounds (no slow fade-in) and matches the hook's energy; change
  section, add a hit or a riser into each scene change; build toward the main reveal; the boldest moment gets the
  biggest hit; end on the tonic with a final hit and a 1–1.5 s tail at exactly the video's length. Pick a tempo
  whose beats land on the scene changes where you can.
- Leave room for the voice: while someone speaks keep the 1–4 kHz band sparse (no busy lead melody; pads filtered
  below ~2 kHz), play hooks and melodies in the gaps and when there is no voice-over.
- Fit the mood the storyboard names (genre, energy, tempo). Rough recipes: cinematic — low string-like saws,
  taiko hits, risers, a swell into each reveal; upbeat/electronic — four-on-the-floor kick, off-beat hats, plucked
  chords, ducked pads; playful — pizzicato/marimba plucks (PluckSynth, FMSynth), bouncy bass, claps; lo-fi — 70–90 bpm,
  swung drums, soft Rhodes (FMSynth), brown-noise crackle; corporate/tech — clean piano-like plucks, light percussion,
  steady 100–120 bpm; tense — drones, pulsing low notes, ticking hats, dissonant swells.
- After each render, read the report: fix errors and clipping, fill unintended quiet seconds, and Read the
  spectrogram to check that energy follows the scenes. At most 2 revisions."""


def chrome() -> Path | None:
    """The headless Chrome HyperFrames installed (`hyperframes browser ensure`)."""
    base = Path.home() / ".cache" / "hyperframes" / "chrome"
    names = ("chrome-headless-shell.exe", "chrome-headless-shell")
    found = [p for n in names for p in base.glob(f"*/*/*/{n}")] + [p for n in names for p in base.glob(f"*/*/{n}")]
    found = [p for p in found if p.is_file() and os.access(p, os.X_OK)]
    return max(found, key=lambda p: p.stat().st_mtime) if found else None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _run_in_chrome(page: Path, seconds: float) -> dict:
    import websockets
    from urllib.request import urlopen

    exe = chrome()
    if not exe:
        raise RuntimeError("Claude Motion's browser isn't installed yet — open Connections → Claude Motion")
    port = _free_port()
    profile = tempfile.mkdtemp(prefix="autoclip-compose-")
    proc = subprocess.Popen([str(exe), f"--remote-debugging-port={port}", f"--user-data-dir={profile}",
                             "--allow-file-access-from-files", "--autoplay-policy=no-user-gesture-required",
                             "--no-first-run", "--mute-audio", "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            try:
                tabs = json.load(urlopen(f"http://127.0.0.1:{port}/json", timeout=1))
                ws_url = next(t["webSocketDebuggerUrl"] for t in tabs if t["type"] == "page")
                break
            except Exception:
                await asyncio.sleep(0.1)
        else:
            raise RuntimeError("The browser didn't start")
        async with websockets.connect(ws_url, max_size=2 ** 28) as ws:
            n = 0

            async def call(method, **params):
                nonlocal n
                n += 1
                await ws.send(json.dumps({"id": n, "method": method, "params": params}))
                while True:
                    m = json.loads(await ws.recv())
                    if m.get("id") == n:
                        return m

            await call("Page.enable")
            await call("Page.navigate", url=page.as_uri())
            for _ in range(100):  # wait for the scripts to load
                r = await call("Runtime.evaluate", expression="typeof window.__render", returnByValue=True)
                if r.get("result", {}).get("result", {}).get("value") == "function":
                    break
                await asyncio.sleep(0.1)
            r = await asyncio.wait_for(call("Runtime.evaluate", expression=f"window.__render({seconds})",
                                            awaitPromise=True, returnByValue=True), timeout=240)
            res = r.get("result", {})
            if res.get("exceptionDetails"):
                return {"error": res["exceptionDetails"].get("text", "error")}
            return res.get("result", {}).get("value") or {"error": "the score returned nothing"}
    finally:
        proc.kill()
        proc.wait()
        shutil.rmtree(profile, ignore_errors=True)


async def render_score(code: str, seconds: float, dest: Path) -> dict:
    """Render a score to `dest` (WAV, loudness-normalized) and measure it. Returns {ok, error?, report}."""
    seconds = max(3.0, min(float(seconds), MAX_SECONDS))
    with tempfile.TemporaryDirectory(prefix="autoclip-score-") as tmp:
        d = Path(tmp)
        shutil.copyfile(TONE_JS, d / "Tone.js")
        (d / "score.js").write_text(code)
        (d / "index.html").write_text(HARNESS)
        started = time.time()
        out = await _run_in_chrome(d / "index.html", seconds)
        if out.get("error"):
            return {"ok": False, "error": out["error"].replace(d.as_uri() + "/", "")[:1500]}
        raw = d / "raw.wav"
        raw.write_bytes(base64.b64decode(out["wav"]))
        took = time.time() - started
        dest.parent.mkdir(parents=True, exist_ok=True)
        stats = await _measure(raw)
        if stats["silent"]:
            return {"ok": False, "error": "The score rendered silence — check that instruments connect to the "
                                          "destination (`.toDestination()`) and notes are scheduled inside the length."}
        # Even loudness for every score, so the mix under the voice is predictable.
        await render._ffmpeg("-i", raw, "-af", "loudnorm=I=-16:TP=-1.5:LRA=11", "-ar", str(SAMPLE_RATE), dest)
    spec = dest.with_suffix(".png")
    await render._ffmpeg("-i", dest, "-lavfi", "aformat=channel_layouts=mono,showspectrumpic=s=1200x300:legend=1:"
                         "fscale=lin:stop=8000:color=intensity:scale=log", spec)
    return {"ok": True, "seconds": round(await render.media_duration(dest), 2), "render_s": round(took, 1), **stats,
            "spectrogram": spec.name}


async def _measure(wav: Path) -> dict:
    """Loudness per second, quiet stretches and clipping of the raw render."""
    proc = await asyncio.create_subprocess_exec(
        render.FFMPEG, "-hide_banner", "-i", str(wav), "-af",
        "volumedetect,asetnsamples=n=44100:p=0,astats=metadata=1:reset=1,ametadata=print:key=lavfi.astats.Overall.RMS_level",
        "-f", "null", "-", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    _, err = await proc.communicate()
    log = err.decode(errors="replace")
    peak = float(m.group(1)) if (m := re.search(r"max_volume: (-?[\d.]+) dB", log)) else -99.0
    mean = float(m.group(1)) if (m := re.search(r"mean_volume: (-?[\d.]+) dB", log)) else -99.0
    rms = [float(x) if x not in ("-inf", "inf") else -99.0
           for x in re.findall(r"RMS_level=(-?inf|-?[\d.]+)", log)]
    per_second = [round(max(-60.0, r)) for r in rms]
    return {"silent": mean < -60, "peak_db": peak, "mean_db": mean, "clipping": peak >= -0.1,
            "loudness_per_second_db": per_second,
            "quiet_seconds": [i for i, r in enumerate(per_second) if r <= -40]}


def report_text(r: dict, path: str) -> str:
    """The tool's answer to Claude."""
    if not r["ok"]:
        return f"✗ compose_music failed:\n{r['error']}\nFix the score and call compose_music again."
    lines = [f"✓ composed → {path} ({r['seconds']}s, rendered in {r['render_s']}s, normalized to -16 LUFS)",
             f"Raw mix: peak {r['peak_db']} dB, mean {r['mean_db']} dB" + (" — CLIPPING: lower the gains" if r["clipping"] else ""),
             "Loudness per second (dB RMS): " + " ".join(map(str, r["loudness_per_second_db"]))]
    if r["quiet_seconds"]:
        lines.append(f"Near-silent seconds: {r['quiet_seconds']} — fine for a deliberate break, otherwise fill them.")
    lines.append(f"Spectrogram: {path.rsplit('/', 1)[0]}/{r['spectrogram']} (Read it to check the arrangement: "
                 "where the energy builds, drops and hits).")
    return "\n".join(lines)


async def for_quick(meta: dict, req: dict, pid: str) -> dict | None:
    """Quick engine: Claude writes the score in one call (one retry with the error), rendered and added to the music
    library. Scene times are estimated from the words, since the voice is recorded later."""
    from . import audio, claude

    total = float(req.get("duration", 45))
    words = [max(1, len(str(s.get("text", "")).split())) for s in meta.get("scenes", [])]
    starts, t = [], 0.0
    for w in words:
        starts.append(round(t, 1))
        t += w / sum(words) * total
    no_voice = req.get("voice") == "none"
    brief = "\n".join([
        f"Compose the background music for this {round(total)}-second video: “{meta.get('title', '')}”.",
        f"Mood: {meta.get('music') or req.get('style') or 'fits the topic'}.",
        "Scenes start at about (seconds): " + ", ".join(map(str, starts)) + ".",
        "Nobody speaks: the music carries the video, so melodies and hooks are welcome." if no_voice
        else "A voice-over runs over the whole video: keep the music under it.",
        "", GUIDE.replace("After each render, read the report: fix errors and clipping, fill unintended quiet seconds, and "
                          "Read the\n  spectrogram to check that energy follows the scenes. At most 2 revisions.",
                          "Make sure it runs without errors and never clips (keep the bus limiter).")])
    dest = audio.MUSIC_DIR / f"composed_{pid}.wav"
    error = ""
    for _ in range(2):
        code = await claude.compose_score(brief + (f"\n\nThe last score failed:\n{error}\nFix it." if error else ""),
                                          project_id=pid, model=req.get("model"))
        r = await render_score(code, total + 0.5, dest)
        if r["ok"]:
            dest.with_suffix(".png").unlink(missing_ok=True)
            try:
                return await audio.music_add(dest, f"Composed for “{meta.get('title', '')[:60]}”",
                                             mood=", ".join(str(meta.get("music") or "").split(",")[:2])[:60],
                                             source="composed")
            finally:
                dest.unlink(missing_ok=True)
        error = r["error"]
    return None
