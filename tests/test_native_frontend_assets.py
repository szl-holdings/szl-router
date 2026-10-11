"""Qualify exact native art and cache-safe component URLs in both shipped fronts."""
from __future__ import annotations

import hashlib
import json
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("directory", ["space/assets/szl", "router_control/static/szl"])
def test_projection_is_an_exact_subset_of_the_accepted_native_source(directory: str) -> None:
    where = ROOT / directory
    projection = json.loads((where / "PROJECTION.json").read_text())
    upstream = json.loads((where / "SOURCE.json").read_text())
    assert projection["canonical_token_source"] == upstream["source_commit"] == "27d12e77fc0f9f1a0e5fae8b34adf2380d51d070"
    assert projection["bundle_commit"] == "4309f6fce89179331b0b3ed8c99494e76c5be256"
    expected = {
        "szl-tokens.css": "81f7c9baeef0b435e6e3f9d0c4e0d70e40fa17df5d1f30697f623cd3ef5f8eab",
        "SOURCE.json": "967a77235227f1d75852fb61873e46336e67403cbc0e7e12b141e31c83fbd93d",
        "szl-icon-32.svg": "6e2b7d72a3af3d9602b79de41df0194ede6496a9661f057108ec224c0efc92e6",
    }
    assert set(projection["installed"]) == set(expected)
    for name, digest in expected.items():
        assert hashlib.sha256((where / name).read_bytes()).hexdigest() == digest
        assert projection["installed"][name]["sha256"] == digest


class Assets(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.styles: list[str] = []
        self.ids: list[str] = []
        self.anchors: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = dict(attrs)
        if attr.get("id"):
            self.ids.append(str(attr["id"]))
        if tag == "link" and attr.get("rel") == "stylesheet":
            self.styles.append(str(attr["href"]))
        if tag == "a" and str(attr.get("href", "")).startswith("#"):
            self.anchors.append(str(attr["href"])[1:])


@pytest.mark.parametrize("page,base", [
    ("space/index.html", "space"),
    ("router_control/static/index.html", "router_control"),
])
def test_component_styles_use_content_versions_and_native_tokens_first(page: str, base: str) -> None:
    html = (ROOT / page).read_text()
    assets = Assets()
    assets.feed(html)
    assert "<style>" not in html
    assert assets.styles and "szl-tokens.css" in assets.styles[0]
    assert len(assets.ids) == len(set(assets.ids))
    assert set(assets.anchors) <= set(assets.ids)
    for url in assets.styles:
        parsed = urlsplit(url)
        assert not parsed.scheme and not parsed.netloc
        source = ROOT / base / parsed.path.lstrip("/")
        assert source.is_file() and not source.is_symlink()
        assert parse_qs(parsed.query) == {"v": [hashlib.sha256(source.read_bytes()).hexdigest()[:12]]}


def test_control_source_evidence_includes_its_native_projection() -> None:
    from fastapi.testclient import TestClient
    from router_control.app import app

    with TestClient(app) as client:
        source = client.get("/api/source").json()
        for name, mime in (("szl-tokens.css", "text/css"), ("szl-icon-32.svg", "image/svg+xml"), ("SOURCE.json", "application/json"), ("PROJECTION.json", "application/json")):
            path = "router_control/static/szl/" + name
            response = client.get("/static/szl/" + name)
            assert response.status_code == 200
            assert response.headers["content-type"].startswith(mime)
            assert source["controlled_files"][path] == hashlib.sha256(response.content).hexdigest()


def test_control_workspace_navigation_needs_no_script_or_effect() -> None:
    html = (ROOT / "router_control/static/index.html").read_text()
    assert 'aria-label="Router workspaces"' in html
    for section in ("readiness", "plan", "completion", "registry", "source"):
        assert f'href="#{section}"' in html
        assert f'id="{section}"' in html
    assert "⟡" not in html
    assert 'src="/static/szl/szl-icon-32.svg"' in html
