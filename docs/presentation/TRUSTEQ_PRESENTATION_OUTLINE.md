# TRUSTEQ - 10-minute presentation outline

**Status:** draft outline.
**Language:** English.
**Prepared:** 3 November 2026.

**Built from:**
- the frozen ADR v1.1 (kept outside the repository);
- the implemented system at commit `5f781b5`;
- `docs/research/ist-analyse-union-investment.md`.

## Ground rules for every slide

1. **Prototype, not production.** Everything below is a well-tested prototype, and the test count is no
   evidence of production readiness:
   - customer sign-in is simulated;
   - KYC, compliance and the depot provider are mocks;
   - all data is synthetic;
   - production controls are missing: bank IAM / FAPI 2.0, high availability, DORA controls, an
     external security review and legal sign-off.
2. **Measured results only.** Every number was measured on 3 November 2026 on commit `5f781b5`. The
   brief mentions 605 tests; the measured count is **606**, because one test case was added in the
   localization commit.
3. **No AI evaluation results yet.** The real LLM and the 20-case evaluation belong to Phase 05
   (5-6 November). Until they are measured, slides show the evaluation protocol, never results.
4. **Research markers follow the Ist-Analyse.** These are VERIFIED FACT, PROJECT INFERENCE, FUTURE
   PROPOSAL and NEEDS VERIFICATION. A NEEDS VERIFICATION item is never presented as fact.

## Timing

| # | Slide | Duration | Cumulative |
|---|---|---|---|
| 1 | Business problem and objective | 0:40 | 0:40 |
| 2 | Ist-Analyse: opening a UnionDepot today | 1:10 | 1:50 |
| 3 | Roles, regulation and current limits | 0:50 | 2:40 |
| 4 | 2030 vision | 0:40 | 3:20 |
| 5 | L3 Controlled Agentic Onboarding and trust boundaries | 0:55 | 4:15 |
| 6 | Architecture, API and state machine | 0:55 | 5:10 |
| 7 | AI architecture and security controls | 0:55 | 6:05 |
| 8 | Live demo: Happy / Denied / Manual Review | 2:00 | 8:05 |
| 9 | Evidence, measured tests, limitations and roadmap | 0:50 | 8:55 |
| 10 | Conclusion and key architectural decisions | 0:35 | 9:30 |
| - | Buffer (demo latency, transitions) | 0:30 | **10:00** |

The speaker notes are written for about 140 words a minute.

---

## Slide 1 - Can an AI agent open a securities account safely? (0:40)

**Talking points**
- A German fund provider wants AI agents to discover its products and start a Wertpapierdepot
  (securities account) opening.
- It sells through its own channels and partner banks; a separate licensed depot bank runs the account.
- Goal: real automation, without giving an LLM identity checks, compliance decisions, consent or account
  opening.
- Deliverables: a 2030 target architecture and a working 2026 prototype.

**Visual:** one-sentence problem statement. Below it, three boxes: *Customer + personal AI agent* →
*Fund provider / partner bank* → *Licensed depot bank*.

**Evidence / source:** ADR v1.1 §01 "Problem and non-negotiables".

**Speaker notes:**
> "Imagine a customer telling their personal AI assistant: open a securities account for me and buy
> this fund. The case asks how a German fund provider can support that safely. The provider sells
> through its own channels and through partner banks, and the account itself is run by a separate,
> licensed depot bank. My objective was to get real value out of the agent without letting it decide
> anything the law assigns to a regulated institution. So I'll show you today's process, the 2030
> target design, and a working prototype."

---

## Slide 2 - Ist-Analyse: how a UnionDepot is opened today (1:10)

**Talking points**
- The contract partner is Union Investment Service Bank AG. The customer sends a signed paper form or
  uses an online sign-up flow, often via a cooperative bank `[S1]`.
- The contract exists only once the depot bank accepts it "after any required Legitimationsprüfung" and
  issues the depot number `[S1 §2.1]`.
- Advice happens at the Volksbanken and Raiffeisenbanken; the depot bank itself is execution-only
  `[S1 §7.2; S5]`.
- Online access is a second, separate step: an activation code arrives by 1-cent transfer within
  1-2 working days `[S4 §1]`.
- Digital routes are growing: minors' depots can be opened via UnionDepotOnline (from 1 January 2026)
  and in partner online banking (from 23 June 2026) `[S3]`.

**Visual:** the simplified Mermaid current-state flow from Ist-Analyse §3.4. Keep the dashed
"identification method: needs verification" node.

**Evidence / source:** Ist-Analyse §§1, 3 with sources S1, S3, S4 and S5 (all VERIFIED FACT). The
identification method for a new UnionDepot is **NEEDS VERIFICATION** `[G3, G4]`.

**Speaker notes:**
> "I used Union Investment as the real-world reference, because its structure matches the case. There's
> a fund company in the cooperative banking group. There's a separate depot bank, Union Investment
> Service Bank. And the Volksbanken and Raiffeisenbanken do the advice. From the official terms: the
> customer sends a signed paper application or uses an online sign-up flow, often through their
> cooperative bank. The contract exists only when the depot bank accepts it - after any required
> identity check - and sends the depot number. Online access is a second process. You register with
> your depot number and date of birth, and the activation code arrives by a one-cent bank transfer
> within one to two working days. Digital routes are growing: depots for minors can now be opened
> online. One limit of my research: the public documents don't say which identification method is used
> for a new depot. So I've marked that as needing verification instead of guessing."

---

## Slide 3 - Roles, regulation and current limits (0:50)

**Talking points**
- **GwG** (Anti-Money Laundering Act): identify and verify the customer before the relationship starts.
  Reliance on a partner bank or outsourcing to an ID provider leaves the responsibility with the obliged
  entity (§§ 10-13, 17) `[S10-S14]`.
- **WpHG** (Securities Trading Act): the service type decides.
  - Advice: suitability assessment plus a Geeignetheitserklärung (§ 64).
  - Non-advised services: appropriateness assessment with warnings (§ 63(10)).
  - Execution-only: only under strict conditions (§ 63(11)) `[S15, S16]`.
- **Tax:** the credit institution records the tax ID (AO § 154) `[S17]`.
- **BaFin:** video identification continues as a "bridge technology" `[S19, S20]`.
- **Consequence (PROJECT INFERENCE):** an agent can prepare and coordinate. It cannot identify, advise,
  consent or open.

**Visual:** responsibility matrix.
- Rows: identification/AML, advice/suitability, custody/execution/tax, product information, consent.
- Columns: partner bank, depot bank, KVG, ID provider, customer, agent.
- The agent column is empty except "prepare / coordinate".

**Evidence / source:** Ist-Analyse §§2, 4 (VERIFIED, law). The MiFID II and PRIIPs article texts are
**NEEDS VERIFICATION** `[G7, G8]`; cite only the verified German provisions.

**Speaker notes:**
> "The regulation draws the lines for us. Under the Anti-Money Laundering Act, the obliged institution
> has to identify and verify the customer before the relationship starts. It can rely on a partner bank
> or outsource to an identification provider, but the responsibility never moves. Under the Securities
> Trading Act, the type of service decides: advice needs a suitability statement, while non-advised
> business needs an appropriateness check with warnings. And the bank must record the tax ID. Today,
> much of this still means paper, signatures and waiting. So the design conclusion is simple: an agent
> can prepare and coordinate. It can't identify the customer, give advice, give consent or open the
> account."

---

## Slide 4 - 2030 vision: agent-ready, bank-controlled onboarding (0:40)

**Talking points**
- The customer's agent discovers verified, versioned product information (fees, risk text, KID
  references) instead of marketing claims.
- The agent prepares the application. The customer confirms on a bank-controlled channel, bound to the
  exact request.
- Identity comes through eID / EUDI wallet and partner-bank KYC reuse, as pluggable adapters, after
  legal review.
- Every step is audited, and a human is reachable at any point.

**Visual:** target-journey swimlane with three lanes:
- *Agent:* discover → draft → handoff;
- *Customer:* authenticate → review → confirm;
- *Bank:* policy → KYC/AML → contract → open.

**Evidence / source:** ADR v1.1 §§03, 06 and "Production protocol targets" (FAPI 2.0, RFC 9396,
RFC 8693, EUDI / OpenID4VP). These are **FUTURE PROPOSAL** and targets, not MVP claims.

**Speaker notes:**
> "For 2030, I assume customers will delegate more tasks to personal agents, and the bank should be
> ready - on its own terms. The agent gets signed, versioned product information instead of web
> marketing. It prepares the application, but the customer confirms on the bank's own channel, bound to
> exactly what was prepared. Identity comes through eID or a European wallet, via adapters, once that
> has been legally assessed. Everything is audited, and a person is always one click away. These are
> design targets, not claims about the prototype."

---

## Slide 5 - L3 Controlled Agentic Onboarding and trust boundaries (0:55)

**Talking points**
- **L3:** the agent orchestrates delegated tasks and bank-controlled systems enforce the rules (ADR-01).
  L2 under-delivers; L4 assumes identity and contract authority that an agent doesn't have.
- **Trust zones:**
  - T0a: the customer, who makes the legal decisions;
  - T0b: the external agent, untrusted software;
  - T1: the verifying edge;
  - T2: the core, which holds policy and opening authority;
  - T3: regulated providers.
- **Principle:** LLM proposes. Policy decides. Customer authorizes. Bank executes.
- **Critical threat: session substitution.** The handoff is one-time and expiring, and it binds the
  authenticated customer to the exact application and action digest.

**Visual:** trust-boundary diagram T0a / T0b → T1 → T2 → T3, annotated with "what the agent never
receives": customer session, credentials, signing authority, raw AML detail.

**Evidence / source:**
- ADR v1.1 §§03, 04, 06 (ADR-01, ADR-06, ADR-07).
- Code: `app/api/auth.py`, `app/services/authorization_service.py`, `app/domain/handoff.py`,
  `app/domain/mandate.py`.
- Tests: `tests/test_handoff.py` (28), `tests/test_agent_authorization.py` (39),
  `tests/test_customer_session.py` (24).

**Speaker notes:**
> "I chose level three: controlled agentic onboarding. At level two the agent only informs, which
> under-delivers. At level four it acts end-to-end for the customer, which assumes identity and contract
> authority that no agent has today. The trust model separates the human customer, who makes the legal
> decisions, from the agent, which is always untrusted software. One sentence sums up the design: the
> LLM proposes, policy decides, the customer authorizes and the bank executes. The most critical threat
> is session substitution - someone else confirming your application. So the handoff is single-use, it
> expires, and it binds the authenticated customer to the exact application and action digest."

---

## Slide 6 - Architecture, API and state machine (0:55)

**Talking points**
- **Modular monolith** (FastAPI, Pydantic v2, SQLAlchemy 2, SQLite), with ports and adapters for the
  mock providers (ADR-02, ADR-05).
- **Agent API:**
  - `GET /v1/providers/{id}/manifest`, `GET /v1/products`;
  - `POST /v1/applications` (with Idempotency-Key), `GET /v1/applications/{id}`;
  - `POST /v1/applications/{id}/handoff`, `POST /v1/applications/{id}/submit`.
- **Customer channel:**
  - `GET /customer/handoffs/{id}`;
  - confirmation as HTML form and as JSON (`POST /v1/customer/handoffs/{id}/confirm`);
  - `request-human` on both channels.
- **Provider:** signed `POST /internal/depot-provider/confirmations`.
- **Persistent state machine.**
  - Main path: `DRAFT` → `AWAITING_AUTHORIZATION` → `AUTHORIZED` → `KYC_PENDING` → `COMPLIANCE_PENDING`
    → `CONTRACT_PENDING` → `SUBMITTED_TO_DEPOT_PROVIDER` → `OPENED`.
  - Side outcomes: `ACTION_REQUIRED`, `MANUAL_REVIEW`, `REJECTED`, `CANCELLED`, `FAILED`.
- **`OPENED` only after a signed depot-provider confirmation.** At that point the agent mandate is
  revoked.

**Visual:** left, a component diagram (agent → REST edge → services and policy → repositories, with
mock adapters at the side). Right, the state diagram with `OPENED` reachable only from the
provider-callback edge.

**Evidence / source:**
- Code: `app/api/routes/applications.py`, `app/api/routes/catalog.py`, `app/api/routes/customer.py`,
  `app/api/routes/internal.py`; states in `app/domain/enums.py`; transitions in
  `app/domain/workflow.py` (`STATIC_TRANSITIONS`).
- Tests: `tests/test_state_machine.py` (33), `tests/test_applications_api.py` (47),
  `tests/test_provider_callback.py` (31).

**Speaker notes:**
> "Technically, it's a modular monolith: FastAPI, Pydantic and SQLAlchemy, with ports and adapters, so
> the mock providers can later be replaced by real ones without touching the core. The agent-facing
> API is small and versioned. The agent can read the provider manifest and products, create a draft
> with an idempotency key, request a customer handoff, read a generic status, and submit after
> consent. The customer uses a separate channel with its own session. The workflow is a persistent
> state machine from DRAFT to OPENED, with side outcomes such as manual review. And one rule is
> absolute: only a signed confirmation from the depot provider can set OPENED. At that moment the
> agent's mandate is revoked."

---

## Slide 7 - AI architecture and security controls (0:55)

**Talking points**
- **The customer-side agent is untrusted by design.** It uses six typed tools: `discover_provider`,
  `get_official_product`, `create_draft`, `request_handoff`, `get_generic_status`,
  `submit_after_consent`. Extra fields are rejected.
- **The server re-checks every call:** actor, scope, policy, state and payload. The policy layer refuses
  an illegal call even when a model produces it (ADR-03).
- **Catalog content is data, not instructions.**
  - The manifest is signed and pinned.
  - Fake, tampered, re-pinned, endpoint- or domain-swapped, stale and expired manifests are rejected.
  - A prompt-injection fixture stays inert data.
- **Minimal disclosure:** the agent sees only coarse status - no AML detail, no session, no signing
  authority.
- **Today vs. Phase 05:** the agent is deterministic and scripted (no API key). The real LLM and the
  20-case evaluation are Phase 05: **results not yet measured**.

**Visual:** tool-call pipeline: agent (scripted today, LLM in Phase 05) → typed tool schema → REST →
authorization and policy gate → state machine. A side box shows "injected catalog text → treated as
data". Add a small table of the 20-case protocol (5 tool use, 5 injection, 4 product claims,
3 mandate/privacy, 3 failure/handoff), marked "to be measured".

**Evidence / source:**
- Code: `app/integrations/agent_tools.py` (schemas with `extra="forbid"`), `app/agents/scripted.py`,
  `app/services/policy.py`, `app/services/catalog_policy.py`.
- Tests: `tests/test_agents.py` (17), `tests/test_agent_rest_integration.py` (14),
  `tests/test_policy.py` (36), `tests/test_verified_discovery.py` (33, including the prompt-injection
  fixture).
- ADR v1.1 §08 (evaluation protocol).

**Speaker notes:**
> "On the AI side, the customer's agent is untrusted by design. It can call only six typed tools, and
> extra fields are rejected. The server checks every call again - actor, scope, policy, state and
> payload - so even if a model produces an illegal call, the policy layer refuses it. Catalog content
> and tool output are data, never instructions. The provider manifest is signed and pinned, and fake,
> tampered, expired or outdated manifests are rejected. A prompt-injection fixture is treated as plain
> data. The agent only ever sees coarse status. Today the agent is deterministic and scripted, with no
> API key. The real LLM and the twenty fixed evaluation cases are the next phase, so I'm not claiming
> any model results yet."

---

## Slide 8 - Live demonstration: Happy / Denied / Manual Review (2:00)

**Talking points**
- **Happy path** (`python -m scripts.demo_happy`): discovery → draft → handoff → customer confirms → mock
  checks pass → signed provider callback → `OPENED` → mandate revoked.
- **Denied** (`python -m scripts.demo_denied`). Each attack is refused with no state change, and an
  audit event is written. The attacks:
  - a fake provider and a fabricated partner channel;
  - an agent naming itself as commission recipient;
  - actions outside the agent's scope;
  - an edited or re-pinned manifest, and a substituted endpoint or domain;
  - a stale or expired manifest;
  - a fake provider using a copied LEI.
- **Manual review** (`python -m scripts.demo_manual_review`): mock compliance returns `REVIEW_REQUIRED`
  → `MANUAL_REVIEW`. The agent sees only generic status; an operator continues in the trusted channel.
- **Fallback:** the recorded run from 8 November, if the live run fails.

**Visual:** terminal with the three commands prepared. Optionally, a browser showing the customer
confirmation page with the "Speak to a person" button.

**Evidence / source:**
- Scripts: `scripts/demo_happy.py`, `scripts/demo_denied.py`, `scripts/demo_manual_review.py`.
- Tests: `tests/test_demo_script.py`, `tests/test_demo_scripts_phase04.py`.
- Measured on 3 November 2026: all three demos exit 0, in about 6 s (happy), 5 s (denied) and 7 s
  (manual review).

**Speaker notes:**
> "Let me show this live. *[Run the happy path.]* The agent discovers the verified provider, creates one
> draft and asks for a handoff. The customer confirms on the bank's page, and the mock checks pass.
> Watch the status: it becomes OPENED only when the signed provider callback arrives, and that's also
> the moment the agent's mandate is revoked. *[Run the denied demo.]* Now the attacks: a fake provider,
> a fabricated partner, an agent trying to name itself as commission recipient, tampered, re-pinned,
> stale and expired manifests. Each one is refused, nothing advances, and an audit event is written.
> *[Run the manual-review demo.]* Finally, the compliance mock asks for a review. The application
> moves to manual review, the agent only sees a generic status, and an operator continues in the
> trusted channel. If anything fails live, I'll switch to the recording."

---

## Slide 9 - Evidence, measured tests, limitations and production roadmap (0:50)

**Talking points**
- **Measured on 3 November 2026 (commit `5f781b5`):**
  - 606 tests pass;
  - Ruff lint and format checks are clean (125 files);
  - all three demos exit 0.
- **Security mutation testing** (security checks deliberately broken to prove the tests notice):
  - 52 of 52 mutants were killed in the last full run, before the Phase 04 finalization;
  - 23 of 23 were killed in a targeted run after it;
  - all 55 definitions were re-validated after the localization, but the suite was not fully re-run.
- **Not production:**
  - sign-in is simulated; KYC, compliance and the depot provider are mocks; the data is synthetic;
  - there is no bank IAM / FAPI 2.0, no high availability, no DORA controls and no external security
    review;
  - the LLM evaluation has not been run.
- **Roadmap:** Phase 05 (LLM and 20 cases) → bank IAM and FAPI 2.0 → eID / EUDI and partner-KYC adapters
  → real depot-provider integration → legal and compliance review → operations.

**Visual:** two columns, "Measured evidence" and "Not production yet", above a roadmap timeline.

**Evidence / source:** pytest, Ruff and demo runs on 3 November 2026; `scripts/security_mutations.py`;
`git log` (commits `ef0e16d`, `85aaa2a`, `16ce080`, `5f781b5`); ADR v1.1 §11.

**Speaker notes:**
> "So what does the evidence show, and what doesn't it show? Measured today on the committed code: 606
> automated tests pass, linting and formatting are clean, and all three demos run. I also used mutation
> testing: I deliberately broke security checks to see whether the tests notice. All 52 mutants were
> caught in the last full run, and 23 of 23 in a targeted run after the final changes. But this is a
> well-tested prototype, not a production system. Sign-in is simulated, KYC and the depot bank are
> mocks, and real identity management, resilience and a security review are still missing. The next
> step is the LLM evaluation, then real identity and bank integrations."

---

## Slide 10 - Conclusion and key architectural decisions (0:35)

**Talking points**
- **L3:** delegate preparation, never authority (ADR-01).
- **Verified discovery:** trust comes from a signed chain, not from a name or a search result (ADR-08,
  §09).
- **Request-bound confirmation** on a bank-controlled channel (ADR-06).
- **Deterministic policy and a persistent state machine:** `OPENED` needs a confirmed provider outcome
  (ADR-03, ADR-05).
- **One named, accountable entity per obligation,** and human review without bypass.

**Visual:** five decision cards (ADR-01, -03, -05, -06, -08) and the closing line "LLM proposes. Policy
decides. Customer authorizes. Bank executes."

**Evidence / source:** ADR v1.1 §06 decision log; slides 5-9.

**Speaker notes:**
> "To conclude: the agent gets delegated preparation, never authority. Trust in a provider comes from a
> verifiable chain, not from a name or a search result. The customer confirms on the bank's channel,
> bound to the exact request. Deterministic policy and a persistent state machine protect the financial
> state, and OPENED needs a confirmed provider outcome. Every obligation has a named, accountable
> entity, and a human is always reachable. Thank you - I'm happy to go deeper into any part."

---

## Evidence mapping

Status values: **IMPLEMENTED + TESTED** (code and passing tests), **VERIFIED FACT** (external source),
**FUTURE PROPOSAL**, **NEEDS VERIFICATION**, **NOT YET MEASURED**.

| # | Claim (slide) | Code | Tests / measurement | Demo | Research / ADR | Status |
|---|---|---|---|---|---|---|
| E1 | The USB is the contract partner; the contract exists after its acceptance following any Legitimationsprüfung (2) | - | - | - | Ist-Analyse S1 §2.1 | VERIFIED FACT |
| E2 | Offer by signed paper form or online flow, often via a cooperative bank (2) | - | - | - | S1 "Zustandekommen des Depotvertrags" | VERIFIED FACT |
| E3 | Online access is a separate step; activation code by 1-cent transfer within 1-2 working days (2) | - | - | - | S4 §1 | VERIFIED FACT |
| E4 | Advice at partner banks; the USB is execution-only (2) | - | - | - | S5; S1 §7.2 | VERIFIED FACT |
| E5 | Online opening of minors' depots via UnionDepotOnline (from 1 Jan 2026) and partner online banking (from 23 Jun 2026) (2) | - | - | - | S3 footnote 6 | VERIFIED FACT |
| E6 | Identification method for a new UnionDepot (2) | - | - | - | Ist-Analyse G3, G4 | **NEEDS VERIFICATION** - do not state |
| E7 | GwG: identify before the relationship; reliance or outsourcing keeps the responsibility (3) | - | - | - | S10-S14 | VERIFIED FACT |
| E8 | WpHG service-type distinction (3) | - | - | - | S15, S16 | VERIFIED FACT |
| E9 | Tax ID recorded by the credit institution; Freistellungsauftrag needs it (3) | - | - | - | S17, S18 | VERIFIED FACT |
| E10 | Video identification continues as a bridge technology (3) | - | - | - | S19, S20 | VERIFIED FACT |
| E11 | 2030: eID / EUDI, FAPI 2.0, partner-KYC reuse (4) | - | - | - | ADR §§03, 06 | FUTURE PROPOSAL |
| E12 | One-time, expiring, request-bound handoff; wrong session refused (5) | `app/domain/handoff.py`, `app/api/routes/customer.py` | `tests/test_handoff.py` (28), `tests/test_customer_session.py` (24) | happy | ADR §03 | IMPLEMENTED + TESTED |
| E13 | Scoped agent mandate; revoked at `OPENED` (5, 6) | `app/services/authorization_service.py`, `app/domain/mandate.py`, `app/api/auth.py` | `tests/test_agent_authorization.py` (39), `tests/test_authorization_service.py` (20), `tests/test_provider_callback.py` (31) | happy, denied | ADR §§03, 11 | IMPLEMENTED + TESTED |
| E14 | `OPENED` only via a signed depot-provider callback (6) | `app/api/routes/internal.py`, `app/integrations/provider_callback.py` | `tests/test_provider_callback.py` (31) | happy | ADR §05 | IMPLEMENTED + TESTED |
| E15 | Persistent state machine and policy gate (6, 7) | `app/domain/enums.py`, `app/domain/workflow.py`, `app/services/policy.py` | `tests/test_state_machine.py` (33), `tests/test_policy.py` (36), `tests/test_recovery.py` (12) | all three | ADR-05 | IMPLEMENTED + TESTED |
| E16 | Idempotent draft creation (6) | `app/domain/idempotency.py`, `app/api/routes/applications.py` | `tests/test_applications_api.py` (47), `tests/test_application_service.py` (40) | happy | ADR §11 | IMPLEMENTED + TESTED |
| E17 | Six typed tools; extra fields rejected (7) | `app/integrations/agent_tools.py`, `app/agents/scripted.py` | `tests/test_agents.py` (17), `tests/test_agent_rest_integration.py` (14) | happy | ADR §08 | IMPLEMENTED + TESTED (scripted agent) |
| E18 | Fake, tampered, expired and stale manifests rejected; injected catalog text stays data (7, 8) | `app/services/catalog_policy.py`, `app/integrations/catalog_fixture.py` | `tests/test_verified_discovery.py` (33) | denied | ADR §09 | IMPLEMENTED + TESTED |
| E19 | Agent sees only generic status; no AML detail (7, 8) | `app/domain/visibility.py` | `tests/test_review.py` (33), `tests/test_onboarding_flow.py` (24) | manual review | ADR §§03, 05 | IMPLEMENTED + TESTED |
| E20 | Manual review without bypass; operator continues in the trusted channel (8) | `app/services/review_service.py`, `app/domain/review.py`, `scripts/dev_operator.py` | `tests/test_review.py` (33) | manual review | ADR §11 | IMPLEMENTED + TESTED |
| E21 | "Speak to a person": session + CSRF; agent credentials refused (8) | `app/api/routes/customer.py`, `app/api/customer_auth.py` | `tests/test_request_human_security.py` (23) | - | ADR §09 | IMPLEMENTED + TESTED |
| E22 | Disclosure receipt per document version; partner attribution bound into the approval (6) | `app/domain/receipts.py`, `app/domain/attribution.py` | `tests/test_disclosures_and_attribution.py` (21) | happy | ADR §09 | IMPLEMENTED + TESTED |
| E23 | 606 tests pass; Ruff clean; three demos exit 0 (9) | - | runs on 3 Nov 2026, commit `5f781b5` | all three | - | MEASURED |
| E24 | Mutation testing: 52/52 (full run, before the Phase 04 finalization) and 23/23 (targeted); 55 definitions validated (9) | `scripts/security_mutations.py` | runs on 3 Nov 2026 | - | - | MEASURED (full suite not re-run since) |
| E25 | LLM tool accuracy, injection resistance, product-claim accuracy (7, 9) | not yet written | 20 fixed cases, Phase 05 | - | ADR §08 | **NOT YET MEASURED** |
| E26 | Production readiness | - | - | - | ADR §11 "Implemented vs simulated" | **NOT CLAIMED** |

## Claims to avoid or keep marked

- **NEEDS VERIFICATION** (Ist-Analyse §9). Do not state any of these as fact on a slide:
  - how a new UnionDepot customer is identified, and whether the USB relies on or outsources it
    (G3, G4);
  - the group's ownership structure (G5) and the KVG legal names (G6);
  - the MiFID II Art. 25 and PRIIPs article texts (G7, G8);
  - whether the online sign-up is open to every new customer (G13);
  - VisualVest's current identification methods (G14);
  - the USB's appropriateness practice (G17).
- **No numbers for the LLM evaluation** until Phase 05 has produced them, and failures are reported as
  failures.
- **No production language.** Avoid "compliant", "certified" and "production-ready". The prototype
  simulates identity and banking partners.
- **Gaps vs the ADR to mention if asked:**
  - `GET /v1/customer/cases/{id}` is not implemented (deferred);
  - KYC results come from in-process mocks, not from the ADR's illustrative `/internal/kyc-results`
    callback;
  - no appropriateness questionnaire (WpHG § 63(10)) is modelled;
  - there is no adapter-contract v1/v2 test; the related checks cover manifest versions and database
    schema versions.
- **Optional future improvements** (not part of the frozen P0; not implemented now):
  - an appropriateness questionnaire (WpHG § 63(10)) with standardised warnings, where a negative result
    leads to a warning, not an automatic rejection;
  - adapter-contract versioning: v1/v2 fixtures, with rejection of unsupported versions.

## Completion plan (4-9 November 2026)

| Date | Work | Done when | Guardrails |
|---|---|---|---|
| 4 Nov | Review the Ist-Analyse and this outline. Check the most important NEEDS VERIFICATION items (G1, G3, G7, G8) in a browser. Build the slide skeleton. Decide whether to commit the two drafts | drafts approved; slide skeleton exists | no code changes |
| 5-6 Nov | **Phase 05.** A real LLM adapter behind the existing agent interface (`app/agents/`), enabled by an explicit flag; the scripted mode stays the default and needs no API key. Typed tool calling over the six existing tools. Run the 20 fixed cases (5 tool use, 5 injection, 4 product claims, 3 mandate/privacy, 3 failure/handoff) | pass/fail measured per case ID, with model name, version and settings recorded; full pytest, Ruff and the three demos pass; targeted mutation run for the touched areas | ADR frozen; P0 core unchanged; results reported as measured, failures included |
| 7 Nov | Final slide content from verified results only; add the Phase 05 numbers to the evidence mapping; finalise the speaker notes | every number on a slide traces back to a test run, a demo or a source | unverified research stays marked |
| 8 Nov | Final full test run; the full mutation suite once on the final code (the last full run predates the Phase 04 finalization); record the three demos as a fallback; two timed rehearsals (at most 10:00 each) | recording saved; rehearsal times noted | no feature work |
| 9 Nov (morning) | Final delivery before the deadline: slides, recording, repository at the final commit, research document | delivered; final commit hash noted | - |

**Schedule conflict to decide:** ADR §11 says "Freeze features on 5 Nov", but Phase 05 is planned for
5-6 November. Either the freeze moves to the end of 6 November for Phase 05 scope only, or Phase 05 is
cut back. The ADR itself stays unchanged.

**If Phase 05 slips:** present the scripted agent and the policy-layer evidence, and state plainly that
the model evaluation is pending.
