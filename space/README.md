---
title: SZL LLM Router
emoji: 🛰️
colorFrom: indigo
colorTo: gray
sdk: docker
app_port: 7860
pinned: false
license: mit
short_description: Public status view of SZL sovereign-first LLM router
---

# SZL LLM Router — public status & concept

A beautiful, honest, investor-grade **public-facing** view of SZL's
**sovereign-first** LLM router. This is a status / marketing surface only — the
`szl-router` codebase and its routing logic stay **private**. No internals, no
scoring heuristics, no provider keys are exposed here.

## The concept

The router prefers compute in strict order:

1. **Own metal (sovereign)** — SZL-owned GPUs, self-hosted. `sovereign: true` only here.
2. **Free hosted tiers** — third-party free inference. `sovereign: false`, `energy_source: grid`.
3. **Paid fallback** — paid hosted models, last resort. `sovereign: false`.

Every response carries an honest **`x_szl_provenance`** stamp: `served_by`,
`sovereign` (true only on own metal), `energy_source` (plain descriptor — no
free-energy claims), and `tier`.

## What's on the page

- An **animated routing diagram** (own metal → free tiers → paid fallback).
- A **sovereign-first tier ladder** and a **provenance field explainer**.
- **Live KPIs** and a **provider fabric** grid fed by the public status endpoints.

## Data source & honest-degrade

The page live-fetches the public router status endpoints:

- `https://a11oy.net/api/a11oy/v1/router/health`
- `https://a11oy.net/api/a11oy/v1/router/models`
- `https://a11oy.net/api/a11oy/v1/router/provenance`

When an endpoint is unreachable (network or cross-origin restriction), that panel
**degrades to a clearly-labeled bundled snapshot** (`assets/snapshot-router-*.json`)
— never to fabricated data and never to a false "all green." The source badge always
states **LIVE**, **PARTIAL**, or **SNAPSHOT**. Auto-refresh ≈ every 15s.

## Honesty / doctrine (v11)

- **Sovereign = own-metal only.** Hosted providers are always `sovereign: false`.
- **No free-energy / joule claims.** `energy_source` is a plain descriptor.
- **Λ (Lambda) = Conjecture 1.** Builds are **SLSA Level 1, honestly stated.**
- **No private routing logic or keys** are exposed — public status + concept only.

## Tech

Static HTML/CSS/JS. Deep-space dark theme, teal/cyan/violet glow, glassmorphism,
responsive, WCAG-contrast, `prefers-reduced-motion` aware. No build step required.

---

## ◇ Part of the SZL Holdings estate

- **Live a11oy console:** [szlholdings-a11oy.hf.space](https://szlholdings-a11oy.hf.space) · [a-11-oy.com](https://a-11-oy.com)
- **Governed-receipt spec + offline verifier:** [governed-receipt-spec](https://github.com/szl-holdings/governed-receipt-spec)
- **More:** [all HF Spaces](https://huggingface.co/SZLHOLDINGS) · [GitHub org](https://github.com/szl-holdings)

<sub>Doctrine v11 · sovereign = own-metal only · no free-energy · Λ = Conjecture 1 · SLSA L1 honest.</sub>
