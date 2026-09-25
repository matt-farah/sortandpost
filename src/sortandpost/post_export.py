"""Clean JPEG exports for upload to robopost.

The uploaded file is **never** the source photo: each one is decoded with the
indexer's own decoder (RAW -> embedded preview, HEIC via pillow-heif), rotated
upright, converted to sRGB, scaled to ``posting.export_long_edge`` and saved as
a fresh JPEG with no EXIF / XMP / GPS and no ICC tag. The source file is only
ever opened for reading (hard constraints #1 and #2).

Exports go to ``<APP_DATA>/local/post_exports/<post_id>/`` (never synced, never
under a library root) and are removed after a successful send.
:func:`export_jpeg` is CPU-bound and picklable, so it runs in a worker process
(hard constraint #7).
"""

from __future__ import annotations

import io
import logging
import os
from pathlib import Path

from PIL import Image, ImageCms, ImageOps
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from sortandpost.db.models import Photo, PhotoPath
from sortandpost.indexer.decode import load_image

log = logging.getLogger(__name__)

_SRGB = ImageCms.createProfile("sRGB")


def _to_srgb(img: Image.Image, icc: bytes | None) -> Image.Image:
    """RGB pixels in sRGB. An embedded ICC profile (Display P3, Adobe RGB,
    CMYK, …) is converted; untagged pixels are taken as sRGB already."""
    if icc:
        try:
            src = ImageCms.ImageCmsProfile(io.BytesIO(icc))
            converted = ImageCms.profileToProfile(img, src, _SRGB, outputMode="RGB")
            if converted is not None:
                return converted
        except (ImageCms.PyCMSError, OSError, ValueError) as exc:
            log.info("ICC conversion skipped (%s); treating pixels as sRGB", exc)
    return img.convert("RGB")


def export_jpeg(src: str, dest: str, long_edge: int, quality: int) -> tuple[int, int]:
    """Write a clean JPEG of *src* to *dest*; returns its ``(width, height)``.
    ``long_edge <= 0`` keeps full resolution. Worker-safe (no handles)."""
    img = load_image(Path(src))
    icc = img.info.get("icc_profile")
    img = ImageOps.exif_transpose(img) or img
    if img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        img = Image.new("RGB", rgba.size, (255, 255, 255))
        img.paste(rgba, mask=rgba.getchannel("A"))
    img = _to_srgb(img, icc if isinstance(icc, bytes) else None)
    if long_edge > 0 and max(img.size) > long_edge:
        img.thumbnail((long_edge, long_edge), Image.Resampling.LANCZOS)
    out = Path(dest)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f"{out.name}.tmp-{os.getpid()}")
    try:
        # A brand-new image: no exif=, no icc_profile=, so nothing from the
        # source (GPS, camera serials, XMP) is carried over.
        img.save(tmp, "JPEG", quality=max(1, min(100, quality)), optimize=True)
        os.replace(tmp, out)
    finally:
        tmp.unlink(missing_ok=True)
    return img.size


def export_key(long_edge: int, quality: int) -> str:
    """Identifies an export's settings; a cached upload is reused only when it
    matches (changing the size/quality forces a fresh upload)."""
    return f"{long_edge}-{quality}"


def resolve_sources(engine: Engine, hashes: list[str]) -> dict[str, str | None]:
    """``pixel_hash -> readable file path`` on this machine (``None`` when no
    present copy exists). Prefers the photo's ``primary_path``."""
    out: dict[str, str | None] = dict.fromkeys(hashes)
    if not hashes:
        return out
    with Session(engine) as s:
        primaries = {
            p.pixel_hash: p.primary_path
            for p in s.exec(select(Photo).where(Photo.pixel_hash.in_(hashes)))  # type: ignore[attr-defined]
        }
        rows = s.exec(
            select(PhotoPath).where(
                PhotoPath.pixel_hash.in_(hashes),  # type: ignore[attr-defined]
                PhotoPath.present == True,  # noqa: E712
            )
        ).all()
    candidates: dict[str, list[str]] = {}
    for r in rows:
        candidates.setdefault(r.pixel_hash, []).append(r.path)
    for h in hashes:
        paths = sorted(candidates.get(h, []), key=lambda p: (p != primaries.get(h), p))
        for p in paths:
            if os.path.isfile(p):
                out[h] = p
                break
    return out
