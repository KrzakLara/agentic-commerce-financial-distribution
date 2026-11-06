"""Signing and verification with the maintained `joserfc` library. No cryptography is written here.

ES256 only, on purpose: one algorithm is easy to review, and an allow-list refuses `none`, HMAC and
algorithm-confusion tricks before any key is touched. Every token type names its own `typ`, so a
token signed for one purpose cannot be replayed as another (OpenID Federation 1.0, section 3.2).
"""

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from joserfc import jws, jwt
from joserfc.errors import JoseError
from joserfc.jwk import ECKey, KeySet

ALGORITHMS = ["ES256"]


class SignatureRejected(Exception):
    """A signature or its header was refused. Carries a machine code, never token content."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def canonical_json(value: Any) -> bytes:  # noqa: ANN401 - any JSON value
    """Sorted keys and no insignificant whitespace.

    Equal to RFC 8785 (JCS) for what this project signs: ASCII keys, strings, booleans, integers
    and lists. It is not a general JCS implementation: floats are not normalized.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def sha256_hex(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def generate_key(kid: str) -> ECKey:
    return ECKey.generate_key("P-256", parameters={"kid": kid, "use": "sig"}, private=True)


def load_private_key(raw: str) -> ECKey:
    """A private JWK in JSON. Raises `ValueError` without repeating any of the input."""
    try:
        key = ECKey.import_key(json.loads(raw))
    except (ValueError, TypeError, JoseError):
        raise ValueError("the signing key is not a valid EC JWK") from None
    if not key.as_dict().get("kid") or not key.is_private:
        raise ValueError("the signing key must be a private JWK with a kid")
    return key


def public_jwk(key: ECKey) -> dict[str, Any]:
    return dict(key.as_dict(private=False))


def public_jwks(*keys: ECKey) -> dict[str, list[dict[str, Any]]]:
    return {"keys": [public_jwk(key) for key in keys]}


def load_key_set(jwks: Mapping[str, Any]) -> KeySet:
    """A public key set. Private members are refused, and every key needs a kid."""
    entries = jwks.get("keys") if isinstance(jwks, Mapping) else None
    if not isinstance(entries, list) or not entries:
        raise SignatureRejected("jwks_invalid")
    for entry in entries:
        if not isinstance(entry, Mapping) or "d" in entry or not entry.get("kid"):
            raise SignatureRejected("jwks_invalid")
    try:
        return KeySet.import_key_set({"keys": list(entries)})
    except (ValueError, TypeError, JoseError):
        raise SignatureRejected("jwks_invalid") from None


def sign_jwt(claims: Mapping[str, Any], key: ECKey, *, typ: str) -> str:
    header = {"alg": "ES256", "typ": typ, "kid": key.as_dict()["kid"]}
    return jwt.encode(header, dict(claims), key, algorithms=ALGORITHMS)


def verify_jwt(
    token: str, jwks: Mapping[str, Any] | KeySet, *, typ: str | None
) -> tuple[dict[str, Any], dict[str, Any]]:
    """(header, claims) if the signature checks out. Times and audience are the caller's job.

    `typ=None` skips the type check: an OpenID Connect ID token has no fixed `typ`.
    """
    keys = jwks if isinstance(jwks, KeySet) else load_key_set(jwks)
    if not isinstance(token, str) or token.count(".") != 2 or len(token) > 16384:
        raise SignatureRejected("malformed")
    try:
        verified = jwt.decode(token, keys, algorithms=ALGORITHMS)
    except (ValueError, TypeError, KeyError, JoseError):
        raise SignatureRejected("bad_signature") from None
    header, claims = dict(verified.header), dict(verified.claims)
    if typ is not None and header.get("typ") != typ:
        raise SignatureRejected("wrong_type")
    if not header.get("kid"):
        raise SignatureRejected("kid_missing")
    return header, claims


def sign_detached(payload: bytes, key: ECKey, *, typ: str) -> dict[str, str]:
    """`{"protected": ..., "signature": ...}` over `payload` (the A2A AgentCardSignature shape)."""
    header = {"alg": "ES256", "typ": typ, "kid": key.as_dict()["kid"]}
    protected, _, signature = jws.serialize_compact(
        header, payload, key, algorithms=ALGORITHMS
    ).split(".")
    return {"protected": protected, "signature": signature}


def sign_compact(payload: bytes, key: ECKey, *, typ: str) -> str:
    header = {"alg": "ES256", "typ": typ, "kid": key.as_dict()["kid"]}
    return jws.serialize_compact(header, payload, key, algorithms=ALGORITHMS)


def jwk_thumbprint(jwk: Mapping[str, Any]) -> str:
    """RFC 7638 thumbprint of a public JWK. A private member is refused."""
    if not isinstance(jwk, Mapping) or "d" in jwk:
        raise SignatureRejected("jwk_invalid")
    try:
        return str(ECKey.import_key(dict(jwk)).thumbprint())
    except (ValueError, TypeError, JoseError):
        raise SignatureRejected("jwk_invalid") from None


def unverified_header(token: str) -> dict[str, Any]:
    """Only to pick out a DPoP proof's embedded key, which the proof's signature then has to
    match."""
    try:
        value = jws.extract_compact(token.encode("ascii"))
    except (ValueError, UnicodeError, JoseError, AttributeError):
        raise SignatureRejected("malformed") from None
    return dict(value.protected)
