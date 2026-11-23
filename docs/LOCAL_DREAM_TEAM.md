# Local Dream Team — bounded advisory, source-only

This optional Python 3 standard-library CLI is owned by `szl-holdings/szl-router`.
It does not turn provider accounts or model aliases into operational privileges.

## One explicitly invoked CPU/loopback cycle

On a machine with Ollama and the four **already installed** models named in
`scripts/local_advisory_council.py`:

```bash
python3 scripts/local_advisory_council.py --run \
  --output /tmp/szl-local-council-unique-receipt.json
```

The absolute output file must be new and its parent directory must exist.
Do not publish the output automatically. Unprivileged invocation without
`--run` dispatches nothing and exits nonzero. There is no daemon, schedule,
retry loop, fallback provider, tool call, model download, paid API or cloud
writer. The CLI contacts only `127.0.0.1:11434` with proxy and HTTP redirects
disabled, using an allowlisted two-endpoint interface and bounded responses.

The fixed local seats are Kimi-VL-A3B-Thinking, Mistral 7B, Qwen 3.5 0.8B
and SZLHOLDINGS A11OY-MINI. The local Ollama daemon must report each precise
manifest digest **before and after** completion; a missing or moved pin is
`UNAVAILABLE`. Each seat has a fixed small token ceiling and one request only.

The natural-language responses are **untrusted advice**. The next seat may
read bounded prior advice as data, but it cannot execute it. The terminal
SHA-256 record chain is a locally generated **unsigned** content commitment,
not signer verification, model quality, paid-compute accounting, license proof
or proof of the executing weight bytes. Zero-width-only, empty, incomplete
and mismatched-model outputs cannot become successful observations.

## External Dream Team seats

Claude, Perplexity and Astra explicitly remain
`UNQUALIFIED_NOT_CALLED` in this local harness. Their separate, disabled
protocol adapters and qualification requirements are described in
[external model protocols](EXTERNAL_MODEL_PROTOCOLS.md). Do not claim that
a browser login, model catalog listing or API key authorizes an agent
with tools, retrieval, billing or production deployment.

## Verification

```bash
python3 -m unittest -v tests.test_local_advisory_council
# or: python3 -m pytest tests/test_local_advisory_council.py -q
```

Synthetic tests require no Ollama, secret, network, GPU or external provider.
They cover exact digest binding, withheld incomplete/blank outputs, no duplicate
inventory names, no automatic cloud calls, bounded token parameters, and
create-only evidence. Live local inference and offline tests are distinct.

This CLI never merges PRs, approves reviews, modifies protected main,
writes to Hugging Face, changes cloud infrastructure or creates training
checkpoints. A reviewed source merge is still not a deployed router release.
