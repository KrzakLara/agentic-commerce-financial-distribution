# Agent authorization (Phase 03)

> **Controlled development authorization implementation - NOT production OAuth 2.0, NOT a
> FAPI 2.0 profile, not certified.** It implements, enforces and tests the properties ADR-001
> v1.1 asks of a "short-lived scoped mandate" (section 03, threat table): actor/customer
> separation, audience, expiry, `jti`, revocation, per-request resource ownership and
> operation-specific scopes. The production target is listed at the end.

Code: `app/services/authorization_service.py` (issue / verify / revoke),
`app/api/auth.py` (bearer dependency + scope per route), `app/domain/mandate.py` (grant and
token model), `app/services/application_service.py` (creation, resource binding, revocation at
terminal states), `scripts/dev_auth.py` (trusted dev bootstrap CLI).

## Concepts

| Term | Meaning here |
|---|---|
| **Mandate (grant)** | Server-side record: *which agent* may do *what*, for *which customer* and *which application*, until when. Existing `Mandate` model, now with a `kind`. |
| **Access token** | An opaque bearer credential for one grant: `tqa_` + 43 random url-safe characters (256 bits from `secrets.token_urlsafe`). Only its **SHA-256 hash** is stored. |
| **Scope** | One operation: `create_draft`, `read_status`, `request_handoff`, `submit_after_consent` (names mirror the agent tools, ADR-09). `discover` exists but is never granted - discovery is public. |
| **Issuer / audience** | Every grant and token records `issuer = trusteq-dev-authorization` and `audience = trusteq-onboarding-api`; both are checked on every request. |
| **jti** | Unique identifier of each token (`jti-<hex>`); audit events and revocation refer to it, never to the token. Grants have their own id (`mdt-...`) and grant jti (`grt-...`). |

## The three kinds of grant

| Kind | Issued by | Scopes (exactly) | Bound to | Lifetime | Ends |
|---|---|---|---|---|---|
| `bootstrap` | the **trusted dev bootstrap** (CLI / demo / tests) - never an HTTP endpoint | `create_draft` | one agent + one customer; the application it creates (single use) | `agent_bootstrap_ttl_seconds` (900) | expiry, or revoked at the application's terminal state |
| `onboarding` | the bank, in the same transaction as the new application | `request_handoff`, `submit_after_consent` | that application, its agent and customer | `mandate_ttl_seconds` (3600) | expiry, or **revoked at OPENED / any terminal state** |
| `status` | the bank, with the application | `read_status` | that application | `agent_status_ttl_seconds` (86400) | expiry only (read-only, see below) |

Every new grant carries exactly its kind's scopes (issuance code; also a domain-model rule for
bootstrap and status grants). A token can never carry more scopes than its grant - checked at
issuance and on every request - and database triggers make grant and token claims immutable after
issue. Grants migrated from Phase 02 keep their recorded scope set; tokens re-issued for them only
ever get the onboarding scopes.

## Lifecycle

```text
 OPERATOR (trusted, dev)           AGENT (T0b)                         BANK (T1/T2)
 python -m scripts.dev_auth  ---> bootstrap token (create_draft,
   bootstrap --agent A               one application, customer C)
   --customer C
                                   POST /v1/applications ----------->  verify token, scope create_draft,
                                   Authorization: Bearer <bootstrap>   customer_ref == C, bootstrap unused
                                                                       => DRAFT + bind bootstrap (single use)
                                                                          + onboarding grant + status grant
                                   <---------------------------------  201 + both tokens (shown ONCE)
                                   POST /{id}/handoff  (onboarding)    request_handoff, bound to {id}
                                   ... customer confirms separately on the bank's page ...
                                   POST /{id}/submit   (onboarding)    submit_after_consent + policy gate
                                   GET  /{id}          (status)        read_status, bound to {id}
                                                                       provider confirms -> OPENED:
                                                                       bootstrap + onboarding grants and
                                                                       their tokens REVOKED (same transaction)
                                   GET  /{id}          (status) -----> OPENED   (read-only, until expiry)
                                   POST /{id}/submit   (onboarding) -> 401 invalid_token (revoked)
```

1. **Trusted bootstrap.** `python -m scripts.dev_auth bootstrap --agent A --customer C` (or the
   demo / tests, in-process) writes a bootstrap grant + token straight into the bank's database
   through the normal repositories. It refuses unless `dev_agent_bootstrap_active` (never in
   production). **There is no HTTP endpoint that issues or extends tokens**, so no remote caller
   can mint itself an identity or a scope; the API's own `AuthorizationService` is constructed
   with issuance disabled.
2. **The agent creates an application** with its bootstrap token. The request must name the
   bootstrap's customer. The grant is bound to the new application by a compare-and-set (`kind =
   bootstrap AND application_id IS NULL`), so it creates at most one application - also under
   concurrency. The application-bound grants and tokens are created in the same transaction.
3. **Subsequent calls** use the application-bound tokens. The agent runtime keeps them in
   `AgentCredentials` (`app/integrations/agent_credentials.py`); they never appear in tool results,
   so a model or a prompt injection cannot read, leak or swap them.
4. **The agent cannot widen anything**: no endpoint accepts scopes, extra body fields are
   rejected (422), a token whose scopes exceed its grant is rejected, and the database refuses to
   change a grant's scopes, expiry, owner, customer, kind or binding.
5. **Customer approval stays separate** (see [onboarding-flow.md](onboarding-flow.md)): no agent
   token is ever accepted on a customer route, and customer sessions are never agent credentials.
6. **Submission** needs the onboarding token with `submit_after_consent` AND the deterministic
   policy gate (stored consent bound to the exact action, disclosure, checks).
7. **Terminal state** (`OPENED`, `REJECTED`, `CANCELLED`, `FAILED`): in the transition's own
   transaction the bootstrap and onboarding grants, all their tokens and any unused customer
   handoff are revoked; the audit event records `mandate_revoked`, `tokens_revoked`,
   `handoffs_revoked`.

### Why the status grant outlives the workflow

ADR section 03, step 5: "*Agent receives only coarse status ... mandate ends after completion*".
The onboarding mandate - everything that can act - ends at completion. A separate, read-only
`status` credential lets the agent learn the coarse outcome (`OPENED`, `DECLINED`, ...) and
nothing else, until it expires. It cannot request a handoff, submit, create or read any other
application. This is a deliberate design decision for review: a stricter variant would revoke it
too, and the agent could then not report the final status.

## Verification on every request

`AuthorizationService.authenticate` (fail closed, one generic answer for every failure):

1. No `Authorization: Bearer` -> **401 `authentication_required`**, `WWW-Authenticate: Bearer realm="trusteq-agent-api"`.
2. Wrong format (not `tqa_` + 43 chars) or unknown SHA-256 hash -> **401 `invalid_token`**.
3. Known token, but any of: token revoked, grant revoked, wrong issuer, wrong audience, not yet
   valid, token expired, grant expired, token scopes beyond the grant -> **401 `invalid_token`**,
   and the precise reason is written to the audit log (`authorization.denied`, with the `jti`).
4. The route requires its scope -> **403 `insufficient_scope`** (audited).
5. The service checks resource binding: the token's agent must own the application AND the token
   must be bound to exactly that application -> otherwise **404 `not_found`**, identical to a
   missing id (IDOR-safe; audited as `application.access_denied` with reason `not_owner` or
   `outside_authorization`).
6. Business rules: the onboarding grant must still be active for the action (`mandate_inactive`,
   403, defence in depth inside the transaction), then the policy gate / state machine.

Error bodies use the normal envelope and never say *why* a token failed. Tokens, token hashes and
session secrets are never logged or audited; the request log has method, path and status only,
and uvicorn's access log (which would include query strings) is disabled.

## Scopes per route

| Route | Required token | Scope |
|---|---|---|
| `GET /v1/providers/{id}/manifest`, `GET /v1/products` | none (public) | - |
| `POST /v1/applications` | bootstrap | `create_draft` |
| `GET /v1/applications/{id}` | status (of `{id}`) | `read_status` |
| `POST /v1/applications/{id}/handoff` | onboarding (of `{id}`) | `request_handoff` |
| `POST /v1/applications/{id}/submit` | onboarding (of `{id}`) | `submit_after_consent` |
| `/v1/customer/...`, `/customer/...` | **never** an agent token (refused, audited) | - |
| `/internal/depot-provider/confirmations` | provider HMAC signature, never a bearer token | - |

## Idempotent replays and lost tokens

`POST /v1/applications` with the same agent, key and body returns the original application with
`authorization: null`: the bank stores only token hashes, so it cannot show the original tokens
again, and it deliberately does not mint new ones on a replay. The scripted agent keeps the tokens
from the first response. A lost token is re-issued by the operator:
`python -m scripts.dev_auth app-tokens --application-id <id>` (only for a non-terminal application
with an active onboarding grant; never widens scopes). The same command makes Phase 02 in-flight
applications usable after the schema upgrade.

## Implemented vs simulated vs production

| | Phase 03 (this prototype) | Production target (not built) |
|---|---|---|
| Token type | opaque, random 256-bit, server-side, SHA-256 hashed | OAuth 2.0 access tokens from a bank-approved authorization server (FAPI 2.0 profile where appropriate) |
| Issuance | trusted dev bootstrap (CLI, in-process) + bank on application creation | authorization-code / CIBA style flows with client authentication; RFC 9396 rich authorization requests for "open one depot at provider X" |
| Delegation | `bootstrap -> onboarding/status` grants in one transaction | RFC 8693 token exchange where justified (not a substitute for customer approval) |
| Agent identity | the grant names the agent; no client authentication | registered confidential clients (private_key_jwt / mTLS) |
| Sender constraint | none: a stolen token works until revoked/expired | DPoP or mTLS-bound tokens |
| Refresh | none; tokens live as long as their grant | refresh tokens with rotation, or short tokens + re-authorization |
| Revocation | server-side, immediate (every request is a DB lookup) | the same plus introspection / revocation endpoints, event distribution |
| Customer authentication | DEV-simulated sign-in | bank SCA / eID / EUDI wallet (out of scope) |
| Rate limiting, anomaly detection | none | required at the API edge |

## Tests

`tests/test_agent_authorization.py` (API boundary), `tests/test_authorization_service.py` (rules),
`tests/test_applications_api.py`, `tests/test_agent_rest_integration.py`, `tests/test_recovery.py`,
`tests/test_schema_migration.py`; targeted mutation testing: `python -m scripts.security_mutations`.

## Phase 04: three separate kinds of identity

| Actor | Credential | Where it is accepted |
|---|---|---|
| Agent | opaque bearer token (scoped, application-bound) | `/v1/applications...` only. Refused (401, audited) on every customer route, including `request-human` |
| Customer | server-side session from the SIMULATED sign-in (hashed secret; HttpOnly, SameSite=Strict cookies `trusteq_customer_session` path `/customer` and `trusteq_customer_api` path `/v1/customer`; Secure on HTTPS; server-enforced expiry and rotation) plus a per-target CSRF token; the `X-Dev-Customer-Id` header is not accepted anywhere (F-1, see [phase-07-gap-closure.md](phase-07-gap-closure.md)) | customer routes only; `request-human` (API and page button) requires the session AND its CSRF token for that application, and that the customer is the application's own customer (else 404) |
| Operator | synthetic DEV identity from `DEV_OPERATORS` (`op-reviewer-1` REVIEWER, `op-supervisor-1` SUPERVISOR) | the trusted dev console only (`scripts/dev_operator.py`); there is no operator HTTP route |

An operator principal is re-checked against the directory on every action (a self-promoted role
is refused). Agent ids, agent tokens and customer secrets are never operator credentials; the
console never echoes a value that looks like a token. Production disables the console
(`dev_operator_cli_active`).
