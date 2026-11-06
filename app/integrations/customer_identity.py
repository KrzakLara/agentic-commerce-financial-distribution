"""The checks a customer identity provider's tokens must pass.

Status: LOCAL VERIFIED against a separate local OIDC provider, and ADAPTER READY for a real one. No
external sandbox or bank identity provider is configured for this project.
`customer_identity_mode=oidc` switches the DEV sign-in off. Without a complete provider
configuration
every customer page fails closed, and `NoIdentityProviderConfigured` refuses every token.

This module gives the login in `app.services.oidc_login`: the configuration check, PKCE, state and
nonce generation, and the deterministic checks of an ID token's claims. These run only after an
`IdTokenVerifier` has verified the signature with the provider's published keys.

An OIDC login at a test provider would prove authentication at that provider. It would not be German
eID identification, regulated KYC or bank-grade identity assurance.
"""

import base64
import hashlib
import hmac
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from app.core.config import Settings
from app.integrations.jose_tools import SignatureRejected, verify_jwt

_REQUIRED_SETTINGS = (
    "oidc_issuer",
    "oidc_client_id",
    "oidc_client_secret",
    "oidc_redirect_uri",
    "oidc_jwks_uri",
)
CLOCK_SKEW_SECONDS = 60


class IdentityRejected(Exception):
    """A token or a response was refused. Carries a machine code and never any token content."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ExpectedIdentity:
    issuer: str
    audience: str
    nonce: str
    now: float


@dataclass(frozen=True)
class VerifiedSubject:
    """Authenticated at the configured provider. No statement about KYC or legal identity."""

    issuer: str
    subject: str


class IdTokenVerifier(Protocol):
    def verify_signature(self, raw_token: str) -> Mapping[str, Any]:
        """Return the claims only if the signature checks out against the provider's keys."""
        ...


class NoIdentityProviderConfigured:
    """The only verifier this prototype ships: it refuses everything."""

    def verify_signature(self, raw_token: str) -> Mapping[str, Any]:  # noqa: ARG002
        raise IdentityRejected("no_identity_provider_configured")


class JwksIdTokenVerifier:
    """Verifies an ID token's signature with the provider's published keys, using `joserfc`."""

    def __init__(self, jwks: Mapping[str, Any]) -> None:
        self._jwks = jwks

    def verify_signature(self, raw_token: str) -> Mapping[str, Any]:
        try:
            _, claims = verify_jwt(raw_token, self._jwks, typ=None)
        except SignatureRejected:
            raise IdentityRejected("bad_signature") from None
        return claims


def missing_oidc_settings(settings: Settings) -> list[str]:
    """Which settings are absent. Names only, so a secret's value can never appear in a log."""
    return [name for name in _REQUIRED_SETTINGS if getattr(settings, name) is None]


def new_state_and_nonce() -> tuple[str, str]:
    return secrets.token_urlsafe(32), secrets.token_urlsafe(32)


def new_pkce_pair() -> tuple[str, str]:
    """(code_verifier, S256 code_challenge), RFC 7636."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def state_matches(expected: str, presented: str | None) -> bool:
    return bool(expected) and bool(presented) and hmac.compare_digest(expected, str(presented))


def check_id_token_claims(claims: Mapping[str, Any], expected: ExpectedIdentity) -> VerifiedSubject:
    """Issuer, audience, expiry, issue time, nonce and subject. Any doubt refuses."""
    if claims.get("iss") != expected.issuer:
        raise IdentityRejected("issuer_mismatch")
    audience = claims.get("aud")
    audiences = [audience] if isinstance(audience, str) else audience
    if not isinstance(audiences, list) or expected.audience not in audiences:
        raise IdentityRejected("audience_mismatch")
    if len(audiences) > 1 and claims.get("azp") != expected.audience:
        raise IdentityRejected("authorized_party_mismatch")
    expires, issued = claims.get("exp"), claims.get("iat")
    if type(expires) not in (int, float) or expected.now >= expires + CLOCK_SKEW_SECONDS:
        raise IdentityRejected("expired")
    if type(issued) not in (int, float) or issued > expected.now + CLOCK_SKEW_SECONDS:
        raise IdentityRejected("issued_in_the_future")
    nonce = claims.get("nonce")
    if not isinstance(nonce, str) or not hmac.compare_digest(nonce, expected.nonce):
        raise IdentityRejected("nonce_mismatch")
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject:
        raise IdentityRejected("subject_missing")
    return VerifiedSubject(issuer=expected.issuer, subject=subject)


def verify_id_token(
    raw_token: str, verifier: IdTokenVerifier, expected: ExpectedIdentity
) -> VerifiedSubject:
    """The signature first, then the claims. With no provider configured this always refuses."""
    return check_id_token_claims(verifier.verify_signature(raw_token), expected)
