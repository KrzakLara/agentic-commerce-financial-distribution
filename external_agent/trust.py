"""Provider verification by the agent, from a trust anchor the agent configured itself.

Implements the checks of OpenID Federation 1.0 section 3.2 that apply here, for a chain of a leaf
and
one or more configured superiors. The signatures are verified by `joserfc`. This is a subset, not a
conformance-tested federation library: no metadata policy, no trust marks and no resolver.

The four answers stay separate, on purpose:

A. metadata_integrity: the signature over the metadata holds.
B. organization_identity: what the configured authority states. Here a synthetic authority.
C. federation_membership: a chain reaches an anchor this agent trusts.
D. regulatory_authorization: never derived from A, B or C, and whatever a provider says about itself
   is ignored. Without an authoritative register the only honest answer is UNKNOWN.
"""

import base64
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import quote, urlsplit

from joserfc import jws, jwt
from joserfc.errors import JoseError
from joserfc.jwk import ECKey, KeySet

from external_agent.net import FetchRefused, SafeHttp

ENTITY_TYPE = "entity-statement+jwt"
CLOCK_SKEW = 60
MAX_CHAIN_LENGTH = 4


class TrustFailure(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class TrustResult:
    entity_id: str
    metadata_integrity: str
    organization_identity: str
    federation_membership: str
    regulatory_authorization: str
    trust_anchor: str
    chain: tuple[str, ...]
    authorized_keys: Mapping[str, Any] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("authorized_keys")
        data["chain"] = list(self.chain)
        return data


def _keys(jwks: Any) -> KeySet:  # noqa: ANN401
    entries = jwks.get("keys") if isinstance(jwks, Mapping) else None
    if not isinstance(entries, list) or not entries:
        raise TrustFailure("jwks_invalid")
    for entry in entries:
        if not isinstance(entry, Mapping) or "d" in entry or not entry.get("kid"):
            raise TrustFailure("jwks_invalid")
    try:
        return KeySet.import_key_set({"keys": list(entries)})
    except (ValueError, TypeError, JoseError):
        raise TrustFailure("jwks_invalid") from None


def _unverified_claims(token: str) -> dict[str, Any]:
    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (ValueError, IndexError, TypeError, AttributeError):
        raise TrustFailure("malformed_statement") from None
    if not isinstance(claims, dict):
        raise TrustFailure("malformed_statement")
    return claims


def _verify_statement(
    token: str,
    jwks: Any,
    *,
    issuer: str,
    subject: str,
    now: float,  # noqa: ANN401
) -> dict[str, Any]:
    """Section 3.2: signed JWT, `typ`, allowed `alg`, `iss`, `sub`, `iat`, `exp`, `jwks`, `kid`."""
    try:
        verified = jwt.decode(token, _keys(jwks), algorithms=["ES256"])
    except TrustFailure:
        raise
    except (ValueError, TypeError, KeyError, JoseError):
        raise TrustFailure("bad_signature") from None
    header, claims = dict(verified.header), dict(verified.claims)
    if header.get("typ") != ENTITY_TYPE:
        raise TrustFailure("wrong_statement_type")
    if not isinstance(header.get("kid"), str) or not header["kid"]:
        raise TrustFailure("kid_missing")
    if claims.get("iss") != issuer or claims.get("sub") != subject:
        raise TrustFailure("wrong_issuer_or_subject")
    issued, expires = claims.get("iat"), claims.get("exp")
    if type(issued) is not int or type(expires) is not int:
        raise TrustFailure("times_missing")
    if issued > now + CLOCK_SKEW:
        raise TrustFailure("issued_in_the_future")
    if now >= expires + CLOCK_SKEW:
        raise TrustFailure("statement_expired")
    _keys(claims.get("jwks"))
    return claims


class ProviderVerifier:
    """`trust_anchors` maps an entity id to the JWKS this agent was configured with, out of band."""

    def __init__(self, http: SafeHttp, trust_anchors: Mapping[str, Any]) -> None:
        self._http = http
        self._anchors = trust_anchors

    def _entity_configuration(self, entity_id: str) -> str:
        try:
            return self._http.get_text(f"{entity_id}/.well-known/openid-federation")
        except FetchRefused as refused:
            raise TrustFailure(f"fetch_{refused.reason}") from None

    def verify(self, entity_id: str, now: float) -> TrustResult:
        """Walk from the provider to a configured anchor. Any doubt raises `TrustFailure`."""
        leaf_token = self._entity_configuration(entity_id)
        leaf_unverified = _unverified_claims(leaf_token)
        # Self-signed: this alone proves only that the same party made the key and the statement.
        leaf = _verify_statement(
            leaf_token,
            leaf_unverified.get("jwks"),
            issuer=entity_id,
            subject=entity_id,
            now=now,
        )
        hints = leaf.get("authority_hints")
        if not isinstance(hints, list) or not hints:
            raise TrustFailure("no_authority_hints")
        # Only superiors this agent knows are ever contacted: a hint cannot pick an address.
        superior = next((hint for hint in hints if hint in self._anchors), None)
        if superior is None:
            raise TrustFailure("untrusted_anchor")
        anchor_jwks = self._anchors[superior]
        anchor_token = self._entity_configuration(superior)
        anchor = _verify_statement(
            anchor_token, anchor_jwks, issuer=superior, subject=superior, now=now
        )
        fetch = (
            anchor.get("metadata", {}).get("federation_entity", {}).get("federation_fetch_endpoint")
        )
        if not isinstance(fetch, str) or urlsplit(fetch).netloc != urlsplit(superior).netloc:
            raise TrustFailure("fetch_endpoint_invalid")
        try:
            statement_token = self._http.get_text(f"{fetch}?sub={quote(entity_id, safe='')}")
        except FetchRefused as refused:
            if refused.reason == "http_404":
                raise TrustFailure("not_a_subordinate") from None
            raise TrustFailure(f"fetch_{refused.reason}") from None
        statement = _verify_statement(
            statement_token, anchor_jwks, issuer=superior, subject=entity_id, now=now
        )
        # What counts is the key the SUPERIOR named. A rotated-out or revoked key fails here.
        authorized = statement["jwks"]
        try:
            _verify_statement(leaf_token, authorized, issuer=entity_id, subject=entity_id, now=now)
        except TrustFailure as failure:
            if failure.reason == "bad_signature":
                raise TrustFailure("key_not_authorized_by_superior") from None
            raise
        assessment = statement.get("metadata", {}).get("trusteq_trust_assessment", {})
        identity = assessment.get("organization_identity", "NOT_ASSESSED")
        return TrustResult(
            entity_id=entity_id,
            metadata_integrity="VERIFIED",
            organization_identity=str(identity),
            federation_membership="MEMBER_OF_CONFIGURED_FEDERATION",
            regulatory_authorization="UNKNOWN_NO_AUTHORITATIVE_LOOKUP",
            trust_anchor=superior,
            chain=(entity_id, superior),
            authorized_keys=authorized,
        )


def canonical(value: Any) -> bytes:  # noqa: ANN401
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def verify_detached(signature: Mapping[str, Any], payload: bytes, jwks: Any) -> None:  # noqa: ANN401
    """An Agent Card signature: JWS with its payload outside. Verified with authorized keys only."""
    protected, value = signature.get("protected"), signature.get("signature")
    if not isinstance(protected, str) or not isinstance(value, str):
        raise TrustFailure("signature_malformed")
    encoded = base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")
    verify_compact(f"{protected}.{encoded}.{value}", jwks)


def verify_compact(token: str, jwks: Any) -> bytes:  # noqa: ANN401
    try:
        return jws.deserialize_compact(token, _keys(jwks), algorithms=["ES256"]).payload
    except TrustFailure:
        raise
    except (ValueError, TypeError, KeyError, JoseError):
        raise TrustFailure("bad_signature") from None


def new_client_key(kid: str) -> ECKey:
    return ECKey.generate_key("P-256", parameters={"kid": kid, "use": "sig"}, private=True)
