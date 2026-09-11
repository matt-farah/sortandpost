"""SQLModel schema. See CLAUDE.md → "Database schema (core tables)".

Identity is always ``pixel_hash`` (sha256 of decoded RGB bytes), never a path or
whole-file hash. Tables split into:

* user-authored (``rating``, manual ``photo_tag``, ``prediction.state``) — every
  mutation also appends a :class:`Change` row so cross-machine conflict copies can
  be merged deterministically.
* derived (everything else) — rebuildable by re-scanning.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #
class TagSource(StrEnum):
    manual = "manual"
    clip = "clip"


class TagKind(StrEnum):
    manual = "manual"
    clip_approved = "clip-approved"


class PredictionState(StrEnum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"


class RatingSource(StrEnum):
    app = "app"
    file = "file"


class ChangeOp(StrEnum):
    rate = "rate"
    tag_add = "tag_add"
    tag_remove = "tag_remove"
    clip_approve = "clip_approve"
    clip_reject = "clip_reject"
    unindex = "unindex"


# --------------------------------------------------------------------------- #
# Tables
# --------------------------------------------------------------------------- #
class Photo(SQLModel, table=True):
    __tablename__ = "photos"

    pixel_hash: str = Field(primary_key=True)
    primary_path: str = Field(index=True)
    width: int
    height: int
    format: str
    is_raw: bool = False
    orientation: int | None = None
    taken_at: datetime | None = Field(default=None, index=True)
    camera_make: str | None = None
    camera_model: str | None = None
    gps_lat: float | None = None
    gps_lon: float | None = None
    file_size: int
    file_mtime_ns: int
    first_indexed_at: datetime
    last_indexed_at: datetime
    missing_since: datetime | None = Field(default=None, index=True)
    phash: str | None = Field(default=None, index=True)


class PhotoPath(SQLModel, table=True):
    __tablename__ = "photo_paths"

    id: int | None = Field(default=None, primary_key=True)
    pixel_hash: str = Field(foreign_key="photos.pixel_hash", index=True)
    path: str = Field(unique=True)
    size: int
    mtime_ns: int
    present: bool = True


class Rating(SQLModel, table=True):
    __tablename__ = "ratings"

    pixel_hash: str = Field(primary_key=True, foreign_key="photos.pixel_hash")
    stars: int  # 0-5; 0 == unrated
    updated_at: datetime
    updated_by_device: str
    source: RatingSource = RatingSource.app


class Tag(SQLModel, table=True):
    __tablename__ = "tags"

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(unique=True, index=True)
    facet: str | None = None
    kind: TagKind = TagKind.manual


class PhotoTag(SQLModel, table=True):
    __tablename__ = "photo_tags"
    __table_args__ = (UniqueConstraint("pixel_hash", "tag_id", name="uq_photo_tag"),)

    id: int | None = Field(default=None, primary_key=True)
    pixel_hash: str = Field(foreign_key="photos.pixel_hash", index=True)
    tag_id: int = Field(foreign_key="tags.id", index=True)
    source: TagSource = TagSource.manual
    score: float | None = None
    added_at: datetime
    added_by_device: str
    written_to_file: bool = False


class Prediction(SQLModel, table=True):
    __tablename__ = "predictions"
    __table_args__ = (UniqueConstraint("pixel_hash", "tag_name", "model", name="uq_prediction"),)

    id: int | None = Field(default=None, primary_key=True)
    pixel_hash: str = Field(foreign_key="photos.pixel_hash", index=True)
    tag_name: str = Field(index=True)
    score: float
    model: str
    run_at: datetime
    state: PredictionState = Field(default=PredictionState.pending, index=True)


class Change(SQLModel, table=True):
    """Append-only user-authored mutation log. Powers conflict merge, audit, undo."""

    __tablename__ = "changes"

    id: int | None = Field(default=None, primary_key=True)
    ts: datetime = Field(index=True)
    device_id: str = Field(index=True)
    op: ChangeOp
    pixel_hash: str = Field(index=True)
    field: str | None = None
    old_value: str | None = None
    new_value: str | None = None


class Tombstone(SQLModel, table=True):
    """A photo the user explicitly deleted. Written by the "Delete photos" action
    and by ``unindex`` merge replay. Travels in the DB so every machine learns of
    the delete; the scanner refuses to re-index a tombstoned ``pixel_hash``.

    ``applied`` = this machine has purged its derived rows (thumbnails, embedding
    row, ``scan_state``) *and* moved its own local copy of the file to trash.
    """

    __tablename__ = "tombstones"

    pixel_hash: str = Field(primary_key=True)
    deleted_at: datetime
    deleted_by_device: str
    applied: bool = False


class EmbeddingRow(SQLModel, table=True):
    __tablename__ = "embedding_rows"

    pixel_hash: str = Field(primary_key=True, foreign_key="photos.pixel_hash")
    row_index: int = Field(index=True)
    model: str


class EmbeddingsMeta(SQLModel, table=True):
    __tablename__ = "embeddings_meta"

    model: str = Field(primary_key=True)
    pretrained: str
    dim: int
    count: int = 0
    updated_at: datetime


class ScanState(SQLModel, table=True):
    __tablename__ = "scan_state"

    path: str = Field(primary_key=True)
    pixel_hash: str | None = Field(default=None, index=True)
    size: int | None = None  # last-seen stat, for the fast path
    mtime_ns: int | None = None
    stages_done: int = 0  # bitmask of completed pipeline stages
    indexed_at: datetime | None = None
    error: str | None = None
    attempts: int = 0
    # A cloud "online-only" placeholder we chose not to download (see
    # sortandpost.indexer.placeholder). Not an error -- attempts stays put; the
    # user hydrates it on demand (sortandpost.indexer.hydrate).
    online_only: bool = Field(default=False, index=True)


class Meta(SQLModel, table=True):
    """Small key/value bag: db_generation, schema_version, ..."""

    __tablename__ = "meta"

    key: str = Field(primary_key=True)
    value: str
