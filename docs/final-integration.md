# Final functional integration before the presentation

The gap-closure pass ([phase-07-gap-closure.md](phase-07-gap-closure.md)) built useful components, but several stayed
beside the journey. This pass connects them to the real application. That document stays as the record of that pass;
this one is current where they differ.

Everything is a prototype with synthetic data. It opens no real account, creates no legally valid contract and claims no
regulatory compliance.

## 1. Status at a glance

| Capability | Status | In one line |
|---|---|---|
| Tax information | **IMPLEMENTED AND INTEGRATED** | Customer page and JSON API, deterministic validation, required before the bank accepts a confirmation |
| Knowledge-and-experience questionnaire | **IMPLEMENTED AND INTEGRATED** | Customer page and JSON API, deterministic result, warning and acknowledgement. Never blocks the account |
| Versioned provider adapter (v1/v2) | **IMPLEMENTED AND INTEGRATED** | The default path of every submission, with an explicit fallback policy |
| Approved product claims at the agent's tool boundary | **IMPLEMENTED AND INTEGRATED** | Structured facts with source and version in `get_official_product` |
| Claim check | **IMPLEMENTED AND INTEGRATED** as an API; not applied to LLM text | `POST /v1/claims/verify`. The bank cannot check a third-party agent's wording |
| Deterministic product answers | **IMPLEMENTED BUT ISOLATED** from the LLM agent | `ProductInformationAgent`, used by the integrated demo |
| OIDC boundary | **IMPLEMENTED BUT ISOLATED** | Configuration, PKCE, claim checks and fail-closed mode. Never talked to a provider |
| KYC and signing typed boundaries | **IMPLEMENTED BUT ISOLATED** | `regulated_boundaries.py`, unchanged |
| Execution-only review | **IMPLEMENTED BUT ISOLATED** | `review_execution_only`. There is no order step |
| Customer sign-in | **SIMULATED** | The labelled DEV sign-in accepts any synthetic ID |
| KYC, compliance, contract, depot provider, operators | **SIMULATED** | Mocks, `mock=true`, HMAC keys in the repository |
| External identity verification | **BLOCKED BY EXTERNAL PROVIDER** | No test identity provider, client registration or keys exist for this project |
| Regulated KYC, QTSP signing, tax-authority checks | **BLOCKED BY EXTERNAL PROVIDER** | No contract or sandbox. None was invented |
| Bank-grade identity, FAPI, QTSP, evidence store, rollout | **PROPOSED FOR 2030** | [technical-concept-2030.md](technical-concept-2030.md) |

## 2. Before and after

| Situation | Before | After |
|---|---|---|
| Customer opens the review page | Confirm is there at once | Tax and knowledge cards first. Confirm appears when the information exists |
| Client posts a confirmation without that information | 200, consent recorded | 409, nothing recorded, audited with the missing items |
| Customer enters tax data | No page, no route, not in the flow | Field statuses (accepted, missing, invalid), and no value is echoed |
| Real-looking 11-digit tax ID | Refused by an isolated function | Refused in the journey: 422 on the page, value not echoed, not stored |
| Answers show a knowledge gap | Isolated function result | Warning on the page. The customer acknowledges and may continue. The account is not refused |
| Agent asks for a fee | Fee data without a stated approval | A bank-approved claim: value, statement, source, version, digest |
| Agent asks about a fee the source lacks | Up to the agent | Scripted path declines. The bank publishes no such claim |
| Agent submits | `MockDepotProvider` answers `accepted` | Versioned adapter validates the schema first, audit records the version |
| Provider retires v2 | Not connected to the application | v1 only if the provider names it and bank policy allows it, recorded as `fallback_from` |
| Provider answers `opened: true` | Not connected to the application | Refused as a contract violation, nothing advances |

## 3. The integrated journey

No state or transition was added. The information is a subflow bound to the live handoff, on the bank's channel.

| # | Step | Status | Where |
|---|---|---|---|
| 1 | Agent discovers the verified fictional provider | IMPLEMENTED AND INTEGRATED, with a SIMULATED trust root | `GET /v1/providers/{id}/manifest` |
| 2 | Agent retrieves approved product information | IMPLEMENTED AND INTEGRATED | `get_official_product`, `approved-claims` |
| 3 | Agent prepares the application | IMPLEMENTED AND INTEGRATED | `POST /v1/applications`, `/handoff` |
| 4 | Customer signs in | **SIMULATED** | DEV sign-in form |
| 5 | Customer reviews product, fees, terms, risks | IMPLEMENTED AND INTEGRATED | Review page |
| 6 | Customer supplies synthetic tax information | IMPLEMENTED AND INTEGRATED | `POST /customer/handoffs/{id}/tax` |
| 7 | Customer completes the assessment | IMPLEMENTED AND INTEGRATED | `POST /customer/handoffs/{id}/assessment` |
| 8 | Bank validates and applies deterministic controls | IMPLEMENTED AND INTEGRATED | `evaluate_customer_information` |
| 9 | Customer independently authorizes | IMPLEMENTED AND INTEGRATED, on a SIMULATED identity | Confirm form: session, CSRF, one-time challenge |
| 10 | Mock KYC, compliance, contract run | **SIMULATED** | `advance_checks` |
| 11 | Versioned provider adapter processes the request | IMPLEMENTED AND INTEGRATED, against a SIMULATED provider | `VersionedDepotAdapter` |
| 12 | Signed confirmation produces `OPENED` | IMPLEMENTED AND INTEGRATED, with a SIMULATED signer | `POST /internal/depot-provider/confirmations` |
| 13 | Audit records the significant transitions | IMPLEMENTED AND INTEGRATED | `audit_events` |

Run it: `.\.venv\Scripts\python.exe -m scripts.demo_integrated`, with `--profile novice` or `--profile incomplete` for the
other results and `--depot-scenario NAME` for a failing provider. Instructions:
[presentation/phase-07-demo-runbook.md](presentation/phase-07-demo-runbook.md).

### The switch for the old scenarios

`TRUSTEQ_CUSTOMER_INFORMATION_REQUIRED` defaults to `true`, so the documented server runs the integrated journey. The three
P0 demos, `demo_llm`, the scripted evaluation and the older tests set it to `false`, because they predate the step and
their purpose is unchanged. Each P0 demo prints a note saying so. New tests switch it on and prove the gate.

## 4. Customer identity: blocked, and what exists

**No legitimate external identity provider is available to this project.** The environment and `.env` hold no issuer, client
ID or keys, and registering with a vendor was out of scope. None was invented, and no fake signed token exists.

What exists (`app/integrations/customer_identity.py`, 100 lines, tested, never run against a provider):

- `TRUSTEQ_CUSTOMER_IDENTITY_MODE=oidc` turns the DEV sign-in off, and every customer page and route fails closed.
- Configuration check that reports missing settings by name, never a value.
- PKCE (S256), state and nonce generation.
- Deterministic checks of issuer, audience, authorized party, expiry, issue time, nonce and subject.
- The only verifier shipped refuses everything, so an unverified token can never pass. Verifying a signature requires a
  provider's keys, and a JWT library, which this pass did not add.

A login at a test provider would prove authentication at that provider. It would not be German eID identification, regulated KYC
or bank-grade assurance.

**F-1 is not resolved.** The header bypass stays closed. While the DEV sign-in is on, anyone who holds the link and the customer
reference can sign in as that customer, supply the information and confirm. `OPENED` still needs the provider's signature.

## 5. Tax information

Reuses `validate_tax_information` unchanged. Bound to the signed-in customer, the application and the live handoff.

| Level | Meaning here |
|---|---|
| Customer-provided | Entered on the bank's page. A client cannot state another source |
| Locally validated | Required fields are present and the format matches. A format check, nothing more |
| Externally verified | **Not implemented.** Stored as `NOT_IMPLEMENTED` on the record and shown on the page. No tax authority or BZSt is contacted |

- **The identifier is validated and discarded.** There is no column for it. A hash of a short ID would be brute-forceable, and
  this prototype has no need to read it back. Production must store the real one encrypted, under retention rules.
- **Never exposed:** it is posted in the body, never in a URL. Tests search responses, the whole database, audit events and logs for
  it, and the agent has no tool or route.
- An invalid attempt never replaces a stored record.
- **Stored fields:** country, US-person declaration, exemption amount, church-tax objection. On real data the church-tax
  objection can point to a religious belief, a special category under GDPR Art. 9, so production needs a legal review before
  collecting it this way.
- The field list is what the prototype models, not a statement of what the law requires.

## 6. Knowledge and experience

Reuses `assess` unchanged, asked for the `ORDER_EXECUTION` context, and records the result with its rule-set version.

| Case | Input | Result | The customer | The account |
|---|---|---|---|---|
| A | `synthetic-experienced` answers | `ASSESSMENT_OK` | Continues | Can open |
| B | `synthetic-novice` answers | `WARNING_REQUIRED` | Reads the warning, acknowledges, decides | Can open |
| C | `synthetic-incomplete` answers | `INSUFFICIENT_INFORMATION` | Reads the notice, acknowledges or completes | Can open |
| D | Tampered values, stated result, other session or no CSRF | Refused (400, 403, 401, 422) | Nothing recorded | Unchanged |

**The boundary.** A securities account is not an order. `assess(ACCOUNT_OPENING)` still answers
`NOT_TRIGGERED_BY_ACCOUNT_OPENING`, and the prototype executes no order. The page says so: the questions prepare later orders, and no
result stops the account opening. What the gate requires is that the customer went through the questions and read any notice,
which is a design choice of this prototype for the case study's "Angemessenheitsprüfung" step. No model takes part, and no agent
route reaches it.

### Where the duty applies, and what compliance must check

An engineering view, not legal advice.

- **Where it is typically required:** WpHG section 63(10), implementing MiFID II Art. 25(3): for securities services other than
  investment advice and portfolio management, the firm obtains knowledge and experience and warns if a product seems inappropriate or
  the information is not enough. It does not itself forbid the order. The firm must not assume a product is appropriate merely
  because the customer continues.
- **Where an exemption might apply:** WpHG section 63(11) and MiFID II Art. 25(4), execution-only: only if *all* conditions hold,
  including non-complex instruments, the customer's own initiative, a prior notice that no assessment takes place, and the conflict
  rules. Nothing here assumes that any product or service is exempt. `review_execution_only` can only say "compliance may consider
  it" or "the assessment remains required".
- **What the bank's compliance team must decide:** which legal entity has the duty; the service model per product and channel; the
  classification of each instrument; the questions and rule set, which the three questions here do not settle; the exact warning text
  and how it must reach the customer; whether answers given at onboarding may serve later orders, and for how long; recording and
  retention duties; and how this interacts with a partner bank's advice.

The questionnaire is a synthetic demonstration, not MiFID II or WpHG compliance.

## 7. Trusted product communication

**What the bank controls:** what it publishes and signs, what passes its deterministic check, and who may act. **What it does not control:** what a
customer's personal agent then writes. No claim of universal hallucination prevention is made.

- `approved_claims` builds every statement from the verified catalog, and each passes `validate_claim` before it is published. Hostile
  catalog text is simply absent.
- `GET /v1/providers/{id}/products/{id}/approved-claims` serves fees, conditions, risk summary, identity and partners, each with source
  document, version and digest.
- `get_official_product` carries the structured kinds to the agent: fees, identity, partners. Narrative text stays out of model traffic,
  the customer reads it on the bank's page, and the Phase 05 control holds.
- `POST /v1/claims/verify` answers whether a structured claim may be presented as official. Free text never is. Guarantees and approval
  promises go to a person.
- `ProductInformationAgent` answers from approved claims, declines what they lack, compares fees only when both are approved and never ranks,
  and sends sensitive questions to a person without a tool call.

| Statement | Result |
|---|---|
| "According to the verified synthetic price list pli-depot-basic version 2026.11, the custody fee of depot-basic is 0.00 EUR p.a." | Published, approved |
| A custody fee of "0 EUR" | `UNSUPPORTED`: differs from the catalog |
| "This investment is guaranteed to be safe." | `ESCALATE_TO_HUMAN` |
| "This bank will definitely approve your account." | `ESCALATE_TO_HUMAN` |
| A fee the catalog does not list | `ESCALATE_TO_HUMAN`, and no claim exists |

**Limits.** `app/agents/llm.py` is unchanged because its digest is recorded evidence, so the LLM has no new tool and its free-form text is not
checked. The wording filter knows listed phrases only. The live 20-case evaluation **predates** the `approved_claims` field in the tool result
and was deliberately not rerun, so the effect of that field on a real model is not measured. Only one product exists in the fixture, so comparison
is shown with a second product in tests.

## 8. Provider versions

Observed results: [evidence/provider-compatibility-matrix.md](evidence/provider-compatibility-matrix.md), generated from the real journey and compared
with a fresh run by a test.

- `VersionedDepotAdapter` is now the default. `TRUSTEQ_MOCK_DEPOT_SCENARIO` selects the mock provider's behaviour, and an unknown name stops the
  application.
- **Fallback needs two agreements:** the provider's answer names the version, and `COMPATIBILITY_POLICY` allows the step. "Gone" alone is not permission.
- Schema validation comes before any business action. Unknown fields, such as `opened`, invalidate the answer.
- A timeout, lost connection or malformed answer is never retried in another version, because the provider may have acted.
- Consent, checks and the policy gate run before the adapter, and the signed callback is unversioned. A fallback changes the wire format only.
- After any failure the application stays in `CONTRACT_PENDING`, no submission is recorded, the agent gets a generic 502, and a correctly signed callback
  is refused with 403.

**Limits.** The provider is in-process, so a timeout is a raised error. A real timeout leaves the outcome unknown, and reconciliation is not implemented.
The signed manifest lists no API versions: its digest stays pinned on purpose. No real provider is involved.

## 9. New security considerations

| Risk | Status |
|---|---|
| Unauthenticated `POST /v1/claims/verify` and `approved-claims`, with no rate limit (F-4) | Read-only and bounded, stores nothing. Public catalog data only. Production needs edge limits |
| F-1 now also covers tax data and answers: a client scripting the DEV sign-in can submit them for a customer | Synthetic only. Disappears with real authentication |
| More same-origin requests carry the one-time token (hidden form fields, redirect `Location`) | Same class as the known `Referer` limitation. One server log at the default level was checked: path only. Proxies were not tested |
| A provider, or anyone on its channel, could claim v2 is gone to force v1 | Only within the bank's policy, recorded as `fallback_from`, and the versions differ in format only. Production needs an authenticated channel |
| `TRUSTEQ_CUSTOMER_INFORMATION_REQUIRED=false` switches the gate off | A deliberate switch for old scenarios. Customer routes do not exist in production mode. Not separately refused there |
| Answers and declarations are stored in clear | Synthetic only. Production needs encryption, retention and a review of the church-tax field (GDPR Art. 9) |
| `SAFE_RESPONSE` text changed | No longer implies staff will help; it points to the customer page, consistent with the corrected acknowledgement |

## 10. Compatibility changes

- Schema version 5: two new tables, no change to existing ones. An upgrade test proves that rows survive.
- `tests/conftest.py` sets the switch to `false` for the older tests; this is the one global test change.
- Pins updated because of additions: the endpoint list (four new routes) and three schema pins. No assertion was weakened.
- `scripts/eval_llm.py` sets the switch explicitly. `llm_eval_cases.py`, `llm.py` and the raw result files are unchanged.
- Two mutants (M20, M53) got more context because the new gate contains the same lines. They test the same controls. M59 to M71 are new.
