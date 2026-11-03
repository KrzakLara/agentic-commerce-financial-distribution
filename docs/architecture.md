# Architecture overview (Phase 01)

Source of truth: `TRUSTEQ_ADR_v1.1_FINAL.docx` (ADR-001, frozen). This page only describes how
the code layout maps onto that decision; it does not change it.

**Decision recap:** L3 - controlled agentic onboarding. *LLM proposes; policy decides;
customer authorizes; bank executes.* Modular monolith with ports/adapters (ADR-02), REST-first
(ADR-04), persistent state machine (ADR-05).

## Package layout and dependency rules

```text
app/
  main.py          create_app() factory + ASGI entry point
  api/             T1 edge: routers, middleware, error mapping, EXTERNAL schemas
  core/            config, logging, exceptions (bottom layer, imports nothing from app)
  domain/          INTERNAL typed contracts (no I/O, no framework code)
  services/        T2 trusted workflow: ApplicationService (create, read, transition)
  repositories/    SQLAlchemy/SQLite: ORM models, repositories, unit of work
  integrations/    ports (Protocols), MOCK adapters, agent tool-gateway contract + REST gateway
  agents/          T0b external customer agent (scripted now, LLM later)
```

Allowed dependency direction (enforced partly by tests):

```text
api -> services -> repositories / integrations(ports) -> domain -> core
agents -> integrations.agent_tools ONLY (+ core.config for the factory)
```

* `integrations/rest_gateway.py` is a plain HTTP client (httpx): it cannot import repositories,
  services or the app (tested). The agent reaches the bank only over the public REST API.
* `agents` must never import `services`, `repositories`, the database layer or compliance code.
  `tests/test_agents.py::test_agent_package_only_depends_on_the_tool_gateway_contract` checks it.
* `domain` models are internal. `api/schemas` are the public contract. They are separate on
  purpose so that internal fields (e.g. compliance outcomes) cannot leak by accident.

## Request flow (Phase 01)

```text
agent (T0b) -> ScriptedCustomerAgent -> RestAgentToolGateway (+ AgentCredentials) --HTTP Bearer-->
   FastAPI route (T1) -> api/auth.py: authenticate token + require scope
   -> ApplicationService / OnboardingService (T2): resource binding, active grant, policy
   -> UnitOfWork -> SQLite
```
Routes return API schemas built from domain models; ORM rows never leave `repositories/`.
(Phase 01/02 used a DEV `X-Dev-Actor-Id` header at the second step; Phase 03 removed it.)

## Phase 03 additions

* `services/authorization_service.py` - issue (trusted bootstrap only), verify and revoke opaque
  scoped bearer tokens; `domain/mandate.py` - grant kinds, scopes, `AgentAccessToken`.
* `api/auth.py` - `Authorization: Bearer` dependency and one scope per route.
* `services/customer_session_service.py`, `api/customer_auth.py` - server-side customer
  sessions, session-bound CSRF tokens; `api/routes/customer.py` refuses agent credentials.
* `integrations/agent_credentials.py` - the agent runtime's token store (tokens never reach tool
  results).
* `repositories/database.py` - schema version 3 with a resumable v2 -> v3 upgrade.
* `scripts/dev_auth.py` - trusted dev bootstrap CLI; `scripts/security_mutations.py` - targeted
  mutation testing of the security controls.

Details: [authorization.md](authorization.md).

## Phase 02 additions

* `integrations/catalog_fixture.py` - verified (pinned-digest) provider discovery.
* `services/policy.py` - pure deterministic policy gate; `services/onboarding_service.py` -
  handoff, confirmation, checks, submission and provider confirmation, all through the state machine.
* `api/routes/{catalog,customer,internal}.py`, `api/customer_auth.py`, `api/pages.py` - discovery,
  the customer ceremony (JSON + HTML) and the hidden provider callback.
* `integrations/provider_callback.py` - HMAC signing/verification for the MOCK provider.
* `container.py` - composition root (swap mock outcomes, clock) for tests and later phases.
* `scripts/demo_happy.py` - the reproducible end-to-end demo.

Full sequence, transitions and limitations: [onboarding-flow.md](onboarding-flow.md).

## What exists

| Area | Status |
|---|---|
| Phase 01: SQLite persistence, repositories, unit of work, idempotency, append-only audit | Implemented |
| Phase 01: explicit state machine (`docs/state-machine.md`), atomic + optimistic-locked transitions | Implemented (rules + service; no endpoint drives it yet) |
| Phase 01: `POST /v1/applications`, `GET /v1/applications/{id}` with actor scoping | Implemented (DEV/TEST actor, not authentication) |
| Phase 01: scripted agent `draft_application` over the real REST API | Implemented |

### Phase 00 foundation

| Area | Status |
|---|---|
| FastAPI app, `GET /health`, OpenAPI, error envelope, correlation id, JSON logs | Implemented |
| Pydantic Settings (`TRUSTEQ_*`), safe defaults, no API key needed | Implemented |
| Domain contracts (11 models) | Typed contracts only, no behaviour |
| Ports for discovery, KYC, compliance, contract, depot, audit, agent tools | Interfaces |
| MOCK KYC / compliance / contract / depot adapters | Fixed deterministic stubs, marked `mock=True` |
| `ScriptedCustomerAgent` + gateway contract | Tested with a fake gateway (Phase 00) and over REST (Phase 01) |
| Token-level scoped mandates (issuer/audience/expiry/jti/revocation/scope/binding per request) | **Implemented in Phase 03** (controlled DEV authorization, not OAuth/FAPI) |
| Real customer authentication (eID / EUDI wallet / bank SCA) | **Not built** - DEV simulation only |

## Phase 04 additions

| Component | Role |
|---|---|
| `app/integrations/catalog_fixture.py` | signed, versioned, expiring manifests verified against the bank's provider/partner registers and trust anchors; product terms with a terms digest; prompt-injection fixture |
| `app/services/catalog_policy.py` | the single "only verified references, usable now" rule (`resolve_offer`) for create, handoff, confirm and submit |
| `app/services/review_service.py` | customer request for a person, operator directory, case workflow and the fixed decision policy |
| `app/domain/review.py`, `attribution.py`, `catalog.py` | review case + evidence, channel attribution, product terms / channel registry contracts |
| `scripts/dev_operator.py` | trusted DEV operator console (no HTTP route) |
| `scripts/demo_denied.py`, `scripts/demo_manual_review.py` | the DENIED and MANUAL REVIEW outcomes next to `demo_happy.py` |

Dependency rules are unchanged: services depend on ports and repositories, never on FastAPI; the
agent package still only talks to the public REST API. See
[phase-04-traceability.md](phase-04-traceability.md).
