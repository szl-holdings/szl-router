# Router request-digest pre-dispatch integrity

For `POST /v1/chat/completions`, the inference-route receipt's `request_digest`
binds the requested model and `messages` to a stable, SHA-256 hash of
sorted, compact, finite JSON (the legacy format for valid JSON inputs).

Before any provider dispatch, the router rejects requests containing NaN,
Infinity or unsupported Python values. It never silently uses a Python
`repr` as receipt evidence. Invalid requests return HTTP 400 and perform
**no** inference call.

For valid JSON objects the encoded digest remains byte-identical to earlier
releases. The digest does **not** prove execution, a signer, a model's identity,
or a payment settlement. It is only a canonical request identifier.

This supports an advisory-only `szl-mesh` observer that can compare a
reported Router receipt to the same original request without treating a
matching hash as authority. Independent DSSE trust verification, current
source/deployment binding, operator approval and production traffic tests
remain separate release gates.

Focused tests: `python -m pytest tests/test_request_digest_preflight.py`.
