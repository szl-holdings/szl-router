"""Live smoke for SZL Router core. Hits whatever providers are armed via env.

Run: python3 test_router.py
Exits non-zero if no route can answer szl-fast (i.e. nothing is actually wired).
"""
import json
import sys

sys.path.insert(0, "szl_router")
import core  # noqa: E402


def show_status():
    st = core.status()
    print("== provider status ==")
    for p in st["providers"]:
        flag = "ARMED " if p["available"] else "off   "
        sov = "SOVEREIGN" if p["sovereign"] else p["tier"]
        print(f"  {flag} {p['provider']:<12} {sov:<10} {p['energy_source']}")
    print()


def try_model(name):
    print(f"== chat({name}) ==")
    try:
        r = core.chat(name, [{"role": "user", "content": "Reply with exactly: ROUTER_OK"}],
                      max_tokens=10, timeout=45)
    except core.RouterError as e:
        print("  FAILED:", e)
        for a in e.attempts:
            print("    -", a.provider, a.upstream_model, "ok=" + str(a.ok), a.error or "")
        return False
    prov = r["x_szl_provenance"]
    msg = r["choices"][0]["message"]["content"].strip()
    print(f"  served_by   : {prov['served_by']}")
    print(f"  sovereign   : {prov['sovereign']}   tier: {prov['tier']}   energy: {prov['energy_source']}")
    print(f"  reply       : {msg!r}")
    print(f"  attempts    : {len(prov['attempts'])}")
    for a in prov["attempts"]:
        print(f"     - {a['provider']}:{a['upstream_model']} ok={a['ok']} {a.get('error') or ''}")
    print()
    return True


def test_harvest_classifier():
    """Pure, offline, deterministic check of the wasted-energy posture mapping.

    No network: validates _classify_harvest + the offline fabric overlay so the
    HARVESTING logic can't silently regress. Exits non-zero on any mismatch."""
    print("== harvest classifier (offline, pure) ==")
    cases = [
        # (price_now, next_min, ren_pct) -> (posture, wasted, window_ahead)
        ((-1.11, -45.87, 80.0), ("negative-price", True, True)),
        ((20.0, 10.0, 107.4), ("curtailed-renewable", True, False)),
        ((20.0, -5.0, 50.0), ("cheap", False, True)),
        ((150.0, 120.0, 40.0), ("expensive", False, False)),
        ((55.0, 60.0, 60.0), ("normal", False, False)),
        ((None, None, None), ("unknown", False, False)),
    ]
    failed = 0
    for args, want in cases:
        got = core._classify_harvest(*args)
        ok = got == want
        failed += 0 if ok else 1
        print(f"  {'OK ' if ok else 'BAD'} {args} -> {got}" + ("" if ok else f"  want {want}"))

    # offline overlay must never fabricate or flip sovereign
    fab = core.fabric_status(include_harvest=True, allow_network=False)
    inv = []
    if fab["harvest"]["status"] != "not-probed":
        inv.append("offline harvest not 'not-probed'")
    if fab["harvesting"] is not False:
        inv.append("offline harvesting must be False")
    if fab["energy_window"] != "unknown":
        inv.append("offline energy_window must be 'unknown'")
    if fab["harvest"].get("sovereign") is not False:
        inv.append("harvest.sovereign must be False")
    for m in inv:
        failed += 1
        print("  BAD invariant:", m)
    if not inv:
        print("  OK  offline fabric overlay honest (not-probed, no harvest, sovereign untouched)")
    print()
    return failed == 0


def show_harvest_live():
    """Best-effort live harvest probe (non-fatal — feeds may be unreachable)."""
    print("== harvest (live, real public grid feeds) ==")
    try:
        h = core.harvest_status(allow_network=True, force=True)
    except Exception as e:  # noqa: BLE001
        print("  (probe error, non-fatal):", e)
        print()
        return
    print(f"  status        : {h['status']}")
    print(f"  posture       : {h['grid_price_posture']}   wasted_available={h['wasted_energy_available']}")
    print(f"  joules_label  : {h['joules_label']}   sovereign={h['sovereign']}")
    for k, sig in h.get("signals", {}).items():
        print(f"  {k:<16}: {sig.get('status')}  " +
              (f"price_now={sig.get('price_now')} next_min={sig.get('next_min')}"
               if k == "wholesale_price" else
               f"ren_share={sig.get('renewable_share_pct')}%" if k == "renewable_share" else
               f"index={sig.get('index')} gco2={sig.get('gco2_per_kwh')}"))
    print()


if __name__ == "__main__":
    show_status()
    classifier_ok = test_harvest_classifier()
    show_harvest_live()
    if not classifier_ok:
        print("RESULT: harvest classifier FAILED — posture mapping regressed.")
        sys.exit(1)
    ok_fast = try_model("szl-fast")
    ok_large = try_model("szl-large")
    if not (ok_fast or ok_large):
        print("RESULT: NO route answered — nothing is actually wired.")
        sys.exit(1)
    print("RESULT: router live and answering through real upstreams.")
