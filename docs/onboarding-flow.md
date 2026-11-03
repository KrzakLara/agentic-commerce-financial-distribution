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
| Provider catalog (fixture) | synthetic provider, products, channels and documents; verified against the bank's register, pinned digests, a DEV-key signature, domain/endpoint, capabilities, channels, version and validity (Phase 04) | no BaFin/LEI lookup, no network fetch, no asymmetric signature, no TLS binding |
| MOCK KYC | returns a configured outcome (default `PASS`), reference `MOCK-KYC-...` | no identity verification |
| MOCK compliance | returns a configured outcome (default `PASS`); `REVIEW_REQUIRED` for customer refs starting `synthetic-review-` (Phase 04 scenario switch); resolved only by the evidence category `SOURCE_OF_FUNDS_CONFIRMATION` on a re-evaluation | no AML, sanctions or suitability checks |
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

## Known limitations (after Phase 04)

* The customer sign-in and the JSON ceremony's `X-Dev-Customer-Id` header are DEV simulations.
* Agent tokens are bearer tokens: a stolen token works until it expires or is revoked (no DPoP /
  mTLS binding); there is no refresh flow (a lost token is re-issued by an operator).
* The callback shared secret is a mock credential; a real provider needs mutual TLS / signed messages.
* Phase 04: a non-`PASS` check opens a review case; operators work through a DEV console with
  synthetic identities (no SSO/RBAC, no four-eyes beyond supervisor-only rejection).
* Phase 04: check results are append-only attempts; the latest attempt decides.
* Phase 04: manifests are signed with a shared DEV HMAC key (a symmetric stand-in), fixture-local.
* No rate limiting; a flood of requests with a once-valid token writes one audit row each.
* Single-writer SQLite; one hand-written schema upgrade step instead of a migration tool.

## Phase 04: verified discovery, disclosure binding, partner attribution

**How the bank verifies a provider** (`app/integrations/catalog_fixture.py`, at every start; fail
closed): the provider must be in the bank's own register; the manifest digest must be the one
the bank pinned; every referenced document must match its digest; the manifest must carry a valid
signature by exactly the trust anchor the register names for this provider; official name, role,
registry reference, verified domain and API endpoint must equal the register (an `https`
endpoint on the verified domain, and on the approved list); capabilities and channels must be
within what the register approved; the version must not be older than the approved minimum; and
the manifest must be inside its validity window - re-checked on every use
(`app/services/catalog_policy.py`). A registry reference (LEI) or a name alone proves nothing:
anybody can copy them into a manifest.

**How disclosure versions are bound:** the approved action contains the product version, the
disclosure document id/version/digest, the `terms_digest` (fees, conditions, risk information,
sources, dates) and the partner. At confirmation the bank re-derives the action from the live
catalog and refuses if anything differs; the disclosure receipt then records provider, product,
product version, document version/digest, terms digest, action digest and the confirmation time.
At submit the bank checks again that the live catalog still offers exactly the approved action.

**How partner attribution is validated:** the agent can only name a channel id. The channel must
be registered for the provider, active (also in the bank's partner register - an ended
partnership deactivates it), serve the product, and name a registered partner *bank* (never a
fund provider or the depot provider itself). The partner comes from the registry, is shown on the
confirmation page, is bound into the approved action and is recorded with the consent in the
append-only `channel_attributions` table. Unknown fields such as `partner_id` or
`commission_recipient` in the agent's request are rejected.

## Phase 04: the three demo outcomes

```text
HAPPY          as above: ... -> SUBMITTED_TO_DEPOT_PROVIDER -> OPENED (provider callback)

DENIED         agent proposes a fake provider / fabricated or inactive channel / its own
               commission recipient -> 422; confirms for the customer, sets OPENED, submits early,
               acts on the customer channel, uses a read-only or forged token -> 401/403/404/405/409;
               fake/tampered/expired/substituted manifests never load.  Every refusal is audited,
               the application's business state does NOT change (denied is not REJECTED), and the
               real customer can still continue.

MANUAL REVIEW  customer confirms -> KYC PASS -> MOCK compliance REVIEW_REQUIRED
               -> MANUAL_REVIEW (resume_status COMPLIANCE_PENDING) + review case (internal reason)
               agent sees ACTION_REQUIRED; submit 409; a signed provider callback is refused
               customer (bank page, session + CSRF): presses "Mit einer Person sprechen"
               -> "Anfrage erhalten" (joins the case; the same neutral answer as always)
               operator: request-info -> (RE_EVALUATE refused while waiting; REJECT needs a
               supervisor) -> add-evidence SYN-SOF-0001 -> decide RE_EVALUATE
               -> MANUAL_REVIEW -> COMPLIANCE_PENDING (OPERATOR) -> compliance attempt 2 PASS
               -> CONTRACT_PENDING -> agent submits -> provider callback -> OPENED
```

### Asking for a person: the "Mit einer Person sprechen" button

The signed-in (simulated) customer sees the button on the bank-controlled review page - before
the contract - and again on the confirmation page - after it (ADR section 09: "before contract;
sometimes after"). It sends one of four fixed categories (no free text) to
`POST /customer/applications/{id}/request-human`; JSON clients use the approved API path
`POST /v1/customer/applications/{id}/request-human`. Both go through ONE gate, in this order:

1. no `Authorization` header - an agent credential is refused (401) and audited;
2. not a cross-site browser request (`Sec-Fetch-Site` / `Origin`; 403);
3. an ACTIVE server-side customer session. The (simulated) sign-in puts the SAME session secret
   into two HttpOnly, SameSite=Strict cookies (Secure on HTTPS): `trusteq_customer_session`
   (path `/customer` - the pages and the button) and `trusteq_customer_api` (path
   `/v1/customer` - the customer JSON API). The server stores only the hash and decides expiry
   (15 minutes) and revocation (a new sign-in rotates the session; both cookies die with it).
   Missing, unknown, expired or revoked: 401;
4. a CSRF token `HMAC(session secret, "csrf|human|<application_id>")`: the bank page embeds it
   in the form (field `csrf_token`); a JSON client sends it as `X-CSRF-Token`. It is useless for
   another session, another application or another purpose (403 `csrf_token_invalid`);
5. then the workflow: only the application's OWN customer (else 404, same answer as an unknown
   id), at most one active case (a repeat joins it), the same neutral acknowledgement every time.

The self-asserted `X-Dev-Customer-Id` header is NOT a credential here (it remains only for the
Phase 02 JSON confirmation ceremony). Every refusal is audited as `review.human_request_denied`
with a machine reason only (`agent_credentials_presented`, `cross_site_request`,
`no_customer_session`, `csrf_token_invalid`, `not_the_customer`, `unknown_application`) - never
with compliance detail.

The application stays where the checks put it: a request pauses an application that is still
in a pausable step (`MANUAL_REVIEW`, remembering the step), joins a case that compliance already
opened, and never pauses, skips or replaces a check - only an operator decision can continue,
and OPENED still needs the provider's signed confirmation.

### Rules of the human review

* One active case per application (database-enforced). A request while a case is open joins it.
* The customer's answer is identical whether or not a case existed and whatever its reason.
* Decisions are a fixed list: `RE_EVALUATE` (check cases; waits while requested information is
  outstanding), `RESUME` (customer cases), `REJECT` (supervisor), `ACKNOWLEDGE` (cases that do
  not hold the workflow, e.g. after submission). None of them leads to `OPENED`.
* `RE_EVALUATE` never edits a result: the check runs again as attempt n+1 with the recorded,
  permitted evidence categories (`SOURCE_OF_FUNDS_CONFIRMATION`, `IDENTITY_REVERIFICATION`,
  `ADDRESS_CONFIRMATION`, `CUSTOMER_CLARIFICATION`; synthetic `SYN-...` references only), and
  the deterministic result decides - including parking again with a new case.
* Every step is audited (`review.case_opened`, `review.human_requested`,
  `review.information_requested`, `review.evidence_recorded`, `review.decision_denied`,
  `review.decided`, `check.re_evaluated`).

Operator console (DEV only, refuses in production; no HTTP route):

```powershell
.\.venv\Scripts\python.exe -m scripts.dev_operator --operator op-reviewer-1 list
.\.venv\Scripts\python.exe -m scripts.dev_operator --operator op-reviewer-1 show --case <case_id>
.\.venv\Scripts\python.exe -m scripts.dev_operator --operator op-reviewer-1 request-info --case <case_id> --category SOURCE_OF_FUNDS_CONFIRMATION
.\.venv\Scripts\python.exe -m scripts.dev_operator --operator op-reviewer-1 add-evidence --case <case_id> --category SOURCE_OF_FUNDS_CONFIRMATION --reference SYN-SOF-0001
.\.venv\Scripts\python.exe -m scripts.dev_operator --operator op-reviewer-1 decide --case <case_id> --decision RE_EVALUATE
```
