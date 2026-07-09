"""Signed inference receipts for SZL Router — additive over x_szl_provenance.

Every /v1/chat/completions answer can carry a real, verifiable receipt of which
model served it, on whose hardware, and at what energy/tier. The receipt is a
DSSE/ECDSA-P256-SHA256 envelope built by the shared szl-receipt library
(optional `sign` extra) and attached to the HTTP response as the base64-JSON
header `x-szl-receipt`. The existing `x_szl_provenance` body block is unchanged —
this is a pure ADDITIVE signing layer.

Doctrine (non-negotiable, mirrors the rest of the fabric):
  * KEYLESS = UNSIGNED-honest. With no signing key, szl-receipt emits an envelope
    with signed=false and an honest note; a signature is NEVER fabricated.
  * Keys come ONLY from the environment, never the repo: a PEM in
    SZL_RECEIPT_KEY_PEM, or a PEM file path in SZL_RECEIPT_KEY_FILE.
  * First boot with no configured key generates an EPHEMERAL session keypair and
    logs its PUBLIC key, so a caller can verify THIS session's receipts. It is
    clearly labelled ephemeral — an honest session identity, not a persistent one.
    Set SZL_RECEIPT_EPHEMERAL=0 to stay truly keyless (UNSIGNED-honest).

szl-receipt is imported lazily so the router keeps its zero-hard-dependency
posture when signing isn't used; if the library is absent, no receipt header is
attached (we cannot construct an envelope) and routing is otherwise unchanged.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
from typing import Any, Dict, Optional, Tuple

ORGAN = "szl-router"
RECEIPT_KIND = "inference.route"


def _try_import():
    try:
        import szl_receipt  # noqa: PLC0415
        return szl_receipt
    except Exception:  # noqa: BLE001 - signing is optional; absence is honest
        return None


class _KeyState:
    def __init__(self) -> None:
        self.private_pem: Optional[bytes] = None
        self.public_pem: Optional[bytes] = None
        self.source: str = "uninitialized"
        self.ephemeral: bool = False
        self.library_available: bool = False


_STATE = _KeyState()
_INIT_LOCK = threading.Lock()
_INITIALIZED = False


def _env_truthy(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _derive_public_pem(priv_pem: bytes) -> bytes:
    """Public PEM for a configured private PEM, via the cryptography lib that
    szl-receipt already depends on (so no new dependency is introduced)."""
    from cryptography.hazmat.primitives import serialization  # noqa: PLC0415
    key = serialization.load_pem_private_key(priv_pem, password=None)
    return key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def init_signing(log=print) -> _KeyState:
    """Resolve the signing key ONCE (idempotent) and log the honest posture.

    Resolution order: SZL_RECEIPT_KEY_PEM -> SZL_RECEIPT_KEY_FILE -> ephemeral
    session key (unless SZL_RECEIPT_EPHEMERAL=0, which keeps us UNSIGNED-honest).
    """
    global _INITIALIZED
    with _INIT_LOCK:
        if _INITIALIZED:
            return _STATE
        _INITIALIZED = True

        sr = _try_import()
        if sr is None:
            _STATE.library_available = False
            _STATE.source = "library-absent"
            log("[szl-router] szl-receipt not installed — answers carry NO receipt "
                "header. Install the 'sign' extra to enable signed receipts.")
            return _STATE
        _STATE.library_available = True

        pem = os.environ.get("SZL_RECEIPT_KEY_PEM", "").strip()
        if pem:
            _STATE.private_pem = pem.encode("utf-8")
            _STATE.source = "env:SZL_RECEIPT_KEY_PEM"
        else:
            path = os.environ.get("SZL_RECEIPT_KEY_FILE", "").strip()
            if path:
                try:
                    with open(path, "rb") as fh:
                        _STATE.private_pem = fh.read()
                    _STATE.source = f"file:{path}"
                except Exception as e:  # noqa: BLE001
                    log(f"[szl-router] could not read SZL_RECEIPT_KEY_FILE={path!r}: "
                        f"{e} — falling back to an ephemeral session key.")

        if _STATE.private_pem:
            try:
                _STATE.public_pem = _derive_public_pem(_STATE.private_pem)
            except Exception as e:  # noqa: BLE001
                log(f"[szl-router] configured signing key is invalid ({e}); "
                    "discarding it and generating an ephemeral session key.")
                _STATE.private_pem = None

        if not _STATE.private_pem:
            if _env_truthy("SZL_RECEIPT_EPHEMERAL", default=True):
                priv, pub = sr.generate_keypair()
                _STATE.private_pem = priv
                _STATE.public_pem = pub
                _STATE.ephemeral = True
                _STATE.source = "ephemeral-session-key"
                log("[szl-router] no SZL_RECEIPT_KEY_PEM/FILE set — generated an "
                    "EPHEMERAL session signing key (honest: a per-session identity, "
                    "NOT a persistent one). Receipts this session verify against this "
                    "PUBLIC key:")
                log(pub.decode("ascii").strip())
            else:
                _STATE.source = "keyless"
                log("[szl-router] SZL_RECEIPT_EPHEMERAL=0 and no key configured — "
                    "receipts are UNSIGNED-honest (signed=false, never fabricated).")
        else:
            log(f"[szl-router] receipt signing armed from {_STATE.source}. "
                "Receipts verify against this PUBLIC key:")
            if _STATE.public_pem:
                log(_STATE.public_pem.decode("ascii").strip())
        return _STATE


def signing_state() -> _KeyState:
    return _STATE


def public_key_pem() -> Optional[str]:
    init_signing()
    if _STATE.public_pem is None:
        return None
    return _STATE.public_pem.decode("ascii")


def request_digest(model: str, messages: Any) -> str:
    """Stable SHA-256 over the request shape (model + messages), so a receipt
    binds to exactly what was asked without storing the prompt itself."""
    blob = json.dumps({"model": model, "messages": messages},
                      sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def build_envelope(
    *,
    provenance: Dict[str, Any],
    model: str,
    usage: Optional[Dict[str, Any]],
    req_digest: str,
    routing: Optional[Dict[str, Any]] = None,
    cost: Optional[Dict[str, Any]] = None,
    observer: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Build (and sign, if a key is armed) the inference.route receipt envelope.

    Returns the szl-receipt envelope dict, or None if szl-receipt is not
    installed (no header is attached in that case). When no key is armed the
    envelope is UNSIGNED-honest (signed=false) — never a fabricated signature.
    """
    sr = _try_import()
    if sr is None:
        return None
    init_signing()
    body = {
        "served_by": provenance.get("served_by"),
        "sovereign": provenance.get("sovereign"),
        "tier": provenance.get("tier"),
        "energy_source": provenance.get("energy_source"),
        # The router runs no joules meter, so bound energy is honestly the literal
        # "UNAVAILABLE" — measured-or-UNAVAILABLE doctrine, never a fabricated number.
        "energy": "UNAVAILABLE",
        "model": model,
        "attempts": provenance.get("attempts"),
        "usage": usage,
        "request_digest": req_digest,
    }
    # Only present for the opt-in "szl-auto" model: the signed, reproducible
    # routing decision. Omitted otherwise so existing receipts stay byte-identical.
    if routing is not None:
        body["routing"] = routing
    # ADDITIVE, honesty-first extensions (each omitted when absent so receipts
    # from older callers stay byte-identical):
    #   cost     — per-call USD block for the served route: paid tiers carry the
    #              spend-guard's auditable ESTIMATE (estimated:true + rate basis
    #              + token counts, matching the append-only ledger); free and
    #              sovereign tiers carry $0.00 vendor charge with an explicit
    #              basis string. Never a fabricated joules/energy figure.
    #   observer — the observer frame this receipt was issued under (endpoint,
    #              auth mode, requested model). The verdict is honest RELATIVE
    #              to this frame: what THIS caller asked and how they were
    #              authenticated — never a claim about any other vantage point.
    if cost is not None:
        body["cost"] = cost
    if observer is not None:
        body["observer"] = observer
    receipt = sr.Receipt(kind=RECEIPT_KIND, body=body)
    return sr.sign_receipt(receipt, _STATE.private_pem, organ=ORGAN)


def encode_header(envelope: Dict[str, Any]) -> str:
    """base64(JSON(envelope)) — compact, header-safe transport for x-szl-receipt."""
    raw = json.dumps(envelope, separators=(",", ":")).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def decode_header(value: str) -> Dict[str, Any]:
    """Inverse of encode_header — decode an x-szl-receipt header back to the envelope."""
    return json.loads(base64.b64decode(value.encode("ascii")).decode("utf-8"))


def verify_envelope(
    envelope: Dict[str, Any],
    public_pem: Optional[str | bytes],
) -> Tuple[bool, str]:
    """Verify an envelope against a public key. Thin pass-through to
    szl_receipt.verify_receipt so a buyer gets the library's exact verdict:
    (True,"ok") | (False,"unsigned-honest") | (False,"signature mismatch") | ..."""
    sr = _try_import()
    if sr is None:
        return False, "szl-receipt not installed"
    return sr.verify_receipt(envelope, public_key_pem=public_pem)
