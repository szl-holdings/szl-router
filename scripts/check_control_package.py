# SPDX-License-Identifier: Apache-2.0
"""Install a candidate wheel away from source and exercise its control surface."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="router-installed-") as temp:
        target = Path(temp) / "installed"
        subprocess.run([sys.executable, "-m", "pip", "install", "--no-deps",
                        "--target", str(target), str(args.wheel.resolve())], check=True,
                       stdout=subprocess.DEVNULL)
        code = '''
import json, pathlib, sys
sys.path.insert(0, sys.argv[1])
from fastapi.testclient import TestClient
from router_control.app import app, STATIC
from router_control.verification import main
client = TestClient(app)
expected = {"/": "text/html", "/static/app.js": "javascript", "/static/styles.css": "text/css", "/version": "application/json", "/api/source": "application/json"}
for route, mime in expected.items():
    response = client.get(route)
    if response.status_code != 200 or mime not in response.headers["content-type"]:
        raise RuntimeError("installed artifact route failed: " + route)
if "router_control/local_store.py" not in client.get("/api/source").json()["controlled_files"]:
    raise RuntimeError("installed byte-admission source missing from source receipt")
if not STATIC.is_relative_to(pathlib.Path(sys.argv[1])):
    raise RuntimeError("source checkout masked installed package")
if "demo" in [p.name for p in pathlib.Path(sys.argv[1]).iterdir()]:
    raise RuntimeError("fixture demo included in production wheel")
if main(["-"]) != 2:
    raise RuntimeError("installed verifier invalid-input exit code")
print(json.dumps({"status":"PASS","scope":"INSTALLED_WHEEL_IMPORT_STATIC_API_AND_CLI"}))
'''
        result = subprocess.run([sys.executable, "-I", "-c", code, str(target)],
                                cwd=temp, input=b"invalid", capture_output=True, check=True)
        print(result.stdout.decode(), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
