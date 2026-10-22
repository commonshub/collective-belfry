"""Stripe webhook signature verification (``Stripe-Signature`` header), without the Stripe SDK.

Scheme: header ``t=<unix>,v1=<hex hmac>[,v1=...]``; signed payload ``"<t>.<raw body>"``;
HMAC-SHA256 with the endpoint secret. https://docs.stripe.com/webhooks#verify-manually
"""

from __future__ import annotations

import hashlib
import hmac
import time

DEFAULT_TOLERANCE = 300


class StripeSignatureError(ValueError):
    pass


def compute_signature(secret: str, timestamp: int, payload: bytes) -> str:
    signed = f"{timestamp}.".encode() + payload
    return hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()


def sign_header(secret: str, payload: bytes, timestamp: int | None = None) -> str:
    """Build a ``Stripe-Signature`` header value (used by tests and the smoke script)."""
    ts = int(time.time()) if timestamp is None else timestamp
    return f"t={ts},v1={compute_signature(secret, ts, payload)}"


def verify_signature(
    payload: bytes,
    header: str | None,
    secret: str,
    tolerance: int = DEFAULT_TOLERANCE,
    now: int | None = None,
) -> None:
    if not secret:
        raise StripeSignatureError("STRIPE_WEBHOOK_SECRET is not configured")
    if not header:
        raise StripeSignatureError("missing Stripe-Signature header")
    timestamp: int | None = None
    signatures: list[str] = []
    for part in header.split(","):
        key, _, value = part.strip().partition("=")
        if key == "t":
            try:
                timestamp = int(value)
            except ValueError as exc:
                raise StripeSignatureError("invalid timestamp") from exc
        elif key == "v1":
            signatures.append(value)
    if timestamp is None or not signatures:
        raise StripeSignatureError("malformed Stripe-Signature header")
    expected = compute_signature(secret, timestamp, payload)
    if not any(hmac.compare_digest(expected, sig) for sig in signatures):
        raise StripeSignatureError("no matching v1 signature")
    current = int(time.time()) if now is None else now
    if tolerance and abs(current - timestamp) > tolerance:
        raise StripeSignatureError("timestamp outside tolerance")
