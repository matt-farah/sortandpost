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
    exclude_names: Iterable[str] = (),
) -> Iterator[Path]:
    """Yield image files under *roots*, depth-first, names sorted at each level.

    *exclude* is a set of directory paths to prune (typically ``APP_DATA`` so the
    generated ``thumbnails/`` tree is never re-ingested). *exclude_names* prunes
    any directory whose name matches (case-insensitive) at any depth -- e.g.
    ``["RAW", "af"]``.
    """
    pruned = {Path(e).resolve() for e in exclude}
    pruned_names = {n.strip().casefold() for n in exclude_names if n.strip()}
    for root in roots:
        root_path = Path(root)
        if root_path.is_dir():
            yield from _walk(root_path, pruned, pruned_names)


def collapse_scope(scope: Iterable[Path | str], roots: Iterable[Path | str]) -> list[Path] | None:
    """Reduce a set of changed paths to the minimal list a scoped scan walks.

    Paths outside every root are dropped, and a path under another one in the
    set is covered by it. Returns ``None`` when a path *is* (or contains) a root:
    that is a full scan. The result is sorted, so a scoped scan is deterministic.
    """
    root_paths = [Path(r) for r in roots]
    kept: list[Path] = []
    for path in sorted({Path(p) for p in scope}):
        if any(r.is_relative_to(path) for r in root_paths):
            return None
        if not any(path.is_relative_to(r) for r in root_paths):
            continue
        if kept and path.is_relative_to(kept[-1]):
            continue  # sorted order puts an ancestor right before its descendants
        kept.append(path)
    return kept


def iter_scoped_files(
    roots: Iterable[Path | str],
    scope: Iterable[Path],
    *,
    exclude: Iterable[Path | str] = (),
    exclude_names: Iterable[str] = (),
) -> Iterator[Path]:
    """Like :func:`iter_image_files`, but only under *scope* (from
    :func:`collapse_scope`): a directory is walked, a file is yielded on its
    own, a path that no longer exists yields nothing. The same pruning applies,
    including to the folders between a root and a scope path -- a change inside
    ``<root>/RAW/`` stays excluded when ``RAW`` is an excluded name."""
    root_paths = [Path(r) for r in roots]
    pruned = {Path(e).resolve() for e in exclude}
    pruned_names = {n.strip().casefold() for n in exclude_names if n.strip()}
    for path in scope:
        root = next((r for r in root_paths if path.is_relative_to(r)), None)
        if root is None or not _scope_allowed(root, path, pruned, pruned_names):
            continue
        try:
            if path.is_symlink() or path.is_junction():
                continue
            is_dir = path.is_dir()
            is_file = path.is_file()
        except OSError:
            continue
        if is_dir:
            yield from _walk(path, pruned, pruned_names)
        elif is_file and path.suffix.lower() in IMAGE_SUFFIXES:
            yield path


def _scope_allowed(root: Path, path: Path, pruned: set[Path], pruned_names: set[str]) -> bool:
    parts = path.relative_to(root).parts
    if any(p.startswith(".") for p in parts):
        return False
    # every folder between the root and the path (the last part is the path
    # itself -- a folder name there is pruned only when it is a folder)
    if any(p.casefold() in pruned_names for p in parts[:-1]):
        return False
    try:
        if parts and parts[-1].casefold() in pruned_names and path.is_dir():
            return False
        resolved = path.resolve()
    except OSError:
        return False
    return not any(resolved.is_relative_to(p) for p in pruned)


def _walk(directory: Path, pruned: set[Path], pruned_names: set[str]) -> Iterator[Path]:
    try:
        entries = sorted(directory.iterdir(), key=lambda p: p.name)
    except OSError:
        return
    for entry in entries:
        if entry.name.startswith("."):
            continue
        try:
            # A Windows junction is a directory link that isn't a symlink; one
            # pointing back up the tree would make the walk endless.
            if entry.is_symlink() or entry.is_junction():
                continue
            is_dir = entry.is_dir()
            is_file = entry.is_file()
            resolved = entry.resolve() if is_dir else None
        except OSError:
            # unreadable entry (permission denied, a macOS data-vault, a broken
            # mount point). Skip it -- one bad path must not abort the whole scan.
            continue
        if is_dir:
            if resolved in pruned or entry.name.casefold() in pruned_names:
                continue
            yield from _walk(entry, pruned, pruned_names)
        elif is_file and entry.suffix.lower() in IMAGE_SUFFIXES:
            yield entry
