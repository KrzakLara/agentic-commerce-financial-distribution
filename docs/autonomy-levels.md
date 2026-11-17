# Autonomy levels: from assisted to fully autonomous agentic commerce

This document derives the levels of agent autonomy for opening a securities account, describes who decides at each
level and what must be in place, and explains why the prototype targets L3 and does not go further.

Status: 7 November 2026.

**These levels are this project's architecture levels, not an official regulatory classification.** They describe a
journey as a whole; one journey may mix levels step by step. They match the table in
[technical-concept-2030.md](technical-concept-2030.md#3-autonomy-levels-l0-to-l4).

## 1. Where we stand today

In the reference case the depot opening is **L0**:
- human-led, on paper or in online banking, with a handwritten signature or a TAN release;
- no machine-readable or verifiable entry point for an agent was found;
- Union Investment's product pages served the homepage to automated requests (Ist-Analyse, B13).

General-purpose AI assistants can already research providers on public pages, which is L1 without verification.
**This is an assessment from one provider group's public documents, not a market survey.**

## 2. The five levels

| Level | Name | What the agent may do | What still needs the customer | Example |
|---|---|---|---|---|
| **L0** | Human-led digital journey | Read public web pages like a browser | Everything, on the bank's site or in the branch | Online application today |
| **L1** | Informational assistant | Read the signed catalog and cite verified facts; ask the bank, which answers from verified facts | Every action | "What does the depot cost?" with sources |
| **L2** | Delegated preparation | L1, plus prepare a draft the customer completes alone | All statements, all binding acts | Prefilled application |
| **L3** | **Controlled agent-led commerce (chosen)** | L2, plus orchestrate the whole journey within a narrow, revocable mandate: request the handoff, track a generic status, submit after consent | Identity, tax statements, answers, confirmation of the exact action, signature | This prototype |
| **L4** | Fully autonomous delegated conclusion | Conclude on the customer's behalf under a standing mandate, without approval per action | Setting and revoking the mandate; reviewing afterwards | Not supportable today |

**"Fully autonomous" (L4)** means the agent concludes binding steps as the customer's delegate: identification,
declarations, consent and signature, without the customer approving each one.

## 3. Who decides at each level

The actors are:
- **C:** customer;
- **A:** the agent, which **prepares or proposes only** unless stated otherwise;
- **P:** the bank's deterministic policy;
- **R:** a regulated provider (identification, signer, depot provider);
- **H:** a human compliance operator.

"L4" entries describe the hypothetical level and are legally open.

| Decision | L0 | L1 | L2 | L3 | L4 |
|---|---|---|---|---|---|
| Provider discovery | C | A, verified | A | A | A |
| Product comparison | C | A explains facts; C compares | A; C | A; C | A within the mandate |
| Product recommendation (advice) | Advising bank | Advising bank | Advising bank | Advising bank; never the agent in the bank's name | A within the mandate, legally open |
| Application preparation | C | C | A drafts; C completes | A prepares | A |
| Customer identity | C with R | C with R | C with R | C with R, in the bank's channel | A presents a wallet-bound mandate; legally open |
| Tax information | C | C | A prefills; C declares | C | A from the mandate; legally open |
| Appropriateness answers | C | C | C | C; P assesses, warns, never rejects | A; legally open, because warnings must reach the customer |
| Delegation scopes | - | - | C | C, per journey | C, once, standing |
| Contract acceptance | C | C | C | C, for the exact action | A under the mandate |
| Electronic signing | C | C | C | C; R signs | Delegate signature; legally open |
| Provider submission | P after C | P after C | P after C | A submits after consent; P gates | A; P gates |
| Final account-opening decision | R | R | R | R: the signed confirmation | R |
| Manual review | H | H | H | H | H |
| Investment order execution | C | C | C | Not in scope | A under the mandate |

The last three rows never move to the agent. At every level the bank's policy decides what applies, and a person
handles exceptions.

## 4. Prerequisites per level

| Prerequisite | L1 | L2 | L3 | L4 |
|---|---|---|---|---|
| Technology | Signed catalog, verified-fact answers | Draft API, idempotency | Durable workflow, customer channel, typed agent tools | Verifiable mandates, per-action mandate checks |
| Identity assurance | None for reading | Customer login | Customer login plus GwG identification in the bank channel | Identification by the delegate, legally recognized |
| Delegation | None | A scoped token | Scoped, sender-bound, revocable delegation per journey | A standing, legally effective mandate |
| Audit | Released and refused answers | Drafts | Every step, decision and version | The same, plus every mandate use |
| Operational controls | Fact governance | Draft limits | Review cases, gates, monitoring | Mandate limits, anomaly detection, liability handling |
| Legal | Marketing and information rules | The same | Customer acts stay personal: no new legal basis needed | Delegation of identification, consent and signature: **no established basis** |
| Security | Signed metadata | Client authentication | Proof of possession, replay protection, injection containment | All of L3, plus mandate theft and misuse |
| Interoperability | Machine-readable provider metadata | A draft API | Agent protocols, provider adapters | Mandate formats across providers |
| Customer protection | Verified facts only | The customer completes | The customer confirms the exact action and signs | After-the-fact review only |
| Failure management | A fixed refusal text | Drafts expire | Fail closed, review cases | Unwinding acts already concluded |

**Missing in the live ecosystem:** a governed trust anchor for provider identity (L1 and up); a common delegation
format (L2 and up); agent-facing custodian APIs (L3); any legal basis for a delegate's identification, consent and
signature (L4).

**Missing in the prototype:** real identification, KYC, a QES where needed, a real custodian, a bank IdP, and a live
model evaluation. **The prototype implements the mechanics of L3 with synthetic parties. It does not show L3 in a
regulated setting.**

## 5. The target: L3, controlled agent-led commerce

| Criterion | Why L3 |
|---|---|
| Customer experience | The agent does the repeatable work: finding, comparing, preparing, tracking. The customer acts only where it binds them |
| Operational scalability | Repeatable steps are automated; exceptions go to durable review cases |
| Legal uncertainty | Identification, consent and signature stay personal acts, so no new legal basis is needed |
| Trust | Provider identity is verified by signature; claims are verified; the agent is untrusted |
| Liability | A clear cut: the agent proposes, the customer and the bank decide |
| Security | No shared credentials; scopes, proof of possession, revocation; the agent has no transition |
| Existing partner relationships | Attribution from evidence; conflicts to a person; the account continues |
| Feasibility | Every mechanism runs today with synthetic parties; real providers plug in behind ports |
| Adaptability to 2030 | Each step can move up a level on its own once its preconditions hold |

**What stays with the customer:** identity, tax statements, answers, confirmation of the exact action, signature,
revocation, and the choice of channel.

**What stays with the bank:** every state transition, policy applicability, KYC and AML, the contract, the opening
through its custodian, review cases, attribution.

**What can be delegated to the agent:** discovery, verification, reading, comparison of verified facts, preparing the
application, requesting the handoff, tracking a generic status, submitting after consent.

**Where a person steps in:**
- failed or expired identification;
- review-required KYC or compliance results;
- a declined or failed signature;
- terms that changed after signing;
- provider failures;
- attribution conflicts;
- any request by the customer to speak to a person.

**Why not L4 now:**
- No established legal basis exists for a delegate's identification under the GwG, or for consent and signature by
  an agent.
- Appropriateness warnings must reach the customer, not the agent.
- Liability when an agent errs is unsettled.
- No verifiable mandate format exists across banks yet.
- **L4 is not inherently unlawful for every future case.** It may become possible when the law, mandate models
  (for example EUDI-based) and provider liability support it (decision D01, revisit trigger).

## 6. The L3 characteristics in the prototype

| L3 characteristic | Where | Proof | Status |
|---|---|---|---|
| The agent holds no state transition | `app/domain/workflow.py` | `test_the_external_agent_can_take_no_edge_at_all` (`tests/test_state_machine.py`) | LOCAL VERIFIED CONTROL |
| Only the provider's signed confirmation opens a depot | `OnboardingService.handle_provider_confirmation` | `test_only_the_provider_confirmation_can_open_a_depot` (`tests/test_state_machine.py`), `test_a_forged_provider_callback_cannot_open_the_depot` | LOCAL VERIFIED CONTROL, mock provider |
| The customer authenticates in the bank's channel | `OidcLoginService`, `GET /customer/oidc/login` | `test_a_wrong_pkce_verifier_is_refused_by_the_provider` (`tests/test_trust_identity_delegation.py`) | LOCAL, synthetic IdP |
| The customer decides the delegation; the agent cannot | `DelegationService.decide` | `test_the_agent_cannot_decide_its_own_delegation` (`tests/test_trust_identity_delegation.py`) | LOCAL VERIFIED CONTROL |
| The agent cannot reach the customer's authorization | Customer routes refuse agent tokens | `test_an_agent_cannot_reach_the_customers_authorization_by_any_route` | LOCAL VERIFIED CONTROL |
| Identity, tax and answers before the confirmation | `TrustGates.confirm_missing` | `test_the_bank_refuses_a_confirmation_without_the_information` (`tests/test_customer_information.py`) | LOCAL, synthetic proofing |
| No submission before the signature | `TrustGates.submit_missing` | `test_the_agent_cannot_submit_before_the_signature` (`tests/test_trust_contract_signing.py`) | LOCAL, test signer |
| Only a person leaves manual review | `ReviewService.decide`, `scripts/dev_operator.py` | `test_manual_review_can_be_continued_by_an_operator_only` (`tests/test_state_machine.py`) | LOCAL, synthetic operators |
| No model decides policy or attribution | `regulatory_policy`, `DistributionGovernanceService.resolve_by_person` | `test_a_model_as_the_decider_is_refused`, `test_the_channel_conflict_business_example_end_to_end` (`tests/test_trust_governance_policy.py`) | LOCAL VERIFIED CONTROL |

**Not claimed:** that local synthetic identity proofing or a test signer demonstrates production-grade L3.
