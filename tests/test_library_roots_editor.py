"""The library-folders editor (Settings → Library and the welcome wizard, V3.md #12): a
folder that isn't there says so on its row, and a visible Remove takes it off the list
(``library.json``) without touching anything on disk."""

from __future__ import annotations

from pathlib import Path

from nicegui import ui
from nicegui.testing.user_simulation import user_simulation
from sqlalchemy.engine import Engine

from sortandpost.app import _library_roots_editor
from sortandpost.config import AppConfig, Env, Settings
from sortandpost.library_roots import LibraryRoot, load_roots, save_roots
from sortandpost.paths import Paths


async def test_missing_folder_is_flagged_and_removable(
    paths: Paths, engine: Engine, tmp_path: Path
) -> None:
    real = tmp_path / "photos"
    real.mkdir()
    gone = tmp_path / "typo-photos"
    roots = [LibraryRoot(container=str(real), host=str(real)), LibraryRoot(str(gone), str(gone))]
    save_roots(paths, "dev-a", "a", roots)
    settings = Settings(env=Env(), paths=paths, config=AppConfig(), device_id="dev-a", hostname="a")
    settings.library_roots = list(roots)

    def root() -> None:
        _library_roots_editor(settings, engine, rescan=False)

    async with user_simulation(root) as user:
        await user.open("/")
        await user.should_see("Not found on this computer")
        assert len(user.find(kind=ui.label, content="Not found on this computer").elements) == 1
        assert len(user.find(kind=ui.button, content="Remove").elements) == 2

        remove = user.find(kind=ui.button, content="Remove")
        remove.elements = {b for b in remove.elements if b.props.get("color") == "warning"}
        assert len(remove.elements) == 1  # only the missing row's button stands out
        remove.click()

        await user.should_not_see("Not found on this computer")
        await user.should_see("its files are untouched")

    assert [r.container for r in load_roots(paths, "dev-a", "a")] == [str(real)]
    assert [r.container for r in settings.library_roots] == [str(real)]
    assert real.is_dir()
