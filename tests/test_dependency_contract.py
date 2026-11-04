"""Dependency contract for szl-router (stdlib only; executes no repository code).

Every non-stdlib import in the repository's Python files is classified here, and the
classification must agree with the manifests:

* DECLARED       listed in pyproject.toml core ([project].dependencies) or in the named
                 extras.
* TRANSITIVE     imported only by tests, and always installed by a core requirement
                 (fastapi requires starlette; uvicorn[standard] requires pyyaml).
                 Package, tooling and Space code may not rely on these.
* HOST_PROVIDED  supplied by the deployment host, never by a distribution; every import
                 site must catch ImportError.

Files are grouped by area. Package modules (szl_router/, router_control/) may load only
core requirements at module scope. The Space app (space/*.py) runs in an image that
installs nothing, so it may import only the standard library. requirements.txt must
equal core, and the router-control pin files must pin exactly at or above the floors.
Unclassified imports and files outside a known area fail closed. pyproject.toml is read
with a minimal reader so the test also runs on Python 3.10 (no tomllib); wherever
tomllib exists the reader is cross-checked against it. The negative controls at the end
show that each check can fail.
"""
from __future__ import annotations

import ast
import functools
import os
import re
import sys
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    tomllib = None

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGES = ("szl_router", "router_control")

# import name -> (distribution, groups that must list it); "core" is [project].dependencies.
DECLARED = {
    "fastapi": ("fastapi", ("core",)),
    "httpx": ("httpx", ("core", "test")),
    "pydantic": ("pydantic", ("core",)),
    "uvicorn": ("uvicorn", ("core",)),
    "szl_receipt": ("szl-receipt-dsse", ("sign", "test")),
    "cryptography": ("cryptography", ("sign", "test")),
    "huggingface_hub": ("huggingface-hub", ("hf-publish", "test")),
    "pytest": ("pytest", ("test",)),
}
# import name -> (distribution, core requirement that always installs it).
TRANSITIVE = {
    "starlette": ("starlette", "fastapi"),
    "yaml": ("pyyaml", "uvicorn[standard]"),
}
HOST_PROVIDED = frozenset({"szl_backend_hardening"})  # fabric probe on the deployment box
TEST_REPEATS = ("sign", "hf-publish")  # the test extra lists every requirement of these
# tomllib joined the standard library in 3.11; the release gate that imports it runs on 3.12.
_STDLIB = frozenset(sys.stdlib_module_names) | {"tomllib"}
_GUARDS = {"ImportError", "ModuleNotFoundError", "Exception", "BaseException"}
_IMPORT_CALLS = ("__import__", "import_module", "importorskip")
_SKIP_DIRS = {"__pycache__", "build", "dist", "node_modules"}


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


_REQ = re.compile(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[([^\]]*)\])?\s*((?:[<>=!~]=?\s*[0-9][0-9.]*\s*,?\s*)*)$")


def parse_requirement(requirement: str) -> tuple[str, frozenset[str], tuple[tuple[str, str], ...]]:
    """(normalized distribution, extras, ((operator, version), ...)); markers and URLs fail closed."""
    match = _REQ.fullmatch(requirement)
    if not match:
        raise ValueError(f"unsupported requirement: {requirement!r}")
    extras = frozenset(_norm(e) for e in (match.group(2) or "").split(",") if e.strip())
    specs = tuple(re.findall(r"([<>=!~]=?)\s*([0-9][0-9.]*)", match.group(3)))
    return _norm(match.group(1)), extras, specs


def _version(text: str) -> tuple[int, ...]:
    parts = [int(p) for p in text.split(".")]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def _floor(requirement: str) -> tuple[int, ...] | None:
    floors = [_version(v) for op, v in parse_requirement(requirement)[2] if op == ">="]
    return max(floors) if floors else None


def area_of(path: str) -> str | None:
    parts = path.split("/")
    if parts[0] in PACKAGES:
        return "package"
    if parts[0] == "tests" or parts[:2] == ["space", "tests"] or (
            len(parts) == 1 and (parts[0].startswith("test_") or parts[0] == "conftest.py")):
        return "test"
    if parts[0] == "space":
        return "space"
    if parts[0] in ("scripts", "demo", "examples") or parts[:2] == [".github", "scripts"]:
        return "tooling"
    return None


def repo_sources(root: Path = REPO_ROOT) -> dict[str, str]:
    """{posix path: text} for every .py file outside dot-directories (except .github),
    build output and virtual environments."""
    sources = {}
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in _SKIP_DIRS and not d.endswith(".egg-info")
            and not (d.startswith(".") and not (here == root and d == ".github"))
            and not (here / d / "pyvenv.cfg").exists())
        for name in sorted(filenames):
            if name.endswith(".py"):
                path = here / name
                sources[path.relative_to(root).as_posix()] = path.read_text(encoding="utf-8")
    return sources


def local_names(sources: dict[str, str]) -> set[str]:
    """Names importable from this checkout: top-level directories and every module stem
    (tests and scripts put their own directories on sys.path)."""
    names = set(PACKAGES)
    for path in sources:
        parts = path.split("/")
        names.update({parts[0].removesuffix(".py"), parts[-1].removesuffix(".py")})
    return names


def _call_name(node: ast.AST) -> str | None:
    func = getattr(node, "func", None)
    return getattr(func, "id", getattr(func, "attr", None))


def _imported_names(node: ast.AST) -> list[str]:
    """Top-level names an absolute import (or a literal __import__, import_module or
    pytest.importorskip call) loads; relative imports are always local."""
    if isinstance(node, ast.Import):
        return [alias.name.split(".")[0] for alias in node.names]
    if isinstance(node, ast.ImportFrom):
        return [] if node.level else [node.module.split(".")[0]]
    if (isinstance(node, ast.Call) and _call_name(node) in _IMPORT_CALLS and node.args
            and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
        return [node.args[0].value.split(".")[0]]
    return []


def _catches_import_error(handler: ast.ExceptHandler) -> bool:
    if handler.type is None:
        return True
    types = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return any(getattr(t, "id", getattr(t, "attr", None)) in _GUARDS for t in types)


def _is_main_block(node: ast.AST) -> bool:
    test = getattr(node, "test", None)
    return (isinstance(node, ast.If) and isinstance(test, ast.Compare)
            and isinstance(test.left, ast.Name) and test.left.id == "__name__"
            and isinstance(test.comparators[0], ast.Constant)
            and test.comparators[0].value == "__main__")


def import_sites(sources: dict[str, str]) -> list[tuple[str, str, int, str, bool]]:
    """(name, path, line, scope, guarded) for every import in ``sources``.

    scope is "module" (runs on import), "function" (runs when called) or "main" (an
    ``if __name__ == "__main__":`` block); guarded means a ``try`` in the same scope
    catches ImportError around the import, or the import is ``pytest.importorskip``.
    """
    sites: list[tuple[str, str, int, str, bool]] = []

    def visit(node: ast.AST, path: str, scope: str, guarded: bool) -> None:
        for field, value in ast.iter_fields(node):
            for child in value if isinstance(value, list) else [value]:
                if not isinstance(child, ast.AST):
                    continue
                inner_scope, inner_guarded = scope, guarded
                if field == "body" and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                    inner_scope, inner_guarded = ("main" if scope == "main" else "function"), False
                elif field == "body" and isinstance(node, ast.Try):
                    inner_guarded = guarded or any(_catches_import_error(h) for h in node.handlers)
                elif field == "body" and _is_main_block(node):
                    inner_scope = "main"
                skips = _call_name(child) == "importorskip"
                for name in _imported_names(child):
                    sites.append((name, path, child.lineno, inner_scope, inner_guarded or skips))
                visit(child, path, inner_scope, inner_guarded)

    for path, text in sorted(sources.items()):
        visit(ast.parse(text, filename=path), path, "module", False)
    return sites


def _external(sites, local):
    return [s for s in sites if s[0] not in local and s[0] not in _STDLIB]


def classification_problems(sites, local) -> list[str]:
    problems = []
    for name, path, line, scope, guarded in _external(sites, local):
        area, where = area_of(path), f"{path}:{line}"
        if area == "space":
            problems.append(f"{where} imports {name!r}; the Space image installs nothing, "
                            "so space/ may import only the standard library")
        elif name in HOST_PROVIDED:
            if not guarded:
                problems.append(f"{where} imports host-provided {name!r} without an ImportError guard")
        elif name in TRANSITIVE:
            if area != "test":
                problems.append(f"{where} imports {name!r}, which arrives only through "
                                f"{TRANSITIVE[name][1]!r}; declare {TRANSITIVE[name][0]!r} directly")
        elif name not in DECLARED:
            problems.append(f"{where} imports unclassified {name!r}; "
                            "add it to DECLARED, TRANSITIVE or HOST_PROVIDED")
        elif area == "package" and scope == "module" and not guarded and "core" not in DECLARED[name][1]:
            problems.append(f"{where} imports {name!r} at module scope; package modules "
                            "may load only core requirements on import")
        elif area == "test" and not guarded and not {"core", "test"} & set(DECLARED[name][1]):
            problems.append(f"{where} imports {name!r} unguarded; add {DECLARED[name][0]!r} to test")
    return problems


def declaration_problems(declared: dict[str, list[str]]) -> list[str]:
    """``declared`` maps "core" and each extra to its requirement strings."""
    parsed = {group: [parse_requirement(r) for r in reqs] for group, reqs in declared.items()}
    listed = {group: {dist for dist, _extras, _specs in reqs} for group, reqs in parsed.items()}
    problems = [f"{dist} missing from [{group}] (imported as {name!r})"
                for name, (dist, groups) in sorted(DECLARED.items()) for group in groups
                if _norm(dist) not in listed.get(group, set())]
    core = {(dist, extras) for dist, extras, _specs in parsed.get("core", [])}
    for name, (dist, provider) in sorted(TRANSITIVE.items()):
        if parse_requirement(provider)[:2] not in core:
            problems.append(f"{dist} (imported as {name!r}) arrives only through "
                            f"{provider!r}, which core no longer lists")
    test = set(parsed.get("test", []))
    for group in TEST_REPEATS:
        problems += [f"[test] lacks {r!r} from [{group}]" for r in declared.get(group, [])
                     if parse_requirement(r) not in test]
    return problems


def read_requirements(text: str) -> list[str]:
    return [line.split("#", 1)[0].strip() for line in text.splitlines() if line.split("#", 1)[0].strip()]


def drift_problems(core: list[str], requirements: list[str]) -> list[str]:
    a = {parse_requirement(r): r for r in requirements}
    b = {parse_requirement(r): r for r in core}
    return ([f"only in requirements.txt: {r}" for r in sorted(a[k] for k in set(a) - set(b))]
            + [f"only in pyproject core: {r}" for r in sorted(b[k] for k in set(b) - set(a))])


def pin_problems(declared: dict[str, list[str]], control: list[str], dev: list[str]) -> list[str]:
    """requirements-router-control.txt pins exactly the core distributions, and the dev file
    adds exact pins of test distributions; every pin sits at or above the declared floor."""
    problems = []

    def pins(lines, name):
        found = {}
        for line in lines:
            dist, extras, specs = parse_requirement(line)
            if len(specs) != 1 or specs[0][0] != "==":
                problems.append(f"{name}: {line!r} is not an exact == pin")
                continue
            found[(dist, extras)] = (line, _version(specs[0][1]))
        return found

    def check_floors(found, group, name):
        floors = {parse_requirement(r)[:2]: (r, _floor(r)) for r in declared.get(group, [])}
        for key, (line, version) in sorted(found.items(), key=lambda item: item[1][0]):
            if key not in floors:
                problems.append(f"{name}: {line!r} is not a [{group}] requirement")
            elif floors[key][1] is not None and version < floors[key][1]:
                problems.append(f"{name}: {line!r} is below the floor {floors[key][0]!r}")
        return floors

    control_pins = pins(control, "requirements-router-control.txt")
    core_floors = check_floors(control_pins, "core", "requirements-router-control.txt")
    missing = sorted(r for key, (r, _f) in core_floors.items() if key not in control_pins)
    problems += [f"requirements-router-control.txt does not pin {r!r}" for r in missing]
    if dev[:1] != ["-r requirements-router-control.txt"]:
        problems.append("requirements-router-control-dev.txt must start with -r requirements-router-control.txt")
    check_floors(pins(dev[1:], "requirements-router-control-dev.txt"), "test",
                 "requirements-router-control-dev.txt")
    return problems


_TABLE = re.compile(r"^\s*\[([^\[\]]+)\]\s*(?:#.*)?$")
_ARRAY = re.compile(r'^\s*"?([A-Za-z0-9_.-]+)"?\s*=\s*(\[.*)$')


def _minimal_reader(text: str) -> dict[str, list[str]]:
    """String arrays of [project].dependencies and [project.optional-dependencies]."""
    groups: dict[str, list[str]] = {}
    table, key, buffer = "", None, ""
    for line in text.splitlines():
        if key is None:
            header, array = _TABLE.match(line), _ARRAY.match(line)
            if header:
                table = header.group(1).strip()
            if not array or not (table == "project.optional-dependencies"
                                 or (table == "project" and array.group(1) == "dependencies")):
                continue
            key, buffer = ("core" if table == "project" else array.group(1)), array.group(2)
        else:
            buffer += "\n" + line
        try:
            value = ast.literal_eval(buffer)
        except SyntaxError:
            continue  # the array continues on the next line
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ValueError(f"pyproject.toml: {key!r} is not an array of strings")
        groups[key], key = value, None
    if key is not None:
        raise ValueError(f"pyproject.toml: array {key!r} is never closed")
    return groups


def read_declared(text: str) -> dict[str, list[str]]:
    groups = _minimal_reader(text)
    if tomllib is not None:
        project = tomllib.loads(text).get("project", {})
        expected = dict(project.get("optional-dependencies", {}))
        if "dependencies" in project:
            expected["core"] = project["dependencies"]
        if groups != expected:
            raise AssertionError(f"minimal reader {groups} != tomllib {expected}")
    return groups


@functools.cache
def _scan() -> tuple[dict[str, str], tuple]:
    sources = repo_sources()
    return sources, tuple(import_sites(sources))


def _read(name: str) -> str:
    return (REPO_ROOT / name).read_text(encoding="utf-8")


def test_every_python_file_has_an_area():
    sources, _ = _scan()
    assert [path for path in sources if area_of(path) is None] == []
    assert {area_of(path) for path in sources} == {"package", "test", "tooling", "space"}


def test_every_third_party_import_is_classified():
    sources, sites = _scan()
    assert classification_problems(sites, local_names(sources)) == []


def test_classification_matches_what_the_repository_imports():
    # The scan must see every classified name: stale entries and an empty scan both fail.
    sources, sites = _scan()
    seen = {site[0] for site in _external(sites, local_names(sources))}
    assert set(DECLARED) | set(TRANSITIVE) | HOST_PROVIDED == seen


def test_pyproject_declares_every_classified_import():
    assert declaration_problems(read_declared(_read("pyproject.toml"))) == []


def test_requirement_files_agree_with_pyproject():
    declared = read_declared(_read("pyproject.toml"))
    assert drift_problems(declared["core"], read_requirements(_read("requirements.txt"))) == []
    assert pin_problems(declared, read_requirements(_read("requirements-router-control.txt")),
                        read_requirements(_read("requirements-router-control-dev.txt"))) == []


PRE_FIX_PYPROJECT = """\
[project]
dependencies = [
  "fastapi>=0.110",
  "uvicorn[standard]>=0.27",
  "httpx>=0.28",
  "pydantic>=2.0",
]

[project.optional-dependencies]
sign = ["szl-receipt-dsse>=0.3.1"]
test = ["pytest", "httpx"]
"""


def test_negative_control_pre_fix_manifests_are_rejected():
    declared = read_declared(PRE_FIX_PYPROJECT)
    assert declaration_problems(declared) == [
        "cryptography missing from [sign] (imported as 'cryptography')",
        "cryptography missing from [test] (imported as 'cryptography')",
        "huggingface-hub missing from [hf-publish] (imported as 'huggingface_hub')",
        "huggingface-hub missing from [test] (imported as 'huggingface_hub')",
        "szl-receipt-dsse missing from [test] (imported as 'szl_receipt')",
        "[test] lacks 'szl-receipt-dsse>=0.3.1' from [sign]",
    ]
    assert drift_problems(declared["core"], ["fastapi>=0.110", "uvicorn[standard]>=0.27"]) == [
        "only in pyproject core: httpx>=0.28", "only in pyproject core: pydantic>=2.0"]


def test_negative_control_unclassified_import_fails_closed():
    sites = import_sites({"szl_router/x.py": "import requests\n"})
    assert classification_problems(sites, {"szl_router", "x"}) == [
        "szl_router/x.py:1 imports unclassified 'requests'; add it to DECLARED, TRANSITIVE or HOST_PROVIDED"]


def test_negative_control_area_rules_are_enforced():
    def problems(path, source):
        return classification_problems(import_sites({path: source}), set(PACKAGES))

    assert problems("router_control/x.py", "def f():\n    import szl_receipt\n") == []
    assert problems("router_control/x.py", "import szl_receipt\n") == [
        "router_control/x.py:1 imports 'szl_receipt' at module scope; package modules "
        "may load only core requirements on import"]
    assert problems("tests/test_x.py", "from starlette.testclient import TestClient\n") == []
    assert len(problems("szl_router/x.py", "from starlette.routing import Route\n")) == 1
    assert len(problems("space/server.py", "import yaml\n")) == 1
    guarded = "try:\n    import szl_backend_hardening\nexcept Exception:\n    pass\n"
    assert problems("szl_router/x.py", guarded) == []
    assert len(problems("szl_router/x.py", "def f():\n    import szl_backend_hardening\n")) == 1
    assert area_of("newdir/tool.py") is None


def test_negative_control_transitive_provider_must_stay_in_core():
    message = "pyyaml (imported as 'yaml') arrives only through 'uvicorn[standard]', which core no longer lists"
    declared = read_declared(PRE_FIX_PYPROJECT)
    assert message not in declaration_problems(declared)
    declared["core"] = [r.replace("uvicorn[standard]", "uvicorn") for r in declared["core"]]
    assert message in declaration_problems(declared)


def test_negative_control_pin_drift_is_rejected():
    declared = {"core": ["fastapi>=0.110", "httpx>=0.28"], "test": ["pytest>=8"]}
    dev = ["-r requirements-router-control.txt", "pytest==7.4.0"]
    assert pin_problems(declared, ["fastapi==0.100.0"], dev) == [
        "requirements-router-control.txt: 'fastapi==0.100.0' is below the floor 'fastapi>=0.110'",
        "requirements-router-control.txt does not pin 'httpx>=0.28'",
        "requirements-router-control-dev.txt: 'pytest==7.4.0' is below the floor 'pytest>=8'",
    ]
    assert pin_problems(declared, ["fastapi>=0.110", "httpx==0.28.1"], ["pytest==8.0.0"]) == [
        "requirements-router-control.txt: 'fastapi>=0.110' is not an exact == pin",
        "requirements-router-control.txt does not pin 'fastapi>=0.110'",
        "requirements-router-control-dev.txt must start with -r requirements-router-control.txt",
    ]


def test_negative_control_reader_fails_closed():
    for text in ('[project]\ndependencies = [\n  "fastapi>=0.110",\n', '[project]\ndependencies = ["x", 1]\n'):
        try:
            _minimal_reader(text)
        except ValueError:
            continue
        raise AssertionError(f"minimal reader accepted {text!r}")
    try:
        parse_requirement("httpx @ https://example.invalid/httpx.whl")
    except ValueError:
        pass
    else:
        raise AssertionError("a URL requirement was accepted")
