"""Thumbnail generation (pipeline stage 4).

Thumbnails are content-addressed by ``pixel_hash`` and write-once: two machines
produce byte-identical files, so ``sync/thumbnails/`` needs no conflict handling.
Each file is written to a temp name and ``os.replace``-d into place so a killed
process never leaves a truncated thumbnail behind (hard constraint #6).

The EXIF orientation flag *is* baked in here (unlike the pixel hash) so the grid
shows photos the right way up.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from pathlib import Path

from PIL import Image, ImageOps

ThumbPathFn = Callable[[str, int, str], Path]


def write_thumbnails(
    img: Image.Image,
    pixel_hash: str,
    *,
    sizes: Sequence[int],
    thumb_path: ThumbPathFn,
    fmt: str = "webp",
    quality: int = 80,
) -> tuple[int, ...]:
    """Write any missing thumbnails for *pixel_hash*; return the sizes written.

    *thumb_path* maps ``(pixel_hash, size, fmt) -> Path`` (normally
    ``Paths.thumbnail_file``). Existing files are left untouched.
    """
    oriented = ImageOps.exif_transpose(img) or img
    base = oriented.convert("RGB")
    written: list[int] = []
    for size in sizes:
        dest = thumb_path(pixel_hash, size, fmt)
        if dest.exists():
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        scaled = base.copy()
        scaled.thumbnail((size, size), Image.Resampling.LANCZOS)
        tmp = dest.with_name(f"{dest.name}.tmp-{os.getpid()}")
        try:
            scaled.save(tmp, fmt.upper(), quality=quality, method=6)
            os.replace(tmp, dest)
        finally:
            tmp.unlink(missing_ok=True)
        written.append(size)
    return tuple(written)
