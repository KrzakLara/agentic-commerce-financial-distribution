# Technical reference

Commands, manual walkthroughs, configuration and detailed scope for **Secure Agentic Investment Onboarding**. The
[README](../README.md) gives the overview; this page holds the detail.

Everything here runs on synthetic data. Identity proofing, KYC/AML, contract signing, the depot provider and the trust
authority are local stand-ins. No real account is opened, no legally valid contract is created, and no regulatory
compliance is claimed.

**Contents**

1. [Conventions](#1-conventions)
2. [Installation](#2-installation)
3. [Offline demos](#3-offline-demos)
4. [Running the API](#4-running-the-api)
5. [Driving the flow by hand](#5-driving-the-flow-by-hand)
6. [Human review and token administration](#6-human-review-and-token-administration)
7. [Scripted and real LLM modes](#7-scripted-and-real-llm-modes)
8. [Tests and quality checks](#8-tests-and-quality-checks)
9. [Configuration](#9-configuration)
10. [Implemented, simulated and target state](#10-implemented-simulated-and-target-state)
11. [Security limitations](#11-security-limitations)
12. [Glossary of German terms](#12-glossary-of-german-terms)
13. [Further documentation](#13-further-documentation)

---

## 1. Conventions

- Commands are for **Windows PowerShell**, run from the repository root.
- They call `.\.venv\Scripts\python.exe` directly, so the virtual environment does not need to be activated and no
  execution-policy change is needed. Below, `$py` stands for that path:

  ```powershell
  $py = ".\.venv\Scripts\python.exe"
  ```

- Python **3.11 or 3.12** is required (`pyproject.toml` excludes 3.13+).

## 2. Installation

```powershell
py -3.12 -m venv .venv
& $py -m pip install --upgrade pip
& $py -m pip install -e ".[dev]"
```

Shortcut: `powershell -ExecutionPolicy Bypass -File scripts\setup.ps1`.

| Extra | Installs | Needed for |
|---|---|---|
| *(none)* | FastAPI, Pydantic, SQLAlchemy, httpx, joserfc, cryptography, Uvicorn | The application in scripted mode |
| `llm` | `anthropic==0.125.0` | The real Claude agent and the live evaluation |
| `local-services` | Authlib, Flask | The separate local OIDC provider |
| `dev` | All of the above plus pytest and Ruff | Development, tests and every demo |

The `anthropic` SDK is pinned to the last release built on `httpx`; from 1.0 it depends on `httpx2`, which would move
`starlette.testclient` off the approved stack.

## 3. Offline demos

All demos are offline (no API key, no network beyond `127.0.0.1`), deterministic, use their own throw-away database,
print no token, one-time challenge or key, and exit non-zero if any check fails. None of them touches `trusteq.db`.

| Command | What it shows | Options |
|---|---|---|
| `& $py -m scripts.demo_trust_journey` | The full-trust journey across separate processes (trust authority, OIDC provider, synthetic providers, bank, independent agent), then negative sections: forged provider identity, revoked delegation, failed identification, declined signature | none |
| `& $py -m scripts.demo_integrated` | One journey with discovery, tax information and the knowledge assessment up to `OPENED` | `--profile experienced\|novice\|incomplete`, `--depot-scenario NAME`, `--keep` |
| `& $py -m scripts.demo_happy` | **HAPPY**: draft, customer confirmation, signed provider confirmation, `OPENED`, revoked privileges, restart | `--keep`, `--no-restart` |
| `& $py -m scripts.demo_denied` | **DENIED**: attacks refused and audited, state unchanged | `--keep` |
| `& $py -m scripts.demo_manual_review` | **MANUAL REVIEW**: review case, "Speak to a person", operator console | `--keep` |

`demo_happy`, `demo_denied` and `demo_manual_review` are the original scenarios of the default profile: they switch the
tax and assessment step off, and say so.

**What `demo_happy` shows.** It creates a throw-away database in the temp folder, starts the real server, and plays four
separate actors (agent, customer, bank, mock provider): the bank's trusted bootstrap grant, discovery, the draft
(returning the application-bound tokens), handoff, customer confirmation, mock checks, submission, the provider's
signed confirmation, `OPENED` and the revoked onboarding privileges. It also shows what the agent tries that must fail:
confirming its own handoff, submitting with a read-only token, reusing its bootstrap grant, reading another agent's
application, a forged token, the removed self-asserted `X-Dev-Actor-Id` header, an unsigned provider callback, and
acting after `OPENED`. Then it restarts the server to prove that the result and the revocation persisted.

**What `demo_denied` shows.** Made-up providers, fabricated or ended partner channels and a self-chosen commission
recipient are refused (`422`). A prompt-injected agent tries to confirm for the customer, set `OPENED`, submit early or
act on the customer channel, and is refused each time. Stolen and forged credentials and seven manifest attacks are
rejected with their reason codes. The application then is still in its previous state (**not** `REJECTED`), and the
real customer can continue.

**What `demo_manual_review` shows.** A synthetic compliance review (`customer_ref` `synthetic-review-...`), the generic
agent view, the customer signing in on the bank page and pressing **"Speak to a person"** (a forged CSRF token is
refused; the answer is the same neutral "Request received"), the operator console (`list`, `show`, `request-info`,
refused decisions, `add-evidence`, `decide RE_EVALUATE`), compliance attempt 2 `PASS`, submission, `OPENED` and a
restart. Its final transition list shows who took each step, for example
`MANUAL_REVIEW -> COMPLIANCE_PENDING by OPERATOR` and `OPENED by DEPOT_PROVIDER_CONFIRMATION`.

**Browser version.** The customer's view of the trust journey in a visible Chrome needs Node.js 18+ and the pinned
Playwright in `scripts/demo_recording`. Setup and commands: [presentation/demo/README.md](presentation/demo/README.md).

## 4. Running the API

```powershell
& $py -m uvicorn app.main:app --reload
```

- Health check: <http://127.0.0.1:8000/health>; Swagger UI: <http://127.0.0.1:8000/docs>.
- The first start creates `trusteq.db` in the current folder (git-ignored) and its tables.
- The public contract covers the provider manifest, products, approved claims, applications (create, read, handoff,
  submit) and the customer routes. The provider callback and the HTML pages are intentionally not listed.
- Customers supply tax information and answer the knowledge questions before they can confirm. Set
  `TRUSTEQ_CUSTOMER_INFORMATION_REQUIRED=false` for the original flow.

## 5. Driving the flow by hand

This walks through the **default profile** with the agent, the customer and the provider as separate actors.

```text
OPERATOR   dev bootstrap: agent gets ONE narrow token (create_draft, 1 application, 1 customer)
AGENT      discover provider and product -> verified fixture metadata (public)
AGENT      create application            -> DRAFT; onboarding + status tokens, shown once
AGENT      request handoff               -> one-time challenge + action digest
CUSTOMER   opens the link (a pointer, not an authorization), signs in (SIMULATED),
           reviews and confirms the exact action (+ CSRF)
BANK       verifies challenge, identity, digest, catalog, mandate;
           mock KYC -> mock compliance -> mock contract recorded
AGENT      submit (after consent)        -> policy gate on stored evidence -> hand-over
PROVIDER   signed "opened" confirmation  -> OPENED; grants and tokens revoked; audit
AGENT      read status                   -> OPENED (read-only token)
AGENT      submit again                  -> 401 invalid_token (privileges gone)
```

The agent's first token comes from the trusted development CLI; there is no HTTP endpoint for it. The provider callback
needs a secret, generated here for this session only; never commit it. Run every command from the repository root, so
that the server and the CLI share `trusteq.db`.

**1. Start the server** with a throw-away callback secret (window A):

```powershell
$secret = [guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")
$env:TRUSTEQ_MOCK_PROVIDER_CALLBACK_SECRET = $secret
& $py -m uvicorn app.main:app
```

**2. Operator** (window B): grant `agent-a` one draft for `demo-customer-1`. The token is printed once.

```powershell
$boot = (& $py -m scripts.dev_auth bootstrap --agent agent-a --customer demo-customer-1 |
         Select-String '^\s+tqa_').Line.Trim()
```

**3. Agent** (window B): discover, create a draft, ask for the customer's approval.

```powershell
$base = "http://127.0.0.1:8000"
Invoke-RestMethod "$base/v1/providers/fixture-bank/manifest"
Invoke-RestMethod "$base/v1/products"

$hdr = @{ Authorization = "Bearer $boot"; "Idempotency-Key" = [guid]::NewGuid().ToString() }
$body = '{"provider_id":"fixture-bank","product_id":"depot-basic",' +
        '"channel_id":"direct","customer_ref":"demo-customer-1"}'
$app = Invoke-RestMethod -Method Post "$base/v1/applications" -Headers $hdr `
       -ContentType application/json -Body $body

# onboarding token: handoff + submit; status token: read-only
$onb = @{ Authorization = "Bearer $($app.authorization.onboarding.access_token)" }
$sts = @{ Authorization = "Bearer $($app.authorization.status.access_token)" }
$appUrl = "$base/v1/applications/$($app.application_id)"

$handoff = Invoke-RestMethod -Method Post "$appUrl/handoff" -Headers $onb
$handoff.confirmation_url   # give this link to the customer
```

**4. Customer:** open `$handoff.confirmation_url` in a browser. The page first asks for the *simulated* sign-in: enter
`demo-customer-1`. It then shows provider, product, channel, application reference, the exact action and the
disclosure version. Enter the synthetic tax information (`DE`, `SYNTH-TAX-700421`, not a US person), answer the
questions and press **Confirm and authorize**. Any other customer id is refused, and the forms are CSRF-protected.

**5. Agent again:** read the status and submit.

```powershell
Invoke-RestMethod $appUrl -Headers $sts                     # IN_PROGRESS
Invoke-RestMethod -Method Post "$appUrl/submit" -Headers $onb
```

**6. Mock provider:** only a signed message may open the account. This helper plays the provider, with the same secret
as step 1:

```powershell
@'
import os, sys, httpx
from app.integrations.provider_callback import build_signed_confirmation
app_id = sys.argv[1]
secret = os.environ["TRUSTEQ_MOCK_PROVIDER_CALLBACK_SECRET"].encode()
cb = build_signed_confirmation(secret, application_id=app_id, provider_ref=f"MOCK-DEPOT-{app_id}")
url = "http://127.0.0.1:8000/internal/depot-provider/confirmations"
r = httpx.post(url, content=cb.body, headers=cb.headers)
print(r.status_code, r.json())
'@ | & $py - $app.application_id

Invoke-RestMethod $appUrl -Headers $sts                     # OPENED
Invoke-RestMethod -Method Post "$appUrl/submit" -Headers $onb  # 401: revoked
```

Flow details: [onboarding-flow.md](onboarding-flow.md) · tokens, scopes and lifecycle:
[authorization.md](authorization.md).

## 6. Human review and token administration

**Operator console** (development only; there is no operator HTTP endpoint):

```powershell
& $py -m scripts.dev_operator --operator op-reviewer-1 list
& $py -m scripts.dev_operator --operator op-reviewer-1 show --case <id>
& $py -m scripts.dev_operator --operator op-reviewer-1 decide --case <id> --decision RE_EVALUATE
```

Subcommands: `list`, `show`, `request-info`, `add-evidence`, `decide`. Decisions: `RE_EVALUATE`, `RESUME`, `REJECT`,
`ACKNOWLEDGE`. `REJECT` needs a supervisor identity (`op-supervisor-1`). There is no "approve".

**Customer asks for a person.** A signed-in customer uses the **"Speak to a person"** button on the review and
confirmation pages, or `POST /v1/customer/applications/{id}/request-human`. Both need the customer's server-side session
and its CSRF token for that application; a self-asserted `X-Dev-Customer-Id` header or an agent token is refused.

**Tokens.** Lost a token, or continuing an application created before scoped authorization existed:

```powershell
& $py -m scripts.dev_auth app-tokens --application-id <id>   # re-issues, never wider
& $py -m scripts.dev_auth revoke --jti <jti>                 # revokes one token server-side
```

## 7. Scripted and real LLM modes

**Scripted (default):** nothing to configure, no key, no model. All offline demos use it.

**Real LLM (explicit opt-in):** the model only proposes tool calls; the six tools, the REST API and the bank's policy
are the same. Put the key into the local, git-ignored `.env`, never into `.env.example` or a commit:

```powershell
Copy-Item .env.example .env
# then edit .env:  ANTHROPIC_API_KEY=<your key>
```

To select the agent in your own code through `build_customer_agent`, set `TRUSTEQ_AI_EXECUTION_MODE=llm`,
`TRUSTEQ_ENABLE_LLM=true` and `TRUSTEQ_LLM_PROVIDER=anthropic`; a missing key or SDK fails closed.

| Command | Purpose | Key | Cost |
|---|---|---|---|
| `& $py -m scripts.demo_llm` | One real onboarding interaction, start to finish | Yes | Billed, a few cents |
| `& $py -m scripts.eval_llm --mode scripted --output scripted.json` | Offline baseline of the 20 fixed cases | No | Free |
| `& $py -m scripts.eval_llm --mode llm --output my-run.json` | The 20 cases with the real customer agent | Yes | Billed |
| `& $py -m scripts.eval_llm --report docs\evaluation\phase-05-live-results.json` | Print a stored result | No | Free |
| `& $py -m scripts.eval_communication` | Answer pipeline with a fake model (`--show-examples`, `--write FILE`) | No | Free |
| `& $py -m scripts.eval_live_llm --dry-run` | Live-evaluation runner with a scripted client | No | Free |
| `& $py -m scripts.eval_live_llm --recompute docs\evaluation\final-live-llm\results.json` | Re-derive every metric from stored records | No | Free |
| `& $py -m scripts.eval_live_llm --live --approve-usd 2.00 --out <new folder>` | Live run of the 19 pre-registered questions; add `--checkpoint <file>` | Yes | Billed, budget-capped |

Without a key, `demo_llm` and `eval_llm` print `BLOCKED` and exit 3 without calling anything. A new live run differs
from the stored one because the model is not seeded: write it to a new folder and never overwrite the stored evidence.

Model, limits and results: [phase-05-evaluation.md](phase-05-evaluation.md) (customer agent) and
[evaluation/final-live-llm/README.md](evaluation/final-live-llm/README.md) (answer pipeline).

## 8. Tests and quality checks

```powershell
& $py -m pytest
& $py -m ruff check .
& $py -m ruff format --check .
```

All at once: `powershell -ExecutionPolicy Bypass -File scripts\check.ps1`.

Tests use in-memory or temporary SQLite databases and real HTTP-level clients, never touch `trusteq.db`, and include
runs of the demos. Model calls are replaced by fake clients; no test calls a paid API.

**Targeted mutation testing.** Each mutant breaks one security control in a temporary copy (your files are never
modified), and the suite must notice:

```powershell
& $py -m scripts.security_mutations              # all mutants
& $py -m scripts.security_mutations --list       # list them
& $py -m scripts.security_mutations --only M01,M17
```

## 9. Configuration

The application runs with **no `.env` file and no API key**. To override settings, copy
[`.env.example`](../.env.example) to `.env` (git-ignored; never commit it). Every variable is documented there.

All variables use the `TRUSTEQ_` prefix, except the provider's usual `ANTHROPIC_API_KEY`. They cover:

- application name, environment (`development`, `test`, `production`), debug, database URL, log level and format;
- the AI execution mode (`scripted` or `llm`) and the optional LLM settings and limits;
- the development switches: agent bootstrap issuance, simulated customer sign-in, operator console;
- lifetimes of the handoff, onboarding grant, bootstrap grant, status grant and customer session;
- the mock provider callback secret and the mock depot scenario (`TRUSTEQ_MOCK_DEPOT_SCENARIO`);
- the full-trust profile (`TRUSTEQ_FULL_TRUST_PROFILE`), OIDC, signing, identity-proofing and partner-key settings;
- allowed CORS origins.

Invalid combinations fail at startup. Examples: `debug=true`, an explicit development switch or the mock callback secret
in **production**; `llm` mode without `TRUSTEQ_ENABLE_LLM=true`; the full-trust profile with
`TRUSTEQ_CUSTOMER_IDENTITY_MODE=dev_simulated`.

`TRUSTEQ_CUSTOMER_IDENTITY_MODE=oidc` switches the development sign-in off. Without a complete OIDC configuration
(issuer, client, redirect URI, JWKS), the customer pages fail closed. `TRUSTEQ_DEV_ACTOR_AUTH_ENABLED` and the
self-asserted `X-Dev-Actor-Id` header were removed when scoped agent authorization was introduced.

## 10. Implemented, simulated and target state

| Area | Implemented and tested | Mocked or simulated | Target, not built |
|---|---|---|---|
| Provider discovery | Bank register as trust root; pinned digest; signed, versioned, expiring manifests; identity, domain, endpoint, capability and channel binding; no rollback; validity re-checked at use; fail closed. In the full-trust profile: JWS-signed metadata with a trust chain | Synthetic provider, partners, register and trust authority; HMAC with a shared development key in the default profile; no network fetch of manifests | Provider-signed manifests (JWS or eIDAS seal) over mTLS from the verified domain, key rotation and revocation in a real federation |
| Product information and disclosures | Versioned terms, fees, conditions, risk, sources with digests; receipts bound to exact versions and the action digest; stale approvals refused at confirmation and submission | All content synthetic | Provider feeds with legal sign-off, wallet-based signed consent, re-consent flow |
| Partner attribution | Registry-based, customer-confirmed, append-only; the agent cannot name a recipient | Synthetic partners; no commission paid | Contractual partner registry, commission settlement |
| Human review | Durable cases, generic customer acknowledgement, fixed operator decisions, re-evaluation as a new deterministic attempt, supervisor-only rejection, audit | Synthetic operators and evidence references; mock compliance scenario switch | Case management with SSO, RBAC, four-eyes review, SLAs, evidence store |
| Agent | Scripted (default) and a real Claude agent behind an explicit flag: the same six tools, REST only; allowlisted, typed, bounded; evaluated on fixed datasets | Every provider and catalog behind it | Provider-side explainer, MCP/A2A adapters beyond the A2A subset, production monitoring of model behaviour |
| Agent authorization | Opaque bearer tokens stored as SHA-256 hashes; issuer, audience, expiry, `jti`, revocation, per-operation scopes, resource binding; single-use bootstrap; revoked at terminal states. DPoP-bound delegated tokens in the full-trust profile | First grant from a trusted development CLI | OAuth 2.0 / FAPI 2.0 authorization server, sender-constrained tokens for all access (DPoP/mTLS), RFC 9396 / RFC 8693 |
| Customer authentication | Server-side sessions (hashed secret; HttpOnly, SameSite=Strict, Secure-on-HTTPS cookies; expiry; rotation) and per-target CSRF tokens. OIDC code flow with PKCE against a separate local provider in the full-trust profile | The development sign-in of the default profile, which any client can use or script (finding F-1) | Bank-controlled strong authentication (online banking, eID, EUDI wallet), a customer case view, phone and chat channels |
| Tax information | Customer page and JSON API, deterministic validation, required before confirmation, identifier discarded | Synthetic format only; no tax authority | Encrypted storage and retention; checks and reporting as legally required |
| Appropriateness | Customer page and JSON API, deterministic result, warning and acknowledgement; never blocks the account | Synthetic questions, not legal compliance; no order step | Compliance-owned rule sets per product and service |
| Product communication | Approved claims with source and version at the agent's tool boundary; claim check API; guarded answers through `CommunicationGuard`; hostile catalog text excluded | Synthetic catalog | Signed claim feeds with legal sign-off; the bank still does not control third-party wording |
| KYC, compliance, contract, depot provider | The deterministic workflow around them; versioned provider adapter with an explicit fallback policy; the signed callback is the only path to `OPENED` | **Mock** results (`mock=true`, `MOCK-` references); test signer at `TEST_NO_LEGAL_EFFECT`; synthetic identity proofing | Contracted regulated providers, a QTSP for qualified signatures, partner integration over mTLS |
| Policy, state machine, audit, idempotency, persistence, recovery | Implemented and tested; schema v6 with transactional, resumable upgrades | - | High-availability database, durable workflow engine, DORA controls, external security review |

The per-area classification of the full-trust integration (LOCAL VERIFIED, ADAPTER READY, BLOCKED, CONCEPT ONLY) is in
[agentic-commerce-integration.md](agentic-commerce-integration.md).

## 11. Security limitations

- **Simulated identities.** In the default profile, anyone who completes the development sign-in can claim any customer.
  What is real is the server-side session, its expiry, rotation, cookie flags and CSRF binding around it. Anyone who can
  run the operator console on the server can act as a synthetic operator. Both are disabled in production. The
  self-asserted customer header no longer works on any route.
- **No customer case view.** `GET /v1/customer/cases/{id}` is deferred: it must never reveal internal review reasons.
- **Shared-key signatures in the default profile.** Manifest signatures and the depot callback use HMAC with a shared
  key, a symmetric stand-in: whoever holds the key could also sign. Key, provider, partners and register are synthetic.
- **Bearer tokens.** Outside the full-trust delegation, agent tokens are bearer tokens: no DPoP or mTLS binding, no
  client authentication, no refresh flow.
- **No rate limiting,** and the audit is append-only but not tamper-evident.
- **No re-consent flow.** A change of terms after the customer's consent blocks submission (fail closed); a new
  application is needed.
- **Not legal evidence.** Mock results are not legal, AML or identity evidence. No regulatory compliance or certification
  is claimed; legal review is required before any real deployment.

Open findings with their locations: [robustness-evidence.md](robustness-evidence.md). Security
validation and mutation evidence: [phase-06-security-reliability.md](phase-06-security-reliability.md).

## 12. Glossary of German terms

The scenario is set in the German market. These legal and industry terms appear in the code and documents:

| Term | Meaning |
|---|---|
| Wertpapierdepot (depot) | Securities account: the custody account that the onboarding opens |
| Hausbank | The customer's relationship bank; here, a registered partner bank that distributes the account |
| AG / eG / GmbH | German legal forms: stock corporation, registered cooperative, limited liability company |
| KVG (Kapitalverwaltungsgesellschaft) | Investment management company under the KAGB (German Investment Code) |
| BaFin | Federal Financial Supervisory Authority |
| GwG | German Anti-Money Laundering Act (Geldwäschegesetz) |
| WpHG | German Securities Trading Act (Wertpapierhandelsgesetz) |

All institutions, people, places (Sampletown, Example Valley), registry references and domains in the prototype are
fictional.

## 13. Further documentation

| Topic | Document |
|---|---|
| Architecture and modules | [architecture.md](architecture.md) |
| State machine and authorities | [state-machine.md](state-machine.md) |
| Authorization: tokens, scopes, lifecycle | [authorization.md](authorization.md) |
| Onboarding flow, handoff, recovery | [onboarding-flow.md](onboarding-flow.md) |
| Identity and delegation | [identity-and-delegation.md](identity-and-delegation.md) |
| Trust boundaries | [trust-boundaries.md](trust-boundaries.md) |
| Database, triggers, migrations | [database.md](database.md) |
| API contracts | [api-contracts.md](api-contracts.md) |
| Regulated communication | [regulated-communication.md](regulated-communication.md) |
| Provider discovery and trust | [provider-discovery-and-trust.md](provider-discovery-and-trust.md) |
| Integrated trust journey | [agentic-commerce-integration.md](agentic-commerce-integration.md), [integration-evidence.md](integration-evidence.md) |
| Decision register (26 decisions) | [decision-register.md](decision-register.md) |
| Target architecture 2030 | [technical-concept-2030.md](technical-concept-2030.md) |
| Autonomy levels L0 to L4 and why L3 | [autonomy-levels.md](autonomy-levels.md) |
| Regulatory policy matrix | [regulatory-policy-matrix.md](regulatory-policy-matrix.md) |
| Security controls against manipulation and deprecation, open findings | [robustness-evidence.md](robustness-evidence.md) |
| Diagrams | [diagrams/README.md](diagrams/README.md) |
| Demo recording and browser demo | [presentation/demo/README.md](presentation/demo/README.md) |

**Key source files**

| Concern | File |
|---|---|
| Transition table and per-edge authority | [`app/domain/workflow.py`](../app/domain/workflow.py) |
| Deterministic policy gate | [`app/services/policy.py`](../app/services/policy.py), [`app/services/regulatory_policy.py`](../app/services/regulatory_policy.py) |
| Scoped agent authorization | [`app/services/authorization_service.py`](../app/services/authorization_service.py), [`app/api/auth.py`](../app/api/auth.py) |
| ORM tables (separate from domain models and API schemas) | [`app/repositories/orm.py`](../app/repositories/orm.py) |
| Guarded communication (`CommunicationGuard`) | [`app/services/regulated_communication.py`](../app/services/regulated_communication.py) |
| Claude agent and answer proposer | [`app/agents/llm.py`](../app/agents/llm.py), [`app/agents/llm_answer_proposer.py`](../app/agents/llm_answer_proposer.py) |
| External ports | [`app/integrations/ports.py`](../app/integrations/ports.py) |

**Development history and background**

These documents record how the prototype was built and the research behind it. Several were written at an earlier
stage, and where they disagree with the code or with later evaluation reports, the later source applies.

| Topic | Document |
|---|---|
| Final integration of tax, assessment, provider adapter and claims | [final-integration.md](final-integration.md) |
| Communication and onboarding evaluation | [communication-onboarding-evaluation.md](communication-onboarding-evaluation.md) |
| Verified discovery, disclosures and human review (with the core freeze) | [phase-04-traceability.md](phase-04-traceability.md) |
| Today's depot-opening process at a reference provider, from public sources | [research/ist-analyse-union-investment.md](research/ist-analyse-union-investment.md) |
| Process evidence matrix, roles and trust boundaries | [research/process-evidence-matrix.md](research/process-evidence-matrix.md), [research/roles-raci-trust-boundaries.md](research/roles-raci-trust-boundaries.md) |
| The prototyped end-to-end scenario, traced to code and tests | [research/page3-e2e-traceability.md](research/page3-e2e-traceability.md) |
