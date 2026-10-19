"""The ``/help`` page: every keyboard shortcut in the app, grouped by the page it
works on. Opened from the header's ``?`` button, or by pressing ``?`` anywhere
outside a text field (:data:`HELP_KEY_JS`, installed by ``app._header``).

:data:`SECTIONS` is the one list of shortcuts -- keep it in step with the
``ui.keyboard`` handlers in ``app`` (the library grid, the photo page, Rate and
the collection picker) when a key is added or changed.

Like ``achievements_ui`` this reuses ``app``'s page chrome through a lazy import.
"""

from __future__ import annotations

from dataclasses import dataclass

from nicegui import ui
from nicegui.elements.mixins.text_element import TextElement

PAGE_URL = "/help"

_UTM = "?utm_source=unsplash&utm_medium=referral&utm_content=creditCopyText"
ART_ARTIST = "Brenda Carolina Ortiz Diaz"
ART_ARTIST_URL = f"https://unsplash.com/@brenortiz/illustrations{_UTM}"
UNSPLASH_URL = f"https://unsplash.com/illustrations{_UTM}"


@dataclass(frozen=True)
class ArtCredit:
    mode: str  # the achievements persona the illustration is
    who: str
    url: str


ART_CREDITS: tuple[ArtCredit, ...] = (
    ArtCredit(
        "tugs",
        "Tugs, the sweet cat",
        f"https://unsplash.com/illustrations/an-orange-cat-with-a-raised-tail-2uR7BOZi3ZQ{_UTM}",
    ),
    ArtCredit(
        "percival",
        "Percival, the grumpy cat",
        f"https://unsplash.com/illustrations/a-grumpy-cartoon-cat-is-sitting-down-DEPoWtdRevo{_UTM}",
    ),
)


@dataclass(frozen=True)
class Shortcut:
    keys: tuple[str, ...]  # alternatives ("←", "K"); "Shift+Space" is one combo
    does: str


@dataclass(frozen=True)
class Section:
    title: str
    icon: str
    where: str  # where the keys work
    shortcuts: tuple[Shortcut, ...]


SECTIONS: tuple[Section, ...] = (
    Section(
        "Everywhere",
        "keyboard",
        "Any page, while you're not typing in a field.",
        (
            Shortcut(("?",), "Open this page"),
            Shortcut(("Esc",), "Leave a text field, so the page's shortcuts work again"),
        ),
    ),
    Section(
        "Library",
        "photo_library",
        "The photo grid. Keys act on the ringed photo, or the one under the mouse.",
        (
            Shortcut(("←", "→"), "Move to the previous / next photo"),
            Shortcut(("↑", "↓"), "Move up / down a row"),
            Shortcut(("Enter",), "Open the photo"),
            Shortcut(("1-5",), "Rate 1 to 5 stars"),
            Shortcut(("0",), "Clear the rating"),
            Shortcut(("F",), "Flag / unflag for editing"),
            Shortcut(("Space",), "Select / deselect the photo"),
            Shortcut(("Shift+Space",), "Select a range from the last selected photo"),
            Shortcut(("Shift+click",), "Select a range"),
            Shortcut(("⌘+click", "Ctrl+click"), "Select / deselect one photo"),
            Shortcut(("Esc",), "Clear the ring and the selection"),
            Shortcut(("/",), "Jump to the search box"),
        ),
    ),
    Section(
        "Photo page",
        "image",
        "One photo, large.",
        (
            Shortcut(("←", "K"), "Previous photo"),
            Shortcut(("→", "J"), "Next photo"),
            Shortcut(("1-5",), "Rate 1 to 5 stars"),
            Shortcut(("0",), "Clear the rating"),
            Shortcut(("F",), "Flag / unflag for editing"),
        ),
    ),
    Section(
        "Rate",
        "star_rate",
        "Rating unrated photos one at a time.",
        (
            Shortcut(("1-5",), "Rate 1 to 5 stars and go to the next photo"),
            Shortcut(("→", "S", "Space"), "Skip"),
            Shortcut(("←",), "Back to the previous photo"),
            Shortcut(("F",), "Flag / unflag for editing"),
        ),
    ),
    Section(
        "Adding photos to a collection",
        "collections_bookmark",
        "The picker opened by a collection's “Add photos”.",
        (
            Shortcut(("←", "→", "↑", "↓"), "Move between photos"),
            Shortcut(("Space",), "Select / deselect the photo"),
            Shortcut(("Shift+Space",), "Select a range"),
            Shortcut(("Enter",), "Add the selected photos"),
            Shortcut(("Esc",), "Close without adding"),
        ),
    ),
)

# `?` opens /help from any page. A plain listener (not ui.keyboard -- one of those
# per page, and the grid's owns it); skipped while typing or with Ctrl / Alt / ⌘.
HELP_KEY_JS = f"""
<script>
if (!window.__spHelpKey) {{
  window.__spHelpKey = 1;
  document.addEventListener('keydown', (e) => {{
    if (e.key !== '?' || e.ctrlKey || e.metaKey || e.altKey || e.repeat) return;
    const t = document.activeElement;
    if (t && (t.isContentEditable || /^(input|textarea|select)$/i.test(t.tagName))) return;
    if (location.pathname === '{PAGE_URL}') return;
    e.preventDefault();
    location.href = '{PAGE_URL}';
  }});
}}
</script>
"""

_CSS = """
.sp-help-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(20rem, 1fr));
  gap: 1rem;
  width: 100%;
  align-items: start;
}
.sp-help-card { padding: 1rem 1.1rem; }
.sp-help-row {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 1rem;
  padding: .35rem 0;
  border-top: 1px solid var(--sp-border);
  font-size: .875rem;
}
.sp-help-row:first-of-type { border-top: none; }
.sp-help-keys { display: flex; flex-shrink: 0; gap: .25rem; justify-content: flex-end; }
.sp-help-or { color: var(--sp-text-faint); font-size: .7rem; align-self: center; }
kbd.sp-kbd {
  display: inline-block;
  min-width: 1.6rem;
  padding: .05rem .4rem;
  border: 1px solid var(--sp-border);
  border-bottom-width: 2px;
  border-radius: .3rem;
  background-color: var(--sp-surface-2);
  color: var(--sp-text);
  font-size: .75rem;
  line-height: 1.3rem;
  text-align: center;
  white-space: nowrap;
}
"""


class _Kbd(TextElement):
    """A ``<kbd>`` key cap."""

    def __init__(self, text: str) -> None:
        super().__init__(tag="kbd", text=text)


def _keys(shortcut: Shortcut) -> None:
    with ui.element("div").classes("sp-help-keys"):
        for i, combo in enumerate(shortcut.keys):
            if i:
                ui.label("or").classes("sp-help-or")
            _Kbd(combo).classes("sp-kbd")


def _credits() -> None:
    """Attribution for the achievements cats (Unsplash's credit text + links)."""
    from sortandpost.achievements_ui import art_url  # lazy: it imports app

    ui.label("Credits").classes("sp-display text-xl font-semibold pt-4")
    with ui.element("div").classes("sp-help-grid"):
        for credit in ART_CREDITS:
            with ui.row().classes("sp-card sp-help-card items-center gap-3 no-wrap"):
                ui.image(art_url(credit.mode)).classes("sp-ach-persona shrink-0")
                with ui.column().classes("gap-0 min-w-0"):
                    ui.label(credit.who).classes("font-medium")
                    with ui.row().classes("items-baseline gap-1 text-sm sp-muted"):
                        ui.link("Illustration", credit.url, new_tab=True).classes("text-primary")
                        ui.label("by")
                        ui.link(ART_ARTIST, ART_ARTIST_URL, new_tab=True).classes("text-primary")
                        ui.label("on")
                        ui.link("Unsplash", UNSPLASH_URL, new_tab=True).classes("text-primary")


def register_pages() -> None:
    from sortandpost import app as A  # lazy: app imports this module

    @ui.page(PAGE_URL)
    def help_page() -> None:
        A._header(PAGE_URL)
        ui.add_css(_CSS, shared=True)
        with ui.column().classes("w-full max-w-6xl mx-auto p-4 pb-16 gap-4"):
            ui.label("Keyboard shortcuts").classes("sp-display text-2xl font-semibold")
            ui.label(
                "Shortcuts don't fire while you're typing in a field — press Esc to leave it."
            ).classes("text-sm sp-muted")
            with ui.element("div").classes("sp-help-grid"):
                for section in SECTIONS:
                    with ui.column().classes("sp-card sp-help-card gap-1"):
                        with ui.row().classes("items-center gap-2 no-wrap"):
                            ui.icon(section.icon).classes("text-lg sp-muted")
                            ui.label(section.title).classes("text-lg font-medium")
                        ui.label(section.where).classes("text-xs sp-muted pb-1")
                        with ui.element("div").classes("w-full"):
                            for shortcut in section.shortcuts:
                                with ui.element("div").classes("sp-help-row"):
                                    ui.label(shortcut.does)
                                    _keys(shortcut)
            _credits()
