"""HTTP adapters of the vendor-neutral signing and identity-proofing boundaries.

The contracts are the bank's own. A real QTSP or identity vendor has its own API (remote signing
often follows the Cloud Signature Consortium API), so integrating one means another implementation
of these two ports. No real provider is integrated, and none of its credentials exist here.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

import httpx
from pydantic import SecretStr

from app.integrations.safe_http import UnsafeUrlError, check_url, origin_of


class ProviderCallFailed(Exception):
    """The provider did not take the request. The bank records it and changes nothing else."""


@dataclass(frozen=True)
class StartedAtProvider:
    provider_ref: str
    customer_url: str


class SignatureProviderPort(Protocol):
    def start_signing(
        self,
        *,
        session_id: str,
        application_id: str,
        package_hash: str,
        signer_ref: str,
        level: str,
        expires_at: int,
    ) -> StartedAtProvider: ...

    def cancel(self, provider_ref: str) -> None: ...


class IdentityProofingPort(Protocol):
    def start_case(
        self, *, case_id: str, application_id: str, customer_ref: str, expires_at: int
    ) -> StartedAtProvider: ...


class _RemoteProvider:
    def __init__(
        self,
        base_url: str,
        api_key: SecretStr | None,
        http_client: Callable[[], httpx.Client],
        *,
        allow_insecure_local: bool,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._key = api_key
        self._http_client = http_client
        self._insecure = allow_insecure_local

    def _post(self, path: str, body: dict[str, object]) -> dict[str, object]:
        url = f"{self._base}{path}"
        headers = {"Authorization": f"Bearer {self._key.get_secret_value()}"} if self._key else {}
        try:
            check_url(
                url,
                allowed_origins=frozenset({origin_of(self._base)}),
                allow_insecure_local=self._insecure,
            )
            with self._http_client() as client:
                response = client.post(url, json=body, headers=headers, follow_redirects=False)
            if response.status_code not in {200, 201}:
                raise ProviderCallFailed("provider_refused")
            data = response.json()
        except (httpx.HTTPError, UnsafeUrlError, ValueError):
            raise ProviderCallFailed("provider_unavailable") from None
        if not isinstance(data, dict):
            raise ProviderCallFailed("provider_response_invalid")
        return data

    def _started(self, data: dict[str, object]) -> StartedAtProvider:
        reference, customer_url = data.get("provider_ref"), data.get("customer_url")
        if not isinstance(reference, str) or not isinstance(customer_url, str):
            raise ProviderCallFailed("provider_response_invalid")
        try:
            check_url(
                customer_url,
                allowed_origins=frozenset({origin_of(self._base)}),
                allow_insecure_local=self._insecure,
            )
        except UnsafeUrlError:
            raise ProviderCallFailed("provider_response_invalid") from None
        return StartedAtProvider(reference, customer_url)


class RemoteSigningProvider(_RemoteProvider):
    def start_signing(
        self,
        *,
        session_id: str,
        application_id: str,
        package_hash: str,
        signer_ref: str,
        level: str,
        expires_at: int,
    ) -> StartedAtProvider:
        return self._started(
            self._post(
                "/signing/v1/sessions",
                {
                    "session_id": session_id,
                    "application_id": application_id,
                    "package_hash": package_hash,
                    "signer_ref": signer_ref,
                    "level": level,
                    "expires_at": expires_at,
                },
            )
        )

    def cancel(self, provider_ref: str) -> None:
        self._post(f"/signing/v1/sessions/{provider_ref}/cancel", {})


class RemoteProofingProvider(_RemoteProvider):
    def start_case(
        self, *, case_id: str, application_id: str, customer_ref: str, expires_at: int
    ) -> StartedAtProvider:
        return self._started(
            self._post(
                "/proofing/v1/cases",
                {
                    "case_id": case_id,
                    "application_id": application_id,
                    "customer_ref": customer_ref,
                    "expires_at": expires_at,
                },
            )
        )
