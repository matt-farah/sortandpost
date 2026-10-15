"""Online-only files in ``sync/`` that this process can't read (Docker on a Mac / PC).

When the shared folder sits in OneDrive / Google Drive / iCloud and the sync
client frees up space, files become cloud placeholders. A native app opening one
makes the client download it, but **inside Docker Desktop the read fails with an
I/O error** -- the bind mount can't ask the host's sync client for the bytes
(checked 2026-10-14 with OneDrive on macOS). An online-only published DB or lock
would then break the handover, and an online-only thumbnail renders blank.

:func:`find_online_only` stats files (never opens them) with the indexer's
placeholder test, important files first, and the app turns a non-empty report
into a banner telling the user to keep the folder downloaded ("Always keep on
this device" / "Mirror files").
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from sortandpost.indexer.placeholder import is_online_only

# Checked first: what the sync layer must be able to read.
_IMPORTANT = ("sortandpost.db", ".sortandpost.lock", "config.yaml", "tags.yaml")
_IMPORTANT_DIRS = ("embeddings", "locks", "devices")


@dataclass
class OnlineOnlyReport:
    found: int = 0  # placeholders seen (stops counting at stop_after)
    checked: int = 0  # files stat'ed
    important: list[str] = field(default_factory=list)  # e.g. "sortandpost.db"
    examples: list[str] = field(default_factory=list)  # a few relative paths
    complete: bool = True  # False when the walk hit max_files / stop_after

    @property
    def any(self) -> bool:
        return self.found > 0


def find_online_only(
    sync_dir: Path, *, max_files: int = 50_000, stop_after: int = 500
) -> OnlineOnlyReport:
    report = OnlineOnlyReport()
    for path, important in _candidates(sync_dir):
        if report.checked >= max_files or report.found >= stop_after:
            report.complete = False
            break
        try:
            st = path.stat()
        except OSError:
            continue
        report.checked += 1
        if not is_online_only(st):
            continue
        report.found += 1
        rel = path.relative_to(sync_dir).as_posix()
        if important:
            report.important.append(rel)
        if len(report.examples) < 5:
            report.examples.append(rel)
    return report


def _candidates(sync_dir: Path) -> Iterator[tuple[Path, bool]]:
    for name in _IMPORTANT:
        yield sync_dir / name, True
    for d in _IMPORTANT_DIRS:
        for p in _walk(sync_dir / d):
            yield p, True
    skip = set(_IMPORTANT_DIRS)
    try:
        top = sorted(os.scandir(sync_dir), key=lambda e: e.name)
    except OSError:
        return
    for entry in top:
        if entry.is_dir(follow_symlinks=False) and entry.name not in skip:
            for p in _walk(Path(entry.path)):
                yield p, False
        elif entry.is_file(follow_symlinks=False) and entry.name not in _IMPORTANT:
            yield Path(entry.path), False


def _walk(root: Path) -> Iterator[Path]:
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            entries = sorted(os.scandir(d), key=lambda e: e.name)
        except OSError:
            continue
        for e in entries:
            if e.is_dir(follow_symlinks=False):
                stack.append(Path(e.path))
            elif e.is_file(follow_symlinks=False):
                yield Path(e.path)
