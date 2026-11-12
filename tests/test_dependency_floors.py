"""Negative controls for scripts/check_dependency_floors.py."""
from importlib import metadata

import pytest

from scripts import check_dependency_floors as floors

REQUIRES = [
    "fastapi>=0.115.7",
    "uvicorn[standard]>=0.27",
    "httpx>=0.28",
    "pydantic>=2.0.2",
    'szl-receipt-dsse>=0.3.3; extra == "sign"',
    'cryptography>=42.0.0; extra == "sign"',
    'huggingface-hub>=1.10.1; extra == "hf-publish"',
    'pytest; extra == "test"',
    'httpx; extra == "test"',
    'szl-receipt-dsse>=0.3.3; extra == "test"',
]
PROVIDED = ["sign", "hf-publish", "test"]


def test_core_and_selected_extras_are_pinned_at_their_floors():
    pins, problems = floors.floors(REQUIRES, PROVIDED, ["sign", "test"], ["pytest"])
    assert problems == []
    assert pins == {"fastapi": "0.115.7", "uvicorn": "0.27", "httpx": "0.28", "pydantic": "2.0.2",
                    "szl-receipt-dsse": "0.3.3", "cryptography": "42.0.0"}


def test_negative_control_unfloored_requirement_fails_closed():
    assert floors.floors(REQUIRES, PROVIDED, ["test"], [])[1] == [
        "pytest has no >= floor; declare one or pass --unfloored pytest"]


def test_negative_control_unknown_extra_and_stale_exemption_fail_closed():
    assert floors.floors(REQUIRES, PROVIDED, ["sign", "tset"], ["httpx"])[1] == [
        "szl-router does not provide extra 'tset'",
        "--unfloored httpx does not name an unfloored requirement"]


def test_negative_control_unpinnable_requirements_fail_closed():
    for line in ('tomli>=2; python_version < "3.11"', "httpx @ https://example.invalid/httpx.whl",
                 "fastapi~=0.115", "fastapi>0.115", "fastapi>=0.115rc1"):
        with pytest.raises(ValueError):
            floors.parse(line)


def test_negative_control_installed_versions_must_equal_the_floors():
    installed = {"fastapi": "0.162.0", "uvicorn": "0.27.0", "pydantic": "2.0.2.post1"}

    def version_of(name):
        if name not in installed:
            raise metadata.PackageNotFoundError(name)
        return installed[name]

    pins = {"fastapi": "0.115.7", "uvicorn": "0.27", "httpx": "0.28", "pydantic": "2.0.2"}
    assert floors.installed_problems(pins, version_of) == [
        "fastapi: installed 0.162.0, declared floor 0.115.7",
        "httpx: not installed (declared floor 0.28)",
        "pydantic: installed 2.0.2.post1, declared floor 2.0.2",
    ]
