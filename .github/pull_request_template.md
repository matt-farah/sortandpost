## What and why

<!-- What does this change, and what problem does it solve? Link the issue if there is one. -->

## How it was tested

<!-- Tests added / run, and what you clicked through in the browser. -->

## Checklist

- [ ] `uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv run pytest` pass
- [ ] Tried it hands-on in the browser
- [ ] Respects the hard constraints in [docs/architecture.md](https://github.com/matt-farah/sortandpost/blob/main/docs/architecture.md) (photo files untouched except metadata writes, RAW read-only, new user state logged to `changes`, no blocking work on the event loop)
- [ ] Updated `docs/architecture.md` if a design decision changed
