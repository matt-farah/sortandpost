"""A small photo library for the Docker tests: JPEGs with EXIF dates + cameras, a
byte-identical duplicate, a PNG, a HEIC, a JPEG that already carries a rating, and a RAW
when the sample fixtures are on this computer (``~/Downloads/sampleImages``, as in
tests/test_raw_heic_audit.py). Deterministic, so a native and a Docker index of two copies
can be compared photo by photo (test_parity.py)."""

from __future__ import annotations

import random
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw

SAMPLES = Path.home() / "Downloads" / "sampleImages"
SAMPLE_CR2 = SAMPLES / "RAWs" / "IMG_4909.CR2"

_EXIF_IFD = 0x8769
_DATETIME_ORIGINAL = 0x9003
_MAKE = 0x010F
_MODEL = 0x0110


@dataclass
class Library:
    root: Path
    files: dict[str, Path] = field(default_factory=dict)  # short name -> file

    def __getitem__(self, name: str) -> Path:
        return self.files[name]

    def snapshot(self) -> dict[str, tuple[int, int]]:
        """Every file under the root (hidden ones too) -> (size, mtime_ns)."""
        return {
            str(p.relative_to(self.root)): (p.stat().st_size, p.stat().st_mtime_ns)
            for p in sorted(self.root.rglob("*"))
            if p.is_file()
        }


def _picture(seed: int, size: tuple[int, int] = (480, 360)) -> Image.Image:
    rng = random.Random(seed)
    img = Image.new("RGB", size, tuple(rng.randrange(256) for _ in range(3)))
    draw = ImageDraw.Draw(img)
    for _ in range(12):
        x0, y0 = rng.randrange(size[0]), rng.randrange(size[1])
        box = (x0, y0, x0 + rng.randrange(20, 200), y0 + rng.randrange(20, 200))
        draw.ellipse(box, fill=tuple(rng.randrange(256) for _ in range(3)))
    return img


def _jpeg(path: Path, seed: int, *, taken: str, make: str, model: str) -> Path:
    exif = Image.Exif()
    exif[_MAKE] = make
    exif[_MODEL] = model
    exif.get_ifd(_EXIF_IFD)[_DATETIME_ORIGINAL] = taken
    path.parent.mkdir(parents=True, exist_ok=True)
    _picture(seed).save(path, "JPEG", quality=90, exif=exif.tobytes())
    return path


def build_library(root: Path, *, with_raw: bool = True) -> Library:
    lib = Library(root)
    root.mkdir(parents=True, exist_ok=True)
    f = lib.files
    f["beach"] = _jpeg(
        root / "2024" / "beach.jpg", 1, taken="2024:07:14 10:00:00", make="Canon", model="EOS R5"
    )
    f["forest"] = _jpeg(
        root / "2024" / "forest.jpg", 2, taken="2024:03:02 08:30:00", make="FUJIFILM", model="X-T5"
    )
    f["city"] = _jpeg(
        root / "2023" / "trip" / "city.jpg",
        3,
        taken="2023:11:20 18:45:00",
        make="Apple",
        model="iPhone 15",
    )
    f["beach_dupe"] = root / "dupes" / "beach.jpg"
    f["beach_dupe"].parent.mkdir(parents=True)
    shutil.copy2(f["beach"], f["beach_dupe"])

    f["snow"] = root / "2023" / "snow.png"
    _picture(4).save(f["snow"], "PNG")

    try:
        import pillow_heif

        pillow_heif.register_heif_opener()
        f["lake"] = root / "2023" / "lake.heic"
        _picture(5).save(f["lake"], "HEIF", quality=90)
    except Exception:  # pragma: no cover - no HEIF encoder in this build
        pass

    exiftool = shutil.which("exiftool")
    if exiftool:  # a photo that already carries a rating (reconcile reads it)
        f["rated"] = _jpeg(
            root / "rated" / "rated.jpg", 6, taken="2022:01:05 12:00:00", make="Nikon", model="Z6"
        )
        subprocess.run(
            [exiftool, "-q", "-overwrite_original", "-XMP-xmp:Rating=3", str(f["rated"])],
            check=True,
        )

    if with_raw and SAMPLE_CR2.is_file():
        f["raw"] = root / "raw" / SAMPLE_CR2.name
        f["raw"].parent.mkdir()
        shutil.copy2(SAMPLE_CR2, f["raw"])
    return lib


def add_photo(root: Path, rel: str, seed: int) -> Path:
    """One more JPEG (a "new photo arrives" event)."""
    return _jpeg(root / rel, seed, taken="2025:05:05 05:05:05", make="Sony", model="A7 IV")
