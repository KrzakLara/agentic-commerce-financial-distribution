# Simulated end-to-end onboarding (Phase 02)

> **Everything here is a prototype with synthetic data.** KYC, compliance/AML, contract
> signing and the depot provider are **MOCK**. The customer sign-in is a **development-only
> simulation**. Nothing opens a real account, verifies a real identity or creates a legally
> valid contract. Architecture: ADR-001 v1.1 (unchanged).

Principle: *LLM proposes; policy decides; customer authorizes; bank executes.*

## Sequence

```text
 AGENT (T0b, untrusted)        BANK (T1/T2)                    CUSTOMER (T0a)        MOCK PROVIDER (T3)
        |                           |                                |                      |
 1 GET /v1/providers/{id}/manifest ->  verified fixture metadata     |                      |
 2 GET /v1/products            ->     product + disclosure version   |                      |
 3 POST /v1/applications       ->     DRAFT + onboarding mandate     |                      |
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
        |                             hand-over; ->SUBMITTED_TO_DEPOT_PROVIDER ------------> |
        |                           |                                   10 signed "opened" -> |
        |                           | <- POST /internal/depot-provider/confirmations ------- |
        |                           |  11 verify signature, application + provider ref,      |
        |                           |     state, replay; ->OPENED; mandate revoked           |
12 GET /{id}  ->  OPENED (generic status reported by the backend)
```

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
| Agent cannot confirm | the agent's credential is rejected (401) on every customer route, and its tool set has no confirm tool |

Failure behaviour (all fail closed, change no state, and are audited):

| Situation | Result |
|---|---|
| Unknown handoff or wrong nonce | 403 `handoff_invalid` (indistinguishable) |
| Used or superseded | 409 `handoff_not_active` |
| Expired | 410 `handoff_expired` |
| Different customer | 403 `customer_mismatch` (does *not* burn the handoff) |
| Digest differs / action no longer current | 409 `action_digest_mismatch` |
| Mandate expired or revoked | 403 `mandate_inactive` |
| Application no longer awaiting | 409 `invalid_state` |

### DEV-only simulated customer sign-in (limitation)

A real deployment needs the customer to authenticate to the bank (eID, EUDI wallet, online
banking) in a channel the bank controls. This prototype has **none of that**. It provides a
clearly labelled stand-in: a separate header `X-Dev-Customer-Id` or a dev sign-in form that sets a
signed, HttpOnly, SameSite=Strict cookie. **Anyone who can use it can claim to be any customer.**
It is disabled in production (every customer route then answers 401) and is a different context
from the agent's `X-Dev-Actor-Id`.

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

## Mandate lifecycle

An onboarding mandate (audience, scopes, `jti`, expiry) is issued with each application and
checked before every agent *action* (`handoff`, `submit`). It is revoked on any terminal state; at
`OPENED` the transition event records `mandate_revoked: true`. Read-only status stays available to
the owner. These are **mock claims**, not tokens; verification of audience/`jti`/replay on every
request is Phase 03.

## Known limitations of Phase 02

* DEV/TEST caller identities (agent and customer) are not authentication.
* The callback shared secret is a mock credential; a real provider needs mutual TLS / signed messages.
* If the process dies after the customer's confirmation but before the checks finish, the
  application stays at the last committed state (submit is blocked: fail closed);
  `OnboardingService.advance_checks` resumes it idempotently, but nothing calls it automatically yet.
* A non-`PASS` check parks the application in `MANUAL_REVIEW` (conservative). There is no operator
  workflow, review case or `REJECTED` decision logic yet (Phase 04).
* One check result per kind per application; no re-check history.
* Catalog verification is a pinned local fixture, not signed manifests or a trust registry (Phase 04).
* The CSRF defence on the form posts is an `Origin` check plus SameSite=Strict, not a token.
* Single-writer SQLite; no migration tool beyond a schema-version marker.
