"""``sortandpost.achievements`` -- definitions, profile, per-device unlocks."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from sqlalchemy.engine import Engine
from sqlmodel import Session

from sortandpost.achievements import (
    EVENT_DEVIL_MODE,
    EVENT_GITHUB_SEARCH,
    EVENT_KONAMI,
    AchievementTracker,
    Metrics,
    all_unlocks,
    compute_metrics,
    definitions,
    due,
    load_profile,
    parse_definitions,
    parse_trigger,
    rated_count,
    resource_path,
    save_profile,
)
from sortandpost.collections import create_collection
from sortandpost.config import StackingConfig
from sortandpost.db.models import Photo, PhotoPath, Rating
from sortandpost.paths import Paths
from sortandpost.posts import create_post, save_post

DEV_A = "aaaaaaaa" * 4
DEV_B = "bbbbbbbb" * 4


def _clock(ts: str = "2026-10-05T12:00:00+00:00"):  # type: ignore[no-untyped-def]
    return lambda: datetime.fromisoformat(ts)


def _photo(session: Session, tag: str, path: str, *, raw: bool = False, first: datetime) -> str:
    h = (tag * 64)[:64]
    session.add(
        Photo(
            pixel_hash=h,
            primary_path=path,
            width=10,
            height=10,
            format="raw" if raw else "jpeg",
            is_raw=raw,
            file_size=1,
            file_mtime_ns=1,
            first_indexed_at=first,
            last_indexed_at=first,
        )
    )
    session.add(PhotoPath(pixel_hash=h, path=path, size=1, mtime_ns=1))
    return h


def _rate(session: Session, h: str, stars: int) -> None:
    session.add(
        Rating(pixel_hash=h, stars=stars, updated_at=datetime.now(UTC), updated_by_device="x")
    )


# --- definitions --------------------------------------------------------------


def test_bundled_table_parses_every_row() -> None:
    defs = definitions()
    src = Path(__file__).parents[1] / "Achievements" / "Achievements.md"
    rows = [ln for ln in src.read_text(encoding="utf-8").splitlines() if ln.startswith("| ")]
    assert len(defs) == len(rows) - 2  # minus the header + separator rows
    keys = [a.key for a in defs]
    assert len(set(keys)) == len(keys)
    by = {a.key: a for a in defs}
    assert by["nice-photographer"].trigger.threshold == 69
    assert by["nice-photographer"].trigger.not_first_day
    assert not by["nice-photographer"].shown
    assert by["beginner-photographer"].trigger.threshold == 1  # "Index one photo"
    assert by["beginner-collector"].trigger.metric == "collections"
    assert by["beginner-poster"].trigger.metric == "posts"
    assert by["expert-rater"].points == 1000
    assert by["reader"].trigger.event == EVENT_GITHUB_SEARCH
    assert by["konami-code"].trigger.event == EVENT_KONAMI
    assert by["devil-mode"].trigger.event == EVENT_DEVIL_MODE


def test_bundled_copy_matches_the_repo_table() -> None:
    repo = Path(__file__).parents[1] / "Achievements" / "Achievements.md"
    assert resource_path("achievements.md").read_text(encoding="utf-8") == repo.read_text(
        encoding="utf-8"
    )
    assert resource_path("Angel.png").is_file()
    assert resource_path("Devil.png").is_file()


def test_parse_trigger_variants() -> None:
    assert parse_trigger("Rate 5000 photos").threshold == 5000  # type: ignore[union-attr]
    assert parse_trigger("index 1 photo -- cannot be triggered on first day").not_first_day  # type: ignore[union-attr]
    assert parse_trigger("Do a barrel roll") is None


def test_unparseable_row_is_skipped() -> None:
    md = (
        "| Name | Trigger | Points | Shown | A | D |\n|---|---|---|---|---|---|\n"
        "| Ok | Rate 2 photos | 5 | Y | a | d |\n| Bad | Juggle | 5 | Y | a | d |\n"
    )
    assert [a.name for a in parse_definitions(md)] == ["Ok"]


def test_markdown_escapes_kept_for_rendering() -> None:
    by = {a.key: a for a in definitions()}
    assert "\\*" in by["devil-mode"].devil
    assert by["devil-mode"].description("angel") == by["devil-mode"].angel


# --- profile ------------------------------------------------------------------


def test_profile_defaults_to_angel(paths: Paths) -> None:
    p = load_profile(paths)
    assert (p.name, p.mode) == ("", "angel")


def test_profile_round_trip_cleans_name(paths: Paths) -> None:
    save_profile(paths, name="  Matt\n  F ", mode="devil", device_id=DEV_A)
    p = load_profile(paths)
    assert (p.name, p.mode, p.updated_by_device) == ("Matt F", "devil", DEV_A)
    assert (paths.sync / "profile.json").is_file()


def test_corrupt_profile_falls_back(paths: Paths) -> None:
    (paths.sync / "profile.json").write_text("{nope", encoding="utf-8")
    assert load_profile(paths).mode == "angel"


# --- metrics + due --------------------------------------------------------------


def test_due_respects_first_day_rule() -> None:
    defs = definitions()
    first_day = Metrics(indexed=100, first_day=True)
    keys = {a.key for a in due(defs, first_day, [])}
    assert "beginner-photographer" in keys
    assert "nice-photographer" not in keys
    later = {a.key for a in due(defs, Metrics(indexed=100, first_day=False), [])}
    assert "nice-photographer" in later
    assert "senior-photographer" not in later


def test_due_skips_already_unlocked() -> None:
    keys = {a.key for a in due(definitions(), Metrics(rated=1), ["beginner-rater"])}
    assert "beginner-rater" not in keys


def test_rated_count_counts_a_stack_once(engine: Engine) -> None:
    first = datetime(2026, 1, 1)
    with Session(engine) as s:
        a = _photo(s, "a", "/lib/IMG_1.CR2", raw=True, first=first)
        b = _photo(s, "b", "/lib/IMG_1.JPG", first=first)
        c = _photo(s, "c", "/lib/IMG_2.JPG", first=first)
        d = _photo(s, "d", "/lib/IMG_3.JPG", first=first)
        s.commit()
        for h in (a, b, c):
            _rate(s, h, 4)
        _rate(s, d, 0)  # unrated
        s.commit()
        assert rated_count(s, StackingConfig(enabled=False)) == 3
        assert rated_count(s, StackingConfig(enabled=True)) == 2
        # below the floor -> the cheap raw count, no stack build
        assert rated_count(s, StackingConfig(enabled=True), at_least=10) == 3


def test_compute_metrics(engine: Engine, paths: Paths) -> None:
    yesterday = datetime.now(UTC) - timedelta(days=2)
    with Session(engine) as s:
        _photo(s, "a", "/lib/a.jpg", first=yesterday)
        s.commit()
        create_collection(paths, "Trip")
        sent = create_post(paths, title="One", photos=[])
        sent.status = "scheduled"
        save_post(paths, sent)
        create_post(paths, title="Draft", photos=[])
        partly = create_post(paths, title="Partly", photos=[])
        partly.status = "failed"
        partly.parts = [{"status": "sent", "channel_ids": ["c1"]}]
        save_post(paths, partly)
        m = compute_metrics(s, paths, StackingConfig())
    assert (m.indexed, m.collections, m.posts, m.rated) == (1, 1, 2, 0)
    assert m.first_day is False


def test_first_day_true_for_today_or_empty(engine: Engine, paths: Paths) -> None:
    with Session(engine) as s:
        assert compute_metrics(s, paths, StackingConfig()).first_day
        _photo(s, "a", "/lib/a.jpg", first=datetime.now(UTC))
        s.commit()
        assert compute_metrics(s, paths, StackingConfig(), today=date.today()).first_day


# --- tracker: per-device files, union -------------------------------------------


def test_check_writes_only_this_devices_file(paths: Paths) -> None:
    t = AchievementTracker(paths, DEV_A, "laptop", clock=_clock())
    got = t.check(Metrics(indexed=1, collections=1))
    assert {u.achievement.key for u in got} == {"beginner-photographer", "beginner-collector"}
    files = list(paths.devices_dir.glob("*/achievements.json"))
    assert [f.parent.name for f in files] == ["laptop-aaaaaaaa"]
    assert t.check(Metrics(indexed=1, collections=1)) == []  # sticky, not re-unlocked
    assert t.score() == 0  # sealed: no points until revealed
    t.reveal("beginner-photographer")
    assert t.score() == 10
    t.reveal("beginner-collector")
    assert t.score() == 20


def test_union_across_devices_earliest_wins(paths: Paths) -> None:
    a = AchievementTracker(paths, DEV_A, "laptop", clock=_clock("2026-10-05T12:00:00+00:00"))
    b = AchievementTracker(paths, DEV_B, "desktop", clock=_clock("2026-10-01T12:00:00+00:00"))
    a.check(Metrics(indexed=1))
    # B unlocked the same one while offline (its file synced in later)
    b.own_file.write_text(
        json.dumps(
            {
                "unlocked": {
                    "beginner-photographer": "2026-10-01T12:00:00+00:00",
                    "beginner-rater": "2026-10-01T12:00:00+00:00",
                }
            }
        ),
        encoding="utf-8",
    )
    u = all_unlocks(paths)
    assert u["beginner-photographer"].startswith("2026-10-01")
    assert set(u) == {"beginner-photographer", "beginner-rater"}
    # A sees B's unlock: it won't re-record it in its own file
    assert a.check(Metrics(rated=1)) == []
    a.reveal("beginner-photographer")
    a.reveal("beginner-rater")
    assert a.score() == 15


def test_conflict_copy_is_folded_in(paths: Paths) -> None:
    t = AchievementTracker(paths, DEV_A, "laptop", clock=_clock())
    t.check(Metrics(indexed=1))
    own = t.own_file
    (own.parent / "achievements (1).json").write_text(
        json.dumps({"unlocked": {"konami-code": "2026-10-02T00:00:00+00:00"}}), encoding="utf-8"
    )
    assert "konami-code" in all_unlocks(paths)


def test_events_unlock_once_and_feed_since(paths: Paths) -> None:
    t = AchievementTracker(paths, DEV_A, "laptop", clock=_clock())
    start = t.seq
    assert [u.achievement.key for u in t.record_event(EVENT_KONAMI)] == ["konami-code"]
    assert t.record_event(EVENT_KONAMI) == []
    assert t.record_event("nonsense") == []
    assert [u.achievement.key for u in t.since(start)] == ["konami-code"]
    assert t.since(t.seq) == []


def test_corrupt_own_file_is_ignored(paths: Paths) -> None:
    t = AchievementTracker(paths, DEV_A, "laptop", clock=_clock())
    t.own_file.write_text("garbage", encoding="utf-8")
    assert all_unlocks(paths) == {}
    assert t.record_event(EVENT_DEVIL_MODE)
    assert "devil-mode" in all_unlocks(paths)


def test_rated_floor(paths: Paths) -> None:
    t = AchievementTracker(paths, DEV_A, "laptop", clock=_clock())
    assert t.rated_floor() == 1
    t.check(Metrics(rated=70))
    assert t.rated_floor() == 100


def test_new_unlocks_stay_sealed_until_revealed(paths: Paths) -> None:
    a = AchievementTracker(paths, DEV_A, "laptop", clock=_clock())
    b = AchievementTracker(paths, DEV_B, "desktop", clock=_clock())
    a.check(Metrics(indexed=1, rated=1))
    assert a.sealed() == ["beginner-photographer", "beginner-rater"]
    v = a.version
    a.reveal("beginner-rater")
    assert a.version == v + 1
    assert a.sealed() == ["beginner-photographer"]
    assert b.sealed() == ["beginner-photographer"]  # a reveal counts on every device
    b.reveal("beginner-photographer")
    assert a.sealed() == []
    # revealing never touches the unlocks themselves
    assert set(all_unlocks(paths)) == {"beginner-photographer", "beginner-rater"}
    # a later unlock keeps the earlier reveals in the device file
    a.record_event(EVENT_KONAMI)
    assert a.sealed() == ["konami-code"]
