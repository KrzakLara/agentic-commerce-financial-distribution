# TRUSTEQ - Secure Agentic Depot Onboarding

A modular-monolith FastAPI project for the TRUSTEQ GmbH AI Engineering case study.

**Current status: Phase 04 - verified discovery, disclosures, attribution and human review.**
Provider manifests are signed, versioned and expiring and are verified against the bank's own
register (identity, domain, endpoint, capabilities, channels, no rollback); product terms and the
attributed partner are bound into what the customer approves, so changed terms invalidate an old
approval; a customer can ask for a person with the **"Mit einer Person sprechen"** button
(server-side customer session + CSRF), a synthetic compliance `REVIEW_REQUIRED` opens a durable
review case, and a bank operator can re-evaluate with evidence - never override. The **P0 core is
frozen** ([docs/phase-04-traceability.md](docs/phase-04-traceability.md#p0-core-freeze)). Three
reproducible demo outcomes: **HAPPY**, **DENIED**, **MANUAL REVIEW**. Phases 00-03 (foundation,
persistence, state machine, end-to-end flow, scoped agent authorization) are complete; see
[docs/phase-04-traceability.md](docs/phase-04-traceability.md).

> **Everything here is a prototype with synthetic data.** KYC, compliance/AML, contract signing
> and the depot provider are **MOCK**. The customer sign-in is a **development-only simulation**,
> not identity verification. Agent authorization is a **controlled development implementation**,
> not production OAuth 2.0 / FAPI 2.0. Nothing opens a real account or creates a legally valid
> contract. No regulatory compliance is claimed.

## Case study objective

A German fund provider wants AI agents to discover its products and start a securities-account
(Wertpapierdepot) onboarding. The case study asks for a trustworthy, compliant, maintainable
architecture plus a minimum convincing prototype. The approved decision is
**ADR-001 v1.1 - L3 controlled agentic onboarding**:

> *LLM proposes; policy decides; customer authorizes; bank executes.*

An external AI agent (always untrusted) coordinates; deterministic bank-side policy and a
licensed depot provider enforce. The agent can never confirm for the customer, approve KYC, sign a
contract or mark a depot `OPENED`.

Architecture sources (not modified by this project): `TRUSTEQ_ADR_v1.1_FINAL.docx` and
`TRUSTEQ_IMPLEMENTATION_PHASES_v2.md`.

## The happy path in one picture

```text
OPERATOR (trusted dev bootstrap) -> agent gets ONE narrow token: create_draft, 1 application, 1 customer
AGENT (untrusted)            BANK                              CUSTOMER           MOCK PROVIDER
discover provider/product -> verified fixture metadata (public)
create application        -> DRAFT; bootstrap consumed; onboarding + status tokens (shown once)
request handoff           -> one-time challenge + action digest        [onboarding token]
        -------- link (a pointer, not an authorization) -------->  sign in (SIMULATED,
                                                                    server-side session), review,
                                                                    confirm exact action (+CSRF)
                          <- verified: challenge, identity, digest, catalog, mandate
                          MOCK KYC -> MOCK compliance -> MOCK contract recorded
submit (after consent)    -> policy gate on stored evidence -> hand-over   [onboarding token]
                                                                    ------ signed "opened" ------>
                          <------------------------------------------------ confirmation
                          OPENED + bootstrap/onboarding grants + tokens revoked + audit
read status               -> OPENED (read-only status token; never claimed by the agent)
submit again              -> 401 invalid_token (privileges gone)
```

Details: [docs/onboarding-flow.md](docs/onboarding-flow.md) (flow, handoff, recovery) and
[docs/authorization.md](docs/authorization.md) (tokens, scopes, lifecycle).

## Architecture summary

* **Modular monolith** with ports/adapters; REST is the canonical API (MCP/A2A are optional
  later adapters, never the core).
* **Trust zones** T0a customer, T0b external agent, T1 API edge, T2 trusted core, T3 regulated
  providers. See [docs/trust-boundaries.md](docs/trust-boundaries.md).
* **Internal domain models** (`app/domain`) are separate from **external API schemas**
  (`app/api/schemas`) and from **ORM tables** (`app/repositories/orm.py`).
* **Persistent state machine** with an explicit transition table and per-edge authority
  ([docs/state-machine.md](docs/state-machine.md)); atomic transitions, optimistic locking,
  database triggers as defence in depth ([docs/database.md](docs/database.md)).
* **Deterministic policy gate** (`app/services/policy.py`): submission needs stored evidence of
  consent, disclosure acknowledgement and passed checks - no LLM output is read.
* **Scoped agent authorization** (`app/services/authorization_service.py`, `app/api/auth.py`):
  enforced at the API and service boundary, never by the agent's own instructions
  ([docs/authorization.md](docs/authorization.md)).
* The **agent package** can only talk to the bank over the public REST API via one narrow tool
  contract. See [docs/architecture.md](docs/architecture.md) and
  [docs/api-contracts.md](docs/api-contracts.md).

## Technology stack

Python 3.11 / 3.12 (developed on 3.12), FastAPI, Pydantic v2 + Pydantic Settings,
SQLAlchemy 2.x (SQLite), Uvicorn, httpx, pytest, ruff. Dependencies are constrained in
`pyproject.toml`. No LLM and no API key is used anywhere.

## Project structure

```text
Trusteq/
├── app/
│   ├── main.py            # create_app() + ASGI entry point
│   ├── container.py       # composition root: catalog + MOCK providers + clock
│   ├── api/               # routers, middleware, errors, schemas, bearer auth, customer session, HTML pages
│   │   └── routes/        # health, catalog, applications, customer, internal (provider callback)
│   ├── core/              # config, logging, exceptions
│   ├── domain/            # typed contracts (grants, tokens, sessions), state-machine rules, visibility
│   ├── services/          # Application/Onboarding/Authorization/CustomerSession services, policy gate
│   ├── repositories/      # ORM models, repositories, unit of work (SQLAlchemy/SQLite), schema upgrade
│   ├── integrations/      # ports, MOCK adapters, verified catalog fixture, REST agent gateway + token store
│   └── agents/            # ScriptedCustomerAgent (untrusted zone T0b)
├── tests/                 # pytest suite (+ fixtures/schema_v2.sql, schema_v3.sql for the upgrade tests)
├── docs/                  # architecture, authorization, flow, trust boundaries, API, state machine, database, Phase 04 traceability
├── scripts/               # setup.ps1, check.ps1, demo_happy/denied/manual_review.py, dev_auth.py, dev_operator.py, security_mutations.py
├── .env.example  .gitignore  pyproject.toml  README.md
```

## Requirements

* Windows 10/11 with PowerShell
* Python **3.11 or 3.12** (the `py` launcher is used below; 3.13+ is not supported)
* A few MB of free disk space (the demo and tests use small temporary SQLite files)

## Installation (PowerShell)

From the project folder:

```powershell
cd C:\Users\larak\Desktop\Projektici\Trusteq
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

(Using `.\.venv\Scripts\python.exe` directly means you do not need to activate the venv, so no
execution-policy change is required.) Shortcut: `powershell -ExecutionPolicy Bypass -File scripts\setup.ps1`

## Run the three demos (recommended first step)

```powershell
.\.venv\Scripts\python.exe -m scripts.demo_happy           # HAPPY: ... -> OPENED
.\.venv\Scripts\python.exe -m scripts.demo_denied          # DENIED: refused, audited, state unchanged
.\.venv\Scripts\python.exe -m scripts.demo_manual_review   # MANUAL REVIEW: case, request-human, operator
```

All three are offline (no LLM key, no network beyond `127.0.0.1`), deterministic, use their own
throw-away database, print no token, challenge or key, and exit non-zero if any check fails.

**2-3 minute presentation** (each demo runs in seconds; the times below are speaking time): run
`demo_happy --no-restart` (about 40 s: agent drafts, customer
confirms on the bank page, provider confirms, `OPENED`, privileges revoked); then `demo_denied`
(about 30 s: show sections 2 "prompt-injected agent" and 4 "manifests never load", and the line
"NOT REJECTED"); then `demo_manual_review` (about 60 s: show sections 2-3 "generic state
ACTION_REQUIRED", 4 the generic acknowledgement and 5 the operator console with the refused
decisions and the re-evaluation as attempt 2). Close on the transition list in section 7:
`MANUAL_REVIEW -> COMPLIANCE_PENDING by OPERATOR`, `OPENED by DEPOT_PROVIDER_CONFIRMATION`.

### What `demo_happy` shows

It creates a throw-away database in your temp folder, starts the real server, and plays four
separate actors (agent, customer, bank, MOCK provider): the bank's trusted bootstrap grant,
discovery, draft (returning the application-bound tokens), handoff, customer confirmation,
MOCK checks, submit, the provider's signed confirmation, `OPENED` and the revoked onboarding
privileges - plus what the agent tries that must fail: confirming its own handoff, submitting
with a read-only token, reusing its bootstrap grant, reading another agent's application, a forged
token, the old self-asserted `X-Dev-Actor-Id` header, an unsigned provider callback, and acting
after `OPENED`. Then it restarts the server to prove the result and the revocation persisted. It
prints ids, scopes and states, never a token, the one-time challenge or any other credential,
deletes its temp data, and exits `0` only if every check holds. Options: `--keep`,
`--no-restart`. It never touches `trusteq.db`.

`demo_denied` shows made-up providers, fabricated or ended partner channels and a self-chosen
commission recipient being refused (`422`), a prompt-injected agent that tries to confirm for the
customer, set `OPENED`, submit early or act on the customer channel (all refused), stolen and
forged credentials, seven manifest attacks rejected with their reason codes, and then that the
application is still in its previous state (**not** `REJECTED`) and the real customer can
continue. `demo_manual_review` shows the synthetic compliance review (`customer_ref`
`synthetic-review-...`), the generic agent view, the customer signing in on the bank page and
pressing **"Mit einer Person sprechen"** (a forged CSRF token is refused; the answer is the same
neutral "Anfrage erhalten"), the operator console (`list`, `show`, `request-info`, refused decisions, `add-evidence`,
`decide RE_EVALUATE`), compliance attempt 2 `PASS`, submission, `OPENED` and a restart.

## Start FastAPI

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

* Health check: <http://127.0.0.1:8000/health>; Swagger UI: <http://127.0.0.1:8000/docs>
* The first start creates `trusteq.db` in the current folder (git-ignored) and its tables.
* Public contract: provider manifest, products, applications (create / read / handoff / submit)
  and the customer confirmation. The provider callback and HTML pages are intentionally not listed.

### Drive the flow by hand

The agent and the customer are different actors with different credentials. The agent's first
token comes from the trusted dev bootstrap CLI (there is no HTTP endpoint for it). The provider
callback needs a secret; generate one for this session only and never commit it. Run every
command from the project folder (server and CLI then share `trusteq.db`).

```powershell
# 1) Start the server with a throw-away callback secret (PowerShell window A)
$env:TRUSTEQ_MOCK_PROVIDER_CALLBACK_SECRET = [guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")
.\.venv\Scripts\python.exe -m uvicorn app.main:app

# 2) OPERATOR (window B): grant agent-a ONE draft for demo-customer-1 (token printed once)
$boot = (.\.venv\Scripts\python.exe -m scripts.dev_auth bootstrap --agent agent-a --customer demo-customer-1 |
         Select-String '^\s+tqa_').Line.Trim()

# 3) AGENT (window B): discover (public), draft (bootstrap token), ask for approval (onboarding token)
$base = "http://127.0.0.1:8000"
Invoke-RestMethod "$base/v1/providers/fixture-bank/manifest"
Invoke-RestMethod "$base/v1/products"
$hdr = @{ Authorization = "Bearer $boot"; "Idempotency-Key" = [guid]::NewGuid().ToString() }
$body = '{"provider_id":"fixture-bank","product_id":"depot-basic","channel_id":"direct","customer_ref":"demo-customer-1"}'
$app = Invoke-RestMethod -Method Post "$base/v1/applications" -Headers $hdr -ContentType application/json -Body $body
$onb = @{ Authorization = "Bearer $($app.authorization.onboarding.access_token)" }   # handoff + submit
$sts = @{ Authorization = "Bearer $($app.authorization.status.access_token)" }       # read-only
$handoff = Invoke-RestMethod -Method Post "$base/v1/applications/$($app.application_id)/handoff" -Headers $onb
$handoff.confirmation_url      # give this link to the CUSTOMER
```

3. **Customer:** open `$handoff.confirmation_url` in a browser. The page first asks for the
   *simulated* sign-in - enter `demo-customer-1` - then shows provider, product, channel,
   application reference, the exact action and the disclosure version. Press **Confirm and
   authorize**. (Any other customer id is refused; the form is CSRF-protected.)

```powershell
# 4) AGENT again: resume after the customer, submit, read the status
Invoke-RestMethod "$base/v1/applications/$($app.application_id)" -Headers $sts          # IN_PROGRESS
Invoke-RestMethod -Method Post "$base/v1/applications/$($app.application_id)/submit" -Headers $onb
```

5. **The MOCK provider** confirms. Because only a signed message may open the account, use the
   helper that plays the provider (same secret as step 1, in window B):

```powershell
@'
import os, sys, httpx
from app.integrations.provider_callback import build_signed_confirmation
app_id = sys.argv[1]
cb = build_signed_confirmation(os.environ["TRUSTEQ_MOCK_PROVIDER_CALLBACK_SECRET"].encode(),
                               application_id=app_id, provider_ref=f"MOCK-DEPOT-{app_id}")
r = httpx.post("http://127.0.0.1:8000/internal/depot-provider/confirmations", content=cb.body, headers=cb.headers)
print(r.status_code, r.json())
'@ | .\.venv\Scripts\python.exe - $app.application_id
Invoke-RestMethod "$base/v1/applications/$($app.application_id)" -Headers $sts          # OPENED
Invoke-RestMethod -Method Post "$base/v1/applications/$($app.application_id)/submit" -Headers $onb  # 401: revoked
```

Human review (Phase 04, DEV only - there is no operator HTTP endpoint):
`python -m scripts.dev_operator --operator op-reviewer-1 list` / `show --case <id>` /
`request-info` / `add-evidence` / `decide --decision RE_EVALUATE|RESUME|REJECT|ACKNOWLEDGE`
(`REJECT` needs `op-supervisor-1`). A signed-in customer asks for a person with the
**"Mit einer Person sprechen"** button on the bank page (review page and confirmation page) or
with `POST /v1/customer/applications/{id}/request-human` - both need the customer's server-side
session and its CSRF token for that application; a self-asserted `X-Dev-Customer-Id` header or
an agent token is refused (details: [docs/onboarding-flow.md](docs/onboarding-flow.md)).

Lost a token, or continuing an application created before Phase 03?
`python -m scripts.dev_auth app-tokens --application-id <id>` re-issues its tokens (never wider);
`python -m scripts.dev_auth revoke --jti <jti>` revokes one server-side.

## Run the tests

```powershell
.\.venv\Scripts\python.exe -m pytest
```

Tests use in-memory or temporary SQLite databases, real HTTP-level clients, and never touch
`trusteq.db`. The suite includes runs of all three demos.

Targeted mutation testing of the security controls (each mutant breaks one control in a
temporary copy - your files are never modified - and the suite must notice):

```powershell
.\.venv\Scripts\python.exe -m scripts.security_mutations          # --list, --only M01,M17
```

## Run ruff

```powershell
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
```

Everything at once: `powershell -ExecutionPolicy Bypass -File scripts\check.ps1`

## Environment configuration

The app runs with **no `.env` file and no API key**. To override settings:

```powershell
Copy-Item .env.example .env   # .env is git-ignored; never commit it
```

All variables use the `TRUSTEQ_` prefix (see `.env.example`): app name, environment
(`development` / `test` / `production`), debug, database URL, log level/format, AI execution mode
(`scripted` / `llm`), optional LLM settings, the DEV/TEST switches (agent bootstrap issuance,
simulated customer sign-in, operator console), lifetimes (handoff, onboarding grant, bootstrap grant, status
grant, customer session), the MOCK provider callback secret, and allowed CORS origins. Invalid
combinations fail at startup: e.g. `debug=true`, an explicit dev switch, or the mock callback
secret in **production**; or `llm` mode without `TRUSTEQ_ENABLE_LLM=true`. Phase 03 removed
`TRUSTEQ_DEV_ACTOR_AUTH_ENABLED` (the `X-Dev-Actor-Id` header no longer exists).

## Implemented (2026 prototype) vs mocked vs 2030 target

| Area | Implemented and tested (2026 prototype) | Mocked / simulated | 2030 target (not built) |
|---|---|---|---|
| Provider discovery | bank register as trust root; pinned digest; signed, versioned, expiring manifests; identity/domain/endpoint/capability/channel binding; no rollback; validity re-checked at use; fail closed | synthetic provider, partners and register; HMAC with a shared DEV key; no network fetch | provider-signed manifests (JWS / eIDAS seal) over mTLS from the verified domain, key rotation and revocation |
| Product information + disclosures | versioned terms, fees, conditions, risk, sources with digests, terms digest; receipts bound to exact versions and the action digest; stale approvals refused at confirm and submit | all content synthetic | provider feeds with legal sign-off, wallet-based signed consent, re-consent flow |
| Partner attribution | registry-based, customer-confirmed, append-only; the agent cannot name a recipient | synthetic partners; no commission | contractual partner registry, commission settlement |
| Human review | durable cases, generic customer acknowledgement, fixed operator decisions, re-evaluation as a new deterministic attempt, supervisor-only rejection, audit | synthetic operators and evidence references; MOCK compliance scenario switch | case management with SSO, RBAC, four-eyes, SLAs, evidence store |
| Agent | scripted, REST only, no key | - | optional LLM behind an explicit flag (Phase 05) |
| Agent authorization | **real and tested, controlled DEV implementation**: opaque bearer tokens stored as SHA-256 hashes; issuer, audience, expiry, `jti`, revocation, per-operation scopes, resource binding; single-use bootstrap; revoked at terminal states | first grant from a trusted DEV CLI | OAuth 2.0 / FAPI 2.0, sender-constrained tokens (DPoP/mTLS), RFC 9396 / RFC 8693 |
| Customer authentication + intervention | server-side session (hashed secret, HttpOnly/SameSite=Strict/Secure-on-HTTPS cookies for the pages and the customer API, expiry, rotation) + per-target CSRF tokens; "Mit einer Person sprechen" and `request-human` require both | **DEV simulation** of the sign-in itself; `X-Dev-Customer-Id` only on the Phase 02 JSON confirmation ceremony | bank-controlled strong authentication (online banking / eID / EUDI wallet), a customer case view, phone/chat channels |
| KYC / compliance / contract / depot provider | the deterministic workflow around them; the signed callback is the only path to `OPENED` | **MOCK** results, `mock=true`, `MOCK-` references | contracted regulated providers, partner integration with mTLS |
| Policy, state machine, audit, idempotency, persistence, recovery | real and tested (schema v4 with resumable upgrades) | - | HA database, durable workflow engine, DORA controls, security review |

## Current security limitations

* The **customer sign-in and the operator identities are simulations**: anyone who completes the
  dev sign-in can claim any customer (what is real is the server-side session, its expiry,
  rotation, cookie flags and CSRF binding around it); the Phase 02 JSON confirmation ceremony
  still accepts the dev customer header; anyone who can run the operator console on the server
  can act as a synthetic operator. All of these are disabled in production.
* There is no customer-facing case view (`GET /v1/customer/cases/{id}` from ADR section 09 is
  deferred: it must never reveal internal review reasons).
* Manifest signatures use a **shared HMAC key** (a symmetric stand-in): whoever holds the key could
  also sign. The key, provider, partners and register are synthetic and fixture-local.
* Agent tokens are **bearer tokens** (no DPoP/mTLS binding, no client authentication, no refresh
  flow, no rate limiting).
* A change of terms after the customer's consent blocks submission (fail closed); there is no
  re-consent flow - a new application is needed.
* Mock results are not legal, AML or identity evidence. Not legal advice and no regulatory
  compliance or certification is claimed (no BaFin, MiFID II or WpHG claim); counsel review is
  required before any real deployment.

## Next implementation phase

**Phase 05** - optional LLM behind an explicit flag, 20-case evaluation (not started).

## Git

Branch `main`, tracking a **private** GitHub repository (`origin`). Phase 04 work stays local
and uncommitted until it is reviewed. `.gitignore` excludes `.env`, `.venv/`, caches, SQLite files
(including WAL files), build output, temp files and `.claude/`.
