"""Zero-shot predictions land from the CLIP pass, and the /review approve / reject
flow writes photo_tags + changes rows + (mocked) EXIF, with RAW kept DB-only and
rejections sticky across re-runs. A fake model stands in for open_clip/torch."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest
from PIL import Image
from sqlmodel import Session, select

from sortandpost.clip.tagging import (
    approve,
    facet_sections,
    is_pending,
    pending_for_tag,
    pending_groups,
    reject,
    unapprove,
)
from sortandpost.clip.vocab import load_vocab
from sortandpost.config import Settings
from sortandpost.db import init_db, make_engine
from sortandpost.db.models import (
    Change,
    ChangeOp,
    Photo,
    PhotoPath,
    PhotoTag,
    Prediction,
    PredictionState,
    Tag,
    TagSource,
)
from sortandpost.indexer import scan_clip_once, scan_once

DIM = 3
NOW = datetime(2026, 6, 1, tzinfo=UTC)


class AlignedFakeModel:
    """Image embedding = unit mean-RGB; text embedding = colour basis vector."""

    @property
    def dim(self) -> int:
        return DIM

    def load(self) -> None:
        pass

    def encode_images(self, images: list[Image.Image]) -> np.ndarray:
        out = np.zeros((len(images), DIM), dtype=np.float32)
        for i, im in enumerate(images):
            rgb = np.asarray(im.convert("RGB").resize((4, 4)), dtype=np.float32).reshape(-1, 3)
            v = rgb.mean(axis=0)
            out[i] = v / (np.linalg.norm(v) or 1.0)
        return out

    def encode_text(self, texts: list[str]) -> np.ndarray:
        out = np.zeros((len(texts), DIM), dtype=np.float32)
        for i, t in enumerate(texts):
            if "red" in t:
                out[i] = [1, 0, 0]
            elif "green" in t:
                out[i] = [0, 1, 0]
            elif "blue" in t:
                out[i] = [0, 0, 1]
        return out


class MockExiftool:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str]]] = []

    def write_keywords(self, path: Path, keywords: list[str]) -> None:
        self.calls.append((path.name, list(keywords)))


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    library = tmp_path / "library"
    library.mkdir()
    monkeypatch.setenv("APP_DATA", str(tmp_path / "appdata"))
    monkeypatch.delenv("PORT", raising=False)
    resolved = Settings.resolve()
    resolved.set_library_roots([library])
    resolved.paths.tags_file.write_text("colour:\n  - red\n  - green\n  - blue\n", encoding="utf-8")
    return resolved


def _lib(settings: Settings) -> Path:
    return Path(settings.library_roots[0].container)


def _session(settings: Settings) -> Session:
    engine = make_engine(settings.paths.working_db)
    init_db(engine)
    return Session(engine)


def _add_photo(s: Session, pixel_hash: str, path: str, *, is_raw: bool = False) -> None:
    fmt = path.rsplit(".", 1)[-1].upper()
    s.add(
        Photo(
            pixel_hash=pixel_hash,
            primary_path=path,
            width=10,
            height=10,
            format=fmt,
            is_raw=is_raw,
            file_size=1,
            file_mtime_ns=1,
            first_indexed_at=NOW,
            last_indexed_at=NOW,
        )
    )
    s.add(PhotoPath(pixel_hash=pixel_hash, path=path, size=1, mtime_ns=1, present=True))


def _jpeg(path: Path, color: tuple[int, int, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (32, 24), color).save(path, "JPEG", quality=95)


# --------------------------------------------------------------------------- #
# scoring from the CLIP pass
# --------------------------------------------------------------------------- #
def test_clip_pass_writes_pending_predictions(settings: Settings) -> None:
    _jpeg(_lib(settings) / "red.jpg", (240, 6, 6))
    _jpeg(_lib(settings) / "green.jpg", (6, 240, 6))
    scan_once(settings)

    stats = scan_clip_once(settings, model=AlignedFakeModel())
    assert stats.embedded == 2
    assert stats.predictions == 2  # one strong colour hit each

    model = settings.config.clip.model
    assert settings.paths.text_embeddings_file(
        model, load_vocab(settings.paths.tags_file).vocab_hash
    ).is_file()

    with _session(settings) as s:
        preds = s.exec(select(Prediction)).all()
        assert {p.tag_name for p in preds} == {"red", "green"}
        assert all(p.state == PredictionState.pending for p in preds)
        assert all(p.model == model for p in preds)
        assert all(p.score >= settings.config.tagging.score_threshold for p in preds)


def test_pending_groups_shape(settings: Settings) -> None:
    _jpeg(_lib(settings) / "red.jpg", (240, 6, 6))
    scan_once(settings)
    scan_clip_once(settings, model=AlignedFakeModel())

    with _session(settings) as s:
        groups = pending_groups(
            s, model=settings.config.clip.model, vocab=load_vocab(settings.paths.tags_file)
        )
    assert [g.tag_name for g in groups] == ["red"]
    assert groups[0].facet == "colour"
    assert groups[0].count == 1


# --------------------------------------------------------------------------- #
# approve
# --------------------------------------------------------------------------- #
def test_approve_writes_tag_change_and_exif(settings: Settings) -> None:
    _jpeg(_lib(settings) / "red.jpg", (240, 6, 6))
    scan_once(settings)
    scan_clip_once(settings, model=AlignedFakeModel())
    model = settings.config.clip.model

    et = MockExiftool()
    with _session(settings) as s:
        ph = s.exec(select(Prediction).where(Prediction.tag_name == "red")).one().pixel_hash
        # a pre-existing manual tag, so the EXIF write must carry the union
        manual = Tag(name="sunset", facet="lighting")
        s.add(manual)
        s.flush()
        s.add(
            PhotoTag(
                pixel_hash=ph,
                tag_id=manual.id,
                source=TagSource.manual,
                added_at=NOW,
                added_by_device="dev",
            )
        )
        s.commit()

        approve(
            s,
            device_id=settings.device_id,
            model=model,
            pixel_hash=ph,
            tag_name="red",
            facet="colour",
            now=NOW,
            exiftool=et,
        )
        s.commit()

        pred = s.exec(select(Prediction).where(Prediction.tag_name == "red")).one()
        assert pred.state == PredictionState.approved

        tag = s.exec(select(Tag).where(Tag.name == "red")).one()
        pt = s.exec(
            select(PhotoTag).where(PhotoTag.pixel_hash == ph, PhotoTag.tag_id == tag.id)
        ).one()
        assert pt.source == TagSource.clip
        assert pt.written_to_file is True

        change = s.exec(select(Change).where(Change.op == ChangeOp.clip_approve)).one()
        assert change.pixel_hash == ph
        assert change.field == "red"

    assert et.calls == [("red.jpg", ["red", "sunset"])]


def test_approve_on_raw_is_db_only(settings: Settings) -> None:
    ph = "r" * 64
    with _session(settings) as s:
        _add_photo(s, ph, "/library/shot.nef", is_raw=True)
        s.add(
            Prediction(
                pixel_hash=ph,
                tag_name="red",
                score=0.8,
                model=settings.config.clip.model,
                run_at=NOW,
                state=PredictionState.pending,
            )
        )
        s.commit()

        et = MockExiftool()
        approve(
            s,
            device_id=settings.device_id,
            model=settings.config.clip.model,
            pixel_hash=ph,
            tag_name="red",
            facet="colour",
            now=NOW,
            exiftool=et,
        )
        s.commit()

        pt = s.exec(select(PhotoTag).where(PhotoTag.pixel_hash == ph)).one()
        assert pt.written_to_file is False
        assert et.calls == []  # RAW never written (hard constraint #2)
        assert s.exec(select(Change).where(Change.op == ChangeOp.clip_approve)).one() is not None


# --------------------------------------------------------------------------- #
# reject
# --------------------------------------------------------------------------- #
def test_reject_is_recorded_and_sticky_across_reembed(settings: Settings) -> None:
    _jpeg(_lib(settings) / "red.jpg", (240, 6, 6))
    scan_once(settings)
    scan_clip_once(settings, model=AlignedFakeModel())
    model = settings.config.clip.model

    with _session(settings) as s:
        ph = s.exec(select(Prediction).where(Prediction.tag_name == "red")).one().pixel_hash
        reject(s, device_id=settings.device_id, model=model, pixel_hash=ph, tag_name="red", now=NOW)
        s.commit()
        assert (
            s.exec(select(Prediction).where(Prediction.tag_name == "red")).one().state
            == PredictionState.rejected
        )
        assert s.exec(select(Change).where(Change.op == ChangeOp.clip_reject)).one().field == "red"

    # a full re-embed must not resurrect the rejection as pending
    scan_clip_once(settings, reembed=True, model=AlignedFakeModel())
    with _session(settings) as s:
        preds = s.exec(select(Prediction).where(Prediction.tag_name == "red")).all()
        assert len(preds) == 1
        assert preds[0].state == PredictionState.rejected


def test_reject_creates_row_when_prediction_absent(settings: Settings) -> None:
    ph = "z" * 64
    model = settings.config.clip.model
    with _session(settings) as s:
        _add_photo(s, ph, "/library/z.jpg")
        s.commit()
        reject(
            s, device_id=settings.device_id, model=model, pixel_hash=ph, tag_name="blue", now=NOW
        )
        s.commit()
        row = s.exec(select(Prediction)).one()
        assert row.state == PredictionState.rejected
        assert row.tag_name == "blue"


# --------------------------------------------------------------------------- #
# unapprove (the photo page's ✕ on an approved CLIP tag)
# --------------------------------------------------------------------------- #
def test_unapprove_removes_tag_rejects_and_stays_gone(settings: Settings) -> None:
    _jpeg(_lib(settings) / "red.jpg", (240, 6, 6))
    scan_once(settings)
    scan_clip_once(settings, model=AlignedFakeModel())
    model = settings.config.clip.model

    et = MockExiftool()
    with _session(settings) as s:
        ph = s.exec(select(Prediction).where(Prediction.tag_name == "red")).one().pixel_hash
        approve(
            s,
            device_id=settings.device_id,
            model=model,
            pixel_hash=ph,
            tag_name="red",
            facet="colour",
            now=NOW,
            exiftool=et,
        )
        s.commit()

        assert unapprove(
            s,
            device_id=settings.device_id,
            model=model,
            pixel_hash=ph,
            tag_name="red",
            now=NOW,
            exiftool=et,
        )
        s.commit()

        assert s.exec(select(PhotoTag).where(PhotoTag.pixel_hash == ph)).all() == []
        pred = s.exec(select(Prediction).where(Prediction.tag_name == "red")).one()
        assert pred.state == PredictionState.rejected
        ops = {c.op for c in s.exec(select(Change).where(Change.field == "red")).all()}
        assert {ChangeOp.tag_remove, ChangeOp.clip_reject} <= ops
        # nothing left to take off: a second call is a no-op
        assert not unapprove(
            s,
            device_id=settings.device_id,
            model=model,
            pixel_hash=ph,
            tag_name="red",
            now=NOW,
            exiftool=et,
        )

    assert et.calls[-1] == ("red.jpg", [])  # the reduced (empty) keyword set was embedded

    scan_clip_once(settings, reembed=True, model=AlignedFakeModel())
    with _session(settings) as s:
        preds = s.exec(select(Prediction).where(Prediction.tag_name == "red")).all()
        assert [p.state for p in preds] == [PredictionState.rejected]


def test_unapprove_leaves_manual_tags_alone(settings: Settings) -> None:
    ph = "m" * 64
    with _session(settings) as s:
        _add_photo(s, ph, "/library/m.jpg")
        tag = Tag(name="red")
        s.add(tag)
        s.flush()
        assert tag.id is not None
        s.add(
            PhotoTag(
                pixel_hash=ph,
                tag_id=tag.id,
                source=TagSource.manual,
                added_at=NOW,
                added_by_device="dev",
            )
        )
        s.commit()
        assert not unapprove(
            s,
            device_id=settings.device_id,
            model=settings.config.clip.model,
            pixel_hash=ph,
            tag_name="red",
            now=NOW,
            exiftool=None,
        )
        assert len(s.exec(select(PhotoTag)).all()) == 1


# --------------------------------------------------------------------------- #
# facet sections + per-photo paging
# --------------------------------------------------------------------------- #
def test_facet_sections_follow_vocab_order_and_put_unknown_last(settings: Settings) -> None:
    settings.paths.tags_file.write_text(
        "scene:\n  - beach\n  - forest\nlighting:\n  - sunset\n", encoding="utf-8"
    )
    vocab = load_vocab(settings.paths.tags_file)
    with _session(settings) as s:
        for i, tag in enumerate(["sunset", "beach", "forest", "beach", "retired-tag"]):
            _add_photo(s, f"h{i}", f"/x/{i}.jpg")
            s.add(
                Prediction(
                    pixel_hash=f"h{i}",
                    tag_name=tag,
                    score=0.3,
                    model="m",
                    run_at=NOW,
                    state=PredictionState.pending,
                )
            )
        s.commit()
        groups = pending_groups(s, model="m", vocab=vocab)

    assert [(g.tag_name, g.count) for g in groups] == [
        ("beach", 2),
        ("forest", 1),
        ("retired-tag", 1),
        ("sunset", 1),
    ]
    sections = facet_sections(groups, vocab)
    assert [(sec.facet, [g.tag_name for g in sec.groups]) for sec in sections] == [
        ("scene", ["beach", "forest"]),
        ("lighting", ["sunset"]),
        (None, ["retired-tag"]),
    ]
    assert [sec.count for sec in sections] == [3, 1, 1]


def test_pending_for_tag_pages_best_score_first_and_skips_decided(settings: Settings) -> None:
    with _session(settings) as s:
        for i, score in enumerate([0.25, 0.9, 0.5, 0.7]):
            _add_photo(s, f"h{i}", f"/x/{i}.jpg")
            s.add(
                Prediction(
                    pixel_hash=f"h{i}",
                    tag_name="red",
                    score=score,
                    model="m",
                    run_at=NOW,
                    state=PredictionState.pending,
                )
            )
        s.commit()
        reject(s, device_id="d", model="m", pixel_hash="h1", tag_name="red", now=NOW)
        s.commit()

        first = pending_for_tag(s, model="m", tag_name="red", limit=2)
        second = pending_for_tag(s, model="m", tag_name="red", limit=2, offset=2)
        assert [h for h, _ in first] == ["h3", "h2"]
        assert [h for h, _ in second] == ["h0"]
        assert not is_pending(s, model="m", pixel_hash="h1", tag_name="red")
        assert is_pending(s, model="m", pixel_hash="h3", tag_name="red")
        assert not is_pending(s, model="other", pixel_hash="h3", tag_name="red")
