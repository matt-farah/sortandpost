"""``sortandpost.themes`` -- seasonal palettes, date -> season, readable tokens."""

from __future__ import annotations

from dataclasses import asdict
from datetime import date

import pytest

from sortandpost.themes import (
    PALETTES,
    SEASONS,
    TOKENS,
    body_class,
    resolve_season,
    season_for,
    theme_css,
)


def _luminance(hex_color: str) -> float:
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i : i + 2], 16) / 255 for i in (0, 2, 4))

    def lin(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def _contrast(a: str, b: str) -> float:
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


@pytest.mark.parametrize(
    ("day", "season"),
    [
        (date(2026, 2, 28), "winter"),
        (date(2026, 3, 1), "spring"),
        (date(2026, 5, 31), "spring"),
        (date(2026, 6, 1), "summer"),
        (date(2026, 8, 31), "summer"),
        (date(2026, 9, 1), "fall"),
        (date(2026, 11, 30), "fall"),
        (date(2026, 12, 1), "winter"),
        (date(2027, 1, 15), "winter"),
    ],
)
def test_season_for_meteorological_boundaries(day: date, season: str) -> None:
    assert season_for(day) == season


def test_resolve_season_auto_follows_date_and_pins_otherwise() -> None:
    assert resolve_season("auto", date(2026, 7, 4)) == "summer"
    assert resolve_season("fall", date(2026, 7, 4)) == "fall"


def test_palettes_are_the_documented_ones() -> None:
    assert PALETTES["summer"] == ("#f7b267", "#f79d65", "#f4845f", "#f27059", "#f25c54")
    assert PALETTES["fall"] == ("#003049", "#d62828", "#f77f00", "#fcbf49", "#eae2b7")
    assert len(PALETTES["spring"]) == 10
    assert len(PALETTES["winter"]) == 8


@pytest.mark.parametrize("key", [(s, d) for s in SEASONS for d in (False, True)])
def test_tokens_are_readable(key: tuple[str, bool]) -> None:
    t = TOKENS[key]  # type: ignore[index]
    for value in asdict(t).values():
        assert value.startswith("#") and len(value) == 7
    for surface in (t.bg, t.surface):
        assert _contrast(t.text, surface) >= 7
        assert _contrast(t.text_dim, surface) >= 4.5
        assert _contrast(t.text_faint, surface) >= 3
        assert _contrast(t.primary, surface) >= 3  # buttons / active nav / links
    assert _contrast(t.on_primary, t.primary) >= 4.5
    assert _contrast(t.statusbar_fg, t.statusbar_bg) >= 4.5


def test_theme_css_covers_every_season_and_mode() -> None:
    css = theme_css()
    for season in SEASONS:
        assert f"body.{body_class(season)}{{" in css
        assert f"body.body--dark.{body_class(season)}{{" in css
    assert "--q-primary:" in css and "!important" in css


def test_first_paint_css_matches_tokens() -> None:
    from sortandpost.themes import first_paint_css

    assert f"--sp-bg:{TOKENS[('fall', True)].bg}" in first_paint_css("fall", True)
    assert "color-scheme:light" in first_paint_css("fall", False)
    both = first_paint_css("winter", None)
    assert TOKENS[("winter", False)].bg in both and TOKENS[("winter", True)].bg in both
    assert "prefers-color-scheme: dark" in both
