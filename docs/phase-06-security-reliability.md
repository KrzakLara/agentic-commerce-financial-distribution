# Phase 06 - Final security and reliability validation

This phase only validates. No source file, test, ADR or Phase 05 evaluation file was changed, and no
paid model call was made. All data is synthetic and every provider is MOCK: this is a case-study
prototype, not production banking software.

Measured on 2026-11-04 on commit `00dfaf2` (Windows 10, Python 3.12.7, pytest 9.2.0, Ruff 0.17.3,
FastAPI 0.162.0, `anthropic` 0.125.0).

**Result: every check passes and all 55 security mutants were killed. In every test and reproduction,
`OPENED` needed the provider's signed confirmation.** The largest gap is a design limit, not a
regression: customer consent is only as strong as the customer sign-in, and that sign-in is simulated
([F-1](#5-findings-and-remaining-limitations)).

## 1. Validation results

| Check | How | Observed |
|---|---|---|
| Phase 05 correction pushed | `git fetch`, then compare `HEAD` with `origin/main` | Both `00dfaf2`, 0 ahead, 0 behind. GitHub answers 404 to an unauthenticated request for the repository page, which fits a private repository; the visibility setting itself was not read (`gh` is not installed) |
| Test suite | `python -m pytest` | **730 passed**, 0 failed, 0 skipped, 121 s. Repeated with this document added: 730 passed |
| Lint and format | `ruff check .`, `ruff format --check .` | Clean: 134 files, 135 with this document. The rule set includes Ruff's `S` (security) rules |
| Demos | `demo_happy`, `demo_denied`, `demo_manual_review`, each in two environments | 6 of 6: exit 0 and `RESULT: PASS`. Their console output holds 0 agent tokens, customer secrets, key-shaped values, real `.env` values or unmasked one-time challenges |
| Scripted mode without a key | The three demos in a temporary copy: no `.env`, no `ANTHROPIC_*` or `TRUSTEQ_*` variable, `import anthropic` blocked | 3 of 3 pass; mode `scripted`, `enable_llm` false, no key configured. Importing `app.main` loads neither the SDK nor `app.agents.llm`. `eval_llm --mode scripted` in a copy without `.env` (SDK installed) passes 14 of 14, per case identical to the recorded baseline |
| Phase 05 evidence | Digests recomputed, history read per file | Preserved and not rerun: 20 of 20 PASS, `claude-sonnet-5-5` on all 20 recorded final responses, 63 requests, 0.154 USD estimate. `agent_code_sha256`, `dataset_sha256` and `system_prompt_sha256` match the record and the report. The two result files, `app/agents/llm.py` and `scripts/llm_eval_cases.py` last changed in `0e7545b` |
| Secrets in Git | All 7 commits, 260 blobs, 160 paths and all commit messages | Real key value: 0 matches. Sensitive file names: none. Secret-shaped patterns: only the deliberate fake key in `tests/test_llm_agent.py` and `tests/test_llm_evaluation.py`. Stashes: 0 |
| Ignore rules | `git check-ignore` | `.env`, `.env.local`, `.claude/`, `.claude.json`, `*.db`, `*.pem`, `secrets/`, `credentials.json`, `id_rsa` and `.venv/` are ignored. `.env.example` is tracked; its key and secret are blank |
| Mutation testing | `python -m scripts.security_mutations` | **55 of 55 killed** ([section 4](#4-mutation-testing-evidence)) |

**How "offline" was checked.** A temporary audit hook, kept outside the repository, refused every network
connection that was not loopback. In the pytest run it saw 2,548 lookups and connects, all to `127.0.0.1`,
and refused none; the demo, scripted-baseline and reproduction runs were also loopback only. The demos' uvicorn
child resets `PYTHONPATH`, so the hook did not cover that process. The conftest removes provider credentials
from the test environment, and mutant copies contain no `.env`.

**Not measured:** no coverage percentage exists (no coverage tool is configured). No dedicated SAST, DAST or
dependency scan, load test or penetration test was run.

## 2. Security controls and supporting tests

All tests named here passed in the run above. "Cases" counts parametrized cases in the set checked for that
control; the names are examples from that set. Requirement-level traceability is in
[phase-04-traceability.md](phase-04-traceability.md).

| Control | Enforced by | Tests (`tests/`) | Cases | Mutants |
|---|---|---|---:|---|
| Authorization boundaries | `authorization_service.py`, `load_authorized` in `application_service.py`, database triggers | `test_agent_authorization.py`: `test_every_protected_route_needs_a_bearer_token`, `test_each_token_is_refused_outside_its_scope`, `test_a_token_for_one_application_cannot_touch_another_of_the_same_agent`, `test_grants_and_tokens_cannot_be_widened_even_with_raw_sql`; `test_authorization_service.py::test_every_reason_to_refuse_a_known_token`; `test_state_machine.py::test_the_external_agent_can_take_no_edge_at_all` | 55 | M01-M16 |
| Customer consent and channel (see F-1) | `confirm_handoff`, `evaluate_submit`, session and CSRF checks in `routes/customer.py` | `test_handoff.py`: `test_the_agents_credentials_cannot_confirm_a_handoff`, `test_wrong_customer_identity_is_rejected_and_the_handoff_stays_usable`, `test_a_changed_action_digest_blocks_confirmation`; `test_policy.py::test_each_missing_prerequisite_blocks_submission_and_is_named`; `test_customer_session.py::test_cross_session_substitution_is_refused`; `test_disclosures_and_attribution.py::test_changes_after_consent_block_the_submission` | 46 | M17-M24, M29, M30, M48-M51, M53-M55 |
| Idempotency | `create_application`, UNIQUE constraint, state check on submit | `test_application_service.py`: `test_same_key_with_a_different_body_conflicts_and_is_audited`, `test_unique_constraint_stops_a_duplicate_when_the_idempotency_check_raced`, `test_concurrent_identical_creates_yield_exactly_one_application`; `test_onboarding_flow.py::test_duplicate_submit_requests_hand_over_to_the_provider_only_once`; `test_recovery.py::test_concurrent_submits_hand_over_exactly_once` | 16 | None dedicated; M09 and M10 cover the bootstrap's single use |
| Replay protection | Handoff: hashed one-time nonce, compare-and-set consume, expiry. Callback: HMAC, 300 s freshness, unique `confirmation_id` | `test_handoff.py`: `test_a_reused_handoff_is_rejected_and_creates_no_second_consent`, `test_two_simultaneous_confirmations_of_one_handoff_let_exactly_one_win`, `test_an_expired_handoff_is_rejected`; `test_provider_callback.py`: `test_a_duplicate_confirmation_has_no_second_effect`, `test_a_confirmation_id_cannot_be_replayed_against_another_application`, `test_stale_and_future_messages_are_rejected_even_when_correctly_signed` | 15 | None |
| Provider validation | `catalog_fixture.py`, `catalog_policy.py`, callback signature | `test_verified_discovery.py`: `test_a_signed_and_pinned_manifest_is_still_rejected_when_it_contradicts_the_register` (21 cases), `test_unsigned_or_untrusted_signatures_are_rejected`, `test_a_manifest_that_expires_while_the_server_runs_is_refused_at_use`, `test_hostile_text_in_a_signed_manifest_is_served_as_inert_data`; `test_provider_callback.py::test_unauthenticated_confirmations_are_rejected_and_change_nothing` | 58 | M41-M47 |
| Audit integrity (see F-6) | Append-only triggers, audit event in the same transaction, secrets kept out | `test_persistence.py::test_audit_events_are_append_only_at_database_level`; `test_application_service.py::test_failed_audit_write_rolls_back_the_status_change`; `test_agent_authorization.py::test_no_raw_token_is_stored_logged_or_audited`; `test_onboarding_flow.py::test_the_audit_timeline_records_every_trusted_step_in_order`; `test_restart_persistence.py::test_applications_state_and_audit_trail_survive_a_restart` | 15 | None on `audit_events`; M39, M40 and M52 on neighbouring append-only tables |
| Safe failure handling | Generic errors, one transaction per step, bounded resume | `test_exceptions.py::test_unhandled_exception_returns_generic_500_and_leaks_nothing`; `test_recovery.py::test_after_a_crash_and_restart_the_next_submit_resumes_and_completes`; `test_onboarding_flow.py`: `test_an_interrupted_check_pipeline_fails_closed_and_can_be_resumed`, `test_in_production_the_whole_ceremony_fails_closed`; `test_agent_rest_integration.py::test_server_errors_and_outages_become_unavailable_without_details` | 25 | M25-M27, M33, M34 |
| Human review without bypass | `review_service.py`, state machine | `test_review.py` and `test_request_human_security.py` (both files), for example `test_no_operator_decision_can_open_an_account`, `test_only_a_supervisor_may_reject_and_rejection_ends_the_agents_privileges`, `test_there_is_no_operator_http_endpoint`, `test_asking_for_a_person_never_bypasses_the_mandatory_checks` | 56 | M31-M40 |
| LLM tool boundary | `app/agents/llm.py`, `rest_gateway.py` | `test_llm_agent.py`: `test_a_tool_outside_the_allowlist_never_reaches_the_gateway`, `test_a_prompt_injected_model_is_contained_by_the_tool_boundary_and_the_bank`, `test_a_tool_call_in_a_refused_or_truncated_turn_is_never_executed`; `test_llm_evaluation.py::test_the_bank_still_holds_when_a_model_attacks_it_directly` | 40 | M28 only |
| Scripted mode without a key | `build_customer_agent`, `Settings` defaults | `test_llm_agent.py::test_scripted_mode_works_without_the_sdk_and_without_any_key`; `test_app.py::test_application_starts_without_ai_api_keys`; `test_llm_evaluation.py::test_no_key_means_blocked_and_no_call_and_no_result_file` | 7 | None |
| Secrets and repository hygiene | `.gitignore`, `SecretStr`, masked output | `test_repo_hygiene.py` (4); `test_config.py::test_api_key_is_never_shown_in_repr`; `test_llm_evaluation.py::test_recorded_results_in_the_repository_contain_no_secret`; `test_demo_scripts_phase04.py::test_the_demo_passes_cleans_up_and_never_prints_credentials` | 12 | None |

## 3. Verified failure scenarios

Each row is what a passing test asserts, unless it says otherwise.

| Scenario | Observed behaviour | Evidence |
|---|---|---|
| A check provider crashes after the customer confirmed | Generic 500; consent stays durable; the application waits at the interrupted step. The next authorized submit resumes, each check is recorded once, and the journey can still reach `OPENED` | `test_recovery.py::test_after_a_crash_and_restart_the_next_submit_resumes_and_completes` (3), `test_the_resume_reuses_recorded_evidence_and_never_repeats_a_provider_call` |
| The provider is still down on the retry | Submit answers 409 and nothing is submitted; `workflow.resumed` is audited as failure, later as success | `test_onboarding_flow.py::test_an_interrupted_check_pipeline_fails_closed_and_can_be_resumed` |
| A store fails in the middle of a transaction | Rolled back each time. Receipts: handoff not consumed, and the retry succeeds with one consent. Tokens: no application or idempotency record. Audit: state and version unchanged. Revocation: no terminal closure | `test_recovery.py`: `test_a_failure_while_recording_consent_leaves_the_handoff_usable`, `test_a_failure_while_issuing_tokens_leaves_no_application_and_keeps_the_grant`; `test_application_service.py::test_failed_audit_write_rolls_back_the_status_change`; `test_agent_authorization.py::test_a_failed_revocation_rolls_back_the_terminal_transition` |
| The manifest expires while the server runs | Discovery 503, product list empty, confirmation 409, new handoff 422 | `test_verified_discovery.py::test_a_manifest_that_expires_while_the_server_runs_is_refused_at_use` |
| The manifest expires, or terms or partner change, after consent | Submit 409 `prerequisites_not_met`; no depot submission | `test_verified_discovery.py::test_a_manifest_that_expires_after_consent_blocks_submission`, `test_disclosures_and_attribution.py::test_changes_after_consent_block_the_submission` (3) |
| The catalog was tampered with at startup | The application does not start | `test_catalog.py::test_a_tampered_catalog_means_the_application_does_not_start`, `test_verified_discovery.py::test_a_rejected_catalog_still_stops_the_application_from_starting` |
| Restart in the middle of a journey | State and audit trail survive and the flow continues | `test_onboarding_flow.py::test_in_flight_state_survives_a_restart_and_the_flow_can_continue`, `test_restart_persistence.py::test_applications_state_and_audit_trail_survive_a_restart` |
| Concurrent duplicates | 8 identical creates give 1 application and 7 replays; parallel confirmations, submits and provider confirmations have one winner | `test_application_service.py::test_concurrent_identical_creates_yield_exactly_one_application`, `test_handoff.py::test_two_simultaneous_confirmations_of_one_handoff_let_exactly_one_win`, `test_recovery.py::test_concurrent_submits_hand_over_exactly_once`, `test_provider_callback.py::test_simultaneous_identical_confirmations_open_exactly_once` |
| The bank is unreachable or answers garbage (agent's view) | `UNAVAILABLE` or `DENIED` with no detail; a malformed 200 is not trusted | `test_agent_rest_integration.py`: `test_server_errors_and_outages_become_unavailable_without_details`, `test_garbage_success_responses_are_not_trusted`, `test_error_bodies_are_never_forwarded_to_the_agent` |
| LLM API failure, refusal, truncation, malformed response or endless loop | The run ends with a fixed fallback text. A failed, refused, truncated or malformed turn runs no tool; a looping model stops at the round limit | `test_llm_agent.py`: `test_an_api_failure_ends_the_run_safely_and_no_tool_runs` (8), `test_a_tool_call_in_a_refused_or_truncated_turn_is_never_executed` (2), `test_a_malformed_model_response_fails_closed` (3), `test_a_looping_model_is_stopped_by_the_round_limit` |
| Production mode | Forged agent token 401, DEV customer header 401, callback 503 | `test_onboarding_flow.py::test_in_production_the_whole_ceremony_fails_closed` |
| Database from a newer version; interrupted upgrade | Refused; the upgrade resumes on the next start | `test_database.py::test_a_database_from_a_newer_version_is_refused`, `test_an_upgrade_interrupted_after_the_first_step_resumes` in both migration test files |
| Unhandled server error | Generic 500 with no internals. Reproduced over a real socket in this phase (F-5): 4 errors, no token, nonce or key in the INFO log | `test_exceptions.py::test_unhandled_exception_returns_generic_500_and_leaks_nothing` |
| Expired catalog, lost create response, depot outage with a real model | PASS in the recorded live run (FAIL-01 to FAIL-03), not rerun. FAIL-02 ends in a safe dead end: no duplicate, but the customer waits on a DRAFT until a person acts | [phase-05-evaluation.md](phase-05-evaluation.md) |

## 4. Mutation testing evidence

### Recorded before this phase

| Claim | Where | Raw output in the repository | How to read it |
|---|---|---|---|
| 52 of 52 killed, last full run before the Phase 04 finalization | Presentation outline, slide 9 and E24 | None | Historical claim. It cannot be verified or itemized from the repository |
| 23 of 23 killed, targeted run after it | Same | None; the 23 are not named | Same |
| All 55 definitions re-validated after the localization, suite not rerun | Same | None | Shows that the patterns still applied. **Not a kill, and not counted as one** |
| Targeted run for the areas Phase 05 touched | Outline, completion plan (a plan, not a result) | None; `docs/phase-05-evaluation.md` mentions no such run | No evidence that it happened |

The harness prints to the console and saves nothing, so these records cannot say which mutants ran. To answer that
exactly, the unchanged harness was executed once in this phase.

### Executed in this phase

`python -m scripts.security_mutations`, unchanged, on `00dfaf2`, 2026-11-04, 15:47 to 16:25 UTC. For each mutant:
a fresh temporary copy, a source pattern that must match exactly once and still compile, then `pytest -x` without
the 7 demo subprocess tests. Baseline: 723 tests pass on an unmodified copy that imports its own `app` package.

**55 executed, 55 killed, 0 survived, 0 not applied, exit 0.** The working tree stayed clean and no temporary copy
remained. Parametrized test ids are shortened below.

| Mutant | Control deliberately broken | First failing test |
|---|---|---|
| M01 | revoked tokens are accepted | `test_agent_authorization.py::test_a_revoked_token_cannot_be_used_and_revocation_is_final` |
| M02 | tokens of a revoked grant are accepted | `test_authorization_service.py::test_every_reason_to_refuse_a_known_token[grant_revoked]` |
| M03 | the issuer is not validated | `test_agent_authorization.py::test_tokens_for_another_audience_issuer_or_beyond_their_grant_are_rejected[wrong_issuer]` |
| M04 | the audience is not validated | same test `[wrong_audience]` |
| M05 | expired tokens are accepted | `test_agent_authorization.py::test_an_expired_bootstrap_token_cannot_create` |
| M06 | tokens of an expired grant are accepted | `test_authorization_service.py::test_every_reason_to_refuse_a_known_token[grant_expired]` |
| M07 | a token may carry more scopes than its grant | `test_agent_authorization.py::test_tokens_for_another_audience_issuer_or_beyond_their_grant_are_rejected[scope_exceeds_grant]` |
| M08 | the scope check is skipped | `test_agent_authorization.py::test_each_token_is_refused_outside_its_scope` |
| M09 | single use is not enforced (the bootstrap grant is never bound) | `test_agent_authorization.py::test_a_bootstrap_grant_creates_exactly_one_application` |
| M10 | the atomic binding's result is ignored (only the racy pre-check left) | `test_agent_authorization.py::test_the_compare_and_set_stops_a_second_use_even_when_the_pre_check_raced` |
| M11 | the agent may create for a customer its grant does not name | `test_agent_authorization.py::test_the_agent_can_only_act_for_the_customer_its_grant_names` |
| M12 | a token works on another application of the same agent | `test_agent_authorization.py::test_a_token_for_one_application_cannot_touch_another_of_the_same_agent` |
| M13 | ownership is not checked (only the token binding) | `test_agent_authorization.py::test_another_agents_valid_tokens_get_404_and_are_audited` |
| M14 | terminal states do not revoke the grants | `test_agent_authorization.py::test_terminal_closure_revokes_grants_tokens_and_open_handoffs_atomically` |
| M15 | terminal states do not revoke the tokens | `test_agent_authorization.py::test_after_opening_only_the_read_only_status_survives` |
| M16 | terminal states leave customer handoffs usable | `test_agent_authorization.py::test_terminal_closure_revokes_grants_tokens_and_open_handoffs_atomically` |
| M17 | the confirm form's CSRF token is not checked | `test_customer_session.py::test_a_missing_csrf_token_is_refused_and_audited` |
| M18 | the sign-in form's (login) CSRF token is not checked | `test_customer_session.py::test_the_sign_in_form_needs_its_own_pre_session_csrf_token` |
| M19 | cross-site browser requests are not refused | `test_customer_pages.py::test_cross_site_form_posts_are_rejected` |
| M20 | agent credentials are accepted on the customer JSON ceremony | `test_handoff.py::test_the_agents_credentials_cannot_confirm_a_handoff` |
| M21 | customer sessions never expire | `test_customer_session.py::test_sessions_expire_on_the_server` |
| M22 | signing in again does not revoke the previous session | `test_customer_session.py::test_a_csrf_token_of_a_rotated_session_is_dead` |
| M23 | the session cookies are readable by scripts (no HttpOnly) | `test_customer_pages.py::test_the_session_cookie_is_httponly_samesite_strict_scoped_and_expiring` |
| M24 | the session cookie is never Secure, even on HTTPS | `test_customer_pages.py::test_the_session_cookie_is_secure_when_served_over_https` |
| M25 | the resume re-requests checks that already have evidence | `test_recovery.py::test_re_entering_a_check_reuses_its_recorded_result_and_never_calls_again` |
| M26 | the v2->v3 upgrade loses the old revocation trigger | `test_schema_migration.py::test_a_phase02_database_is_upgraded_in_place_without_losing_data` |
| M27 | schema upgrades silently drop old rows (grants, check results) | same test |
| M28 | the agent runtime leaks its token into tool results | `test_agent_rest_integration.py::test_gateway_sends_the_idempotency_key_and_the_right_bearer_token` |
| M29 | an agent token is accepted on request-human | `test_request_human_security.py::test_the_button_needs_the_session_and_its_csrf_token[agent_token]` |
| M30 | any customer may ask for a person on any application | `test_request_human_security.py::test_a_self_asserted_customer_header_is_not_a_session` |
| M31 | an operator may RESUME past a compliance review (skip the re-run) | `test_review.py::test_no_operator_decision_can_open_an_account` |
| M32 | a reviewer may REJECT without a supervisor | `test_review.py::test_only_a_supervisor_may_reject_and_rejection_ends_the_agents_privileges` |
| M33 | RE_EVALUATE does not re-run the check (the old result stands) | `test_review.py::test_evidence_and_re_evaluation_let_the_deterministic_check_decide` |
| M34 | a check that does not pass parks without a review case | `test_request_human_security.py::test_the_acknowledgement_is_identical_whether_or_not_compliance_reviews` |
| M35 | an application may have two active review cases | `test_review.py::test_the_database_keeps_one_active_case_and_review_history_final` |
| M36 | RE_EVALUATE is allowed while requested information is outstanding | `test_review.py::test_evidence_and_re_evaluation_let_the_deterministic_check_decide` |
| M37 | an operator principal is not checked against the directory (role escalation) | `test_review.py::test_operator_identities_are_separate_from_agents_customers_and_tokens` |
| M38 | the customer's acknowledgement reveals an existing review | `test_request_human_security.py::test_repeated_requests_are_idempotent` |
| M39 | a closed review case can be reopened | `test_review.py::test_the_database_keeps_one_active_case_and_review_history_final` |
| M40 | check attempts can be overwritten | same test |
| M41 | the manifest signature is not verified | `test_verified_discovery.py::test_a_pinned_but_unsigned_edit_fails_the_signature_check` |
| M42 | any trusted key may sign for any provider | `test_verified_discovery.py::test_unsigned_or_untrusted_signatures_are_rejected` |
| M43 | the verified domain is not compared with the register | `test_verified_discovery.py::test_a_signed_and_pinned_manifest_is_still_rejected_when_it_contradicts_the_register[domain-mismatch]` |
| M44 | a substituted API endpoint is accepted | same test `[endpoint-substituted]` |
| M45 | a stale (rolled-back) manifest version is accepted | same test `[stale-version]` |
| M46 | manifest validity is only checked at startup, not at use | `test_llm_evaluation.py::test_a_correct_agent_passes_over_the_real_bank_api[FAIL-01]` |
| M47 | inactive channels (ended partnerships) can be used | `test_catalog.py::test_products_endpoint_returns_document_version_and_digest` |
| M48 | the approval does not bind the product terms | `test_disclosures_and_attribution.py::test_the_approved_action_binds_the_terms_and_the_registered_partner` |
| M49 | the approval does not bind the attributed partner | same test |
| M50 | submit does not re-check the approval against the live catalog | `test_disclosures_and_attribution.py::test_changes_after_consent_block_the_submission[fee-changed]` |
| M51 | the disclosure receipt is not bound to terms and action | `test_disclosures_and_attribution.py::test_the_disclosure_receipt_is_bound_to_the_exact_version_and_action` |
| M52 | the channel attribution can be changed after consent | `test_disclosures_and_attribution.py::test_the_attribution_cannot_change_after_consent` |
| M53 | request-human trusts the self-asserted customer header instead of the session | `test_request_human_security.py::test_a_signed_in_customer_with_the_pages_csrf_token_is_accepted` |
| M54 | the request-human CSRF token is not checked | `test_request_human_security.py::test_a_missing_or_foreign_csrf_token_is_refused[missing]` |
| M55 | cross-site browser requests may ask for a person | `test_request_human_security.py::test_a_cross_site_browser_request_is_refused` |

### What these 55 of 55 do not show

- **Only the first failure is reported.** `-x` stops at the first failing test, in file order. A kill shows that the
  suite noticed, not that the dedicated test did. M53 fell on a positive test, because the mutant breaks the
  legitimate session path; M47 fell on a channel-list assertion. Their dedicated refusal tests did not get to run.
- **It is not a mutation score.** The 55 mutants were chosen by hand and say nothing about the rest of the code.
- **Some controls have no mutant.** The harness targets Phase 03 and 04 controls. Nothing mutates the handoff nonce,
  expiry, single use or digest check; the callback's signature, freshness or replay checks; the idempotency conflict;
  the `audit_events` triggers; the state-machine table; `evaluate_submit` itself; or the LLM tool boundary beyond M28.
  **Replay protection and audit integrity are in this group:** their evidence is passing tests, with no proof that
  those tests would notice a broken control.

The outline's slide 9 and rows E23 to E25 (606 tests, 52 of 52, LLM evaluation not measured) predate Phases 05 and 06.
They were left as they are in this phase.

## 5. Findings and remaining limitations

None of these was fixed in this phase and no test was weakened. In what was tested, none lets an agent reach `OPENED`
or exposes a credential. Severity is given for this prototype and for a production system that kept the same code.

| ID | Finding | Status | Prototype | Production |
|---|---|---|---|---|
| F-1 | Values an agent already holds, plus the DEV header, produce a valid customer consent | Known limit, impact newly reproduced | High | Critical |
| F-2 | An unset environment means development, with all DEV identities active | Newly raised | Low | High |
| F-3 | The confirmation link's host comes from the request's `Host` header | New | Low | Medium |
| F-4 | An unauthenticated caller can grow the audit table | New; existing docs describe it more narrowly | Low | Medium |
| F-5 | The callback's signature parser raises on some malformed headers | New | Low | Low |
| F-6 | The audit trail is append-only by trigger, not tamper-evident | Known design limit, newly reproduced | Low | High |
| F-7 | Some "never logged or printed" comments claim more than was tested | Open since Phase 05 | Info | Info |

**F-1: consent depends on the simulated sign-in.** Reproduced on uvicorn over a real socket, development mode:

1. The agent creates an application and requests a handoff. It now holds the handoff id, nonce, digest and customer reference.
2. `POST /v1/customer/handoffs/{id}/confirm` with those values, `X-Dev-Customer-Id` **and its bearer token**: 401. Tests
   and `demo_denied` cover requests of this kind.
3. The same request **without** `Authorization`: 200, state `CONTRACT_PENDING`, one consent receipt, and the audit event
   `handoff.confirmed` names the actor as `customer`.
4. `POST /v1/applications/{id}/submit` with the onboarding token: 202, state `SUBMITTED_TO_DEPOT_PROVIDER`.
5. The provider callback without a valid signature: 401. `OPENED` stays out of reach.

Neither agent implementation can do this: no tool and no gateway path reaches a customer route
(`test_the_scripted_agent_never_sends_customer_credentials_or_touches_customer_routes`,
`test_a_tool_outside_the_allowlist_never_reaches_the_gateway`). It takes an untrusted client that speaks HTTP itself, which
is zone T0b. Production mode refuses the header (`test_in_production_the_whole_ceremony_fails_closed`), but then nobody
can confirm, because no real customer channel exists. The limit is documented in `docs/onboarding-flow.md:108-110` and
`app/api/customer_auth.py:25-27`, while `docs/onboarding-flow.md:82` ("Agent cannot confirm") and the demo show only the
bearer case. When presenting, say: *"An agent cannot confirm with its credentials and has no tool for it. Who the customer
is, is simulated here; a real bank sign-in is what closes this."* Do not say that a rogue client cannot self-approve in
this prototype.

**F-2: unsafe default for a deployment.** `Settings(_env_file=None)` gives `environment=development` with customer sign-in,
agent bootstrap and operator console active; `environment=production` turns all three off. The default is pinned by
`test_config.py::test_defaults_are_safe_and_need_no_secrets`. A deployment that forgets `TRUSTEQ_ENVIRONMENT=production`
exposes F-1.

**F-3: host taken from the request.** `POST /v1/applications/{id}/handoff` with `Host: phish.example` returned 201 and a
`confirmation_url` starting `http://phish.example/` (`app/api/routes/applications.py:168-169`; `app/main.py` has no trusted-host
check). An agent gains nothing new, because it already holds the nonce, and the link still requires sign-in. The weakness is
that a bank response can carry a host chosen by the caller. A model cannot set this header.

**F-4: unauthenticated audit growth.** 25 `POST /v1/customer/applications/<random id>/request-human` requests with no
credential returned 401 each and added 25 audit rows. There is no rate limit. `docs/onboarding-flow.md:189` describes a
flood "with a once-valid token", which is narrower than what was observed.

**F-5: malformed callback headers.** Timestamp byte `\xb2`, signature byte `\xe9`, and timestamps of 400 and 5,000 digits made
`verify()` raise `ValueError`, `TypeError`, `OverflowError` and `ValueError`, although its docstring promises `False`
(`app/integrations/provider_callback.py:32`). The caller got a generic 500 instead of 401. It fails closed: state unchanged, no
audit row, no signature bypass. `test_any_tampering_or_malformed_input_fails_verification` covers ASCII input only. The endpoint
is active only with a mock secret configured, which production refuses.

**F-6: audit trail.** `UPDATE audit_events` is refused while the trigger exists. After `DROP TRIGGER trg_audit_events_no_update`,
26 denials on a throwaway database were rewritten as successes and nothing detects it: there is no hash chain. Separately, a
forged well-formed token gets 401 with no audit row (`app/services/authorization_service.py:329-330`), so guessing shows only
in the request log. The server's INFO log from these reproductions held no token, nonce or key (53 lines).

**F-7: claims wider than the tests.** `.env.example:33`, `app/agents/llm.py:491` and `scripts/demo_llm.py:11-12` say that the key
is never logged or printed. No test captures the output of a model run or the SDK's logging; section 2 of the Phase 05 report
states the real scope. Left unchanged: editing `llm.py` would change the recorded `agent_code_sha256`.

**Other remaining limitations**

- Agent tokens are bearer tokens: no sender constraint, no client authentication, no refresh flow.
- Manifests use HMAC with a shared DEV key in the repository, and the callback uses a shared mock secret.
- The customer's one-time link passes through the model provider, and the model's answer text is not enforced at runtime.
- The product name is provider free text that reaches the model; it stays inert towards the bank.
- The live evaluation took one sample per case, on cases written by the prompt's author. The model made no bypass
  attempt, so the run barely exercised the bank's controls; the attack evidence is offline.
- `test_llm_evaluation.py::test_the_recorded_live_run_still_describes_the_dataset_and_the_prompt` checks the dataset and
  prompt digests, not `agent_code_sha256`. This phase compared that digest by hand.
- After a crash, checks resume only on the agent's next submit, so an application whose agent never returns stays parked.
- The depot call runs inside the database transaction, with no outbox (`app/services/onboarding_service.py:915`).
- Size limits apply only after the whole body was read (`app/api/routes/customer.py:283`, `app/api/routes/internal.py:52`).

## 6. Production-readiness gaps

| Area | Prototype today | Required for a production banking system |
|---|---|---|
| Customer identity | Simulated sign-in, DEV header | Bank strong customer authentication (eID, EUDI wallet or online banking) in a bank-controlled channel; DEV routes absent from the production build |
| Agent identity | Opaque tokens from a DEV bootstrap | Bank IAM, OAuth 2.0 with a FAPI 2.0 profile where appropriate, registered clients, sender-constrained tokens |
| Provider trust | Fixture catalog, HMAC with a DEV key, shared callback secret | Asymmetric signatures (JWS or eIDAS seal), mutual TLS, key rotation and revocation, allow-listed network paths |
| Regulated functions | MOCK KYC, compliance, signing and depot; no appropriateness questionnaire | Contracted providers, legal and compliance review (GwG, WpHG, BaFin expectations), DORA controls. No compliance is claimed here |
| Secrets | `.env`, `SecretStr` | Vault or KMS/HSM, rotation, no shared development keys |
| API edge | None | TLS and HSTS, trusted hosts and a fixed public origin, rate and body-size limits, abuse detection |
| Configuration | Defaults to development | Fail closed unless explicitly configured; a separate production build |
| Data | Single-writer SQLite, hand-written upgrades, synthetic data | Managed database with high availability, tested backup and restore, encryption at rest, a migration tool, retention and data-protection rules |
| Audit and monitoring | Trigger-protected table, JSON logs on stdout | Tamper-evident storage, SIEM, alerting, tested log redaction, attribution of failed authentication |
| Reliability | Bounded synchronous resume, provider call inside the transaction | Outbox or queue, retries with backoff, timeouts, circuit breakers, load tests, disaster recovery |
| Human review | DEV console, synthetic operators | Case management, SSO, RBAC, four-eyes approval, SLA tracking |
| LLM | One pinned model, 20 fixed cases, one sample each | One-time link kept out of model traffic, status shown from the server, constrained catalog text, repeated evaluation with an independent red team, monitoring |
| Assurance | Own tests, 55 hand-picked mutants | External penetration test and threat-model review, SAST, DAST and dependency scans in CI, coverage, mutants for the gaps in section 4 |

## 7. Risk matrix

Likelihood and impact are judgement calls for a production system that kept this code, not measurements. **P0:** before any
real customer data. **P1:** before go-live. **P2:** hardening. **P3:** housekeeping.

| # | Risk | Likelihood | Impact | Priority | Effect on the prototype today |
|---:|---|---|---|---|---|
| 1 | Forged consent, because customer identity is simulated (F-1) | High | Critical | **P0** | Accepted; state it when presenting |
| 2 | DEV identities active through a missing setting (F-2) | Medium | Critical | **P0** | None; the demos need development mode |
| 3 | Mock KYC, compliance, signing or depot treated as real | Low | Critical | **P0** | Labelled MOCK |
| 4 | Stolen bearer token works until expiry or revocation | Medium | High | **P0** | Accepted, documented |
| 5 | Shared DEV keys for manifests and callback | Medium | High | **P0** | Synthetic data only |
| 6 | Flooding: no rate or size limits, audit growth (F-4) | High | Medium | **P1** | Local demo only |
| 7 | Single-node SQLite, no high availability, no outbox | High | High | **P1** | Sufficient for the case study |
| 8 | Audit trail rewritable with database access (F-6) | Low | High | **P1** | Accepted |
| 9 | Confirmation link with a caller-chosen host (F-3) | Medium | Medium | **P1** | The agent already holds the nonce |
| 10 | One-time link and catalog text cross the model boundary; answers not enforced | Medium | Medium | **P1** | Synthetic data only |
| 11 | Controls without mutants could regress unnoticed (section 4) | Medium | Medium | **P2** | Tests pass; the gap is stated |
| 12 | Callback parser answers 500 on malformed headers (F-5) | Medium | Low | **P3** | Fails closed |
| 13 | Comments claim more than was tested (F-7) | Low | Low | **P3** | Correct wording is in the Phase 05 report |

## 8. Prototype versus production

**Real, enforced and tested in this prototype:**

- the persistent state machine, with `OPENED` reachable only through a signed provider callback;
- the deterministic policy gate over stored evidence;
- scoped, expiring, revocable agent tokens stored as hashes, with resource binding;
- the one-time, expiring handoff bound to the exact action;
- sessions and CSRF protection around the sign-in;
- signed, pinned, register-checked manifests;
- idempotency, compare-and-set updates and append-only records;
- audited denials, and an LLM behind the same tools that fails closed.

**Simulated:** customer sign-in and the DEV header; KYC, compliance, signing and the depot provider; providers, partners, the
register and their DEV keys; operator identities; all data.

**Absent:** everything in the right-hand column of section 6.

This is a well-tested prototype of a control design. 730 tests, 55 of 55 mutants and 20 of 20 live cases describe this
prototype on this code. They do not certify security, and no production readiness or regulatory compliance is claimed.

## 9. Reproduce

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
.\.venv\Scripts\python.exe -m scripts.demo_happy           # also demo_denied, demo_manual_review
.\.venv\Scripts\python.exe -m scripts.security_mutations   # about 38 minutes here; prints, saves nothing
.\.venv\Scripts\python.exe -m scripts.eval_llm --mode scripted --output "$env:TEMP\scripted.json"   # offline
```

The network guard, the history scan and the reproduction script for section 5 were temporary and are not in the repository;
each finding lists its requests. The history scan compared the `.env` values with every object from
`git rev-list --objects --all` and with `git log --all --format=%B`, in memory, without printing them.
