"""HMAC-SHA256 signed receipts. The key comes from env ASKAROUND_SIGNING_KEY and is never printed or stored."""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
from pathlib import Path

from rich.console import Console

ALG = "HMAC-SHA256"
ENV = "ASKAROUND_SIGNING_KEY"


def _key(key: str | bytes | None = None) -> bytes | None:
    k = key if key is not None else os.environ.get(ENV, "")
    k = k.encode() if isinstance(k, str) else k
    return k or None


def canonical(receipt: dict) -> bytes:
    """Deterministic bytes of everything except the signature block."""
    body = {k: v for k, v in receipt.items() if k != "signature"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def sign_receipt(receipt: dict, key: str | bytes | None = None) -> dict:
    """Return the receipt with a signature block, or unchanged if no key is configured."""
    k = _key(key)
    if not k:
        return receipt
    out = {kk: v for kk, v in receipt.items() if kk != "signature"}
    out["signature"] = {"alg": ALG, "value": hmac.new(k, canonical(out), hashlib.sha256).hexdigest()}
    return out


def verify_receipt(receipt: dict, key: str | bytes | None = None) -> tuple[bool, str]:
    k = _key(key)
    if not k:
        return False, f"no signing key: set {ENV}"
    sig = receipt.get("signature")
    if not isinstance(sig, dict) or sig.get("alg") != ALG or not isinstance(sig.get("value"), str):
        return False, "receipt is not signed"
    expected = hmac.new(k, canonical(receipt), hashlib.sha256).hexdigest()
    if hmac.compare_digest(expected, sig["value"]):
        return True, "signature valid"
    return False, "SIGNATURE MISMATCH: receipt was altered or signed with a different key"


def verify_cmd(argv: list[str], console: Console) -> int:
    ap = argparse.ArgumentParser(prog="askaround verify", description="Verify a signed receipt")
    ap.add_argument("receipt", nargs="?", default="receipt.json")
    a = ap.parse_args(argv)
    try:
        receipt = json.loads(Path(a.receipt).read_text())
    except (OSError, json.JSONDecodeError) as e:
        console.print(f"[red]cannot read receipt:[/] {type(e).__name__}")
        return 2
    ok, msg = verify_receipt(receipt)
    console.print(f"[{'green' if ok else 'red'}]{msg}[/]")
    if not _key():
        return 2
    return 0 if ok else 1
