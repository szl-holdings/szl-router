# szl-router domain leader study r1 (propose-only)

Date: 2026-11-22. Method: web-verified 2026 gateway landscape + governing-seat synthesis (Kimi nonce W26/W27-P3-ROUTER-BRIEF). This card is documentation only; it changes no code, CI, config, or existing PR status. All absorb actions are FUTURE PROPOSALS (non-authorizing); implementation and merges remain separately reviewed and gated.

## Domain: LLM gateway / router

## Leaders studied (web-verified 2026-11-22)

1. **LiteLLM** (open-source, self-host, 0% markup). Study basis: 2026 gateway comparisons (LiteLLM vs Portkey vs OpenRouter). Moat: per-key/route budget controls, spend tracking, self-host ownership.
2. **Portkey** (enterprise SaaS). Moat: governance + observability: request tracing, prompt management, RBAC, audit trails first-class; strongest out-of-box compliance story (per 2026 TrueFoundry comparison).
3. **OpenRouter** (managed multi-provider). Moat: instant multi-provider breadth, thin dashboard analytics. Its public pricing states inference is billed at provider list price, with platform fees charged when credits are purchased rather than as a per-request markup.

## Absorb actions (FUTURE PROPOSALS, non-authorizing)

1. (LiteLLM) Atomic pre-dispatch spend checks/reservations for per-upstream and per-key caps; refuse on exceeded cap or unavailable accounting; reconcile actual usage; attach estimated/actual cost, currency, pricing source, cap outcome to the estate receipt; unknown cost is explicit, never zero.
2. (Portkey) Per-request trace/audit record: route+policy decisions, upstream attempts, timestamps, pseudonymous key id, spend outcome, response validation, denial reason codes; bind identifier/hash to the estate receipt chain; redact secrets; no success receipt unless durable audit linkage succeeds.
3. (OpenRouter, honesty-doctrine-adjusted) One status surface with per-upstream health scores from windowed latency, transport errors, empty-answer rate, malformed-response rate, citation-validation failures; publish inputs, sample size, window, freshness; explicit unknown/stale/degraded states; never infer healthy from reachability alone; refuse with receipted reason when no upstream satisfies the policy.
4. (Baseline note, read-only) Open PRs #162/#163/#164 already encode the fail-closed validation family (Claude Messages/Perplexity protocol violations, empty upstream answers, malformed falsey Anthropic citations). Reference only; no status or validation claim is made here.

## Do not do (per governing seat)

Do not merge, bypass the signature wall, or claim implementation beyond this card.
