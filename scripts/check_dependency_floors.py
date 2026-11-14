"""Pin and check the dependency floors szl-router declares (stdlib only).

The floors job in .github/workflows/ci.yml installs the package with every declared
floor pinned exactly, checks that pip installed exactly those versions, and runs the
test suite, so a floor that stops working fails CI instead of a user's install:

    python -m pip install --no-deps .
    python scripts/check_dependency_floors.py constraints --extras sign,test --unfloored pytest > floors.txt
    python -m pip install --constraint floors.txt ".[test,sign]"
    python scripts/check_dependency_floors.py verify --extras sign,test --unfloored pytest

Floors come from the installed distribution's Requires-Dist metadata, which is what pip
resolves. Core requirements are always included; --extras adds extras. Anything the
script cannot interpret fails closed: a marker other than extra == "name", a URL
requirement, an operator other than >=, ==, < or !=, a version that is not a plain
release number, an extra the distribution does not provide, and a requirement with no
floor unless --unfloored names it.
"""
from __future__ import annotations

import argparse
import re
import sys
from importlib import metadata

DIST = "szl-router"
_REQ = re.compile(r"([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*\(?\s*((?:[<>=!~]=?\s*[0-9][0-9.]*\s*,?\s*)*)\)?")
_SPEC = re.compile(r"([<>=!~]=?)\s*([0-9][0-9.]*)")
_EXTRA = re.compile(r"""extra\s*==\s*(["'])([A-Za-z0-9._-]+)\1""")
_RELEASE = re.compile(r"[0-9]+(?:\.[0-9]+)*")


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _version(text: str) -> tuple[int, ...]:
    parts = [int(p) for p in text.split(".")]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def parse(line: str) -> tuple[str, str | None, list[tuple[str, str]]]:
    """(normalized name, extra or None for core, [(operator, version), ...])."""
    requirement, _, marker = line.partition(";")
    extra = None
    if marker.strip():
        match = _EXTRA.fullmatch(marker.strip())
        if not match:
            raise ValueError(f"unsupported marker in {line!r}")
        extra = _norm(match.group(2))
    match = _REQ.fullmatch(requirement.strip())
    if not match:
        raise ValueError(f"unsupported requirement {line!r}")
    specs = _SPEC.findall(match.group(2))
    for op, version in specs:
        if op not in (">=", "==", "<", "!=") or not _RELEASE.fullmatch(version):
            raise ValueError(f"cannot pin a floor from {op}{version} in {line!r}")
    return _norm(match.group(1)), extra, specs


def floors(requires: list[str], provided: list[str], extras: list[str],
           unfloored: list[str]) -> tuple[dict[str, str], list[str]]:
    """({distribution: floor}, problems) for core plus the named extras."""
    known = {_norm(p) for p in provided}
    selected = {_norm(e) for e in extras}
    problems = [f"{DIST} does not provide extra {e!r}" for e in extras if _norm(e) not in known]
    found: dict[str, str | None] = {}
    for line in requires:
        name, extra, specs = parse(line)
        if extra is not None and extra not in selected:
            continue
        lows = [version for op, version in specs if op in (">=", "==")]
        low = max(lows, key=_version) if lows else None
        if low is not None and (found.get(name) is None or _version(low) > _version(found[name])):
            found[name] = low
        else:
            found.setdefault(name, None)
    allowed = {_norm(n) for n in unfloored}
    problems += [f"{name} has no >= floor; declare one or pass --unfloored {name}"
                 for name in sorted(found) if found[name] is None and name not in allowed]
    problems += [f"--unfloored {name} does not name an unfloored requirement"
                 for name in sorted(allowed) if found.get(name, "") is not None]
    if not any(found.values()):
        problems.append(f"no floors found in {DIST} metadata")
    return {name: low for name, low in found.items() if low is not None}, problems


def installed_problems(pins: dict[str, str], version_of=metadata.version) -> list[str]:
    """Every pinned distribution must be installed at exactly its floor."""
    problems = []
    for name, low in sorted(pins.items()):
        try:
            installed = version_of(name)
        except metadata.PackageNotFoundError:
            problems.append(f"{name}: not installed (declared floor {low})")
            continue
        if not _RELEASE.fullmatch(installed) or _version(installed) != _version(low):
            problems.append(f"{name}: installed {installed}, declared floor {low}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=f"Pin or check the dependency floors of {DIST}.")
    parser.add_argument("command", choices=("constraints", "verify"))
    parser.add_argument("--extras", default="", help="comma-separated extras to include with core")
    parser.add_argument("--unfloored", action="append", default=[],
                        help="a requirement allowed to have no floor (repeatable)")
    args = parser.parse_args(argv)
    extras = [e.strip() for e in args.extras.split(",") if e.strip()]
    try:
        requires = metadata.requires(DIST) or []
        provided = metadata.metadata(DIST).get_all("Provides-Extra") or []
        pins, problems = floors(requires, provided, extras, args.unfloored)
        if args.command == "verify":
            problems += installed_problems(pins)
    except (metadata.PackageNotFoundError, ValueError) as exc:
        problems = [f"{type(exc).__name__}: {exc}"]
    if problems:
        print("dependency floors: FAIL", *problems, sep="\n  ", file=sys.stderr)
        return 1
    if args.command == "constraints":
        print(f"# {DIST} {metadata.version(DIST)}: core + {', '.join(extras) or 'no extras'}")
        for name, low in sorted(pins.items()):
            print(f"{name}=={low}")
        for name in args.unfloored:
            print(f"# {name}: no declared floor, so pip installs the newest release")
    else:
        print(f"dependency floors: PASS, {len(pins)} distributions installed at their declared floor")
        for name, low in sorted(pins.items()):
            print(f"  {name}=={low}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
