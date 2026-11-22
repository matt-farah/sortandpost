"""Layer 3 -- "basic functionality is the same": index one library natively (this
computer's venv) and in the container, then compare what each recorded.

The pixel hash must match (constraint #3: if Linux decoded a HEIC / RAW / PNG
differently, one photo would become two the moment a second machine indexed it), and so
must everything read from the file. Thumbnails are content-addressed and write-once, and
docs/architecture.md says two machines write byte-identical files -- checked here too."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from tests.docker import harness
from tests.docker.library import build_library

# Columns that are expected to differ between two machines.
_LOCAL_ONLY = ("path", "indexed_at", "mtime", "missing_since", "id")


def _rows(db: Path, sql: str) -> list[tuple[Any, ...]]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def _photos(db: Path) -> dict[str, dict[str, Any]]:
    cols = [r[1] for r in _rows(db, "PRAGMA table_info(photos)")]
    keep = [c for c in cols if not any(part in c for part in _LOCAL_ONLY) or c == "pixel_hash"]
    out = {}
    for row in _rows(db, f"SELECT {', '.join(keep)} FROM photos"):
        rec = dict(zip(keep, row, strict=True))
        out[rec["pixel_hash"]] = rec
    return out


def _thumbs(sync: Path) -> dict[str, str]:
    return {
        str(p.relative_to(sync)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted((sync / "thumbnails").rglob("*.webp"))
    }


def _index_natively(root: Path, library: Path) -> Path:
    """`python -m sortandpost.indexer --once` on this computer, single-folder layout."""
    app = root / "native"
    (app / "local").mkdir(parents=True)
    (app / "sync").mkdir()
    config = json.loads(json.dumps(harness.TEST_CONFIG))
    config["library_roots"] = [str(library)]
    (app / "sync" / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    # An existing device + finished wizard: roots seed from config.yaml, no wizard.
    (app / "local" / "device.json").write_text(
        json.dumps({"device_id": "native-parity-0001", "hostname": "native"})
    )
    (app / "local" / "onboarding.json").write_text(json.dumps({"how": "new"}))
    env = {**os.environ, "APP_DATA": str(app), "HF_HUB_OFFLINE": "1"}
    proc = subprocess.run(
        [sys.executable, "-m", "sortandpost.indexer", "--once"],
        env=env,
        cwd=root,  # never the repo, whose .env may name a real library
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    return app


def test_native_and_docker_index_the_same(make_stack: Callable[..., harness.Stack]) -> None:
    stack = make_stack()
    native_lib = stack.root.parent / "native-library"
    lib = build_library(native_lib)
    shutil.rmtree(stack.library)
    shutil.copytree(native_lib, stack.library)  # same bytes, same mtimes

    native = _index_natively(stack.root.parent, native_lib)
    stack.compose("run", "--rm", "--no-deps", "indexer", "--once", timeout=900)

    n_db = native / "local" / "sortandpost.db"
    d_db = stack.local / "sortandpost.db"
    native_photos, docker_photos = _photos(n_db), _photos(d_db)
    assert len(native_photos) == len(lib.files) - 1  # the duplicate is one photo
    assert sorted(native_photos) == sorted(docker_photos), "pixel hashes differ"
    for ph, rec in native_photos.items():
        assert docker_photos[ph] == rec, f"{ph[:12]} differs: native {rec} vs docker"

    ratings = "SELECT pixel_hash, stars FROM ratings ORDER BY 1"
    assert _rows(n_db, ratings) == _rows(d_db, ratings)
    if "rated" in lib.files:
        assert any(stars == 3 for _, stars in _rows(d_db, ratings)), "XMP rating not read"

    paths = "SELECT COUNT(*), SUM(present) FROM photo_paths"
    assert _rows(n_db, paths) == _rows(d_db, paths)

    native_thumbs, docker_thumbs = _thumbs(native / "sync"), _thumbs(stack.sync)
    assert sorted(native_thumbs) == sorted(docker_thumbs), "a different set of thumbnails"
    differ = [k for k in native_thumbs if native_thumbs[k] != docker_thumbs[k]]
    assert not differ, f"{len(differ)} thumbnails aren't byte-identical: {differ[:3]}"
