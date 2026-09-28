"""Seasonal color themes.

The UI has two independent, per-machine appearance choices (both in
``local/ui_prefs.json``, never synced -- see :mod:`sortandpost.prefs`):

* **mode** -- Light / Dark / System (Quasar's ``body--dark``);
* **season** -- Spring / Summer / Fall / Winter, or **Auto** (the default),
  which follows today's date: Mar-May Spring, Jun-Aug Summer, Sep-Nov Fall,
  Dec-Feb Winter (northern-hemisphere meteorological seasons).

Season x mode gives eight looks. Each season is built from one fixed palette
(:data:`PALETTES`, chosen by the user -- keep these values verbatim):

========  =================================================================
Spring    #fec5bb #fcd5ce #fae1dd #f8edeb #e8e8e4 #d8e2dc #ece4db #ffe5d9
          #ffd7ba #fec89a
Summer    #f7b267 #f79d65 #f4845f #f27059 #f25c54
Fall      #003049 #d62828 #f77f00 #fcbf49 #eae2b7
Winter    #10002b #240046 #3c096c #5a189a #7b2cbf #9d4edd #c77dff #e0aaff
========  =================================================================

:data:`TOKENS` maps each (season, mode) to the CSS design tokens the app's
stylesheet reads (``--sp-bg``, ``--sp-primary``, ...). Palette colors are used
as-is wherever they read well; where a role needs more contrast than the
palette offers (body text, a pastel accent used as text), the value is a
*derived* shade -- a palette color deepened or lightened -- and says so in its
comment. ``tests/test_themes.py`` holds every combination to WCAG contrast
minimums, so tuning a value can't quietly make text unreadable.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from typing import Literal

Season = Literal["spring", "summer", "fall", "winter"]
SeasonChoice = Literal["auto", "spring", "summer", "fall", "winter"]
SEASONS: tuple[Season, ...] = ("spring", "summer", "fall", "winter")
SEASON_CHOICES: tuple[SeasonChoice, ...] = ("auto", *SEASONS)

SEASON_LABELS: dict[SeasonChoice, str] = {
    "auto": "Auto",
    "spring": "Spring",
    "summer": "Summer",
    "fall": "Fall",
    "winter": "Winter",
}
# Material icon per season (the app-bar glyph and the settings swatches).
SEASON_ICONS: dict[SeasonChoice, str] = {
    "auto": "event_repeat",
    "spring": "local_florist",
    "summer": "wb_sunny",
    "fall": "eco",
    "winter": "ac_unit",
}

PALETTES: dict[Season, tuple[str, ...]] = {
    "spring": (
        "#fec5bb",
        "#fcd5ce",
        "#fae1dd",
        "#f8edeb",
        "#e8e8e4",
        "#d8e2dc",
        "#ece4db",
        "#ffe5d9",
        "#ffd7ba",
        "#fec89a",
    ),
    "summer": ("#f7b267", "#f79d65", "#f4845f", "#f27059", "#f25c54"),
    "fall": ("#003049", "#d62828", "#f77f00", "#fcbf49", "#eae2b7"),
    "winter": (
        "#10002b",
        "#240046",
        "#3c096c",
        "#5a189a",
        "#7b2cbf",
        "#9d4edd",
        "#c77dff",
        "#e0aaff",
    ),
}


@dataclass(frozen=True)
class SeasonTokens:
    """One season x mode's design tokens (all ``#rrggbb``)."""

    bg: str  # page background
    surface: str  # cards, inputs, menus
    surface_2: str  # hover / secondary surface
    border: str
    text: str
    text_dim: str  # secondary text (``.sp-muted``)
    text_faint: str  # tertiary text / captions (``.sp-faint``)
    primary: str  # Quasar ``primary``: buttons, active nav, toggles, links
    on_primary: str  # text on a ``primary`` fill
    secondary: str
    accent: str
    focus: str  # keyboard-focus ring on grid tiles
    statusbar_bg: str
    statusbar_fg: str
    statusbar_border: str


TOKENS: dict[tuple[Season, bool], SeasonTokens] = {
    # -- Spring: blush, peach and sage pastels ------------------------------ #
    ("spring", False): SeasonTokens(
        bg="#f8edeb",
        surface="#fffaf8",  # derived: lightened #f8edeb
        surface_2="#fae1dd",
        border="#fcd5ce",
        text="#3b2a26",  # derived: deep brown of #fec89a
        text_dim="#6e554e",  # derived
        text_faint="#8f766f",  # derived
        primary="#b4533f",  # derived: deepened #fec5bb (the pastel itself fails as text)
        on_primary="#ffffff",
        secondary="#fec89a",
        accent="#d8e2dc",
        focus="#fec89a",
        statusbar_bg="#fec89a",
        statusbar_fg="#3b2a26",
        statusbar_border="#ffd7ba",
    ),
    ("spring", True): SeasonTokens(
        bg="#1f1b1a",  # derived: near-black of #e8e8e4 warmed with #ece4db
        surface="#29231f",  # derived
        surface_2="#342c28",  # derived
        border="#4a3d38",  # derived
        text="#f8edeb",
        text_dim="#d8c8c2",  # derived
        text_faint="#a8958e",  # derived
        primary="#fec5bb",
        on_primary="#3b2a26",
        secondary="#ffd7ba",
        accent="#d8e2dc",
        focus="#fec89a",
        statusbar_bg="#161312",  # derived
        statusbar_fg="#fcd5ce",
        statusbar_border="#4a3d38",
    ),
    # -- Summer: apricot to coral ------------------------------------------ #
    ("summer", False): SeasonTokens(
        bg="#fff6ec",  # derived: pale tint of #f7b267
        surface="#ffffff",
        surface_2="#fde6cf",  # derived: tint of #f7b267
        border="#f7c99a",  # derived: tint of #f7b267
        text="#3a1d12",  # derived: deep brown of #f25c54
        text_dim="#6f4232",  # derived
        text_faint="#94604c",  # derived
        primary="#c93a33",  # derived: deepened #f25c54 for text contrast
        on_primary="#ffffff",
        secondary="#f4845f",
        accent="#f7b267",
        focus="#f79d65",
        statusbar_bg="#f27059",
        statusbar_fg="#2a120b",  # derived
        statusbar_border="#f25c54",
    ),
    ("summer", True): SeasonTokens(
        bg="#1e1410",  # derived: ember brown
        surface="#2a1b15",  # derived
        surface_2="#36231b",  # derived
        border="#57331f",  # derived
        text="#fdebdc",  # derived: pale #f7b267
        text_dim="#e0bca6",  # derived
        text_faint="#ad8b77",  # derived
        primary="#f79d65",
        on_primary="#2a120b",
        secondary="#f4845f",
        accent="#f7b267",
        focus="#f7b267",
        statusbar_bg="#140c09",  # derived
        statusbar_fg="#f7b267",
        statusbar_border="#f25c54",
    ),
    # -- Fall: navy, brick, pumpkin, gold, parchment ------------------------ #
    ("fall", False): SeasonTokens(
        bg="#f7f3e1",  # derived: lightened #eae2b7
        surface="#fffdf4",  # derived
        surface_2="#eae2b7",
        border="#d9cf9d",  # derived: #eae2b7 toward #003049
        text="#003049",
        text_dim="#2e5468",  # derived: #003049 lightened
        text_faint="#5d7684",  # derived
        primary="#d62828",
        on_primary="#ffffff",
        secondary="#f77f00",
        accent="#fcbf49",
        focus="#fcbf49",
        statusbar_bg="#003049",
        statusbar_fg="#eae2b7",
        statusbar_border="#003049",
    ),
    ("fall", True): SeasonTokens(
        bg="#001d2e",  # derived: darkened #003049
        surface="#002a40",  # derived
        surface_2="#003049",
        border="#1d4a63",  # derived
        text="#eae2b7",
        text_dim="#c9c29c",  # derived
        text_faint="#8f9a92",  # derived
        primary="#f77f00",
        on_primary="#001d2e",
        secondary="#fcbf49",
        accent="#d62828",
        focus="#fcbf49",
        statusbar_bg="#00131f",  # derived
        statusbar_fg="#fcbf49",
        statusbar_border="#003049",
    ),
    # -- Winter: midnight to lavender --------------------------------------- #
    ("winter", False): SeasonTokens(
        bg="#f8f1ff",  # derived: pale tint of #e0aaff
        surface="#ffffff",
        surface_2="#efe0ff",  # derived: tint of #e0aaff
        border="#e0aaff",
        text="#10002b",
        text_dim="#3c096c",
        text_faint="#6c4d8c",  # derived
        primary="#5a189a",
        on_primary="#ffffff",
        secondary="#7b2cbf",
        accent="#9d4edd",
        focus="#c77dff",
        statusbar_bg="#240046",
        statusbar_fg="#e0aaff",
        statusbar_border="#3c096c",
    ),
    ("winter", True): SeasonTokens(
        bg="#10002b",
        surface="#1a0038",  # derived: between #10002b and #240046
        surface_2="#240046",
        border="#3c096c",
        text="#f3e6ff",  # derived: pale #e0aaff
        text_dim="#e0aaff",
        text_faint="#a585c4",  # derived
        primary="#c77dff",
        on_primary="#10002b",
        secondary="#9d4edd",
        accent="#e0aaff",
        focus="#e0aaff",
        statusbar_bg="#0a001c",  # derived
        statusbar_fg="#e0aaff",
        statusbar_border="#3c096c",
    ),
}


def season_for(day: date) -> Season:
    """The meteorological (northern-hemisphere) season of *day*."""
    return ("winter", "spring", "summer", "fall")[(day.month % 12) // 3]


def resolve_season(choice: SeasonChoice, today: date | None = None) -> Season:
    """*choice* itself, or today's season for ``"auto"``."""
    if choice == "auto":
        return season_for(today or date.today())
    return choice


def body_class(season: Season) -> str:
    return f"sp-season-{season}"


def _decls(t: SeasonTokens) -> str:
    return "".join(
        f"--sp-{name.replace('_', '-')}:{value};" for name, value in asdict(t).items()
    ) + (
        # Quasar's brand variables, so every color=primary / text-primary follows.
        # !important: Quasar sets these inline on <body> (setCssVar).
        f"--q-primary:{t.primary} !important;--q-secondary:{t.secondary} !important;"
        f"--q-accent:{t.accent} !important;"
    )


def theme_css() -> str:
    """Token blocks for every season x mode, keyed on the ``<body>`` classes
    (``sp-season-<s>`` from the app, ``body--dark`` from Quasar)."""
    out: list[str] = []
    for season in SEASONS:
        stripe = ",".join(PALETTES[season])
        for dark in (False, True):
            sel = f"body{'.body--dark' if dark else ''}.{body_class(season)}"
            out.append(f"{sel}{{{_decls(TOKENS[(season, dark)])}}}")
        out.append(
            f"body.{body_class(season)}{{--sp-season-stripe:linear-gradient(90deg,{stripe});}}"
        )
    return "\n".join(out)
