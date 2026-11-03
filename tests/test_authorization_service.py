"""Unit level: the grant/token model and the verification rules (no HTTP)."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.core.exceptions import (
    AgentAuthenticationRequiredError,
    InsufficientScopeError,
    InvalidTokenError,
    ServiceDisabledError,
)
from app.domain.enums import MandateKind, MandateScope
from app.domain.mandate import (
    AGENT_API_AUDIENCE,
    AUTHORIZATION_ISSUER,
    BOOTSTRAP_SCOPES,
    ONBOARDING_SCOPES,
    STATUS_SCOPES,
    AgentAccessToken,
    Mandate,
)
from app.integrations.agent_credentials import AgentCredentials, ApplicationTokens
from app.repositories.interfaces import UnitOfWork
from app.services.authorization_service import (
    AuthorizationService,
    _rejection_reason,
    hash_token,
    looks_like_agent_token,
)

NOW = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)


def grant(**overrides: object) -> Mandate:
    values: dict[str, object] = {
        "mandate_id": "mdt-1", "jti": "grt-1", "kind": MandateKind.STATUS,
        "application_id": "app-1", "agent_actor_id": "agent-a", "customer_id": "cust-1",
        "issuer": AUTHORIZATION_ISSUER, "audience": AGENT_API_AUDIENCE, "scopes": STATUS_SCOPES,
        "issued_at": NOW, "expires_at": NOW + timedelta(hours=1),
    }  # fmt: skip
    values.update(overrides)
    return Mandate(**values)  # type: ignore[arg-type]


def token(**overrides: object) -> AgentAccessToken:
    values: dict[str, object] = {
        "jti": "jti-1", "token_hash": "a" * 64, "mandate_id": "mdt-1",
        "issuer": AUTHORIZATION_ISSUER, "audience": AGENT_API_AUDIENCE, "scopes": STATUS_SCOPES,
        "issued_at": NOW, "expires_at": NOW + timedelta(hours=1),
    }  # fmt: skip
    values.update(overrides)
    return AgentAccessToken(**values)  # type: ignore[arg-type]


# --- the domain model -----------------------------------------------------------------------


def test_every_kind_of_grant_carries_exactly_its_own_scopes() -> None:
    assert {MandateScope.CREATE_DRAFT} == BOOTSTRAP_SCOPES
    assert {MandateScope.REQUEST_HANDOFF, MandateScope.SUBMIT_AFTER_CONSENT} == ONBOARDING_SCOPES
    assert {MandateScope.READ_STATUS} == STATUS_SCOPES
    assert not (BOOTSTRAP_SCOPES & ONBOARDING_SCOPES or ONBOARDING_SCOPES & STATUS_SCOPES)
    assert MandateScope.DISCOVER not in BOOTSTRAP_SCOPES | ONBOARDING_SCOPES | STATUS_SCOPES
    grant(kind=MandateKind.BOOTSTRAP, scopes=BOOTSTRAP_SCOPES, application_id=None)
    with pytest.raises(ValidationError, match="exactly its own scopes"):
        grant(kind=MandateKind.BOOTSTRAP, scopes=frozenset(MandateScope), application_id=None)
    with pytest.raises(ValidationError, match="exactly its own scopes"):
        grant(scopes=STATUS_SCOPES | {MandateScope.SUBMIT_AFTER_CONSENT})
    with pytest.raises(ValidationError, match="bound to one application"):
        grant(application_id=None)


def test_tokens_and_grants_need_a_positive_lifetime() -> None:
    with pytest.raises(ValidationError):
        token(expires_at=NOW)
    with pytest.raises(ValidationError):
        grant(expires_at=NOW - timedelta(seconds=1))
    with pytest.raises(ValidationError):
        token(scopes=frozenset())
    with pytest.raises(ValidationError):
        token(token_hash="not-a-sha256")  # noqa: S106 - deliberately invalid test value


# --- the verification rules -------------------------------------------------------------------


def test_a_valid_token_of_a_valid_grant_is_accepted() -> None:
    assert _rejection_reason(token(), grant(), AGENT_API_AUDIENCE, NOW) is None
    assert (
        _rejection_reason(token(), grant(), AGENT_API_AUDIENCE, NOW + timedelta(minutes=59)) is None
    )


@pytest.mark.parametrize(
    ("make_token", "make_grant", "at", "reason"),
    [
        (token, lambda: None, NOW, "grant_missing"),
        (lambda: token(revoked_at=NOW), grant, NOW, "token_revoked"),
        (token, lambda: grant(revoked_at=NOW), NOW, "grant_revoked"),
        (lambda: token(issuer="other"), grant, NOW, "wrong_issuer"),
        (token, lambda: grant(issuer="other"), NOW, "wrong_issuer"),
        (lambda: token(audience="trusteq-customer-channel"), grant, NOW, "wrong_audience"),
        (token, lambda: grant(audience="other"), NOW, "wrong_audience"),
        (token, grant, NOW - timedelta(seconds=1), "not_yet_valid"),
        (token, grant, NOW + timedelta(hours=1), "token_expired"),
        (
            lambda: token(expires_at=NOW + timedelta(hours=2)),
            grant,
            NOW + timedelta(hours=1),
            "grant_expired",
        ),
        (
            lambda: token(scopes=STATUS_SCOPES | {MandateScope.SUBMIT_AFTER_CONSENT}),
            grant,
            NOW,
            "scope_exceeds_grant",
        ),
    ],
)
def test_every_reason_to_refuse_a_known_token(
    make_token: Callable[[], AgentAccessToken],
    make_grant: Callable[[], Mandate | None],
    at: datetime,
    reason: str,
) -> None:
    assert _rejection_reason(make_token(), make_grant(), AGENT_API_AUDIENCE, at) == reason


def test_a_token_for_this_api_is_refused_by_another_audience() -> None:
    assert _rejection_reason(token(), grant(), "trusteq-customer-channel", NOW) == "wrong_audience"


# --- the service -----------------------------------------------------------------------------


def test_the_bootstrap_is_off_unless_explicitly_enabled(
    uow_factory: Callable[[], UnitOfWork],
) -> None:
    with pytest.raises(ServiceDisabledError):
        AuthorizationService(uow_factory).issue_bootstrap(
            agent_actor_id="agent-a", customer_id="cust-1", correlation_id="c"
        )
    with pytest.raises(ServiceDisabledError):
        AuthorizationService(uow_factory).reissue_for_application(
            application_id="app-1", correlation_id="c"
        )


def test_authenticate_fails_closed_on_missing_malformed_and_unknown_tokens(
    authz: AuthorizationService,
) -> None:
    with pytest.raises(AgentAuthenticationRequiredError):
        authz.authenticate(None, audience=AGENT_API_AUDIENCE, correlation_id="c")
    for raw in ("", "garbage", "tqa_" + "A" * 43, "tqs_" + "A" * 43):
        with pytest.raises((InvalidTokenError, AgentAuthenticationRequiredError)):
            authz.authenticate(raw, audience=AGENT_API_AUDIENCE, correlation_id="c")


def test_a_bootstrap_principal_has_only_the_create_scope(authz: AuthorizationService) -> None:
    issued = authz.issue_bootstrap(
        agent_actor_id="agent-a", customer_id="cust-1", correlation_id="c"
    )
    principal = authz.authenticate(issued.token, audience=AGENT_API_AUDIENCE, correlation_id="c")
    assert (principal.agent_actor_id, principal.customer_id) == ("agent-a", "cust-1")
    assert principal.scopes == BOOTSTRAP_SCOPES and principal.application_id is None
    assert principal.mandate_kind is MandateKind.BOOTSTRAP and principal.jti == issued.jti
    authz.require_scope(principal, MandateScope.CREATE_DRAFT, correlation_id="c")
    for scope in (MandateScope.READ_STATUS, MandateScope.SUBMIT_AFTER_CONSENT):
        with pytest.raises(InsufficientScopeError):
            authz.require_scope(principal, scope, correlation_id="c")
    with pytest.raises(InvalidTokenError):  # the same token is not valid for another audience
        authz.authenticate(issued.token, audience="trusteq-customer-channel", correlation_id="c")


def test_tokens_have_a_recognisable_prefix_and_only_their_hash_is_kept(
    authz: AuthorizationService,
) -> None:
    issued = authz.issue_bootstrap(
        agent_actor_id="agent-a", customer_id="cust-1", correlation_id="c"
    )
    assert looks_like_agent_token(issued.token) and not looks_like_agent_token("tqs_" + "A" * 43)
    assert len(hash_token(issued.token)) == 64 and hash_token(issued.token) != issued.token
    assert issued.token not in repr(issued)  # never in a log line or traceback


def test_credential_holders_never_print_their_tokens() -> None:
    store = AgentCredentials("tqa_" + "B" * 43)
    store.remember(
        "app-1", ApplicationTokens(onboarding="tqa_" + "O" * 43, status="tqa_" + "S" * 43)
    )
    for text in (repr(store), repr(store.for_application("app-1")), str(store)):
        assert "tqa_" not in text
