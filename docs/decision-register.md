# Decision register

The case study asks for every central decision to be documented:
- what had to be clarified;
- which options were considered, and by which criteria;
- what was chosen and why;
- what was rejected and why;
- what it costs and what it trades away;
- what could tip the choice.

This register does that for the TRUSTEQ concept and prototype. Status as of 2026-11-07.

**Numbering.**
- D01 to D17 keep the numbers of the decision log in
  [technical-concept-2030.md](technical-concept-2030.md#8-decision-log), which stays the one-line index.
- D18 to D22 record decisions taken in the later phases.
- D23 to D26 were written down in the final review on 7 November 2026. The code already embodied these four decisions,
  but the register had no entry for them.

**Fields.** Every entry has the same 17 fields:
- The 15 the case asks for: problem, alternatives, criteria, chosen, why chosen, why not the alternatives, security,
  regulatory, cost and operations, trust and interoperability, trade-off, evidence, reversibility, revisit if, status.
- Two more: failure scenarios, and the requirements served.

**Criteria.** The criteria field marks the decisive one.
- **R** regulatory fit;
- **T** trust and security;
- **C** cost and operational complexity;
- **D** deprecation and change risk;
- **U** customer experience;
- **V** delivery and demonstration value.

These are engineering recommendations, not proofs of optimality.

**Status values.** Combinations name which part is which.
- **Implemented:** built and covered by tests in this repository.
- **Implemented, synthetic party:** built and tested, while the regulated counterparty is a labelled stand-in.
- **Concept:** designed, not built.
- **Legal validation required:** the technical side exists; the legal applicability is open.

**Requirements** the decisions serve, from the case study:
- **C1:** discoverability and a verifiable provider identity for customer agents.
- **C2:** agent-based depot opening, including identity proof, authorization and contract conclusion.
- **C3:** trustworthy customer communication and brand presence without a website visit.
- **C4:** reliable detection of when an agent may not conclude autonomously and a person must step in.
- **G1:** no sharper channel conflict with the partner banks.
- **G2:** no regulatory breach.
- **Q1:** robustness against manipulation.
- **Q2:** robustness against deprecation.

**Evidence** names code and tests in this repository. A prototype test shows that a mechanism works with synthetic
parties. It proves nothing about legal compliance or production behaviour.

## Priority decisions

**Seven decisions carry the architecture.** Each one would change the system's character if decided the other way.

| Why it matters | Decision |
|---|---|
| It sets where the human stays, and so what is legally and practically buildable today | **D01** Level L3: the agent orchestrates, the customer performs every legally binding act |
| An LLM cannot be the authorizer of a regulated action: it is not testable as one and is open to injection | **D03** The LLM proposes, deterministic policy and the state machine decide |
| A credential shared with an agent is a credential stolen: the agent's identity must never be the customer's | **D07** Separate customer, agent and mandate identities with scoped, revocable, sender-bound delegation |
| Account opening, advice and order execution trigger different duties; blending them invents or skips rules | **D10** Account opening is its own service; appropriateness is assessed for the later order and never rejects the account |
| Provider APIs will change many times before 2030 | **D13** Versioned provider adapters with an explicit, two-sided fallback |
| A wrong fee or a guarantee from a model is a regulatory incident, and output filters read no meaning | **D15** Verified structured claims: a model may only select facts the bank has verified |
| The partner banks must not lose customers to agents, or the network turns against the channel | **D19** Attribution from signed referral proofs and the bank's register; conflicts go to a person, never block the account, and no AI allocates commission |

**Seven decision questions are asked most often.** Each maps to entries of this register. The scorecards below compare
the options for each.

| Question | Decision |
|---|---|
| 1. Autonomy: L3 or L4? | D01 |
| 2. Identity: who proves who the customer is? | D16, with D07 for the delegation |
| 3. Model or rules: who decides? | D03 |
| 4. Provider integration: what happens when an API version changes? | D13, with D14 |
| 5. Product data for agents: what may an agent be told? | D15 |
| 6. Prototype fidelity: what may a prototype claim? | D20 |
| 7. Channels: whose customer is it, and who is paid? | D19 |

## Scorecards for the seven decision questions

**How to read them.** Each option is rated per criterion from the bank's point of view: **+** favourable, **o**
neutral or mixed, **−** unfavourable. For operational complexity, integration effort, vendor dependence and cost, **+**
means low. The ratings are the author's qualitative judgments, argued in the register entries. They are not
measurements and not weighted scores, so no total is computed. "Proof in the prototype" states what this repository
demonstrates.

### 1. Autonomy (D01)

| Criterion | L2 delegated preparation | **L3 controlled (chosen)** | L4 autonomous conclusion |
|---|---|---|---|
| Regulatory defensibility | + | + no new legal basis needed | − no established basis for a delegate's identification, consent and signature |
| Security and trust | + | + binding acts behind the bank's login | − a stolen mandate concludes contracts |
| Customer experience | − the customer coordinates everything | o one channel break per binding act | + fewest steps |
| Operational complexity | + | o | − mandate checks, liability handling |
| Integration effort | + | o | − |
| Vendor dependence | + | + | o mandate-format providers |
| Cost | + | o | − |
| Reversibility | + | + each step can move up a level | − concluded acts must be unwound |
| Proof in the prototype | covered: L3 includes the preparation | yes, end to end in the full-trust profile | no |

### 2. Identity (D16 with D07)

| Criterion | **Bank login and contracted proofing behind a port (chosen)** | In-house identity proofing | Authentication per partner channel | Customer credentials shared with the agent | Wallet only, now |
|---|---|---|---|---|---|
| Regulatory defensibility | + approved methods, supervised providers | o the bank must run approved methods itself | o | − no attribution, credential sharing | o not yet usable for every customer |
| Security and trust | + | o | o identities fragmented | − an unrestricted, unrevocable credential | + |
| Customer experience | o a login plus one proofing step | o | − several identities | + looks easy | + for wallet holders, − for the rest |
| Operational complexity | o | − | − | + | o |
| Integration effort | o | − build and certify | − | + | o |
| Vendor dependence | − contracted providers | + | o | + | − the wallet ecosystem |
| Cost | o a price per identification | − build and operate | o | + | o |
| Reversibility | + providers replaced behind the port | − | − | − a leaked credential stays leaked | + behind the same port |
| Proof in the prototype | yes, with a local OIDC provider and a synthetic proofing provider | no | no | refused by design: the agent never receives one | no |

### 3. Model or rules (D03)

| Criterion | **Model proposes, deterministic policy decides (chosen)** | The LLM decides | Hybrid: the LLM may override the rules |
|---|---|---|---|
| Regulatory defensibility | + reproducible and explainable | − | − |
| Security and trust | + injection cannot change state | − open to injection | − injection where the rules say no |
| Customer experience | o unforeseen cases wait for a person | + flexible | o |
| Operational complexity | o rules to write, review and version | o a model to evaluate and monitor | − both |
| Integration effort | o | + | − |
| Vendor dependence | + the model is replaceable, the policy is the bank's | − | − |
| Cost | o | o model calls per decision | − |
| Reversibility | o deliberately fixed: it is a guarantee | + a setting | + a setting |
| Proof in the prototype | yes: no model holds a transition; each decision records its policy version | refused by a test | no |

### 4. Provider integration (D13 with D14)

| Criterion | **Versioned adapter, two-sided fallback (chosen)** | One version only | Automatic downgrade on failure |
|---|---|---|---|
| Regulatory defensibility | + no control weakened by a version change | + | − may run the opening on weaker terms |
| Security and trust | + no downgrade without two permissions | + | − a downgrade attack |
| Customer experience | o refused when no permitted version exists | − breaks at the first deprecation | + seems to always work |
| Operational complexity | o a compatibility policy | + until a deprecation, then − | o |
| Integration effort | o | + | o |
| Vendor dependence | o the provider must signal versions | − tied to one version | o |
| Cost | o | + at first, − later | o |
| Reversibility | + | − | o |
| Proof in the prototype | yes, v1 and v2 against the bank's mock provider | no | refused by a test: a gone version alone is no permission |

### 5. Product data for agents (D15)

| Criterion | **Verified structured claims (chosen)** | Unguarded LLM answers | Web search or scraping by the agent | Proprietary access per provider only |
|---|---|---|---|---|
| Regulatory defensibility | + correct and attributable | − | − unverified and possibly stale | o correct, but only for contracted agents |
| Security and trust | + invented fees and guarantees refused | − | − manipulated pages | o |
| Customer experience | o limited to registered facts | + fluent | o | − independent agents get nothing |
| Operational complexity | − a fact registry to maintain | + | + | o |
| Integration effort | o | + | + | − a contract per agent |
| Vendor dependence | + the model is replaceable | − | o | − |
| Cost | o | o | + | − |
| Reversibility | o | + | + | o |
| Proof in the prototype | yes: 84 of 84 offline cases, mutants detected | no | no | no |

### 6. Prototype fidelity (D20)

| Criterion | **Real mechanics, synthetic regulated parties (chosen)** | An architecture slideshow | Paid production providers | A hard-coded happy-path animation |
|---|---|---|---|---|
| Regulatory defensibility, here the honesty of claims | + labelled in the data | + claims nothing | o contracts needed | − implies a function that does not exist |
| Security and trust, here the value as evidence | + refusals, signatures and persistence shown | − | + | − |
| Customer experience, here the clarity of the demo | + | o | + | o |
| Operational complexity | o | + | − | + |
| Integration effort | o | + | − | + |
| Vendor dependence | + | + | − | + |
| Cost | + | + | − paid, contracts | + |
| Reversibility | + stand-ins replaced behind the ports | o | o | − thrown away |
| Proof in the prototype | yes | no | not available to a case study | no |

### 7. Channels (D19)

| Criterion | **Signed proofs and register, conflicts to a person (chosen)** | Last touch wins | The direct channel always wins | The partner bank always wins | A model allocates the commission |
|---|---|---|---|---|---|
| Regulatory defensibility | + evidence only; payment by contract and WpHG § 70 | − | o | o | − no authority to allocate money |
| Security and trust | + the agent cannot name a partner | − an agent always touches last | o | o a valid second claim is ignored | − |
| Partner relations (G1, specific to this decision) | + | − | − partners lose customers | + | − |
| Customer experience | + the account never waits | + | + | + | + |
| Operational complexity | − a person per conflict, a partner register | + | + | + | o |
| Integration effort | o the partners' signing keys | + | + | o | o |
| Vendor dependence | o | + | + | o | − |
| Cost | o | + | + | + | o |
| Reversibility | o | + | + | + | o |
| Proof in the prototype | yes, the business example end to end | no | no | no | refused by a test |

## Register

### D01 Autonomy level

| Field | Content |
|---|---|
| Problem | How far may a personal agent go in opening a depot? |
| Alternatives | L2 delegated preparation: the agent prepares a draft that the customer completes and submits. L4 delegated conclusion under a standing mandate, without approval per action |
| Criteria | R, T, V; decisive: R |
| Chosen | **L3 controlled agentic onboarding**: the agent prepares, orchestrates and tracks a generic status. The customer authenticates, supplies tax and knowledge data, confirms the exact action and authorizes the signature in the bank's channel |
| Why chosen | L3 is the highest level that needs no new legal basis. The agent takes over the repeatable coordination: discovery, preparation and tracking. Every act the law ties to the person stays with the customer, and every decision stays with the bank. It also makes the whole journey demonstrable today |
| Why not the alternatives | L2 leaves the coordination with the customer and adds little to today's online banking. L4 would need several things that do not exist: a legal basis for a delegate's identification under the GwG and for consent and signature by an agent, warnings that reach the customer, settled liability and a mandate format |
| Security | Every binding act happens behind the bank's authentication, never through an agent-relayed approval |
| Regulatory | Identification (GwG §§ 10-13), consent and signature stay personal acts. L4 would need a legal basis for delegating them, which is not established |
| Cost and operations | A customer channel the bank must operate. L4 would add mandate verification and liability handling |
| Trust and interoperability | Any independent agent with a delegation can take part, over the A2A subset or REST (D04); no agent vendor is trusted. Identity providers for the customer channel (D16) |
| Trade-off | A channel break: the customer leaves the agent for the bank's pages for each binding act. This costs convenience and probably completion rate, for legal clarity |
| Evidence | `test_the_five_steps_are_one_journey_from_discovery_to_opened` (`tests/test_onboarding_e2e.py`); the state machine gives the agent no transition (`app/domain/workflow.py`; `test_the_external_agent_can_take_no_edge_at_all` in `tests/test_state_machine.py`) |
| Reversibility | High per step: each step can move up a level once its preconditions hold |
| Revisit if | The law and verifiable mandate models (for example wallet-bound delegation) support delegated identification and consent, and liability for an agent's errors is settled by contract |
| Status | Implemented, synthetic party: the L3 journey runs end to end in the full-trust profile; the regulated parties are stand-ins |
| Failure scenarios | The customer abandons at the channel break. An agent misleads the customer before the handover; the bank's page shows the exact action, so a mismatch cannot be confirmed |
| Requirements | C2, C4, G2, Q1 |

### D02 Shape of the bank core

| Field | Content |
|---|---|
| Problem | Microservices or one deployable for the onboarding core? |
| Alternatives | Microservices from the start |
| Criteria | C, V; decisive: C |
| Chosen | **Modular monolith with ports and adapters** |
| Why chosen | State, evidence and audit live in one transaction, so a small team can build and test the whole journey. Ports keep the external boundaries clean, and a later split stays cheap |
| Why not the alternatives | Microservices add distributed transactions, network failure modes and deployment overhead before any team or load requires them. Partial failures inside the core would also complicate the evidence |
| Security | One transaction for state, evidence and audit, so no distributed partial failure inside the core |
| Regulatory | Neutral. Outsourcing and DORA questions attach to the external providers, not to the internal split |
| Cost and operations | Low now. Ports keep the later split cheap |
| Trust and interoperability | No vendor dependency. External parties are reached only through ports |
| Trade-off | One node and one database: components cannot scale or fail independently until the split |
| Evidence | `app/integrations/ports.py`, adapters behind every external party |
| Reversibility | High: ports become service boundaries when teams or load require it |
| Revisit if | Separate teams, independent scaling or regulatory segregation require service boundaries |
| Status | Implemented as a modular monolith; a production topology with high availability is concept |
| Failure scenarios | One node and SQLite in the prototype: no high availability (a stated production limitation) |
| Requirements | Q2 |

### D03 Who authorizes

| Field | Content |
|---|---|
| Problem | Who decides whether a request, a step or an answer is allowed? |
| Alternatives | An LLM judges requests; a hybrid where the LLM can override the rules |
| Criteria | R, T; decisive: T |
| Chosen | **The LLM proposes, deterministic policy authorizes.** The bank's state machine owns every transition, and versioned rules decide applicability |
| Why chosen | A regulated decision must be reproducible, testable and explainable: the same inputs and policy version give the same answer, which can be shown to an auditor. A model's output is probabilistic, can be steered by injected text and cannot be tested exhaustively. So it may propose, but not decide |
| Why not the alternatives | An LLM judge is non-deterministic, open to prompt injection, hard to audit, and cannot carry the institution's responsibility. A hybrid override brings back the same risk exactly where the rules say no |
| Security | Prompt injection cannot change state: no model holds a transition |
| Regulatory | Decisions must be explainable and reproducible. GDPR Art. 22 concerns about automated decisions point the same way |
| Cost and operations | Rules must be written, reviewed and versioned |
| Trust and interoperability | No vendor dependency. The model is replaceable behind a fixed schema (D15, scenario 9 of the concept); the policy is the bank's own code |
| Trade-off | Rules must cover every case, and an unforeseen case goes to a person instead of being judged. This is less flexible than a model, by design |
| Evidence | `app/services/regulatory_policy.py`; `test_a_model_as_the_decider_is_refused`, `test_each_decision_records_its_inputs_and_policy_version` (`tests/test_trust_governance_policy.py`); the transition table in `app/domain/workflow.py`, where `test_the_external_agent_can_take_no_edge_at_all` (`tests/test_state_machine.py`) pins that the agent has no edge |
| Reversibility | Low by design: this is a guarantee, not a setting |
| Revisit if | Never as sole authorizer. A model may help triage for a person |
| Status | Implemented |
| Failure scenarios | A rule is wrong. Mitigation: versioned rules, a person for unresolved cases, tests per rule |
| Requirements | C4, G2, Q1 |

### D04 Agent interface

| Field | Content |
|---|---|
| Problem | Which protocol do agents use? |
| Alternatives | MCP first; A2A only |
| Criteria | D, C; decisive: D |
| Chosen | **REST first; protocols as thin adapters.** The A2A 1.0 subset and `POST /v1/agent/questions` reach the same pipeline |
| Why chosen | Agent protocols change fast, and the bank's capability is a small set of operations. So each protocol is a thin adapter over one pipeline with one authorization model. A2A 1.0 serves discovery and task exchange between organizations; REST serves everything else |
| Why not the alternatives | MCP first would tie the bank's public interface to a protocol made for a model's tools rather than for a service between organizations. A2A only would make every client implement A2A and tie the core to its releases |
| Security | One authorization model behind all protocols |
| Regulatory | Neutral |
| Cost and operations | One adapter per protocol |
| Trust and interoperability | Low vendor dependency: no single protocol is load-bearing. The official `a2a-sdk` 1.3.0 parsed the card and called the bank once, in a scratch environment ([provider-discovery-and-trust.md](provider-discovery-and-trust.md)) |
| Trade-off | Each protocol is a subset: A2A without streaming or push, so agents that need those are not served |
| Evidence | `test_an_unsupported_a2a_version_gets_version_not_supported`, `test_an_incompatible_protocol_version_fails_predictably` (`tests/test_trust_discovery.py`); `test_the_rest_route_answers_like_the_a2a_skill` |
| Reversibility | High |
| Revisit if | One protocol becomes the market standard with its own conformance suite |
| Status | Implemented: REST and the A2A 1.0 subset. MCP is not implemented |
| Failure scenarios | A protocol changes incompatibly: the adapter answers with a clear version error |
| Requirements | C1, Q2 |

### D05 Workflow

| Field | Content |
|---|---|
| Problem | Where does the onboarding state live? |
| Alternatives | Conversation memory; a BPMN or Temporal engine |
| Criteria | T, C; decisive: T |
| Chosen | **A persistent state machine with transactional guards** and an explicit transition table |
| Why chosen | An explicit table with an authority per edge makes every state change checkable and auditable. The onboarding is short and needs no timers, so an engine would add operations without adding control |
| Why not the alternatives | Conversation memory is neither durable nor authoritative, and it lets a model's context hold financial state. An engine is heavier to run, and its generic operations, such as setting a variable or completing a task, would need extra guards to keep the same guarantees |
| Security | No generic set-status operation. `OPENED` has one incoming edge, enforced in code and by a database trigger |
| Regulatory | Every state change has an authority and an audit event |
| Cost and operations | Low. No workflow-engine features such as timers |
| Trust and interoperability | No vendor dependency. The table is plain code and can be ported to an engine |
| Trade-off | No timers, deadlines or reminders. Production would add a scheduler for expiries and escalation |
| Evidence | `app/domain/workflow.py`; `test_after_a_crash_and_restart_the_next_submit_resumes_and_completes` (`tests/test_recovery.py`); `test_direct_opened_is_rejected_from_every_reachable_state` (`tests/test_application_service.py`) |
| Reversibility | Medium: the table can be ported to an engine |
| Revisit if | Long-running scale, timers and many parallel workflows warrant an engine |
| Status | Implemented |
| Failure scenarios | A crash between steps: `advance_checks` resumes idempotently |
| Requirements | C2, C4, Q1 |

### D06 Customer approval

| Field | Content |
|---|---|
| Problem | How does the customer approve what the agent prepared? |
| Alternatives | The agent relays the approval; the customer approves inside the agent |
| Criteria | R, T, U; decisive: T |
| Chosen | **A bank-controlled, one-time, request-bound handoff**: the customer confirms the exact action digest on the bank's page |
| Why chosen | Consent counts only if the customer sees the exact, current action on the institution's own page, behind its own login. A one-time, request-bound handoff means the agent cannot forge the approval, and the approval cannot be reused for another application |
| Why not the alternatives | A relayed approval lets the agent claim a consent it never got. An approval inside the agent puts the binding act into software the bank neither controls nor can attest |
| Security | No session substitution: the approval is bound to the customer's session, the handoff and the live catalog |
| Regulatory | Consent is given to the institution, for an exact and current action |
| Cost and operations | A channel break for the customer |
| Trust and interoperability | Works with any agent: the agent only passes a link, so no user-interface standard is needed on the agent side. No vendor dependency |
| Trade-off | The channel break costs convenience. A wallet-based signed intent could remove it later (revisit trigger) |
| Evidence | `OnboardingService.confirm_handoff`; `test_an_agent_cannot_reach_the_customers_authorization_by_any_route` |
| Reversibility | Medium |
| Revisit if | Wallet-centric presentation of signed intents becomes deployable |
| Status | Implemented |
| Failure scenarios | A phishing link: the bank's page shows the real action and needs the bank's login |
| Requirements | C2, Q1 |

### D07 Identities and delegation

| Field | Content |
|---|---|
| Problem | How does the agent act for the customer? |
| Alternatives | One shared identity; the customer passes tokens or passwords to the agent |
| Criteria | T, R; decisive: T |
| Chosen | **Separate identities**: the customer logs in with OIDC (code flow with PKCE). The registered agent asks with a client assertion, the customer approves on the bank's page, and the agent gets DPoP-bound, scoped, revocable tokens |
| Why chosen | Separate identities make every request attributable to whoever made it. The customer can revoke the agent without changing her own credentials. Sender-bound tokens (DPoP) make a stolen token useless without the agent's key |
| Why not the alternatives | A shared identity makes the agent indistinguishable from the customer and every action deniable. A password or bearer token handed to the agent is an unrestricted credential that the bank cannot revoke separately |
| Security | A stolen agent token is useless without the agent's key. Revocation ends every token at once |
| Regulatory | The customer's own authentication stays personal. Delegation does not transfer regulated duties |
| Cost and operations | More moving parts: registration, PAR, token exchange |
| Trust and interoperability | An IdP is needed. OAuth 2.0, OIDC, PKCE, PAR and DPoP are open standards. FAPI 2.0 is the 2030 target, not implemented |
| Trade-off | More moving parts, and a consent step for the customer before the agent can act |
| Evidence | `test_a_valid_delegation_gives_the_agent_a_bound_token_and_no_customer_secret`, `test_a_replayed_proof_fails`, `test_the_agent_cannot_decide_its_own_delegation` (`tests/test_trust_identity_delegation.py`) |
| Reversibility | Low: sharing credentials would be a security regression |
| Revisit if | Partner IAM frameworks or EUDI mandates make a standard delegation format available |
| Status | Implemented in the full-trust profile with a local IdP; FAPI 2.0 conformance is concept |
| Failure scenarios | Scope escalation, replayed proofs, stale proofs: all refused |
| Requirements | C2, Q1 |

### D08 Roles of provider and depot bank

| Field | Content |
|---|---|
| Problem | Is the fund provider the entity that opens the depot? |
| Alternatives | Assume the fund provider does everything |
| Criteria | R; decisive: R |
| Chosen | **Distinct roles**, attributed explicitly. In the reference case the depot contract is with a depot bank distinct from the fund companies |
| Why chosen | Each duty belongs to the entity that holds the licence and the contract, so modelling the roles separately keeps every duty with an owner |
| Why not the alternatives | One "provider" would put KYC, the KID and the depot contract on one entity and hide who is accountable for each |
| Security | Separate trust relations per party |
| Regulatory | Each duty belongs to the entity holding the licence and the contract |
| Cost and operations | More adapters |
| Trust and interoperability | One adapter and one trust relation per role |
| Trade-off | More adapters, contracts and keys to manage |
| Evidence | Public contract documents of a reference provider group, summarized in [regulatory-policy-matrix.md](regulatory-policy-matrix.md#ownership-and-automation-boundaries) |
| Reversibility | High |
| Revisit if | A specific client's legal structure justifies a simpler model |
| Status | Implemented as separate ports with synthetic parties. Where the matrix says so, the legal attribution of a duty is legal validation required |
| Failure scenarios | A duty lands with the wrong entity: the matrix names an owner per duty, marked for legal validation |
| Requirements | G2 |

### D09 Tax and appropriateness in the flow

| Field | Content |
|---|---|
| Problem | New workflow states, or a step inside the handoff? |
| Alternatives | New states per step |
| Criteria | D, C, V; decisive: D |
| Chosen | **A subflow bound to the handoff**; the state machine is unchanged and the confirmation gate checks it |
| Why chosen | The state machine stays unchanged, so there is no new transition to aim for, and the confirmation gate still refuses to proceed without the data |
| Why not the alternatives | New states would add transitions, migrations and agent-visible statuses for data that belongs to the customer |
| Security | No new transition an attacker could aim for |
| Regulatory | The gate is mandatory before the confirmation in the full-trust profile |
| Cost and operations | Low |
| Trust and interoperability | No vendor dependency. The agent sees only a generic status; the customer's tax and questionnaire data never reach it (data minimization) |
| Trade-off | The agent cannot tell the customer which step is missing beyond the generic status |
| Evidence | `test_missing_tax_information_blocks_the_confirmation_and_the_submission`, `test_the_workflow_keeps_its_gates_even_if_that_setting_is_bypassed` |
| Reversibility | Medium |
| Revisit if | A regulator or provider needs separate statuses |
| Status | Implemented |
| Failure scenarios | The agent cannot see the step's status (by design: it is the customer's data) |
| Requirements | C2, G2 |

### D10 Account opening, advice and orders

| Field | Content |
|---|---|
| Problem | Does an account opening trigger the appropriateness test, and what happens on a knowledge gap? |
| Alternatives | Skip the questionnaire; block the account on a knowledge gap; assume execution-only |
| Criteria | R, V; decisive: R |
| Chosen | **The account opening is its own service.** The questionnaire is assessed for the later non-advised order, a gap gives a warning the customer acknowledges, and no answer rejects the account. Advice and execution-only are not offered, and an exemption is never assumed |
| Why chosen | Each service has its own duties. The opening triggers identification, tax and contract duties; appropriateness attaches to the later non-advised order, suitability to advice. Keeping them apart neither invents a duty for the opening nor skips one for the order |
| Why not the alternatives | Skipping the questionnaire leaves the later order without its assessment. Blocking the account turns into a rejection what the law gives as a warning. Assuming execution-only ignores the conditions of WpHG § 63(11) |
| Security | No model can set or override the assessment |
| Regulatory | WpHG § 63(10) attaches the assessment to services other than advice and portfolio management, with warnings. § 63(11) exempts only under all its conditions. The applicability at account opening is **LEGAL VALIDATION REQUIRED** |
| Cost and operations | Rule sets per product, owned by compliance |
| Trust and interoperability | No vendor dependency. Rule sets are versioned data and code |
| Trade-off | The customer answers at the opening questions that formally serve a later order, and the answers can age (regulatory-policy-matrix assumption 3) |
| Evidence | `app/services/appropriateness.py`; `test_a_warning_needs_the_customers_decision_and_does_not_reject_the_account`, `test_a_request_for_another_service_is_refused_not_turned_into_an_account` |
| Reversibility | High: rule sets are versioned data and code |
| Revisit if | Compliance requires the assessment at order time only, or the service model changes |
| Status | Implemented with synthetic rule sets; the applicability at the opening is legal validation required |
| Failure scenarios | Reusing old answers too long (regulatory-policy-matrix assumption 3) |
| Requirements | G2, C4 |

### D11 Storage of the tax identifier

| Field | Content |
|---|---|
| Problem | How to keep the tax identifier in a prototype |
| Alternatives | Plain text; a hash |
| Criteria | T, C; decisive: T |
| Chosen | **Validate the format and discard it** |
| Why chosen | The prototype has to show the format check, not keep the identifier. Discarding it removes a sensitive value from every table, log and audit record |
| Why not the alternatives | Plain text is needless risk. A hash of a short identifier can be brute-forced and so protects nothing |
| Security | A hash of a short identifier can be brute-forced; plain text is needless risk |
| Regulatory | Production must store it under AO § 154(2a) and retention rules |
| Cost and operations | Low |
| Trust and interoperability | No vendor dependency |
| Trade-off | The bank cannot read the identifier back, so tax reporting cannot be demonstrated |
| Evidence | `TaxInformationRow` has no identifier column; `test_complete_tax_information_is_stored_without_the_identifier` (`tests/test_customer_information.py`) |
| Reversibility | High: production adds encrypted storage |
| Revisit if | Production: encrypted storage with access control and retention |
| Status | Implemented; encrypted storage for production is concept |
| Failure scenarios | The bank cannot read it back (accepted for the prototype) |
| Requirements | G2, Q1 |

### D12 Default of the information gate

| Field | Content |
|---|---|
| Problem | Is the tax and questionnaire step on or off by default? |
| Alternatives | Off with an opt-in |
| Criteria | T, V; decisive: T |
| Chosen | **On by default**; the original P0 scenarios opt out and say so. Full trust cannot run without it |
| Why chosen | A mandatory step that depends on remembering a flag will eventually be skipped. On by default, and refused for full trust when off, makes the safe path the default path |
| Why not the alternatives | Off with an opt-in relies on every deployment remembering the flag, and would leave the integration a demonstration trick |
| Security | The settings refuse full trust without it, and the workflow keeps the gate even if a copied settings object skipped validation |
| Regulatory | A mandatory step must not depend on remembering a flag |
| Cost and operations | Old tests run with a flag |
| Trust and interoperability | No vendor dependency |
| Trade-off | The original P0 scenarios must opt out explicitly, and they say so |
| Evidence | `test_the_full_trust_profile_cannot_run_without_the_customers_information` (`tests/test_onboarding_e2e.py`) |
| Reversibility | High |
| Revisit if | The original P0 scenarios are retired: then the opt-out itself can be removed |
| Status | Implemented |
| Failure scenarios | A misconfiguration: refused at start-up |
| Requirements | G2 |

### D13 Provider version fallback

| Field | Content |
|---|---|
| Problem | How to survive the provider's API versions and deprecations |
| Alternatives | No fallback: integrate one provider version only. Silent fallback: downgrade automatically whenever the newer version fails |
| Criteria | D, T; decisive: D |
| Chosen | **A versioned adapter**: the bank asks for the newest version it supports and falls back only when the provider names that fallback and the bank's compatibility policy allows it |
| Why chosen | Providers will deprecate versions before 2030. A versioned adapter keeps working across versions, and requiring both sides' permission for a fallback prevents a downgrade attack and any accidental loss of a control |
| Why not the alternatives | One version breaks at the first deprecation. An automatic downgrade can be triggered by an attacker or a fault, and it could run the opening silently on an older contract with weaker guarantees |
| Security | No downgrade without a two-sided permission |
| Regulatory | No version change may weaken a control: consent and gates run before the adapter |
| Cost and operations | A compatibility policy to maintain |
| Trust and interoperability | The provider must signal versions. The supported versions are listed in [evidence/provider-compatibility-matrix.md](evidence/provider-compatibility-matrix.md) |
| Trade-off | When no permitted version is available, the opening is refused, not degraded |
| Evidence | `app/integrations/provider_contract.py`; `test_a_gone_version_alone_is_no_permission_to_fall_back`, `test_acceptance_c_fallback_lowers_no_control` (`tests/test_provider_integration.py`) |
| Reversibility | High |
| Revisit if | Providers publish signed version metadata and sunset dates |
| Status | Implemented against the bank's own mock provider, versions 1 and 2. It is not certified against a real custodian API |
| Failure scenarios | A gone version without permission: refused, not retried |
| Requirements | Q2 |

### D14 Provider failures and retries

| Field | Content |
|---|---|
| Problem | What to do on a timeout or an unexpected answer |
| Alternatives | Retry in another version |
| Criteria | T; decisive: T |
| Chosen | **Refuse.** Only an explicit "not processed" may fall back to another version |
| Why chosen | After a timeout the outcome is unknown, so any automatic retry, in the same or another version, risks a second account. Refusing and recording leaves one source of truth: the provider's signed callback |
| Why not the alternatives | A retry in another version can open two accounts, or one on weaker terms |
| Security | A synchronous answer can never open an account |
| Regulatory | No duplicate account opening |
| Cost and operations | Manual reconciliation in production |
| Trust and interoperability | Idempotency support at the provider would simplify it |
| Trade-off | An unknown outcome needs a person to reconcile it |
| Evidence | `test_acceptance_f_an_outage_is_never_retried_in_another_version` (`tests/test_provider_integration.py`), `test_provider_errors_fail_safely_and_create_no_opened` |
| Reversibility | Medium |
| Revisit if | The provider guarantees idempotency keys and reconciliation endpoints |
| Status | Implemented: refusal and audit. Reconciliation is a production task, concept |
| Failure scenarios | An unknown outcome after a timeout: refused and audited, reconciliation is a production task |
| Requirements | Q1, Q2 |

### D15 Where claims are enforced

| Field | Content |
|---|---|
| Problem | How to keep financial statements by or through agents correct |
| Alternatives | Unguarded LLM answers with an output filter; unrestricted web search or scraping by agents; product data only through each provider's proprietary access |
| Criteria | T, R, D, C; decisive: T |
| Chosen | **Verified structured claims.** The bank publishes signed, versioned facts with their sources. For its own question answering, a model may only propose fact identifiers, and the bank verifies them and renders the answer from facts and fixed texts |
| Why chosen | A fee or a guarantee stated wrongly is a regulatory incident, and output filters cannot read meaning. When only a verified, versioned fact with its source can be released, a model can at worst pick the wrong fact, never invent one |
| Why not the alternatives | A filter on free model text leaves the model's sentence as the statement and misses paraphrases. Scraping gives agents unverified, stale or manipulated pages. Proprietary access per provider helps only agents with a contract and leaves independent agents without verifiable facts |
| Security | Invented fees, guarantees, licences and citations are refused; no model text is released |
| Regulatory | Product information must be correct and attributable: fees, risks and document versions come from the verified catalog |
| Cost and operations | A fact registry and a typed contract |
| Trust and interoperability | Low vendor dependency: the model is replaceable. Any agent can read the signed facts, but how a third party's agent displays them is outside the bank's control |
| Trade-off | Answers are limited to registered facts and fixed texts. They read less fluently, and a true fact can miss the question |
| Evidence | `CommunicationGuard.release`; 84 of 84 offline cases; mutants M91 and M94 to M99 detected ([communication-onboarding-evaluation.md](communication-onboarding-evaluation.md)) |
| Reversibility | Medium |
| Revisit if | Agents adopt signed-claim display standards |
| Status | Implemented with a synthetic catalog. Authoritative product feeds from the fund companies are concept. **Not guaranteed** ([regulated-communication.md](regulated-communication.md), section 5): relevance and completeness (LIM-01, LIM-02); the classification of questions, which uses listed phrases offline and was not measured for a real model; the truth of the provider's own document; fixed texts, which are tested, not verified facts; anything a third party's agent writes; a real model's behaviour, never run |
| Failure scenarios | A true fact can miss the question; a third party's agent renders facts its own way |
| Requirements | C3, Q1, G2 |

### D16 Customer identity: authentication and identity proofing

| Field | Content |
|---|---|
| Problem | How does the bank know who the customer is: in 2030, and in a prototype without a real eID or bank IdP? |
| Alternatives | In-house identity proofing by the bank; authentication per partner channel; the customer's credentials shared with the agent (rejected in D07); wallet-based credentials only, now. For the prototype: fake tokens presented as real; a vendor sandbox registration |
| Criteria | R, T, U, C; decisive: R |
| Chosen | **2030:** the bank's own strong customer authentication through OIDC (target profile FAPI 2.0), and contracted, regulated identity-proofing providers behind one port: online ID or video identification first, the EUDI Wallet later. **Prototype:** a separate local OIDC provider and a synthetic proofing provider behind real protocol checks, labelled synthetic. The older DEV sign-in stays only in the default development profile |
| Why chosen | Authentication and identity proofing are different acts. An OIDC login says who is signing in. Identification under the GwG establishes who the person legally is, with an approved method. Contracted providers bring approved methods and their own supervision, and a port keeps them replaceable, the wallet included |
| Why not the alternatives | In-house proofing makes the bank build and certify identification methods it can buy. Authentication per partner fragments the customer's identity across channels. Shared credentials break D07. Wallet only would exclude customers until wallets are common. A fake token presented as real would be false. A vendor sandbox needs a registration this case study does not have |
| Security | ES256 only, issuer, audience, nonce, PKCE and one-time state are all checked |
| Regulatory | An OIDC login is not identification under the GwG. In the prototype no regulated identification happens: `regulatory_evidence=false`, and any result claiming otherwise is refused |
| Cost and operations | Medium. In production, contracted proofing has a price per identification |
| Trust and interoperability | OIDC is an open standard; proofing providers are replaceable behind the port, and the EUDI Wallet joins behind the same boundary. No vendor dependency now; a bank IdP and an eID or wallet provider later |
| Trade-off | A dependency on contracted providers and their availability, and two steps for the customer: login and proofing |
| Evidence | `tests/test_trust_identity_delegation.py`, among them `test_dev_sign_in_alone_cannot_start_proofing` and `test_a_server_with_full_trust_and_the_dev_identity_mode_does_not_start` |
| Reversibility | High: replaced behind the same boundary |
| Revisit if | A legitimate sandbox for a bank IdP or an eID service becomes available, or the EUDI Wallet becomes usable for identification at the bank |
| Status | Implemented, synthetic party: a local OIDC provider and synthetic proofing. The bank's login and regulated proofing are concept |
| Failure scenarios | F-1: the DEV sign-in in the default profile accepts any customer ID. The full-trust profile refuses it (D21) |
| Requirements | C2, Q1 |

### D17 Version information in the provider manifest

| Field | Content |
|---|---|
| Problem | Add API versions to the signed manifest now? |
| Alternatives | Re-sign and re-pin the manifest with versions |
| Criteria | D, C; decisive: C |
| Chosen | **Keep the manifest**, with its digest pinned; the version policy lives in the bank's code |
| Why chosen | The pinned digest already detects any change to the manifest. Adding versions now would mean re-signing and re-pinning a verified artifact late in the project, for no new guarantee in the prototype |
| Why not the alternatives | Re-pinning late adds risk to recorded evidence for little benefit |
| Security | The pinned digest detects any change. The version policy in the bank's code is not attested by the provider, which is a known gap |
| Regulatory | Neutral |
| Cost and operations | Low |
| Trust and interoperability | No vendor dependency. Agents learn the versions from the bank's metadata, not from the signed manifest |
| Trade-off | The version policy can drift from the provider; contract tests cover it |
| Evidence | `app/integrations/catalog_fixture.py` pinned digest |
| Reversibility | High |
| Revisit if | Production: versions and sunset dates belong in the signed manifest |
| Status | Implemented as described; versions in the signed manifest are concept |
| Failure scenarios | Version policy drifts from the provider: covered by contract tests |
| Requirements | Q2 |

### D18 The only path to an opened depot

| Field | Content |
|---|---|
| Problem | What may set a depot to opened? |
| Alternatives | The synchronous submission answer; polling; an operator action |
| Criteria | T, R; decisive: T |
| Chosen | **Only the depot provider's signed confirmation**, bound to the recorded `provider_ref`, within a freshness window, idempotent by confirmation id |
| Why chosen | The account exists when the licensed institution says so. A signed, bound, fresh and idempotent callback is the only statement that carries that authority, and a single incoming edge makes the rule enforceable in code and in the database |
| Why not the alternatives | A synchronous answer can be spoofed or lost and is not final. Polling adds delay and a second source of truth. An operator action would let a person bypass the provider's acceptance |
| Security | Forged, wrong-application, replayed and duplicate callbacks change nothing |
| Regulatory | The account exists when the licensed institution says so |
| Cost and operations | Low |
| Trust and interoperability | The provider must sign its callbacks. The prototype uses a shared-secret HMAC; asymmetric signatures or mTLS are the 2030 target |
| Trade-off | A lost callback leaves the application submitted until the provider sends it again |
| Evidence | `handle_provider_confirmation`; `test_a_forged_provider_callback_cannot_open_the_depot`, `test_a_duplicate_provider_callback_has_no_second_effect`; `test_only_the_provider_confirmation_can_open_a_depot` (`tests/test_state_machine.py`) |
| Reversibility | Low by design |
| Revisit if | The provider offers a standard event protocol with asymmetric signatures |
| Status | Implemented with the mock provider; asymmetric callback signatures are concept |
| Failure scenarios | A lost callback leaves the application submitted: visible, retryable by the provider |
| Requirements | C2, Q1 |

### D19 Channel attribution governance

| Field | Content |
|---|---|
| Problem | Whose customer is an agent-originated onboarding, and how to avoid channel conflict |
| Alternatives | Trust the agent's claim; first come first served, or last touch wins; the direct channel always wins; the partner bank always wins; a model allocates the commission; block the account on conflict |
| Criteria | R, T, V and G1; decisive: R and G1 |
| Chosen | **Attribution from signed partner referral proofs and the bank's register.** The customer's choice of bank is shown and bound into the approved action; a conflict goes to a person and never stops the account; no AI decides commission |
| Why chosen | Who is paid is a contractual and regulatory question (WpHG § 70), not a technical one; the bank can only prove where a customer came from. Signed proofs and the register make that evidence verifiable. A person settles real conflicts, and the customer's account never waits for the dispute |
| Why not the alternatives | The agent's claim cannot be verified. Last touch rewards whoever reaches the customer last, which an agent always does. "Direct always wins" takes customers from the partner banks, and "partner always wins" ignores a valid second claim. A model has no authority to allocate money. Blocking the account punishes the customer for a dispute between banks |
| Security | An agent cannot name a partner or a commission recipient |
| Regulatory | Inducement rules (WpHG § 70) and contracts govern compensation, not code. **LEGAL VALIDATION REQUIRED** for any commission model |
| Cost and operations | A governed partner register and a dispute process |
| Trust and interoperability | The partner banks' signing keys. A common referral-proof format needs the partners' agreement |
| Trade-off | Each conflict costs a person's time, and the commission is withheld until it is settled |
| Evidence | [channel-governance.md](channel-governance.md); `test_the_channel_conflict_business_example_end_to_end` and the governance tests in `tests/test_trust_governance_policy.py`; `test_partner_attribution_is_not_overwritten_at_any_later_step` (`tests/test_onboarding_e2e.py`) |
| Reversibility | Medium |
| Revisit if | Partners agree a different allocation model in their contracts |
| Status | Implemented with synthetic partners. The commission model is legal validation required; partner contracts are concept |
| Failure scenarios | Two valid claims: recorded as a conflict for a person |
| Requirements | G1, Q1 |

### D20 Synthetic stand-ins instead of claimed integrations

| Field | Content |
|---|---|
| Problem | How to show regulated steps without regulated providers |
| Alternatives | An architecture slideshow only; paid or inaccessible production providers; a hard-coded happy-path animation; mocks presented as integrations; unvetted public sandboxes |
| Criteria | T, R, V; decisive: V and R |
| Chosen | **Local synthetic providers behind the real ports**, labelled in the data (`mock=true`, `regulatory_evidence=false`, `TEST_NO_LEGAL_EFFECT`) and refused in production |
| Why chosen | Without contracts, the strongest evidence is real execution with only the counterparties simulated. That covers real protocols, persisted state, signed callbacks, failure handling and authorization checks. Labelling the stand-ins in the data, not only on a slide, keeps every claim honest |
| Why not the alternatives | A slideshow proves nothing about the mechanics. Production providers need contracts and money and are out of reach for a case study. A happy-path animation cannot show a refusal. A mock presented as an integration would be false. Public sandboxes are unvetted and can change |
| Security | Production settings refuse the mock callback and a claimed regulated provider |
| Regulatory | No claim of regulated identification, KYC or QES |
| Cost and operations | Low |
| Trust and interoperability | No vendor dependency now. Each stand-in sits behind the port a real provider would use, so replacing it does not change the core |
| Trade-off | Nothing about a real provider's behaviour is proven: its latency, errors, formats and legal effect stay untested |
| Evidence | `test_production_refuses_the_insecure_local_setting_and_a_regulated_claim` (`tests/test_trust_identity_delegation.py`), `test_nothing_can_claim_a_qualified_signature_without_a_named_qtsp` (`tests/test_trust_contract_signing.py`) |
| Reversibility | High: each stand-in is replaced behind its port |
| Revisit if | Sandboxes with contracts exist for each provider |
| Status | Implemented |
| Failure scenarios | A reader mistakes the demo for a live integration: every label says otherwise |
| Requirements | G2 |

### D21 Full-trust profile configuration

| Field | Content |
|---|---|
| Problem | May the full-trust gates run next to the DEV sign-in? |
| Alternatives | Rely on documentation; allow it with a warning |
| Criteria | T; decisive: T |
| Chosen | **No.** The profile requires `customer_identity_mode=oidc`, the server does not start otherwise, and only sessions from a verified login count |
| Why chosen | A rule that only documentation enforces will be broken. Refusing to start makes the unsafe combination impossible |
| Why not the alternatives | Documentation and warnings do not stop a misconfiguration |
| Security | Closes the path where a DEV session approved a delegation |
| Regulatory | Neutral |
| Cost and operations | Low |
| Trust and interoperability | No vendor dependency |
| Trade-off | The DEV sign-in remains in the default development profile (F-1), so that profile must never face real users |
| Evidence | `test_a_server_with_full_trust_and_the_dev_identity_mode_does_not_start`, `test_dev_sign_in_alone_cannot_start_proofing` (`tests/test_trust_identity_delegation.py`); mutants M104 to M106 |
| Reversibility | High |
| Revisit if | A second verified identity mode is added, such as an eID login: the rule extends to it. It is never relaxed to admit the DEV sign-in |
| Status | Implemented |
| Failure scenarios | A misconfiguration fails at start-up |
| Requirements | Q1 |

### D22 Schema evolution

| Field | Content |
|---|---|
| Problem | How to change the database schema without losing evidence |
| Alternatives | A migration framework; drop and recreate |
| Criteria | D, T; decisive: T |
| Chosen | **In-place upgrade steps in one transaction**, with table rebuilds where SQLite cannot alter a constraint, and refusal of a newer schema |
| Why chosen | Audit and review evidence must survive every upgrade. One transaction per upgrade leaves the database as it was when a step fails, and refusing a newer schema stops an old program from damaging new data |
| Why not the alternatives | A migration framework is a new dependency for a single-file prototype database. Drop and recreate destroys evidence |
| Security | A failed upgrade leaves the database as it was |
| Regulatory | Audit and review evidence must survive upgrades |
| Cost and operations | Hand-written steps, tested against real historical schemas |
| Trust and interoperability | SQLite in the prototype; a managed database in production |
| Trade-off | Hand-written steps need a test against every historical schema they upgrade |
| Evidence | `test_a_failed_upgrade_leaves_the_version_5_database_exactly_as_it_was`, `test_a_database_from_a_newer_version_is_refused`; mutants M107, M108 |
| Reversibility | High |
| Revisit if | Production moves to a managed database with a migration framework |
| Status | Implemented: the upgrade from version 5 to version 6 is tested against a real version-5 database |
| Failure scenarios | An old CHECK constraint survives an upgrade: found on 2026-11-07, fixed with a rebuild and a real v5 fixture |
| Requirements | Q2 |

### D23 Provider discovery and trust

| Field | Content |
|---|---|
| Problem | How does an independent agent find the provider, and verify that it is the real institution? |
| Alternatives | Trust the domain name and its TLS certificate alone; a central directory or marketplace run by one platform; allow-lists kept by each agent vendor |
| Criteria | T, D, V; decisive: T |
| Chosen | **Signed, machine-readable discovery.** An A2A 1.0 Agent Card at the well-known URI is signed by a key that an Entity Statement in the OpenID Federation format names. The statement is signed by a trust authority the agent already trusts, and the agent verifies the chain before it reads the card. Product facts come in a signed manifest whose digest is pinned. In the prototype the trust authority is synthetic, so the licence status stays `NOT_ESTABLISHED` |
| Why chosen | TLS proves control of a domain, not a licence. A signed statement from an authority the agent trusts can say who the provider is and which keys speak for it. Keys can then rotate without breaking agents |
| Why not the alternatives | TLS alone cannot tell a bank from a look-alike domain. A central marketplace creates a gatekeeper and a single point of failure. Vendor allow-lists fragment trust and do not reach independent agents |
| Security | Refused: forged metadata with the same key id, an unknown or expired anchor, a wrong anchor key, a revoked entity, a withdrawn key, and tampered or unsigned statements. A card cannot send the agent to another origin. The agent's outbound calls refuse redirects and hosts that are not configured |
| Regulatory | A licence is claimed only when an authoritative register confirms it. The synthetic authority confirms nothing, so the licence stays `NOT_ESTABLISHED` |
| Cost and operations | Key management and statement renewal for the provider; someone must operate the anchor |
| Trust and interoperability | Open formats: the A2A Agent Card, OpenID Federation entity statements, JOSE with ES256. Conformance is not claimed: a subset without metadata policy, trust marks or a resolver, and chains of leaf and anchor only ([provider-discovery-and-trust.md](provider-discovery-and-trust.md)) |
| Trade-off | Without a governed sector anchor, verification proves the mechanism, not the identity |
| Evidence | `test_the_agent_card_is_at_the_a2a_well_known_uri_with_the_required_fields`, `test_a_tampered_card_fails_its_signature`, `test_forged_provider_metadata_signed_by_another_key_fails`, `test_an_unknown_trust_anchor_fails`, `test_key_rotation_works_while_the_superior_names_both_keys_and_ends_after`, `test_a_card_cannot_send_the_agent_to_another_origin` (`tests/test_trust_discovery.py`); `test_an_expired_trust_anchor_is_rejected` (`tests/test_verified_discovery.py`); section C of `python -m scripts.demo_trust_journey` |
| Reversibility | Medium: the formats are standard, and the synthetic anchor can be replaced by a real federation |
| Revisit if | A sector trust anchor or a supervisor's register publishes machine-readable statements, or A2A or the federation specification changes incompatibly |
| Status | Implemented, synthetic party: the trust authority is synthetic. Global discovery and a governed anchor are concept |
| Failure scenarios | The anchor's key is compromised: every statement it signed must be revoked. A provider's key leaks: the superior withdraws it and names a new one |
| Requirements | C1, Q1 |

### D24 Contract package and signature form

| Field | Content |
|---|---|
| Problem | What exactly does the customer sign, and in which legal form? |
| Alternatives | Sign a rendered page or PDF without a binding hash; let the agent assemble the contract; require a qualified electronic signature (QES) for every declaration; treat the test signature as legally valid |
| Criteria | R, T; decisive: R |
| Chosen | **An immutable contract package with a stable hash**, built from the confirmed exact action and the current terms. The customer authorizes the hash on the bank's page, and the signer signs it. The signature form is a port: a test signer marked `TEST_NO_LEGAL_EFFECT` now, and in production the form each declaration legally requires, a QES only where a form demands one |
| Why chosen | A hash binds the signature to exactly what the customer saw, and any later change becomes detectable. A separate signer port keeps the legal form open until the legal answer exists, without changing the core |
| Why not the alternatives | A signature over a page without a hash cannot prove what was signed. An agent-assembled contract would let the agent change terms. A QES everywhere adds cost and friction where no form requires it, and no general form requirement for the depot contract was found. A test signature presented as valid would be false |
| Security | The package cannot be changed or deleted. A wrong hash, a package rewritten with a matching hash, an unknown signer key, and a package without a signing session are all refused |
| Regulatory | The legal form per declaration (BGB § 126b, DepotG, eIDAS) is **LEGAL VALIDATION REQUIRED**. Nothing can claim a qualified signature without a named QTSP |
| Cost and operations | A QTSP contract only where a form requires it; packages are kept as evidence |
| Trust and interoperability | SHA-256 and a signer port: a QTSP or a wallet signature can stand behind it |
| Trade-off | The legal effect waits for the legal answer, and the customer signs a hash-bound package instead of a familiar PDF flow |
| Evidence | `test_the_package_binds_the_confirmed_action_and_has_a_stable_hash`, `test_the_package_cannot_be_changed_or_deleted`, `test_a_wrong_package_hash_is_refused`, `test_nothing_can_claim_a_qualified_signature_without_a_named_qtsp`, `test_the_test_signer_page_says_it_is_no_qes`, `test_a_message_from_an_unknown_signer_key_is_refused` (`tests/test_trust_contract_signing.py`); `test_a_package_rewritten_with_a_matching_hash_is_still_refused` (`tests/test_onboarding_e2e.py`) |
| Reversibility | High for the signer behind the port; the hash binding stays |
| Revisit if | The legal validation names a form per declaration, or a qualified signature from the EUDI Wallet becomes usable in the bank's channel |
| Status | Implemented with a test signer; a QTSP or QES integration is concept |
| Failure scenarios | The terms change before the package exists: it is not built (`test_terms_changed_before_the_package_exists_stop_it_from_being_built`). A contract that no longer holds can only be rejected by a person (`test_a_person_can_only_reject_a_contract_that_no_longer_holds`, both in `tests/test_onboarding_e2e.py`) |
| Requirements | C2, G2, Q1 |

### D25 Human review

| Field | Content |
|---|---|
| Problem | Where and how do people decide the exceptions, without becoming a way around the controls? |
| Alternatives | An operator web route on the same server; an "approve" decision for operators; the agent or a model deciding the case; no human review |
| Criteria | R, T; decisive: T |
| Chosen | **Review cases on a trusted console, with a fixed set of decisions.** A failed or unclear check parks the application in `MANUAL_REVIEW`, which remembers the interrupted step. Operators choose `RE_EVALUATE`, `RESUME`, `REJECT` or `ACKNOWLEDGE`; there is no approve. `REJECT` is a supervisor's business decision, and no operator decision can open an account. The console is a local command (`scripts/dev_operator.py`) with synthetic operators, there is no operator HTTP endpoint, and the console is off in production |
| Why chosen | People must handle what rules cannot. But a person must not become a way around KYC, consent, signing or the provider. A fixed decision list and no path to `OPENED` keep people in charge of exceptions only |
| Why not the alternatives | An operator route on the customer-facing server adds an attack surface. An approve decision would let a person skip a failed check. A model deciding the case would undo D03. No review would turn every unclear case into a rejection |
| Security | Operator identities are separate from agents, customers and tokens; an unknown operator is refused; `RE_EVALUATE` re-runs the deterministic check with the recorded evidence |
| Regulatory | Rejection and escalation stay with the institution's staff. The customer gets the same generic acknowledgement in every case (no tipping-off) |
| Cost and operations | Staffed queues in production; the console is a development tool |
| Trust and interoperability | Production needs the bank's case-management tool and staff IAM in front of the same service |
| Trade-off | Cases wait for a person, and the development console has no staff authentication |
| Evidence | `ReviewService.decide` (`app/services/review_service.py`); `test_manual_review_can_be_continued_by_an_operator_only` (`tests/test_state_machine.py`); `test_only_a_supervisor_may_reject_and_rejection_ends_the_agents_privileges`, `test_no_operator_decision_can_open_an_account`, `test_operator_identities_are_separate_from_agents_customers_and_tokens`, `test_there_is_no_operator_http_endpoint`, `test_the_operator_console_is_off_in_production` (`tests/test_review.py`); section E of the demo |
| Reversibility | Medium |
| Revisit if | The bank connects its case-management system and staff IAM, or a supervisor requires a specific escalation path |
| Status | Implemented with a development console and synthetic operators; a staffed production review is concept |
| Failure scenarios | A case is never picked up: the application stays parked, visible as action required, and never opens |
| Requirements | C4, G2, Q1 |

### D26 Evidence and audit

| Field | Content |
|---|---|
| Problem | Which evidence does the bank keep, and how is it protected? |
| Alternatives | Application logs only; a status table that can be updated; a tamper-evident hash chain or WORM store now |
| Criteria | R, T, C; decisive: R |
| Chosen | **Append-only audit and decision records, written in the same transaction as the state change.** Each request has a correlation id and each decision its policy version. The audit details hold no raw token, idempotency key or customer data. Database triggers refuse updates and deletes. A tamper-evident WORM store is the production target |
| Why chosen | Disputes and supervision need the order of events and the authority behind each one. Writing the audit in the same transaction means there is no state change without its evidence, and keeping data out of the audit is data minimization |
| Why not the alternatives | Logs can be lost and are not transactional. An updatable table can be rewritten. A hash chain or WORM store adds operations that a single-node prototype does not justify, so finding F-6 stays open |
| Security | A failed audit write rolls the state change back, and triggers refuse updates and deletes. But whoever controls the database file can still rewrite it: append-only is not tamper-evident (F-6) |
| Regulatory | Retention periods and the evidential form are production questions, LEGAL VALIDATION REQUIRED |
| Cost and operations | Storage grows; unauthenticated requests can add events, since there is no rate limit (F-4) |
| Trust and interoperability | Correlation ids travel in HTTP headers; the plain tables can be mirrored to a WORM store |
| Trade-off | Append-only inside one database file is no proof against an administrator |
| Evidence | `test_audit_events_are_append_only_at_database_level` (`tests/test_persistence.py`); `test_failed_audit_write_rolls_back_the_status_change`, `test_audit_details_contain_no_idempotency_keys_or_customer_data` (`tests/test_application_service.py`); `test_decisions_and_claims_are_append_only`, `test_policy_decisions_are_append_only`, `test_each_decision_records_its_inputs_and_policy_version` (`tests/test_trust_governance_policy.py`); `test_no_raw_token_is_stored_logged_or_audited` (`tests/test_agent_authorization.py`); `test_audit_correlation_id_matches_the_http_response_header` (`tests/test_applications_api.py`) |
| Reversibility | High: the records can be mirrored to a WORM store |
| Revisit if | Production: a WORM or hash-chained store and retention rules, which closes F-6 |
| Status | Implemented as append-only; tamper evidence is concept (F-6 open) |
| Failure scenarios | Someone with access to the database file rewrites history: not detected in the prototype (F-6) |
| Requirements | G2, Q1 |
