# Trust zones and boundaries (from ADR-001 v1.1, section 03)

Trust is granted by evidence and controls, not by zone labels alone.

| Zone | Who / what | Trust |
|---|---|---|
| **T0a** | Human customer - the legal decision-maker | Authenticated only through a bank-controlled channel |
| **T0b** | External AI agent (software) | **Always untrusted** |
| **T1** | API edge: verifies requests and provider claims | Validates schemas, rate limits, auth, action policy |
| **T2** | Trusted core: policy and account-opening authority | Authoritative |
| **T3** | Contracted regulated providers (KYC, compliance, signing, depot) | Trusted only for confirmed, idempotent, audited outcomes |

## Boundaries

| Boundary | Control | What the agent never receives |
|---|---|---|
| T0b -> T1 | Verify provider endpoint and catalog; validate schemas, rate limits, auth, action policy | A trusted status just because an LLM or search result says so |
| T1 -> T2 | Short-lived scoped mandate; actor/customer separation; replay-safe, request-bound handoff | Customer session, identity credentials, signing authority, unconstrained bank access |
| T2 -> T3 | Explicit service adapters; confirmed outcomes; idempotent operations; audited status changes | Raw AML flags, privileged compliance rationale, unfiltered personal data |

## Secure customer handoff (threat: session substitution)

1. **Initiate** - agent requests a handoff for one application; server stores a one-time nonce
   (hash), expiry and the hash of the immutable proposed action.
2. **Authenticate** - customer uses an independently authenticated bank-controlled channel; a
   QR/link alone does not transfer ownership.
3. **Review** - bank shows verified legal entity, product, channel attribution and exact digest.
4. **Bind and confirm** - server binds authenticated customer + application + exact action;
   invalid or replayed requests fail closed.
5. **Complete** - agent receives only a coarse status; the mandate ends after completion.

Phase 02 implements this ceremony in prototype form - see [onboarding-flow.md](onboarding-flow.md).
The weak point is step 2: the customer authentication is a **DEV-only simulation**. Phase 03
hardens the mandate/handoff claims.

## Phase 01: how the boundaries are enforced so far

* **T0b -> T1:** the agent's only identity is the DEV/TEST header `X-Dev-Actor-Id` (not
  authentication, never active in production). Every body is validated with extra fields
  rejected; the agent cannot supply a status.
* **Ownership:** every read is scoped to the owning actor; foreign ids look like missing ones.
* **T1 -> T2:** state changes exist only as `ApplicationService.transition`, which takes a
  server-chosen `TransitionAuthority`. The agent authority is on no edge; only
  `DEPOT_PROVIDER_CONFIRMATION` can open a depot.
* **Information disclosure:** the agent sees a coarse state; KYC vs compliance vs manual review
  are indistinguishable, and audit data is never returned.

## Invariants the code must keep

* The agent cannot approve a contract, self-verify identity, decide AML outcomes or mark a
  depot `OPENED`. Only a trusted depot-provider confirmation advances to `OPENED`.
* `ProviderSubmission` has no `opened` field; `AgentToolName` has no approve/sign/set-opened tool.
* Fail closed if policy, authorization or a provider callback is unavailable.
* Synthetic data only. Every mock is labelled `MOCK` and is not a real or certified integration.
