"""The post composer UI: ``/posts``, ``/posts/{id}`` and the Settings →
Posting panel. Provider-agnostic: names, per-network settings, key sources and
base URLs all come from the post's (or the active) ``ProviderSpec``.

Kept out of ``app.py`` (already very long); it reuses that module's page
chrome (``_header``, ``_thumb_src``, ``State``…) through a lazy import, since
``app`` imports this module. Every API call and every export runs off the
event loop (``run.io_bound``; hard constraint #7). Nothing is ever sent without
an explicit click on a confirm dialog that lists exactly what goes where.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, tzinfo
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from nicegui import run as nicegui_run
from nicegui import ui
from sqlalchemy.engine import Engine

from sortandpost.collections import get_collection, load_collections
from sortandpost.config import Settings
from sortandpost.post_export import (
    IgSpec,
    resolve_sources,
    upright_sizes,
)
from sortandpost.post_send import is_in_flight, resolve_interrupted, send_post
from sortandpost.posting import (
    PROVIDERS,
    active_provider,
    base_url,
    limiter,
    open_client,
    provider_for,
)
from sortandpost.posting.client import check_base_url, normalize_base_url
from sortandpost.posting.model import Channel, PostingError
from sortandpost.posting.spec import FieldSpec, ProviderSpec
from sortandpost.posts import (
    IG_MAX_BORDER,
    MIN_LEAD,
    SLOT_HOURS,
    Post,
    SendGroup,
    SendMode,
    create_post,
    delete_post,
    duplicate_post,
    get_post,
    list_posts,
    load_channel_cache,
    plan_send,
    save_channel_cache,
    save_post,
    schedule_slots,
    selected_networks,
    validate,
)
from sortandpost.secrets_store import (
    KeySource,
    cached_key,
    clear_key,
    get_key,
    keychain_available,
    keychain_name,
    mask,
    set_key,
)
from sortandpost.sync import WriteMode
from sortandpost.sync.clock import utcnow

if TYPE_CHECKING:
    from collections.abc import Callable

log = logging.getLogger(__name__)

_HEX_RE = re.compile(r"^#?[0-9a-fA-F]{6}$")

_STATUS_LABEL = {
    "draft": ("Draft", "sp-muted"),
    "sending": ("Sending…", "text-sky-600 dark:text-sky-400"),
    "sent_draft": ("Draft in {provider}", "text-emerald-600 dark:text-emerald-400"),
    "scheduled": ("Scheduled", "text-emerald-600 dark:text-emerald-400"),
    "failed": ("Failed", "text-red-600 dark:text-red-400"),
}
_PARTLY_SENT = ("Partly sent", "text-amber-600 dark:text-amber-400")


def _status_label(p: Post, settings: Settings) -> tuple[str, str]:
    if p.status == "failed" and p.partly_sent:
        return _PARTLY_SENT
    label, color = _STATUS_LABEL.get(p.status, (p.status, ""))
    return label.format(provider=provider_for(p, settings).name), color


def _group_title(g: SendGroup) -> str:
    if g.is_instagram:
        return "Instagram"
    return "Other channels" if g.name == "other" else "All channels"


def _ig_size_text(spec: IgSpec | None) -> str:
    if spec is None:
        return "already in an Instagram ratio, used as they are"
    text = f"fitted onto {spec.width}x{spec.height}"
    if spec.border:
        r, g, b = spec.rgb
        text += f" with a {spec.border} px #{r:02x}{g:02x}{b:02x} border"
    return text


_EDGE_CHOICES = {1080: "1080 px", 2048: "2048 px", 3072: "3072 px", 4096: "4096 px", 0: "Full size"}
_TZ_JS = "Intl.DateTimeFormat().resolvedOptions().timeZone"


# --------------------------------------------------------------------------- #
# API calls (blocking; always via run.io_bound)
# --------------------------------------------------------------------------- #
def _refresh_channels(settings: Settings, spec: ProviderSpec) -> tuple[str, list[Channel]]:
    """Validate the key and reload the channel list; returns (account label, channels)."""
    key, _ = get_key(spec)
    if not key:
        raise PostingError(401, "no API key configured", provider=spec.name)
    try:
        api = open_client(settings, spec, key)
    except ValueError as exc:  # a bad base URL
        raise PostingError(0, str(exc), provider=spec.name) from None
    with api:
        label, chans = api.check_auth(), api.list_channels()
    save_channel_cache(settings.paths, spec.key, chans)
    return label, chans


def _source_names(engine: Engine, hashes: list[str]) -> dict[str, str | None]:
    return {
        h: (os.path.basename(p) if p else None) for h, p in resolve_sources(engine, hashes).items()
    }


@dataclass(frozen=True)
class _KeyStatus:
    """What the settings page shows about the API key -- never the key itself."""

    masked: str
    source: KeySource | None
    keychain: str | None  # the backend's name; ``None`` = no OS keychain here


def _key_status(spec: ProviderSpec) -> _KeyStatus:
    """Blocking (keychain; may wait on an OS prompt): always via run.io_bound."""
    key, source = get_key(spec)
    return _KeyStatus(
        masked=mask(key),
        source=source,
        keychain=keychain_name() if keychain_available() else None,
    )


def compose_from(
    settings: Settings,
    hashes: list[str],
    *,
    title: str,
    caption: str = "",
    collection_id: str | None = None,
) -> None:
    """Create a draft post from *hashes* (in order) and open the composer."""
    if not hashes:
        ui.notify("Nothing to post — no photos selected.", type="warning")
        return
    post = create_post(
        settings.paths,
        title=title,
        photos=hashes,
        caption=caption,
        source_collection_id=collection_id,
        device_id=settings.device_id,
    )
    ui.navigate.to(f"/posts/{post.id}")


def _fmt_local(iso: str | None, tz: tzinfo) -> str:
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    return dt.astimezone(tz).strftime("%a %d %b %Y, %H:%M")


async def _browser_tz() -> tzinfo:
    """The viewer's time zone (Docker's server clock is UTC, the user's isn't)."""
    try:
        await ui.context.client.connected(timeout=5)
        name = await ui.run_javascript(_TZ_JS, timeout=3.0)
        if isinstance(name, str) and name:
            return ZoneInfo(name)
    except (TimeoutError, ZoneInfoNotFoundError, ValueError):
        pass
    local = datetime.now().astimezone().tzinfo
    return local if local is not None else ZoneInfo("UTC")


# --------------------------------------------------------------------------- #
# pages
# --------------------------------------------------------------------------- #
def register_post_pages(settings: Settings, engine: Engine, thumb_small: int) -> None:
    from sortandpost import app as A  # lazy: app imports this module

    State = A.State

    @ui.page("/posts")
    async def posts_index() -> None:
        A._header("/posts")
        tz = await _browser_tz()
        with ui.row().classes("items-center justify-between px-4 pt-4 w-full max-w-4xl"):
            ui.label("Posts").classes("sp-display text-2xl font-semibold")

            def _new() -> None:
                post = create_post(
                    settings.paths, title="New post", photos=[], device_id=settings.device_id
                )
                ui.navigate.to(f"/posts/{post.id}")

            ui.button("New post", icon="add", on_click=_new).props("color=primary flat")
        ui.label(
            "Compose a post from a collection (“Compose post” on a collection) or from photos "
            f"selected in the library, then send it to {active_provider(settings).name} as a "
            "draft or schedule it."
        ).classes("px-4 text-xs sp-muted max-w-2xl")
        posts = list_posts(settings.paths)
        if not posts:
            A._empty_state("send", "No posts yet", "Start from a collection or the library.")
            return
        colls = {c.id: c.name for c in load_collections(settings.paths)}
        with ui.column().classes("w-full max-w-4xl gap-2 p-4"):
            for p in posts:
                label, color = _status_label(p, settings)
                with (
                    ui.link(target=f"/posts/{p.id}").classes("w-full no-underline"),
                    ui.card().classes("w-full flex-row items-center gap-4 p-2"),
                ):
                    cover = p.cover()
                    if cover:
                        ui.image(A._thumb_src(cover, thumb_small)).classes(
                            "w-16 h-16 rounded object-cover sp-placeholder"
                        )
                    else:
                        ui.icon("image_not_supported", size="2rem").classes("w-16 sp-faint")
                    with ui.column().classes("gap-0 grow"):
                        ui.label(p.title).classes("font-medium")
                        bits = [f"{len(p.photos)} photo(s)"]
                        if p.source_collection_id in colls:
                            bits.append(f"from “{colls[p.source_collection_id]}”")
                        if p.schedule_at:
                            bits.append(_fmt_local(p.schedule_at, tz))
                        ui.label(" · ".join(bits)).classes("text-xs sp-muted")
                    ui.label(label).classes(f"sp-chip {color}")

    @ui.page("/posts/{post_id}")
    async def post_detail(post_id: str) -> None:
        A._header("/posts")
        client = ui.context.client
        tz = await _browser_tz()
        post = get_post(settings.paths, post_id)
        if post is None:
            A._empty_state("search_off", "Post not found")
            ui.link("← All posts", "/posts").classes("mx-auto")
            return
        state: dict[str, Any] = {"post": post, "names": {}}

        def cur() -> Post:
            p: Post = state["post"]
            return p

        def prov() -> ProviderSpec:
            """The provider this post sends through (its own once it went out)."""
            return provider_for(cur(), settings)

        def channels() -> tuple[list[Channel], str | None]:
            return load_channel_cache(settings.paths, prov().key)

        def _save() -> None:
            save_post(settings.paths, cur(), device_id=settings.device_id)
            _checks.refresh()

        async def _load_names() -> None:
            names = await nicegui_run.io_bound(_source_names, engine, list(cur().photos))
            if names is None:
                return
            state["names"] = names
            state["sizes"] = await nicegui_run.io_bound(upright_sizes, engine, list(cur().photos))
            with client:
                _photos.refresh()
                _ig_preview.refresh()
                _checks.refresh()

        ui.button("← All posts", on_click=lambda: ui.navigate.to("/posts")).props(
            "flat dense"
        ).classes("mx-4 mt-2")

        # -- title / status / actions ---------------------------------------- #
        @ui.refreshable
        def _top() -> None:
            p = cur()
            name = prov().name
            label, color = _status_label(p, settings)
            with ui.row().classes("items-center gap-3 px-4 w-full"):
                title = (
                    ui.input(value=p.title)
                    .props("dense borderless")
                    .classes("sp-display text-2xl font-bold min-w-[16rem]")
                )
                title.set_enabled(p.editable)

                def _rename() -> None:
                    new = (title.value or "").strip()
                    if new and new != cur().title:
                        cur().title = new
                        _save()

                title.on("blur", _rename)
                title.on("keydown.enter", _rename)
                ui.label(label).classes(f"sp-chip {color}")
                ui.button("Duplicate", icon="content_copy", on_click=_duplicate).props(
                    "flat dense no-caps"
                ).tooltip("A new draft with the same photos and text")
                ui.button("Delete", icon="delete", on_click=_confirm_delete).props(
                    "flat dense no-caps color=negative"
                )
            chans, _ = channels()
            names = {c.id: c.name for c in chans}
            if p.status == "sending" and not is_in_flight(p.id):
                part = p.sending_part()
                doubt = ", ".join(names.get(c, c) for c in part["channel_ids"]) if part else ""
                with ui.card().classes(
                    "mx-4 bg-amber-50 dark:bg-amber-950 border border-amber-400 gap-1"
                ):
                    ui.label(f"This send was interrupted before {name} confirmed it.").classes(
                        "font-medium"
                    )
                    if p.sent_channel_ids():
                        ui.label(
                            "Already sent (not in question): "
                            + ", ".join(names.get(c, c) for c in sorted(p.sent_channel_ids()))
                        ).classes("text-sm")
                    ui.label(
                        ("The post to " + doubt if doubt else "The post")
                        + f" may or may not exist in {name}. Check {name}, then tell "
                        "sortandpost what happened — it never re-sends on its own."
                    ).classes("text-sm")
                    with ui.row().classes("gap-2"):
                        ui.button(
                            "It was sent", on_click=lambda: _resolve(True), icon="check"
                        ).props("color=positive flat")
                        ui.button(
                            "It wasn't sent", on_click=lambda: _resolve(False), icon="undo"
                        ).props("flat")
            elif p.partly_sent:
                left = [c for c in p.channel_ids if c not in p.sent_channel_ids()]
                ui.label(
                    f"Partly sent — already in {name} for "
                    + ", ".join(names.get(c, c) for c in sorted(p.sent_channel_ids()))
                    + ". Sending again only goes to "
                    + (", ".join(names.get(c, c) for c in left) or "no one (it's then marked sent)")
                    + "."
                ).classes("px-4 text-sm text-amber-700 dark:text-amber-400")
            elif p.sent:
                ui.label(
                    f"Sent to {name} — edit or cancel it there (sortandpost can't change a "
                    "sent post). Use Duplicate to send it again."
                ).classes("px-4 text-sm sp-muted")
            if State.write_mode != WriteMode.writable:
                ui.label(
                    f"Read-only — {State.lock_holder} holds the write lock, so posts can't be "
                    "sent from this machine right now."
                ).classes("px-4 text-sm text-amber-600")

        def _resolve(was_sent: bool) -> None:
            fresh = resolve_interrupted(settings, cur().id, was_sent=was_sent)
            if fresh is not None:
                state["post"] = fresh
            _refresh_all()

        def _duplicate() -> None:
            copy = duplicate_post(settings.paths, cur().id, device_id=settings.device_id)
            if copy is not None:
                ui.navigate.to(f"/posts/{copy.id}")

        def _confirm_delete() -> None:
            with ui.dialog() as dialog, ui.card().classes("gap-2 max-w-md"):
                ui.label(f"Delete “{cur().title}”?").classes("font-medium")
                ui.label(
                    f"Only this record in sortandpost is removed. Nothing in {prov().name} "
                    "changes (delete it there if needed), and no photo files are touched."
                ).classes("text-xs sp-muted")
                with ui.row().classes("w-full justify-end gap-2"):
                    ui.button("Cancel", on_click=dialog.close).props("flat")

                    def _go() -> None:
                        delete_post(settings.paths, cur().id)
                        dialog.close()
                        ui.navigate.to("/posts")

                    ui.button("Delete", on_click=_go).props("color=negative")
            dialog.open()

        # -- photos ---------------------------------------------------------- #
        def _move(h: str, step: int) -> None:
            order = cur().photos
            i = order.index(h)
            j = i + step
            if 0 <= j < len(order):
                order[i], order[j] = order[j], order[i]
                _save()
                _photos.refresh()

        def _remove(h: str) -> None:
            cur().photos.remove(h)
            _save()
            _photos.refresh()

        def _add_from_collection(coll_id: str) -> None:
            c = get_collection(settings.paths, coll_id)
            if c is None:
                return
            have = set(cur().photos)
            new = [h for h in c.photos if h not in have]
            cur().photos.extend(new)
            if not cur().caption.strip() and c.notes.strip():
                cur().caption = c.notes.strip()
                _text.refresh()
            _save()
            ui.notify(f"added {len(new)} photo(s) from “{c.name}”")
            ui.timer(0.01, _load_names, once=True)

        @ui.refreshable
        def _photos() -> None:
            p = cur()
            names: dict[str, str | None] = state["names"]
            with ui.row().classes("items-center gap-2 px-4 pt-4"):
                ui.label(f"Photos · {len(p.photos)}").classes("font-bold")
                if p.editable:
                    with (
                        ui.button("Add from collection", icon="library_add").props(
                            "flat dense no-caps"
                        ),
                        ui.menu(),
                    ):
                        colls = load_collections(settings.paths)
                        if not colls:
                            ui.menu_item("No collections yet").props("disable")
                        for c in colls:
                            ui.menu_item(
                                f"{c.name}  ·  {c.count}",
                                on_click=lambda cid=c.id: _add_from_collection(cid),
                            )
            if not p.photos:
                ui.label(
                    "No photos yet. Add some from a collection, or select photos in the library "
                    "and use “Compose post” in the toolbar."
                ).classes("px-4 text-sm sp-muted")
                return
            with ui.row().classes("gap-2 px-4 flex-wrap"):
                for i, h in enumerate(p.photos):
                    missing = h in names and names[h] is None
                    with ui.element("div").classes("group relative w-32"):
                        ui.image(A._thumb_src(h, thumb_small)).classes(
                            "w-32 h-32 rounded object-cover sp-placeholder"
                            + (" opacity-40" if missing else "")
                        )
                        ui.label(str(i + 1)).classes(
                            "absolute left-1 top-1 rounded bg-black/70 px-1.5 py-0.5 text-xs "
                            "font-medium text-white"
                        )
                        if missing:
                            ui.label("no file here").classes(
                                "absolute left-1 bottom-6 rounded bg-red-700/80 px-1 text-[10px] "
                                "text-white"
                            )
                        ui.label(names.get(h) or "").classes("truncate text-[11px] sp-faint")
                        if p.editable:
                            with ui.row().classes(
                                "absolute right-1 top-1 gap-0.5 opacity-0 transition "
                                "group-hover:opacity-100"
                            ):
                                if i > 0:
                                    ui.button(
                                        icon="arrow_back", on_click=lambda h=h: _move(h, -1)
                                    ).props("flat dense round size=xs color=white").classes(
                                        "bg-black/55"
                                    ).tooltip("move earlier")
                                if i < len(p.photos) - 1:
                                    ui.button(
                                        icon="arrow_forward", on_click=lambda h=h: _move(h, 1)
                                    ).props("flat dense round size=xs color=white").classes(
                                        "bg-black/55"
                                    ).tooltip("move later")
                                ui.button(icon="close", on_click=lambda h=h: _remove(h)).props(
                                    "flat dense round size=xs color=white"
                                ).classes("bg-black/55").tooltip("remove from post")

        # -- caption / first comment ---------------------------------------- #
        @ui.refreshable
        def _text() -> None:
            p = cur()
            with ui.column().classes("w-full max-w-2xl gap-1 px-4 pt-4"):
                ui.label("Caption").classes("font-bold")
                cap = (
                    ui.textarea(value=p.caption, placeholder="What's this post about?")
                    .props("outlined autogrow")
                    .classes("w-full")
                )
                cap_n = ui.label(f"{len(p.caption)} characters").classes("text-xs sp-faint")
                ui.label("First comment (optional)").classes("font-bold pt-2")
                ui.label("Posted as the first comment — a common place for hashtags.").classes(
                    "text-xs sp-muted"
                )
                fc = (
                    ui.textarea(value=p.first_comment, placeholder="#hashtags …")
                    .props("outlined autogrow")
                    .classes("w-full")
                )
                cap.set_enabled(p.editable)
                fc.set_enabled(p.editable)
                cap.on_value_change(lambda e: cap_n.set_text(f"{len(e.value or '')} characters"))

                def _save_text() -> None:
                    changed = False
                    if (cap.value or "") != cur().caption:
                        cur().caption = cap.value or ""
                        changed = True
                    if (fc.value or "") != cur().first_comment:
                        cur().first_comment = fc.value or ""
                        changed = True
                    if changed:
                        _save()

                cap.on("blur", _save_text)
                fc.on("blur", _save_text)

        # -- destinations ---------------------------------------------------- #
        async def _refresh_channel_list() -> None:
            try:
                got = await nicegui_run.io_bound(_refresh_channels, settings, prov())
            except PostingError as exc:
                with client:
                    ui.notify(exc.friendly, type="negative", multi_line=True)
                return
            if got is None:  # app shutting down
                return
            with client:
                ui.notify(f"{len(got[1])} channel(s) loaded from {prov().name}")
                _dest.refresh()
                _checks.refresh()

        @ui.refreshable
        def _dest() -> None:
            p = cur()
            sp = prov()
            chans, fetched = channels()
            with ui.column().classes("w-full max-w-2xl gap-1 px-4 pt-4"):
                with ui.row().classes("items-center gap-2"):
                    ui.label(f"Channels · {sp.name}").classes("font-bold")
                    ui.button(icon="refresh", on_click=_refresh_channel_list).props(
                        "flat dense round size=sm"
                    ).tooltip(f"Reload the channel list from {sp.name}")
                    if fetched:
                        ui.label(f"updated {_fmt_local(fetched, tz)}").classes("text-xs sp-faint")
                if not chans:
                    ui.label(
                        "No channels loaded. Add your API key in Settings → Posting, then press "
                        "refresh."
                        + (
                            f" (Channels are optional for a {sp.name} draft.)"
                            if sp.channelless_drafts
                            else ""
                        )
                    ).classes("text-sm sp-muted")
                known = {c.id for c in chans}
                stale = [
                    c for c in p.channel_ids if c not in known and c not in p.sent_channel_ids()
                ]
                if stale and p.editable:
                    with ui.row().classes("items-center gap-2"):
                        ui.label(
                            f"{len(stale)} chosen channel(s) aren't in {sp.name} — re-pick below."
                        ).classes("text-sm text-amber-600")
                        ui.button("Clear them", on_click=lambda s=stale: _drop_channels(s)).props(
                            "flat dense no-caps"
                        )
                by_net: dict[str, list[Channel]] = {}
                for c in chans:
                    by_net.setdefault(c.network, []).append(c)
                done = p.sent_channel_ids()
                for net, group in sorted(by_net.items()):
                    ui.label(net.title()).classes("text-xs sp-faint pt-1")
                    for c in group:
                        note = f"  (reconnect in {sp.name})" if c.must_reconnect else ""
                        if c.id in done:
                            note = "  (sent)"
                        cb = ui.checkbox(c.name + note, value=c.id in p.channel_ids or c.id in done)
                        cb.set_enabled(p.editable and c.id not in done)
                        cb.on_value_change(lambda e, cid=c.id: _toggle_channel(cid, bool(e.value)))
                nets = selected_networks(p, chans)
                for net in sorted(nets):
                    _network_fields(net, sp.fields_for(net))
                    if net == "instagram":
                        _ig_sizing()

        def _drop_channels(ids: list[str]) -> None:
            cur().channel_ids = [c for c in cur().channel_ids if c not in ids]
            _save()
            _dest.refresh()

        def _toggle_channel(cid: str, on: bool) -> None:
            ids = cur().channel_ids
            if on and cid not in ids:
                ids.append(cid)
            elif not on and cid in ids:
                ids.remove(cid)
            _save()
            _dest.refresh()

        def _set_setting(net: str, key: str, value: str) -> None:
            cur().network_settings.setdefault(net, {})[key] = value
            _save()

        def _sizes() -> list[tuple[int, int]]:
            sizes: dict[str, tuple[int, int]] = state.get("sizes", {})
            return [sizes[h] for h in cur().photos if h in sizes]

        def _ig_spec() -> IgSpec | None:
            """The canvas for this post's Instagram photos (None = used as they are)."""
            return cur().ig_spec(_sizes())

        def _ig_sizing() -> None:
            p = cur()
            ui.label(
                "Instagram needs every photo in a post to be the same size. Photos are fitted "
                "onto one canvas (never cropped)."
            ).classes("text-xs sp-faint pt-1")
            if not p.ig_story():
                with ui.row().classes("items-center gap-2 pt-1"):
                    ui.label("Instagram format").classes("text-sm")
                    labels = {
                        "portrait": "Portrait 1080x1350",
                        "square": "Square 1080x1080",
                        "landscape": "Landscape 1080x566",
                    }
                    t = ui.toggle(labels, value=p.ig_format()).props("dense no-caps")
                    t.set_enabled(p.editable)

                    def _fmt(e: Any) -> None:
                        _set_setting("instagram", "format", str(e.value))
                        _ig_preview.refresh()

                    t.on_value_change(_fmt)
            else:
                ui.label("Story format 1080x1920").classes("text-sm pt-1")
            with ui.row().classes("items-center gap-3 pt-1"):
                ui.label("Border").classes("text-sm")
                ui.label("None").classes("text-xs sp-faint")
                bd = ui.slider(min=0, max=IG_MAX_BORDER, step=1, value=p.ig_border()).classes(
                    "w-48"
                )
                ui.label("Large").classes("text-xs sp-faint")
                ui.label("Border color").classes("text-sm pl-4")
                # Native browser colour chooser + a hex field, kept in step.
                picker = (
                    ui.element("input")
                    .props(f'type=color value="{p.ig_color()}"')
                    .style("width:2.5rem;height:2rem;padding:0;border:0;background:none")
                )
                hexbox = (
                    ui.input(value=p.ig_color(), placeholder="#rrggbb")
                    .props("dense outlined maxlength=7 style=width:7rem")
                    .classes("font-mono")
                )
                for w in (bd, hexbox):
                    w.set_enabled(p.editable)
                if not p.editable:
                    picker.props("disabled")

                def _border(_e: Any = None) -> None:
                    _set_setting("instagram", "border", str(int(bd.value or 0)))
                    _ig_preview.refresh()

                bd.on("change", _border)  # on release: one save, not one per drag step

                def _apply_color(value: str) -> None:
                    v = value.strip()
                    if not _HEX_RE.match(v):
                        return
                    v = v.lower() if v.startswith("#") else "#" + v.lower()
                    _set_setting("instagram", "padColor", v)
                    if hexbox.value != v:
                        hexbox.set_value(v)
                    picker.props(f'value="{v}"')
                    picker.update()
                    _ig_preview.refresh()

                hexbox.on_value_change(lambda e: _apply_color(str(e.value or "")))
                picker.on(
                    "change",
                    lambda e: _apply_color(str(e.args)),
                    js_handler="(e) => emit(e.target.value)",
                )
            _ig_preview()

        @ui.refreshable
        def _ig_preview() -> None:
            p = cur()
            if "instagram" not in selected_networks(p, channels()[0]):
                return
            sizes: dict[str, tuple[int, int]] = state.get("sizes", {})
            spec = _ig_spec()
            if not p.photos or not sizes:
                return
            if spec is None:
                ui.label(
                    "Every photo already has an Instagram ratio, so they are used as they are."
                ).classes("text-sm pt-1")
            tile_w = 112
            with ui.row().classes("gap-2 flex-wrap pt-1"):
                for h in p.photos:
                    if spec is None:
                        w0, h0 = sizes.get(h, (1, 1))
                        box = f"width:{tile_w}px;aspect-ratio:{w0}/{h0};background:transparent;"
                        pad = 0.0
                    else:
                        box = (
                            f"width:{tile_w}px;aspect-ratio:{spec.width}/{spec.height};"
                            f"background:{p.ig_color()};"
                        )
                        pad = spec.border / 2 * tile_w / spec.width
                    with (
                        ui.element("div")
                        .classes("rounded overflow-hidden")
                        .style(
                            box + f"padding:{pad:.2f}px;box-sizing:border-box;"
                            "outline:1px solid var(--sp-border);"
                        )
                    ):
                        ui.image(A._thumb_src(h, thumb_small)).props("fit=contain").classes(
                            "w-full h-full"
                        )
            if spec is not None:
                ui.label(
                    f"Preview: {spec.width}x{spec.height}"
                    + (f", {spec.border}px border" if spec.border else "")
                ).classes("text-xs sp-faint")
            groups = plan_send(p, channels()[0], _sizes())
            if len(groups) > 1:
                ui.label(
                    f"Instagram goes out as its own {prov().name} post with these sized copies; "
                    "your other channels get the normal export first."
                ).classes("text-xs sp-faint")

        def _network_fields(net: str, fields: tuple[FieldSpec, ...]) -> None:
            """The provider's settings for one network, from its spec."""
            if not fields:
                return
            p = cur()
            with ui.column().classes("gap-1 pt-2 w-full"):
                for f in fields:
                    value = p.setting(net, f.key, f.default)
                    if f.kind == "select":
                        with ui.row().classes("items-center gap-2"):
                            ui.label(f.label).classes("text-sm")
                            opt = f.option(value)
                            t = ui.toggle(
                                {o.value: o.label for o in f.options},
                                value=opt.value if opt else None,
                            ).props("dense no-caps")
                            t.set_enabled(p.editable)

                            def _sel(e: Any, f: FieldSpec = f) -> None:
                                _set_setting(net, f.key, str(e.value or f.default))
                                _dest.refresh()  # e.g. an Instagram story changes the sizing

                            t.on_value_change(_sel)
                        continue
                    inp = (
                        ui.input(f.label, value=value, placeholder=f.placeholder)
                        .props("dense outlined")
                        .classes("w-full")
                    )
                    inp.set_enabled(p.editable)
                    inp.on(
                        "blur",
                        lambda _e, i=inp, k=f.key: _set_setting(net, k, str(i.value or "")),
                    )

        # -- when ------------------------------------------------------------ #
        @ui.refreshable
        def _when() -> None:
            p = cur()
            when = p.schedule_dt()
            local = when.astimezone(tz) if when else None
            slots = schedule_slots(p.id, tz)
            picked = when is not None and any(when == t for _d, ts in slots for t in ts)
            custom = state.get("custom_time", False) or (when is not None and not picked)
            earliest = utcnow() + MIN_LEAD
            with ui.column().classes("w-full max-w-2xl gap-1 px-4 pt-4"):
                ui.label("When").classes("font-bold")
                with ui.element("div").classes(
                    "grid grid-cols-[auto_repeat(4,minmax(0,1fr))] gap-1 items-center w-full"
                ):
                    ui.element("div")
                    for label, _hour in SLOT_HOURS:
                        ui.label(label).classes("text-xs sp-faint text-center")
                    for day, times in slots:
                        ui.label(f"{day:%a} {day.day} {day:%b}").classes(
                            "text-sm pr-2 whitespace-nowrap"
                        )
                        for t in times:
                            on = when is not None and when == t
                            b = ui.button(f"{t:%H:%M}", on_click=lambda t=t: _pick(t)).props(
                                "dense no-caps unelevated" if on else "dense no-caps outline"
                            )
                            if on:
                                b.props("color=primary")
                            b.set_enabled(p.editable and t > earliest)
                with ui.row().classes("items-center gap-2 pt-1"):
                    ui.button(
                        "Pick a date & time",
                        icon="edit_calendar",
                        on_click=lambda: _show_custom(not custom),
                    ).props("flat dense no-caps").set_enabled(p.editable)
                    clear = (
                        ui.button(icon="event_busy")
                        .props("flat dense round")
                        .tooltip("Clear the time")
                    )
                    clear.set_enabled(p.editable and when is not None)
                    clear.on_click(_clear)
                if custom:
                    with ui.row().classes("items-center gap-2"):
                        d = ui.input(
                            "Date", value=local.strftime("%Y-%m-%d") if local else ""
                        ).props("type=date dense outlined")
                        t_in = ui.input(
                            "Time", value=local.strftime("%H:%M") if local else ""
                        ).props("type=time dense outlined")
                        for w in (d, t_in):
                            w.set_enabled(p.editable)

                    def _apply() -> None:
                        if not d.value:
                            return
                        try:
                            naive = datetime.fromisoformat(f"{d.value}T{t_in.value or '09:00'}")
                        except ValueError:
                            ui.notify("Can't read that date/time.", type="warning")
                            return
                        _set_when(naive.replace(tzinfo=tz))

                    d.on("change", _apply)
                    t_in.on("change", _apply)
                ui.label(
                    f"{_fmt_local(p.schedule_at, tz)} ({getattr(tz, 'key', tz)})"
                    + (f"  ·  {when:%Y-%m-%d %H:%M} UTC" if when else "")
                    if when
                    else "No time set — required to schedule; optional for a draft."
                ).classes("text-xs sp-muted")

        def _set_when(local_dt: datetime) -> None:
            cur().schedule_at = local_dt.astimezone(ZoneInfo("UTC")).isoformat()
            _save()
            _when.refresh()

        def _pick(t: datetime) -> None:
            state["custom_time"] = False
            _set_when(t)

        def _show_custom(on: bool) -> None:
            state["custom_time"] = on
            _when.refresh()

        def _clear() -> None:
            cur().schedule_at = None
            state["custom_time"] = False
            _save()
            _when.refresh()

        # -- checks + send --------------------------------------------------- #
        @ui.refreshable
        def _checks() -> None:
            p = cur()
            sp = prov()
            chans, _ = channels()
            names: dict[str, str | None] = state["names"]
            unavailable = {h for h, n in names.items() if n is None}
            active = active_provider(settings).key
            d_err, d_warn = validate(p, chans, "draft", sp, unavailable=unavailable, active=active)
            s_err, s_warn = validate(
                p, chans, "schedule", sp, unavailable=unavailable, active=active
            )
            if not sp.drafts:  # only a schedule exists: its checks are the checks
                d_err, d_warn = s_err, s_warn
            budget = _budget_note(settings, sp, len(plan_send(p, chans, _sizes())) or 1)
            if budget:
                d_err, s_err = [*d_err, budget], [*s_err, budget]
            loaded = cached_key(sp)  # read off-loop by _load_key below
            key = loaded[0] if loaded else None
            writable = State.write_mode == WriteMode.writable
            with ui.column().classes("w-full max-w-2xl gap-1 px-4 pt-4 pb-2"):
                if p.editable:
                    for msg in dict.fromkeys(d_err + [e for e in s_err if e not in d_err]):
                        ui.label(("✕ " if msg in d_err else "• schedule: ") + msg).classes(
                            "text-sm text-red-600 dark:text-red-400"
                        )
                    for msg in dict.fromkeys(d_warn + s_warn):
                        ui.label("⚠ " + msg).classes("text-sm text-amber-600")
                    if loaded is None:
                        ui.label(f"Checking the keychain for the {sp.name} API key…").classes(
                            "text-sm sp-muted"
                        )
                    elif not key:
                        ui.label(f"✕ No {sp.name} API key — add it in Settings → Posting.").classes(
                            "text-sm text-red-600"
                        )
                with ui.row().classes("gap-2 pt-2"):
                    if sp.drafts:
                        b1 = ui.button(
                            "Send as draft",
                            icon="drafts",
                            on_click=lambda: _confirm("draft"),
                        ).props("no-caps outline")
                        b1.set_enabled(p.editable and writable and bool(key) and not d_err)
                    b2 = ui.button(
                        "Schedule",
                        icon="schedule_send",
                        on_click=lambda: _confirm("schedule"),
                    ).props("no-caps color=primary")
                    b2.set_enabled(p.editable and writable and bool(key) and not s_err)

        async def _confirm(mode: SendMode) -> None:
            p = cur()
            chans, _ = channels()
            names = await nicegui_run.io_bound(_source_names, engine, list(p.photos))
            sizes = await nicegui_run.io_bound(upright_sizes, engine, list(p.photos))
            if names is None or sizes is None:
                return
            state["names"] = names
            state["sizes"] = sizes
            with client:
                unavailable = {h for h, n in names.items() if n is None}
                errors, warnings = validate(
                    p,
                    chans,
                    mode,
                    prov(),
                    unavailable=unavailable,
                    active=active_provider(settings).key,
                )
                if errors:
                    ui.notify(errors[0], type="warning")
                    _checks.refresh()
                    return
                _confirm_dialog(p, mode, chans, names, warnings)

        def _confirm_dialog(
            p: Post,
            mode: SendMode,
            chans: list[Channel],
            names: dict[str, str | None],
            warnings: list[str],
        ) -> None:
            pc = settings.config.posting
            name = prov().name
            by_id = {c.id: c for c in chans}
            groups = plan_send(p, chans, _sizes())
            edge = "full size" if pc.export_long_edge <= 0 else f"{pc.export_long_edge} px"
            with ui.dialog() as dialog, ui.card().classes("gap-2 max-w-xl"):
                ui.label(
                    f"Send to {name} as a draft?" if mode == "draft" else "Schedule this post?"
                ).classes("text-lg font-medium")
                ui.label(
                    f"{len(p.photos)} photo(s) will be uploaded as new JPEGs "
                    f"(quality {pc.export_quality}). GPS and all other metadata are stripped; "
                    "your original files are not changed."
                ).classes("text-sm")
                with ui.row().classes("gap-1 flex-wrap"):
                    for i, h in enumerate(p.photos):
                        with ui.column().classes("gap-0 w-20"):
                            ui.image(A._thumb_src(h, thumb_small)).classes(
                                "w-20 h-20 rounded object-cover"
                            )
                            ui.label(f"{i + 1}. {names.get(h) or '?'}").classes(
                                "truncate text-[10px] sp-faint"
                            )
                ui.separator()
                done = sorted(p.sent_channel_ids())
                if done:
                    ui.label(
                        "Already sent, skipped: "
                        + ", ".join(by_id[c].name if c in by_id else c for c in done)
                    ).classes("text-sm sp-muted")
                if len(groups) > 1:
                    ui.label(
                        f"This goes to {name} as {len(groups)} separate posts, in this order. "
                        "If one fails, a retry only sends what's left."
                    ).classes("text-sm font-medium")
                if not groups:
                    ui.label("Nothing left to send — the post will be marked as sent.").classes(
                        "text-sm"
                    )
                for i, g in enumerate(groups):
                    size = (
                        "photos " + _ig_size_text(g.ig) if g.is_instagram else f"{edge} long edge"
                    )
                    head = f"{i + 1}. {_group_title(g)}" if len(groups) > 1 else "Destinations"
                    ui.label(f"{head} — {size}").classes("text-sm font-medium")
                    for c in g.channels:
                        ui.label(f"• {c.name} — {c.social_network}").classes("text-sm")
                    if not g.channels:
                        ui.label(f"No channels chosen — you'll pick them in {name}.").classes(
                            "text-sm"
                        )
                when = p.schedule_dt()
                if mode == "schedule" and when is not None:
                    ui.label(
                        f"Goes live {_fmt_local(p.schedule_at, tz)} ({getattr(tz, 'key', tz)}) · "
                        f"{when:%Y-%m-%d %H:%M} UTC"
                    ).classes("text-sm font-medium")
                elif when is not None:
                    ui.label(f"Draft time: {_fmt_local(p.schedule_at, tz)}").classes("text-sm")
                ui.label(
                    f"Caption: {len(p.caption)} characters"
                    + (" · with a first comment" if p.first_comment.strip() else "")
                ).classes("text-sm")
                for w in warnings:
                    ui.label("⚠ " + w).classes("text-sm text-amber-600")
                ui.label(
                    f"After sending, this post can only be edited or cancelled inside {name}."
                ).classes("text-xs sp-muted")
                with ui.row().classes("w-full justify-end gap-2"):
                    ui.button("Cancel", on_click=dialog.close).props("flat")
                    go: ui.button = ui.button("Send draft" if mode == "draft" else "Schedule")
                    go.props("color=primary")
                    go.on_click(lambda: _do_send(mode, dialog, go))
            dialog.open()

        async def _do_send(mode: SendMode, dialog: ui.dialog, go: ui.button) -> None:
            go.props("loading")
            go.disable()
            res = await nicegui_run.io_bound(
                send_post,
                settings,
                engine,
                cur().id,
                mode,
                writable=State.write_mode == WriteMode.writable,
            )
            if res is None:  # app shutting down
                return
            with client:
                dialog.close()
                if res.post is not None:
                    state["post"] = res.post
                ui.notify(
                    res.message,
                    type="positive" if res.ok else "negative",
                    multi_line=True,
                    timeout=8000,
                )
                _refresh_all()

        # -- history ----------------------------------------------------------- #
        @ui.refreshable
        def _history() -> None:
            p = cur()
            if not p.log:
                return
            with ui.expansion("History", icon="history").classes("px-4 w-full max-w-2xl"):
                for e in reversed(p.log):
                    line = f"{_fmt_local(str(e.get('ts', '')), tz)} · {e.get('action')}"
                    if e.get("mode"):
                        line += f" ({e['mode']})"
                    if e.get("part") in ("instagram", "other"):
                        line += " · Instagram" if e["part"] == "instagram" else " · other channels"
                    ids = e.get("remote_post_ids") or e.get("robopost_post_ids")
                    if ids:
                        who = PROVIDERS.get(str(e.get("provider") or "robopost"))
                        line += f" · {who.name if who else 'remote'} id {', '.join(ids)}"
                    if "was_sent" in e:
                        line += " · marked sent" if e["was_sent"] else " · marked not sent"
                    ui.label(line).classes("text-xs")
                    if e.get("error"):
                        ui.label(str(e["error"])).classes("text-xs text-red-600 pl-4")

        def _refresh_all() -> None:
            _top.refresh()
            _photos.refresh()
            _text.refresh()
            _dest.refresh()
            _when.refresh()
            _checks.refresh()
            _history.refresh()

        _top()
        _photos()
        _text()
        _dest()
        _when()
        _checks()
        _history()
        ui.element("div").classes("h-8")
        await _load_names()
        if cached_key(prov()) is None:
            # the first read can wait on a keychain prompt: never on the event loop
            await nicegui_run.io_bound(get_key, prov())
            with client:
                _checks.refresh()


# --------------------------------------------------------------------------- #
# Settings → Posting
# --------------------------------------------------------------------------- #
_OP_LABELS = {"create": "new posts", "upload": "uploads", "channels": "channel lists"}


def _budget_note(settings: Settings, spec: ProviderSpec, creates: int) -> str | None:
    """An error when *creates* more posts would break the provider's known
    rate limit right now (checked again by the send itself)."""
    lim = limiter(settings, spec)
    wait = lim.check("create", creates)
    if wait is None:
        return None
    rl = lim.limit("create")
    minutes = max(1, -(-wait // 60))
    return (
        f"{spec.name} allows {rl.describe('new posts') if rl else 'only so many new posts'}; "
        f"try again in {minutes} minute(s)."
    )


def posting_settings_panel(settings: Settings, *, writable: bool) -> None:
    """Which posting service, then -- for that service, all from its spec --
    the API key (write-only, OS keychain), its server address when it has one,
    its rate limits, a connection test with the channel list; and the upload
    export size/quality, shared by every service."""
    client = ui.context.client
    pc = settings.config.posting

    @ui.refreshable
    def _body() -> None:
        spec = active_provider(settings)
        with ui.row().classes("items-center gap-3"):
            ui.label("Posting service").classes("text-sm")
            t = ui.toggle({k: sp.name for k, sp in PROVIDERS.items()}, value=spec.key).props(
                "dense no-caps"
            )
            t.set_enabled(writable)

            def _switch(e: Any) -> None:
                new = str(e.value or "")
                if new not in PROVIDERS or new == pc.provider:
                    return
                pc.provider = new
                settings.config.dump(settings.paths.config_file)
                ui.notify(
                    f"Posting now uses {PROVIDERS[new].name}. Unsent drafts follow it; posts "
                    "that already went out stay with the service they went to.",
                    multi_line=True,
                )
                _body.refresh()

            t.on_value_change(_switch)
        ui.label(f"sortandpost {spec.about} {spec.key_help}").classes("text-xs sp-faint max-w-xl")
        _key_panel(spec, client)
        if spec.base_url_editable:
            _base_url_panel(settings, spec, writable=writable)
        _limits_panel(settings, spec)
        _connection_panel(settings, spec, client)

    _body()

    if not writable:
        return
    with ui.row().classes("items-center gap-4 pt-2"):
        ui.label("Upload size").classes("text-sm")
        edge = (
            ui.select(
                _EDGE_CHOICES
                if pc.export_long_edge in _EDGE_CHOICES
                else {**_EDGE_CHOICES, pc.export_long_edge: f"{pc.export_long_edge} px"},
                value=pc.export_long_edge,
            )
            .props("dense outlined")
            .classes("w-36")
        )
        ui.label("JPEG quality").classes("text-sm")
        q = (
            ui.slider(min=70, max=100, step=1, value=pc.export_quality)
            .props("label")
            .classes("w-40")
        )
        q_lbl = ui.label(str(pc.export_quality)).classes("text-sm w-8")

    def _save_export(_e: Any = None) -> None:
        pc.export_long_edge = int(edge.value or 0)
        pc.export_quality = int(q.value or 90)
        q_lbl.set_text(str(pc.export_quality))
        settings.config.dump(settings.paths.config_file)

    edge.on_value_change(_save_export)
    q.on("change", _save_export)
    q.on_value_change(lambda e: q_lbl.set_text(str(int(e.value or 0))))


def _key_panel(spec: ProviderSpec, client: Any) -> None:
    key_state: dict[str, _KeyStatus | None] = {"status": None}

    async def _load_key() -> None:
        # keychain calls can wait on an OS prompt: never on the event loop
        st = await nicegui_run.io_bound(_key_status, spec)
        if st is None:  # the pool was shut down under us (app closing)
            return
        key_state["status"] = st
        with client:
            _key_row.refresh()

    @ui.refreshable
    def _key_row() -> None:
        st = key_state["status"]
        with ui.column().classes("gap-1"):
            if st is None:
                ui.label("Checking the keychain…").classes("text-sm sp-muted")
                ui.label(
                    "If macOS asks whether sortandpost may use its keychain item, answer "
                    "that prompt (Always Allow avoids it next time)."
                ).classes("text-xs sp-faint")
                return
            if st.source == "env":
                ui.label(
                    f"{spec.name} API key {st.masked} — from the {spec.env_var} "
                    "environment variable."
                )
                ui.label(
                    "Change it where the app is launched (.env / docker-compose). The "
                    "environment variable always overrides a saved key."
                ).classes("text-xs sp-muted")
                return
            if st.keychain is None:
                ui.label("No OS keychain is available here (e.g. inside Docker).").classes(
                    "text-sm text-amber-600"
                )
                ui.label(
                    f"Set {spec.env_var} in .env and restart. The key is never stored in the "
                    "app-data folder."
                ).classes("text-xs sp-muted")
                return
            has_key = bool(st.masked)
            if has_key:
                ui.label(f"{spec.name} API key {st.masked} — stored in the {st.keychain}.")
            else:
                ui.label(f"No {spec.name} API key saved.").classes("text-sm sp-muted")
            with ui.row().classes("items-center gap-2"):
                inp = (
                    ui.input(
                        "Paste a new API key" if has_key else "API key",
                        password=True,
                        password_toggle_button=False,
                    )
                    .props("dense outlined autocomplete=off")
                    .classes("w-80")
                )

                async def _save_key() -> None:
                    value = (inp.value or "").strip()
                    inp.value = ""  # never keep it in the page
                    if not value:
                        return
                    try:
                        await nicegui_run.io_bound(set_key, spec, value)
                    except Exception as exc:  # keyring backends raise their own types
                        with client:
                            ui.notify(
                                f"Couldn't save to the keychain: {type(exc).__name__}",
                                type="negative",
                            )
                        return
                    with client:
                        ui.notify(f"{spec.name} API key saved to the {st.keychain}")
                    await _load_key()

                inp.on("keydown.enter", _save_key)
                ui.button("Save", on_click=_save_key).props("flat dense")
                if has_key:

                    async def _remove() -> None:
                        await nicegui_run.io_bound(clear_key, spec)
                        with client:
                            ui.notify(f"{spec.name} API key removed from the keychain")
                        await _load_key()

                    ui.button("Remove", on_click=_remove).props("flat dense color=negative")
            ui.label(
                "Only the last 4 characters are ever shown. The key never goes to the browser "
                "or into the app-data folder."
            ).classes("text-xs sp-faint")

    _key_row()
    ui.timer(0, _load_key, once=True)


def _base_url_panel(settings: Settings, spec: ProviderSpec, *, writable: bool) -> None:
    pc = settings.config.posting
    with ui.column().classes("gap-1 pt-2 w-full max-w-xl"):
        ui.label(f"{spec.name} server").classes("text-sm font-medium")
        with ui.row().classes("items-center gap-1"):
            for preset in spec.base_url_presets:
                b = ui.button(
                    preset.label, on_click=lambda pr=preset: _use_preset(pr.url, pr.hint)
                ).props("flat dense no-caps")
                b.set_enabled(writable)
        inp = (
            ui.input("API address", value=base_url(settings, spec))
            .props("dense outlined")
            .classes("w-full")
        )
        inp.set_enabled(writable)
        hint = ui.label().classes("text-xs sp-faint")
        note = ui.label().classes("text-xs")

        def _show(raw: str) -> tuple[str, str | None]:
            url = normalize_base_url(spec, raw)
            err, warning = check_base_url(spec, url)
            text = err or warning or (f"Uses {url}" if url != raw.strip() else "")
            note.set_text(text)
            note.classes(
                replace="text-xs "
                + ("text-red-600" if err else "text-amber-600" if warning else "sp-faint")
            )
            return url, err

        def _use_preset(url: str, preset_hint: str) -> None:
            inp.set_value(url)
            hint.set_text(preset_hint)
            if url.endswith("://"):
                inp.run_method("focus")
            else:
                _save()

        def _save() -> None:
            """Saved as soon as it's typed (enter / leaving the field): an
            address left unsaved silently sent the key to the default server."""
            raw = str(inp.value or "").strip()
            if raw in ("", "https://", "http://"):
                return  # a preset waiting to be filled in
            url, err = _show(raw)
            if err:
                ui.notify(err, type="warning")
                return
            if url == base_url(settings, spec):
                inp.set_value(url)
                return  # unchanged
            if url == spec.default_base_url:
                pc.base_urls.pop(spec.key, None)
            else:
                pc.base_urls[spec.key] = url
            settings.config.dump(settings.paths.config_file)
            inp.set_value(url)
            ui.notify(f"{spec.name} server saved: {url}")

        inp.on_value_change(lambda e: _show(str(e.value or "")))
        inp.on("keydown.enter", _save)
        inp.on("blur", _save)
        _show(str(inp.value or ""))


def _limits_panel(settings: Settings, spec: ProviderSpec) -> None:
    lim = limiter(settings, spec)
    lines = []
    for op in _OP_LABELS:
        rl = lim.limit(op)
        if rl is not None:
            lines.append(
                f"{spec.name} allows {rl.describe(_OP_LABELS[op])} "
                f"({lim.used(op)} used from this computer)."
            )
    for line in lines:
        ui.label(line).classes("text-xs sp-muted")
    if lines:
        ui.label(
            "A send that would go over is refused before anything is uploaded. "
            "Self-hosted with a different limit? Set posting.rate_limit_overrides in config.yaml."
        ).classes("text-xs sp-faint max-w-xl")


def _connection_panel(settings: Settings, spec: ProviderSpec, client: Any) -> None:
    status = ui.label().classes("text-sm")

    @ui.refreshable
    def _channels() -> None:
        chans, _fetched = load_channel_cache(settings.paths, spec.key)
        if not chans:
            return
        with ui.column().classes("gap-0"):
            for c in chans:
                ui.label(
                    f"• {c.name} — {c.social_network}"
                    + (f"  ⚠ reconnect in {spec.name}" if c.must_reconnect else "")
                ).classes("text-sm" + (" text-amber-600" if c.must_reconnect else ""))

    async def _test() -> None:
        status.set_text("Connecting…")
        try:
            got = await nicegui_run.io_bound(_refresh_channels, settings, spec)
        except PostingError as exc:
            with client:
                where = f" (server: {base_url(settings, spec)})" if spec.base_url_editable else ""
                status.set_text(exc.friendly + where)
                status.classes(replace="text-sm text-red-600")
            return
        if got is None:
            return
        label, chans = got
        who = f" {spec.account_label} “{label}”" if label else ""
        with client:
            status.set_text(f"Connected to {spec.name}{who} · {len(chans)} channel(s)")
            status.classes(replace="text-sm text-emerald-600")
            _channels.refresh()

    ui.button("Test connection & load channels", icon="wifi_tethering", on_click=_test).props(
        "flat dense no-caps"
    )
    _channels()


def posted_note(settings: Settings, pixel_hash: str) -> None:
    """Photo page: which posts include this photo (links)."""
    from sortandpost.posts import posts_containing

    rows = posts_containing(settings.paths, pixel_hash)
    if not rows:
        return
    with ui.row().classes("items-center gap-2 flex-wrap"):
        ui.icon("send", size="1rem").classes("text-emerald-600")
        ui.label("In posts:").classes("text-sm")
        for p in rows:
            label, _ = _status_label(p, settings)
            ui.link(f"{p.title} ({label.lower()})", f"/posts/{p.id}").classes("text-sm")


def compose_button(
    settings: Settings,
    get_hashes: Callable[[], list[str]],
    *,
    title: Callable[[], str],
    caption: Callable[[], str] = lambda: "",
    collection_id: str | None = None,
    label: str | None = "Compose post",
) -> None:
    """A "Compose post" button (icon-only when ``label`` is None)."""

    def _go() -> None:
        compose_from(
            settings,
            get_hashes(),
            title=title(),
            caption=caption(),
            collection_id=collection_id,
        )

    if label is None:
        ui.button(icon="send", on_click=_go).props("flat dense round").tooltip("Compose post")
    else:
        ui.button(label, icon="send", on_click=_go).props("flat dense no-caps")
