"""Undo a delete: put a trash batch back into the library and the index.

The reverse of :func:`sortandpost.db.deletion.delete_photos`, and like it only
ever run from an explicit user click (the "Undo" button after a delete, or
"Put back" on ``/settings``). For a batch in ``<APP_DATA>/local/trash``:

1. move the files back to their original paths (:func:`sortandpost.trash.put_back`
   -- refused per file if the path is taken or its folder is gone);
2. for every photo with a file back: drop its tombstone and append a
   ``changes`` row ``op=restore`` (constraint #4);
3. re-index those paths right away (thumbnails, metadata; CLIP follows on the
   next deferred pass) and re-insert the rating / tags / CLIP decisions the
   delete saved in the batch manifest.

Runs under the indexer's process-wide lock so it never races a scan pass.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import Engine
from sqlmodel import Session

from sortandpost import binaries
from sortandpost.config import Settings
from sortandpost.db.deletion import lift_tombstone, restore_photo_state
from sortandpost.indexer.pipeline import PipelineConfig, index_file
from sortandpost.indexer.scan import _INDEX_LOCK
from sortandpost.metadata import ExiftoolClient
from sortandpost.sync.clock import utcnow
from sortandpost.trash import TrashedItem, find_batch, put_back

log = logging.getLogger(__name__)


@dataclass
class UndeleteResult:
    photos_restored: list[str] = field(default_factory=list)  # pixel_hashes back in the index
    files_restored: int = 0
    failed: list[TrashedItem] = field(default_factory=list)
    index_errors: list[str] = field(default_factory=list)
    error: str | None = None  # the whole batch could not be used

    def summary(self) -> str:
        if self.error:
            return self.error
        msg = f"put back {len(self.photos_restored)} photo(s)"
        if self.failed:
            msg += f"; {len(self.failed)} file(s) not restored ({self.failed[0].error})"
        if self.index_errors:
            msg += f"; {len(self.index_errors)} could not be re-indexed"
        return msg


def put_back_batch(
    settings: Settings,
    engine: Engine,
    batch_id: str,
    *,
    only_hashes: Iterable[str] | None = None,
) -> UndeleteResult:
    """Put back *batch_id* (or just *only_hashes* from it). Blocking -- call it
    off the event loop."""
    result = UndeleteResult()
    batch = find_batch(settings.paths.local, batch_id)
    if batch is None:
        result.error = "that delete is no longer in the app trash"
        return result

    with _INDEX_LOCK:
        pb = put_back(batch.path, only_hashes=only_hashes)
        result.failed = pb.failed
        result.files_restored = len(pb.restored)
        if not pb.restored:
            return result

        tcfg = settings.config.thumbnails
        pcfg = PipelineConfig(
            thumbnail_sizes=tuple(tcfg.sizes),
            thumbnail_format=tcfg.format,
            thumbnail_quality=tcfg.quality,
            thumbnail_path=settings.paths.thumbnail_file,
            online_only_mode="download",  # we just moved real bytes back; never a stub
        )
        et_ctx = (
            ExiftoolClient(
                write_windows_compat_tags=settings.config.rating.write_windows_compat_tags
            )
            if binaries.exiftool_available()
            else nullcontext(None)
        )
        now = utcnow()
        with Session(engine) as session, et_ctx as exiftool:
            for h in pb.hashes:
                n = sum(1 for i in pb.restored if i.pixel_hash == h)
                lift_tombstone(
                    session, pixel_hash=h, device_id=settings.device_id, now=now, n_paths=n
                )
            session.flush()  # the pipeline's tombstone guard must see them gone
            for item in pb.restored:
                outcome = index_file(
                    session,
                    Path(item.original),
                    config=pcfg,
                    exiftool=exiftool,
                    device_id=settings.device_id,
                    now=now,
                )
                if outcome.error:
                    log.warning(
                        "re-index after put back failed for %s: %s", item.original, outcome.error
                    )
                    result.index_errors.append(item.original)
            for h in pb.hashes:
                state = pb.photos.get(h, {})
                if restore_photo_state(session, h, state):
                    result.photos_restored.append(h)
            session.commit()
    log.info("put back %s: %s", batch_id, result.summary())
    return result
