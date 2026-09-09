"""Shared builders for sync-layer tests: throwaway SQLite DBs with known rows."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from sqlmodel import Session, select

from sortandpost.db import init_db, make_engine
from sortandpost.db.meta import set_generation
from sortandpost.db.models import ChangeOp, Photo, PhotoPath, Rating, RatingSource
from sortandpost.sync.changelog import record_change

EPOCH = datetime(2020, 1, 1, tzinfo=UTC)


def ts(offset_sec: int) -> datetime:
    return datetime.fromtimestamp(EPOCH.timestamp() + offset_sec, tz=UTC)


def hash_for(tag: str) -> str:
    return (tag * 64)[:64]


def make_db(path: Path, *, photos: list[str], generation: int = 1) -> None:
    """Create a DB at *path* with one Photo+PhotoPath per tag in *photos*."""
    engine = make_engine(path)
    init_db(engine)
    now = ts(0)
    with Session(engine) as s:
        for tag in photos:
            ph = hash_for(tag)
            s.add(
                Photo(
                    pixel_hash=ph,
                    primary_path=f"/library/{tag}.jpg",
                    width=100,
                    height=100,
                    format="JPEG",
                    file_size=1000,
                    file_mtime_ns=1_000_000_000,
                    first_indexed_at=now,
                    last_indexed_at=now,
                )
            )
            s.add(
                PhotoPath(
                    pixel_hash=ph,
                    path=f"/library/{tag}.jpg",
                    size=1000,
                    mtime_ns=1_000_000_000,
                    present=True,
                )
            )
        set_generation(s, generation)
        s.commit()
    engine.dispose()


def rate(path: Path, tag: str, stars: int, *, device: str, when: datetime) -> None:
    """Apply a rating + its ``changes`` row to the DB at *path* (as the UI will)."""
    engine = make_engine(path)
    with Session(engine) as s:
        ph = hash_for(tag)
        row = s.get(Rating, ph)
        old = str(row.stars) if row is not None else None
        if row is None:
            s.add(
                Rating(
                    pixel_hash=ph,
                    stars=stars,
                    updated_at=when,
                    updated_by_device=device,
                    source=RatingSource.app,
                )
            )
        else:
            row.stars = stars
            row.updated_at = when
            row.updated_by_device = device
        record_change(
            s,
            device_id=device,
            op=ChangeOp.rate,
            pixel_hash=ph,
            ts=when,
            field="stars",
            old_value=old,
            new_value=str(stars),
        )
        s.commit()
    engine.dispose()


def read_ratings(path: Path) -> dict[str, int]:
    engine = make_engine(path)
    try:
        with Session(engine) as s:
            return {r.pixel_hash: r.stars for r in s.exec(select(Rating)).all()}
    finally:
        engine.dispose()
