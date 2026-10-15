# CLAUDE.md — rules for AI coding agents working on AutoClip Studio

Read [CONTRIBUTING.md](CONTRIBUTING.md) first; everything there applies. These rules are the short, non-negotiable version.

## Git workflow

- **Never push to `main`** and never force-push any shared branch. `main` is protected; changes land only through pull requests.
  The only exception is the repository owner telling their own agent, in their own session, to push to `main`.
- Work on a branch named `feat/…`, `fix/…`, `docs/…` or `i18n/…`, created from an up-to-date `main`.
- Open a pull request with `gh pr create` and fill in the template (what, why, how it was tested). Don't merge your own pull request; a maintainer reviews and merges.
- Don't rewrite history that others may have pulled; add commits instead.
- Commit only what the change needs. Never commit secrets, API keys, tokens, `.env`, `data/`, `.venv/`, renders or test output.
- Commit messages: imperative subject ≤ 72 characters, a body that explains what and why.

## Code

- English only in source code, comments, commit messages, UI strings and docs. Vietnamese (and other languages) only in `static/locales/<lang>.json`; English text is the translation key, so add the translation for every new UI string.
- No required configuration: every connection is set up from the web UI. Don't add mandatory env vars or config files.
- Never choose a Claude account for the user, and never write outside `DATA_DIR` at runtime — especially not into global agent folders such as `~/.claude/skills` or `~/.agents/skills`.
- Keep the architecture: FastAPI backend in `app/`, the single-file Alpine.js UI in `static/index.html`, ffmpeg via imageio-ffmpeg. Ask before adding a dependency.
- Match the surrounding code's style, naming and comment density. Reuse existing UI pieces (cards, `x-select` menus, galleries, dialogs) and CSS tokens so light/dark themes and both languages keep working.
- Claude runs cost the user money or plan limits: keep prompts and agent context small, prefer cheaper models/lower effort for small tasks, and never add extra Claude runs without saying so in the PR.

## Before opening the pull request

- `.venv/bin/python -c "import app.main"` passes, `static/index.html`'s scripts parse, `static/locales/*.json` are valid JSON (CI runs these).
- Test with an isolated data folder (`DATA_DIR=/tmp/autoclip-dev PORT=8765 ./start.sh --no-browser`), never the user's real `data/`.
- Check UI changes in the browser (light/dark, English/Vietnamese, narrow window) and include screenshots in the PR.
- Update `README.md` / `docs/API.md` when behavior or endpoints change.
