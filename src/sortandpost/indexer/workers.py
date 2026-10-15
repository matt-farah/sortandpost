"""CPU-bound per-file work that runs in a ``ProcessPoolExecutor`` (docs/architecture.md hard
constraint #7: "all CPU-bound work goes through the process pool").

A worker decodes one file, computes its ``pixel_hash`` + perceptual ``dhash``,
and writes its (content-addressed, write-once) thumbnails -- everything that does
**not** need the database or the persistent ``exiftool`` subprocess. The main
process takes the returned :class:`DecodedFile` and does the DB upserts +
embedded-metadata reconcile serially (see
:func:`sortandpost.indexer.pipeline.persist_decoded`).

Nothing here holds a DB session, an engine, or an exiftool handle, so a
:class:`DecodedFile` is cheap to pickle back to the parent and a worker can be a
``spawn``-ed process with no shared state.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from sortandpost.dedup import dhash
from sortandpost.hashing import pixel_hash_of_image
from sortandpost.indexer.decode import DecodeError, load_image
from sortandpost.indexer.placeholder import is_online_only
from sortandpost.indexer.thumbnails import ThumbPathFn, write_thumbnails
from sortandpost.metadata import is_raw
from sortandpost.paths import Paths


@dataclass
class DecodedFile:
    """Result of the worker-side stages 2-4 for one file. ``error`` set => the
    file failed at ``failed_stage`` ("decode" or "thumbnail"); ``pixel_hash`` is
    still populated for a thumbnail failure so the DB row survives."""

    path: str
    size: int
    mtime_ns: int
    pixel_hash: str | None = None
    width: int = 0
    height: int = 0
    fmt: str = ""
    is_raw: bool = False
    phash: str | None = None
    thumbnails_written: tuple[int, ...] = ()
    error: str | None = None
    failed_stage: str | None = None  # "decode" | "thumbnail"
    online_only: bool = False  # a cloud placeholder we skipped (not an error)


@dataclass(frozen=True)
class DecodeJob:
    """Immutable, picklable bundle of everything a worker needs. No handles."""

    local_dir: str
    sync_dir: str
    thumb_sizes: tuple[int, ...]
    thumb_fmt: str
    thumb_quality: int
    online_only_mode: str = "skip"  # "skip" defers cloud placeholders; "download" decodes them


def decode_local(
    path: Path,
    *,
    thumb_path_fn: ThumbPathFn,
    sizes: tuple[int, ...],
    fmt: str,
    quality: int,
    online_only_mode: str = "skip",
) -> DecodedFile:
    """Run stages 2-4 (decode, pixel hash + dhash, thumbnails) for one file.

    Used directly on the main thread for the serial path and small scans; the
    parallel path calls :func:`decode_file` in a worker, which wraps this.

    When ``online_only_mode == "skip"`` and *path* is a cloud placeholder whose
    bytes are not local (:func:`sortandpost.indexer.placeholder.is_online_only`),
    it returns early with ``online_only=True`` and no decode -- so a first scan
    of a dehydrated library does not pull every file down.
    """
    key = str(path)
    try:
        st = os.stat(path)
    except OSError as exc:
        return DecodedFile(
            path=key, size=0, mtime_ns=0, error=f"stat: {exc}", failed_stage="decode"
        )

    if online_only_mode == "skip" and is_online_only(st):
        return DecodedFile(
            path=key,
            size=st.st_size,
            mtime_ns=st.st_mtime_ns,
            is_raw=is_raw(path),
            online_only=True,
        )

    out = DecodedFile(path=key, size=st.st_size, mtime_ns=st.st_mtime_ns, is_raw=is_raw(path))
    try:
        img = load_image(path)
    except DecodeError as exc:
        out.error = str(exc)
        out.failed_stage = "decode"
        return out

    try:
        out.width, out.height = img.size
        out.fmt = (img.format or path.suffix.lstrip(".").upper() or "UNKNOWN").upper()
        out.pixel_hash = pixel_hash_of_image(img)
        out.phash = dhash(img)
        try:
            out.thumbnails_written = write_thumbnails(
                img,
                out.pixel_hash,
                sizes=sizes,
                thumb_path=thumb_path_fn,
                fmt=fmt,
                quality=quality,
            )
        except OSError as exc:
            out.error = str(exc)
            out.failed_stage = "thumbnail"
    finally:
        img.close()
    return out


def decode_file(job: DecodeJob, path_str: str) -> DecodedFile:
    """Worker-process entry point. Rebuilds the thumbnail path function from
    the job's roots so nothing unpicklable crosses the process boundary."""
    thumbs = Paths.split(local=Path(job.local_dir), sync=Path(job.sync_dir))
    return decode_local(
        Path(path_str),
        thumb_path_fn=thumbs.thumbnail_file,
        sizes=job.thumb_sizes,
        fmt=job.thumb_fmt,
        quality=job.thumb_quality,
        online_only_mode=job.online_only_mode,
    )
