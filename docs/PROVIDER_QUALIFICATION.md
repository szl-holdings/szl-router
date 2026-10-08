# Provider qualification and runtime alignment

This is a source review queue, not an approved free-provider list. As of
2026-10-08, no external model in this repository has a verified, current
model/account/region-specific zero-price and quota contract. An API key,
provider listing, successful response, or model name ending in `:free` is not
qualification evidence. Unknown or expired pricing, quota, or terms remain
`UNQUALIFIED` and must not be recorded as `$0` or routed as a free fallback.

The governed `router_control` gateway accepts a bounded slash-qualified HTTPS
upstream model name as a JSON value. Its public alias and provider ID stay
separate bounded identifiers. Configuration still requires caller auth, an
exact HTTPS host allowlist, a named upstream credential, classification policy,
and explicit egress enablement. An illustrative candidate record must have
`"enabled": false`; syntax validation and `cost_tier` are not billing or quota
admission. The legacy `szl_router` unqualified cloud-grid routes are blocked
while model-specific qualification is unknown. Its paid Moonshot route is also
blocked before transport, even with a key, until exact model pricing and a
strict pre-call spend reservation are qualified. Neither router has an
implemented concurrent pre-reservation mechanism for a strict external spend
cap.

## Current terms and offer checkpoints

| Candidate | Evidence requiring operator review | Current route state |
| --- | --- | --- |
| Z.ai standard API (`api.z.ai`) | [Pricing](https://docs.z.ai/guides/overview/pricing) lists `glm-4.7-flash`, `glm-4.5-flash`, and `glm-4.6v-flash` input/output as free, while built-in Web Search is paid. [API terms](https://docs.z.ai/legal-agreement/terms-of-use) restrict using models, prompts, or outputs for external-model development or optimization. The [API DPA](https://docs.z.ai/legal-agreement/privacy-policy) says content is not stored, but the [FAQ](https://docs.z.ai/help/faq) says request content may be cached for an undisclosed period. Do not claim zero retention. Account limits and eligibility are unverified. | `UNQUALIFIED`, disabled |
| Domestic Zhipu/BigModel (`bigmodel.cn`) | This is a different endpoint and [agreement](https://docs.bigmodel.cn/cn/terms/user-agreement); unpaid features are limited to noncommercial personal research/study, and outputs may not train or optimize other models. Its [`glm-4.5-flash` notice](https://docs.bigmodel.cn/cn/guide/models/free/glm-4.5-flash) says that ID now routes to `glm-4.7-flash` after retirement. Neither Z.ai pricing nor model identity transfers here. | `UNQUALIFIED`, disabled |
| SiliconFlow `.com` | [Terms](https://docs.siliconflow.com/en/legals/terms-of-service) bar attempted benchmarking, commercial use, and AI training/development. [Pricing](https://www.siliconflow.com/pricing) offers starter credit followed by usage billing, not a durable free tier; its [privacy policy](https://docs.siliconflow.com/en/legals/privacy-policy) gives no zero-retention guarantee. Mainland-China users are directed to `.cn`. | `UNQUALIFIED`, disabled |
| SiliconFlow `.cn` | The separate [agreement](https://docs.siliconflow.cn/docs/legals/terms-of-service) covers personal/internal business use and pay-after-use billing; general public-service and benchmark rights are unverified. [Pricing](https://www.siliconflow.cn/pricing) marks selected models free, but account eligibility and quota are unknown. Its [privacy policy](https://docs.siliconflow.cn/docs/legals/privacy-policy) describes logs, review, and retention exceptions. | `UNQUALIFIED`, disabled |
| Alibaba Model Studio | [Free-quota rules](https://help.aliyun.com/zh/model-studio/new-free-quota) describe model-specific new-user quota for 90 days in the Beijing real-time inference region. Verified accounts default to paid overage after exhaustion or expiry unless a per-model stop is set; setting changes may lag and the displayed balance can be stale. [Budgets](https://help.aliyun.com/zh/user-center/how-to-manage-a-budget) are alerts, not caps. | `UNQUALIFIED`, disabled |

Do not send provider prompts or outputs to SZL or Hugging Face model training
without a separately verified right to do so. Own software, policy, and offline
synthetic evaluation can advance without external model-output distillation.
These checkpoints are dated observations; terms and offers must be checked
again before any activation or use, including whether a proposed benchmark is
permitted. No provider request, account action, or live benchmark is part of
this source repair.

## Required evidence before a future route can be qualified

- Bind the exact provider domain and legal entity, model ID and revision or
  alias, region, account/key eligibility, permitted inference and benchmark
  rights, data classification, and retention policy to current primary sources.
  Preserve an immutable evidence revision
  or access date and expiry where available.
- Verify input/output and optional-tool prices, remaining model-specific quota,
  reset and expiry, overage behavior, and the account's actual stop setting.
  Mark any unknown or expired field `UNQUALIFIED`; a successful request does
  not prove zero cost.
- Review policy and durable, verified signed decision/outcome requirements
  before an A11oy-originated provider call. The control gateway's SHA256 receipt
  is unsigned and does not satisfy the legacy DSSE or A11oy signature contract.
- Run only authorized, bounded, public-data acceptance checks in a separately
  approved activation step. A hard concurrent spend cap needs a cost estimate
  reserved before transport and reconciled afterward; that mechanism is
  `NOT_IMPLEMENTED` for these external candidates.

For a Hugging Face `llm-router-live` followup, bind the Space image and source
metadata to the exact reviewed GitHub commit (`SOURCE_REVISION`) and read back
the deployed Space revision. Check configuration, runtime transport, model
identity, and a verified completion separately. Space readiness or a model
listing is only a status observation. Remote mutable model aliases retain
`UNAVAILABLE_MUTABLE_MODEL_ALIAS` until immutable executing artifact identity
is independently attested. This document does not authorize a Space write or
claim deployed inference.
