# Phase 04 - traceability, scope and 2030 target

Phase 04 adds **verified provider discovery, trusted product information and disclosures,
partner-channel attribution and human review**, and makes the three demo outcomes reproducible:
**HAPPY** (`scripts/demo_happy.py`), **DENIED** (`scripts/demo_denied.py`) and **MANUAL REVIEW**
(`scripts/demo_manual_review.py`).

> *LLM proposes. Policy decides. Customer authorizes. Bank executes.* In Phase 04 a human
> reviewer joins - and is bound by the same rule: a person can re-run or end a step, never
> approve a check, sign for the customer or open an account.

Everything is synthetic: fictional providers, partners, registry references (`MOCKLEI...`),
`.example` domains, a DEV signing key, MOCK checks and simulated customer and operator
identities. No real institution, no BaFin/registry lookup, no regulatory compliance is claimed.

## Traceability

Tests are in `tests/`; `M..` are mutants in `scripts/security_mutations.py`.

| # | Requirement | Implementation | API / component | Tests | Demo |
|---|---|---|---|---|---|
| 1 | The customer can ask for a human - only on the authenticated (simulated) customer session with CSRF protection, never with a self-asserted id or an agent token | `_human_request_customer` gate (agent credential, cross-site, active session, CSRF bound to session + application), `ReviewService.request_human`, `record_request_denial` | `POST /v1/customer/applications/{id}/request-human` (cookie `trusteq_customer_api` + `X-CSRF-Token`) | `test_request_human_security.py` (missing/expired/revoked session, self-asserted header, wrong customer, agent token, missing/foreign CSRF, cross-site, cookie flags), `test_review.py`; M29, M30, M53-M55, M21-M24 | manual_review 3-4, denied 2-3 |
| 1b | A visible "Mit einer Person sprechen" button on the bank-controlled pages; neutral acknowledgement; creates or reuses the case; the application state and checks are preserved | `pages.human_request_form` / `human_request_ack_page` on the review page and the confirmation page | `POST /customer/applications/{id}/request-human` (page session + form CSRF field, same gate) | `test_the_review_page_shows_the_button...`, `test_pressing_the_button_opens_a_case...`, `test_the_button_needs_the_session_and_its_csrf_token` (5), `test_repeated_requests_are_idempotent`, `test_the_acknowledgement_is_identical_whether_or_not_compliance_reviews`, `test_asking_for_a_person_never_bypasses_the_mandatory_checks` | manual_review 2, 4 |
| 2 | Fixed reason categories, no AML/compliance information, safe acknowledgement, no duplicate active cases | `HumanRequestCategory`, `HumanRequestAck`, join-existing-case path, partial unique index | same route | `test_only_a_fixed_category_is_accepted`, `test_a_second_request_joins...`, `test_the_acknowledgement_never_reveals_an_existing_compliance_case`, `test_the_database_keeps_one_active_case...`; M35, M38 | manual_review 4 |
| 3 | Synthetic REVIEW_REQUIRED: stop, persist MANUAL_REVIEW, open a case, keep the interrupted step, audit, generic agent status, wait for a human | `MockComplianceProvider` (`synthetic-review-` scenario), `OnboardingService._run_check` / `_open_check_review`, existing `resume_status` | state machine (no second machine) | `test_the_synthetic_review_scenario_parks_the_application_with_a_durable_case`, `test_ordinary_customers_are_not_affected...`; M34 | manual_review 2-3 |
| 4 | A human cannot bypass KYC/AML/consent/signing/provider; no manual `OPENED` | `allowed_decisions` (RE_EVALUATE / RESUME / REJECT / ACKNOWLEDGE only), `ReviewService.decide`, transition table | dev console | `test_no_operator_decision_can_open_an_account`, `test_only_a_supervisor_may_reject...`, `test_a_decided_case_cannot_be_decided_again`, `test_resume_never_repeats_or_skips_a_recorded_check`; M31, M32, M37 | manual_review 5-7 |
| 5 | Permitted evidence and re-evaluation under deterministic policy | `ReviewEvidence` (category + synthetic `SYN-...` reference), append-only check attempts, `_authorized_re_evaluation` | `request-info`, `add-evidence`, `decide RE_EVALUATE` | `test_evidence_and_re_evaluation_let_the_deterministic_check_decide`, `test_re_evaluation_without_the_resolving_evidence_parks_again`, `test_evidence_of_another_kind...`, `test_evidence_references_must_be_synthetic_ids`; M33, M36, M40 | manual_review 5 |
| 6 | Dev-only operator workflow; operator identity distinct from agent, customer, provider; no agent tokens; no dashboard | `scripts/dev_operator.py`, `DEV_OPERATORS`, `OperatorPrincipal`, `dev_operator_cli_active` | trusted CLI, **no HTTP route** | `test_the_operator_console_...` (3), `test_operator_identities_are_separate...`, `test_there_is_no_operator_http_endpoint`; M37 | manual_review 5 |
| 7 | Versioned provider manifest: id, official name, fictional LEI, verified domain, approved endpoint, capabilities, channels, version, issued/expires, signature, trust anchor | `catalog_fixture`: `PROVIDER_REGISTER`, `PARTNER_REGISTER`, `TRUST_ANCHORS`, `sign_manifest` | `GET /v1/providers/{id}/manifest` (+ verification fields) | `test_verified_discovery.py`: `test_the_default_manifest_passes_every_check`, `test_the_manifest_endpoint_shows_what_was_verified` | denied 4 |
| 8 | Reject unknown/fake, tampered, expired manifests, endpoint substitution, provider/domain mismatch, unsupported capabilities, unapproved channels, stale versions | `FixtureProviderDiscovery._load_provider` (register, pin, documents, signature, identity/domain/endpoint, scope, freshness) | startup fails closed | `test_a_signed_and_pinned_manifest_is_still_rejected...` (20 cases), `test_a_pinned_but_unsigned_edit...`, `test_unsigned_or_untrusted_signatures...`, `test_an_expired_trust_anchor...`, `test_a_rejected_catalog_still_stops...`, `test_catalog.py`; M41-M45 | denied 4 |
| 9 | An LEI alone is not proof of endpoint ownership | only the bank's register binds provider -> verified domain -> approved endpoint; no lookup by LEI exists | - | `test_a_registry_reference_alone_is_not_proof_of_endpoint_ownership` | denied 4 |
| 10 | Validity enforced while running, not only at startup | `catalog_policy.resolve_offer` / `provider_is_current` | create, handoff, confirm, submit; discovery 503 | `test_a_manifest_that_expires_while_the_server_runs...`, `test_a_manifest_that_expires_after_consent...`; M46 | - |
| 11 | Prompt-injection metadata is DATA: cannot change state, authorization, provider identity or tools | escaped rendering, allow-listed gateway fields, fixed `AgentToolName`, scopes from grants, text inside the terms digest | review page, tool gateway | `test_hostile_text_in_a_signed_manifest_is_served_as_inert_data`, `test_hostile_text_is_escaped_on_the_customer_page`, `test_the_hostile_text_is_part_of_what_the_customer_approves` | denied 2, 4 |
| 12 | Product information: id, provider, version, fees, conditions, risk, sources/documents, versions, effective/published dates, integrity; no advice / MiFID / WpHG claims | `Product`, `Fee`, `SourceReference`, `terms_digest`, `ProductView.notice` | `GET /v1/products` | `test_disclosures_and_attribution.py`: `test_product_information_is_complete...`, `test_the_terms_digest_covers_every_term` | - |
| 13 | Disclosure evidence bound to application, customer, provider, product, document version, action digest, confirmation time | `DisclosureReceipt` (+5 columns), written in `confirm_handoff` | customer confirmation | `test_the_disclosure_receipt_is_bound_to_the_exact_version_and_action`; M51 | happy, manual_review |
| 14 | Changed terms or disclosure after the handoff invalidate the old approval | `ApprovedAction.terms_digest` / `partner_id`; confirm re-derives the action; submit checks `_still_current` | confirm 409, submit 409 | `test_changed_terms_after_the_handoff...`, `test_changes_after_consent_block_the_submission` (3), `test_handoff.py::test_confirmation_is_refused_when_the_catalog_changed...`; M48-M50 | - |
| 15 | Channel registry: direct, partner-bank, customer-confirmed; reject unknown, fabricated, inactive, mismatched channels | `Channel` (kind, partner, active, products), `PARTNER_REGISTER`, `resolve_offer` | create / handoff 422 | `test_inactive_unknown_or_fabricated_channels_are_refused`, `test_a_channel_that_does_not_serve_the_product_is_refused`, `test_an_ended_partnership_stops_a_pending_approval`; M47 | denied 1 |
| 16 | Persist verified attribution; the agent cannot pick a commission recipient; no change after consent; fund/distributor vs partner bank vs depot provider distinct | `ChannelAttribution` (append-only, 1 per application), identity trigger, `ProviderRole.PARTNER_BANK` | confirm | `test_a_partner_attribution_is_recorded...`, `test_the_agent_cannot_choose_a_commission_recipient`, `test_the_attribution_cannot_change_after_consent`, fund/depot-as-partner cases in #8; M49, M52 | denied 1, manual_review (partner-1) |
| 17 | Three demos: offline, no LLM key, deterministic, non-zero exit on failure, no secrets printed | `scripts/demo_happy.py` (unchanged), `demo_denied.py`, `demo_manual_review.py` | - | `test_demo_script.py`, `test_demo_scripts_phase04.py` | all |
| 18 | Existing data survives the schema change | `init_database` v3 -> v4 (rebuild `check_results`, add receipt columns, new tables and triggers) | - | `test_schema_migration_v4.py` (5), `test_schema_migration.py` | - |

"Customer-selected" channel: the agent can only *propose* a registered channel id. The bank
derives the partner from its registry, shows it on the bank-controlled confirmation page
("Distribution partner ... this account opening is attributed to this registered partner bank")
and binds it into the action digest the customer confirms. A dedicated "pick your Hausbank" UI is
not built.

## Implemented (2026 prototype) vs mocked vs 2030 target

| Area | Implemented and tested in the 2026 prototype | Mocked / simulated | 2030 target (not built) |
|---|---|---|---|
| Provider trust | bank register as trust root; pinned digest; signed, versioned, expiring manifests; identity/domain/endpoint/capability/channel binding; rollback protection; validity re-checked at use; fail closed | the provider, its key and the register are synthetic; HMAC with a shared DEV key instead of asymmetric signatures; no network fetch | provider-signed manifests (JWS / eIDAS qualified seal) fetched over mTLS from the verified domain; key rotation and revocation; registry data (LEI, BaFin register) as *inputs* to onboarding a provider, never as proof |
| Product information | versioned terms, fees, conditions, risk class and summary, source documents with digests, terms digest, non-personalized notice | all content synthetic | provider product-information feeds with legal sign-off, KID/PRIIPs documents, multi-language |
| Disclosure and consent | receipts bound to the exact document version, product terms and action digest; stale approvals refused at confirm and submit | simulated customer identity | wallet-based (EUDI) or online-banking approval with qualified electronic signatures; a re-consent flow after changed terms |
| Attribution | registry-based, customer-confirmed, append-only; the agent cannot name a recipient; roles kept distinct | synthetic partners; no commission is calculated | contractual partner registry, commission settlement and reconciliation, partner self-service |
| Customer intervention | "Mit einer Person sprechen" on the bank pages and the customer JSON API, both on the server-side session with session- and application-bound CSRF tokens; neutral acknowledgement; one active case | the sign-in that creates the session is a DEV simulation (anyone can type a customer id) | strong customer authentication (online banking / EUDI wallet), a customer case view without internal reasons, phone/chat channels, accessibility |
| Human review | durable cases, generic customer acknowledgement, fixed operator decisions, re-evaluation as a new deterministic attempt, supervisor-only rejection, full audit trail | synthetic operator identities and evidence references, MOCK compliance scenario switch | case-management system with SSO, RBAC and four-eyes approval, SLA tracking, an evidence store for real documents, customer case status |
| Agent | scripted, REST only, no key | - | optional LLM behind the same tools and policy (Phase 05), MCP/A2A adapters |

## Deliberately deferred

* `GET /v1/customer/cases/{id}`: the customer's acknowledgement is deliberately generic, so it
  never reveals whether a compliance review exists (no tipping-off). Case status is an operator
  view; the agent only ever sees `ACTION_REQUIRED`.
* A re-consent flow after a post-consent change of terms: submission is refused (fail closed)
  and the application cannot proceed; a new application is needed.
* Cancellation endpoint, four-eyes approval beyond supervisor-only rejection, commission
  calculation, real signatures and network-fetched manifests (see the 2030 column).

## P0 core freeze

After the Phase 04 finalization (all checks executed and passed - see the final report) the P0
core feature set is **frozen**. Later work (Phase 05 LLM agent, evaluation, presentation) must
build on it, not change it.

| Scenario | What the frozen core demonstrates | Evidence |
|---|---|---|
| HAPPY | verified fictional provider; approved product; scoped agent authorization; trusted customer confirmation on the bank page; MOCK checks; trusted depot-provider confirmation; `OPENED` | `scripts/demo_happy.py`, `test_demo_script.py`, `test_onboarding_flow.py` |
| DENIED | fake or manipulated providers rejected; unauthorized agent operations rejected (401/403/404/405/409/422); no unauthorized state transition - the application is NOT `REJECTED` and the real customer can continue | `scripts/demo_denied.py`, `test_verified_discovery.py`, `test_agent_authorization.py`, `test_request_human_security.py` |
| MANUAL REVIEW | the customer requests a human ("Mit einer Person sprechen", session + CSRF); MOCK compliance `REVIEW_REQUIRED`; a durable review case; an authorized simulated operator re-evaluates with evidence; mandatory checks cannot be bypassed | `scripts/demo_manual_review.py`, `test_review.py`, `test_request_human_security.py` |
