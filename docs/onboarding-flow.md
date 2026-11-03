# Simulated end-to-end onboarding (Phase 02, scoped authorization since Phase 03)

> **Everything here is a prototype with synthetic data.** KYC, compliance/AML, contract
> signing and the depot provider are **MOCK**. The customer sign-in is a **development-only
> simulation**. Agent authorization is a **controlled development implementation**, not
> production OAuth/FAPI. Nothing opens a real account, verifies a real identity or creates a
> legally valid contract. Architecture: ADR-001 v1.1 (unchanged).

Principle: *LLM proposes; policy decides; customer authorizes; bank executes.*

## Sequence

Before step 3 a trusted operator issues the agent's narrow bootstrap grant
(`python -m scripts.dev_auth bootstrap`; no HTTP endpoint): `create_draft`, one application, one
named customer. Every agent call below carries `Authorization: Bearer <token>`
([authorization.md](authorization.md)).

```text
 AGENT (T0b, untrusted)        BANK (T1/T2)                    CUSTOMER (T0a)        MOCK PROVIDER (T3)
        |                           |                                |                      |
 1 GET /v1/providers/{id}/manifest ->  verified fixture metadata     |                      |
 2 GET /v1/products            ->     product + disclosure version   |                      |
 3 POST /v1/applications       ->     DRAFT; bootstrap grant bound   |                      |
   (bootstrap token)                  (single use); onboarding +     |                      |
                                      status tokens returned once    |                      |
 4 POST /{id}/handoff          ->     one-time challenge, action     |                      |
        |                             digest; DRAFT->AWAITING_AUTH.  |                      |
        |  -- link (pointer, not an authorization) --------------->  |                      |
        |                           |   5 opens page: asks for sign-in (reveals nothing)    |
        |                           |   6 (DEV) sign-in, reviews provider/product/channel/  |
        |                           |     action/disclosure v1.0, presses Confirm           |
        |                           | <- POST confirm (nonce + digest + customer identity)  |
        |                           |   7 verify challenge, identity, digest, catalog,      |
        |                           |     mandate; consume handoff; receipts;               |
        |                           |     AWAITING_AUTH.->AUTHORIZED                         |
        |                           |   8 MOCK KYC PASS -> MOCK compliance PASS ->           |
        |                           |     MOCK contract recorded  => CONTRACT_PENDING        |
 9 POST /{id}/submit           ->     policy gate (evidence, not labels) ->                   |
   (onboarding token)                 hand-over; ->SUBMITTED_TO_DEPOT_PROVIDER ------------> |
        |                           |                                   10 signed "opened" -> |
        |                           | <- POST /internal/depot-provider/confirmations ------- |
        |                           |  11 verify signature, application + provider ref,      |
        |                           |     state, replay; ->OPENED; bootstrap + onboarding    |
        |                           |     grants, their tokens, open handoffs REVOKED         |
12 GET /{id}  ->  OPENED (status token: read-only, generic status reported by the backend)
13 POST /{id}/submit -> 401 invalid_token (its onboarding privileges are gone)
```

Steps 4 and 9 use the application's **onboarding** token (`request_handoff`,
`submit_after_consent`); every status read uses the separate read-only **status** token.

The agent only ever *requests* (steps 4 and 9). Every state change is performed by trusted
code with its own authority; the agent never holds the customer's or the provider's.

## Who may take which transition

| # | Transition | Authority | Triggered by |
|---|---|---|---|
| 4 | DRAFT -> AWAITING_AUTHORIZATION | `SYSTEM_POLICY` | handoff created (the agent's *request*) |
| 7 | AWAITING_AUTHORIZATION -> AUTHORIZED | `CUSTOMER_CHANNEL` | the customer's confirmation |
| 8 | AUTHORIZED -> KYC_PENDING -> COMPLIANCE_PENDING -> CONTRACT_PENDING | `SYSTEM_POLICY` | deterministic checks (MOCK results) |
| 9 | CONTRACT_PENDING -> SUBMITTED_TO_DEPOT_PROVIDER | `SYSTEM_POLICY` | `submit`, only if the policy gate allows |
| 11 | SUBMITTED_TO_DEPOT_PROVIDER -> OPENED | `DEPOT_PROVIDER_CONFIRMATION` | the signed provider callback |
| - | any check that is not `PASS` -> MANUAL_REVIEW | `SYSTEM_POLICY` | fail closed; nothing advances |

Edges and authorities are enforced by `app/domain/workflow.py` (see `docs/state-machine.md`).
The agent authority appears on **no** edge.

## The secure customer handoff

| Property | How it is achieved |
|---|---|
| Bound to the application | `application_id` is inside the approved action and the handoff row |
| Bound to the exact action | SHA-256 digest of `{application, provider, product, channel, customer ref, catalog version, disclosure document id/version/digest}` |
| Bound to the customer | the authenticated customer id must equal the application's customer reference; stored when consumed |
| Single use | `consume` is a compare-and-set (`consumed_at IS NULL`); a second use gets 409 |
| Expiring | `handoff_ttl_seconds` (default 600); at or after expiry the answer is 410 |
| One active handoff per application | a new request supersedes the old one; a partial unique index enforces it in the database |
| Challenge protected | only the SHA-256 of the nonce is stored; compared in constant time; checked *first*, so a caller without it learns nothing |
| Not changed behind the customer's back | the action is re-derived from the live catalog at confirmation; if it changed (e.g. new disclosure version) the confirmation is refused |
| Link/QR is not authentication | the page requires the customer's own sign-in before showing anything; the nonce alone never confirms |
| Agent cannot confirm | any request with an `Authorization` header is refused (401) on every customer route - even next to a customer context - and audited as `agent_credentials_presented`; the agent's tool set has no confirm tool |
| Session-bound CSRF (Phase 03) | the confirm form carries `csrf_token = HMAC-SHA256(session secret, "confirm" + handoff id)`: useless in another session or for another handoff; missing/invalid -> 403, audited (`csrf_token_invalid`), handoff untouched |
| Login CSRF (Phase 03) | the sign-in form carries a token bound to a short-lived pre-session cookie (HttpOnly, SameSite=Strict, 10 min); without it no session is created |
| Cross-site requests (Phase 03) | form posts with `Sec-Fetch-Site` other than `same-origin`/`none`, or a foreign / `null` `Origin`, are refused (defence in depth next to the CSRF token) |
| Server-side session (Phase 03) | random secret in an HttpOnly, SameSite=Strict cookie on `/customer` (Secure over HTTPS); only its SHA-256 is stored; expiry (`customer_session_ttl_seconds`, 900) and revocation are enforced by the server; signing in again revokes the previous session |

Failure behaviour (all fail closed, change no state, and are audited):

| Situation | Result |
|---|---|
| Unknown handoff or wrong nonce | 403 `handoff_invalid` (indistinguishable) |
| Used or superseded | 409 `handoff_not_active` |
| Expired | 410 `handoff_expired` |
| Different customer | 403 `customer_mismatch` (does *not* burn the handoff) |
| Digest differs / action no longer current | 409 `action_digest_mismatch` |
| The application's onboarding grant expired or was revoked (customer confirm) | 403 `mandate_inactive` |
| The agent's token expired or was revoked (agent handoff / submit) | 401 `invalid_token` |
| Application no longer awaiting | 409 `invalid_state` |
| Missing / invalid / foreign-session CSRF token (HTML form) | 403 |
| Session expired, revoked or forged | 401 (the page shows the sign-in form again) |

### DEV-only simulated customer sign-in (limitation)

A real deployment needs the customer to authenticate to the bank (eID, EUDI wallet, online
banking) in a channel the bank controls. This prototype has **none of that**. It provides a
clearly labelled stand-in: the dev sign-in form (which now creates a server-side session, see
above) and, for the JSON ceremony, the header `X-Dev-Customer-Id`. **Anyone who can use them can
claim to be any customer** - preventing exactly that is the job of the real identity channel.
Both are disabled in production (every customer route then answers 401). What *is* real: the
customer credential is completely separate from the agent's bearer tokens and never
interchangeable with them.

## The deterministic policy gate (`app/services/policy.py`)

`submit` is allowed only if *all* of these hold, checked against **stored evidence**, not just the
state label: an active mandate with the submit scope; state `CONTRACT_PENDING`; a consent receipt
whose digest equals the consumed handoff's and whose customer is the application's customer; the
matching disclosure acknowledgement (document id, version, digest); and `PASS` results for KYC,
compliance and the contract step. Otherwise `409 prerequisites_not_met` with no detail; the exact
missing items go to the audit log only. No LLM output is read anywhere in this path.

## Mock services

| Service | What it does | What it does NOT do |
|---|---|---|
| Provider catalog (fixture) | verified, pinned-digest synthetic provider/product/channels/disclosure | no BaFin/LEI lookup, no signed manifest, no TLS binding |
| MOCK KYC | returns a configured outcome (default `PASS`), reference `MOCK-KYC-...` | no identity verification |
| MOCK compliance | returns a configured outcome (default `PASS`) | no AML, sanctions or suitability checks |
| MOCK contract | records that the authenticated simulated customer confirmed the contract and disclosure version | no legally valid signature |
| MOCK depot provider | accepts the hand-over (`MOCK-DEPOT-...`); its signed callback is the only way to `OPENED` | no real custody or account |

Every mock result carries `mock=true`. Change a mock in `app/container.py` (or pass a container to
`create_app`) to see another path, e.g. `MockComplianceProvider(CheckOutcome.REVIEW_REQUIRED)` ends
in `MANUAL_REVIEW` and `submit` is refused.

## The provider callback (T3 -> T2)

`POST /internal/depot-provider/confirmations` is not part of the public OpenAPI. It exists only when
`TRUSTEQ_MOCK_PROVIDER_CALLBACK_SECRET` is set (production refuses to start with it set). It
accepts one message kind - "opened" - authenticated by HMAC-SHA256 over `timestamp.body` with a
5-minute freshness window. The service additionally requires: known application, matching
`provider_ref` recorded at submission, state `SUBMITTED_TO_DEPOT_PROVIDER`, and a never-seen
`confirmation_id`. An exact repeat is acknowledged (`duplicate_ignored`) with no further effect.

## Mandate and token lifecycle (Phase 03)

Grants are real server-side authorization records and every agent request is verified against
them ([authorization.md](authorization.md)): a single-use bootstrap grant (create one draft for one
customer), an onboarding grant (handoff + submit) and a read-only status grant per application,
each with opaque bearer tokens stored as hashes. At any terminal state the bootstrap and onboarding
grants, all their tokens and any open handoff are revoked atomically with the transition (audit:
`mandate_revoked`, `tokens_revoked`, `handoffs_revoked`); the status grant lets the agent read the
coarse outcome until it expires.

## Recovery after an interrupted process (Phase 03)

If the process stops after the customer's confirmation but before the mandatory checks finished,
the consent is durable and the application waits in `AUTHORIZED`, `KYC_PENDING`,
`COMPLIANCE_PENDING` or `CONTRACT_PENDING` without contract evidence. The agent's next authorized
`submit` (onboarding token, active grant, **stored customer consent**) first resumes the pipeline
with `advance_checks`:

* bounded (a fixed number of steps) and idempotent: a provider is only called for a check that has
  no recorded evidence yet; recorded results are reused, never re-requested;
* concurrency-safe: unique evidence per check + compare-and-set transitions; a run that loses a
  race rolls back and re-reads;
* fail closed: a provider that is still unavailable is audited (`workflow.resumed`, outcome
  `failure`) and the policy gate refuses the submit (409); a check that does not pass still parks
  the application in `MANUAL_REVIEW`;
* the irreversible external step (hand-over to the depot provider) is NOT part of the resume; it
  only happens inside `submit` after the policy gate, and the mock provider is idempotent per
  application (a real provider must deduplicate on the application id / consent receipt id too).

There is deliberately no background worker or startup sweep: recovery happens on the next
legitimate request, so nothing runs without an authorized caller. An application whose agent never
comes back stays parked - visible to operators, not lost.

## Known limitations (after Phase 03)

* The customer sign-in and the JSON ceremony's `X-Dev-Customer-Id` header are DEV simulations.
* Agent tokens are bearer tokens: a stolen token works until it expires or is revoked (no DPoP /
  mTLS binding); there is no refresh flow (a lost token is re-issued by an operator).
* The callback shared secret is a mock credential; a real provider needs mutual TLS / signed messages.
* A non-`PASS` check parks the application in `MANUAL_REVIEW` (conservative). There is no operator
  workflow, review case, `request-human` or `REJECTED` decision logic yet.
* One check result per kind per application; no re-check history.
* Catalog verification is a pinned local fixture, not signed manifests or a trust registry (Phase 04).
* No rate limiting; a flood of requests with a once-valid token writes one audit row each.
* Single-writer SQLite; one hand-written schema upgrade step instead of a migration tool.
