"""Clean JPEG exports for upload to a posting service.

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
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageCms, ImageOps
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from sortandpost.db.models import Photo, PhotoPath
from sortandpost.indexer.decode import load_image

log = logging.getLogger(__name__)

_SRGB = ImageCms.createProfile("sRGB")

# Instagram canvases (width, height). Story is only offered for postType STORIES.
IG_FORMATS: dict[str, tuple[int, int]] = {
    "portrait": (1080, 1350),
    "square": (1080, 1080),
    "landscape": (1080, 566),
    "story": (1080, 1920),
}
DEFAULT_IG_FORMAT = "portrait"
IG_RATIO_TOLERANCE = 0.015  # relative; 1079x1349 style rounding still counts


@dataclass(frozen=True)
class IgSpec:
    """An Instagram canvas: pad-and-fit to ``width`` x ``height`` with a
    ``border`` (px, total, as in the reference script) in ``rgb``. Frozen and
    plain so it pickles into the process pool."""

    width: int
    height: int
    border: int = 0
    rgb: tuple[int, int, int] = (255, 255, 255)


def fit_and_pad(
    img: Image.Image, width: int, height: int, border: int, rgb: tuple[int, int, int]
) -> Image.Image:
    """Scale *img* to fit inside ``width`` x ``height`` minus *border* and centre
    it on a solid canvas of exactly that size (``tools/reference/ResizePhoto.py``)."""
    ow, oh = img.size
    ratio = min((width - border) / ow, (height - border) / oh)
    new = (max(1, round(ow * ratio)), max(1, round(oh * ratio)))
    resized = img.resize(new, Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", (width, height), rgb)
    canvas.paste(resized, ((width - new[0]) // 2, (height - new[1]) // 2))
    return canvas


def is_instagram_ratio(width: int, height: int, *, story: bool = False) -> bool:
    """Is the (upright) aspect ratio exactly one Instagram sizing we offer: 4:5,
    1:1 or 1.91:1 for a post, 9:16 for a Story (within a small tolerance)."""
    if width <= 0 or height <= 0:
        return False
    r = width / height
    targets = (9 / 16,) if story else (4 / 5, 1.0, 1.91)
    return any(abs(r - t) <= t * IG_RATIO_TOLERANCE for t in targets)


def parse_color(
    value: str, default: tuple[int, int, int] = (255, 255, 255)
) -> tuple[int, int, int]:
    """``#rrggbb`` -> rgb; anything else -> *default*."""
    v = value.strip().lstrip("#")
    if len(v) == 6:
        try:
            return int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16)
        except ValueError:
            pass
    return default


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


def upright_srgb(img: Image.Image) -> Image.Image:
    """A decoded image rotated upright, transparency flattened onto white, as
    sRGB RGB pixels. Shared by post exports and profile pictures."""
    icc = img.info.get("icc_profile")
    img = ImageOps.exif_transpose(img) or img
    if img.mode in ("RGBA", "LA", "PA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        img = Image.new("RGB", rgba.size, (255, 255, 255))
        img.paste(rgba, mask=rgba.getchannel("A"))
    return _to_srgb(img, icc if isinstance(icc, bytes) else None)


def export_jpeg(
    src: str, dest: str, long_edge: int, quality: int, ig: IgSpec | None = None
) -> tuple[int, int]:
    """Write a clean JPEG of *src* to *dest*; returns its ``(width, height)``.
    ``long_edge <= 0`` keeps full resolution. With *ig*, the photo is fitted and
    padded onto that Instagram canvas instead. Worker-safe (no handles)."""
    img = upright_srgb(load_image(Path(src)))
    if ig is not None:
        img = fit_and_pad(img, ig.width, ig.height, ig.border, ig.rgb)
    elif long_edge > 0 and max(img.size) > long_edge:
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


def export_key(long_edge: int, quality: int, ig: IgSpec | None = None) -> str:
    """Identifies an export's settings; a cached upload is reused only when it
    matches (changing the size/quality/Instagram canvas forces a fresh upload)."""
    key = f"{long_edge}-{quality}"
    if ig is not None:
        r, g, b = ig.rgb
        key += f"-ig{ig.width}x{ig.height}b{ig.border}c{r:02x}{g:02x}{b:02x}"
    return key


def upright_sizes(engine: Engine, hashes: list[str]) -> dict[str, tuple[int, int]]:
    """``pixel_hash -> (width, height)`` as displayed (EXIF orientation 5-8
    swaps the stored native size). Hashes not in the DB are omitted."""
    out: dict[str, tuple[int, int]] = {}
    if not hashes:
        return out
    with Session(engine) as s:
        for p in s.exec(select(Photo).where(Photo.pixel_hash.in_(hashes))):  # type: ignore[attr-defined]
            w, h = p.width, p.height
            if p.orientation in (5, 6, 7, 8):
                w, h = h, w
            out[p.pixel_hash] = (w, h)
    return out


def resolve_ig_spec(
    sizes: list[tuple[int, int]], *, fmt: str, border: int, rgb: tuple[int, int, int], story: bool
) -> IgSpec | None:
    """The canvas every photo in an Instagram post is sized to, or ``None`` when
    every photo already has an Instagram ratio and is used as it is. A Story
    format only applies to a Story post (otherwise portrait)."""
    if sizes and all(is_instagram_ratio(w, h, story=story) for w, h in sizes):
        return None
    if story:
        fmt = "story"
    elif fmt not in IG_FORMATS or fmt == "story":
        fmt = DEFAULT_IG_FORMAT
    w, h = IG_FORMATS[fmt]
    return IgSpec(w, h, border, rgb)


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
