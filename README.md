# TRUSTEQ - Secure Agentic Depot Onboarding

A modular-monolith FastAPI project for the TRUSTEQ GmbH AI Engineering case study.

**Current status: Phase 02 - a complete SIMULATED end-to-end onboarding runs through the real
REST API, with a separate customer confirmation, deterministic policy checks, MOCK banking
providers, persistent audit events and a provider-confirmed `OPENED` state.** Phases 00 and 01
(foundation, persistence, state machine) are complete.

> **Everything here is a prototype with synthetic data.** KYC, compliance/AML, contract signing
> and the depot provider are **MOCK**. The customer sign-in is a **development-only simulation**,
> not identity verification. Nothing opens a real account or creates a legally valid contract.

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
AGENT (untrusted)            BANK                              CUSTOMER           MOCK PROVIDER
discover provider/product -> verified fixture metadata
create application        -> DRAFT (+ onboarding mandate)
request handoff           -> one-time challenge + action digest
        -------- link (a pointer, not an authorization) -------->  sign in (SIMULATED),
                                                                    review, confirm exact action
                          <- verified: challenge, identity, digest, catalog, mandate
                          MOCK KYC -> MOCK compliance -> MOCK contract recorded
submit (after consent)    -> policy gate on stored evidence -> hand-over
                                                                    ------ signed "opened" ------>
                          <------------------------------------------------ confirmation
                          OPENED + mandate revoked + audit
read status               -> OPENED (reported by the backend, never claimed by the agent)
```

Details, transition authorities, handoff properties and limitations:
[docs/onboarding-flow.md](docs/onboarding-flow.md).

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
│   ├── api/               # routers, middleware, errors, schemas, dev auth contexts, HTML pages
│   │   └── routes/        # health, catalog, applications, customer, internal (provider callback)
│   ├── core/              # config, logging, exceptions
│   ├── domain/            # typed contracts, state-machine rules, visibility mapping
│   ├── services/          # ApplicationService, OnboardingService, policy gate
│   ├── repositories/      # ORM models, repositories, unit of work (SQLAlchemy/SQLite)
│   ├── integrations/      # ports, MOCK adapters, verified catalog fixture, REST agent gateway
│   └── agents/            # ScriptedCustomerAgent (untrusted zone T0b)
├── tests/                 # pytest suite
├── docs/                  # architecture, flow, trust boundaries, API, state machine, database
├── scripts/               # setup.ps1, check.ps1, demo_happy.py
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

## Run the complete demo (recommended first step)

```powershell
.\.venv\Scripts\python.exe -m scripts.demo_happy
```

It creates a throw-away database in your temp folder, starts the real server, and plays four
separate actors (agent, customer, bank verification, MOCK provider) through discovery, draft,
handoff, customer confirmation, MOCK checks, submit, the provider's signed confirmation and
`OPENED` - plus things the agent tries that must fail - then restarts the server to prove the
result persisted. It prints a timeline with real application ids and states, never the one-time
challenge or any secret, deletes its temp data, and exits `0` only if every check holds.
Options: `--keep` (keep the temp database), `--no-restart`. It never touches `trusteq.db`.

## Start FastAPI

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

* Health check: <http://127.0.0.1:8000/health>; Swagger UI: <http://127.0.0.1:8000/docs>
* The first start creates `trusteq.db` in the current folder (git-ignored) and its tables.
* Public contract: provider manifest, products, applications (create / read / handoff / submit)
  and the customer confirmation. The provider callback and HTML pages are intentionally not listed.

### Drive the flow by hand

The agent and the customer are different actors with different (dev) identities. The provider
callback needs a secret; generate one for this session only and never commit it.

```powershell
# 1) Start the server with a throw-away callback secret (PowerShell window A)
$env:TRUSTEQ_MOCK_PROVIDER_CALLBACK_SECRET = [guid]::NewGuid().ToString("N") + [guid]::NewGuid().ToString("N")
.\.venv\Scripts\python.exe -m uvicorn app.main:app

# 2) AGENT (PowerShell window B): discover, draft, ask for approval
$base = "http://127.0.0.1:8000"
$agent = @{ "X-Dev-Actor-Id" = "agent-a" }
Invoke-RestMethod "$base/v1/providers/fixture-bank/manifest"
Invoke-RestMethod "$base/v1/products"
$hdr = $agent + @{ "Idempotency-Key" = [guid]::NewGuid().ToString() }
$body = '{"provider_id":"fixture-bank","product_id":"depot-basic","channel_id":"direct","customer_ref":"demo-customer-1"}'
$app = Invoke-RestMethod -Method Post "$base/v1/applications" -Headers $hdr -ContentType application/json -Body $body
$handoff = Invoke-RestMethod -Method Post "$base/v1/applications/$($app.application_id)/handoff" -Headers $agent
$handoff.confirmation_url      # give this link to the CUSTOMER
```

3. **Customer:** open `$handoff.confirmation_url` in a browser. The page first asks for the
   *simulated* sign-in - enter `demo-customer-1` - then shows provider, product, channel,
   application reference, the exact action and the disclosure version. Press **Confirm and
   authorize**. (Any other customer id is refused.)

```powershell
# 4) AGENT again: resume after the customer, submit, read the status
Invoke-RestMethod "$base/v1/applications/$($app.application_id)" -Headers $agent        # IN_PROGRESS
Invoke-RestMethod -Method Post "$base/v1/applications/$($app.application_id)/submit" -Headers $agent
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
Invoke-RestMethod "$base/v1/applications/$($app.application_id)" -Headers $agent        # OPENED
```

## Run the tests

```powershell
.\.venv\Scripts\python.exe -m pytest
```

Tests use in-memory or temporary SQLite databases, real HTTP-level clients, and never touch
`trusteq.db`. The suite includes the end-to-end demo run.

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
(`scripted` / `llm`), optional LLM settings, the DEV/TEST agent and customer switches, handoff and
mandate lifetimes, the MOCK provider callback secret, and allowed CORS origins. Invalid
combinations fail at startup: e.g. `debug=true`, an explicit dev-identity switch, or the mock
callback secret in **production**; or `llm` mode without `TRUSTEQ_ENABLE_LLM=true`.

## Implemented vs mocked vs production target

| Area | Phase 02 prototype | Production target (not built) |
|---|---|---|
| Provider discovery | verified local fixture, digests pinned in code, allowlist, fail closed | signed manifests, TLS/domain binding, expiry, trust registry |
| Agent | scripted, REST only, no key | optional LLM behind an explicit flag (Phase 05) |
| Agent identity | DEV header `X-Dev-Actor-Id` | scoped, short-lived tokens (FAPI 2.0, RAR, token exchange) |
| Customer authentication | **DEV simulation** (header / signed cookie) | bank-controlled strong authentication (eID / EUDI wallet) |
| Handoff | real one-time, expiring, action-digest- and identity-bound challenge | same, with wallet-backed approval |
| KYC / compliance / contract | **MOCK**, fixed results, `mock=true`, `MOCK-` references | contracted regulated providers |
| Depot provider | **MOCK**, HMAC-signed callback is the only path to `OPENED` | partner integration, mutual TLS |
| Policy, state machine, audit, idempotency, persistence | real and tested | HA database, DORA controls, security review |
| Mandate | issued per application, checked before actions, revoked at end | verified token claims per request (Phase 03) |

## Current security limitations

* Agent and customer identification are **not authentication**; whoever can set the dev header
  or use the dev sign-in can claim any identity. Both are disabled in production (fail closed).
* The provider-callback secret is a shared mock credential; the endpoint is disabled by default
  and refused in production.
* The CSRF defence on form posts is an `Origin` check plus SameSite=Strict, not a token.
* A crash between the customer's confirmation and the end of the checks leaves the application
  at its last committed state (submit stays blocked); resuming is a trusted operation not yet
  triggered automatically.
* A non-passing check parks the application in `MANUAL_REVIEW`; there is no operator workflow yet.
* Mock results are not legal, AML or identity evidence. Not legal advice; counsel review is
  required before any real deployment (see the ADR).

## Next implementation phases

1. **Phase 03** - scoped mandates replacing the dev actor, request-bound handoff hardening,
   `request-human`, security tests (substitution / IDOR / replay / expiry).
2. **Phase 04** - verified discovery hardening, channel attribution, `denied` and `manual_review`
   demo outcomes.
3. **Phase 05** - optional LLM behind an explicit flag, 20-case evaluation.

## Git

This folder is not a Git repository yet. Nothing is committed or pushed. `.gitignore` already
excludes `.env`, `.venv/`, caches, SQLite files (including WAL files), build output, temp files and
`.claude/`.
