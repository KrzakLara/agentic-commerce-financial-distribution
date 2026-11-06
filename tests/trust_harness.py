"""The full-trust application in-process, with its separate services behind a dispatching transport.

Every actor still talks HTTP: the external agent, the bank's outbound calls and the providers'
callbacks go through `httpx.MockTransport`, which hands each request to the right application by
host.
The IdP here is a small test double signing real tokens. The separate Authlib provider is exercised
through real processes in `tests/test_trust_journey.py`.
"""

import base64
import hashlib
import json
import re
import shutil
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from joserfc import jwt
from pydantic import SecretStr
from sqlalchemy import text

from app.container import build_default_container
from app.core.config import Environment, Settings
from app.integrations.jose_tools import generate_key, public_jwks
from app.integrations.provider_callback import build_signed_confirmation
from app.main import create_app
from app.repositories.sqlalchemy_repositories import SqlAlchemyUnitOfWork
from app.services.authorization_service import AuthorizationService
from app.services.delegation_service import register_agent
from app.services.oidc_login import customer_ref_for
from external_agent.client import BankAgent, artifact_of
from external_agent.net import SafeHttp
from external_agent.trust import ProviderVerifier, new_client_key
from local_services import synthetic_providers, trust_authority
from scripts import customer_steps as steps
from tests.flow import CALLBACK_PATH, PROVIDER_SECRET, FakeClock

BANK = "https://bank.test"
AUTHORITY = "https://authority.test"
IDP = "https://idp.test"
PROVIDERS = "https://providers.test"
ROGUE = "https://rogue.test"
SIGNER_ID = "test-signer"
PROOFING_ID = "test-proofing"
ALL_SCOPES = "create_draft request_handoff read_status submit_after_consent"
OFFER = {"provider_id": "fixture-bank", "product_id": "depot-basic", "channel_id": "direct"}


class FakeIdp:
    """An OIDC provider for in-process tests: real discovery, PKCE check and signed ID tokens."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.key = generate_key("idp-1")
        self.codes: dict[str, dict[str, Any]] = {}
        self.next_subject = "alice"
        self.tamper: dict[str, Any] = {}
        self.sign_with = self.key
        self.discovery_overrides: dict[str, Any] = {}
        self.authorize_calls: list[dict[str, str]] = []

    def app(self) -> FastAPI:
        app = FastAPI()

        @app.get("/.well-known/openid-configuration")
        def discovery() -> dict[str, Any]:
            return {
                "issuer": IDP,
                "authorization_endpoint": f"{IDP}/authorize",
                "token_endpoint": f"{IDP}/token",
                "jwks_uri": f"{IDP}/jwks",
                "code_challenge_methods_supported": ["S256"],
                **self.discovery_overrides,
            }

        @app.get("/jwks")
        def keys() -> dict[str, Any]:
            return public_jwks(self.key)

        return app

    def handle_token(self, request: httpx.Request) -> httpx.Response:
        form = parse_qs(request.content.decode())
        code = form.get("code", [""])[0]
        item = self.codes.pop(code, None)
        if item is None:
            return httpx.Response(400, json={"error": "invalid_grant"})
        verifier = form.get("code_verifier", [""])[0]
        expected = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        if expected != item["challenge"]:
            return httpx.Response(400, json={"error": "invalid_grant"})
        now = int(self.clock().timestamp())
        claims = {
            "iss": IDP,
            "sub": item["subject"],
            "aud": "bank-client",
            "nonce": item["nonce"],
            "iat": now,
            "exp": now + 300,
            "auth_time": now,
            "acr": "urn:test",
            "amr": ["pwd"],
            **self.tamper,
        }
        token = jwt.encode(
            {"alg": "ES256", "kid": self.key.as_dict()["kid"]},
            claims,
            self.sign_with,
            algorithms=["ES256"],
        )
        return httpx.Response(200, json={"id_token": token, "access_token": "unused"})

    def issue_code(self, query: dict[str, str]) -> str:
        code = uuid.uuid4().hex
        self.codes[code] = {
            "subject": self.next_subject,
            "nonce": query["nonce"],
            "challenge": query["code_challenge"],
        }
        return code


@dataclass
class TrustWorld:
    settings: Settings
    clock: FakeClock
    app: FastAPI
    bank: TestClient
    authority_config: dict[str, Any]
    authority_key: Any
    bank_key: Any
    idp: FakeIdp
    providers: FastAPI
    partner_keys: dict[str, Any]
    client_key: Any
    transport: httpx.MockTransport
    signer_key: Any = None
    proofing_key: Any = None
    browsers: dict[str, TestClient] = field(default_factory=dict)

    @property
    def anchors(self) -> dict[str, Any]:
        return {AUTHORITY: public_jwks(self.authority_key)}

    def safe_http(self) -> SafeHttp:
        return SafeHttp(
            {"bank.test", "authority.test", "rogue.test"},
            allow_local_http=False,
            transport=self.transport,
        )

    def verifier(self, anchors: dict[str, Any] | None = None) -> ProviderVerifier:
        http = self.safe_http()
        return ProviderVerifier(http, anchors if anchors is not None else self.anchors)

    def agent(self, client_id: str = "agent-1", client_key: Any = None) -> BankAgent:
        http = self.safe_http()
        agent = BankAgent(
            BANK,
            http,
            client_id=client_id,
            client_key=client_key or self.client_key,
            dpop_key=new_client_key("dpop"),
            clock=lambda: self.clock().timestamp(),
        )
        trust = ProviderVerifier(http, self.anchors).verify(BANK, self.clock().timestamp())
        agent.discover(trust)
        return agent

    def browser(self, subject: str = "alice") -> TestClient:
        return self.browsers.setdefault(subject, TestClient(self.app, base_url=BANK))

    def login(self, subject: str = "alice", *, browser: TestClient | None = None) -> TestClient:
        """The authorization-code flow, as a browser performs it."""
        client = browser or self.browser(subject)
        self.idp.next_subject = subject
        target = "/customer/delegations"
        encoded = base64.urlsafe_b64encode(target.encode()).decode().rstrip("=")
        start = client.get(f"/customer/oidc/login?next={encoded}", follow_redirects=False)
        assert start.status_code == 303, start.text
        query = {k: v[0] for k, v in parse_qs(urlsplit(start.headers["location"]).query).items()}
        code = self.idp.issue_code(query)
        done = client.get(
            "/customer/oidc/callback",
            params={"code": code, "state": query["state"], "iss": IDP},
        )
        assert done.status_code == 200, done.text
        return client

    def customer_ref(self, subject: str = "alice") -> str:
        return customer_ref_for(IDP, subject)

    def delegate(
        self,
        agent: BankAgent,
        subject: str = "alice",
        *,
        scope: str = ALL_SCOPES,
        offer: dict[str, str] | None = None,
        approve: bool = True,
        browser: TestClient | None = None,
    ) -> dict[str, Any]:
        """The agent asks, the customer decides on the bank's page, the agent collects."""
        pushed = agent.request_delegation(scope=scope, **(offer or OFFER))
        assert agent.poll_token(pushed["device_code"]) == "pending"
        self.decide(pushed, subject, approve=approve, browser=browser)
        self.clock.advance(seconds=2)
        return pushed

    def decide(
        self,
        pushed: dict[str, Any],
        subject: str = "alice",
        *,
        approve: bool = True,
        browser: TestClient | None = None,
    ) -> None:
        client = browser or self.login(subject)
        link = urlsplit(pushed["verification_uri_complete"])
        page = client.get(f"{link.path}?{link.query}")
        assert page.status_code == 200, page.text
        form = steps.page_form(page.text, "/customer/delegations/authorize")
        assert form is not None
        response = client.post(
            "/customer/delegations/authorize",
            data={
                "request": form["request"],
                "csrf_token": form["csrf_token"],
                "decision": "approve" if approve else "deny",
            },
        )
        assert response.status_code == 200, response.text

    def start_application(self, agent: BankAgent, subject: str = "alice") -> dict[str, Any]:
        """Delegation, draft and handoff through the A2A interface."""
        pushed = self.delegate(agent, subject)
        assert agent.poll_token(pushed["device_code"]) == "issued"
        started = artifact_of(
            agent.call("depot-onboarding", agent.access_token, action="start", **OFFER)
        )
        return {
            "application_id": started["application_id"],
            "tokens": started["authorization"],
            "delegation_id": pushed["delegation_id"],
        }

    def request_link(self, agent: BankAgent, started: dict[str, Any]) -> str:
        handoff = artifact_of(
            agent.call(
                "depot-onboarding",
                started["tokens"]["onboarding_token"],
                action="request_customer_confirmation",
                application_id=started["application_id"],
            )
        )
        return handoff["customer_link"].removeprefix(BANK)

    def complete_proofing(
        self, browser: TestClient, link: str, outcome: str = "VERIFIED"
    ) -> httpx.Response:
        page = browser.get(link).text
        form = steps.page_form(page, "/identity-proofing")
        assert form is not None, "no identity form on the page"
        started = browser.post(
            form["_action"],
            data={"token": form["token"], "csrf_token": form["csrf_token"]},
            follow_redirects=False,
        )
        assert started.status_code == 200, started.text
        reference = provider_reference(started.text)
        return self.providers_http.post(
            f"/proofing/cases/{reference}/complete", data={"outcome": outcome}
        )

    @property
    def providers_http(self) -> TestClient:
        return TestClient(self.providers, base_url=PROVIDERS)

    def to_contract_pending(self, agent: BankAgent, subject: str = "alice") -> dict[str, Any]:
        browser = self.login(subject)
        started = self.start_application(agent, subject)
        link = self.request_link(agent, started)
        assert self.complete_proofing(browser, link).status_code == 200
        steps.complete_customer_information(browser, link)
        confirmed = steps.post_confirmation(browser, link)
        assert confirmed.status_code == 200, confirmed.text
        return {**started, "link": link, "browser": browser}

    def contract_form(self, browser: TestClient, application_id: str) -> dict[str, str]:
        page = browser.get(f"/customer/applications/{application_id}/contract")
        assert page.status_code == 200, page.text
        form = steps.page_form(page.text, "/contract/authorize")
        assert form is not None
        return form

    def authorize_signing(self, browser: TestClient, application_id: str) -> str:
        form = self.contract_form(browser, application_id)
        response = browser.post(
            form["_action"],
            data={"csrf_token": form["csrf_token"], "package_hash": form["package_hash"]},
            follow_redirects=False,
        )
        assert response.status_code == 200, response.text
        return provider_reference(response.text)

    def signer_outcome(self, reference: str, outcome: str) -> httpx.Response:
        return self.providers_http.post(
            f"/signing/sessions/{reference}/complete", data={"outcome": outcome}
        )

    def sign(self, browser: TestClient, application_id: str, outcome: str = "SIGNED") -> str:
        reference = self.authorize_signing(browser, application_id)
        assert self.signer_outcome(reference, outcome).status_code == 200
        return reference

    def provider_opens(self, application_id: str) -> httpx.Response:
        callback = build_signed_confirmation(
            PROVIDER_SECRET.encode(),
            application_id=application_id,
            provider_ref=f"MOCK-DEPOT-{application_id}",
            now=self.clock().timestamp(),
        )
        client = TestClient(self.app, base_url=BANK)
        return client.post(CALLBACK_PATH, content=callback.body, headers=callback.headers)

    def sql(self, statement: str, **params: object) -> list[tuple]:
        with self.app.state.engine.connect() as connection:
            return [tuple(row) for row in connection.execute(text(statement), params)]

    def status_of(self, application_id: str) -> str:
        return self.sql(
            "SELECT status FROM applications WHERE application_id = :i", i=application_id
        )[0][0]

    def event_types(self, application_id: str) -> list[str]:
        rows = self.sql(
            "SELECT event_type FROM audit_events WHERE application_id = :i ORDER BY rowid",
            i=application_id,
        )
        return [row[0] for row in rows]

    def audit_details(self, event_type: str, application_id: str | None = None) -> list[dict]:
        rows = self.sql(
            "SELECT details, application_id FROM audit_events WHERE event_type = :e ORDER BY rowid",
            e=event_type,
        )
        return [
            json.loads(d) if isinstance(d, str) else d
            for d, app in rows
            if application_id is None or app == application_id
        ]

    def authz(self) -> AuthorizationService:
        factory = self.app.state.session_factory
        return AuthorizationService(
            lambda: SqlAlchemyUnitOfWork(factory), clock=self.clock, bootstrap_enabled=True
        )


def provider_reference(page: str) -> str:
    """The provider's reference from the bank's handover link."""
    match = re.search(r'id="provider-link" href="[^"]*/([^/"]+)"', page)
    assert match, "no link to the provider"
    return match.group(1)


def _bank_settings(db: Path, **extra: Any) -> Settings:  # noqa: ANN401
    values: dict[str, Any] = {
        "environment": Environment.TEST,
        "database_url": f"sqlite:///{db.as_posix()}",
        "public_base_url": BANK,
        "mock_provider_callback_secret": SecretStr(PROVIDER_SECRET),
        "full_trust_profile": True,
        "customer_information_required": True,
        "customer_identity_mode": "oidc",
        "oidc_issuer": IDP,
        "oidc_client_id": "bank-client",
        "oidc_client_secret": SecretStr("test-only-client-value"),
        "oidc_redirect_uri": f"{BANK}/customer/oidc/callback",
        "oidc_jwks_uri": f"{IDP}/jwks",
        "trust_superior_entity": AUTHORITY,
        "signature_provider_id": SIGNER_ID,
        "signature_provider_url": PROVIDERS,
        "proofing_provider_id": PROOFING_ID,
        "proofing_provider_url": PROVIDERS,
        "log_level": "ERROR",
    }
    values.update(extra)
    return Settings(_env_file=None, **values)


@contextmanager
def trust_world(
    *,
    register: bool = True,
    allowed_scopes: str = ALL_SCOPES,
    **settings_extra: Any,  # noqa: ANN401
) -> Iterator[TrustWorld]:
    workdir = Path(tempfile.mkdtemp(prefix="trusteq_world_"))
    clock = FakeClock()
    idp = FakeIdp(clock)
    signer_key, proofing_key = generate_key("signer-1"), generate_key("proofing-1")
    partner_keys = {
        "sampletown-coop-bank": generate_key("partner-a"),
        "riverside-sample-bank": generate_key("partner-b"),
        "example-valley-savings-bank": generate_key("partner-c"),
    }
    settings = _bank_settings(
        workdir / "bank.db",
        signature_provider_jwks=json.dumps(public_jwks(signer_key)),
        proofing_provider_jwks=json.dumps(public_jwks(proofing_key)),
        partner_referral_jwks=json.dumps(
            {partner: public_jwks(key) for partner, key in partner_keys.items()}
        ),
        **settings_extra,
    )
    bank_key = generate_key("bank-1")
    authority_key = generate_key("authority-1")
    container = build_default_container(settings)
    container.clock = clock
    container.provider_key = bank_key
    apps: dict[str, Any] = {}

    def dispatch(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host == "idp.test" and request.url.path == "/token":
            return idp.handle_token(request)
        target = apps.get(host)
        if target is None:
            raise httpx.ConnectError("unknown host")
        client = TestClient(target, base_url=f"https://{host}", raise_server_exceptions=True)
        response = client.request(
            request.method,
            request.url.raw_path.decode(),
            content=request.content,
            headers={k: v for k, v in request.headers.items() if k.lower() != "host"},
            follow_redirects=False,
        )
        return httpx.Response(
            response.status_code, content=response.content, headers=response.headers
        )

    transport = httpx.MockTransport(dispatch)
    container.outbound_http = lambda: httpx.Client(transport=transport, timeout=5.0)
    app = create_app(settings, container)
    authority_config = {
        "entity_id": AUTHORITY,
        "signing_key": authority_key.as_dict(private=True),
        "subordinates": [
            {
                "entity_id": BANK,
                "jwks": public_jwks(bank_key),
                "organization_identity": "ASSERTED_BY_SYNTHETIC_AUTHORITY_TEST_ONLY",
            }
        ],
    }
    authority_app = trust_authority.create_app(lambda: authority_config, clock=clock)

    def provider_post(url: str, content: str) -> httpx.Response:
        with httpx.Client(transport=transport) as client:
            return client.post(url, content=content)

    providers = synthetic_providers.create_app(
        {
            "base_url": PROVIDERS,
            "bank_base_url": BANK,
            "signer_id": SIGNER_ID,
            "proofing_id": PROOFING_ID,
            "signer_key": signer_key.as_dict(private=True),
            "proofing_key": proofing_key.as_dict(private=True),
        },
        post=provider_post,
        clock=clock,
    )
    apps.update(
        {
            "bank.test": app,
            "authority.test": authority_app,
            "idp.test": idp.app(),
            "providers.test": providers,
        }
    )
    client_key = new_client_key("agent-key-1")
    try:
        with TestClient(app, base_url=BANK) as bank:
            if register:
                with SqlAlchemyUnitOfWork(app.state.session_factory) as uow:
                    register_agent(
                        uow,
                        client_id="agent-1",
                        display_name="Personal agent <test>",
                        jwks={"keys": [client_key.as_dict(private=False)]},
                        allowed_scopes=frozenset(allowed_scopes.split()),
                        now=clock(),
                    )
                    uow.commit()
            yield TrustWorld(
                settings=settings,
                clock=clock,
                app=app,
                bank=bank,
                authority_config=authority_config,
                authority_key=authority_key,
                bank_key=bank_key,
                idp=idp,
                providers=providers,
                partner_keys=partner_keys,
                client_key=client_key,
                transport=transport,
                signer_key=signer_key,
                proofing_key=proofing_key,
            )
            apps.clear()
    finally:
        app.state.engine.dispose()
        shutil.rmtree(workdir, ignore_errors=True)


def sign_referral(
    world: TrustWorld,
    partner: str,
    *,
    key: Any = None,  # noqa: ANN401
    terms: str = "sample-2026.11",
    product: str = "depot-basic",
    lifetime: int = 600,
) -> str:
    now = int(world.clock().timestamp())
    key = key or world.partner_keys[partner]
    return jwt.encode(
        {"alg": "ES256", "typ": "partner-referral+jwt", "kid": key.as_dict()["kid"]},
        {
            "iss": partner,
            "aud": BANK,
            "product_id": product,
            "terms_version": terms,
            "iat": now,
            "exp": now + lifetime,
            "jti": uuid.uuid4().hex,
        },
        key,
        algorithms=["ES256"],
    )


def sign_event(key: Any, typ: str, claims: dict[str, Any], clock: FakeClock) -> str:  # noqa: ANN401
    now = int(clock().timestamp())
    return jwt.encode(
        {"alg": "ES256", "typ": typ, "kid": key.as_dict()["kid"]},
        {"iat": now, "exp": now + 300, "aud": BANK, "jti": uuid.uuid4().hex * 2, **claims},
        key,
        algorithms=["ES256"],
    )
