# SZL Router

**Sovereign, OpenAI-compatible LLM gateway with a signed receipt on every answer.**

One endpoint in front of many brains — our own GPU first, then free grid tiers,
then a paid fallback — and every answer comes with a **signed, verifiable receipt**
of which model served it, on whose hardware, and at what energy/tier.

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

You can also call `provider:upstream_model` directly (e.g. `groq:llama-3.3-70b-versatile`).

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
pip install "git+https://github.com/szl-holdings/szl-receipt.git@v0.1.0" pytest httpx
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
