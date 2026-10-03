# Provider qualification and runtime alignment

This is a source review queue, not an approved free-provider list. As of
2026-10-03, no external model in this repository has a verified, current
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
while model-specific qualification is unknown; its paid Moonshot estimate
remains advisory. Neither router has an implemented concurrent
pre-reservation mechanism for a strict external spend cap.

## Current terms and offer checkpoints

| Candidate | Evidence requiring operator review | Current route state |
| --- | --- | --- |
| Z.ai / Zhipu | [API terms](https://docs.z.ai/legal-agreement/terms-of-use) restrict using its models, prompts, or generated content for external-model development, training, labeling, fine-tuning, or optimization without authorization. Its [FAQ](https://docs.z.ai/help/faq) says request content may be cached; no zero-retention claim follows. Verify exact model, account, region, pricing, quota, and permitted data use. | `UNQUALIFIED`, disabled |
| SiliconFlow `.com` | [Terms](https://docs.siliconflow.com/en/legals/terms-of-service) restrict benchmarking, commercial use, and AI training/development. The `.com` terms direct mainland-China users to a separate [.cn agreement](https://docs.siliconflow.cn/docs/legals/terms-of-service); eligibility and price cannot be transferred between them. | `UNQUALIFIED`, disabled |
| Alibaba Model Studio | [Free-quota rules](https://help.aliyun.com/zh/model-studio/new-free-quota) describe model-specific new-user quota for 90 days in the Beijing real-time inference region. Verified accounts default to paid overage after exhaustion or expiry unless a per-model quota-only stop is set; setting changes may lag. | `UNQUALIFIED`, disabled |

Do not send provider prompts or outputs to SZL or Hugging Face model training
without a separately verified right to do so. Own software, policy, and offline
synthetic evaluation can advance without external model-output distillation.
These checkpoints are dated observations; terms and offers must be checked
again before any activation or use, including whether a proposed benchmark is
permitted. No provider request, account action, or live benchmark is part of
this source repair.

## Required evidence before a future route can be qualified

- Bind the exact provider domain, model ID and revision or alias, region,
  account/key eligibility, permitted use, data classification, and retention
  policy to current primary sources. Preserve an immutable evidence revision
  or access date and expiry where available.
- Verify input/output prices, remaining model-specific quota, reset and expiry,
  overage behavior, and the account's actual stop setting. Mark any unknown or
  expired field `UNQUALIFIED`; a successful request does not prove zero cost.
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
