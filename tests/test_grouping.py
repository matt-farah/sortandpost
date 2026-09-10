"""``sortandpost.grouping`` -- library group-by sections."""

from __future__ import annotations

from datetime import date, datetime

from sortandpost.grouping import (
    GroupRow,
    group_photos,
    parse_folder_sort,
    parse_mode,
)


def _row(
    h: str,
    *,
    taken_at: datetime | None = None,
    path: str = "/lib/a/img.jpg",
    make: str | None = None,
    model: str | None = None,
    mtime_ns: int = 0,
) -> GroupRow:
    return GroupRow(h, taken_at, path, make, model, mtime_ns)


# --- param parsing ----------------------------------------------------------


def test_parse_mode() -> None:
    assert parse_mode("day") == "day"
    assert parse_mode("folder") == "folder"
    assert parse_mode("") is None
    assert parse_mode(None) is None
    assert parse_mode("bogus") is None


def test_parse_folder_sort_falls_back_to_newest() -> None:
    assert parse_folder_sort("az") == "az"
    assert parse_folder_sort("") == "newest"
    assert parse_folder_sort(None) == "newest"
    assert parse_folder_sort("nonsense") == "newest"


# --- day / month ----------------------------------------------------------


def test_day_groups_in_input_order_with_relative_labels() -> None:
    today = date(2026, 9, 10)
    rows = [
        _row("a", taken_at=datetime(2026, 9, 10, 18, 0)),
        _row("b", taken_at=datetime(2026, 9, 10, 8, 0)),
        _row("c", taken_at=datetime(2026, 9, 9, 12, 0)),
        _row("d", taken_at=datetime(2026, 9, 3, 12, 0)),
    ]
    groups = group_photos(rows, "day", today=today)
    assert [(g.label, g.hashes) for g in groups] == [
        ("Today", ["a", "b"]),
        ("Yesterday", ["c"]),
        ("September 3, 2026", ["d"]),
    ]
    assert all(g.sublabel is None for g in groups)


def test_undated_photos_collect_in_a_trailing_no_date_group() -> None:
    rows = [
        _row("a", taken_at=datetime(2026, 9, 10, 8, 0)),
        _row("b"),
        _row("c", taken_at=datetime(2026, 9, 9, 8, 0)),
        _row("d"),
    ]
    groups = group_photos(rows, "day", today=date(2026, 9, 10))
    assert groups[-1].label == "No date"
    assert groups[-1].hashes == ["b", "d"]
    assert [g.label for g in groups[:-1]] == ["Today", "Yesterday"]


def test_month_groups() -> None:
    rows = [
        _row("a", taken_at=datetime(2026, 9, 30, 8, 0)),
        _row("b", taken_at=datetime(2026, 9, 1, 8, 0)),
        _row("c", taken_at=datetime(2026, 8, 15, 8, 0)),
        _row("d", taken_at=datetime(2025, 12, 25, 8, 0)),
    ]
    groups = group_photos(rows, "month", today=date(2026, 9, 10))
    assert [(g.label, g.hashes) for g in groups] == [
        ("September 2026", ["a", "b"]),
        ("August 2026", ["c"]),
        ("December 2025", ["d"]),
    ]


def test_all_undated_yields_one_no_date_group() -> None:
    groups = group_photos([_row("a"), _row("b")], "day", today=date(2026, 9, 10))
    assert len(groups) == 1
    assert groups[0].label == "No date" and groups[0].hashes == ["a", "b"]


def test_empty_input_yields_no_groups() -> None:
    assert group_photos([], "day", today=date(2026, 9, 10)) == []
    assert group_photos([], "folder") == []
    assert group_photos([], "camera") == []


# --- camera --------------------------------------------------------------


def test_camera_groups_ordered_by_newest_photo_unknown_last() -> None:
    rows = [
        _row("a", taken_at=datetime(2026, 1, 5), make="Fujifilm", model="X-T5"),
        _row("b", taken_at=datetime(2026, 9, 1), make="Sony", model="ILCE-7M4"),
        _row("c", taken_at=datetime(2026, 3, 1), make="Fujifilm", model="X-T5"),
        _row("d", taken_at=datetime(2026, 8, 1)),  # no camera
    ]
    groups = group_photos(rows, "camera")
    assert [(g.label, g.hashes) for g in groups] == [
        ("Sony ILCE-7M4", ["b"]),
        ("Fujifilm X-T5", ["a", "c"]),
        ("Unknown camera", ["d"]),
    ]


def test_camera_label_avoids_repeating_make_in_model() -> None:
    rows = [_row("a", taken_at=datetime(2026, 1, 1), make="Canon", model="Canon EOS R6")]
    assert group_photos(rows, "camera")[0].label == "Canon EOS R6"


def test_camera_make_only_still_groups() -> None:
    rows = [
        _row("a", taken_at=datetime(2026, 1, 1), make="Apple", model=None),
        _row("b", taken_at=datetime(2026, 2, 1), make="Apple"),
    ]
    groups = group_photos(rows, "camera")
    assert len(groups) == 1 and groups[0].label == "Apple" and groups[0].hashes == ["a", "b"]


# --- folder --------------------------------------------------------------


def _folder_rows() -> list[GroupRow]:
    return [
        _row("a", path="/lib/2026/summer/a.jpg", mtime_ns=300),
        _row("b", path="/lib/2026/summer/b.jpg", mtime_ns=100),
        _row("c", path="/lib/2024/winter/c.jpg", mtime_ns=500),
        _row("d", path="/lib/archive/d.jpg", mtime_ns=200),
    ]


def test_folder_header_is_leaf_name_with_full_path_sublabel() -> None:
    groups = group_photos(_folder_rows(), "folder", folder_sort="az")
    by_label = {g.label: g for g in groups}
    assert set(by_label) == {"summer", "winter", "archive"}
    assert by_label["summer"].sublabel == "/lib/2026/summer"
    assert by_label["summer"].hashes == ["a", "b"]


def test_folder_sort_newest_and_oldest_use_max_mtime() -> None:
    newest = group_photos(_folder_rows(), "folder", folder_sort="newest")
    assert [g.label for g in newest] == ["winter", "summer", "archive"]  # 500, 300, 200
    oldest = group_photos(_folder_rows(), "folder", folder_sort="oldest")
    assert [g.label for g in oldest] == ["archive", "summer", "winter"]


def test_folder_sort_az_and_za() -> None:
    az = group_photos(_folder_rows(), "folder", folder_sort="az")
    assert [g.label for g in az] == ["archive", "summer", "winter"]
    za = group_photos(_folder_rows(), "folder", folder_sort="za")
    assert [g.label for g in za] == ["winter", "summer", "archive"]


def test_folder_handles_windows_paths() -> None:
    rows = [
        _row("a", path=r"C:\Photos\Trips\Rome\a.jpg", mtime_ns=1),
        _row("b", path=r"C:\Photos\Trips\Rome\b.jpg", mtime_ns=2),
    ]
    (grp,) = group_photos(rows, "folder")
    assert grp.label == "Rome"
    assert grp.sublabel == r"C:\Photos\Trips\Rome"
    assert grp.hashes == ["a", "b"]


def test_within_folder_photos_keep_incoming_order() -> None:
    rows = [
        _row("x", path="/p/f/x.jpg", mtime_ns=1),
        _row("y", path="/p/f/y.jpg", mtime_ns=1),
        _row("z", path="/p/f/z.jpg", mtime_ns=1),
    ]
    (grp,) = group_photos(rows, "folder")
    assert grp.hashes == ["x", "y", "z"]
