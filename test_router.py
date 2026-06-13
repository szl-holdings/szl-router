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


if __name__ == "__main__":
    show_status()
    ok_fast = try_model("szl-fast")
    ok_large = try_model("szl-large")
    if not (ok_fast or ok_large):
        print("RESULT: NO route answered — nothing is actually wired.")
        sys.exit(1)
    print("RESULT: router live and answering through real upstreams.")
