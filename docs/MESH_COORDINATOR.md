# SZL Mesh Coordinator — real OpenAI-/v1 load balancer across the sovereign GPU mesh

`szl_router/mesh_coordinator.py` is a small, **runnable**, zero-dependency
(pure-stdlib) OpenAI-`/v1`-compatible **load balancer** that fronts the *separate*
sovereign GPU workers (traveling RTX 5050 laptop, always-on OMEN RTX 4060 Ti,
chaski) and spreads requests across the ones a **live reachability probe** says are
up — with honest provenance and an honest cloud failover.

It is the runtime that lets `A11OY_MODEL_BASE_URL` point at a **real balancer**
instead of one laptop.

## What it does (and does NOT do)

- **Picks only reachable workers.** Each request first refreshes reachability
  (reuses the box's real `szl_backend_hardening.probe_fabric_pool()` when that
  module is importable on the host — same data the box's `/compute-pool-hardened`
  serves; otherwise a self-contained stdlib TCP probe with the identical honesty
  contract). A down node is **never picked** and is **never claimed to have served**.
- **Spreads load** across reachable **sovereign** workers by **least-inflight**
  (round-robin tie-break) — the generalized `examples/forge-mesh-router.py`
  least-connections pattern, lifted from "two ports on one box" to "N tailnet
  sovereign nodes".
- **Labels every response** honestly:
  - response header `x-szl-serve-tier`: `mesh-live` (≥2 reachable sovereigns, real
    redundancy) | `mesh-degraded` (exactly one sovereign reachable — honest reduced
    redundancy) | `hf-failover` (no sovereign up; served by the cloud tier).
  - response headers `x-szl-served-by`, `x-szl-sovereign`.
  - JSON body `x_szl_provenance`: `served_by`, `base_url`, `sovereign` (true **only**
    for owned metal — never inferred from reachability, never set for the cloud
    tier), `energy_source`, `serve_tier`, `placement`, and the full `attempts` trail.
- **Fails loud.** If no sovereign worker is reachable and no cloud failover is
  armed, it returns **502** with the full attempt trail and `served_by: null`. It
  **never fabricates** an answer.
- **NEVER claims fused/combined VRAM.** This is **horizontal placement +
  load-balance across separate workers**. The `placement` field literally states
  "separate worker; VRAM not fused". Memory does not merge across the network.
- **No secrets on disk.** Per-worker upstream keys (if any) come only from env
  (`SZL_MESH_<NAME>_TOKEN`). Nothing secret is logged.

## Run it on the box (the always-on host — NOT the traveling laptop)

```bash
# sovereign workers (owned metal). Defaults are the known tailnet endpoints;
# override per box. A worker whose URL is empty is simply dropped (never half-armed).
export SZL_MESH_LAPTOP_BASE_URL=http://100.125.77.31:11434/v1   # traveling RTX 5050 (default)
export SZL_MESH_OMEN_BASE_URL=http://100.70.130.45:11434/v1     # always-on OMEN 4060 Ti (default)
# export SZL_MESH_CHASKI_BASE_URL=http://100.76.58.50:11434/v1  # tailnet node — sovereign=FALSE (not owned metal)

# cloud failover (sovereign=false). Default = NVIDIA NIM. Token from env, never hardcoded.
export SZL_MESH_FAILOVER_BASE_URL=https://integrate.api.nvidia.com/v1
export SZL_MESH_NVIDIA_NIM_TOKEN=<nim-key>     # optional; only if the failover needs auth

# run the balancer (stdlib only — no pip install needed)
python3 -m szl_router.mesh_coordinator --host 0.0.0.0 --port 11500
# or: python3 szl_router/mesh_coordinator.py --port 11500
```

Check it:

```bash
curl -s localhost:11500/healthz
curl -s localhost:11500/coordinator/status | python3 -m json.tool   # honest live reachability + next pick
curl -s localhost:11500/v1/chat/completions \
  -H 'content-type: application/json' \
  -d '{"model":"llama3.1:8b","messages":[{"role":"user","content":"ping"}]}' -i   # see x-szl-serve-tier header
```

## Wire it into `szl-router` (the value-change)

`szl-router`'s `box_gpu` provider resolves **one** base URL from
`A11OY_MODEL_BASE_URL`. Today that points at a single node (the laptop). Point it at
the **coordinator** instead, and the sequential router now front-ends a real
balancer — `szl-large`/`szl-fast`/`szl-coder` spread across whatever sovereign GPUs
are live, with the coordinator's honest serve-tier and provenance flowing through:

```bash
# on the host running szl-router (the always-on box):
export A11OY_MODEL_BASE_URL=http://localhost:11500/v1   # -> the coordinator, NOT one laptop
export A11OY_GPU_TOKEN=<any-nonempty>                   # parity with existing box_gpu arming
```

That single env edit is the cutover. No `szl-router` code change is required — the
coordinator is OpenAI-`/v1`-compatible and `box_gpu` already reads its URL from env.

## Honesty / doctrine

- `reachable` = a real probe **this sweep** only; a timeout/refusal is honest
  unreachable. `sovereign` = a property of owned hardware, passed through, never
  inferred. `chaski` is a tailnet node but **not owned metal**, so it is
  `sovereign=false`. The cloud failover is `sovereign=false`.
- Horizontal placement + load-balance only. **VRAM is never fused/combined.**
- Fail loud over fake green: 502 + attempts when nothing is reachable; never a
  fabricated answer; never a claim that a down node served.
