"""The agent's view of a bank: discovery, validation, delegation and the A2A JSON-RPC calls."""

import base64
import hashlib
import json
import time
import uuid
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlsplit

from joserfc import jwt
from joserfc.jwk import ECKey

from external_agent.net import FetchRefused, SafeHttp
from external_agent.trust import (
    TrustFailure,
    TrustResult,
    canonical,
    verify_compact,
    verify_detached,
)

SUPPORTED_A2A_VERSION = "1.0"
SUPPORTED_METADATA_VERSION = "1.0"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"  # noqa: S105 - a grant type name
_CARD_REQUIRED = (
    "name",
    "description",
    "supportedInterfaces",
    "version",
    "capabilities",
    "defaultInputModes",
    "defaultOutputModes",
    "skills",
)


class AgentError(Exception):
    def __init__(self, reason: str, detail: Any = None) -> None:  # noqa: ANN401
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


def validate_card(card: Any, origin: str) -> dict[str, Any]:  # noqa: ANN401
    """Refuse a malformed card and pick the one interface this agent can use.

    The interface must be on the discovered origin, so a card cannot redirect the agent elsewhere.
    Returns the chosen interface.
    """
    if not isinstance(card, Mapping) or any(name not in card for name in _CARD_REQUIRED):
        raise AgentError("card_malformed")
    skills = card["skills"]
    if not isinstance(skills, list) or not all(
        isinstance(s, Mapping) and isinstance(s.get("id"), str) for s in skills
    ):
        raise AgentError("card_malformed")
    interfaces = card["supportedInterfaces"]
    if not isinstance(interfaces, list) or not interfaces:
        raise AgentError("card_malformed")
    expected = urlsplit(origin).netloc
    versions_seen = []
    for interface in interfaces:
        if not isinstance(interface, Mapping) or not isinstance(interface.get("url"), str):
            raise AgentError("card_malformed")
        versions_seen.append(interface.get("protocolVersion"))
        if (
            interface.get("protocolBinding") == "JSONRPC"
            and interface.get("protocolVersion") == SUPPORTED_A2A_VERSION
        ):
            if urlsplit(interface["url"]).netloc != expected:
                raise AgentError("card_interface_on_another_origin")
            return dict(interface)
    raise AgentError("no_supported_protocol_version", versions_seen)


class BankAgent:
    def __init__(
        self,
        origin: str,
        http: SafeHttp,
        *,
        client_id: str,
        client_key: ECKey,
        dpop_key: ECKey,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.origin = origin.rstrip("/")
        self._http = http
        self._client_id = client_id
        self._client_key = client_key
        self._dpop_key = dpop_key
        self._clock = clock
        self.card: dict[str, Any] = {}
        self.interface: dict[str, Any] = {}
        self.access_token: str | None = None
        self.grant: dict[str, Any] = {}

    # --- discovery --------------------------------------------------------------------------------

    def discover(self, trust: TrustResult) -> dict[str, Any]:
        """Read the card and verify its signature with keys the verified chain authorized."""
        try:
            card = self._http.get_json(f"{self.origin}/.well-known/agent-card.json")
        except FetchRefused as refused:
            raise AgentError(f"card_{refused.reason}") from None
        self.interface = validate_card(card, self.origin)
        signatures = card.get("signatures")
        if not isinstance(signatures, list) or not signatures:
            raise AgentError("card_unsigned")
        unsigned = {key: value for key, value in card.items() if key != "signatures"}
        try:
            verify_detached(signatures[0], canonical(unsigned), trust.authorized_keys)
        except TrustFailure as failure:
            raise AgentError(f"card_{failure.reason}") from None
        self.card = card
        return card

    def skills(self) -> set[str]:
        return {skill["id"] for skill in self.card.get("skills", [])}

    def require_skill(self, skill: str) -> None:
        if skill not in self.skills():
            raise AgentError("capability_not_offered", skill)

    def _metadata(self, path: str, version: str = SUPPORTED_METADATA_VERSION) -> Any:  # noqa: ANN401
        response = self._http.request(
            "GET", f"{self.origin}{path}", headers={"Trusteq-Metadata-Version": version}
        )
        if response.status_code == 406:
            raise AgentError("metadata_version_refused", response.json()["error"]["supported"])
        if response.status_code != 200:
            raise AgentError(f"metadata_http_{response.status_code}")
        return response.json()

    def verified_profile(self, trust: TrustResult) -> dict[str, Any]:
        """The provider profile, only if its signature matches its content."""
        data = self._metadata("/v1/agent/profile")
        try:
            payload = verify_compact(data["signature"], trust.authorized_keys)
        except (TrustFailure, KeyError) as failure:
            raise AgentError("profile_signature_invalid") from failure
        if payload != canonical(data["profile"]):
            raise AgentError("profile_signature_invalid")
        return data["profile"]

    def catalog(self, version: str = SUPPORTED_METADATA_VERSION) -> dict[str, Any]:
        return self._metadata("/v1/agent/catalog", version)

    # --- proofs -----------------------------------------------------------------------------------

    def _client_assertion(self) -> str:
        now = int(self._clock())
        claims = {
            "iss": self._client_id,
            "sub": self._client_id,
            "aud": f"{self.origin}/v1/delegation/token",
            "iat": now,
            "exp": now + 120,
            "jti": uuid.uuid4().hex,
        }
        header = {"alg": "ES256", "kid": self._client_key.as_dict()["kid"]}
        return jwt.encode(header, claims, self._client_key, algorithms=["ES256"])

    def _dpop(self, method: str, url: str, token: str | None = None) -> str:
        claims: dict[str, Any] = {
            "htm": method,
            "htu": url,
            "iat": int(self._clock()),
            "jti": uuid.uuid4().hex + uuid.uuid4().hex,
        }
        if token:
            digest = hashlib.sha256(token.encode("ascii")).digest()
            claims["ath"] = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
        header = {
            "alg": "ES256",
            "typ": "dpop+jwt",
            "jwk": dict(self._dpop_key.as_dict(private=False)),
        }
        return jwt.encode(header, claims, self._dpop_key, algorithms=["ES256"])

    # --- delegation -------------------------------------------------------------------------------

    def request_delegation(
        self, *, scope: str, provider_id: str, product_id: str, channel_id: str
    ) -> dict[str, Any]:
        url = f"{self.origin}/v1/delegation/par"
        response = self._http.request(
            "POST",
            url,
            headers={"DPoP": self._dpop("POST", url)},
            data={
                "client_assertion": self._client_assertion(),
                "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
                "scope": scope,
                "authorization_details": json.dumps(
                    {
                        "type": "depot_onboarding",
                        "provider_id": provider_id,
                        "product_id": product_id,
                        "channel_id": channel_id,
                    }
                ),
            },
        )
        if response.status_code != 201:
            raise AgentError("delegation_refused", _error_of(response))
        return response.json()

    def poll_token(self, device_code: str) -> str:
        """`pending`, or stores the token and returns `issued`. Refusals raise `AgentError`."""
        url = f"{self.origin}/v1/delegation/token"
        response = self._http.request(
            "POST",
            url,
            headers={"DPoP": self._dpop("POST", url)},
            data={
                "grant_type": DEVICE_GRANT,
                "device_code": device_code,
                "client_assertion": self._client_assertion(),
            },
        )
        if response.status_code == 200:
            self.grant = response.json()
            self.access_token = self.grant["access_token"]
            return "issued"
        error = _error_of(response)
        if error in {"authorization_pending", "slow_down"}:
            return "pending"
        raise AgentError("token_refused", error)

    # --- A2A --------------------------------------------------------------------------------------

    def call(self, skill: str, token: str | None = None, **data: Any) -> dict[str, Any]:  # noqa: ANN401
        """`SendMessage` with a structured request. Returns the task. RPC errors raise."""
        self.require_skill(skill)
        url = self.interface["url"]
        headers = {"A2A-Version": SUPPORTED_A2A_VERSION, "Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"DPoP {token}"
            headers["DPoP"] = self._dpop("POST", url, token)
        return self.rpc(
            "SendMessage",
            {
                "message": {
                    "messageId": uuid.uuid4().hex,
                    "role": "ROLE_USER",
                    "parts": [{"data": {"skill": skill, **data}}],
                }
            },
            headers=headers,
        )["task"]

    def rpc(
        self, method: str, params: Mapping[str, Any], *, headers: Mapping[str, str] | None = None
    ) -> dict[str, Any]:
        response = self._http.request(
            "POST",
            self.interface["url"],
            headers=headers or {"A2A-Version": SUPPORTED_A2A_VERSION},
            json={"jsonrpc": "2.0", "id": uuid.uuid4().hex, "method": method, "params": params},
        )
        body = response.json()
        if "error" in body:
            raise AgentError("rpc_error", body["error"])
        return body["result"]

    def rest_status(self, application_id: str, token: str) -> int:
        url = f"{self.origin}/v1/applications/{application_id}"
        response = self._http.request(
            "GET",
            url,
            headers={"Authorization": f"DPoP {token}", "DPoP": self._dpop("GET", url, token)},
        )
        return response.status_code

    def submit_referral(self, application_id: str, token: str, referral: str) -> dict[str, Any]:
        url = f"{self.origin}/v1/applications/{application_id}/referrals"
        response = self._http.request(
            "POST",
            url,
            headers={"Authorization": f"DPoP {token}", "DPoP": self._dpop("POST", url, token)},
            json={"referral_token": referral},
        )
        if response.status_code != 200:
            raise AgentError("referral_refused", response.status_code)
        return response.json()


def artifact_of(task: Mapping[str, Any]) -> dict[str, Any]:
    return task["artifacts"][0]["parts"][0]["data"]


def _error_of(response: Any) -> str:  # noqa: ANN401
    try:
        return str(response.json().get("error"))
    except ValueError:
        return f"http_{response.status_code}"
