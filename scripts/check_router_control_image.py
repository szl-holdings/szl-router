"""Exercise the built control image without exposing it to a network."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys


IN_CONTAINER_CHECK = r"""
import json
import sys
import time
import urllib.error
import urllib.request

expected = sys.argv[1]
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
base = "http://127.0.0.1:7860"

for attempt in range(20):
    try:
        with opener.open(base + "/deployment.json", timeout=2) as response:
            assert response.status == 200
            assert response.headers.get("Cache-Control") == "no-store"
            deployment = json.load(response)
        break
    except urllib.error.URLError:
        if attempt == 19:
            raise
        time.sleep(1)

assert deployment["source_revision"] == expected, deployment
assert deployment["egress_enabled"] is False, deployment
assert deployment["inference"]["ready_for_requests"] is False, deployment
assert deployment["hub_publication"].startswith("UNAVAILABLE"), deployment

try:
    opener.open(base + "/readyz/inference", timeout=2)
except urllib.error.HTTPError as response:
    assert response.code == 503, response.code
    readiness = json.load(response)
    assert readiness["ready_for_requests"] is False, readiness
    assert readiness["inference_witness"] == "UNAVAILABLE", readiness
else:
    raise AssertionError("unconfigured inference unexpectedly admitted")

print(json.dumps({"source_revision": expected, "deployment": "PASS",
                  "default_egress": "DENIED", "inference": "UNAVAILABLE"},
                 sort_keys=True))
"""


def run(*args: str) -> str:
    result = subprocess.run(
        args,
        check=True,
        capture_output=True,
        text=True,
        timeout=90,
    )
    return result.stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--source-revision", required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{40}", args.source_revision):
        parser.error("source revision must be a full Git commit SHA")

    config = json.loads(run("docker", "image", "inspect", args.image, "--format", "{{json .Config}}"))
    if config["User"] != "10001:10001":
        raise AssertionError("control image must run as the non-root user")
    if config["Labels"].get("org.opencontainers.image.revision") != args.source_revision:
        raise AssertionError("image source label does not match the selected commit")
    environment = set(config["Env"])
    if f"SOURCE_REVISION={args.source_revision}" not in environment:
        raise AssertionError("image does not embed the selected source revision")
    if "SZL_ROUTER_ENABLE_EGRESS=0" not in environment:
        raise AssertionError("image does not default to denied egress")

    container = run("docker", "run", "--detach", "--rm", "--network", "none", args.image)
    try:
        result = run("docker", "exec", container, "python", "-c", IN_CONTAINER_CHECK, args.source_revision)
        print(result)
    finally:
        subprocess.run(("docker", "stop", container), capture_output=True, text=True, timeout=20, check=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
