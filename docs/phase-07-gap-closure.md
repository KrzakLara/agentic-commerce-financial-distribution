# Phase 07 - Targeted gap closure

A bounded pass over seven gaps before the final presentation, measured on 2026-11-05 on top of `2dffc53`. All data is
synthetic and every provider is MOCK. No production readiness or regulatory compliance is claimed. Phase 05 to 07
reports and the frozen ADR were not rewritten: where this document differs from them, this document is newer
([section 10](#10-impact-on-the-presentation)).

## 1. Executive summary

| # | Gap | Status | In one line |
|---:|---|---|---|
| 1 | Customer authorization (F-1) | **PARTIALLY DEMONSTRATED** | The demonstrated header bypass is closed. Impersonation through the DEV sign-in remains and is pinned by a test |
| 2 | Provider adapter v1/v2 | **PARTIALLY DEMONSTRATED** | Tested through the real bank. Not wired into the demos; the signed callback is not versioned |
| 3 | Appropriateness | **PARTIALLY DEMONSTRATED** | Isolated deterministic component; no customer page, not in the journey |
| 4 | Tax information | **MOCK ONLY** | Standalone typed mock and integration contract |
| 5 | KYC and contract signing | **MOCK ONLY** | Typed boundaries over the existing mocks. No QES, no QTSP, no real provider |
| 6 | Trusted bank communication | **PARTIALLY DEMONSTRATED** | Deterministic check of structured claims. The agent's free text does not pass through it |
| 7 | Production security roadmap | **PROPOSED FOR 2030** | A gap matrix ([section 9](#9-production-security-and-integration-roadmap)); nothing implemented |

No gap is marked as fully resolved. F-2 to F-7 are unchanged; F-8 stays as resolved in Phase 07.

## 2. What was implemented

| Change | Where | Wired into the three demos? |
|---|---|---|
| The JSON confirmation requires the server-side session and CSRF token; `X-Dev-Customer-Id` removed from the code | `app/api/routes/customer.py`, `app/api/customer_auth.py` | **Yes** |
| Versioned provider contract, fallback and safe failure | `app/integrations/provider_contract.py` | No; set through `AppContainer.depot` |
| Knowledge-and-experience assessment | `app/services/appropriateness.py` | No |
| Synthetic tax-information contract and validation | `app/services/tax_information.py` | No |
| KYC and signing boundaries | `app/integrations/regulated_boundaries.py` | No |
| Claim and source check | `app/services/bank_claims.py` | No |
| Console demo of the isolated components | `scripts/demo_gap_components.py` | Separate command |

The state machine, policy gate, database schema, `app/agents/llm.py` and the Phase 05 data are unchanged.

**Changes to existing tests and scripts, all because the header no longer works:** `Flow.customer_confirm` now signs in
and sends the session's CSRF token; three tests in `test_handoff.py`, one in `test_llm_agent.py`, `demo_denied.py` and
`demo_llm.py` do the same. No assertion was removed or relaxed. Mutant M53 was rewritten because it reused the removed
helper; M56 to M58 are new. Tests that send the header and expect a refusal still send it.

## 3. What remains mocked, and what is missing

**Mocked or simulated:** customer identity, KYC, compliance, contract signing, the depot provider and its contract
fixtures, providers and partners, the register, operators, all data.

**Missing:**

- real bank authentication, and any customer-facing delegation or strong authorization;
- a customer page for the assessment, its warning and its acknowledgement, or for tax data;
- a persistent record of assessment results or tax data, and any policy-gate rule that uses them;
- the signature lifecycle in the onboarding flow, QES, any QTSP, any real KYC or AML provider;
- runtime enforcement of what the model writes;
- a versioned callback, version monitoring and an automatic hand-over to a person when a version is gone.

## 4. Test evidence

| Check | Command | Result |
|---|---|---|
| Full suite | `python -m pytest` | **867 passed**, 0 failed, 0 skipped, about 110 s (baseline 733; 134 new) |
| Lint and format | `ruff check .`, `ruff format --check .` | Clean, 150 files |
| F-1 regression | `pytest tests/test_f1_customer_authorization.py` | 25 passed |
| Adapter contract | `pytest tests/test_provider_contract.py` | 34 passed |
| Appropriateness / tax | `pytest tests/test_appropriateness.py tests/test_tax_information.py` | 16 and 16 passed |
| Boundaries / claims | `pytest tests/test_regulated_boundaries.py tests/test_bank_claims.py` | 13 and 29 passed |
| Component demo test | `pytest tests/test_demo_gap_components.py` | 1 passed |
| Three offline demos | `demo_happy`, `demo_denied`, `demo_manual_review` | 3 of 3 exit 0, `RESULT: PASS`. `demo_denied` still lists 7 audited refusals |
| Scripted mode without a key | The three demos in a copy with no `.env`, no `TRUSTEQ_*` or `ANTHROPIC_*` variable, `import anthropic` blocked | 3 of 3 exit 0, `RESULT: PASS`. `demo_llm` there: exit 3, `BLOCKED`, no call |
| Scripted baseline | `eval_llm --mode scripted` | 14 of 14, 6 not applicable, each case's status and the summary identical to the recorded file |
| Phase 05 data | SHA-256 before and after, `git status` | Unchanged: both result files, `app/agents/llm.py`, `scripts/llm_eval_cases.py` |
| Chrome (customer routes changed) | Chrome 156, headless, real server | [Section 5](#5-f-1-before-and-after) |
| Focused mutants | `security_mutations --only M17,M19,M20,M29,M30,M53,M54,M55,M56,M57,M58` | 11 of 11 killed, baseline 860 tests on the unmodified copy (7 demo subprocess tests excluded by the harness). M56, which restores the header bypass, fell first on a positive test; run alone, the dedicated attack test also fails on it |

**Not run:** the paid live LLM evaluation and `demo_llm` with a real model. The **historical 20 of 20** describes the
Phase 05 code. It does not validate this change; the model and tool code it measured did not change, while the customer
route did. An offline test (`test_llm_agent.py`) runs the same model loop with a fake client through the new session
path. The full mutation harness was not rerun: 55 of 55 remains the Phase 06 result on `00dfaf2`.

## 5. F-1 before and after

Same script over a real socket, once on `2dffc53`, once on this change. It prints status codes only.

| Step, by a client that speaks HTTP itself | Before | After |
|---|---|---|
| A1. Handoff values, `X-Dev-Customer-Id`, bearer token | 401 | 401 |
| A2. Handoff values, `X-Dev-Customer-Id`, no bearer token | **200, consent recorded** | **401, nothing recorded** |
| A3. The agent submits after A2 | 202, `SUBMITTED_TO_DEPOT_PROVIDER` | 409, `AWAITING_AUTHORIZATION` |
| B1. Scripted DEV sign-in form with the customer's reference | 303 | 303 |
| B2. Confirm form, with the page's CSRF token | **200, consent recorded** | **200, consent recorded** |
| B3. Unsigned provider callback | 401, not opened | 401, not opened |

**So the demonstrated path is gone, but the attacker's goal is not out of reach.** B2 worked before and works after,
because the DEV sign-in accepts any customer ID. An untrusted client that holds the link and the customer reference can
still record a consent, with one more step and a `customer.session_started` audit event marked `simulated_sign_in`.
`test_residual_limit_any_http_client_can_still_script_the_dev_sign_in` pins this so that it stays visible. A real bank
sign-in closes it; this prototype has none, and **this is not production identity verification.**

**Now required on `POST /v1/customer/handoffs/{id}/confirm`, in order:** no `Authorization` header (401, audited); no
cross-site browser signal (403); an active server-side session (401); a CSRF token bound to this session and handoff
(403, audited); then the existing bindings to the application's customer (403), the one-time challenge (403), the digest
(409) and single use (409). `OPENED` still needs the signed provider callback. Production mode has no session, so it
answers 401. Requests with no session or a cross-site signal leave no audit row, as on the form route; an audit row there
would widen F-4.

**Negative tests:** missing session, invalid session (3), expired session, wrong customer, missing or invalid CSRF token
(5), CSRF token of another handoff or session, agent token next to a valid session and token, replay, wrong challenge,
tampered digest, cross-origin (4), valid customer through to `OPENED`.

**Chrome 156, headless, on the changed server:** link, sign-in, review page, **Confirm and authorize** and **Speak to a
person** all work, with no console errors. In the signed-in browser context, the JSON route with the header but no CSRF
token got 403, and with a cross-site signal set by the automation got 403. A script on a real page from another origin
failed with `TypeError`, and no consent was recorded. Where that request stopped was not determined, so it is not
counted as proof of a server-side refusal. A visible window, a human hand and other browsers were not tested on this
change.

## 6. Adapter v1/v2 compatibility

| Situation (through the real bank unless noted) | Result | Test in `test_provider_contract.py` |
|---|---|---|
| Provider serves v1, v2, or both | Same journey to `SUBMITTED_TO_DEPOT_PROVIDER`, exactly one hand-over; `OPENED` only after the signed callback | `test_every_supported_version_completes_the_same_journey` (3) |
| Fixtures, request builder and mock agree | Pass, per version | `test_request_response_and_mock_all_match_the_fixture` (2) |
| Missing field, wrong type, invented `opened` field or status, wrong or unknown version (in-process) | Rejected | `test_malformed_or_unsupported_answers_are_rejected` (16) |
| v2 withdrawn (in-process) | Falls back to v1 once, no duplicate | `test_the_newest_version_is_used_and_a_gone_version_falls_back_once` |
| Malformed answer (in-process) | Refused, **never** retried in another version, because the provider may have acted | `test_a_malformed_answer_is_refused_and_never_retried_in_another_version` |
| Invented field or status, declined, all versions gone | 502 `provider_unavailable`, stays `CONTRACT_PENDING`, no submission | `test_a_bad_or_absent_provider_answer_fails_safely_and_advances_nothing` (4) |
| Submit before consent | 409, the provider is never called | `test_no_version_reaches_the_provider_before_consent` |
| Provider returns after an outage | The same application continues, one submission | `test_after_a_version_outage_the_same_application_can_still_continue` |

**Implemented:** the synthetic contract, its tests and the safe failure. **Proposed for production, not built:**

| Mechanism | Proposal |
|---|---|
| Version negotiation | Provider publishes supported versions in the signed manifest, and the bank pins an allow-list per provider |
| Contract tests in CI | Provider-supplied fixtures and consumer-driven contracts; a failure blocks the release |
| Change monitoring | Alert on deprecation headers, sunset dates, manifest changes and rising `contract_violation` events |
| Deprecation windows | Contractual notice, overlap of two versions and a tested cut-over date |
| Flags and rollback | Per-provider version flag with an audited change and instant rollback |
| Degradation and escalation | Stop new hand-overs, keep applications parked and open a review case automatically. Today the agent gets `UNAVAILABLE`; only the customer's "Speak to a person" reaches staff |

## 7. Appropriateness and tax scope

**Appropriateness (prototype demonstration, not legally complete MiFID II or WpHG compliance):** three synthetic questions
and three synthetic profiles, with outcomes `ASSESSMENT_OK`, `WARNING_REQUIRED` and `INSUFFICIENT_INFORMATION`. The two
non-OK outcomes carry a notice and ask for the customer's acknowledgement; neither accepts or rejects anyone silently.
Account opening returns `NOT_TRIGGERED_BY_ACCOUNT_OPENING`, because the duty attaches to the order. No outcome means
"exempt": when every execution-only fact is established (WpHG § 63(11)), the answer is only that compliance *may
consider* an exemption, and otherwise the assessment remains required. Answers from an agent or a model do not count.
Integration point: a separate order step on the bank's channel after `OPENED`. The frozen state machine has no such step,
so the component stays isolated.

**Tax (standalone mock):** a typed contract for residency, a synthetic identifier, a US-person self-declaration, and an
optional Freistellungsauftrag amount and Sperrvermerk. Each field is reported as collected, missing or invalid. The
identifier format is invented (`SYNTH-TAX-000123`), so a real-looking 11-digit ID fails validation. Data from an agent or
a model is never authoritative. Audit details hold statuses only, and the identifier stays out of `repr` and JSON dumps.
The agent has no tool for it and the model code does not reference it. There is no tax-authority integration, the field
list claims no completeness, and FATCA/CRS relevance remains marked as needing verification in the Ist-Analyse (G9, G16).
Integration point: the bank's own page, after sign-in and before KYC, with the status summary as policy evidence.

## 8. KYC, signing and communication

**Reviewed:** `KycPort`, `ContractSigningPort` and `DepotProviderPort` were not rewritten. They suit the mocks, but they
are synchronous and cannot express an outage as a result or a signature completed later. `regulated_boundaries.py` adds
the contract a real integration needs, unwired and in memory:

| Question | Answer in the contract |
|---|---|
| Who owns what | The bank creates requests, never from agent text; the provider owns results; the customer signs, never the agent |
| Identity outcome | `VERIFIED`, `NOT_VERIFIED`, `REVIEW_REQUIRED`, `PROVIDER_UNAVAILABLE` |
| Is a mock PASS compliance? | No: `is_regulatory_evidence` is false for every mock result |
| Signature state | `REQUESTED`, `PENDING_CUSTOMER`, `SIGNED`, `DECLINED`, `EXPIRED`, `PROVIDER_UNAVAILABLE`, with legal transitions only |
| Callback | HMAC check reused, freshness, bound to request and action digest; malformed input is rejected (the F-5 exceptions are caught here, and F-5 itself is unchanged) |
| Replay | A repeated event has no second effect |
| Mandatory human step | Anything except `VERIFIED` or a completed signature. The customer's own authorization is never removed |

A mock cannot claim `QUALIFIED_VIA_QTSP`: construction fails. **No QES exists here and no eIDAS-qualified signing is
claimed.** Production needs contracted providers, a QTSP on a trusted list, real signed messages or mTLS, and legal review.

**Grounded communication (limited source governance, not supervision of all free-form model language):**
`validate_claim` approves a structured claim only if its value equals the verified catalog and it cites that value's
source by document, version and digest. It covers fees, conditions, risk summary, provider identity and partner
attribution. Fabricated fees, false identities, unregistered or inactive partners and missing or wrong sources are
unsupported. Listed guarantee and recommendation wording, instruction-like text (including hostile text in a *signed*
catalog) and missing information go to a person, with a fixed safe answer. There is no model, RAG or CMS. **Limits:**
nothing routes the agent's text through this check at runtime, because `llm.py` was left unchanged and its digest is
recorded evidence. The wording filter only knows listed phrases.

## 9. Production security and integration roadmap

Proposed, not implemented. Stages: **S1** before any real customer data, **S2** before go-live, **S3** after go-live
towards 2030. Nothing here blocks the prototype demo, as long as the wording in [section 10](#10-impact-on-the-presentation)
is used; every row blocks production unless marked otherwise.

| Area | Current implementation | Verified protection | Remaining risk | Recommended production control | Role | Stage |
|---|---|---|---|---|---|---|
| Customer identity and authentication (F-1) | DEV sign-in accepts any customer ID | Session and CSRF on all customer routes; header refused (`test_f1_...`) | Impersonation at the DEV boundary | Bank SCA, eID or EUDI wallet in a bank channel; DEV routes absent from the production build | IAM architect | S1 |
| Strong customer authorization and delegation | One-time handoff bound to the action digest | Replay, digest and cross-customer tests | Consent only as strong as the sign-in; no delegation model | Transaction-bound SCA; signed, revocable delegation record | Security architect, Legal | S1 |
| OAuth, OIDC, FAPI, PAR | Opaque DEV tokens with scope and resource binding | Agent authorization tests, M01-M16 | Not a standard profile; no client authentication | OIDC for customers; FAPI 2.0 with PAR for agents, where the regulator's and partners' needs justify it | IAM architect | S2 |
| mTLS and proof of possession | None | None | A stolen bearer token works until it expires or is revoked | mTLS or DPoP sender-constrained tokens | Security engineer | S2 |
| Credential lifecycle and token theft | Hashed, expiring, revocable tokens; DEV keys in the repo; `.env` | Revocation tests; history scan in Phase 06 | Shared DEV keys, no rotation, F-2 unsafe default | KMS or HSM, rotation, theft detection, fail closed unless configured | Platform engineer | S1 |
| KYC and AML providers | Mock; `mock=true` stored | Mock can never be regulatory evidence (boundary tests) | No real identification | Contracted providers, compliance sign-off, adapters over the typed boundary | Compliance lead, Integration engineer | S1 |
| QTSP and signing | Mock; no legal effect | Mock cannot claim QES; callback checks | No valid contract | QTSP on a trusted list, signature lifecycle in the flow, legal review | Legal, Integration engineer | S2 |
| Tamper-evident audit (F-6) | Append-only by trigger | Trigger and rollback tests; no mutant on `audit_events` | Rewritable with database access; no detection | Hash chain or WORM store, SIEM export, mutants for the triggers | Security engineer | S2 |
| Rate limiting and abuse (F-4) | None | None | Audit growth, flooding, sign-in abuse | Edge rate and body-size limits, abuse detection | Platform engineer | S2 |
| Input validation and safe errors (F-5) | Strict schemas, generic errors | Exception tests; F-5 fails closed with 500 | Malformed callback headers answer 500; limits apply after reading the body | Parse defensively, 401 on malformed input, limit body size at the edge | Backend engineer | S2 |
| Trusted host generation (F-3) | Link host from the `Host` header | None: reproduced in Phase 06 | Bank response can carry a caller-chosen host | Fixed public origin from configuration, trusted-host middleware | Backend engineer | S1 |
| Tokens in `Referer`, logs and history | One-time `token=` in the URL. With `same-origin`, form posts carry it to the bank's own server | One local server log at its default level checked (Phase 07). No proxy or gateway log was tested | Exposure in proxy logs and browser history; token passes through the model provider | Exchange the link for a server-side reference on first load, redact logs, test the real proxies | Security engineer | S1 |
| API version migration | Synthetic v1/v2, unwired | Contract tests ([section 6](#6-adapter-v1v2-compatibility)) | Callback unversioned; no monitoring | The mechanisms in section 6 | Integration engineer | S2 |
| Availability, monitoring, incident response | SQLite, JSON logs, synchronous resume; depot call inside the transaction | Recovery and restart tests | Single node, no outbox, no alerting | Managed database, outbox, SLOs, alerting, tested runbooks, DORA controls | SRE lead | S2 |
| Performance, penetration, dependency testing (F-7 wording) | Own tests, hand-picked mutants | 867 tests; 55 of 55 on `00dfaf2`; focused subset here | No load test, pen test, SAST, DAST or dependency scan; comments claim more than tested | External pen test, scans in CI, load tests, correct the comments | QA lead, external testers | S2 |

## 10. Impact on the presentation

The Phase 07 runbook and outline were deliberately not changed. **These statements are now out of date; update them in
the presentation work:**

| Where | Old | Use instead |
|---|---|---|
| Runbook section 1, row "Customer identity" | The JSON confirmation accepts the header | The sign-in form accepts any typed customer ID; the JSON confirmation needs the session and CSRF token |
| Runbook section 2 | "It is not fixed", header path reproduced | The header path is closed; impersonation through the DEV sign-in remains |
| Runbook section 5, disclosure (1:42) | "...with the DEV-only customer header could impersonate..." | "One limit, clearly. The agent can't confirm with its own scoped credentials, but customer identity is simulated: anyone who can use the DEV sign-in form could impersonate a customer here. Production needs real bank authentication and stronger identity and authorization controls." |
| Runbook section 5, question "Can a rogue client approve by itself?" | Sends the DEV header | "In this prototype, yes: a caller with the handoff values can script the DEV sign-in and record a consent. An agent cannot do it with its credentials and has no tool for it. The header shortcut is closed. `OPENED` still needs the provider's signature. A real bank sign-in closes this" |
| Runbook section 6, Scenario B excerpt | Old `Reminder` line | The new one, and one more `HTTP 401` line in the full transcript. The narrated "seven refusals" stays correct |

**Still do not say:** "a rogue client cannot self-approve", "F-1 is fixed", "production-grade authentication", "compliant",
"the assessment runs in the journey", or "the agent's statements are verified".

**Can now be said, with the matching status:** the customer's JSON route needs a session and a CSRF token; the provider
contract has v1/v2 tests and fails safely when a version disappears; assessment, tax and claim checks exist as isolated
synthetic components, shown with `python -m scripts.demo_gap_components`. The three timed demos are unchanged in length
apart from one transcript line, and the new command is not part of the 2-minute walkthrough.

**Numbers:** 867 tests, measured on this change. Keep the outline's stale numbers out of the slides (606 tests, 52 of 52,
"LLM not measured"). 20 of 20 and 55 of 55 stay labelled with the code they measured.

## 11. Implemented / Simulated / Proposed 2030

| Capability | Implemented in the prototype | Simulated or isolated | Proposed for 2030 |
|---|---|---|---|
| Verified discovery | Signed, pinned, register-checked manifests | Providers, keys and register are fixtures | Asymmetric signatures, eIDAS seals, public agent discovery |
| Agent authorization | Scoped, expiring, revocable tokens with resource binding | DEV bootstrap | OAuth, FAPI 2.0, sender-constrained tokens |
| Customer authorization | Session, CSRF, digest, one-time challenge on every customer route | **Who the customer is** (DEV sign-in) | Bank SCA, EUDI wallet, signed delegation |
| Workflow and policy | Persistent state machine, deterministic gate, signed path to `OPENED` | Mock evidence behind the gate | Same design on production infrastructure |
| Human intervention | Review cases, "Speak to a person", no override | DEV console, synthetic operators | Case management, SSO, four-eyes, SLA |
| Provider versions | v1/v2 contract tests, fallback, safe failure | Mock transport, unwired, callback unversioned | Negotiation, CI contracts, monitoring, deprecation windows |
| Appropriateness | Deterministic function and its tests | Isolated, synthetic questions, no page | Order step with compliance-approved content |
| Tax data | Typed validation and its tests | Standalone mock, invented identifier | Bank-channel collection, BZSt integration where required |
| KYC and AML | Evidence recorded and gated, labelled MOCK | All outcomes; typed boundary unwired | Contracted regulated providers |
| Contract signing | Consent receipts bound to the action | Mock signer; boundary with no legal effect | QTSP and QES, reusable eID |
| Communication | Structured claim check over the catalog | Not applied to the agent's text at runtime | Runtime enforcement, independent evaluation |
| Audit | Append-only records in one transaction | - | Tamper-evident storage, SIEM |
| LLM | Real model behind the same tools, fail closed, scripted default | One recorded run, 20 fixed cases | Repeated evaluation, red team, monitoring |

## 12. Reproduce

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
.\.venv\Scripts\python.exe -m scripts.demo_happy           # also demo_denied, demo_manual_review
.\.venv\Scripts\python.exe -m scripts.demo_gap_components
.\.venv\Scripts\python.exe -m scripts.security_mutations --only M17,M19,M20,M29,M30,M53,M54,M55,M56,M57,M58
```

The before-and-after script, the clean-copy check and the Chrome driver were temporary and are not in the repository;
section 5 lists their requests and observed answers.
