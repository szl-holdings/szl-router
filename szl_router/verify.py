"""Offline receipt verifier — `python -m szl_router.verify`.

Lets a buyer independently verify an `x-szl-receipt` envelope WITHOUT running the
router, using only szl-receipt + a public key. Honest by construction: a keyless
envelope reports 'unsigned-honest' (never a fake pass) and a tampered body fails.

Usage:
    # verify a signed receipt against the session public key
    python -m szl_router.verify --envelope receipt.b64 --pubkey session.pub

    # --envelope accepts the base64 header value OR a JSON envelope file;
    #   pass '-' to read it from stdin. --pubkey is a PEM file path.
    echo "$X_SZL_RECEIPT" | python -m szl_router.verify --envelope - --pubkey session.pub

Exit code: 0 if valid, 1 otherwise (so it is scriptable in CI/sales demos).
"""
from __future__ import annotations

import argparse
import json
import sys

from . import receipts


def _load_envelope(value: str) -> dict:
    raw = sys.stdin.read() if value == "-" else _read_file_or_literal(value)
    raw = raw.strip()
    try:
        return json.loads(raw)          # JSON envelope
    except Exception:  # noqa: BLE001
        return receipts.decode_header(raw)  # base64-JSON header value


def _read_file_or_literal(value: str) -> str:
    try:
        with open(value, "r", encoding="utf-8") as fh:
            return fh.read()
    except (FileNotFoundError, IsADirectoryError, OSError):
        return value  # treat the argument itself as the envelope string


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="szl_router.verify",
                                 description="Verify an x-szl-receipt envelope offline.")
    ap.add_argument("--envelope", required=True,
                    help="base64 header value, a JSON envelope, a file path to either, or '-' for stdin")
    ap.add_argument("--pubkey", default=None,
                    help="PEM public-key file path (omit to confirm an UNSIGNED-honest envelope)")
    args = ap.parse_args(argv)

    envelope = _load_envelope(args.envelope)
    pub = None
    if args.pubkey:
        with open(args.pubkey, "rb") as fh:
            pub = fh.read()

    valid, detail = receipts.verify_envelope(envelope, pub)
    print(json.dumps({
        "valid": valid,
        "detail": detail,
        "signed": bool(envelope.get("signed", False)),
        "organ": envelope.get("organ"),
        "digest": envelope.get("digest"),
    }, indent=2))
    return 0 if valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
