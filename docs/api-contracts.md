# API contracts (from ADR-001 v1.1, section 05)

All routes are versioned and validated; agent-facing routes expose generic, least-privilege
status. **Implemented (Phase 02 + Phase 03 authorization):** `GET /health`,
`GET /v1/providers/{id}/manifest`, `GET /v1/products`, `POST /v1/applications`,
`GET /v1/applications/{id}`, `POST /v1/applications/{id}/handoff`,
`POST /v1/customer/handoffs/{id}/confirm`, `POST /v1/applications/{id}/submit`, plus the
non-public provider callback and HTML pages. See [onboarding-flow.md](onboarding-flow.md) for the
whole sequence and [authorization.md](authorization.md) for the token model.

Agent authorization is a **controlled development implementation** (opaque, scoped,
server-side tokens), not production OAuth 2.0 / FAPI 2.0. A real deployment needs a
bank-approved authorization server and API security controls.

| Method / route | Caller | Credential + scope | Purpose / restriction |
|---|---|---|---|
| `GET /health` | anyone | none | Liveness; no secrets |
| `GET /v1/providers/{id}/manifest` | public agent | none | Official provider metadata; verified (fixture) identity and integrity |
| `GET /v1/products` | public agent | none | Non-personalized offers, document version/digest, channels |
| `POST /v1/applications` | agent | bootstrap token, `create_draft` | Create ONE draft for the grant's customer with `Idempotency-Key`; returns the application-bound tokens once |
| `GET /v1/applications/{id}` | agent | status token of `{id}`, `read_status` | Own application only, generic state, no raw compliance detail |
| `POST /v1/applications/{id}/handoff` | agent | onboarding token of `{id}`, `request_handoff` | Short-lived one-time request for direct customer approval; digest fixed |
| `POST /v1/applications/{id}/submit` | agent | onboarding token of `{id}`, `submit_after_consent` | Only after server-verified consent, KYC/compliance, contract prerequisites |
| `POST /v1/customer/handoffs/{id}/confirm` | customer (DEV sim.) | `X-Dev-Customer-Id`; an `Authorization` header is refused | Bank-controlled ceremony binding customer, application, exact action |
| `POST /internal/depot-provider/confirmations` | trusted provider (MOCK) | HMAC signature (no bearer token) | Replay/duplicate/transition checks; the only path to `OPENED` (hidden, disabled by default) |
| `POST /v1/customer/applications/{id}/request-human`, `GET /v1/customer/cases/{id}` | customer | - | **Not implemented** - listed in plan v2 phase 3 but not part of the Phase 03 brief; deferred |

There is no endpoint that issues, exchanges, refreshes or widens tokens. The first (bootstrap)
token comes from the trusted operator CLI `python -m scripts.dev_auth bootstrap`.

## Conventions

* Errors always use one envelope:

  ```json
  {"error": {"code": "not_found", "message": "...", "correlation_id": "...", "details": null}}
  ```

  `details` is only present for validation errors (field location, message, type - never the
  submitted value).
* Every response carries `X-Correlation-ID` (client value accepted only if `[A-Za-z0-9._-]{1,64}`).
* Unhandled errors return a generic `internal_error` 500; stack traces go to logs only.
* `/docs`, `/redoc` and `/openapi.json` are disabled when `TRUSTEQ_ENVIRONMENT=production`.

## Authorization (Phase 03)

Protected agent routes require:

```http
Authorization: Bearer tqa_<43 url-safe characters>
```

| Situation | Status | `error.code` | Extra |
|---|---|---|---|
| No bearer token (also: the removed `X-Dev-Actor-Id` header, `Basic`, empty) | 401 | `authentication_required` | `WWW-Authenticate: Bearer realm="trusteq-agent-api"` |
| Malformed, unknown, modified, expired, revoked token; revoked or expired grant; wrong issuer or audience; token scopes beyond its grant | 401 | `invalid_token` | `WWW-Authenticate: ... error="invalid_token"`; the precise reason is audited only |
| Valid token without the route's scope (e.g. status token on `submit`, bootstrap token on `GET`) | 403 | `insufficient_scope` | `WWW-Authenticate: ... error="insufficient_scope"` |
| Valid token for another application, or another agent's application | 404 | `not_found` | identical to a missing id; audited |
| Bootstrap grant already used, or `customer_ref` is not the grant's customer | 403 | `mandate_inactive` | audited as `application.create_denied` |

### `POST /v1/applications`

```http
POST /v1/applications
Authorization: Bearer tqa_...            (bootstrap token: create_draft, customer demo-customer-1)
Idempotency-Key: 6f1b1d0e-5a1c-4d62-9a39-0c1a0d4d2c11
Content-Type: application/json

{"provider_id":"fixture-bank","product_id":"depot-basic","channel_id":"direct","customer_ref":"demo-customer-1"}
```

```http
HTTP/1.1 201 Created
Cache-Control: no-store

{"application_id":"app-cda9...","provider_id":"fixture-bank","product_id":"depot-basic",
 "channel_id":"direct","state":"DRAFT","created_at":"...","updated_at":"...",
 "authorization":{
   "onboarding":{"access_token":"tqa_...","token_type":"Bearer",
                 "scope":"request_handoff submit_after_consent","expires_at":"..."},
   "status":{"access_token":"tqa_...","token_type":"Bearer","scope":"read_status","expires_at":"..."}}}
```

| Situation | Result |
|---|---|
| New key, unused bootstrap grant for this customer | `201` + new DRAFT + both tokens (shown once) |
| Same agent + key + identical body | `200`, `Idempotent-Replayed: true`, the original application (current state), `authorization: null` |
| Same agent + key, different body | `409` `idempotency_conflict` (audited) |
| Same key, different agent | `201`, a separate application (keys are per agent) |
| Second application with the same bootstrap grant | `403` `mandate_inactive` (the grant is single use) |
| Unknown/extra body field (e.g. `status`, `scopes`) | `422` `validation_error` |
| Missing / non-UUID `Idempotency-Key` | `422` |

### `GET /v1/applications/{application_id}`

`200` with the public application shape (no `authorization` field), for the status token bound
to this application only. A foreign, unbound or unknown id all give `404 not_found` with
identical bodies, so ids cannot be probed (attempts on existing ids are audited).

`state` is the coarse agent-visible state: `DRAFT`, `AWAITING_CUSTOMER`, `IN_PROGRESS`,
`ACTION_REQUIRED`, `OPENED`, `DECLINED`, `CANCELLED`, `FAILED` (mapping in `docs/state-machine.md`).
`customer_ref`, owner id, internal state, version, grants and audit data are never returned. The
status token keeps working after `OPENED` (read-only) until it expires.

### `POST /v1/applications/{id}/handoff` (onboarding token)

No body. `201`, `Cache-Control: no-store`:

```json
{"handoff_id":"hnd-3f...","application_id":"app-a6...","state":"AWAITING_CUSTOMER",
 "expires_at":"2026-11-02T21:18:41Z","action_digest":"9c1e...",
 "action":{"action_type":"OPEN_DEPOT_ACCOUNT","application_id":"app-a6...","provider_id":"fixture-bank",
           "product_id":"depot-basic","channel_id":"direct","catalog_version":"2026.11",
           "document_id":"kid-depot-basic","document_version":"1.0","document_digest":"f67a5d99..."},
 "confirmation_url":"http://127.0.0.1:8000/customer/handoffs/hnd-3f...?token=<one-time challenge>"}
```

The URL is a pointer for the customer, **not** an authorization. A new handoff supersedes an
unused earlier one. Errors: authorization errors above, `409 invalid_state`.

### `POST /v1/customer/handoffs/{handoff_id}/confirm` (customer, DEV sim.)

```http
POST /v1/customer/handoffs/hnd-3f.../confirm
X-Dev-Customer-Id: demo-customer-1
{"nonce":"<one-time challenge>","action_digest":"9c1e..."}
```

`200 {"handoff_id":"hnd-3f...","application_id":"app-a6...","state":"IN_PROGRESS"}`. Refusals leave
all state unchanged: `401` (no customer context, or ANY `Authorization` header - an agent token is
never a customer credential; audited as `agent_credentials_presented`), `403 handoff_invalid |
customer_mismatch | mandate_inactive` (the application's onboarding grant ended), `409
handoff_not_active | action_digest_mismatch | invalid_state`, `410 handoff_expired`, `422`.

### `POST /v1/applications/{id}/submit` (onboarding token)

No body. `202` + the generic application (state `IN_PROGRESS`; never `OPENED`). Repeat: `200`, no
second hand-over. If a crash left the mandatory checks unfinished after the customer's
confirmation, they are resumed first (bounded, idempotent). Without verified consent and passed
checks: `409 prerequisites_not_met`, nothing else (the reasons are audited internally). After the
workflow ended: `401 invalid_token` (the onboarding token was revoked).

### Provider callback (not in OpenAPI)

`POST /internal/depot-provider/confirmations` with headers `X-Provider-Timestamp` and
`X-Provider-Signature: sha256=<HMAC(secret, timestamp + "." + body)>` and body
`{"application_id","provider_ref","confirmation_id","outcome":"OPENED"}`. `503` if no secret is
configured (the default), `401` bad/stale signature (a bearer token does not help), `403` mismatch
or wrong state, `422` malformed, `200 {"result":"accepted"|"duplicate_ignored"}`.

## Contract changes in Phase 03 (vs Phase 02)

| Before (Phase 02) | Now (Phase 03) |
|---|---|
| `X-Dev-Actor-Id: <any id>` named the agent | removed; `Authorization: Bearer` with a server-issued token |
| create response = application only | + `authorization` (two tokens) on 201, `null` on replay; `Cache-Control: no-store` |
| one actor id worked on all of its applications | each token is bound to exactly one application and one scope set |
| after `OPENED` / expiry: `403 mandate_inactive` on handoff/submit | `401 invalid_token` (the credential itself is revoked/expired); reads keep working with the status token |
| customer confirm accepted a request that also carried agent headers | any `Authorization` header is refused on customer routes (401) |
| HTML form posts: Origin check + SameSite cookie | + session-bound CSRF tokens, login CSRF, `Sec-Fetch-Site`, server-side session expiry |

## Business states (implemented in Phase 01)

`DRAFT -> AWAITING_AUTHORIZATION -> AUTHORIZED -> KYC_PENDING -> COMPLIANCE_PENDING ->
CONTRACT_PENDING -> SUBMITTED_TO_DEPOT_PROVIDER -> OPENED`; side outcomes `ACTION_REQUIRED`,
`MANUAL_REVIEW`, `REJECTED`, `CANCELLED`, `FAILED`. Only a trusted depot-provider confirmation
sets `OPENED`.
