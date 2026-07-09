> **SZL Holdings** · Doctrine v11 · Λ = Conjecture 1 (advisory, never "green"/theorem) · canonical [a-11-oy.com](https://a-11-oy.com)

# SZL Router

**Sovereign, OpenAI-compatible LLM gateway with a receipt on every answer — signed when a key is armed, else UNSIGNED-honest.**

One endpoint in front of many brains — our own GPU first, then free grid tiers,
then a paid fallback — and every answer comes with a **verifiable receipt**
(signed when a key is armed, else UNSIGNED-honest) of which model served it, on
whose hardware, and at what energy/tier.

## 🚀 Quickstart (`docker run`)

```bash
docker build -t szl-router .
# Point it at any upstream via env (no key is baked into the image):
docker run -p 8000:8000 -e GROQ_API_KEY=... szl-router
```

Then point any OpenAI client at `http://localhost:8000/v1`:

```bash
curl -i localhost:8000/v1/chat/completions -H 'content-type: application/json' \
  -d '{"model":"szl-large","messages":[{"role":"user","content":"hi"}]}'
```

The response body carries the honest `x_szl_provenance` block, and the HTTP
response carries an **`x-szl-receipt`** header: a base64-JSON DSSE/ECDSA-P256
envelope you can verify independently.

```python
from openai import OpenAI
client = OpenAI(base_url="http://localhost:8000/v1", api_key="unused")
client.chat.completions.create(model="szl-large",
    messages=[{"role": "user", "content": "hi"}])
```

### Verify a receipt (independently)

On first boot with no key configured, the server generates an **ephemeral session
signing key** and logs its public key (honest: a per-session identity, not a
persistent one). Fetch it and verify any receipt — offline, with no trust in us:

```bash
# this session's public key
curl -s localhost:8000/v1/receipt/pubkey | python -c 'import sys,json;print(json.load(sys.stdin)["public_key_pem"])' > session.pub

# grab the receipt header from a response, then verify via the CLI...
RECEIPT=$(curl -si localhost:8000/v1/chat/completions -H 'content-type: application/json' \
  -d '{"model":"szl-large","messages":[{"role":"user","content":"hi"}]}' \
  | tr -d '\r' | awk -F': ' 'tolower($1)=="x-szl-receipt"{print $2}')
echo "$RECEIPT" | python -m szl_router.verify --envelope - --pubkey session.pub

# ...or via the verify endpoint
curl -s localhost:8000/v1/receipt/verify -H 'content-type: application/json' \
  -d "{\"envelope\":\"$RECEIPT\",\"public_key_pem\":\"$(awk '{printf "%s\\n",$0}' session.pub)\"}"
```

For a **persistent** signing identity, set `SZL_RECEIPT_KEY_PEM` (a PEM string) or
`SZL_RECEIPT_KEY_FILE` (a PEM path) from the environment. **Keyless is honest:**
with `SZL_RECEIPT_EPHEMERAL=0` and no key, receipts are emitted UNSIGNED-honest
(`signed:false`) — a signature is never fabricated. Signing requires the optional
`sign` extra (the git-only `szl-receipt` library); the Docker image installs it.

### Receipt fields: `cost` + `observer` + `grid_context` (additive)

Beyond provenance, usage and the request digest, every new receipt also carries:

- **`cost`** — an honest per-call USD block for the served route. Paid tiers
  carry the spend-guard's auditable **estimate** (`estimated:true`, the price-table
  basis, and the token counts it priced — the *same* figure the append-only spend
  ledger records, so receipt and ledger always agree). Free and sovereign tiers
  carry `$0.00` **vendor charge** with an explicit basis string (sovereign metal:
  "electricity not metered here" — we say so instead of inventing a number).
- **`observer`** — the observer frame the receipt was issued under: endpoint,
  auth mode (`bearer`/`open`), and the requested model. A receipt's verdict is
  honest *relative to this frame* — what this caller asked and how they were
  authenticated — never a claim about any other vantage point.
- **`grid_context`** *(opt-in)* — the observed, **REPORTED** grid signal at route
  time (carbon intensity, gCO2/kWh) from the **keyless UK Carbon Intensity API**
  (`https://api.carbonintensity.org.uk/intensity`), carried **verbatim** with its
  `source`, `observed_at` and `fetched_at`. It follows the same honest pattern as
  `szl-energy-attest` (grid-*average*, never claimed "marginal"; no price, so
  `price` is `UNAVAILABLE`). It **documents** the grid window a run happened in —
  it never measures a joule (the receipt's `energy` stays `"UNAVAILABLE"`) and
  never creates energy. Enable with `SZL_RECEIPT_GRID_CONTEXT=1`; the keyless
  fetch runs in a background thread behind a short-TTL cache (`SZL_RECEIPT_GRID_TTL`,
  default 300s) and **never** blocks the request path. A missing/unreachable feed
  yields an honest all-null `UNAVAILABLE` block — never a fabricated number.

Both fields are **additive**: they are simply omitted when absent, so receipts
produced by older builds stay byte-identical.

---

Our own unified, OpenAI-compatible LLM router. One endpoint in front of many
brains — our own GPU first, then free grid tiers, then a paid fallback — with
**honest provenance on every answer**.

We studied the leaders (LiteLLM proxy, OpenRouter, lm-sys RouteLLM) and then
built our own, leaner version that fits our doctrine:

- **Sovereign-first.** Routes try our own metal (`box_gpu`, `nvidia_gpu`) before
  any third-party cloud, before any paid tier.
- **Honest labels.** Every response carries `x_szl_provenance` with `served_by`,
  `sovereign` (true *only* for hardware we own), `energy_source`, `tier`, and the
  full `attempts` trail. A free/grid tier is never labelled sovereign.
- **No secrets in the repo.** All upstream keys come from the environment. Nothing
  secret is ever written to disk or logged.
- **No half-state.** A logical model either resolves to a working upstream or the
  call fails loud (HTTP 502) with the complete attempt trail. Unavailable
  providers (missing key/url) are skipped — never faked.

## Logical models

| model       | intent              | fallback order (sovereign → free → paid)                          |
|-------------|---------------------|-------------------------------------------------------------------|
| `szl-large` | general large brain | box_gpu → nvidia_gpu → groq → nvidia_nim → moonshot(Kimi)          |
| `szl-fast`  | low-latency small   | box_gpu → groq → nvidia_nim                                        |
| `szl-coder` | coding              | box_gpu → nvidia_gpu → nvidia_nim → groq                           |
| `szl-auto`  | smart routing       | scores the prompt, then dispatches to one of the three above      |

You can also call `provider:upstream_model` directly (e.g. `groq:llama-3.3-70b-versatile`).

## Smart routing (`szl-auto`)

We studied how the leaders route by quality (lm-sys **RouteLLM**, OpenRouter's
*auto* model) and built our own, doctrine-true version. Ask for the opt-in
`szl-auto` model and the router scores the prompt with a **deterministic,
no-LLM heuristic** (`heuristic-v1`) — prompt length, code markers, conversation
depth, reasoning cues — then dispatches to the cheapest **capable** real logical
model, **sovereign-first**: a greeting goes to `szl-fast`, a hard multi-step
question to `szl-large`, anything with code to `szl-coder`.

What makes it *ours*: the routing decision is recorded honestly in
`x_szl_provenance.routing` **and signed into the inference receipt** — a
governed, independently-verifiable record of *why* a given brain answered. No
competitor ships a signed routing-decision receipt.

It is honest by construction: the score is a **routing estimate, never a quality
guarantee**, it makes **no extra upstream call**, and it is **pure** — the same
prompt always routes the same way, so the receipt is reproducible. Existing
models are untouched: their provenance and receipts are byte-identical to before
(the `routing` block is present only for `szl-auto`). Tune the escalation point
with `SZL_AUTO_LARGE_THRESHOLD` (default `0.50`).

```bash
curl localhost:8099/v1/chat/completions -H 'content-type: application/json' \
  -d '{"model":"szl-auto","messages":[{"role":"user","content":"hi"}]}' -i
# -> body.x_szl_provenance.routing shows chosen_logical=szl-fast;
#    the x-szl-receipt header carries the SIGNED routing decision.
```

## Providers

| provider      | tier       | sovereign | key env                                  |
|---------------|------------|-----------|------------------------------------------|
| box_gpu       | sovereign  | yes       | `A11OY_MODEL_BASE_URL` + `A11OY_GPU_TOKEN` |
| nvidia_gpu    | sovereign  | yes       | `NVIDIA_GPU_BASE_URL` + `NVIDIA_GPU_TOKEN` |
| groq          | free-grid  | no        | `GROQ_API_KEY`                           |
| nvidia_nim    | free-grid  | no        | `NVIDIA_NIM_API_KEY`                     |
| zhipu         | free-grid  | no        | `ZHIPU_API_KEY`                          |
| siliconflow   | free-grid  | no        | `SILICONFLOW_API_KEY`                    |
| moonshot/Kimi | paid-grid  | no        | `KIMI_API_KEY`                           |

A provider is **armed** the moment its key (and url, for the GPU tiers) is set.

## Run

```bash
pip install -r requirements.txt
uvicorn szl_router.app:app --host 0.0.0.0 --port 8099
```

```bash
curl localhost:8099/v1/chat/completions -H 'content-type: application/json' \
  -d '{"model":"szl-large","messages":[{"role":"user","content":"hi"}]}'
```

Set `SZL_ROUTER_TOKEN` to require `Authorization: Bearer <token>` on callers.

## Reliability

Two layers of resilience, both honest (a real failure is always surfaced in the
`attempts` trail — never papered over):

- **Route failover** — each logical model walks its ordered route list
  (sovereign → free-grid → paid-grid) and returns the first upstream that
  answers. An unavailable provider (no key/url) is skipped, never faked.
- **Same-provider transient retry** — before falling through to the next route, a
  provider that blips transiently (HTTP `429`/`500`/`502`/`503`/`504`, or a
  dropped connection) is retried with **exponential backoff + full jitter**, so
  concurrent callers don't retry in lockstep and stampede a recovering upstream.
  Permanent errors (`400`/`401`/`404`) are **not** retried — they fail through to
  the next route immediately. Tunable via env:

  | env | default | meaning |
  |-----|---------|---------|
  | `SZL_RETRY_MAX_ATTEMPTS` | `3` | total tries per provider |
  | `SZL_RETRY_BASE_DELAY`   | `0.25` | base backoff (seconds) |
  | `SZL_RETRY_MAX_DELAY`    | `4.0` | per-sleep cap (seconds) |

- **Failure cooldown** — after a provider fails a route (retry budget spent or a
  permanent error), it cools down for `SZL_COOLDOWN_SECONDS` (default `30`, `0`
  disables) so the very next callers don't pay the same failure latency again.
  Honest by construction: a cooled provider is skipped **only while a warm
  fallback remains** — as the last resort it is *always* tried (trying loudly
  beats refusing silently); every skip is recorded in the `attempts` trail as
  `cooldown-skip (...)` so the receipt shows exactly why a provider was not
  consulted; and a single success clears the cooldown immediately.

## Arm the NVIDIA GPU

When the rented GPU is reachable on an OpenAI-compatible URL (vLLM / NIM /
Ollama), set:

```bash
export NVIDIA_GPU_BASE_URL="https://<gpu-host>:<port>/v1"
export NVIDIA_GPU_TOKEN="<token>"
```

It immediately becomes the top sovereign route for every logical model — no code
change needed.

## Test

```bash
python3 test_router.py
```

Hits whatever providers are armed via env and prints the honest provenance of
each answer. Exits non-zero if nothing is wired.

The offline, deterministic suite (no network, no real keys) runs under pytest —
including `test_signed_receipt.py`, which mocks the upstream and proves the
signed-receipt contract end to end (signed receipt verifies, tampering fails,
keyless is UNSIGNED-honest):

```bash
pip install "git+https://github.com/szl-holdings/szl-receipt.git@v0.2.0" pytest httpx
python -m pytest -q
```

## Public status Space (governed source-of-truth)

The investor-facing status page at
[`SZLHOLDINGS/llm-router-live`](https://huggingface.co/spaces/SZLHOLDINGS/llm-router-live)
is a **static** surface (no internals, no keys) and is now governed from this repo
under [`space/`](space/) — previously it was hand-built on HF with no GitHub source.
It is deployed whole-context (`hf-space-deploy.yml`, `huggingface_hub.upload_folder`)
because its `COPY . /app` Dockerfile is intentionally excluded by the org's per-file
deployer. `hf-space-drift-check.yml` re-fetches every file from the live Space and
asserts `sha256 == space/`. This is distinct from the **router gateway image**
(`publish.yml` → GHCR), which stays private and serves a different surface on `:8000`.

## The Ouroboros loop (doctrine cross-reference)

The router **is** an instance of the estate's Ouroboros bounded-recursion loop. The
canonical definition is the receipt-closed kernel
[`szl-holdings/ouroboros` → `src/loop-kernel.ts`](https://github.com/szl-holdings/ouroboros/blob/main/src/loop-kernel.ts)
(`runLoop`): *bounded recursion with measurable convergence* that MUST terminate on one
of four exit conditions — `converged | consistent | aborted | budgetExhausted` — and
emits a governance receipt for every run.

How the router embodies that same primitive (in `szl_router/core.py`, see **Reliability** above):

- **Bounded.** Each logical model walks an *ordered, finite* route list (sovereign →
  free-grid → paid-grid); each provider retries at most `SZL_RETRY_MAX_ATTEMPTS` times
  with exponential backoff + full jitter; a spent provider cools down. There is no
  unbounded retry — the loop always terminates, either on a working upstream or a loud
  `HTTP 502` carrying the complete `attempts` trail.
- **Receipt-closed.** Every answer carries the `x-szl-receipt` envelope; the router's
  `attempts` trail is the loop's `LoopTrace` — **the trace is the product.**
- **Metaphor (doctrine, not math):** `receipts.in ≡ receipts.out` — the snake eats its
  own tail; each answer's provenance is fed back as an auditable input.

**Honesty (Doctrine v11):** Λ (the trust aggregator the kernel references) is
**Conjecture 1** — advisory, *never* a proven theorem (unconditional uniqueness is
machine-checked FALSE; only conditional CUT-2 uniqueness is proven in
[`lutar-lean`](https://github.com/szl-holdings/lutar-lean)). This loop is a *bounded,
terminating* control primitive — it makes **no** perpetual-motion or zero-cost claim.

---

**Explore the SZL estate:** [a11oy console](https://a-11-oy.com) · [Receipt format spec](https://github.com/szl-holdings/governed-receipt-spec) · [Lean proofs](https://github.com/szl-holdings/lutar-lean) · [Docs](https://github.com/szl-holdings/docs-site) · [🤗 SZLHOLDINGS](https://huggingface.co/SZLHOLDINGS)
