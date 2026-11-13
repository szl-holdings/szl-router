# SPDX-License-Identifier: Apache-2.0
"""Static accessibility contract for the router-control page (no browser needed).

Regression for: axe-core critical aria-required-children on #providers (the empty and
"Registry evidence unavailable" states appended plain <div>s into role="list"), and for
explicit label association on #model, #classification and #cost.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "router_control" / "static"


class _Collect(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.labels_for: set[str] = set()
        self.providers_children: list[dict[str, str | None]] = []
        self._in_providers = 0
        self.brand_label: str | None = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "label" and a.get("for"):
            self.labels_for.add(a["for"] or "")
        if tag == "a" and "brand" in (a.get("class") or "").split():
            self.brand_label = a.get("aria-label")
        if self._in_providers == 1:
            self.providers_children.append(a)
        if a.get("id") == "providers":
            self._in_providers = 1
        elif self._in_providers:
            self._in_providers += 1

    def handle_endtag(self, tag):
        if self._in_providers:
            self._in_providers -= 1


def _page() -> _Collect:
    c = _Collect()
    c.feed((STATIC / "index.html").read_text(encoding="utf-8"))
    return c


def test_policy_controls_have_explicit_labels() -> None:
    assert {"model", "classification", "cost"} <= _page().labels_for


def test_providers_initial_children_are_listitems() -> None:
    kids = _page().providers_children
    assert kids, "#providers should not start as an empty role=list"
    assert all(k.get("role") == "listitem" for k in kids)


def test_every_providers_append_uses_listitem() -> None:
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    # No plain element(...) may be appended straight into #providers.
    assert not re.search(r'providers\.append\(element\(', js)
    assert not re.search(r'\$\("#providers"\)\.replaceChildren\(element\(', js)
    assert 'card.setAttribute("role", "listitem")' in js
    assert 'item.setAttribute("role", "listitem")' in js


def test_brand_accessible_name_contains_visible_text() -> None:
    # WCAG 2.5.3 Label in Name: visible "SZL Router" + "Control interface" must be in the name.
    # The link's name now comes from its visible content; an aria-label override must not return
    # unless it contains the visible words.
    name = _page().brand_label
    assert name is None or ("SZL Router" in name and "Control interface" in name)
