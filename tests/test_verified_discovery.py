"""Phase 04 verified provider discovery: signed, expiring, register-bound manifests - and
provider-supplied text that stays inert data (prompt-injection fixture)."""

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest

from app.domain.catalog import ChannelKind
from app.integrations.agent_tools import AgentToolCall, AgentToolName
from app.integrations.catalog_fixture import (
    DEFAULT_FIXTURE,
    HOSTILE_INSTRUCTIONS,
    PROVIDER_REGISTER,
    TRUST_ANCHORS,
    VERIFICATION_CHECKS,
    CatalogFixture,
    FixtureProviderDiscovery,
    ManifestRejected,
    TrustAnchor,
    canonical_json,
    prompt_injection_fixture,
    republish,
    sha256_hex,
    sign_manifest,
    tampered_copy,
)
from app.integrations.rest_gateway import RestAgentToolGateway
from tests.flow import FakeClock, Flow

Manifest = dict[str, Any]
Edit = Callable[[Manifest], None]
LATER = datetime(2027, 12, 31, tzinfo=UTC)


def signed_variant(edit: Edit) -> CatalogFixture:
    """The manifest edited, then validly SIGNED and PINNED again - the worst case: the content
    is cryptographically fine, and only the bank's register policy can refuse it."""
    fixture = tampered_copy()
    edit(fixture.manifests["fixture-bank"])
    republish(fixture, "fixture-bank")
    return fixture


def rejection(fixture: CatalogFixture, **kwargs: Any) -> ManifestRejected:
    with pytest.raises(ManifestRejected) as caught:
        FixtureProviderDiscovery(fixture, **kwargs)
    return caught.value


def provider(manifest: Manifest) -> Manifest:
    return manifest["provider"]


def channel(manifest: Manifest, channel_id: str) -> Manifest:
    return next(c for c in manifest["channels"] if c["channel_id"] == channel_id)


def set_provider(**values: Any) -> Edit:
    return lambda m: provider(m).update(values)


def set_channel(channel_id: str, **values: Any) -> Edit:
    return lambda m: channel(m, channel_id).update(values)


def add_capability(name: str) -> Edit:
    return lambda m: provider(m)["capabilities"].append(name)


def drop_capability(name: str) -> Edit:
    return lambda m: provider(m)["capabilities"].remove(name)


def add_unapproved_channel(m: Manifest) -> None:
    m["channels"].append({**channel(m, "partner-1"), "channel_id": "partner-evil"})


def overlong_conditions(m: Manifest) -> None:
    m["products"][0]["conditions"] = "x" * 201


# --- the default manifest passes every check --------------------------------------------------


def test_the_default_manifest_passes_every_check() -> None:
    catalog = FixtureProviderDiscovery()
    bank = catalog.get_provider("fixture-bank")
    assert bank.verified_domain == "depot.examplebank.example"
    assert bank.api_endpoint == "https://api.depot.examplebank.example/onboarding/v1"
    assert bank.trust_anchor == "dev-fixture-bank-2026"
    assert bank.registry_reference == "MOCKLEI0FIXTUREBANK1"
    assert bank.expires_at == datetime(2027, 11, 1, tzinfo=UTC)
    channels = {c.channel_id: c for c in catalog.list_channels("fixture-bank")}
    assert channels["partner-1"].kind is ChannelKind.PARTNER_BANK
    assert channels["partner-1"].partner_name == "Sampletown Cooperative Bank eG (MOCK)"  # register
    assert channels["partner-1"].active and not channels["partner-2"].active
    assert channels["direct"].partner_id is None


def test_the_manifest_endpoint_shows_what_was_verified(flow: Flow) -> None:
    body = flow.agent_http.get("/v1/providers/fixture-bank/manifest").json()
    assert body["registry_reference"] == "MOCKLEI0FIXTUREBANK1"
    assert body["verified_domain"] == "depot.examplebank.example"
    assert body["api_endpoint"].startswith("https://api.depot.examplebank.example/")
    assert body["signature_algorithm"] == "HMAC-SHA256"
    assert body["trust_anchor"] == "dev-fixture-bank-2026"
    assert body["verification_checks"] == list(VERIFICATION_CHECKS)
    assert body["expires_at"].startswith("2027-11-01")
    # an ended partnership is not offered at all
    assert {c["channel_id"] for c in body["channels"]} == {"direct", "partner-1"}


# --- what must be rejected - even when validly signed and pinned -------------------------------

ROGUE_ENDPOINT = "https://api.rogue.example/onboarding/v1"
CASES: list[tuple[str, Edit, str]] = [
    ("endpoint-substituted", set_provider(api_endpoint=ROGUE_ENDPOINT), "endpoint_not_approved"),
    (
        "endpoint-unapproved-subdomain",
        set_provider(api_endpoint="https://evil.depot.examplebank.example/v1"),
        "endpoint_not_approved",
    ),
    (
        "endpoint-not-https",
        set_provider(api_endpoint="http://api.depot.examplebank.example/onboarding/v1"),
        "endpoint_not_approved",
    ),
    (
        "domain-mismatch",
        set_provider(verified_domain="examplebank-depot.example"),
        "domain_mismatch",
    ),
    ("name-mismatch", set_provider(legal_name="Fake Bank AG"), "identity_mismatch"),
    ("role-mismatch", set_provider(role="FUND_PROVIDER"), "identity_mismatch"),
    (
        "registry-reference-mismatch",
        set_provider(registry_reference="MOCKLEI0OTHERBANK0001"),
        "identity_mismatch",
    ),
    ("unsupported-capability", add_capability("CRYPTO_CUSTODY"), "unsupported_capability"),
    (
        "missing-required-capability",
        drop_capability("SIGNED_STATUS_CALLBACK"),
        "unsupported_capability",
    ),
    ("unapproved-channel", add_unapproved_channel, "channel_not_approved"),
    (
        "fund-provider-as-partner",
        set_channel("partner-1", partner_id="sample-fund-manager"),
        "channel_not_approved",
    ),
    (
        "depot-provider-as-partner",
        set_channel("partner-1", partner_id="fixture-bank"),
        "channel_not_approved",
    ),
    ("unknown-partner", set_channel("partner-1", partner_id="unknown"), "channel_not_approved"),
    (
        "direct-channel-with-partner",
        set_channel("direct", partner_id="sampletown-coop-bank"),
        "malformed",
    ),
    ("stale-version", set_provider(manifest_version="2026-10-01"), "stale_version"),
    ("previous-version", set_provider(manifest_version="2026-11-01"), "stale_version"),
    ("malformed-version", set_provider(manifest_version="v1"), "malformed"),
    ("expired", set_provider(expires_at="2026-11-03T08:00:00+00:00"), "expired"),
    ("not-yet-valid", set_provider(issued_at="2026-12-01T00:00:00+00:00"), "not_yet_valid"),
    ("naive-timestamp", set_provider(expires_at="2027-11-01T00:00:00"), "malformed"),
    ("overlong-text", overlong_conditions, "malformed"),
]


@pytest.mark.parametrize(("edit", "reason"), [c[1:] for c in CASES], ids=[c[0] for c in CASES])
def test_a_signed_and_pinned_manifest_is_still_rejected_when_it_contradicts_the_register(
    edit: Edit, reason: str
) -> None:
    clock = FakeClock()  # 2026-11-03 09:00 UTC
    assert rejection(signed_variant(edit), clock=clock).reason == reason


def test_a_pinned_but_unsigned_edit_fails_the_signature_check() -> None:
    fixture = tampered_copy()
    provider(fixture.manifests["fixture-bank"])["api_endpoint"] = ROGUE_ENDPOINT
    fixture.pinned_manifest_digests["fixture-bank"] = sha256_hex(
        canonical_json(fixture.manifests["fixture-bank"])
    )  # an insider re-pins - but cannot produce the provider's signature
    assert rejection(fixture).reason == "bad_signature"


def test_unsigned_or_untrusted_signatures_are_rejected() -> None:
    unsigned = tampered_copy()
    unsigned.signatures.clear()
    assert rejection(unsigned).reason == "untrusted_signature"

    wrong_alg = tampered_copy()
    wrong_alg.signatures["fixture-bank"]["alg"] = "none"
    assert rejection(wrong_alg).reason == "untrusted_signature"

    # A key the bank trusts - but for ANOTHER provider - cannot vouch for this one.
    other = TrustAnchor("dev-other-bank", b"another synthetic key", frozenset({"other"}), LATER)
    anchors = {**TRUST_ANCHORS, other.key_id: other}
    borrowed = tampered_copy()
    borrowed.signatures["fixture-bank"] = sign_manifest(
        borrowed.manifests["fixture-bank"], other.key_id, anchors
    )
    assert rejection(borrowed, trust_anchors=anchors).reason == "untrusted_signature"


def test_an_expired_trust_anchor_is_rejected() -> None:
    after_anchor = datetime(2028, 1, 1, tzinfo=UTC)
    assert rejection(DEFAULT_FIXTURE, clock=lambda: after_anchor).reason == "untrusted_signature"


def test_a_registry_reference_alone_is_not_proof_of_endpoint_ownership() -> None:
    """A copied LEI, name and domain do not make a provider: only the bank's register does."""
    fixture = tampered_copy()
    copied = canonical_json(fixture.manifests["fixture-bank"]).decode()
    fixture.manifests["rogue-bank"] = json.loads(
        copied.replace('"provider_id":"fixture-bank"', '"provider_id":"rogue-bank"')
    )
    rogue_key = TrustAnchor("rogue-key", b"rogue", frozenset({"rogue-bank"}), LATER)
    anchors = {**TRUST_ANCHORS, rogue_key.key_id: rogue_key}
    fixture.pinned_manifest_digests["rogue-bank"] = sha256_hex(
        canonical_json(fixture.manifests["rogue-bank"])
    )
    fixture.signatures["rogue-bank"] = sign_manifest(
        fixture.manifests["rogue-bank"], "rogue-key", anchors
    )
    error = rejection(fixture, trust_anchors=anchors)
    assert error.reason == "unknown_provider" and error.provider_id == "rogue-bank"
    # And the real provider's LEI next to a substituted endpoint is refused as well.
    swapped = signed_variant(set_provider(api_endpoint=ROGUE_ENDPOINT))
    assert provider(swapped.manifests["fixture-bank"])["registry_reference"] == (
        PROVIDER_REGISTER["fixture-bank"].registry_reference
    )
    assert rejection(swapped).reason == "endpoint_not_approved"


def test_a_rejected_catalog_still_stops_the_application_from_starting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.container as container_module
    from app.core.config import Environment, Settings
    from app.core.exceptions import CatalogIntegrityError
    from app.main import create_app

    fixture = signed_variant(set_provider(api_endpoint=ROGUE_ENDPOINT))
    monkeypatch.setattr(
        container_module, "FixtureProviderDiscovery", lambda: FixtureProviderDiscovery(fixture)
    )
    with pytest.raises(CatalogIntegrityError, match="endpoint"):
        create_app(Settings(_env_file=None, environment=Environment.TEST))


# --- the validity window is enforced at use, not only at startup ---------------------------------


def short_lived(flow_clock: FakeClock, until: str) -> FixtureProviderDiscovery:
    return FixtureProviderDiscovery(
        signed_variant(set_provider(expires_at=until)), clock=flow_clock
    )


def test_a_manifest_that_expires_while_the_server_runs_is_refused_at_use(
    flow_factory: Callable[..., Flow],
) -> None:
    flow = flow_factory(handoff_ttl_seconds=3600, mandate_ttl_seconds=86400)
    flow.container.catalog = short_lived(flow.clock, "2026-11-03T09:30:00+00:00")
    started = flow.start()  # valid now
    flow.clock.advance(minutes=40)  # the manifest has expired; handoff and grant have not

    assert flow.agent_http.get("/v1/providers/fixture-bank/manifest").status_code == 503
    assert flow.agent_http.get("/v1/products").json() == {"products": []}
    response = flow.customer_confirm(started)
    assert response.status_code == 409  # an approval of an expired offer is refused
    assert flow.status_of(started.application_id) == "AWAITING_AUTHORIZATION"
    reasons = [e.details.get("reason") for e in flow.audit(started.application_id)]
    assert "action_no_longer_current" in reasons
    retry = flow.agent_http.post(
        f"/v1/applications/{started.application_id}/handoff",
        headers=flow.onboarding_headers(started.application_id),
    )
    assert retry.status_code == 422  # no new handoff for an expired provider either


def test_a_manifest_that_expires_after_consent_blocks_submission(
    flow_factory: Callable[..., Flow],
) -> None:
    flow = flow_factory(mandate_ttl_seconds=86400)
    flow.container.catalog = short_lived(flow.clock, "2026-11-03T09:30:00+00:00")
    started = flow.start()
    assert flow.customer_confirm(started).status_code == 200
    flow.clock.advance(minutes=40)
    response = flow.agent_http.post(
        f"/v1/applications/{started.application_id}/submit",
        headers=flow.onboarding_headers(started.application_id),
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "prerequisites_not_met"
    denied = [
        e.details
        for e in flow.audit(started.application_id)
        if e.event_type == "application.submit_denied"
    ]
    assert denied[-1] == {"missing": ["approved_action_current"]}
    assert flow.sql("SELECT COUNT(*) FROM depot_submissions") == [(0,)]


# --- prompt-injection fixture: provider-supplied text is DATA -----------------------------------


def test_hostile_text_in_a_signed_manifest_is_served_as_inert_data(
    flow_factory: Callable[..., Flow],
) -> None:
    flow = flow_factory(catalog=FixtureProviderDiscovery(prompt_injection_fixture()))
    (product,) = flow.agent_http.get("/v1/products").json()["products"]
    assert product["risk_summary"] == HOSTILE_INSTRUCTIONS  # exactly the text: a JSON string

    # The agent's tool layer passes only allow-listed structured fields ...
    gateway = RestAgentToolGateway(flow.agent_http, credentials=flow.credentials())
    result = gateway.execute(
        AgentToolCall(
            tool=AgentToolName.GET_OFFICIAL_PRODUCT,
            arguments={"provider_id": "fixture-bank", "product_id": "depot-basic"},
        )
    )
    assert "IGNORE" not in repr(result.data) and "approve_consent" not in repr(result.data)

    # ... and nothing the text asks for happens: same provider, same tools, same scopes.
    tools_before = {t.value for t in AgentToolName}
    started = flow.start()
    app_id = started.application_id
    assert flow.status_of(app_id) == "AWAITING_AUTHORIZATION"
    assert flow.sql("SELECT provider_id, channel_id FROM applications") == [
        ("fixture-bank", "direct")
    ]
    assert {t.value for t in AgentToolName} == tools_before
    assert "approve_consent" not in tools_before
    scopes = flow.sql(
        "SELECT kind, scopes FROM mandates WHERE application_id = :a ORDER BY kind", a=app_id
    )
    assert [(kind, sorted(json.loads(s))) for kind, s in scopes] == [
        ("bootstrap", ["create_draft"]),
        ("onboarding", ["request_handoff", "submit_after_consent"]),
        ("status", ["read_status"]),
    ]
    # The customer still decides; OPENED still needs the provider.
    assert flow.customer_confirm(started).status_code == 200
    assert flow.status_of(app_id) == "CONTRACT_PENDING"


def test_hostile_text_is_escaped_on_the_customer_page(flow_factory: Callable[..., Flow]) -> None:
    flow = flow_factory(catalog=FixtureProviderDiscovery(prompt_injection_fixture()))
    started = flow.start()
    flow.page_sign_in(started)
    page = flow.customer_http.get(
        f"/customer/handoffs/{started.handoff_id}", params={"token": started.nonce}
    )
    assert page.status_code == 200
    assert '<script>alert("x")</script>' not in page.text
    assert "&lt;script&gt;" in page.text  # shown as text, never executed


def test_the_hostile_text_is_part_of_what_the_customer_approves() -> None:
    """The terms digest covers the provider's text: what is shown is exactly what is bound."""
    plain = FixtureProviderDiscovery().list_products("fixture-bank")[0]
    hostile = FixtureProviderDiscovery(prompt_injection_fixture()).list_products("fixture-bank")[0]
    assert plain.terms_digest != hostile.terms_digest
