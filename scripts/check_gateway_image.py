"""Offline gateway image smoke check, supplied on stdin by CI.

Run inside the built image with no network access, a read-only root filesystem,
and no provider credentials. No completion or provider endpoint is requested.
"""

import json
import os
from importlib.metadata import version
from importlib.resources import files
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import urlopen


def main() -> None:
    assert os.getuid() != 0, "gateway must run as a non-root user"
    assert version("szl-router"), "installed router distribution missing"
    assert version("szl-receipt") == "0.1.0", "receipt release changed"
    # Both packages and frontend assets are part of the declared wheel contract.
    for asset in ("index.html", "szl/szl-tokens.css", "szl/SOURCE.json"):
        assert files("router_control").joinpath("static", asset).is_file(), asset

    environment = dict(os.environ, SZL_RECEIPT_EPHEMERAL="0", PORT="8000")
    process = subprocess.Popen([sys.executable, "-m", "szl_router.app"], env=environment)
    try:
        deadline = time.monotonic() + 20
        while True:
            assert process.poll() is None, "gateway exited before liveness check"
            try:
                with urlopen("http://127.0.0.1:8000/healthz", timeout=1) as response:
                    health = json.load(response)
                break
            except (URLError, TimeoutError):
                if time.monotonic() >= deadline:
                    raise RuntimeError("gateway did not become reachable")
                time.sleep(0.1)
        assert health["ok"] is True and health["service"] == "szl-router"
        with urlopen("http://127.0.0.1:8000/v1/receipt/pubkey", timeout=2) as response:
            receipt = json.load(response)
        assert receipt["library_available"] is True
        assert receipt["public_key_pem"] is None
        assert receipt["ephemeral"] is False
        print("gateway image smoke passed: non-root boot, package assets, keyless receipt library")
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


if __name__ == "__main__":
    main()
