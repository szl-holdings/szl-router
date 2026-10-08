# Architecture — szl-router

> Doctrine v11 · Λ = **Conjecture 1** · SLSA L1 honest. Honest provenance on every
> answer is non-negotiable.

`szl-router` is SZL's own unified, **OpenAI-compatible** LLM router: one endpoint in
front of declared routes, with owned GPU first and honest provenance on every
response. Unqualified cloud-grid routes and the named paid Moonshot candidate
are currently blocked before transport.

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

| model       | intent              | declared order (qualification gates apply)                |
|-------------|---------------------|-----------------------------------------------------------|
| `szl-large` | general large brain | box_gpu → nvidia_gpu → groq → nvidia_nim → moonshot(Kimi)  |
| `szl-fast`  | low-latency small   | box_gpu → groq → nvidia_nim                                |
| `szl-coder` | coding              | box_gpu → nvidia_gpu → nvidia_nim → groq                   |

This table preserves route declaration order, not current fallback availability.
A configured key does not bypass qualification. Moonshot requires exact model
pricing and strict pre-call spend reservation before dispatch can be enabled.

Direct `provider:upstream_model` syntax (e.g. `groq:llama-3.3-70b-versatile`) is
supported, but unqualified cloud-grid and paid Moonshot overrides fail before
transport. The router was built after studying LiteLLM proxy, OpenRouter, and
lm-sys RouteLLM — leaner, and pinned to SZL doctrine.

## CI

`CI` workflow (`.github/workflows/ci.yml`) runs the test suite. This is a **private**
repository.

---

© 2026 Lutar, Stephen P. — SZL Holdings · Apache-2.0
