# Integration evidence

What was measured for the trust integration, on which code, and what it does not show. Measured on 2026-11-06 on the uncommitted branch
`feature/agentic-trust-integration`, on top of commit `20b4a30`. Windows 10, Python 3.12.7, Chrome 156.0.8078.80, `joserfc` 1.8.0, Authlib 1.9.5.

**No paid model and no chargeable API was called. Nothing was committed or pushed.**

## 1. Results

| Check | Result | Timing relative to the final code |
|---|---|---|
| Full test suite | **1286 passed**, 0 failed, 324 s | Final code, before the commit |
| Earlier tests | No test function or test file deleted against `20b4a30` | - |
| Ruff lint and format | Clean | After the last edit |
| `demo_integrated`, `demo_happy`, `demo_denied`, `demo_manual_review` | 4 of 4: exit 0, `RESULT: PASS` | Final code, before the commit |
| `demo_trust_journey`: separate processes, 4 negative sections | Exit 0, `RESULT: PASS` | Final code, before the commit |
| Scripted evaluation | 14 of 14 | Before the last edit |
| Existing Chrome regression (`drive6.cjs`) | 34 of 34, exit 0 | Before the last edit |
| New Chrome journey (`drive_trust.cjs`) | 30 of 30, exit 0 | After the last edit |
| Official `a2a-sdk` 1.3.0, in its own environment | Parsed the card with unknown fields refused; got a completed task over JSON-RPC | After the last edit |
| New focused mutants M72 to M93 | 19 of 22 killed; the other 3 killed after focused tests were added (section 2) | After the last edit |
| Tests added for those 3 | 12 passed, included in the 1286 | Final code, before the commit |
| Earlier focused mutants M20, M53, M59 to M71 | 15 of 15 killed | After the last edit |
| Scan of outgoing changes | See section 4 | After the last edit |

**The last edit** split two long string literals to satisfy Ruff, with no change in behaviour. The scripted evaluation and the existing Chrome regression were not repeated after it. The full suite, the
demos, the Chrome journey and the targeted mutant baselines did run on that code.

The drivers `drive6.cjs` and `drive_trust.cjs` and the SDK script are local tools outside this repository. The automated suite contains no real browser.

## 2. Mutation results

These are hand-picked mutants, not a mutation score. **All 71 earlier mutants were not run on this code.** 55 of 55 remains the Phase 06 result on `00dfaf2`, and it is not
added to anything here.

All runs used targeted test files, not the whole suite. An earlier run of M72 to M93 was stopped because the code changed while it ran, and its results were discarded.

| Run | Test files | Result |
|---|---|---|
| M72 to M93 | The five trust test files | 19 of 22 killed. M74, M80 and M87 survived |
| M74, M80, M87 again | `tests/test_trust_mutation_closure.py`, 12 tests added for this | **3 of 3 killed** |

No application code changed for this. All three survivors were gaps in the tests, not defects in the code.

| Mutant | Control removed | Classification | Direct evidence |
|---|---|---|---|
| M74 | The check that a delegation is live | Another independent layer held it, and a direct test was missing | Delegation marked revoked with live tokens: refused, `delegation_revoked`. Token revoked with a live delegation: refused, `token_revoked` |
| M80 | The signature check in `signature_gate` | Another independent layer held it, and a direct test was missing | Policy layer off: refused, `contract_signature`. Gate off: refused, `policy:SIGNATURE-LEVEL`. **Both off, in a one-off check outside the repository: the submission went through,** so these two layers are the only ones |
| M87 | The browser binding of a login | Missing regression test | A browser with its own valid flow and another browser's state: refused, reason `browser_binding_invalid`, no session, and the real browser's login still works |

## 3. What each area's evidence covers

| Area | Positive | Negative | Not shown |
|---|---|---|---|
| 1 Discovery | Separate process discovers through the configured list; SDK reads the card | Malformed, version, origin, tampered, SSRF rules | Public reachability |
| 2 Provider trust | Chain, rotation | Forged, unknown anchor, revoked, expired, tampered, self-signed | Federation conformance, any licence |
| 3 Identity | Authlib provider through real processes and Chrome | Token claims, PKCE, state, browser binding, mix-up, discovery, open redirect, switching | Real provider or proofing |
| 4 Delegation | Full workflow | Expired, revoked, escalation, replay, other key, downgrade | FAPI conformance |
| 5 Signing | Path to `OPENED`, hash traced | Hash, signatory, level, replay, cancel, expiry, outage, tampered store | Any legal signature |
| 6 Communication | Supported text released with evidence | Adversarial cases with a fake model | A real model; meaning of prose |
| 7 Brand | Signed profile read without any page | Changed content, expired manifest | A real brand or domain proof |
| 8 Governance | Direct, partner, relationship, referral | Forged, outdated, replay, conflict | Real contracts, permissibility |
| 9 Policy | Decisions recorded with version | Missing result refuses, model as decider refused | Legal compliance |

**A finding from the real browser:** the first Chrome run of the journey stopped at the handover to a provider. The pages' CSP (`form-action 'self'`) blocks a form post that redirects to another origin, and
`httpx`-based tests cannot see that. The bank now shows a link, and the CSP is unchanged.

## 4. Scan of outgoing changes

On 79 outgoing files, `final_scan.py` compared the one real `.env` value of sufficient length: 0 matches. By pattern: no Anthropic key, AWS key, GitHub token, private key block, private JWK member, live agent token,
one-time link, Gmail address, German IBAN or the owner's surname. No `.env`, database, cache, `.claude/`, PDF or mail file. No correspondence is in the repository. Only one test credential appears, labelled
`test-only-client-value`. Runbook and README commands contain the local Windows user folder, as they did before this work.

The Phase 05 result files, `app/agents/llm.py` and `scripts/llm_eval_cases.py` show no difference against `20b4a30`.

A pattern scan finds what it knows, and does not prove that nothing else exists.

## 5. Reproduce

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m scripts.demo_trust_journey
.\.venv\Scripts\python.exe -m scripts.security_mutations --only M72,M73,M74,M75,M76,M77,M78,M79,M80,M81,M82,M83,M84,M85,M86,M87,M88,M89,M90,M91,M92,M93 --tests tests/test_trust_discovery.py,tests/test_trust_identity_delegation.py,tests/test_trust_contract_signing.py,tests/test_trust_communication.py,tests/test_trust_governance_policy.py,tests/test_trust_mutation_closure.py
```

A fresh install was not verified, and no lock file is tracked.

## 6. Not measured

Clicks by a human hand, a real model, an external penetration test, load, SAST, DAST, dependency scanning, the rehearsal of this journey with a stopwatch, any browser but Chrome.
