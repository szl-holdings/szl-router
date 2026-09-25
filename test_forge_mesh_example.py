"""The documented example must parse and import without starting a listener."""
import runpy
from pathlib import Path


def test_example_loads_and_routes_without_starting_server():
    module = runpy.run_path(str(Path(__file__).parent / "examples" / "forge-mesh-router.py"), run_name="example_test")
    assert module["pick_backend"]("/api/embed")[0] == module["IGPU"]
    assert module["pick_backend"]("/v1/chat/completions")[0] == module["DGPU"]
    module["inflight"][module["DGPU"]] = 5
    assert module["pick_backend"]("/v1/chat/completions")[0] == module["IGPU"]
