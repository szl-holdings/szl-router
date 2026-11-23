# Governed external-model protocol bridge (source-only)

GitHub `szl-holdings/szl-router` is the source owner. No Hub Space, DNS, model
card, GPU, credential, budget, or production runtime was changed by this
proposal. This is **text-only non-streaming** interoperability, not an agent
with tool authority or a model quality endorsement.

## Provider protocol matrix (2026-11-22)

| Lane | Protocol and verified endpoint | Source integration | Activation |
| --- | --- | --- | --- |
| Claude | Anthropic Messages: `https://api.anthropic.com/v1/messages`, `anthropic-version: 2026-10-21` | `anthropic_messages_https`; system-prefix + text, no tools | DISABLED pending model/account/terms/retention/strict-USD admission |
| Perplexity | Agent: `https://api.perplexity.ai/v1/agent` (Responses alias: `/v1/responses`) | `perplexity_agent_https`; **single-turn, text-only, no web search**, `store=false` | DISABLED pending provider contract, agent/tool and spend gates |
| Kimi | Moonshot OpenAI-compatible: `https://api.moonshot.ai/v1/chat/completions` | Existing `openai_https`; paid Moonshot lane remains separately blocked in legacy router | DISABLED pending model-specific price/spend gate |
| Astra | GPT-6 Astra may be offered by third-party Agent API; Google Project Astra is a different research prototype | **No invented direct API route**; verify exact provider/model/account before qualification | UNKNOWN / DISABLED |

The protocol bridge accepts only the fixed official host for Anthropic
(`api.anthropic.com`) and Perplexity (`api.perplexity.ai`), despite any
over-broad operator host allowlist. Model aliases are mutable and are never
claimed as immutable weights. Identity, provider usage, and a successful HTTP
status do not establish license rights, actual spend settlement, trained SZL
weights or deployment of this proposed source.

## Safe candidate registry (NEVER install as production without qualification)

All three candidates below use `"enabled": false` and are not a release
request. Environment variable names are identifiers, not embedded credentials.

```json
{"providers": [
  {"id":"claude-candidate","provider_type":"anthropic_messages_https",
   "base_url":"https://api.anthropic.com","models":{"szl-claude":"claude-opus-5-5"},
   "token_env":"ANTHROPIC_API_KEY","classifications":["public"],"cost_tier":9,"enabled":false},
  {"id":"perplexity-agent-candidate","provider_type":"perplexity_agent_https",
   "base_url":"https://api.perplexity.ai","models":{"szl-research":"openai/gpt-5.6-sol"},
   "token_env":"PERPLEXITY_API_KEY","classifications":["public"],"cost_tier":9,"enabled":false},
  {"id":"kimi-candidate","provider_type":"openai_https",
   "base_url":"https://api.moonshot.ai/v1","models":{"szl-kimi":"kimi-k3"},
   "token_env":"MOONSHOT_API_KEY","classifications":["public"],"cost_tier":9,"enabled":false}
]}
```

`SZL_ROUTER_ALLOWED_HOSTS` must explicitly list the intended official
hostnames. The router remains default-deny with
`SZL_ROUTER_ENABLE_EGRESS=0` and requires its own independent caller token.
Unlike the legacy `szl_router` owner-qualified OpenAI path, this control
gateway has no strict per-call USD reservation; **do not enable paid
candidates** until that control, qualification records, keys and human
review have been separately admitted.

Unsupported tool roles, later system messages, unsupported provider response
items, citations, and incomplete runs return an error, rather than being
erased or treated as ordinary prose. Perplexity's older Sonar Chat
Completions support ended 2026-09-27; do not point that vendor at
`/chat/completions` as the Agent API. A separate review is required to
preserve retrieval citations and authorize web-search tool costs.

## CPU-only acceptance

`uv run --with 'fastapi>=0.115' --with 'pydantic>=2' --with 'httpx>=0.27' --with pytest pytest -q tests/test_provider_protocols.py`

All network calls in that test use local `httpx.MockTransport`. Tests
require zero upstream keys, zero live model calls and no GPU. Exact-head
hosted CI, independent review and downstream Space/source digest checks
remain separate.

### Official upstream contracts

- Anthropic Messages: https://platform.claude.com/docs/en/api/messages/create
- Anthropic API version: https://platform.claude.com/docs/en/api/versioning
- Perplexity Agent API: https://docs.perplexity.ai/docs/agent-api/quickstart
- Perplexity Sonar migration: https://docs.perplexity.ai/docs/agent-api/migrate-from-sonar/overview
- Kimi provider protocol: https://www.kimi.com/code/docs/en/kimi-code-cli/configuration/providers
- Google Project Astra: https://deepmind.google/models/project-astra/
