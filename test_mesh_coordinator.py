"""Pure, offline, deterministic tests for the SZL Mesh Coordinator.

NO network: every test drives the picker / gating / provenance / fail-loud logic
directly, or monkeypatches the proxy transport with an in-memory fake. Exits
non-zero on any failure so CI can gate the load-balancer against silent regression.

Run: python3 test_mesh_coordinator.py
"""

from __future__ import annotations

import json
import sys

sys.path.insert(0, "szl_router")
import mesh_coordinator as mc  # noqa: E402


FAILED = 0


def check(cond: bool, msg: str) -> None:
    global FAILED
    if cond:
        print("  OK  " + msg)
    else:
        FAILED += 1
        print("  BAD " + msg)


def _workers():
    return [
        mc.Worker(name="laptop", base_url="http://100.125.77.31:11434/v1", sovereign=True),
        mc.Worker(name="omen", base_url="http://100.70.130.45:11434/v1", sovereign=True),
        mc.Worker(name="chaski", base_url="http://100.76.58.50:11434/v1", sovereign=False,
                  kind="tailnet-gpu", energy_source="grid"),
    ]


def _failover():
    return mc.Worker(name="nvidia-nim", base_url="https://integrate.api.nvidia.com/v1",
                     sovereign=False, kind="hosted-inference", energy_source="grid")


def test_normalize_base():
    print("== _normalize_base ==")
    check(mc._normalize_base("http://1.2.3.4:11434") == "http://1.2.3.4:11434/v1",
          "bare host gets /v1 appended")
    check(mc._normalize_base("http://1.2.3.4:11434/v1") == "http://1.2.3.4:11434/v1",
          "existing /v1 path left as-is")
    check(mc._normalize_base("") == "", "empty stays empty (never half-armed)")
    print()


def test_least_connections_and_gating():
    print("== least-connections picker + reachability gating ==")
    c = mc.MeshCoordinator(_workers(), failover=_failover())
    # mark reachability by hand (simulating a probe result) — NO network
    by = {w.name: w for w in c.workers}
    by["laptop"].reachable = True
    by["omen"].reachable = True
    by["chaski"].reachable = True   # reachable but sovereign=False -> still never counts as sovereign
    c.failover.reachable = True

    # equal inflight -> both sovereign in pool, tier mesh-live (redundancy)
    order, tier = c.pick_order()
    names = [w.name for w in order]
    check(set(n for n in names) <= {"laptop", "omen"},
          "picker only returns SOVEREIGN reachable nodes (chaski excluded though reachable)")
    check(tier == mc.TIER_MESH_LIVE, "two reachable sovereigns -> mesh-live")

    # least-connections: load up omen, laptop must be picked first
    by["omen"].inflight = 5
    by["laptop"].inflight = 0
    order, _ = c.pick_order()
    check(order[0].name == "laptop", "least-inflight node (laptop) picked first")
    by["laptop"].inflight = 9
    by["omen"].inflight = 1
    order, _ = c.pick_order()
    check(order[0].name == "omen", "after load shifts, least-inflight (omen) picked first")

    # down node is NEVER picked
    by["omen"].reachable = False
    by["laptop"].reachable = True
    by["laptop"].inflight = 0
    order, tier = c.pick_order()
    check([w.name for w in order] == ["laptop"], "unreachable sovereign (omen) is never picked")
    check(tier == mc.TIER_MESH_DEGRADED, "single reachable sovereign -> mesh-degraded (honest reduced redundancy)")

    # no sovereign reachable -> cloud failover tier
    by["laptop"].reachable = False
    order, tier = c.pick_order()
    check([w.name for w in order] == ["nvidia-nim"], "no sovereign up -> cloud failover picked")
    check(tier == mc.TIER_HF_FAILOVER, "no sovereign up -> hf-failover tier")

    # nothing reachable at all -> empty (caller fails loud)
    c.failover.reachable = False
    order, tier = c.pick_order()
    check(order == [], "nothing reachable -> empty order (will fail loud)")
    print()


def test_round_robin_tie_break():
    print("== round-robin tie-break on equal inflight ==")
    c = mc.MeshCoordinator(_workers(), failover=_failover())
    for w in c.workers:
        w.reachable = w.sovereign  # laptop + omen reachable
        w.inflight = 0
    firsts = set()
    for _ in range(8):
        order, _ = c.pick_order()
        firsts.add(order[0].name)
    check(firsts == {"laptop", "omen"},
          "equal-load reachable sovereigns share the front slot (both seen): %s" % sorted(firsts))
    print()


def test_sovereign_never_inferred():
    print("== sovereign is owned-metal only, never inferred from reachability ==")
    c = mc.MeshCoordinator(_workers(), failover=_failover())
    chaski = next(w for w in c.workers if w.name == "chaski")
    chaski.reachable = True
    check(chaski.sovereign is False, "chaski reachable but sovereign stays False (not owned metal)")
    nim = c.failover
    nim.reachable = True
    check(nim.sovereign is False, "cloud failover reachable but sovereign stays False")
    print()


def test_provenance_injection():
    print("== honest provenance injection (no fused-VRAM claim) ==")
    w = mc.Worker(name="omen", base_url="http://100.70.130.45:11434/v1", sovereign=True)
    body = json.dumps({"choices": [{"message": {"content": "hi"}}]}).encode()
    out = mc._inject_provenance(body, w, mc.TIER_MESH_LIVE,
                               [mc.Attempt("omen", w.base_url, ok=True, status=200)])
    obj = json.loads(out)
    prov = obj["x_szl_provenance"]
    check(prov["served_by"] == "omen", "served_by stamped to the chosen worker")
    check(prov["sovereign"] is True, "sovereign True for owned metal")
    check(prov["serve_tier"] == mc.TIER_MESH_LIVE, "serve_tier recorded in body")
    check("VRAM not fused" in prov["placement"], "placement explicitly states VRAM NOT fused")
    check(len(prov["attempts"]) == 1 and prov["attempts"][0]["ok"] is True, "attempts trail present")

    # cloud failover answer must NOT be labeled sovereign
    f = mc.Worker(name="nvidia-nim", base_url="https://integrate.api.nvidia.com/v1", sovereign=False)
    out2 = mc._inject_provenance(body, f, mc.TIER_HF_FAILOVER,
                                [mc.Attempt("nvidia-nim", f.base_url, ok=True, status=200)])
    check(json.loads(out2)["x_szl_provenance"]["sovereign"] is False,
          "cloud failover answer labeled sovereign=False")

    # non-JSON / streaming body passes through untouched
    streamed = b"data: {\"delta\": 1}\n\n"
    check(mc._inject_provenance(streamed, w, mc.TIER_MESH_LIVE, []) == streamed,
          "non-JSON stream body passed through unchanged (headers still carry provenance)")
    print()


def test_fail_loud_no_fabrication():
    print("== fail loud: no reachable worker -> CoordinatorError, never fabricate ==")
    c = mc.MeshCoordinator(_workers(), failover=_failover())
    # force everything unreachable WITHOUT touching the network: stub the probe
    c.refresh_reachability = lambda force=False: None  # type: ignore
    for w in c.workers:
        w.reachable = False
    c.failover.reachable = False
    raised = False
    try:
        c.proxy("POST", "/chat/completions", b"{}", {})
    except mc.CoordinatorError as e:
        raised = True
        check("no reachable worker" in str(e), "error message names the real cause")
    check(raised, "proxy raises CoordinatorError when nothing is reachable (no fabricated answer)")
    print()


def test_proxy_picks_reachable_and_failover():
    print("== proxy routes to reachable sovereign, fails over honestly (fake transport) ==")
    c = mc.MeshCoordinator(_workers(), failover=_failover())
    c.refresh_reachability = lambda force=False: None  # type: ignore

    # Fake http transport: omen 200, laptop 500. Records who was called.
    calls = []

    class FakeResp:
        def __init__(self, status, body):
            self.status = status
            self._body = body
        def read(self):
            return self._body
        def getheaders(self):
            return [("Content-Type", "application/json")]

    class FakeConn:
        def __init__(self, host, port, timeout=None):
            self.host = host
        def request(self, method, path, body=None, headers=None):
            calls.append(self.host)
            self._reply = (FakeResp(200, b'{"choices":[{"message":{"content":"ok"}}]}')
                           if self.host == "100.70.130.45"
                           else FakeResp(500, b'{"error":"boom"}'))
        def getresponse(self):
            return self._reply
        def close(self):
            pass

    orig = mc.http.client.HTTPConnection
    mc.http.client.HTTPConnection = FakeConn  # type: ignore
    try:
        # omen reachable + healthy, laptop reachable but 500s
        by = {w.name: w for w in c.workers}
        by["omen"].reachable = True
        by["laptop"].reachable = True
        by["omen"].inflight = 0
        by["laptop"].inflight = 0
        status, headers, raw, attempts = c.proxy("POST", "/chat/completions", b"{}", {})
        obj = json.loads(raw)
        check(status == 200, "got a 200 from a reachable healthy worker")
        check(obj["x_szl_provenance"]["served_by"] == "omen", "served_by is the worker that actually answered")
        check(headers["x-szl-serve-tier"] in (mc.TIER_MESH_LIVE, mc.TIER_MESH_DEGRADED),
              "serve-tier header stamped")
        check(headers["x-szl-sovereign"] == "true", "sovereign header true for owned metal")
        # inflight counters returned to baseline (no leak)
        check(by["omen"].inflight == 0 and by["laptop"].inflight == 0,
              "inflight counters released after request (no leak)")
    finally:
        mc.http.client.HTTPConnection = orig  # type: ignore
    print()


def test_chaski_armed_as_real_sovereign_worker() -> None:
    """PowerD4: chaski is wired as a REAL dispatch target by default -- the 2nd live
    sovereign GPU. Was previously empty-default + sovereign=False so the picker never
    selected it (idle horsepower). Now: armed default URL, sovereign=True, hosts the
    larger brain (qwen2.5:32b) -> szl-large, with an honest PENDING_EXPORTER joule hint.
    The picker gating logic is UNCHANGED -- it still honors sovereign=False (see
    test_least_connections_and_gating); only chaski's default classification changed,
    matching the live mesh + szl_energy_operator (which meters chaski as a peer node)."""
    print("test_chaski_armed_as_real_sovereign_worker")
    import os
    for k in [x for x in os.environ if x.startswith("SZL_MESH_")]:
        del os.environ[k]
    workers, _failover = mc.build_workers_from_env()
    by = {w.name: w for w in workers}
    check("chaski" in by, "chaski is armed in the default registry")
    check(by["chaski"].sovereign is True, "chaski sovereign=True (peer sovereign GPU, matches live mesh)")
    check(by["chaski"].base_url == "http://100.102.173.88:11434/v1",
          "chaski default base = live tailnet IP")
    check(by["chaski"].gen_model == "qwen2.5:32b", "chaski hosts the larger brain qwen2.5:32b")
    check(by["chaski"].serve_role == "szl-large", "chaski anchors szl-large (big brain)")
    check(by["chaski"].joule_label_hint == "PENDING_EXPORTER",
          "chaski joule hint = PENDING_EXPORTER (honest; never a fabricated joule)")
    # picker now selects chaski when reachable
    c = mc.MeshCoordinator(workers, failover=None)
    for w in workers:
        w.reachable = (w.name in ("laptop", "chaski")); w.inflight = 0
    order, tier = c.pick_order()
    names = [w.name for w in order]
    check("chaski" in names and "laptop" in names, "chaski IS dispatched alongside laptop (THE FIX)")
    check(tier == mc.TIER_MESH_LIVE, "two reachable sovereigns -> mesh-live (real redundancy)")
    by["laptop"].inflight = 5
    check(c.pick_order()[0][0].name == "chaski", "least-connections picks idle chaski over busy laptop")
    # provenance carries the documentation fields
    out = mc._inject_provenance(json.dumps({"a": 1}).encode(), by["chaski"], mc.TIER_MESH_LIVE, [])
    prov = json.loads(out)["x_szl_provenance"]
    check(prov["node_gen_model"] == "qwen2.5:32b" and prov["serve_role"] == "szl-large"
          and prov["joule_label_hint"] == "PENDING_EXPORTER",
          "provenance surfaces chaski model+tier+honest joule hint")
    check("VRAM not fused" in prov["placement"], "placement still asserts VRAM not fused")
    print()


if __name__ == "__main__":
    test_normalize_base()
    test_least_connections_and_gating()
    test_round_robin_tie_break()
    test_sovereign_never_inferred()
    test_provenance_injection()
    test_fail_loud_no_fabrication()
    test_proxy_picks_reachable_and_failover()
    test_chaski_armed_as_real_sovereign_worker()
    if FAILED:
        print("\nRESULT: %d check(s) FAILED — coordinator logic regressed." % FAILED)
        sys.exit(1)
    print("\nRESULT: all coordinator checks passed (offline, deterministic).")
