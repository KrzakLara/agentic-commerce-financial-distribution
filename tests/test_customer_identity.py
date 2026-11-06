"""The OIDC boundary: configuration, fail-closed behaviour and claim checks. No provider exists."""

import base64
import hashlib

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.core.config import Environment, Settings
from app.integrations.customer_identity import (
    ExpectedIdentity,
    IdentityRejected,
    NoIdentityProviderConfigured,
    check_id_token_claims,
    missing_oidc_settings,
    new_pkce_pair,
    new_state_and_nonce,
    state_matches,
    verify_id_token,
)
from app.main import create_app

NOW = 1_800_000_000.0
EXPECTED = ExpectedIdentity(
    issuer="https://idp.example", audience="trusteq-client", nonce="n-1", now=NOW
)
GOOD = {
    "iss": "https://idp.example",
    "aud": "trusteq-client",
    "exp": NOW + 300,
    "iat": NOW - 5,
    "nonce": "n-1",
    "sub": "subject-1",
}


def test_the_dev_sign_in_is_the_default_and_stays_labelled() -> None:
    settings = Settings(_env_file=None, environment=Environment.TEST)
    assert settings.customer_identity_mode == "dev_simulated"
    assert settings.dev_customer_auth_active is True


def test_oidc_mode_switches_the_dev_sign_in_off_and_every_page_fails_closed() -> None:
    settings = Settings(_env_file=None, environment=Environment.TEST, customer_identity_mode="oidc")
    assert settings.dev_customer_auth_active is False
    with TestClient(create_app(settings)) as client:
        token = "A" * 43
        page = client.get("/customer/handoffs/hnd-1", params={"token": token})
        assert page.status_code == 401 and "Sign-in unavailable" in page.text
        assert "<form" not in page.text and "set-cookie" not in page.headers
        login = client.post(
            "/customer/dev-login",
            data={"customer_id": "cust-1", "handoff_id": "hnd-1", "token": token},
        )
        assert login.status_code == 401 and "set-cookie" not in login.headers
        assert (
            client.post(
                "/v1/customer/handoffs/hnd-1/tax-information",
                json={"nonce": token, "tax_residency_country": "DE"},
            ).status_code
            == 401
        )


def test_an_unknown_identity_mode_stops_the_configuration() -> None:
    with pytest.raises(ValueError, match="customer_identity_mode"):
        Settings(_env_file=None, customer_identity_mode="trust-me")


def test_incomplete_configuration_is_reported_by_name_and_never_by_value() -> None:
    settings = Settings(
        _env_file=None,
        customer_identity_mode="oidc",
        oidc_issuer="https://idp.example",
        oidc_client_secret=SecretStr("do-not-print-me"),
    )
    missing = missing_oidc_settings(settings)
    assert missing == ["oidc_client_id", "oidc_redirect_uri", "oidc_jwks_uri"]
    assert "do-not-print-me" not in repr(settings)
    blank = Settings(_env_file=None, oidc_client_secret="  ", oidc_issuer="")  # noqa: S106
    assert blank.oidc_client_secret is None and blank.oidc_issuer is None


def test_the_only_shipped_verifier_refuses_every_token() -> None:
    for token in ("", "not.a.token", "eyJhbGciOiJub25lIn0.e30."):
        with pytest.raises(IdentityRejected) as caught:
            verify_id_token(token, NoIdentityProviderConfigured(), EXPECTED)
        assert caught.value.reason == "no_identity_provider_configured"


def test_valid_claims_give_only_an_authenticated_subject() -> None:
    subject = check_id_token_claims(GOOD, EXPECTED)
    assert (subject.issuer, subject.subject) == ("https://idp.example", "subject-1")


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"iss": "https://evil.example"}, "issuer_mismatch"),
        ({"iss": None}, "issuer_mismatch"),
        ({"aud": "other-client"}, "audience_mismatch"),
        ({"aud": None}, "audience_mismatch"),
        ({"aud": ["trusteq-client", "other"]}, "authorized_party_mismatch"),
        ({"exp": NOW - 61}, "expired"),
        ({"exp": None}, "expired"),
        ({"exp": "soon"}, "expired"),
        ({"iat": NOW + 61}, "issued_in_the_future"),
        ({"iat": None}, "issued_in_the_future"),
        ({"nonce": "n-2"}, "nonce_mismatch"),
        ({"nonce": None}, "nonce_mismatch"),
        ({"sub": ""}, "subject_missing"),
        ({"sub": None}, "subject_missing"),
    ],
)
def test_any_wrong_claim_is_refused_with_a_code_and_no_content(
    change: dict[str, object], reason: str
) -> None:
    with pytest.raises(IdentityRejected) as caught:
        check_id_token_claims({**GOOD, **change}, EXPECTED)
    assert caught.value.reason == reason
    assert "evil" not in str(caught.value)


def test_several_audiences_need_the_authorized_party() -> None:
    claims = {**GOOD, "aud": ["trusteq-client", "other"], "azp": "trusteq-client"}
    assert check_id_token_claims(claims, EXPECTED).subject == "subject-1"


def test_pkce_uses_s256_and_fresh_values() -> None:
    verifier, challenge = new_pkce_pair()
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    assert challenge == base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    assert 43 <= len(verifier) <= 128
    assert new_pkce_pair()[0] != verifier
    state, nonce = new_state_and_nonce()
    assert state != nonce
    assert state_matches(state, state)
    assert not state_matches(state, nonce) and not state_matches(state, None)
    assert not state_matches("", "")
