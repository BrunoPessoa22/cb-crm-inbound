"""Svix-style webhook signature verification (Resend webhooks).

Resend signs webhooks the Svix way: the signed content is
``{svix-id}.{svix-timestamp}.{raw-body}`` HMAC-SHA256'd with the secret
that follows the ``whsec_`` prefix (base64-decoded). The signature header
can carry several space-separated ``v1,<base64sig>`` entries.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import time

TOLERANCE_SECONDS = 5 * 60


class SignatureError(Exception):
    """Raised when a webhook signature cannot be verified."""


def verify(
    secret: str,
    payload: bytes,
    svix_id: str,
    svix_timestamp: str,
    svix_signature: str,
    now: float | None = None,
) -> None:
    """Raise SignatureError unless the signature is valid and fresh."""
    if not secret:
        raise SignatureError("webhook secret not configured")
    if not (svix_id and svix_timestamp and svix_signature):
        raise SignatureError("missing svix headers")

    try:
        ts = int(svix_timestamp)
    except ValueError as exc:
        raise SignatureError("non-integer svix-timestamp") from exc
    current = now if now is not None else time.time()
    if abs(current - ts) > TOLERANCE_SECONDS:
        raise SignatureError("timestamp outside tolerance")

    raw_secret = secret.removeprefix("whsec_")
    try:
        key = base64.b64decode(raw_secret + "=" * (-len(raw_secret) % 4))
    except (ValueError, TypeError) as exc:
        raise SignatureError("secret is not valid base64") from exc

    signed_content = f"{svix_id}.{ts}.".encode() + payload
    expected = base64.b64encode(
        hmac.new(key, signed_content, hashlib.sha256).digest()
    ).decode()

    for candidate in svix_signature.split(" "):
        if "," not in candidate:
            continue
        version, _, sig = candidate.partition(",")
        if version != "v1":
            continue
        if hmac.compare_digest(sig, expected):
            return
    raise SignatureError("no matching v1 signature")
