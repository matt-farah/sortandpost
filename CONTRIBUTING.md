# Contributing to sortandpost.photos

Thanks for helping out! Bug reports, fixes and ideas are all welcome. For anything bigger
than a small fix, please open an issue first so we can agree on the approach before you
spend time on it.

## Before you start: the design

Read [docs/architecture.md](docs/architecture.md), at least **Hard constraints**. A few
of them shape almost every change:

- **Never modify the user's photo files or folders.** The only exceptions are writing rating
  / keyword metadata (via `sortandpost.metadata`, the one module allowed to write under a
  library folder) and the explicit, confirmed "Delete photos" action. RAW files are never
  written to.
- **A photo's identity is its pixel hash**, never its path.
- **Any new user-authored state must also write a `changes` row**, or it will be lost when
  two computers' copies are merged.
- **Never block the web event loop** with image decoding, CLIP or exiftool. CPU work goes
  through the process pool; blocking I/O through `run.io_bound`.
- **The indexer must survive being killed at any moment** and pick up where it left off.

## Dev setup

You need [uv](https://docs.astral.sh/uv/) and [exiftool](https://exiftool.org/)
(`brew install exiftool`, `apt install libimage-exiftool-perl`, or `winget install exiftool`).
Python 3.12 or 3.13; uv installs the pinned one from `.python-version` for you.

```bash
uv sync --locked --extra dev --extra desktop   # the venv exactly matches uv.lock
uv run python -m sortandpost                   # http://localhost:8080
```

The first run opens a setup wizard. To keep your dev library apart from a real one, point the
app at a throwaway folder: `APP_DATA=/tmp/sp-dev uv run python -m sortandpost`. CLIP weights
(~600 MB) download on the first scan. To try a fresh first run without touching your own
settings, also set `SORTANDPOST_PREF_DIR` and `SORTANDPOST_LOCAL_DIR` to empty folders.

## Checks

CI runs all of these on every pull request; please run them before pushing:

```bash
uv run ruff check .
uv run ruff format --check .     # `uv run ruff format .` to fix
uv run mypy src
uv run pytest
uv audit
```

- Tests that need exiftool, Playwright's Chromium (`uv run playwright install chromium`) or
  sample RAW files skip themselves when those aren't there.
- If you change a database model, add a migration and check that autogenerate comes out
  empty afterwards: `uv run alembic revision --autogenerate -m "describe it"`.
- If you add or change Tailwind classes in the UI, rebuild the CSS with
  `tools/tailwind/build.sh` (needs Node) and commit `src/sortandpost/resources/tailwind.css`.
- UI colours come from the theme tokens (`sp-muted`, `color=primary`, …), not hard-coded
  colours, so every seasonal theme keeps working. `tests/test_themes.py` checks contrast.

## Pull requests

- Keep each PR to one change, with a test where it makes sense.
- **Click through your change in the browser.** Automated tests aren't enough for UI work.
- If your change alters a design decision, update `docs/architecture.md` in the same PR.
- Write user-facing text plainly: short sentences, no jargon.

## Repository map

See the "Repository layout" table in the [README](README.md#repository-layout).
`dev-notes/` is the project's planning history (roadmaps, progress log, issue notes). It's
kept for context, but it isn't documentation you need to read.

## License

By contributing, you agree that your contributions are licensed under the
[MIT License](LICENSE).
