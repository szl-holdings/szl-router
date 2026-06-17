# Architecture — szl-router

> Doctrine v11 · Λ = **Conjecture 1** · SLSA L1 honest. Honest provenance on every
> answer is non-negotiable.

`szl-router` is SZL's own unified, **OpenAI-compatible** LLM router: one endpoint in
front of many brains — owned GPU first, then free grid tiers, then a paid fallback —
with honest provenance on every response.

## Design principles

- **Sovereign-first.** Routes try owned metal (`box_gpu`, `nvidia_gpu`) before any
  third-party cloud, before any paid tier.
- **Honest labels.** Every response carries `x_szl_provenance` with `served_by`,
  `sovereign` (true *only* for hardware we own), `energy_source`, `tier`, and the full
  `attempts` trail. A free / grid tier is never labelled sovereign.
- **No secrets in the repo.** All upstream keys come from the environment; nothing
  secret is written to disk or logged.
- **No half-state.** A logical model either resolves to a working upstream or the call
  fails loud (HTTP 502) with the complete attempt trail. Unavailable providers
  (missing key/url) are skipped — never faked.

## Repository layout

```
szl-router/
├── szl_router/             Router package (resolution, provenance, mesh coordinator).
├── config.example.yaml     Example configuration (logical models + fallback chains).
├── docs/                   Design + usage docs.
├── examples/               Runnable examples.
├── test_router.py
├── test_embed_cache_pool.py
├── test_mesh_coordinator.py
└── requirements.txt
```

## Logical models & fallback

| model       | intent              | fallback order (sovereign → free → paid)                  |
|-------------|---------------------|-----------------------------------------------------------|
| `szl-large` | general large brain | box_gpu → nvidia_gpu → groq → nvidia_nim → moonshot(Kimi)  |
| `szl-fast`  | low-latency small   | box_gpu → groq → nvidia_nim                                |
| `szl-coder` | coding              | box_gpu → nvidia_gpu → nvidia_nim → groq                   |

Direct `provider:upstream_model` calls (e.g. `groq:llama-3.3-70b-versatile`) are also
supported. The router was built after studying LiteLLM proxy, OpenRouter, and
lm-sys RouteLLM — leaner, and pinned to SZL doctrine.

## CI

`CI` workflow (`.github/workflows/ci.yml`) runs the test suite. This is a **private**
repository.

---

© 2026 Lutar, Stephen P. — SZL Holdings · Apache-2.0
