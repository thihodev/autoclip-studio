# Contributing to AutoClip Studio

Thanks for helping! Bug reports, ideas, translations, new visual styles and video types, and code are all welcome.

## How contributions work

`main` is protected: nobody pushes to it directly. Every change goes through a pull request.

1. **Open an issue first** for anything bigger than a small fix, so we can agree on the approach before you spend time on it.
2. **Fork** the repository (maintainers: create a branch in the repo instead).
3. **Create a branch** from an up-to-date `main`: `feat/short-description`, `fix/short-description`, `docs/…`, `i18n/…`.
4. Make focused commits (see *Commits* below). Keep one topic per pull request.
5. **Open a pull request** against `main` and fill in the template: what changed, why, and how you tested it (screenshots or a short screen recording for UI changes).
6. CI must pass and a maintainer must approve. Address review comments with new commits; the maintainer squash-merges.

Never commit secrets, API keys, tokens, `.env` files or anything under `data/`.

## Development setup

```bash
git clone https://github.com/<you>/autoclip-studio.git && cd autoclip-studio
./start.sh --no-browser          # creates .venv, installs requirements, runs on http://localhost:8000
```

Use a separate data folder for experiments so you never touch your real videos: `DATA_DIR=/tmp/autoclip-dev PORT=8765 ./start.sh --no-browser`.

Before opening a pull request, run the same checks as CI:

```bash
.venv/bin/python -m compileall -q app && .venv/bin/python -c "import app.main"
node -e "const s=require('fs').readFileSync('static/index.html','utf8');for(const m of s.matchAll(/<script>([\s\S]*?)<\/script>/g))new Function(m[1])"
python3 -c "import json;json.load(open('static/locales/vi.json'))"
```

Then try your change in the browser (both light and dark themes, English and Vietnamese, and a narrow window for layout changes). Changes that run Claude, render videos or publish should be tested end to end with your own accounts; say in the PR what you ran.

## Conventions

- **English everywhere in the source**: code, comments, commit messages, UI strings, docs. The UI is translated through `static/locales/<lang>.json` (English text is the key). Vietnamese lives only in `static/locales/vi.json`.
- **No pre-configuration**: every connection is made from the web UI. Don't add required environment variables or config files.
- **Never pick a Claude account for the user**: the user chooses (sign-in, this computer's login or an API key).
- **Never write outside `DATA_DIR`** at runtime — in particular not into the user's global agent folders (`~/.claude/skills`, `~/.agents/skills`, …).
- **Keep it simple to run**: Python + FastAPI backend, one-file Alpine.js UI (`static/index.html`), ffmpeg through imageio-ffmpeg. New dependencies need a good reason; mention it in the PR.
- **Match the surrounding code**: naming, comment density, small functions, docstrings that say *why*.
- **Design**: follow the existing components (cards, Claude-style menus via `x-select`, galleries, dialogs). Use the CSS color tokens, so light and dark themes keep working.
- **Claude usage costs the user money or plan limits**: prefer cheap models and low effort for small tasks, keep prompts and agent context small, never add hidden extra runs.

## Commits

Write the subject in the imperative, ≤ 72 characters ("Add music library", "Fix cover text wrapping"), then a short body explaining what and why. Reference the issue (`Fixes #123`).

## Common contributions

- **A language**: copy `static/locales/vi.json` to `static/locales/<code>.json`, translate the values, and add the language to the switcher in `static/index.html`.
- **A visual style**: add a folder under `app/assets/styles/<source>/<id>/` with `style.json` (see the FrameJam ones for the fields) and a `poster.jpg`; include the license if it comes from another project.
- **A video type (playbook)**: add `app/assets/playbooks/autoclip/<id>/` with `playbook.json` and a short `SKILL.md` recipe (hook, beats, pacing, ending).
- **A publishing platform**: implement a `Platform` subclass in `app/platforms.py` (OAuth + publish) and register it.

## Reporting bugs and security issues

Use the issue templates. For security problems (for example a way to read files or tokens through the API), please don't open a public issue: use GitHub's *Report a vulnerability* (Security tab) so it can be fixed first.

## License

By contributing you agree that your contributions are licensed under the [MIT License](LICENSE).
