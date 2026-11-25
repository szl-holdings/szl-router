# Leader-Study Upgrade Plan - W27 loop-8 - 2026-11-23

Status: PROPOSE-ONLY DRAFT - DO NOT MERGE. Owner-gated: merging requires governing approval and the estate signature-wall owner unlock.

Repo: szl-router - FLAGSHIP: sovereign-first OpenAI-compatible LLM router with verifiable routing receipts; source of truth for SZLHOLDINGS/llm-router-live

Frame: OUROBOROS loop-8 leader study. Governing seat: Kimi kimi-k3.1-pro (json_ok, think off). Advisory seats: Astra/Sol/Claude community rails (provenance unverified, advisory only). Leader evidence web-verified 2026-11-23.

## Leader moats to absorb
- OpenRouter: Managed LLM gateway at Cloudflare edge; Auto Router ranks providers by aggregate community spend over trailing 7-day window per task type; provider-level routing deprioritizes unreliable providers; failed requests not billed. Moat: Managed edge operations plus provider-selection and reliability signals, with billing aligned to successful requests.
- LiteLLM: Self-hosted proxy on own infrastructure using Docker and PostgreSQL; free core; SSO, RBAC, and audit logs in Enterprise; managed-versus-self-host crossover is arithmetic on the 5.5 percent credit fee. Moat: Self-hosted deployment and operational control, with enterprise identity, authorization, and audit capabilities.
- RouteLLM: arXiv 2406.18665, UC Berkeley/Anyscale: router models trained on human preference data dynamically select stronger versus weaker LLM per query, with greater than 2x cost reduction without quality loss. Moat: Learned per-query model selection using human preference data to optimize the cost-quality tradeoff.

## Honest gaps (grounded in repo facts)
- The facts describe provider failover and gated external candidates, but do not state that szl-router ranks providers using aggregate community spend over a trailing 7-day window per task type.
- The facts do not state that provider-level routing automatically deprioritizes unreliable providers.
- The facts do not state a failed-requests-not-billed policy or integration that reconciles external provider charges.
- The facts do not establish SSO, RBAC, or PostgreSQL-backed audit logs; they establish mandatory caller authentication for router_control.app and verifiable receipts, which are not substitutes for those controls.
- The facts do not describe a router model trained on human preference data or per-query stronger-versus-weaker model selection.
- The facts do not provide a measured cost-reduction or quality-nonregression evaluation comparable to RouteLLM's stated result.
- The facts do not provide a managed-versus-self-hosted cost calculator incorporating the stated 5.5 percent credit fee.
- The supplied README head is truncated after 'Without a qualified sovereign', so the complete sovereign-route qualification rule cannot be verified from the supplied facts.
- pushed_at is null, so recent implementation activity cannot be established from the supplied metadata; this is an evidence limitation, not proof that the repository is unmaintained.

## Dosed upgrade plan
- First obtain the full README, docs/ROUTER_CONTROL.md, application code, and tests; map existing receipt formats, authentication, provider allowlisting, readiness checks, and failover behavior before proposing changes.
- Add a proposal-only provider reliability evidence design: define success, failure, latency, and observation-window metrics; keep explicit allowlists and dispatch gates authoritative; specify fail-closed behavior for insufficient evidence.
- Specify failed-request and provider-billing semantics as an integration contract, distinguishing router observations from provider-controlled charges and avoiding any unverified no-billing claim.
- Define an optional authorization and audit enhancement separately from receipts: identity roles, permitted actions, durable audit retention, and explicit enterprise or deployment boundaries; do not claim SSO/RBAC until implemented and tested.
- Create an offline evaluation proposal for preference-based routing with fixed quality, cost, latency, and safety datasets; compare against current routing and require human review before any advisory output can influence dispatch.
- Add cost-model documentation that compares self-hosting and managed gateway fees, including the 5.5 percent credit fee where applicable, with assumptions and workload sensitivity made explicit.
- Extend receipt schemas only through versioned, backward-compatible proposals that keep router_control.app SHA256 receipts distinct from legacy szl_router.app DSSE receipts and retain signed-versus-UNSIGNED honesty.
- Require tests for authentication denial, provider allowlisting, configuration readiness, loopback Ollama restrictions, digest pins, receipt verification, and fail-closed insufficient-evidence paths before considering implementation.
- Risk note: Medium-to-high if leader features are copied without evidence: reliability scoring could override sovereign-first policy, learned routing could degrade quality or admission control, billing claims could be false, and receipt or authorization changes could conflate distinct security guarantees. The immediate audit risk is evidentiary because only a truncated README head and metadata were supplied.
- Propose-only note: This audit proposes evaluation and design steps only. It does not authorize code changes, production routing changes, paid compute, GPU jobs, external dispatch, or claims that unverified features already exist. Any implementation should be separately reviewed against the full repository and the existing router-control contract.

## Advisory notes (unverified rails, non-binding)
- astra: szl-router: Verifiable routing provenance via cryptographic receipts. szl-forge: Domain-specific data curation pipelines for high-signal QLoRA. hatun-mcp: Formal semantic mapping between doctrine and tool schemas.
- sol: szl-router: Verifiable routing provenance via cryptographic receipts. szl-forge: Domain-specific QLoRA optimization presets for sovereign hardware. hatun-mcp: Formal doctrine-to-tool mapping for deterministic server behavior.
- claude: szl-router: Cryptographic proof of routing intent to prevent provider-side steering. szl-forge: Automated synthetic data curation loops tied to routing receipt failures. hatun-mcp: Formal verification of doctrine compliance for tool-call execution.

## DO NOT MERGE
This plan is a proposal artifact of the 10-pass OUROBOROS loop (loop-8 of 10). No code, no CI, no behavior changes in this file. A reviewer with authority may convert to a work order later.