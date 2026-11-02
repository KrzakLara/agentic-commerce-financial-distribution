# Database (Phase 01 + Phase 02)

SQLite via SQLAlchemy 2.x. Default URL `sqlite:///./trusteq.db` (git-ignored). The schema is
created at application startup (`init_database`) and stamped with `PRAGMA user_version = 1`;
a database with a *newer* version is refused. SQLite runs in WAL mode with foreign keys on and a
15 s busy timeout.

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
| `mandates` | per-application onboarding mandate (scopes, `jti`, expiry, `revoked_at`) | UNIQUE `application_id`; a revoked mandate stays revoked (trigger) |
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

There is no migration tool yet. `SCHEMA_VERSION` + `PRAGMA user_version` mark the schema so a
later phase can add Alembic (or numbered SQL steps) without ambiguity about what is deployed.
