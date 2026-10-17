"""Profile picture UI: the round avatar (header chip, Settings, the wizard),
its editor (Upload / Choose from library / Remove), the square-crop dialog and
the photo page's "Use as profile picture".

Everything reads and writes through :func:`sortandpost.profiles.profile_store`
-- never the files -- so a hosted build can swap the store. Rendering runs in a
one-shot worker process (docs/architecture.md constraint #7); the crop dialog
is plain NiceGUI (no JS library: works offline, nothing to bundle). Writes
need the write lock: callers only offer them when writable, and
:func:`sortandpost.sync.changelog.ensure_writable` catches a handover race.

Like ``posts_ui`` this reuses ``app``'s helpers through a lazy import.
"""

from __future__ import annotations

import base64
import logging
import multiprocessing
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from typing import Any

from fastapi import HTTPException
from fastapi.responses import FileResponse
from nicegui import app as nicegui_app
from nicegui import events, ui
from nicegui import run as nicegui_run
from sqlalchemy.engine import Engine
from sqlmodel import Session, col, select

from sortandpost.config import Settings
from sortandpost.db.models import Photo, PhotoPath, Rating
from sortandpost.post_export import resolve_sources
from sortandpost.procwatch import pool_worker_init
from sortandpost.profiles import (
    AVATAR_URL,
    MAX_UPLOAD_BYTES,
    MAX_ZOOM,
    AvatarError,
    Crop,
    FileProfileStore,
    Profile,
    ProfileStore,
    preview_jpeg,
    profile_store,
    render_avatar,
)
from sortandpost.sync.changelog import ReadOnlyError, ensure_writable

log = logging.getLogger(__name__)

_SETTINGS: Settings | None = None
_ENGINE: Engine | None = None
# Bumped on every avatar change in this process, so open tabs' header chips
# repaint without re-reading sync/ every few seconds.
VERSION = 0
_PICKER_LIMIT = 60
_ACCEPT = "image/jpeg,image/png,image/webp,image/heic,image/heif,.heic,.heif"

_CSS = """
.sp-avatar { border-radius: 50%; object-fit: cover; flex-shrink: 0; overflow: hidden;
  display: inline-flex; align-items: center; justify-content: center;
  background-color: var(--sp-surface-2); color: var(--sp-text-dim);
  border: 1px solid var(--sp-border); font-weight: 700; user-select: none; }
.sp-avatar-sm { width: 1.5rem; height: 1.5rem; font-size: .65rem; }
.sp-avatar-lg { width: 4.5rem; height: 4.5rem; font-size: 1.4rem; }
.sp-avatar-crop { width: min(18rem, 70vw); aspect-ratio: 1; border-radius: 50%;
  overflow: hidden; border: 2px solid var(--q-primary); background: var(--sp-surface-2); }
.sp-avatar-crop img { width: 100%; height: 100%; object-fit: cover; display: block;
  cursor: grab; touch-action: none; user-select: none; }
.sp-avatar-crop img.sp-dragging { cursor: grabbing; }
.sp-avatar-pick { display: grid; gap: .4rem; width: 100%;
  grid-template-columns: repeat(auto-fill, minmax(6rem, 1fr)); }
.sp-avatar-pick img { width: 100%; aspect-ratio: 1; object-fit: cover; border-radius: .5rem;
  cursor: pointer; border: 2px solid transparent; }
.sp-avatar-pick img:hover, .sp-avatar-pick img:focus { border-color: var(--q-primary); }
"""


# Drag the crop preview. One delegated listener: the dialog's <img> comes and
# goes. Moving the pointer by d frame pixels moves the visible square by
# d * side / frame image pixels, i.e. by that over the slack (W - side) in
# cx / cy -- the inverse of Crop.box. Reports the end position to Python.
_DRAG_JS = """
<script>
(function () {
  const clamp = (v) => Math.min(1, Math.max(0, v));
  document.addEventListener('pointerdown', (e) => {
    const img = e.target.closest && e.target.closest('.sp-avatar-crop img');
    if (!img || !img.naturalWidth) return;
    e.preventDefault();
    img.setPointerCapture(e.pointerId);
    img.classList.add('sp-dragging');
    const frame = img.parentElement.getBoundingClientRect().width || 1;
    const W = img.naturalWidth, H = img.naturalHeight;
    const side = Math.min(W, H) / (parseFloat(img.dataset.zoom) || 1);
    const k = side / frame;
    const x0 = e.clientX, y0 = e.clientY;
    const cx0 = parseFloat(img.dataset.cx) || 0.5, cy0 = parseFloat(img.dataset.cy) || 0.5;
    let cx = cx0, cy = cy0;
    const move = (ev) => {
      if (W > side) cx = clamp(cx0 - (ev.clientX - x0) * k / (W - side));
      if (H > side) cy = clamp(cy0 - (ev.clientY - y0) * k / (H - side));
      const pos = (cx * 100) + '% ' + (cy * 100) + '%';
      img.style.objectPosition = pos;
      img.style.transformOrigin = pos;
    };
    const end = () => {
      img.removeEventListener('pointermove', move);
      img.removeEventListener('pointerup', end);
      img.removeEventListener('pointercancel', end);
      img.classList.remove('sp-dragging');
      img.dataset.cx = cx; img.dataset.cy = cy;
      img.dispatchEvent(new CustomEvent('sp-crop-move', {detail: {cx: cx, cy: cy}}));
    };
    img.addEventListener('pointermove', move);
    img.addEventListener('pointerup', end);
    img.addEventListener('pointercancel', end);
  });
})();
</script>
"""


def start(settings: Settings, engine: Engine) -> None:
    """Serve avatars (behind the server guard, like every route) and keep the
    handles the editor needs."""
    global _SETTINGS, _ENGINE
    _SETTINGS, _ENGINE = settings, engine
    ui.add_css(_CSS, shared=True)
    ui.add_body_html(_DRAG_JS, shared=True)

    @nicegui_app.get(AVATAR_URL + "/{name}")
    def _avatar(name: str) -> FileResponse:
        digest = name.removesuffix(".webp")
        path = _file_store(settings).avatar_file(digest) if name.endswith(".webp") else None
        if path is None:
            raise HTTPException(status_code=404)
        # content-addressed: a digest's bytes never change
        return FileResponse(
            path, media_type="image/webp", headers={"Cache-Control": "max-age=31536000, immutable"}
        )


def store(settings: Settings) -> ProfileStore:
    return profile_store(settings.paths, settings.device_id)


def _file_store(settings: Settings) -> FileProfileStore:
    return FileProfileStore(settings.paths, settings.device_id)


def avatar(profile: Profile, url: str | None, *, size: str = "sm") -> ui.element:
    """The round picture, or the name's initials / a person icon."""
    if url:
        return ui.image(url).classes(f"sp-avatar sp-avatar-{size}").props("no-spinner")
    with ui.element("div").classes(f"sp-avatar sp-avatar-{size}") as box:
        if profile.initials:
            ui.label(profile.initials)
        else:
            ui.icon("person")
    return box


# --------------------------------------------------------------------------- #
# work off the event loop
# --------------------------------------------------------------------------- #
def _in_worker[T](fn: Callable[..., T], *args: Any) -> T:
    """Run *fn* in a one-shot spawn worker (decode is CPU-bound; constraint #7).
    Called from a thread (``run.io_bound``)."""
    ctx = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=1, mp_context=ctx, initializer=pool_worker_init) as pool:
        return pool.submit(fn, *args).result()


def _render_and_save(settings: Settings, source: str | bytes, crop: Crop) -> Profile:
    webp = _in_worker(render_avatar, source, crop)
    ensure_writable()
    return store(settings).set_avatar(webp)


def _clear(settings: Settings) -> Profile:
    ensure_writable()
    return store(settings).clear_avatar()


def _bump() -> None:
    global VERSION
    VERSION += 1


# --------------------------------------------------------------------------- #
# the crop dialog
# --------------------------------------------------------------------------- #
def crop_dialog(
    settings: Settings,
    preview_src: str,
    source: str | bytes,
    on_saved: Callable[[Profile], None] | None = None,
) -> None:
    """Pick the square: drag the picture to move it, Zoom with the slider,
    over a round preview of the upright image. The same numbers drive CSS
    ``object-position`` + ``transform-origin`` here and :meth:`Crop.box` in the
    render, so what is shown is what is saved. The drag runs in the browser
    (``_DRAG_JS``) and reports where it ended."""
    client = ui.context.client
    state = {"cx": 0.5, "cy": 0.5, "zoom": 1.0}
    with ui.dialog() as dialog, ui.card().classes("items-center gap-3 p-6"):
        ui.label("Profile picture").classes("sp-display text-xl font-semibold")
        with ui.element("div").classes("sp-avatar-crop"):
            img = ui.element("img").props(f'src="{preview_src}" alt="" draggable="false"')
        ui.label("Drag to move").classes("text-xs sp-faint")
        with ui.row().classes("w-64 items-center gap-2 no-wrap"):
            ui.icon("zoom_out").classes("sp-muted")
            zoom = ui.slider(min=1.0, max=MAX_ZOOM, step=0.05, value=1.0).classes("grow")
            ui.icon("zoom_in").classes("sp-muted")

        def _paint() -> None:
            x, y, z = state["cx"] * 100, state["cy"] * 100, state["zoom"]
            img.style(
                f"object-position: {x}% {y}%; transform-origin: {x}% {y}%; transform: scale({z})"
            )
            img.props(f'data-cx="{state["cx"]}" data-cy="{state["cy"]}" data-zoom="{z}"')

        def _zoomed() -> None:
            state["zoom"] = float(zoom.value)
            _paint()

        def _moved(e: events.GenericEventArguments) -> None:
            detail = e.args.get("detail") if isinstance(e.args, dict) else None
            if isinstance(detail, dict):
                for k in ("cx", "cy"):
                    v = detail.get(k)
                    if isinstance(v, int | float):
                        state[k] = min(max(float(v), 0.0), 1.0)
            _paint()

        zoom.on_value_change(_zoomed)
        img.on("sp-crop-move", _moved, args=["detail"])
        _paint()

        with ui.row().classes("gap-2 justify-end w-full"):
            ui.button("Cancel", on_click=dialog.close).props("flat no-caps")
            save = ui.button("Save", icon="check").props("color=primary unelevated no-caps")

        async def _save() -> None:
            crop = Crop(cx=state["cx"], cy=state["cy"], zoom=state["zoom"])
            save.props("loading")
            try:
                profile = await nicegui_run.io_bound(_render_and_save, settings, source, crop)
            except ReadOnlyError:
                with client:
                    ui.notify("Not saved: another computer is editing", type="warning")
                return
            except (AvatarError, OSError) as exc:
                with client:
                    ui.notify(f"Couldn't use that picture: {exc}", type="negative")
                return
            finally:
                save.props(remove="loading")
            if profile is None:  # app shutting down
                return
            _bump()
            with client:
                dialog.close()
                ui.notify("Profile picture saved", position="bottom")
                if on_saved is not None:
                    on_saved(profile)

        save.on_click(_save)
    dialog.open()


def crop_library_photo(
    settings: Settings,
    engine: Engine,
    pixel_hash: str,
    on_saved: Callable[[Profile], None] | None = None,
) -> None:
    """Open the crop dialog for one library photo (its large thumbnail is the
    preview; the render decodes the file itself)."""
    from sortandpost import app  # lazy: app imports this module

    src = resolve_sources(engine, [pixel_hash]).get(pixel_hash)
    if src is None:
        ui.notify("That photo's file isn't on this computer", type="warning")
        return
    size = max(settings.config.thumbnails.sizes)
    crop_dialog(settings, app._thumb_src(pixel_hash, size), src, on_saved)


async def _crop_upload(
    settings: Settings, e: events.UploadEventArguments, on_saved: Callable[[Profile], None]
) -> None:
    data = await e.file.read()
    try:
        preview = await nicegui_run.io_bound(_in_worker, preview_jpeg, data)
    except AvatarError as exc:
        ui.notify(str(exc), type="negative")
        return
    if preview is None:
        return
    url = "data:image/jpeg;base64," + base64.b64encode(preview).decode("ascii")
    crop_dialog(settings, url, data, on_saved)


# --------------------------------------------------------------------------- #
# choose from library
# --------------------------------------------------------------------------- #
def _picker_photos(engine: Engine) -> list[str]:
    """Best-rated first, then newest; only photos with a present copy."""
    with Session(engine) as s:
        present = select(PhotoPath.pixel_hash).where(PhotoPath.present == True)  # noqa: E712
        rows = s.exec(
            select(Photo.pixel_hash)
            .outerjoin(Rating, col(Rating.pixel_hash) == col(Photo.pixel_hash))
            .where(col(Photo.pixel_hash).in_(present))
            .order_by(
                col(Rating.stars).desc().nulls_last(), col(Photo.taken_at).desc().nulls_last()
            )
            .limit(_PICKER_LIMIT)
        ).all()
    return list(rows)


async def _pick_from_library(
    settings: Settings, engine: Engine, on_saved: Callable[[Profile], None]
) -> None:
    from sortandpost import app  # lazy: app imports this module

    hashes = await nicegui_run.io_bound(_picker_photos, engine)
    if not hashes:
        ui.notify("No photos to choose from yet", type="info")
        return
    size = min(settings.config.thumbnails.sizes)
    with ui.dialog() as dialog, ui.card().classes("w-[40rem] max-w-[92vw] gap-3"):
        ui.label("Choose a photo").classes("sp-display text-xl font-semibold")
        ui.label(
            "Your best-rated photos first. Any photo's page also has “Use as profile picture”."
        ).classes("text-xs sp-faint")
        with ui.element("div").classes("sp-avatar-pick"):
            for h in hashes:

                def _choose(h: str = h) -> None:
                    dialog.close()
                    crop_library_photo(settings, engine, h, on_saved)

                ui.element("img").props(
                    f'src="{app._thumb_src(h, size)}" alt="" tabindex="0" loading="lazy"'
                ).on("click", _choose).on("keydown.enter", _choose)
        with ui.row().classes("justify-end w-full"):
            ui.button("Cancel", on_click=dialog.close).props("flat no-caps")
    dialog.open()


# --------------------------------------------------------------------------- #
# the editor (Settings → Profile, the wizard's "You" step)
# --------------------------------------------------------------------------- #
def avatar_editor(settings: Settings) -> ui.column:
    """Picture + Upload / Choose from library / Remove. Returns an empty slot
    beside the picture, above the buttons, for the caller's fields (the name).
    The caller gates on the write lock."""
    engine = _ENGINE
    st = store(settings)

    @ui.refreshable
    def _face() -> None:
        profile = st.get()
        avatar(profile, st.avatar_url(profile), size="lg")
        remove.set_visibility(bool(profile.avatar))

    def _saved(_: Profile) -> None:
        _face.refresh()

    async def _remove() -> None:
        try:
            result = await nicegui_run.io_bound(_clear, settings)
        except ReadOnlyError:
            ui.notify("Not saved: another computer is editing", type="warning")
            return
        except OSError as exc:
            ui.notify(f"couldn't save: {exc}", type="negative")
            return
        if result is not None:
            _bump()
            _face.refresh()

    async def _on_upload(e: events.UploadEventArguments) -> None:
        uploader.reset()  # so picking the same file again uploads again
        await _crop_upload(settings, e, _saved)

    uploader = (
        ui.upload(
            auto_upload=True,
            max_file_size=MAX_UPLOAD_BYTES,
            on_upload=lambda e: _on_upload(e),
            on_rejected=lambda: ui.notify(
                f"Pick an image under {MAX_UPLOAD_BYTES // (1024 * 1024)} MB", type="warning"
            ),
        )
        .props(f'accept="{_ACCEPT}"')
        .classes("hidden")
    )
    with ui.row().classes("items-center gap-4 no-wrap"):
        face_box = ui.element("div")
        with ui.column().classes("gap-1"):
            slot = ui.column().classes("gap-2")
            with ui.row().classes("gap-1 items-center"):
                ui.button(
                    "Upload picture…",
                    icon="upload",
                    on_click=lambda: uploader.run_method("pickFiles"),
                ).props("flat dense no-caps")
                if engine is not None:
                    ui.button(
                        "Choose from library",
                        icon="photo_library",
                        on_click=lambda: _pick_from_library(settings, engine, _saved),
                    ).props("flat dense no-caps")
                remove = ui.button("Remove", icon="delete_outline", on_click=_remove).props(
                    "flat dense no-caps"
                )
    with face_box:
        _face()
    return slot


def use_as_profile_button(settings: Settings, engine: Engine, pixel_hash: str) -> None:
    """Photo page: "Use as profile picture" for the photo being shown."""
    ui.button(
        "Use as profile picture",
        icon="account_circle",
        on_click=lambda: crop_library_photo(settings, engine, pixel_hash),
    ).props("flat dense no-caps")
