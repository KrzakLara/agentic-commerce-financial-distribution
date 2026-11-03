# Database (Phase 01 - Phase 04)

SQLite via SQLAlchemy 2.x. Default URL `sqlite:///./trusteq.db` (git-ignored). The schema is
created at application startup (`init_database`) and stamped with `PRAGMA user_version`
(currently **3**); older schemas are upgraded in place, a database with a *newer* version is
refused. SQLite runs in WAL mode with foreign keys on and a 15 s busy timeout.

## Layers (three separate model kinds)

| Layer | Where | Purpose |
|---|---|---|
| Domain models | `app/domain/` | Immutable Pydantic contracts used by services |
| ORM models | `app/repositories/orm.py` | Table definitions; **never leave the persistence layer** |
| API schemas | `app/api/schemas/` | Public request/response shapes (coarse state only) |

Repositories (`app/repositories/sqlalchemy_repositories.py`) convert ORM rows to domain models.
Services depend on the interfaces in `app/repositories/interfaces.py`. A `UnitOfWork` owns one
transaction, so a business operation and its audit event commit together or not at all.

## Tables

```text
applications 1 ──── 0..1 idempotency_records
     │
     └──── 0..n audit_events
```

### `applications`
| Column | Type | Notes |
|---|---|---|
| `application_id` | TEXT PK | `app-<uuid hex>`, unguessable |
| `provider_id`, `product_id`, `channel_id` | TEXT | Agent proposals (opaque ids) |
| `customer_ref` | TEXT | Opaque reference; never copied to audit or API responses |
| `agent_actor_id` | TEXT, indexed | Owner; every read is scoped to it |
| `status` | TEXT, indexed | CHECK in the 13 `ApplicationStatus` values |
| `resume_status` | TEXT NULL | Step interrupted by `ACTION_REQUIRED` / `MANUAL_REVIEW` |
| `version` | INTEGER | Optimistic lock, `>= 1`, +1 per status change |
| `created_at`, `updated_at` | DATETIME (UTC) | Timezone-aware in Python, stored as naive UTC |

Constraints: status CHECKs; `(status IN parked states) = (resume_status IS NOT NULL)`; `version >= 1`.

### `idempotency_records`
| Column | Notes |
|---|---|
| `id` | INTEGER PK |
| `actor_id`, `operation`, `idempotency_key` | **UNIQUE together** (`uq_idempotency_actor_operation_key`) |
| `request_hash` | SHA-256 of the canonical request body |
| `application_id` | FK -> `applications` (RESTRICT), **UNIQUE** (one record per application) |
| `created_at` | |

The UNIQUE constraint is what makes concurrent duplicate creation impossible; the loser's whole
transaction (application + record + audit) is rolled back and it replays the winner's result.

### `audit_events` (append-only)
| Column | Notes |
|---|---|
| `seq` | INTEGER PK autoincrement: stable total order |
| `event_id` | UNIQUE |
| `occurred_at`, `correlation_id` (indexed), `event_type` (indexed) | |
| `actor_type`, `actor_id` | agent / system / customer / operator / provider |
| `application_id` | FK -> `applications` (RESTRICT), nullable; composite index `(application_id, seq)` |
| `outcome` | success / denied / failure |
| `details` | JSON; only non-sensitive keys (never idempotency keys, customer data or compliance detail) |

## Tables added in Phase 02 (schema version 2)

```text
applications 1 ──── 1 mandates
     ├──── 0..n handoffs (at most ONE active) ──── 0..1 consent_receipts
     ├──── 0..1 disclosure_receipts
     ├──── 0..3 check_results (KYC, COMPLIANCE, CONTRACT)
     └──── 0..1 depot_submissions
```

| Table | Purpose | Key constraints |
|---|---|---|
| `mandates` | per-application onboarding mandate (scopes, `jti`, expiry, `revoked_at`) - **rebuilt in Phase 03, see below** | UNIQUE `application_id` (v2); a revoked mandate stays revoked (trigger) |
| `handoffs` | one-time customer challenge: `nonce_hash` (never the nonce), `action` JSON, `action_digest`, `expires_at`, `customer_id`/`consumed_at`/`revoked_at` | **partial UNIQUE index: one active handoff per application**; CHECK `customer_id` set iff consumed |
| `consent_receipts` | which customer approved which action digest, via which handoff | UNIQUE `handoff_id`; append-only (triggers) |
| `disclosure_receipts` | acknowledgement of a document id + version + digest | append-only (triggers) |
| `check_results` | internal evidence: KYC / COMPLIANCE / CONTRACT outcome, `mock` flag, `MOCK-` reference | UNIQUE `(application_id, check_type)`; CHECK on type/outcome; append-only; never exposed to the agent |
| `depot_submissions` | hand-over to the provider, `provider_ref`, trusted `confirmation_id`/`confirmed_at` | PK `application_id`; UNIQUE `confirmation_id`; frozen once confirmed (trigger) |

`SCHEMA_VERSION` is now `2`. Version 2 only adds tables, so an existing version-1 database is
upgraded in place by `init_database`; a database from a newer version is refused.

Additional audit events (Phase 02): `handoff.created`, `handoff.confirmed`, `handoff.rejected`
(denied), `handoff.request_denied` (denied), `contract.confirmation_recorded`,
`application.submitted`, `application.submit_denied` (denied; lists the missing prerequisites),
`provider.confirmation_accepted`, `provider.confirmation_rejected` (denied). Transition events at a
terminal state also carry `mandate_revoked: true`. Audit details never contain the nonce, the
callback secret, the idempotency key or the customer reference.

## Phase 03: grants, tokens, customer sessions (schema version 3)

```text
applications 1 ──── 0..1 mandates(kind=bootstrap)   (bound when it created this application)
     ├──────────── 1    mandates(kind=onboarding) ──── 1..n agent_access_tokens
     └──────────── 1    mandates(kind=status)     ──── 1..n agent_access_tokens
mandates(kind=bootstrap, application_id NULL)  = an unused bootstrap grant
customer_sessions                               (independent of agents and applications)
```

### `mandates` (rebuilt)
| Column | Notes |
|---|---|
| `mandate_id` | PK `mdt-<hex>` |
| `jti` | UNIQUE grant identifier (`grt-<hex>`; `jti-<hex>` for Phase 02 grants) |
| `kind` | `bootstrap` / `onboarding` / `status` (CHECK) |
| `application_id` | FK, **NULL only for an unused bootstrap grant** (CHECK `kind = 'bootstrap' OR application_id IS NOT NULL`) |
| `agent_actor_id`, `customer_id` | who may act, for whom |
| `issuer`, `audience` | `trusteq-dev-authorization`, `trusteq-onboarding-api` |
| `scopes` | JSON list; exactly the kind's scopes |
| `issued_at`, `expires_at`, `revoked_at` | CHECK `expires_at > issued_at` |

UNIQUE `(application_id, kind)`: one grant of each kind per application (NULLs never collide, so
any number of unused bootstrap grants can exist).

### `agent_access_tokens`
| Column | Notes |
|---|---|
| `jti` | PK `jti-<hex>`; what audit events and revocation refer to |
| `token_hash` | UNIQUE SHA-256 hex of the bearer token (CHECK length 64). **The token itself is never stored.** |
| `mandate_id` | FK -> `mandates` (RESTRICT), indexed |
| `issuer`, `audience`, `scopes` | checked on every request; scopes must be a subset of the grant's |
| `issued_at`, `expires_at`, `revoked_at` | CHECK `expires_at > issued_at`; expiry equals the grant's |

### `customer_sessions`
| Column | Notes |
|---|---|
| `session_id` | PK `ses-<hex>`; a public id for audit events, not a credential |
| `secret_hash` | UNIQUE SHA-256 hex of the cookie secret (CHECK length 64) |
| `customer_id` | indexed |
| `created_at`, `expires_at`, `revoked_at` | expiry and revocation are decided here, not by the cookie |

### Phase 03 triggers

| Trigger | Rule |
|---|---|
| `trg_mandates_claims_are_immutable` | `jti`, `kind`, agent, customer, issuer, audience, scopes, issued/expiry can never change |
| `trg_mandates_binding_is_final` | only an unused, unrevoked bootstrap grant can get an `application_id`, once |
| `trg_mandates_revocation_is_final` | (Phase 02, recreated on the new table) a revoked grant stays revoked |
| `trg_agent_access_tokens_claims_are_immutable` / `_revocation_is_final` | same for tokens |
| `trg_customer_sessions_claims_are_immutable` / `_revocation_is_final` | same for sessions |

### Upgrade 2 -> 3 (`init_database`)

SQLite cannot drop a `NOT NULL` or a column-level `UNIQUE` in place, so `mandates` is rebuilt:
(1) drop its old trigger and rename it to `_mandates_v2`; (2) `create_all` creates the new
`mandates`, `agent_access_tokens` and `customer_sessions` with all constraints and triggers;
(3) copy every row as `kind = 'onboarding'`, `issuer = trusteq-dev-authorization`, verify that
none is missing (else `SchemaMigrationError`, nothing dropped), drop `_mandates_v2`; (4) stamp
version 3. Every step checks the current state first, so an interrupted upgrade resumes on the next
start (`tests/test_schema_migration.py` builds a real v2 database from
`tests/fixtures/schema_v2.sql` and covers this). Phase 02 data is preserved; a Phase 02 *in-flight*
application has no tokens - an operator re-issues them with
`python -m scripts.dev_auth app-tokens --application-id <id>`.

Additional audit events (Phase 03): `authorization.bootstrap_issued` (operator),
`authorization.denied` (reason: `token_revoked`, `grant_revoked`, `token_expired`,
`grant_expired`, `wrong_issuer`, `wrong_audience`, `not_yet_valid`, `scope_exceeds_grant`,
`insufficient_scope`), `authorization.tokens_reissued`, `authorization.token_revoked`,
`application.create_denied` (`bootstrap_already_used`, `bootstrap_inactive`,
`customer_not_covered`), `application.access_denied` (`not_owner`, `outside_authorization`),
`customer.session_started`, `handoff.rejected` (new reasons `agent_credentials_presented`,
`csrf_token_invalid`), `workflow.resumed` (success / failure). Terminal transitions also carry
`tokens_revoked` and `handoffs_revoked`. Never stored in audit details: tokens, token hashes,
session secrets, the nonce, customer references.

## Triggers (defence in depth, enforced even for raw SQL)

| Trigger | Rule |
|---|---|
| `trg_applications_insert_only_draft` | A new application must start as `DRAFT` |
| `trg_applications_opened_only_from_submitted` | `OPENED` only from `SUBMITTED_TO_DEPOT_PROVIDER` |
| `trg_applications_terminal_frozen` | `OPENED`, `REJECTED`, `CANCELLED`, `FAILED` can never change |
| `trg_audit_events_no_update` / `_no_delete` | Audit rows can never be updated or deleted |

These do not replace the Python state machine (`docs/state-machine.md`); they make sure a bug or
a manual SQL statement cannot silently break the most important invariants.

## Audit events written in Phase 01

| `event_type` | Outcome | When |
|---|---|---|
| `application.created` | success | A draft is created |
| `application.transition` | success | A state change is applied |
| `application.transition_rejected` | denied | Invalid edge or wrong authority (own transaction, so it survives the rollback) |
| `application.idempotency_conflict` | denied | Same key, different body |
| `application.access_denied` | denied | An actor read another actor's application |

Audit data is internal. The API never returns it, and agent responses contain only the coarse
state.

## Migrations

There is no migration tool. `SCHEMA_VERSION` + `PRAGMA user_version` mark the schema; version 2
only added tables, version 3 contains one hand-written, resumable rebuild step (above). A
production deployment would move to Alembic (or numbered, reviewed SQL steps) and a real database
with online migrations.

## Phase 04: review cases, check attempts, attribution, disclosure binding (schema version 4)

| Table / change | Purpose and guarantees |
|---|---|
| `check_results` (rebuilt) | one row per **attempt** (`attempt >= 1`, UNIQUE (application, check, attempt)); still append-only; the latest attempt decides |
| `review_cases` (new) | reason, interrupted step, check type + attempt, customer request category, requested information, evidence (category + synthetic reference), resolution; **partial unique index: at most one active (not CLOSED) case per application**; CLOSED <=> resolution; triggers: a closed case never changes, the facts (why/where opened) are immutable, never deleted |
| `channel_attributions` (new) | the verified attribution recorded with the consent: provider, channel, kind, partner (only for `PARTNER_BANK`), registry version (manifest digest), consent receipt (FK, unique); append-only; one per application |
| `disclosure_receipts` (+5 nullable columns) | `provider_id`, `product_id`, `product_version`, `terms_digest`, `action_digest`; NULL on receipts written before Phase 04 |
| `applications` (new trigger) | `trg_applications_identity_is_final`: provider, product, channel, customer and owner can never change after creation |

### Upgrade 3 -> 4 (`init_database`)

Same resumable pattern as 2 -> 3: `check_results` is set aside (`_check_results_v3`, old triggers
dropped), `create_all` creates the new table with its constraints and triggers, every row is copied
as attempt 1, a missing-row check runs (`SchemaMigrationError`, nothing dropped), then the old
table is dropped. The receipt columns are added with `ALTER TABLE ... ADD COLUMN`; the identity
trigger is created for the existing table. An interrupted upgrade resumes on the next start;
running it twice is harmless. Tested against `tests/fixtures/schema_v3.sql` (the exact Phase 03
DDL) in `tests/test_schema_migration_v4.py`, including a Phase 03 approval that still submits.
