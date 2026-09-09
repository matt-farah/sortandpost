"""Discover candidate image files under the configured library roots.

Pure filesystem walk -- no DB, no decode. Deterministic order (sorted) so a
killed-and-resumed scan revisits paths in the same sequence. Hidden entries and
symlinked directories are skipped; a missing root is silently ignored (hard
constraint #8: the library may be partially offline).
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from pathlib import Path

from sortandpost.metadata import EMBED_CAPABLE_SUFFIXES, RAW_SUFFIXES

# Everything the scanner will hand to the pipeline. RAW is included (read-only);
# a few extra decodable-but-not-embed-capable formats round it out.
IMAGE_SUFFIXES = EMBED_CAPABLE_SUFFIXES | RAW_SUFFIXES | frozenset({".webp", ".gif", ".bmp"})


def iter_image_files(
    roots: Iterable[Path | str],
    *,
    exclude: Iterable[Path | str] = (),
) -> Iterator[Path]:
    """Yield image files under *roots*, depth-first, names sorted at each level.

    *exclude* is a set of directory paths to prune (typically ``APP_DATA`` so the
    generated ``thumbnails/`` tree is never re-ingested).
    """
    pruned = {Path(e).resolve() for e in exclude}
    for root in roots:
        root_path = Path(root)
        if root_path.is_dir():
            yield from _walk(root_path, pruned)


def _walk(directory: Path, pruned: set[Path]) -> Iterator[Path]:
    try:
        entries = sorted(directory.iterdir(), key=lambda p: p.name)
    except OSError:
        return
    for entry in entries:
        if entry.name.startswith("."):
            continue
        if entry.is_symlink():
            continue
        if entry.is_dir():
            if entry.resolve() in pruned:
                continue
            yield from _walk(entry, pruned)
        elif entry.is_file() and entry.suffix.lower() in IMAGE_SUFFIXES:
            yield entry
