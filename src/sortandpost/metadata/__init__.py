"""The only module allowed to write under a photo library root.

Wraps a persistent ``exiftool -stay_open True`` process. Writes:

* rating  -> ``XMP-xmp:Rating`` (0-5; 0 clears), plus Windows/Lightroom compat
  tags when enabled
* approved tags -> ``XMP-dc:Subject`` (+ ``IPTC:Keywords``)

Never writes CLIP predictions or anything else. RAW paths are rejected here as a
safety net. Implemented in build-order step 2.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import TracebackType

RAW_SUFFIXES = frozenset(
    {".cr2", ".cr3", ".nef", ".arw", ".raf", ".rw2", ".orf", ".dng", ".pef", ".srw"}
)
EMBED_CAPABLE_SUFFIXES = frozenset({".jpg", ".jpeg", ".tif", ".tiff", ".png", ".heic", ".heif"})


@dataclass
class EmbeddedMetadata:
    rating: int | None = None
    keywords: tuple[str, ...] = ()
    taken_at: datetime | None = None
    orientation: int | None = None
    camera_make: str | None = None
    camera_model: str | None = None
    gps_lat: float | None = None
    gps_lon: float | None = None


class MetadataError(RuntimeError):
    pass


class ExiftoolClient:
    """Context manager around a long-lived exiftool process."""

    def __init__(self, *, write_windows_compat_tags: bool = True) -> None:
        self.write_windows_compat_tags = write_windows_compat_tags

    def __enter__(self) -> ExiftoolClient:
        raise NotImplementedError("build-order step 2: metadata engine")

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    def read(self, path: Path) -> EmbeddedMetadata:
        raise NotImplementedError("build-order step 2: metadata engine")

    def write_rating(self, path: Path, stars: int) -> None:
        raise NotImplementedError("build-order step 2: metadata engine")

    def write_keywords(self, path: Path, keywords: list[str]) -> None:
        raise NotImplementedError("build-order step 2: metadata engine")


def is_raw(path: Path) -> bool:
    return path.suffix.lower() in RAW_SUFFIXES


def can_embed_metadata(path: Path) -> bool:
    return path.suffix.lower() in EMBED_CAPABLE_SUFFIXES
