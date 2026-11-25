# szl-router — Domain Leader Study Card r1 (propose-only)

OUROBOROS pass 1 of 10 - 2026-11-25 - CTO web lane (github_app). Propose-only: never merge without owner GO.

## Domain leaders
- RouteLLM: Learning to Route LLMs with Preference Data - Isaac Ong, Amjad Almahairi, Vincent Wu, Wei-Lin Chiang, Tianhao Wu, Joseph E. Gonzalez, M Waleed Kadous, Ion Stoica (UC Berkeley / Anyscale / Canva), arXiv:2406.18665. Formalizes LLM routing as cost-quality trade-off; preference-trained routers achieve >2x cost savings; open-source routers + preference data.
- FrugalGPT line (Chen et al., Stanford): LLM cascade alternative (sequential escalation).
- LLM-BLENDER (Jiang et al.): ensemble alternative (higher inference cost).

## Moat to absorb (estate-specific)
- Routing quality disclosure AS a receipt: carry router-score, candidate routes, expected cost delta per decision (third-party auditable economics on top of the existing verifiable routing receipt).
- RouteLLM open-data play -> receipt-annotated routing-decision corpus (synthetic doctrine-valid subset), independently replayable.
- APGR-style cost-vs-quality curve as the flagship reporting format; never single-score.

## Recommended upgrades (owner-gated)
1. README 'Routing lineage' section citing the above with sovereign-first framing (route-receipt carriage on top of the cost-quality literature).
2. Per-query receipt enrichment: router_score, routes_considered[], expected_cost_delta_vs_baseline.
3. Replay-mode CLI re-scoring a shipped decision corpus against the live router (drift measurement).
4. CI job: APGR-style curve on a fixed synthetic lane, SVG receipt card via estate-cards weaver.

Sources: arXiv:2406.18665 (https://arxiv.org/pdf/2406.18665v2).