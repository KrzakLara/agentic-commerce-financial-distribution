# API contracts (from ADR-001 v1.1, section 05)

All routes are versioned and validated; agent-facing routes expose generic, least-privilege
status. **Implemented (Phase 02):** `GET /health`, `GET /v1/providers/{id}/manifest`, `GET /v1/products`,
`POST /v1/applications`, `GET /v1/applications/{id}`, `POST /v1/applications/{id}/handoff`,
`POST /v1/customer/handoffs/{id}/confirm`, `POST /v1/applications/{id}/submit`, plus the
non-public provider callback. See [onboarding-flow.md](onboarding-flow.md) for the whole sequence. A real deployment needs bank-approved authentication and API security
controls; tokens in the prototype are MOCK.

| Method / route | Actor | Purpose / restriction | Planned phase |
|---|---|---|---|
| `GET /health` | anyone | Liveness; no secrets | **00 (done)** |
| `GET /v1/providers/{id}/manifest` | Public agent | Official provider metadata; verified (fixture) identity and integrity | **02 (done, fixture)** |
| `GET /v1/products` | Public agent | Non-personalized offers, document version/digest, channels | **02 (done, fixture)** |
| `POST /v1/applications` | Authorized agent | Create draft with `Idempotency-Key`; agent fields are untrusted proposals | **01 (done)** |
| `GET /v1/applications/{id}` | Authorized agent | Own application only, generic state, no raw compliance detail | **01 (done)** |
| `POST /v1/applications/{id}/handoff` | Authorized agent | Short-lived one-time request for direct customer approval; digest fixed | **02 (done)** |
| `POST /v1/applications/{id}/submit` | Authorized agent | Only after server-verified consent, KYC/compliance, contract prerequisites | **02 (done)** |
| `POST /v1/customer/handoffs/{id}/confirm` | Authenticated customer (DEV sim.) | Bank-controlled ceremony binding customer, application, exact action | **02 (done, DEV sign-in)** |
| `POST /v1/customer/applications/{id}/request-human` | Authenticated customer | Creates a manual-review case without overriding KYC/risk controls | 03 |
| `GET /v1/customer/cases/{id}` | Authenticated customer | Case status | 03 |
| `POST /internal/depot-provider/confirmations` | Trusted provider (MOCK) | HMAC-authenticated callback; replay, duplicate, transition checks; the only path to `OPENED` | **02 (done, hidden, disabled by default)** |

## Conventions already in place (Phase 00)

* Errors always use one envelope:

  ```json
  {"error": {"code": "not_found", "message": "...", "correlation_id": "...", "details": null}}
  ```

  `details` is only present for validation errors (field location, message, type - never the
  submitted value).
* Every response carries `X-Correlation-ID` (client value accepted only if `[A-Za-z0-9._-]{1,64}`).
* Unhandled errors return a generic `internal_error` 500; stack traces go to logs only.
* `/docs`, `/redoc` and `/openapi.json` are disabled when `TRUSTEQ_ENVIRONMENT=production`.

## Phase 01 endpoints in detail

Caller identification is a **DEV/TEST stand-in**: header `X-Dev-Actor-Id: <agent-id>`. It is not
authentication, is disabled in production (every `/v1` route then answers 401) and is replaced
by scoped mandates in Phase 03.

### `POST /v1/applications`

```http
POST /v1/applications
X-Dev-Actor-Id: agent-a
Idempotency-Key: 6f1b1d0e-5a1c-4d62-9a39-0c1a0d4d2c11
Content-Type: application/json

{"provider_id":"fixture-bank","product_id":"depot-basic","channel_id":"direct","customer_ref":"opaque-id"}
```

```http
HTTP/1.1 201 Created
X-Correlation-ID: 9b2c...

{"application_id":"app-cda9ac6c18fd4acda6f11e8a64fb57ae","provider_id":"fixture-bank",
 "product_id":"depot-basic","channel_id":"direct","state":"DRAFT",
 "created_at":"2026-11-02T21:00:00.123456Z","updated_at":"2026-11-02T21:00:00.123456Z"}
```

| Situation | Result |
|---|---|
| New key | `201` + new DRAFT application |
| Same actor + key + identical body | `200`, header `Idempotent-Replayed: true`, the original application (current state) |
| Same actor + key, different body | `409` `idempotency_conflict` (audited) |
| Same key, different actor | `201`, a separate application (keys are per actor) |
| Unknown/extra body field (e.g. `status`) | `422` `validation_error` |
| Missing / non-UUID `Idempotency-Key` | `422` |
| Missing or malformed actor header | `401` `authentication_required` |

### `GET /v1/applications/{application_id}`

`200` with the same body shape, for the **owning actor only**. A foreign or unknown id both give
`404 not_found` with identical bodies, so ids cannot be probed (the foreign attempt is audited).

`state` is the coarse agent-visible state: `DRAFT`, `AWAITING_CUSTOMER`, `IN_PROGRESS`,
`ACTION_REQUIRED`, `OPENED`, `DECLINED`, `CANCELLED`, `FAILED` (mapping in `docs/state-machine.md`).
`customer_ref`, owner id, internal state, version and audit data are never returned.

### What does not exist (by design)

No `PUT`, `PATCH` or `DELETE`, and no `/status`, `/transition` or `/open` sub-resource. The agent
cannot change an application's state through the API; `handoff` and `submit` are *requests* that the
trusted core evaluates, and every transition is performed by bank-side code with its own authority
(`OnboardingService` -> `ApplicationService.apply_transition`).

## Phase 02 endpoints in detail

Caller identification stays a DEV/TEST stand-in: agents send `X-Dev-Actor-Id`, the simulated
customer sends `X-Dev-Customer-Id` (or uses the dev sign-in form on the confirmation page). The two
contexts are separate and never interchangeable.

### `GET /v1/providers/{provider_id}/manifest` and `GET /v1/products[?provider_id=]` (public)

```json
{"provider_id":"fixture-bank","legal_name":"Beispielbank Depot AG (MOCK)","role":"DEPOT_PROVIDER",
 "manifest_version":"2026-11-01","manifest_digest":"5441d9a6...","is_mock":true,
 "verification":"LOCAL_FIXTURE_DIGEST_PINNED",
 "channels":[{"channel_id":"direct","label":"Direktvertrieb (MOCK)","is_partner":false},
             {"channel_id":"partner-1","label":"Hausbank Musterstadt (MOCK)","is_partner":true}],
 "product_ids":["depot-basic"]}
```

```json
{"products":[{"product_id":"depot-basic","provider_id":"fixture-bank","name":"Wertpapierdepot Basis (MOCK)",
 "catalog_version":"2026.11","document_id":"kid-depot-basic","document_version":"1.0",
 "document_digest":"f67a5d99...","channel_ids":["direct","partner-1"]}]}
```

Unknown provider: `404`. A provider, product or channel that is not in the verified catalog also
cannot create an application: `POST /v1/applications` answers `422 unverified_reference`.

### `POST /v1/applications/{id}/handoff` (agent)

No body. `201`:

```json
{"handoff_id":"hnd-3f...","application_id":"app-a6...","state":"AWAITING_CUSTOMER",
 "expires_at":"2026-11-02T21:18:41Z","action_digest":"9c1e...",
 "action":{"action_type":"OPEN_DEPOT_ACCOUNT","application_id":"app-a6...","provider_id":"fixture-bank",
           "product_id":"depot-basic","channel_id":"direct","catalog_version":"2026.11",
           "document_id":"kid-depot-basic","document_version":"1.0","document_digest":"f67a5d99..."},
 "confirmation_url":"http://127.0.0.1:8000/customer/handoffs/hnd-3f...?token=<one-time challenge>"}
```

`Cache-Control: no-store`. The URL is a pointer for the customer, **not** an authorization. A new
handoff supersedes an unused earlier one. Errors: `404` (not yours), `403 mandate_inactive`,
`409 invalid_state`.

### `POST /v1/customer/handoffs/{handoff_id}/confirm` (customer, DEV sim.)

```http
POST /v1/customer/handoffs/hnd-3f.../confirm
X-Dev-Customer-Id: demo-customer-1
{"nonce":"<one-time challenge>","action_digest":"9c1e..."}
```

`200 {"handoff_id":"hnd-3f...","application_id":"app-a6...","state":"IN_PROGRESS"}`. Refusals leave
all state unchanged: `401` (no/agent context), `403 handoff_invalid | customer_mismatch |
mandate_inactive`, `409 handoff_not_active | action_digest_mismatch | invalid_state`,
`410 handoff_expired`, `422` (extra/invalid fields).

### `POST /v1/applications/{id}/submit` (agent)

No body. `202` + the generic application (state `IN_PROGRESS`; never `OPENED`). Repeat: `200`, no
second hand-over. Without verified consent and passed checks: `409 prerequisites_not_met`, nothing
else (the reasons are audited internally). After the workflow ended: `403 mandate_inactive`.

### Provider callback (not in OpenAPI)

`POST /internal/depot-provider/confirmations` with headers `X-Provider-Timestamp` and
`X-Provider-Signature: sha256=<HMAC(secret, timestamp + "." + body)>` and body
`{"application_id","provider_ref","confirmation_id","outcome":"OPENED"}`. `503` if no secret is
configured (the default), `401` bad/stale signature, `403` mismatch or wrong state, `422` malformed,
`200 {"result":"accepted"|"duplicate_ignored"}`.

## Business states (implemented in Phase 01)

`DRAFT -> AWAITING_AUTHORIZATION -> AUTHORIZED -> KYC_PENDING -> COMPLIANCE_PENDING ->
CONTRACT_PENDING -> SUBMITTED_TO_DEPOT_PROVIDER -> OPENED`; side outcomes `ACTION_REQUIRED`,
`MANUAL_REVIEW`, `REJECTED`, `CANCELLED`, `FAILED`. Only a trusted depot-provider confirmation
sets `OPENED`.
