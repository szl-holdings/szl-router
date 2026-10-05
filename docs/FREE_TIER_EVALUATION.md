<!-- SPDX-License-Identifier: Apache-2.0 -->
# Free-tier evaluation in the existing router control surface

Owner: `szl-holdings/szl-router#89`. This adds source-owned **research and disabled
proposals**, not provider activation or free tokens in another vendor's account.
The existing control app exposes `GET /api/free-provider-options`; the same data
is available without a server through `python -m router_control.free_tier`.
The control UI's **Inspect options** button performs one credential-free,
same-origin GET. It does not touch the completion form, active registry, keys,
local workers, or providers. Reference URLs are text, not third-party requests.

The October 4, 2026 primary-documentation review identifies exact Z.ai standard
API `glm-4.7-flash` / `glm-4.5-flash` candidates and Groq Free Plan
`openai/gpt-oss-120b`. Standard Z.ai and `bigmodel.cn` / Coding Plan are distinct
endpoints and account contracts. FlashX/5.x and paid built-in tools are not
substitutes. Groq's published limits are not this account's remaining quota.

The records remain **HOLD** with unqualified account, billing enforcement,
rights/retention, native completion, tool compatibility and model identity.
A source-reviewed quote is REPORTED, not SZL-measured cost, a permanent offer or
an immutable release. The local October 11 recheck deadline never extends on
HTTP refresh. A stale or backwards clock changes the evidence state and semantic
key but cannot authorize dispatch. Disabled proposals conform to the existing
ProviderRecord schema but are never installed into it. `cost_tier=0` is an
ordinal, not a dollar cap. The legacy spend guard is advisory, not a strict cap.

## Next legitimate activation requirement

Use the existing owner and protected source path. Verify the exact account's
eligibility, quotas, zero-charge enforcement/overage settings, endpoint/model,
applicable terms and public-data disclosure rights. Never paste keys into chat,
source or receipts. Do not repurpose a Codex/HF login as a third-party key.
Only after those independent conditions and an explicit source-admitted test
contract may one synthetic bounded request run. Bind request/model/endpoint,
response/outcome, quota headers and tool/refusal behavior to that test. Do not
turn an ambiguous outcome or 429 into a retry loop, account rotation or paid
fallback. A completion API is not automatically compatible with a coding agent's
tool/Responses protocol. Private repository content remains excluded.

No attempt is launched by this implementation, its tests, or its report CLI.
The private recovery/training/device exclusions are unchanged. Any later
activation must preserve all existing caller, host, source and egress controls.

## Verification and delivery boundaries

`python -m pytest -q tests/test_router_free_tier.py` runs actual-module and mounted
FastAPI tests plus executable Node DOM/transport seams. Node is required, not
silently skipped. General required CI collects this file through its existing
full-suite command. This patch does not alter any workflow or required check.
The existing Docker COPY and wheel package-data rules include the new module,
JS and CSS; `/api/source` includes their content digests for delivery inspection.

The router-control image has its own existing **manual** protected-main publisher.
The public `SZLHOLDINGS/llm-router-live` Space is a distinct legacy artifact.
Do not patch that mirror or create a second publisher to claim this new control
UI is live there. A11oy/storage recovery and product/proof activation are separate
work. Source merge, image/package qualification, publication and user-journey
acceptance must be evidenced separately.

Rollback is a normal source revert of this additive feature; it requires no key,
provider, model, account, billing, registry or private-data cleanup because none
is created or changed. Preserve the issue and original test/review history.

## Primary references, moving pages (not stable API release announcements)

- https://docs.z.ai/guides/overview/pricing
- https://docs.z.ai/guides/overview/quick-start
- https://console.groq.com/docs/rate-limits
- https://console.groq.com/docs/openai
- https://console.groq.com/docs/your-data
