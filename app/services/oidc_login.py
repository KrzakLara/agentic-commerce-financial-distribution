"""OpenID Connect Authorization Code login with PKCE for the bank's customer channel.

Status: LOCAL VERIFIED with a separate standards-based provider (`local_services.oidc_idp`). ADAPTER
READY for a bank's identity provider, which needs a client registration this project does not have.

It proves that the customer authenticated at the configured provider. It sets NO
identity-verification
status: regulated identity proofing is a separate, explicit step (`app.services.identity_proofing`).

The customer reference is derived from the verified (issuer, subject), never from a request, so a
caller cannot choose whose session it gets.
"""

import hashlib
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx
from sqlalchemy import select

from app.core.config import Settings
from app.integrations.customer_identity import (
    ExpectedIdentity,
    IdentityRejected,
    JwksIdTokenVerifier,
    new_pkce_pair,
    new_state_and_nonce,
    verify_id_token,
)
from app.integrations.safe_http import UnsafeUrlError, check_url, get_json, origin_of
from app.repositories.interfaces import UnitOfWork
from app.repositories.trust_orm import (
    AuthenticationEventRow,
    IdentityBindingRow,
    OidcLoginStateRow,
)

LOGIN_STATE_TTL = timedelta(minutes=5)
ASSURANCE_OIDC = "oidc_authenticated_not_proofed"
ASSURANCE_DEV = "dev_simulated"


class OidcLoginError(Exception):
    """A login was refused. Carries a machine code only, never a token or a claim."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class VerifiedLogin:
    customer_ref: str
    issuer: str
    acr: str | None
    amr: tuple[str, ...]
    auth_time: int | None


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def customer_ref_for(issuer: str, subject: str) -> str:
    """The bank's pseudonymous reference. Same login, same reference; no personal data in it."""
    return "cid-" + _hash(f"{issuer}|{subject}")[:40]


class OidcLoginService:
    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        settings: Settings,
        http_client: Callable[[], httpx.Client],
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._uow_factory = uow_factory
        self._settings = settings
        self._http_client = http_client
        self._clock = clock

    def _metadata(self, client: httpx.Client) -> Mapping[str, Any]:
        """The provider's validated discovery document.

        Its endpoints must stay on the issuer's own origin, so a changed or malicious document
        cannot send the bank's credentials or its next request anywhere else.
        """
        issuer = self._settings.oidc_issuer or ""
        allowed = frozenset({origin_of(issuer)})
        insecure = self._settings.oidc_allow_insecure_local
        try:
            url = f"{issuer.rstrip('/')}/.well-known/openid-configuration"
            check_url(url, allowed_origins=allowed, allow_insecure_local=insecure)
            metadata = get_json(client, url)
            if metadata.get("issuer") != issuer:
                raise OidcLoginError("issuer_mismatch")
            for name in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
                value = metadata.get(name)
                if not isinstance(value, str):
                    raise OidcLoginError("discovery_incomplete")
                check_url(value, allowed_origins=allowed, allow_insecure_local=insecure)
        except UnsafeUrlError as error:
            raise OidcLoginError(f"discovery_refused_{error}") from None
        except httpx.HTTPError:
            raise OidcLoginError("provider_unavailable") from None
        if metadata["jwks_uri"] != self._settings.oidc_jwks_uri:
            raise OidcLoginError("jwks_uri_mismatch")
        if "S256" not in metadata.get("code_challenge_methods_supported", ["S256"]):
            raise OidcLoginError("pkce_not_supported")
        return metadata

    def begin(self, signed_in_customer: str | None = None) -> tuple[str, str]:
        """(the provider's authorization URL, the value for the browser-binding cookie).

        `signed_in_customer` is who this browser is signed in as now. A login that comes back as
        somebody else is refused: to switch accounts, a customer signs out first.
        """
        now = self._clock()
        state, nonce = new_state_and_nonce()
        verifier, challenge = new_pkce_pair()
        browser_binding = secrets.token_urlsafe(32)
        with self._http_client() as client:
            metadata = self._metadata(client)
        with self._uow_factory() as uow:
            uow.trust.add(
                OidcLoginStateRow(
                    state_hash=_hash(state),
                    browser_binding_hash=_hash(browser_binding),
                    nonce=nonce,
                    code_verifier=verifier,
                    signed_in_customer=signed_in_customer,
                    created_at=now,
                    expires_at=now + LOGIN_STATE_TTL,
                )
            )
            uow.commit()
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self._settings.oidc_client_id,
                "redirect_uri": self._settings.oidc_redirect_uri,
                "scope": "openid profile",
                "state": state,
                "nonce": nonce,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "prompt": "login",
            }
        )
        return f"{metadata['authorization_endpoint']}?{query}", browser_binding

    def complete(
        self,
        *,
        state: str | None,
        code: str | None,
        issuer_parameter: str | None,
        browser_binding: str | None,
    ) -> VerifiedLogin:
        """Check state, browser, code and token. A state works once, for the browser it began in."""
        now = self._clock()
        if not state or not code or not browser_binding:
            raise OidcLoginError("callback_incomplete")
        issuer = self._settings.oidc_issuer or ""
        if issuer_parameter is not None and issuer_parameter != issuer:
            raise OidcLoginError("issuer_mismatch")  # RFC 9207: mix-up attack
        with self._uow_factory() as uow:
            row = uow.trust.get(OidcLoginStateRow, _hash(state))
            if row is None or row.used_at is not None or now >= row.expires_at:
                raise OidcLoginError("state_invalid")
            if row.browser_binding_hash != _hash(browser_binding):
                raise OidcLoginError("browser_binding_invalid")
            row.used_at = now
            nonce, verifier = row.nonce, row.code_verifier
            signed_in_customer = row.signed_in_customer
            uow.commit()
        with self._http_client() as client:
            metadata = self._metadata(client)
            id_token = self._redeem(client, metadata, code, verifier)
            try:
                jwks = get_json(client, metadata["jwks_uri"])
            except (UnsafeUrlError, httpx.HTTPError):
                raise OidcLoginError("jwks_unavailable") from None
        expected = ExpectedIdentity(
            issuer=issuer,
            audience=self._settings.oidc_client_id or "",
            nonce=nonce,
            now=now.timestamp(),
        )
        try:
            verifier_with_keys = JwksIdTokenVerifier(jwks)
            subject = verify_id_token(id_token, verifier_with_keys, expected)
            claims = verifier_with_keys.verify_signature(id_token)
        except IdentityRejected as rejected:
            raise OidcLoginError(rejected.reason) from None
        customer_ref = customer_ref_for(subject.issuer, subject.subject)
        if signed_in_customer is not None and signed_in_customer != customer_ref:
            raise OidcLoginError("account_switch_refused")
        amr = claims.get("amr")
        auth_time = claims.get("auth_time")
        acr = claims.get("acr")
        return VerifiedLogin(
            customer_ref=customer_ref,
            issuer=subject.issuer,
            acr=acr if isinstance(acr, str) else None,
            amr=tuple(str(item)[:40] for item in amr) if isinstance(amr, list) else (),
            auth_time=auth_time if type(auth_time) is int else None,
        )

    def _redeem(
        self, client: httpx.Client, metadata: Mapping[str, Any], code: str, verifier: str
    ) -> str:
        secret = self._settings.oidc_client_secret
        try:
            response = client.post(
                metadata["token_endpoint"],
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": self._settings.oidc_redirect_uri,
                    "code_verifier": verifier,
                },
                auth=(
                    self._settings.oidc_client_id or "",
                    secret.get_secret_value() if secret else "",
                ),
            )
        except httpx.HTTPError:
            raise OidcLoginError("provider_unavailable") from None
        if response.status_code != 200:
            raise OidcLoginError("code_refused")
        try:
            token = response.json().get("id_token")
        except (ValueError, AttributeError):
            raise OidcLoginError("token_response_invalid") from None
        if not isinstance(token, str):
            raise OidcLoginError("token_response_invalid")
        return token

    def record(self, login: VerifiedLogin, session_id: str) -> None:
        """Bind (issuer, subject) to the customer reference, and keep authentication evidence."""
        now = self._clock()
        with self._uow_factory() as uow:
            existing = uow.trust.get(IdentityBindingRow, login.customer_ref)
            if existing is None:
                uow.trust.add(
                    IdentityBindingRow(
                        customer_ref=login.customer_ref,
                        issuer=login.issuer,
                        subject_hash=login.customer_ref[4:],
                        created_at=now,
                    )
                )
            uow.trust.add(
                AuthenticationEventRow(
                    session_id=session_id,
                    customer_ref=login.customer_ref,
                    issuer=login.issuer,
                    acr=login.acr,
                    amr=list(login.amr),
                    auth_time=login.auth_time,
                    recorded_at=now,
                )
            )
            uow.commit()


def session_was_authenticated(uow: UnitOfWork, session_id: str | None, customer_ref: str) -> bool:
    """True only for a session created by a verified OIDC login of this customer."""
    if session_id is None:
        return False
    event = uow.trust.scalar(
        select(AuthenticationEventRow).where(AuthenticationEventRow.session_id == session_id)
    )
    return event is not None and event.customer_ref == customer_ref
