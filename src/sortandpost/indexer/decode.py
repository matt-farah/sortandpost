"""Decode a library file to a single PIL image (pipeline stage 2).

One open per file: the returned image is hashed *and* thumbnailed. RAW files use
the largest embedded preview for speed and determinism (a full demosaic only if
no usable preview exists) -- and, per hard constraint #2, are never written to.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, UnidentifiedImageError

from sortandpost.metadata import is_raw

try:  # HEIC/HEIF decode, only when the optional plugin is installed
    import pillow_heif

    pillow_heif.register_heif_opener()
except ModuleNotFoundError:  # pragma: no cover - optional dependency
    pass

try:  # RAW decode via LibRaw; absent in the lightweight test env
    import rawpy
except ModuleNotFoundError:  # pragma: no cover - optional dependency
    rawpy = None

# Camera files routinely exceed Pillow's default ~89 MPix bomb guard.
Image.MAX_IMAGE_PIXELS = 500_000_000

# Suffixes Pillow (+ plugins) can open directly.
_PIL_READABLE = frozenset(
    {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp", ".gif", ".bmp", ".heic", ".heif"}
)


class DecodeError(RuntimeError):
    """The file could not be turned into an image (corrupt, unsupported, truncated)."""


def load_image(path: Path) -> Image.Image:
    """Return a decoded :class:`PIL.Image.Image` for *path*.

    The image is fully loaded into memory before returning so the caller can
    close the file handle immediately. Raises :class:`DecodeError` on any failure
    -- the pipeline records it on ``scan_state`` and moves on.
    """
    if is_raw(path):
        return _load_raw_preview(path)
    if path.suffix.lower() not in _PIL_READABLE:
        raise DecodeError(f"no decoder for {path.suffix or path.name!r}")
    try:
        img = Image.open(path)
        img.load()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise DecodeError(f"{type(exc).__name__}: {exc}") from exc
    return img


def _load_raw_preview(path: Path) -> Image.Image:
    if rawpy is None:
        raise DecodeError(f"rawpy/LibRaw not installed; cannot decode RAW {path.name}")
    try:
        with rawpy.imread(str(path)) as raw:
            try:
                thumb = raw.extract_thumb()
            except (rawpy.LibRawNoThumbnailError, rawpy.LibRawUnsupportedThumbnailError):
                thumb = None
            if thumb is not None and thumb.format == rawpy.ThumbFormat.JPEG:
                import io

                img = Image.open(io.BytesIO(thumb.data))
                img.load()
                return img
            rgb = raw.postprocess(use_camera_wb=True, no_auto_bright=True)
    except (rawpy.LibRawError, OSError, ValueError) as exc:
        raise DecodeError(f"RAW decode failed: {exc}") from exc
    return Image.fromarray(rgb)
