"""Signing and verification of the MOCK depot provider's confirmation callback.

The depot provider (T3) tells the trusted core (T2) that an account was opened. That message
is the ONLY thing that can lead to ``OPENED``, so it is authenticated: an HMAC-SHA256 over
``<timestamp>.<raw body>`` with a shared secret, a freshness window, and (in the service) a
unique ``confirmation_id`` against replay. This is a prototype stand-in for mutual TLS / signed
messages with a real provider; the shared secret is a MOCK credential and the whole endpoint is
refused in production.
"""

import hashlib
import hmac
import json
import time
import uuid
from dataclasses import dataclass

SIGNATURE_HEADER = "X-Provider-Signature"
TIMESTAMP_HEADER = "X-Provider-Timestamp"
MAX_CLOCK_SKEW_SECONDS = 300
_PREFIX = "sha256="


def sign(secret: bytes, timestamp: str, body: bytes) -> str:
    mac = hmac.new(secret, timestamp.encode("ascii") + b"." + body, hashlib.sha256)
    return _PREFIX + mac.hexdigest()


def verify(
    secret: bytes, *, timestamp: str | None, signature: str | None, body: bytes, now: float
) -> bool:
    """Constant-time signature check plus freshness. Any malformed input is simply False."""
    if not timestamp or not signature or not timestamp.isdigit():
        return False
    if abs(now - int(timestamp)) > MAX_CLOCK_SKEW_SECONDS:
        return False
    return hmac.compare_digest(sign(secret, timestamp, body), signature)


@dataclass(frozen=True)
class SignedCallback:
    """What the mock provider sends: raw body bytes plus the authentication headers."""

    body: bytes
    headers: dict[str, str]


def build_signed_confirmation(
    secret: bytes,
    *,
    application_id: str,
    provider_ref: str,
    confirmation_id: str | None = None,
    now: float | None = None,
) -> SignedCallback:
    """Produce the MOCK provider's signed 'account opened' confirmation."""
    body = json.dumps(
        {
            "application_id": application_id,
            "provider_ref": provider_ref,
            "confirmation_id": confirmation_id or uuid.uuid4().hex,
            "outcome": "OPENED",
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    timestamp = str(int(now if now is not None else time.time()))
    return SignedCallback(
        body=body,
        headers={
            TIMESTAMP_HEADER: timestamp,
            SIGNATURE_HEADER: sign(secret, timestamp, body),
            "Content-Type": "application/json",
        },
    )
