"""Helpers of src/cli/tag.py: reading and rebuilding the cluster tag-style.

Protects datacenter.cfg: tag-style is one property, so rewriting the
color-map must keep shape, ordering and case-sensitive as they were.
"""

from __future__ import annotations

import pytest

from src.cli.tag import _build_tag_style, _parse_color_map


@pytest.mark.parametrize("style, expected", [
    (None, {}),
    ("", {}),
    ({}, {}),
    ({"shape": "circle"}, {}),
    ("shape=circle", {}),
    ({"color-map": "prod:FF0000;dev:00FF00:000000"}, {"prod": "FF0000", "dev": "00FF00:000000"}),
    ("color-map=prod:FF0000;dev:00FF00:000000,shape=dense",
     {"prod": "FF0000", "dev": "00FF00:000000"}),
    ("shape=full,color-map=web:0000FF", {"web": "0000FF"}),
    ({"color-map": "a:111111; ;b"}, {"a": "111111"}),
])
def test_parse_color_map(style, expected):
    assert _parse_color_map(style) == expected


def test_parse_color_map_keeps_distinct_tags():
    colors = _parse_color_map({"color-map": "prod:FF0000;production:00FF00"})
    assert colors["prod"] != colors["production"]


def test_build_keeps_other_options_from_a_dict():
    existing = {"color-map": "old:000000", "shape": "circle", "ordering": "config", "case-sensitive": 1}
    assert _build_tag_style({"web": "0000FF", "db": "FF0000"}, existing) == \
        "color-map=db:FF0000;web:0000FF,shape=circle,ordering=config,case-sensitive=1"


def test_build_keeps_other_options_from_a_string():
    existing = "shape=dense,color-map=old:000000,case-sensitive=0"
    assert _build_tag_style({"web": "0000FF:FFFFFF"}, existing) == \
        "color-map=web:0000FF:FFFFFF,shape=dense,case-sensitive=0"


def test_build_without_colors_keeps_only_the_other_options():
    assert _build_tag_style({}, {"color-map": "a:111111", "shape": "none"}) == "shape=none"
    assert _build_tag_style({}, "color-map=a:111111,ordering=alphabetical") == "ordering=alphabetical"


@pytest.mark.parametrize("existing", [None, "", {}, {"color-map": "a:1"}, "color-map=a:1",
                                      {"shape": None}, {"shape": ""}])
def test_build_empty_gives_empty(existing):
    assert _build_tag_style({}, existing) == ""


def test_build_then_parse_round_trip():
    colors = {"prod": "FF0000", "dev": "00FF00:000000"}
    style = _build_tag_style(colors, {"shape": "circle"})
    assert _parse_color_map(style) == colors
