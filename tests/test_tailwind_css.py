"""The app runs NiceGUI with ``tailwind=False`` and serves a prebuilt stylesheet
(``src/sortandpost/resources/tailwind.css``, from ``tools/tailwind/build.sh``):
the in-browser compiler rescanned every element on each DOM mutation and froze
the infinite-scroll grid. These keep that committed file honest.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import nicegui
import pytest

ROOT = Path(__file__).resolve().parents[1]
CSS = ROOT / "src" / "sortandpost" / "resources" / "tailwind.css"
TOOL = ROOT / "tools" / "tailwind"


def _version(text: str) -> str:
    m = re.search(r"tailwindcss v(\d+\.\d+\.\d+)", text)
    assert m, "no tailwindcss version banner"
    return m.group(1)


def test_prebuilt_css_matches_nicegui_tailwind_version() -> None:
    """Same Tailwind release NiceGUI would have run in the browser -- after a
    NiceGUI upgrade, bump tools/tailwind/package.json and rebuild."""
    bundled = Path(nicegui.__file__).parent / "static" / "tailwindcss.min.js"
    m = re.search(r'"(4\.\d+\.\d+)"', bundled.read_text(encoding="utf-8"))
    assert m, "can't find NiceGUI's bundled Tailwind version"
    assert _version(CSS.read_text(encoding="utf-8")) == m.group(1)


def test_prebuilt_css_is_current(tmp_path: Path) -> None:
    """Rebuilding from the current sources gives the committed file byte-for-byte,
    i.e. nobody added a class in src/ without running tools/tailwind/build.sh."""
    cli = TOOL / "node_modules" / ".bin" / "tailwindcss"
    if not cli.exists() or shutil.which("node") is None:
        pytest.skip("Tailwind CLI not installed (cd tools/tailwind && npm ci)")
    out = tmp_path / "tailwind.css"
    subprocess.run(
        [str(cli), "-i", "input.css", "-o", str(out), "--minify"],
        cwd=TOOL,
        check=True,
        capture_output=True,
    )
    assert out.read_bytes() == CSS.read_bytes(), "stale -- run tools/tailwind/build.sh"
