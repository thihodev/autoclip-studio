"""GreenSock's official GSAP skills for Claude Motion.

HyperFrames animates every frame with a paused GSAP timeline and documents the core API itself, but not GSAP's
plugins (SplitText, DrawSVG, MorphSVG, ScrambleText, CustomEase…), which are what make motion graphics feel
designed. The skills (MIT, github.com/greensock/gsap-skills) are bundled in app/assets/skills/gsap — only the
parts that fit a seek-rendered video, not React, framework or scroll ones — and copied into each project.
"""
import shutil
from pathlib import Path

GSAP_VERSION = "3.14.2"  # what HyperFrames' templates load; plugins must match it
SKILLS_SRC = Path(__file__).parent / "assets" / "skills" / "gsap"
SKILLS = ["gsap-core", "gsap-timeline", "gsap-plugins", "gsap-utils", "gsap-performance"]


def install(root: Path):
    """Copy the skills into the project's .claude/skills, where the agent finds them."""
    dest = root / ".claude" / "skills"
    dest.mkdir(parents=True, exist_ok=True)
    for name in SKILLS:
        if (SKILLS_SRC / name).is_dir() and not (dest / name).exists():
            shutil.copytree(SKILLS_SRC / name, dest / name)


def brief_lines() -> list[str]:
    cdn = f"https://cdn.jsdelivr.net/npm/gsap@{GSAP_VERSION}/dist"
    return [
        "", "## GSAP toolkit",
        "GreenSock's official GSAP skills are in `.claude/skills/` (" + ", ".join(f"`{s}`" for s in SKILLS) + "). "
        "HyperFrames' rules come first: one paused timeline per frame, seek-safe, entrances with `fromTo`, "
        "no `repeat`/`yoyo`, no randomness.",
        "- Every GSAP plugin is free. In Step 4, use one where it makes a shot better and name it in the Scene line "
        "(e.g. \"headline words rise in — SplitText words, stagger 0.06\"), so the frame worker builds it:",
        "  - SplitText: per-word or per-character reveals of a headline. Split synchronously when the frame script "
        "runs (`type: \"words\"` or `\"words, chars\"`), never `autoSplit`/`onSplit`; no `mask` for Vietnamese "
        "(the clip cuts off diacritics).",
        "  - ScrambleText: a number or label decoding into place (short, under a second).",
        "  - DrawSVG: lines, arrows, outlines, chart axes and underlines drawing on.",
        "  - MorphSVG: one icon or shape turning into another.",
        "  - MotionPath: an object travelling along a curve.",
        "  - CustomEase: one signature ease reused across the video.",
        "  - `gsap.utils.distribute`, `interpolate`, `mapRange`: deterministic staggers, counters and charts "
        "(never `utils.random`/`shuffle`).",
        f"- Load a plugin inside the frame's `<template>`, right after GSAP and at the same version "
        f"(`<script src=\"{cdn}/SplitText.min.js\"></script>`), then `gsap.registerPlugin(SplitText)`. The worker "
        "checks the API in that skill's section (grep the heading, read only that part).",
        "- Not for a rendered video (real-time or input-driven): ScrollTrigger, ScrollSmoother, ScrollTo, Draggable, "
        "Inertia, Observer, Flip, GSDevTools, `gsap.matchMedia`, `gsap.ticker`, `quickTo`.",
    ]
