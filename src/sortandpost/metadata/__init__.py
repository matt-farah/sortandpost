"""The only module allowed to write under a photo library root.

Wraps a persistent ``exiftool -stay_open True`` process and batches reads/writes.

Writes, and nothing else:

* rating -> ``XMP:Rating`` (1-5; 0 removes the tag). With
  ``write_windows_compat_tags`` it also mirrors ``EXIF:Rating`` /
  ``EXIF:RatingPercent`` / ``XMP-microsoft:RatingPercent`` for Windows Explorer
  and Lightroom parity.
* approved tags -> ``XMP:Subject`` + ``IPTC:Keywords`` (replace semantics).

Writes use ``-overwrite_original_in_place`` so the inode, permissions and xattrs
survive; mtime is deliberately allowed to change so sync tools notice. CLIP
predictions are never written here, and RAW paths are refused as a safety net
(hard constraints #1, #2, #5).
"""

from __future__ import annotations

import contextlib
import json
import subprocess
import threading
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import IO

from sortandpost import binaries

RAW_SUFFIXES = frozenset(
    {".cr2", ".cr3", ".nef", ".arw", ".raf", ".rw2", ".orf", ".dng", ".pef", ".srw"}
)
EMBED_CAPABLE_SUFFIXES = frozenset({".jpg", ".jpeg", ".tif", ".tiff", ".png", ".heic", ".heif"})

# stars 0-5 -> RatingPercent, the Microsoft / Windows Explorer convention.
_RATING_PERCENT = (0, 1, 25, 50, 75, 99)

# Tags requested on every read. ``-G0`` prefixes JSON keys with the family-0
# group so XMP/EXIF/IPTC values never collide; ``-n`` yields numeric orientation
# and signed-decimal GPS; ``-d`` normalises datetimes to ISO 8601.
_READ_ARGS: tuple[str, ...] = (
    "-json",
    "-G0",
    "-n",
    "-d",
    "%Y-%m-%dT%H:%M:%S",
    "-XMP:Rating",
    "-EXIF:Rating",
    "-XMP:Subject",
    "-IPTC:Keywords",
    "-EXIF:DateTimeOriginal",
    "-EXIF:CreateDate",
    "-XMP:DateCreated",
    "-EXIF:Orientation",
    "-EXIF:Make",
    "-EXIF:Model",
    "-Composite:GPSLatitude",
    "-Composite:GPSLongitude",
)

# How long to wait for exiftool's post-command stderr sentinel (``-echo4``).
_SENTINEL_TIMEOUT_SEC = 30.0


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


def is_raw(path: Path) -> bool:
    return path.suffix.lower() in RAW_SUFFIXES


def can_embed_metadata(path: Path) -> bool:
    return path.suffix.lower() in EMBED_CAPABLE_SUFFIXES


def stars_to_rating_percent(stars: int) -> int:
    if not 0 <= stars <= 5:
        raise MetadataError(f"stars must be 0-5, got {stars}")
    return _RATING_PERCENT[stars]


class ExiftoolClient:
    """Context manager around one long-lived ``exiftool -stay_open`` process.

    Not thread-safe: one command in flight at a time. Use it as::

        with ExiftoolClient() as et:
            md = et.read(path)
            et.write_rating(path, 4)
    """

    def __init__(
        self,
        *,
        write_windows_compat_tags: bool = True,
        exiftool_path: str | None = None,
    ) -> None:
        self.write_windows_compat_tags = write_windows_compat_tags
        self._bin = exiftool_path or binaries.exiftool_path()
        self._proc: subprocess.Popen[str] | None = None
        self._seq = 0
        self._last_messages: tuple[str, ...] = ()
        self._stderr_lines: list[str] = []
        self._stderr_lock = threading.Lock()

    # -- lifecycle ------------------------------------------------------------ #

    def __enter__(self) -> ExiftoolClient:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def start(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            return
        try:
            self._proc = subprocess.Popen(
                [self._bin, "-stay_open", "True", "-@", "-"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                creationflags=binaries.no_console_flags(),
            )
        except FileNotFoundError as exc:
            raise MetadataError(
                f"exiftool executable not found ({self._bin!r}); "
                "install exiftool or pass exiftool_path="
            ) from exc
        self._seq = 0
        with self._stderr_lock:
            self._stderr_lines.clear()
        assert self._proc.stderr is not None
        threading.Thread(target=self._pump_stderr, args=(self._proc.stderr,), daemon=True).start()

    def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.poll() is None and proc.stdin is not None:
                proc.stdin.write("-stay_open\nFalse\n")
                proc.stdin.flush()
                proc.stdin.close()
            proc.wait(timeout=5)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            proc.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=5)

    @property
    def last_messages(self) -> tuple[str, ...]:
        """stderr lines (summary + warnings) from the most recent command."""
        return self._last_messages

    # -- reads -------------------------------------------------------------- #

    def read(self, path: Path) -> EmbeddedMetadata:
        stdout, _ = self._execute([*_READ_ARGS, _arg_path(path)])
        rows = _loads(stdout)
        if not rows:
            raise MetadataError(f"exiftool returned no data for {path}")
        return _to_metadata(rows[0])

    def read_batch(self, paths: Iterable[Path]) -> dict[Path, EmbeddedMetadata]:
        wanted = [Path(p) for p in paths]
        if not wanted:
            return {}
        stdout, _ = self._execute([*_READ_ARGS, *(_arg_path(p) for p in wanted)])
        by_source: dict[str, dict[str, object]] = {}
        for row in _loads(stdout):
            src = row.get("SourceFile")
            if isinstance(src, str):
                by_source[str(Path(src).resolve())] = row
        result: dict[Path, EmbeddedMetadata] = {}
        for path in wanted:
            matched = by_source.get(str(path.resolve()))
            if matched is not None:
                result[path] = _to_metadata(matched)
        return result

    # -- writes ----------------------------------------------------------- #

    def write_rating(self, path: Path, stars: int) -> None:
        if not 0 <= stars <= 5:
            raise MetadataError(f"stars must be 0-5, got {stars}")
        self._guard_writable(path)
        if stars == 0:
            tags = ["-XMP:Rating="]
            if self.write_windows_compat_tags:
                tags += [
                    "-EXIF:Rating=",
                    "-EXIF:RatingPercent=",
                    "-XMP-microsoft:RatingPercent=",
                ]
        else:
            tags = [f"-XMP:Rating={stars}"]
            if self.write_windows_compat_tags:
                pct = _RATING_PERCENT[stars]
                tags += [
                    f"-EXIF:Rating={stars}",
                    f"-EXIF:RatingPercent={pct}",
                    f"-XMP-microsoft:RatingPercent={pct}",
                ]
        self._commit(path, tags)

    def write_keywords(self, path: Path, keywords: list[str]) -> None:
        self._guard_writable(path)
        cleaned: list[str] = []
        for raw in keywords:
            keyword = raw.strip()
            if keyword and keyword not in cleaned:
                cleaned.append(keyword)
        tags = ["-XMP:Subject=", "-IPTC:Keywords="]  # replace, do not append
        for keyword in cleaned:
            tags += [f"-XMP:Subject={keyword}", f"-IPTC:Keywords={keyword}"]
        self._commit(path, tags)

    # -- internals ------------------------------------------------------- #

    def _guard_writable(self, path: Path) -> None:
        if is_raw(path):
            raise MetadataError(
                f"refusing to write metadata to RAW file {path.name} (hard constraint #2)"
            )
        if not can_embed_metadata(path):
            raise MetadataError(
                f"{path.suffix or path.name!r} does not support embedded metadata writes"
            )
        if not path.is_file():
            raise MetadataError(f"file does not exist: {path}")

    def _commit(self, path: Path, tag_args: Sequence[str]) -> None:
        # In -stay_open mode exiftool prints "N image files updated" to stdout;
        # warnings/errors go to stderr. Check both.
        stdout, messages = self._execute(
            [
                "-overwrite_original_in_place",
                "-codedcharacterset=utf8",
                *tag_args,
                _arg_path(path),
            ]
        )
        report = "\n".join([stdout, *messages])
        if "image files updated" in report or "image files unchanged" in report:
            return
        detail = report.strip() or "no changes written"
        raise MetadataError(f"exiftool did not update {path}: {detail}")

    def _execute(self, args: Sequence[str]) -> tuple[str, list[str]]:
        proc = self._proc
        if proc is None or proc.poll() is not None:
            raise MetadataError("exiftool is not running; use `with ExiftoolClient() as et: ...`")
        assert proc.stdin is not None and proc.stdout is not None

        self._seq += 1
        sentinel = f"{{ready{self._seq}}}"
        with self._stderr_lock:
            self._stderr_lines.clear()

        payload = [*args, "-echo4", sentinel, f"-execute{self._seq}"]
        proc.stdin.write("".join(f"{line}\n" for line in payload))
        proc.stdin.flush()

        stdout: list[str] = []
        while True:
            line = proc.stdout.readline()
            if line == "":
                raise MetadataError("exiftool exited unexpectedly (stdout closed)")
            if line.strip() == sentinel:
                break
            stdout.append(line)

        # ``-echo4`` writes the same sentinel to stderr once the command is done;
        # stderr is not synchronised with stdout, so poll briefly for it.
        deadline = time.monotonic() + _SENTINEL_TIMEOUT_SEC
        while time.monotonic() < deadline:
            with self._stderr_lock:
                if any(s.strip() == sentinel for s in self._stderr_lines):
                    break
            time.sleep(0.005)
        with self._stderr_lock:
            stripped = (s.strip() for s in self._stderr_lines)
            messages = [s for s in stripped if s and s != sentinel]
        self._last_messages = tuple(messages)

        fatal = [m for m in messages if m.lower().startswith("error:")]
        if fatal:
            raise MetadataError("; ".join(fatal))
        return "".join(stdout), messages

    def _pump_stderr(self, stream: IO[str]) -> None:
        for line in stream:
            with self._stderr_lock:
                self._stderr_lines.append(line.rstrip("\n"))


# --------------------------------------------------------------------------- #
# Parsing helpers
# --------------------------------------------------------------------------- #
def _arg_path(path: Path) -> str:
    text = str(path)
    if "\n" in text:
        raise MetadataError(f"path contains a newline, unsupported by exiftool: {text!r}")
    return text


def _loads(text: str) -> list[dict[str, object]]:
    text = text.strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise MetadataError(f"could not parse exiftool JSON output: {exc}") from exc
    if not isinstance(parsed, list):
        raise MetadataError("unexpected exiftool JSON shape (expected a list)")
    return parsed


def _as_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value.strip()))
        except ValueError:
            return None
    return None


def _as_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _parse_datetime(value: str) -> datetime | None:
    value = value.strip()
    if not value or value.startswith(("0000", "0001")):
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y:%m:%d %H:%M:%S"):
        try:
            return datetime.strptime(value[:19], fmt)
        except ValueError:
            continue
    return None


def _to_metadata(row: dict[str, object]) -> EmbeddedMetadata:
    def first(*keys: str) -> object | None:
        for key in keys:
            val = row.get(key)
            if val not in (None, ""):
                return val
        return None

    rating = _as_int(first("XMP:Rating", "EXIF:Rating"))
    if rating is not None:
        rating = max(0, min(5, rating))

    keywords: list[str] = []
    for key in ("XMP:Subject", "IPTC:Keywords"):
        val = row.get(key)
        if val is None:
            continue
        for item in val if isinstance(val, list) else [val]:
            text = str(item).strip()
            if text and text not in keywords:
                keywords.append(text)

    taken_raw = first("EXIF:DateTimeOriginal", "EXIF:CreateDate", "XMP:DateCreated")
    make = first("EXIF:Make")
    model = first("EXIF:Model")

    return EmbeddedMetadata(
        rating=rating,
        keywords=tuple(keywords),
        taken_at=_parse_datetime(taken_raw) if isinstance(taken_raw, str) else None,
        orientation=_as_int(first("EXIF:Orientation")),
        camera_make=str(make).strip() if make is not None else None,
        camera_model=str(model).strip() if model is not None else None,
        gps_lat=_as_float(first("Composite:GPSLatitude")),
        gps_lon=_as_float(first("Composite:GPSLongitude")),
    )
