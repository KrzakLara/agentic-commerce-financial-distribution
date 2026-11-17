# The prototyped end-to-end scenario, traced to code and tests

Case-study page 3 asks for one concrete end-to-end scenario, built as a prototype. This is that scenario, step by step,
as the code runs it. Checked on 2026-11-07 against the working tree on `feature/llm-communication-onboarding` (on top
of `4cbcee4`, uncommitted), after the two onboarding-audit fixes.

**Scope.** The full-trust profile only: `full_trust_profile=true` with `customer_identity_mode=oidc`, as configured
by `tests/trust_harness.py` and `scripts/trust_stack.py`. The settings refuse the profile in any other identity mode.
The four older demos run the default development profile (DEV sign-in, no identity-proofing or signature gate) and
are **not** evidence for this path. Diagram: [../diagrams/03-full-trust-e2e-sequence.mmd](../diagrams/03-full-trust-e2e-sequence.mmd).

**Classes.** **LOCAL** = implemented and integrated locally, with tests. **SYNTHETIC** = a labelled local stand-in for
an external party. **MOCK** = a fixed or scripted response. **ADAPTER READY** = the boundary exists, the real
counterpart does not. **2030 TARGET** = design only. Nothing is connected to a real eID, KYC, QTSP, bank or custodian.

## 1. The journey

Tests are in `tests/test_onboarding_e2e.py` unless another file is named. The positive test for every row is
`test_the_five_steps_are_one_journey_from_discovery_to_opened`, which runs all seventeen points over the real routes in
one process. `scripts.demo_trust_journey` runs the same journey across separate processes (bank, trust authority, OIDC
provider, synthetic providers, and the agent as its own process).

| # | Step | Implementation | Route | State | Evidence record | Who may act | Negative test | Demo | Class |
|---|---|---|---|---|---|---|---|---|---|
| 1 | Discover the provider | `local_services/trust_authority.py` list; `agent_discovery.entity_configuration` | authority `/list`, `GET /.well-known/openid-federation` | - | none (public reading stores nothing) | anyone | `test_a_tampered_card_fails_its_signature` (`test_trust_discovery.py`) | yes, part A and C | LOCAL, authority SYNTHETIC |
| 2 | Verify provider metadata | `external_agent/trust.py` `ProviderVerifier.verify`; `agent_discovery.agent_card`, `signed_profile` | `GET /.well-known/agent-card.json`, `GET /v1/agent/profile` | - | none | the agent verifies; the bank signs | demo part C: untrusted anchor, bad signature | yes | LOCAL. Licence stays `NOT_ESTABLISHED` |
| 3 | Read verified product facts | `CommunicationGuard.verified_answer`, `verified_facts.build_fact_lookup` | A2A `product-information` | - | none | anyone | `test_a_fee_the_price_list_does_not_state_is_not_a_fact` (`test_communication_boundary.py`) | yes | LOCAL, catalog SYNTHETIC |
| 4 | Customer signs in | `OidcLoginService.begin`, `complete`, `record` | `GET /customer/oidc/login`, `GET /customer/oidc/callback` | - | `oidc_login_states`, `identity_bindings`, `authentication_events`, `customer_sessions` | customer, at the IdP | `test_a_wrong_id_token_is_refused`, `test_the_state_works_once_and_only_for_the_browser_that_began`, `test_a_wrong_pkce_verifier_is_refused_by_the_provider` (`test_trust_identity_delegation.py`) | yes | LOCAL, IdP SYNTHETIC |
| 5 | Customer delegates | `DelegationService.push_request`, `decide`, `exchange` | `POST /v1/delegation/par`, `GET`/`POST /customer/delegations/authorize`, `POST /v1/delegation/token` | - | `delegations`, `used_jtis` | agent asks (registered key, DPoP), customer decides with an OIDC session | `test_the_agent_cannot_decide_its_own_delegation`, `test_a_replayed_proof_fails`, `test_dev_sign_in_alone_cannot_start_proofing` (`test_trust_identity_delegation.py`) | yes, revocation in part D | LOCAL |
| 6 | Prepare the application | `_onboarding_skill` start, `ApplicationService.create_application`, `_refuse_other_services`; then `OnboardingService.request_handoff` | `POST /a2a/v1` skill `depot-onboarding`, actions `start` and `request_customer_confirmation` | none -> `DRAFT` -> `AWAITING_AUTHORIZATION` | `applications`, `mandates`, `delegated_mandates`, `handoffs`, attribution rows | agent with a delegated token | `test_the_customer_comes_from_the_delegation_not_from_the_agent` (`test_trust_identity_delegation.py`), `test_a_request_for_another_service_is_refused_not_turned_into_an_account` | yes | LOCAL |
| 7 | **1 Identity proofing** | `IdentityProofingService.start`, `handle_callback`; gate `TrustGates.confirm_missing` | `POST /customer/handoffs/{id}/identity-proofing`, `POST /internal/identity-proofing/callbacks` | stays `AWAITING_AUTHORIZATION`; a failed result -> `MANUAL_REVIEW` | `identity_proofing_cases`, `used_jtis`, `review_cases` | customer with an OIDC session; provider by an ES256 event with a pinned key | `test_an_invalid_identity_cannot_continue_at_any_later_gate`, `test_a_forged_identity_result_cannot_verify_anybody`, `test_a_replayed_identity_result_changes_nothing`, `test_another_customers_identity_result_does_not_count` | yes, failure in part E | SYNTHETIC provider, LOCAL gate |
| 8 | **2 Tax information** | `CustomerInformationService.submit_tax`, `validate_tax_information` | `POST /customer/handoffs/{id}/tax`, `POST /v1/customer/handoffs/{id}/tax-information` | unchanged | `tax_information` (no identifier column), audit with statuses only | customer: session, CSRF, one-time nonce, ownership | `test_missing_tax_information_blocks_the_confirmation_and_the_submission`, `test_invalid_tax_information_is_refused_and_keeps_the_gate_closed`, `test_another_customer_cannot_change_the_tax_information_on_either_route` | yes | LOCAL, format check only |
| 9 | **3 Knowledge and experience** | `CustomerInformationService.submit_assessment`, `acknowledge_assessment`; `appropriateness.assess` | `POST /customer/handoffs/{id}/assessment`, `POST /v1/customer/handoffs/{id}/appropriateness` | unchanged | `appropriateness_assessments`, audit with rule-set version and `assessed_for_service` | customer only | `test_a_warning_needs_the_customers_decision_and_does_not_reject_the_account`, `test_no_agent_or_model_can_supply_or_override_the_assessment` | yes, warning in part F | LOCAL, rules SYNTHETIC |
| 10 | Bank policy decides what applies | `TrustGates.consent_evidence`, `regulatory_policy.evaluate`, `service_applicability` | inside step 11 | - | `policy_decisions` (append-only), audit `policy.evaluated` | bank policy, never a model | `test_each_service_has_its_own_decision` | yes | LOCAL. An engineering control, not legal advice |
| 11 | Customer confirms the exact action | `OnboardingService.confirm_handoff`, then `advance_checks` | `POST /customer/handoffs/{id}/confirm`, `POST /v1/customer/handoffs/{id}/confirm` | `AWAITING_AUTHORIZATION` -> `AUTHORIZED` -> `KYC_PENDING` -> `COMPLIANCE_PENDING` -> `CONTRACT_PENDING` | `consent_receipts`, `disclosure_receipts`, `channel_attributions`, `check_results` | customer (`CUSTOMER_CHANNEL`), then bank policy | `test_the_json_confirmation_is_refused_without_proofing` (`test_trust_identity_delegation.py`), `test_an_agent_cannot_reach_the_customers_authorization_by_any_route` | yes | LOCAL; KYC and compliance MOCK |
| 12 | **4 Contract package and authorization** | `ContractExecutionService.prepare_package`, `authorize_signing` | `GET /customer/applications/{id}/contract`, `POST .../contract/authorize` | unchanged | `contract_packages` (append-only, SHA-256 of the canonical JSON), `signature_sessions` | customer: session, CSRF, the package hash | `test_the_package_cannot_be_changed_or_deleted` (`test_trust_contract_signing.py`), `test_a_package_rewritten_with_a_matching_hash_is_still_refused`, `test_authorization_is_one_time_while_a_signature_is_running` | yes | LOCAL |
| 13 | Test signature | `ContractExecutionService.handle_callback`, `signature_gate` | `POST /internal/signature-provider/callbacks` | unchanged; a failed signature -> `MANUAL_REVIEW` | `signature_sessions` at `TEST_NO_LEGAL_EFFECT` | signer by an ES256 event bound to package, signatory and level | `test_a_correctly_signed_but_wrong_message_is_refused` (`test_trust_contract_signing.py`), `test_a_failed_signature_cannot_open_the_depot` | yes, failure in part F | SYNTHETIC signer. QES ADAPTER READY, no QTSP |
| 14 | The agent asks a product question | `VerifiedLlmAgent.answer`, `CommunicationGuard.release` | A2A `product-question`, `POST /v1/agent/questions` | - | audit `communication.answer_released` or `communication.answer_withheld` | agent with its token; the answer is rendered by the bank | `test_an_unsupported_claim_is_not_released_and_the_refusal_is_audited`, `test_a_question_cannot_be_tied_to_another_customers_application` | yes | LOCAL, offline proposer. Real model ADAPTER READY, not live-validated |
| 15 | **5 Submission** | `OnboardingService.submit_application`, `TrustGates.submit_missing`, `VersionedDepotAdapter` | A2A `submit`, `POST /v1/applications/{id}/submit` | `CONTRACT_PENDING` -> `SUBMITTED_TO_DEPOT_PROVIDER` | `depot_submissions`, audit `application.submitted` with the full evidence | agent with `submit_after_consent`; the bank's gates decide | `test_the_agent_cannot_submit_before_the_signature` (`test_trust_contract_signing.py`), `test_the_workflow_keeps_its_gates_even_if_that_setting_is_bypassed` | yes | LOCAL, provider MOCK |
| 16 | **5 Depot opening** | `OnboardingService.handle_provider_confirmation`; `provider_callback.verify` | `POST /internal/depot-provider/confirmations` | `SUBMITTED_TO_DEPOT_PROVIDER` -> `OPENED`, the only edge into it | `depot_submissions` confirmation id, audit `provider.confirmation_accepted` | depot provider: HMAC over timestamp and body, within 300 s, matching `provider_ref` | `test_a_forged_provider_callback_cannot_open_the_depot`, `test_a_correctly_signed_but_wrong_callback_cannot_open_the_depot`, `test_a_duplicate_provider_callback_has_no_second_effect`, `test_provider_errors_fail_safely_and_create_no_opened` | yes | LOCAL, provider MOCK |
| 17 | Persistent audit | `ApplicationService.new_event`, `TrustGates.submit_evidence`; SQLite triggers | - | - | `audit_events` (append-only by trigger), a correlation id on every event | bank | point 17 of the positive test; `test_on_an_upgraded_database_a_rejected_identity_goes_to_a_person` (`test_schema_migration_v6.py`) | yes, part B2 lists 25 events | LOCAL. Triggers are not tamper-proof against database access (F-6) |

**Who can never take a step.** The state machine (`app/domain/workflow.py`) gives the agent no transition at all, and
`OPENED` has exactly one incoming edge, taken only by the depot provider's confirmation. A database trigger enforces
that edge as well. `MANUAL_REVIEW` can be left only by an operator, and only to the interrupted step or to `REJECTED`.

## 2. The case's required capabilities, mapped

The case study (page 2, "Funktionsumfang") names four capabilities.

| Capability, as the case states it | Where it is in the prototype | Evidence | Class |
|---|---|---|---|
| Discoverability and reachability for customer agents (discovery, verifiable provider identity) | A2A Agent Card, signed entity configuration and profile, a synthetic federation | steps 1 to 3; `tests/test_trust_discovery.py` | LOCAL, federation SYNTHETIC. Regulatory status not established |
| Agent-based depot opening including identity proof, authorization and contract conclusion | Delegation, the five steps, the signed package and the provider confirmation | steps 4 to 16 | LOCAL mechanics, all regulated parties SYNTHETIC or MOCK |
| Trustworthy customer communication and brand presence without a website visit | The communication boundary and signed brand profile | step 3 and 14; [../regulated-communication.md](../regulated-communication.md) | LOCAL. A third party's agent is outside the bank's control |
| Reliable detection of when an agent may not conclude autonomously and a person must step in | Deterministic gates, failure paths to `MANUAL_REVIEW`, a request for a person at every step | steps 7, 11, 13, 15; diagram [04](../diagrams/04-decision-escalation-boundaries.mmd) | LOCAL |

## 3. Measured on 2026-11-07

- **Tests:** the full suite has 1452 passed, run once after the audit fixes. `tests/test_onboarding_e2e.py`: 57
  passed.
- **Cross-process demo:** `python -m scripts.demo_trust_journey` exited 0, ended in `OPENED` and printed
  `RESULT: PASS`, including its negative parts.
- **Chrome:** the full-trust browser journey passed 30 of 30 checks.
- **Details:** [../communication-onboarding-evaluation.md](../communication-onboarding-evaluation.md), section 10.

What this does not show: a real eID or KYC check, a qualified signature, a real custodian, a real bank's identity
provider, rate limits, or a production deployment.
