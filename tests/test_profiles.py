"""Profile store + profile pictures (sortandpost.profiles)."""

from __future__ import annotations

import io
import json
import os
import uuid
from datetime import datetime
from pathlib import Path

import pytest
from PIL import Image

from sortandpost.paths import Paths
from sortandpost.profiles import (
    AVATAR_KEEP_OLD,
    AVATAR_SIZE,
    MAX_UPLOAD_BYTES,
    AvatarError,
    Crop,
    FileProfileStore,
    Profile,
    avatars_dir,
    load_profile,
    profile_file,
    render_avatar,
    save_profile,
)

DEV_A = "aaaaaaaa" * 4
DEV_B = "bbbbbbbb" * 4


def _clock(ts: str = "2026-10-17T12:00:00+00:00"):  # type: ignore[no-untyped-def]
    return lambda: datetime.fromisoformat(ts)


def _jpeg(size: tuple[int, int] = (300, 200), color: str = "red", **save: object) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, "JPEG", **save)  # type: ignore[arg-type]
    return out.getvalue()


def _webp(seed: int) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (8, 8), ((seed * 29) % 256, (seed * 71) % 256, 0)).save(
        out, "WEBP", lossless=True
    )
    return out.getvalue()


# --- the profile file -----------------------------------------------------------


def test_legacy_profile_loads_and_first_save_adds_a_stable_id(paths: Paths) -> None:
    profile_file(paths).write_text(json.dumps({"name": "Matt", "mode": "devil"}))
    p = load_profile(paths)
    assert (p.id, p.name, p.mode, p.avatar) == ("", "Matt", "percival", "")  # was Devil
    assert json.loads(profile_file(paths).read_text()) == {"name": "Matt", "mode": "devil"}

    first = save_profile(paths, name="Matt", mode="tugs", device_id=DEV_A)
    uuid.UUID(first.id)
    again = save_profile(paths, name="M", mode="tugs", device_id=DEV_B)
    assert again.id == first.id == load_profile(paths).id


def test_bad_fields_are_ignored(paths: Paths) -> None:
    profile_file(paths).write_text(json.dumps({"id": "nope", "avatar": "../../etc", "mode": "x"}))
    p = load_profile(paths)
    assert (p.id, p.avatar, p.mode) == ("", "", "tugs")


def test_initials() -> None:
    assert Profile(name="matt farah").initials == "MF"
    assert Profile(name="Ann").initials == "A"
    assert Profile().initials == ""


# --- avatars --------------------------------------------------------------------


def test_set_avatar_keeps_name_and_mode_and_vice_versa(paths: Paths) -> None:
    store = FileProfileStore(paths, DEV_A, _clock())
    store.save(name="Matt", mode="percival")
    p = store.set_avatar(_webp(1))
    assert (p.name, p.mode) == ("Matt", "percival")
    assert (avatars_dir(paths) / f"{p.avatar}.webp").read_bytes() == _webp(1)

    p2 = FileProfileStore(paths, DEV_B).save(name="Matt F", mode="tugs")
    assert p2.avatar == p.avatar
    assert store.avatar_url(p2) == f"/profile-avatar/{p.avatar}.webp"


def test_same_picture_twice_is_one_file(paths: Paths) -> None:
    store = FileProfileStore(paths, DEV_A)
    a = store.set_avatar(_webp(1)).avatar
    b = FileProfileStore(paths, DEV_B).set_avatar(_webp(1)).avatar
    assert a == b
    assert len(list(avatars_dir(paths).glob("*.webp"))) == 1


def test_prune_keeps_current_and_newest_others(paths: Paths) -> None:
    store = FileProfileStore(paths, DEV_A)
    digests = []
    for i in range(AVATAR_KEEP_OLD + 4):
        digests.append(store.set_avatar(_webp(i)).avatar)
        f = avatars_dir(paths) / f"{digests[-1]}.webp"
        os.utime(f, (1_000_000 + i, 1_000_000 + i))
    left = {p.stem for p in avatars_dir(paths).glob("*.webp")}
    assert left == set(digests[-(AVATAR_KEEP_OLD + 1) :])


def test_clear_and_missing_file_fall_back(paths: Paths) -> None:
    store = FileProfileStore(paths, DEV_A)
    p = store.set_avatar(_webp(1))
    (avatars_dir(paths) / f"{p.avatar}.webp").unlink()  # not synced here yet
    assert store.avatar_url(store.get()) is None
    assert store.clear_avatar().avatar == ""
    assert store.avatar_url(store.get()) is None


def test_avatar_file_rejects_non_digests(paths: Paths) -> None:
    store = FileProfileStore(paths, DEV_A)
    assert store.avatar_file("../profile") is None
    assert store.avatar_file("A" * 64) is None


# --- rendering --------------------------------------------------------------------


def test_render_upload_is_square_webp_without_metadata() -> None:
    exif = Image.Exif()
    exif[0x010F] = "CameraMaker"
    exif.get_ifd(0x8825)[2] = (1.0, 2.0, 3.0)  # GPSLatitude
    out = render_avatar(_jpeg(exif=exif.tobytes()), Crop())
    with Image.open(io.BytesIO(out)) as im:
        assert im.format == "WEBP"
        assert im.size == (AVATAR_SIZE, AVATAR_SIZE)
        assert not im.getexif()
        assert "icc_profile" not in im.info


def test_render_honors_orientation_and_crop(tmp_path: Path) -> None:
    # 400x200 stored, left half red / right half blue; Orientation 6 = rotate 90° CW
    img = Image.new("RGB", (400, 200), "red")
    img.paste(Image.new("RGB", (200, 200), "blue"), (200, 0))
    exif = Image.Exif()
    exif[0x0112] = 6
    src = tmp_path / "a.jpg"
    img.save(src, "JPEG", exif=exif.tobytes(), quality=95)
    # upright it is 200x400: red on top, blue below
    top = render_avatar(str(src), Crop(cy=0.0))
    bottom = render_avatar(str(src), Crop(cy=1.0))
    with Image.open(io.BytesIO(top)) as t, Image.open(io.BytesIO(bottom)) as b:
        tr, _, tb = t.convert("RGB").getpixel((256, 256))  # type: ignore[misc]
        br, _, bb = b.convert("RGB").getpixel((256, 256))  # type: ignore[misc]
    assert tr > 200 and tb < 60
    assert bb > 200 and br < 60


def test_crop_box_math() -> None:
    assert Crop().box(300, 200) == (50, 0, 250, 200)
    assert Crop(cx=0.0).box(300, 200) == (0, 0, 200, 200)
    assert Crop(cx=1.0, zoom=2.0).box(300, 200) == (200, 50, 300, 150)
    assert Crop(cx=9, cy=-1, zoom=0.1).box(300, 200) == (100, 0, 300, 200)  # clamped


def test_garbage_and_oversize_uploads_are_refused() -> None:
    with pytest.raises(AvatarError):
        render_avatar(b"not an image", Crop())
    with pytest.raises(AvatarError):
        render_avatar(b"\0" * (MAX_UPLOAD_BYTES + 1), Crop())


def test_decompression_bomb_is_refused() -> None:
    # tiny on disk, 20000 x 20000 pixels when decoded
    out = io.BytesIO()
    Image.new("1", (20000, 20000)).save(out, "PNG")
    with pytest.raises(AvatarError):
        render_avatar(out.getvalue(), Crop())
