# Process and evidence matrix: today, the prototype, 2030

Case-study page 3, "Schritte und Nachweise". For each step of a depot opening this matrix records what happens today,
what the TRUSTEQ prototype implements, and what the 2030 target needs. Status: 7 November 2026.

**Three categories, kept apart in every block:**
- **TODAY** is the real-world process of the reference case. Source tags `[S#]` point to the register of
  [ist-analyse-union-investment.md](ist-analyse-union-investment.md#8-source-register). Untagged statements in this
  category are PROJECT INFERENCE.
- **PROTOTYPE** is the full-trust profile of this repository, run locally. Every external party is a labelled synthetic
  or mock stand-in. Nothing here is a production integration.
- **2030** is the target design from [../technical-concept-2030.md](../technical-concept-2030.md).

**Agent role**, in the 2030 column:
- **PERFORM**: the agent can do the step itself;
- **PREPARE**: the agent prepares, and the customer or the bank completes it;
- **EXPLAIN ONLY**: the agent may only explain.

Legal references are the R-rows of [../regulatory-policy-matrix.md](../regulatory-policy-matrix.md). Frictions FR1 to
FR7 are ranked in [ist-analyse-union-investment.md](ist-analyse-union-investment.md#5-frictions-evidence-and-prioritization).
Unless another file is named, tests are in `tests/test_onboarding_e2e.py`, whose positive test
`test_the_five_steps_are_one_journey_from_discovery_to_opened` runs every step below.

## The five onboarding steps, mapped to the real process

| Prototype step | Real-world steps | E2E evidence, rows of [page3-e2e-traceability.md](page3-e2e-traceability.md) | Key negative test | What differs from today |
|---|---|---|---|---|
| Before the five steps | 1 to 5: discovery, channel, documents, the application | rows 1 to 6 | `test_a_request_for_another_service_is_refused_not_turned_into_an_account` | Today the customer fills in a form with a person or online. In the prototype the agent drafts it with a delegated token |
| **1 IDENTITY** | 6, and the identity part of 12 | rows 4 and 7 | `test_a_forged_identity_result_cannot_verify_anybody` | Today the partner bank checks the ID in person, or an online ID or video provider does. In the prototype a synthetic provider sends a signed result, with `regulatory_evidence=false` |
| **2 TAX** | 7 | row 8 | `test_missing_tax_information_blocks_the_confirmation_and_the_submission` | Today: tax ID, US status, other residences, the church-tax query by the depot bank. In the prototype the form is format-checked, the identifier is discarded, and there is no BZSt query |
| **3 APPROPRIATENESS** | 8 and 9 | rows 9 and 10 | `test_no_agent_or_model_can_supply_or_override_the_assessment` | Today: knowledge and experience in the remote form; appropriateness for complex funds. In the prototype: synthetic rules, a warning instead of a rejection, and no order step |
| **4 CUSTOMER AUTHORIZATION + CONTRACT** | 10 and 11 | rows 11 to 13 | `test_an_agent_cannot_reach_the_customers_authorization_by_any_route` | Today: a handwritten signature or a TAN release. In the prototype: the exact action digest, an immutable package hash and a test signer without legal effect |
| **5 DEPOT OPENING** | 12, 13 and 14 | rows 15 and 16 | `test_a_forged_provider_callback_cannot_open_the_depot` | Today the depot bank accepts and sends the depot number. In the prototype only a signed callback from the mock provider sets `OPENED`, and the agent's mandate ends |

## Step 1: Discover a provider

| | Attribute | Content |
|---|---|---|
| **TODAY** | Business purpose | Find a fund provider and a channel the customer trusts |
| | Initiating actor | Customer |
| | Responsible actor | Customer; the partner bank where it advises `[S5]` |
| | Customer action | Visits a branch or a website |
| | Data collected | None |
| | Evidence generated | None |
| | Legal or contractual basis | None specific |
| | Applies when | Always |
| | Friction | FR7: nothing machine-verifiable for an agent; product pages served the homepage to automated requests (B13) |
| **PROTOTYPE** | Component | Synthetic trust authority, `agent_discovery.entity_configuration`, `agent_card`, `signed_profile`; the agent's `ProviderVerifier.verify` |
| | Route, service, record | `GET /.well-known/openid-federation`, `GET /.well-known/agent-card.json`, `GET /v1/agent/profile`. No record: public reading stores nothing |
| | Test evidence | `test_a_tampered_card_fails_its_signature`, `test_a_revoked_entity_fails`, `test_expired_statements_fail` (`tests/test_trust_discovery.py`) |
| **2030** | Agent role | PERFORM: discover and verify the provider |
| | Human participation | None |
| | Provider dependency | A sector trust anchor under agreed governance |
| | Open assumptions | Who operates the anchor; a regulated attestation of provider metadata does not exist yet |

## Step 2: Choose a distribution channel

| | Attribute | Content |
|---|---|---|
| **TODAY** | Business purpose | Decide where and how to open: branch, partner online banking, MeinInvest, JuniorDepot online |
| | Initiating actor | Customer |
| | Responsible actor | Customer; the partner bank decides whom it serves online `[S33]` |
| | Customer action | Goes to a branch, books an appointment, or logs in to online banking |
| | Data collected | None at this step |
| | Evidence generated | None |
| | Legal or contractual basis | Partner contracts; for the Komfort depot a framework agreement `[S1; S3]` |
| | Applies when | Always; online banking only for the bank's own account holders `[S33]` |
| | Friction | FR5: the channel decides what is possible, and partners may lose customers to direct channels |
| **PROTOTYPE** | Component | Channels in the signed manifest, `DistributionGovernanceService`, partner register, signed referral proofs |
| | Route, service, record | `POST /v1/applications/{id}/referrals`; `attribution_claims`, `attribution_decisions`, `channel_attributions` |
| | Test evidence | `test_an_invalid_referral_is_rejected_and_recorded`, `test_attribution_with_a_partner_goes_to_an_inducement_review` (`tests/test_trust_governance_policy.py`), `test_partner_attribution_is_not_overwritten_at_any_later_step` |
| **2030** | Agent role | PREPARE: show the options; the customer chooses, and a partner's proof is carried, never invented |
| | Human participation | A person settles attribution conflicts |
| | Provider dependency | Partner banks' signing keys; a governed partner register |
| | Open assumptions | The commission model is the partners' and the law's, not this design's (R13) |

## Step 3: Review products, costs, risks and documents

| | Attribute | Content |
|---|---|---|
| **TODAY** | Business purpose | The customer understands product, costs and risks before deciding |
| | Initiating actor | Partner bank or depot bank |
| | Responsible actor | KVG for the KID content; the selling or advising firm for providing it; the depot bank for its terms |
| | Customer action | Receives and reads KID, prospectus, reports, cost information, terms and the starter pack |
| | Data collected | None |
| | Evidence generated | The partner bank ticks that the documents came "rechtzeitig vor Vertragsschluss" `[S22]` |
| | Legal or contractual basis | R11 cost information, R12 KID |
| | Applies when | Before the first order; the application usually contains one `[S22]` |
| | Friction | FR3: separate documents, confirmed by a tick |
| **PROTOTYPE** | Component | Signed, versioned catalog with fees, risk class and document digests; verified facts; `CommunicationGuard` |
| | Route, service, record | A2A `product-information`, `product-comparison`, `document-retrieval`; `disclosure_receipts` at confirmation |
| | Test evidence | `test_a_fee_the_price_list_does_not_state_is_not_a_fact`, `test_a_withdrawn_product_in_a_signed_manifest_is_really_withdrawn` (`tests/test_communication_boundary.py`), `test_every_published_claim_cites_its_source_and_the_product_version`, `test_an_expired_manifest_publishes_no_claims` (`tests/test_agent_trusted_claims.py`) |
| **2030** | Agent role | EXPLAIN ONLY: present the bank's verified facts with sources |
| | Human participation | None for facts. A question the facts cannot answer goes to a person |
| | Provider dependency | Authoritative product feeds and KIDs from the KVGs |
| | Open assumptions | How a third party's agent displays signed facts is outside the bank's control |

## Step 4: Request a depot relationship

| | Attribute | Content |
|---|---|---|
| **TODAY** | Business purpose | The customer makes a binding offer to the depot bank |
| | Initiating actor | Customer |
| | Responsible actor | Depot bank, as the contract partner `[S1 "Zustandekommen"]` |
| | Customer action | Fills in form 004185 or the online flow |
| | Data collected | See step 5; also delivery channel, first order, SEPA mandate `[S22]` |
| | Evidence generated | The application |
| | Legal or contractual basis | The depot terms `[S1]` |
| | Applies when | Always |
| | Friction | FR1: a form that is returned when altered or incomplete |
| **PROTOTYPE** | Component | `DelegationService`, `ApplicationService.create_application`, `_refuse_other_services`, `OnboardingService.request_handoff` |
| | Route, service, record | `POST /v1/delegation/par`, `POST /v1/delegation/token`, `POST /a2a/v1` skill `depot-onboarding` (`start`, `request_customer_confirmation`); `applications`, `delegations`, `handoffs` |
| | Test evidence | `test_a_valid_delegation_gives_the_agent_a_bound_token_and_no_customer_secret`, `test_the_customer_comes_from_the_delegation_not_from_the_agent` (`tests/test_trust_identity_delegation.py`), `test_a_request_for_another_service_is_refused_not_turned_into_an_account` |
| **2030** | Agent role | PREPARE: draft the application within a scoped delegation |
| | Human participation | The customer delegates and later confirms |
| | Provider dependency | The bank's identity provider; a FAPI profile for agent clients |
| | Open assumptions | Whether agent delegation has civil-law or GwG effects (regulatory matrix, assumption 8) |

## Step 5: Provide personal information

| | Attribute | Content |
|---|---|---|
| **TODAY** | Business purpose | Collect the identity data the law requires |
| | Initiating actor | Customer |
| | Responsible actor | Depot bank as obliged entity |
| | Customer action | Enters name, address, date, place and country of birth, nationality, occupation; phone and e-mail optional `[S22]` |
| | Data collected | As listed |
| | Evidence generated | The application |
| | Legal or contractual basis | R01 (GwG §11(4) no. 1), R05 |
| | Applies when | Always |
| | Friction | FR1 |
| **PROTOTYPE** | Component | OIDC login; the customer is known as `customer_ref`, derived from issuer and subject. No personal-data form |
| | Route, service, record | `GET /customer/oidc/login`, `GET /customer/oidc/callback`; `identity_bindings`, `authentication_events`, `customer_sessions` |
| | Test evidence | `test_a_wrong_id_token_is_refused`, `test_the_state_works_once_and_only_for_the_browser_that_began` (`tests/test_trust_identity_delegation.py`) |
| **2030** | Agent role | PREPARE: prefill what the customer allows; the verified values come from the identification |
| | Human participation | The customer confirms the data |
| | Provider dependency | Identification provider or the EUDI Wallet for verified attributes |
| | Open assumptions | Which attributes may be taken from a wallet presentation |

## Step 6: Identity verification

| | Attribute | Content |
|---|---|---|
| **TODAY** | Business purpose | Identify the customer before the relationship |
| | Initiating actor | Partner bank, or the remote channel |
| | Responsible actor | Depot bank, presumably, as obliged entity; executed by the partner bank in the branch `[S22]` or by an identification provider `[S24, S26]` |
| | Customer action | Shows the ID in person, or uses the online ID or a video call |
| | Data collected | Identity document data; a copy of the ID `[S22]` |
| | Evidence generated | Presence confirmation and ID copy `[S22]`; the provider's identification record |
| | Legal or contractual basis | R01 to R03 |
| | Applies when | Always, "gegebenenfalls erforderlich" `[S1 §2.1]`; for minors all guardians `[S24, S37]` |
| | Friction | FR2 |
| **PROTOTYPE** | Component | `IdentityProofingService`, gate `TrustGates.confirm_missing`, synthetic proofing provider |
| | Route, service, record | `POST /customer/handoffs/{id}/identity-proofing`, `POST /internal/identity-proofing/callbacks`; `identity_proofing_cases`, with no document or biometric data |
| | Test evidence | `test_a_failed_identification_goes_to_a_person_and_never_opens`, `test_dev_sign_in_alone_cannot_start_proofing` (`tests/test_trust_identity_delegation.py`), `test_an_invalid_identity_cannot_continue_at_any_later_gate`, `test_a_forged_identity_result_cannot_verify_anybody` |
| **2030** | Agent role | EXPLAIN ONLY: start the bank's step through the handoff, never handle documents |
| | Human participation | The customer, personally. A person for failed or expired results |
| | Provider dependency | A contracted identification provider; later the EUDI Wallet (R16) |
| | Open assumptions | The legal basis between depot bank and partner bank (G4) |

## Step 7: Tax information

| | Attribute | Content |
|---|---|---|
| **TODAY** | Business purpose | Withholding, reporting and account legitimation |
| | Initiating actor | Customer, on the form |
| | Responsible actor | Depot bank as paying agent and reporting institution |
| | Customer action | Tax ID, tax status abroad, US status, other residences on a supplementary form `[S22]` |
| | Data collected | As listed. The church-tax attribute comes from the BZSt `[S2 §11.4]` |
| | Evidence generated | The application; the church-tax query |
| | Legal or contractual basis | R05 to R07 |
| | Applies when | Always; the supplementary form only for other tax residences |
| | Friction | B5, below the top seven |
| **PROTOTYPE** | Component | `CustomerInformationService.submit_tax`, `validate_tax_information` |
| | Route, service, record | `POST /customer/handoffs/{id}/tax`, `POST /v1/customer/handoffs/{id}/tax-information`; `tax_information` with no identifier column |
| | Test evidence | `test_complete_tax_information_is_stored_without_the_identifier` (`tests/test_customer_information.py`), `test_missing_tax_information_blocks_the_confirmation_and_the_submission`, `test_invalid_tax_information_is_refused_and_keeps_the_gate_closed` |
| **2030** | Agent role | EXPLAIN ONLY: the tax ID and the self-certification are the customer's own declarations |
| | Human participation | The customer, personally |
| | Provider dependency | BZSt interfaces; encrypted storage |
| | Open assumptions | The church-tax objection field is a simplification (matrix assumption 9); one residence only (assumption 10) |

## Step 8: Knowledge and experience

| | Attribute | Content |
|---|---|---|
| **TODAY** | Business purpose | Assess appropriateness for non-advised orders |
| | Initiating actor | Depot bank or partner bank |
| | Responsible actor | The firm providing the service; the depot bank for complex funds `[S1 §7.2]` |
| | Customer action | Answers the questions, in the remote form at opening `[S23]` |
| | Data collected | Knowledge and experience |
| | Evidence generated | Answers and assessment |
| | Legal or contractual basis | R08, R09 |
| | Applies when | Non-advised orders in complex funds; not for UCITS execution-only `[S1 §7.2]` |
| | Friction | FR4 |
| **PROTOTYPE** | Component | `CustomerInformationService.submit_assessment`, `acknowledge_assessment`; `appropriateness.assess` with rules `synthetic-2026.11` |
| | Route, service, record | `POST /customer/handoffs/{id}/assessment`, `POST /v1/customer/handoffs/{id}/appropriateness`; `appropriateness_assessments` with `assessed_for_service` |
| | Test evidence | `test_a_warning_needs_the_customers_decision_and_does_not_reject_the_account`, `test_no_agent_or_model_can_supply_or_override_the_assessment` |
| **2030** | Agent role | EXPLAIN ONLY: explain the questions and what a warning means |
| | Human participation | The customer answers and decides |
| | Provider dependency | Compliance-owned rule sets per product |
| | Open assumptions | Reusing answers from the opening for a later order (matrix assumption 3) |

## Step 9: Disclosures, warnings or suitability

| | Attribute | Content |
|---|---|---|
| **TODAY** | Business purpose | Apply the regime that fits the service |
| | Initiating actor | Partner bank or depot bank |
| | Responsible actor | The advising partner bank for suitability; the depot bank for non-advised orders |
| | Customer action | Receives a Geeignetheitserklärung after advice, a warning, or signs an execution-only declaration `[S22; S23]` |
| | Data collected | For advice: finances, objectives, risk tolerance |
| | Evidence generated | Geeignetheitserklärung; warning; execution-only declaration |
| | Legal or contractual basis | R08 to R10 |
| | Applies when | Depends on the service type: four on the partner's sheet `[S22]` |
| | Friction | FR4 |
| **PROTOTYPE** | Component | `regulatory_policy.evaluate`, `service_applicability`; advice is not offered |
| | Route, service, record | Inside the confirmation; `policy_decisions` (append-only), audit `policy.evaluated` |
| | Test evidence | `test_guarantees_and_personal_recommendations_go_to_a_person` (`tests/test_bank_claims.py`), `test_each_service_has_its_own_decision` |
| **2030** | Agent role | EXPLAIN ONLY: the agent never advises in the bank's name |
| | Human participation | An advisor, when the customer wants advice |
| | Provider dependency | The advising partner bank |
| | Open assumptions | Whether an agent-prepared order is at the client's initiative (matrix assumption 6) |

## Step 10: Review the contract terms

| | Attribute | Content |
|---|---|---|
| **TODAY** | Business purpose | Agree the terms and price list that apply |
| | Initiating actor | Depot bank |
| | Responsible actor | Depot bank |
| | Customer action | Receives the terms, special terms and price list, agreed at opening `[S1 §1.2]` |
| | Data collected | None |
| | Evidence generated | The versions in force at agreement |
| | Legal or contractual basis | The depot terms; R15 |
| | Applies when | Always |
| | Friction | FR3 |
| **PROTOTYPE** | Component | `ContractExecutionService.prepare_package`: the exact terms, versions and hash |
| | Route, service, record | `GET /customer/applications/{id}/contract`; `contract_packages`, immutable by trigger |
| | Test evidence | `test_every_approved_term_stays_visible_on_the_review_page` (`tests/test_customer_pages.py`), `test_a_package_rewritten_with_a_matching_hash_is_still_refused`, `test_a_change_of_terms_after_the_signature_invalidates_it_and_goes_to_a_person` |
| **2030** | Agent role | EXPLAIN ONLY: summarize the verified terms |
| | Human participation | The customer reviews them |
| | Provider dependency | The bank's contract generation |
| | Open assumptions | None beyond R15 |

## Step 11: Consent and signature

| | Attribute | Content |
|---|---|---|
| **TODAY** | Business purpose | A binding declaration by the customer |
| | Initiating actor | Customer |
| | Responsible actor | Customer; the depot bank designs the process |
| | Customer action | Signs by hand, or releases by TAN online `[S22; S27; S36]` |
| | Data collected | Signatures; optional data-processing consent `[S22]` |
| | Evidence generated | The signed application, kept by the partner bank `[S22]`; the TAN release |
| | Legal or contractual basis | R15: no QES found to be required |
| | Applies when | Always |
| | Friction | FR1 |
| **PROTOTYPE** | Component | `OnboardingService.confirm_handoff`; `ContractExecutionService.authorize_signing`, `handle_callback`; test signer |
| | Route, service, record | `POST /customer/handoffs/{id}/confirm`, `POST /customer/applications/{id}/contract/authorize`, `POST /internal/signature-provider/callbacks`; `consent_receipts`, `signature_sessions` at `TEST_NO_LEGAL_EFFECT` |
| | Test evidence | `test_a_correctly_signed_but_wrong_message_is_refused` (`tests/test_trust_contract_signing.py`), `test_an_agent_cannot_reach_the_customers_authorization_by_any_route`, `test_authorization_is_one_time_while_a_signature_is_running` |
| **2030** | Agent role | PREPARE: show the exact action and hand over; the customer consents and signs |
| | Human participation | The customer, personally |
| | Provider dependency | A QTSP only where a form requires a QES |
| | Open assumptions | The form of each declaration (R15) |

## Step 12: The provider processes the application

| | Attribute | Content |
|---|---|---|
| **TODAY** | Business purpose | Check completeness and run the due diligence |
| | Initiating actor | Partner bank, by transmitting electronically `[S22]` |
| | Responsible actor | Depot bank |
| | Customer action | None, unless asked to correct |
| | Data collected | Screening results (internal, not public) |
| | Evidence generated | The due-diligence record |
| | Legal or contractual basis | R02, R04 |
| | Applies when | Always |
| | Friction | FR1: returned forms |
| **PROTOTYPE** | Component | `OnboardingService.advance_checks` with mock KYC and compliance providers; the review service |
| | Route, service, record | Inside the confirmation; `check_results`, `review_cases` |
| | Test evidence | `test_a_check_that_does_not_pass_stops_the_workflow_in_manual_review` (`tests/test_onboarding_flow.py`), `test_manual_review_remembers_and_resumes_the_interrupted_step` (`tests/test_application_service.py`), `test_after_a_crash_and_restart_the_next_submit_resumes_and_completes` (`tests/test_recovery.py`) |
| **2030** | Agent role | None: it sees a generic status |
| | Human participation | A person for review cases |
| | Provider dependency | Screening provider for sanctions and PEP |
| | Open assumptions | Risk rating and monitoring are out of scope |

## Step 13: Depot opened, rejected or referred

| | Attribute | Content |
|---|---|---|
| **TODAY** | Business purpose | The contract comes into existence |
| | Initiating actor | Depot bank |
| | Responsible actor | Depot bank |
| | Customer action | None |
| | Data collected | None |
| | Evidence generated | The acceptance with the depot number `[S1 §2.1]` |
| | Legal or contractual basis | The depot terms |
| | Applies when | Acceptance, or refusal of an incomplete application `[S1 §2.1]` |
| | Friction | FR6 |
| **PROTOTYPE** | Component | `OnboardingService.submit_application`, `TrustGates.submit_missing`, `VersionedDepotAdapter`, `handle_provider_confirmation` |
| | Route, service, record | `POST /v1/applications/{id}/submit`, `POST /internal/depot-provider/confirmations`; `depot_submissions`; `OPENED` only from the signed callback |
| | Test evidence | `test_the_agent_cannot_submit_before_the_signature` (`tests/test_trust_contract_signing.py`), `test_a_provider_outage_needs_a_person_and_is_not_evidence` (`tests/test_regulated_boundaries.py`), `test_a_forged_provider_callback_cannot_open_the_depot`, `test_a_duplicate_provider_callback_has_no_second_effect`, `test_provider_errors_fail_safely_and_create_no_opened` |
| **2030** | Agent role | PREPARE: submit after consent; the bank's gates decide |
| | Human participation | A person when the provider fails |
| | Provider dependency | The custodian's API with signed events |
| | Open assumptions | Reconciliation after a timeout (decision D14) |

## Step 14: Confirmation and documents

| | Attribute | Content |
|---|---|---|
| **TODAY** | Business purpose | Tell the customer and give access |
| | Initiating actor | Depot bank |
| | Responsible actor | Depot bank |
| | Customer action | Registers for online access separately `[S25]` |
| | Data collected | E-mail, mobile number |
| | Evidence generated | Notice with the depot number; text-form confirmation for MeinInvest `[S27]`; electronic mailbox |
| | Legal or contractual basis | The depot terms; R14 records |
| | Applies when | Always |
| | Friction | FR6 |
| **PROTOTYPE** | Component | Generic status for the agent; the customer's status page; the mandate is revoked at `OPENED` |
| | Route, service, record | A2A status, `read_status` scope; `audit_events` with a correlation id |
| | Test evidence | `test_a_valid_confirmation_opens_the_application_and_revokes_the_mandate` (`tests/test_provider_callback.py`), `test_after_opening_only_the_read_only_status_survives` (`tests/test_agent_authorization.py`), `test_agent_creates_a_draft_and_reads_its_generic_status_via_rest` (`tests/test_agent_rest_integration.py`) |
| **2030** | Agent role | PERFORM: track the generic status |
| | Human participation | None |
| | Provider dependency | The bank's document delivery |
| | Open assumptions | Processing time and delivery channel today (G20) |

## Across all steps: a person on request

Today advisers and the service centre are reachable by branch, phone and chat `[S5]`. In the prototype the customer can
ask for a person at every active step, and doing so never skips a mandatory check:
`test_asking_for_a_person_never_bypasses_the_mandatory_checks` (`tests/test_request_human_security.py`).
Operators continue only through the trusted console:
`test_manual_review_can_be_continued_by_an_operator_only` (`tests/test_state_machine.py`).
