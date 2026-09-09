"""Pixel-identity hashing -- CLAUDE.md hard constraint #3.

``pixel_hash`` is the sha256 of the decoded RGB bytes at the image's stored
resolution, ignoring the EXIF orientation flag. It does not change when we (or
any other tool) rewrite the file's metadata, nor when the file is re-saved
without touching pixels. Every cross-reference in the app keys on it.

The full scanner (decode fast-paths, RAW previews, HEIC) is build-order step 3;
this module is just the decode+hash core it will call.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from PIL import Image

try:  # HEIC/HEIF decode, only when the optional plugin is installed
    import pillow_heif

    pillow_heif.register_heif_opener()
except ModuleNotFoundError:  # pragma: no cover - optional dependency
    pass

# Camera files routinely exceed Pillow's default ~89 MPix bomb guard; raise it
# to a value that still rejects obviously hostile inputs.
Image.MAX_IMAGE_PIXELS = 500_000_000


def pixel_hash(path: Path | str) -> str:
    """sha256 hex digest of the decoded RGB pixel buffer at stored resolution.

    Alpha is dropped (``convert("RGB")``); palette, grayscale and CMYK are
    normalised the same way so the digest depends only on visible pixels.
    """
    with Image.open(path) as img:
        return pixel_hash_of_image(img)


def pixel_hash_of_image(img: Image.Image) -> str:
    """``pixel_hash`` for an already-decoded PIL image.

    The scanner opens each file exactly once and feeds the same image object to
    this and to thumbnail generation. The EXIF orientation flag is intentionally
    ignored -- the digest is of the pixels as stored, not as displayed.
    """
    rgb = img.convert("RGB")
    return hashlib.sha256(rgb.tobytes()).hexdigest()
