# TRUSTEQ - evidence summary

**This page records the state of commit `20b4a30`.** Later results, including the trust integration, are in
[integration-evidence.md](../integration-evidence.md).

What was measured, on which code, and what it does not show. Use this page for slide numbers and for questions.

Everything is a prototype with synthetic data. It opens no real account, creates no legally valid contract and claims no
regulatory compliance or certification.

**Code state.** "Current" results were measured on 2026-11-05 and 2026-11-06, on the final-integration code on top of commit
`5a10024`. After those runs only documentation changed. Older results are labelled with the code they measured and do not
validate the later changes.

**Environment.** Windows 10, Python 3.12.7, Chrome 156.0.8078.80. Everything ran locally on `127.0.0.1`.

## 1. Current results

| Check | Date | Result |
|---|---|---|
| Full test suite (`python -m pytest`) | 2026-11-05 | **991 passed**, 0 failed, 163 s |
| Focused: identity, F-1 and sessions | 2026-11-05 | 71 passed |
| Focused: customer information, tax and appropriateness | 2026-11-05 | 83 passed |
| Focused: trusted claims | 2026-11-05 | 47 passed |
| Focused: provider integration and contract | 2026-11-05 | 55 passed |
| Ruff lint and format | 2026-11-05, 2026-11-06 | Clean |
| Offline demos | 2026-11-05 | 4 of 4: exit 0, `RESULT: PASS` |
| `demo_denied`, run again to check the runbook text | 2026-11-06 | Exit 0, `RESULT: PASS`, 7 audited refusals |
| Scripted evaluation (`eval_llm --mode scripted`) | 2026-11-05 | 14 of 14, no model and no key |
| Focused security mutants | 2026-11-05 | 15 of 15 killed (section 5) |
| Runbook Blocks 1 to 6, run from the file | 2026-11-05 | All six as stated. A script played the browser step of Block 4 |
| Integrated journey in Chrome | 2026-11-06 | 34 of 34 checks, exit 0 (section 2) |
| Scan of all outgoing changes | 2026-11-06 | Clean (section 8) |

The four demos are `demo_integrated`, `demo_happy`, `demo_denied` and `demo_manual_review`. They need no internet and no API key.
The last three are the original P0 scenarios: they switch the tax and assessment step off and print a note saying so. So does the
scripted evaluation. New tests and `demo_integrated` run with the step on.

## 2. The integrated journey

`demo_integrated` and the Chrome run both cover this journey. The demo posts what the real pages offer; the Chrome run presses
them in a browser.

| Step | Status | Observed |
|---|---|---|
| Agent discovers the provider and reads approved facts | Real, with a synthetic signed catalog | `demo_integrated` prints the approved custody fee with its source |
| Customer signs in | **Simulated** | The labelled DEV form accepts any synthetic ID |
| Customer reviews product, fees, conditions, risks and disclosure | Real page, synthetic data | Chrome: all shown. No Confirm before the information exists |
| Tax information | Real validation, synthetic data | Chrome: a real-looking ID is refused and not echoed, empty fields show Missing, and the saved value is not on the page or in the URL |
| Knowledge and experience | Real rules, synthetic data | Chrome: `ASSESSMENT_OK`, `WARNING_REQUIRED` and `INSUFFICIENT_INFORMATION`. The last two need an acknowledgement, and none stops the account |
| Customer authorizes | Real controls, on a simulated identity | Chrome: session, CSRF and one-time challenge. A tampered answer, a tampered CSRF token and a post from another origin are refused |
| Human assistance | Real request, simulated operator | Chrome: pressing **Speak to a person** pauses the application, and forms and Confirm disappear. The operator's list then shows the open case. The request page promises no staff contact |
| Mock KYC, compliance, contract | **Simulated** | Fixed results, `mock=true` |
| Agent submits through the versioned adapter | Real adapter, mock provider | Chrome run: `202`, generic state `IN_PROGRESS` |
| Provider completion | Real signature check, simulated signer | Chrome run: signed callback `200`, then `OPENED` |

**How the Chrome run works.** An automated, visible Chrome with a temporary profile drove the real pages. The driver is a local
tool and is not in this repository, so the automated suite still has no real browser. The only host requested was
`127.0.0.1:8000`, with no unexpected console or page errors. No tax ID, token or `token=` link appeared in that run's server log.
That is one local log at the default level, not a claim about every log or proxy.

## 3. Provider v1/v2 compatibility

[The compatibility matrix](../evidence/provider-compatibility-matrix.md) has nine scenarios, generated from the real journey and
compared with a fresh run by a test.

| Situation | Result |
|---|---|
| v1 only, or v2 and v1 | Completes, ending in `OPENED` |
| Provider retired v2 and explicitly permits v1 | Falls back, recorded as `fallback_from`, ending in `OPENED` |
| Provider retired v2 without permission, or offers only v3 | `502`, stays `CONTRACT_PENDING` |
| Malformed answer, outage or timeout | `502`, stays `CONTRACT_PENDING`, never retried in another version |
| Answer claims the account is open | Refused as a contract violation, stays `CONTRACT_PENDING` |

After every failed submission, a correctly signed callback is refused with `403`.

**Limits:** the provider is an in-process mock, so a timeout is a raised error. A real timeout leaves the outcome unknown, and
reconciliation is not implemented. The signed manifest lists no API versions. No real provider is involved.

## 4. Approved product claims

**What exists:** bank-approved facts with value, source, version and digest, at `approved-claims` and in the `get_official_product`
tool result. `POST /v1/claims/verify` checks a structured claim. Hostile catalog text is left out, and narrative text stays out of
model traffic.

**Limits:**

- The bank controls what it publishes and signs, not what a customer's agent then writes. This is not hallucination prevention.
- The LLM agent's free-form text is not checked against the claims.
- The wording filter knows listed phrases only.
- The effect of the claims on a real model has not been measured.
- The fixture has one product, so fee comparison is shown with a second product in tests only.

## 5. Mutation testing

These are hand-picked mutants, not a mutation score. Do not add the two results together.

| Result | Code it measured | Scope |
|---|---|---|
| **15 of 15 killed** | Final integration, 2026-11-05 | M20, M53 and M59 to M71 only. Baseline: 980 tests on the unmodified copy, because the harness leaves out three demo test files |
| 55 of 55 killed | Phase 06, commit `00dfaf2` | Historical. It predates the final integration |

The harness now holds 71 mutants. **All 71 have not been run on the final code.** A static check shows that each pattern still
matches exactly once.

## 6. Live LLM evaluation (historical)

One live run of the 20 fixed cases on `claude-sonnet-5-5` passed **20 of 20**, on 2026-11-04, on an earlier code version, at an
estimated 0.15 USD. It was not rerun.

- One sample per case, on cases written by the prompt's author.
- The model made no bypass attempt, so it mostly measured model behaviour. The attack evidence is offline.
- It predates the approved claims in the tool result, so it says nothing about their effect.
- The result files and `app/agents/llm.py` are unchanged, and the current scan confirms it.

Details: [phase-05-evaluation.md](../phase-05-evaluation.md).

## 7. What remains simulated or isolated

| Area | Status |
|---|---|
| Customer identity | **Simulated.** No external identity provider is available to this project |
| KYC, compliance, contract and signing | **Mock**, with no legal effect. No regulated provider and no QTSP |
| Tax authority | **None.** A local format check of an invented ID, which is then discarded |
| Depot provider and bank operator | **Mock** provider signed with a local key; DEV operator console with synthetic identities |
| Appropriateness | **Synthetic demonstration**, not legal compliance. Whether and when it is required is for the bank's compliance team |
| OIDC boundary | Isolated. Fails closed, and has never talked to a provider |
| KYC and signing typed boundaries, execution-only review | Isolated from the journey |
| Product answers (`ProductInformationAgent`) | Used by the integrated demo, not by the LLM agent |

The target design is in [technical-concept-2030.md](../technical-concept-2030.md). It is a proposal, not an implemented system.

## 8. Known security limitations

Details: [phase-06-security-reliability.md](../phase-06-security-reliability.md) and [final-integration.md](../final-integration.md#9-new-security-considerations).

- **F-1, partly closed.** `X-Dev-Customer-Id` no longer works on any route, and an agent's own credentials are refused on the
  customer routes. **The limit remains:** anyone with the handoff values who can use the DEV sign-in can impersonate a customer,
  supply the information and record a consent. `OPENED` still needs the provider's signature.
- **F-2 to F-7 are unchanged:** an unset environment means development (F-2); the link's host comes from the `Host` header (F-3);
  an unauthenticated caller can grow the audit table, and there are no rate limits (F-4); malformed callback headers answer 500 (F-5);
  the audit trail is append-only by trigger, not tamper-evident (F-6); some code comments claim more than was tested (F-7).
- **One-time token in same-origin requests.** It is in the link, the redirect `Location` and hidden form fields, and browsers send it
  in the `Referer` to the same origin. Other origins get none.
- **Stored in clear:** the synthetic answers and declarations. Production needs encryption, retention rules and a review of the
  church-tax field (GDPR Art. 9).
- **Shared DEV keys** are in the repository on purpose. They are synthetic, not credentials of any real service.
- **Bearer tokens** are not sender-constrained, and this is not OAuth 2.0 or FAPI 2.0.
- **Not done:** penetration test, load test, SAST, DAST and dependency scanning.

**Outgoing-change scan on 2026-11-06:** `.env` values compared, 0 matches. No API key, private key, live token, Gmail address or
German IBAN pattern, and no `.env`, database, cache or `.claude/` file. A pattern scan finds what it knows and does not prove that nothing
else exists.

## 9. Not measured

- A stopwatch rehearsal. The 1:45 of speaker notes is calculated from 244 words, before typing and window switches.
- Clicks by a human hand on the integrated page, a visible PowerShell console, Alt+Tab, a projector.
- Any browser but Chrome.
- A fresh install: no lock file is tracked.
- A recorded fallback video: none exists.

## 10. How to repeat it

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
.\.venv\Scripts\python.exe -m scripts.demo_integrated
.\.venv\Scripts\python.exe -m scripts.demo_happy
.\.venv\Scripts\python.exe -m scripts.demo_denied
.\.venv\Scripts\python.exe -m scripts.demo_manual_review
.\.venv\Scripts\python.exe -m scripts.provider_compat_matrix
.\.venv\Scripts\python.exe -m scripts.eval_llm --mode scripted --output "$env:TEMP\trusteq_scripted.json" --overwrite
.\.venv\Scripts\python.exe -m scripts.security_mutations --only M20,M53,M59,M60,M61,M62,M63,M64,M65,M66,M67,M68,M69,M70,M71
```

Dates, durations and IDs will differ per run. The mutants take several minutes. To walk through the browser journey by hand, use
[the demo runbook](phase-07-demo-runbook.md), Blocks 3 and 4.
