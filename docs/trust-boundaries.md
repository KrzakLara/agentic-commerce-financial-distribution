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

Phase 02 implements this ceremony in prototype form - see [onboarding-flow.md](onboarding-flow.md);
Phase 03 hardens it (server-side customer sessions, CSRF tokens, agent credentials refused). The
remaining weak point is step 2: the customer authentication itself is a **DEV-only simulation**.

## Phase 03: how the boundaries are enforced now

| Boundary | Real, enforced and tested | Simulated / not production |
|---|---|---|
| T0b -> T1 (agent -> API) | `Authorization: Bearer` opaque token, SHA-256 hash lookup; issuer, audience, issued/expiry, revocation of token and grant; one scope per operation; resource binding (owner AND bound application, else 404); generic 401/403/404, precise reasons audited ([authorization.md](authorization.md)) | tokens come from a trusted DEV bootstrap, not an OAuth/FAPI authorization server; no client authentication, no sender-constrained tokens, no rate limiting |
| Agent self-authorization | no HTTP endpoint issues, exchanges or widens tokens; grant/token claims immutable (DB triggers); a bootstrap grant creates ONE application for ONE named customer | - |
| T1 -> T2 (API -> core) | services re-check scope, binding and the active onboarding grant inside the transaction; policy gate and state machine unchanged | - |
| T0a (customer channel) | separate credential: server-side session (hashed secret, HttpOnly + SameSite=Strict + Secure on HTTPS cookies scoped to `/customer` (pages) and `/v1/customer` (customer API, Phase 04), server-enforced expiry, rotation on sign-in); CSRF token bound to session + handoff (confirmation) or session + application (request-human, Phase 04); login CSRF; `Sec-Fetch-Site` / `Origin` refusal; any `Authorization` header refused (audited) | the sign-in itself: anyone can type a customer id; the JSON ceremony's `X-Dev-Customer-Id` header |
| T2 -> T3 (provider callback) | unchanged: HMAC signature, freshness, replay/duplicate checks; a bearer token never substitutes the signature | shared-secret MOCK credential instead of mutual TLS |
| Completion | at any terminal state the bootstrap + onboarding grants, their tokens and open handoffs are revoked in the transition's transaction; the read-only status grant expires on its own | - |

What the agent never receives, and how that is checked: customer session secrets (separate
cookie on `/customer`, never sent to the agent; `tqs_` secrets are rejected on agent routes),
signing authority (no tool, no route), provider callback secrets (never in any response or log),
raw KYC/AML data and compliance rationale (generic states only; tested by
`test_agent_facing_responses_never_expose_internal_or_compliance_detail`), and its own tokens are
kept out of tool results so a model cannot see them.

## Phase 01: how the boundaries were enforced (historical)

Phase 03 replaced the `X-Dev-Actor-Id` header described below with bearer tokens.

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
* An agent token is never a customer credential and a customer session is never an agent
  credential. No HTTP route issues or widens agent authority.
* Tokens, token hashes, session secrets and the handoff nonce are never logged or audited.
* `ProviderSubmission` has no `opened` field; `AgentToolName` has no approve/sign/set-opened tool.
* Fail closed if policy, authorization or a provider callback is unavailable.
* Synthetic data only. Every mock is labelled `MOCK` and is not a real or certified integration.

## Phase 04: provider trust, data-only text, human review

* **Provider trust root (T2):** the bank's own register (`PROVIDER_REGISTER`, `PARTNER_REGISTER`,
  `TRUST_ANCHORS`). A manifest (T3 content) can only *match* it: identity, domain, endpoint,
  capabilities and channels come from the register; a registry reference (LEI) is never proof of
  endpoint ownership. Verification fails closed at startup and validity is re-checked at use.
* **Provider-supplied text is data.** Names, conditions and risk summaries are rendered escaped,
  passed to the agent only through allow-listed structured tool fields, and never read by policy,
  scopes or the tool registry. A hostile text cannot add a tool, change a state, widen a scope or
  rename a provider (prompt-injection fixture tests).
* **Human review stays inside T2.** Operators act through a trusted console, not over HTTP; the
  customer's request ("Mit einer Person sprechen") crosses T0a -> T1 only on the customer's own
  server-side session with a CSRF token bound to that session and application - a
  self-asserted customer id is not accepted, agent credentials are refused (audited). Internal review reasons, evidence and decisions never cross to T0a/T0b:
  the customer gets a generic acknowledgement, the agent the coarse state `ACTION_REQUIRED`.
