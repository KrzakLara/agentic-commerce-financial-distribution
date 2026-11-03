-- Phase 02 database schema (schema version 2), captured from commit ef0e16d.
-- Used by tests/test_schema_migration.py to prove the v2 -> v3 upgrade keeps data.

CREATE TABLE applications (
	application_id VARCHAR(64) NOT NULL, 
	provider_id VARCHAR(64) NOT NULL, 
	product_id VARCHAR(64) NOT NULL, 
	channel_id VARCHAR(64) NOT NULL, 
	customer_ref VARCHAR(64) NOT NULL, 
	agent_actor_id VARCHAR(64) NOT NULL, 
	status VARCHAR(40) NOT NULL, 
	resume_status VARCHAR(40), 
	version INTEGER NOT NULL, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (application_id), 
	CONSTRAINT ck_applications_status CHECK (status IN ('ACTION_REQUIRED', 'AUTHORIZED', 'AWAITING_AUTHORIZATION', 'CANCELLED', 'COMPLIANCE_PENDING', 'CONTRACT_PENDING', 'DRAFT', 'FAILED', 'KYC_PENDING', 'MANUAL_REVIEW', 'OPENED', 'REJECTED', 'SUBMITTED_TO_DEPOT_PROVIDER')), 
	CONSTRAINT ck_applications_resume_status CHECK (resume_status IS NULL OR resume_status IN ('ACTION_REQUIRED', 'AUTHORIZED', 'AWAITING_AUTHORIZATION', 'CANCELLED', 'COMPLIANCE_PENDING', 'CONTRACT_PENDING', 'DRAFT', 'FAILED', 'KYC_PENDING', 'MANUAL_REVIEW', 'OPENED', 'REJECTED', 'SUBMITTED_TO_DEPOT_PROVIDER')), 
	CONSTRAINT ck_applications_resume_only_when_parked CHECK ((status IN ('ACTION_REQUIRED', 'MANUAL_REVIEW')) = (resume_status IS NOT NULL)), 
	CONSTRAINT ck_applications_version CHECK (version >= 1)
);

CREATE TABLE idempotency_records (
	id INTEGER NOT NULL, 
	actor_id VARCHAR(64) NOT NULL, 
	operation VARCHAR(100) NOT NULL, 
	idempotency_key VARCHAR(64) NOT NULL, 
	request_hash VARCHAR(64) NOT NULL, 
	application_id VARCHAR(64) NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_idempotency_actor_operation_key UNIQUE (actor_id, operation, idempotency_key), 
	CONSTRAINT uq_idempotency_application UNIQUE (application_id), 
	FOREIGN KEY(application_id) REFERENCES applications (application_id) ON DELETE RESTRICT
);

CREATE TABLE audit_events (
	seq INTEGER NOT NULL, 
	event_id VARCHAR(64) NOT NULL, 
	occurred_at DATETIME NOT NULL, 
	correlation_id VARCHAR(64) NOT NULL, 
	event_type VARCHAR(100) NOT NULL, 
	actor_type VARCHAR(20) NOT NULL, 
	actor_id VARCHAR(64), 
	application_id VARCHAR(64), 
	outcome VARCHAR(20) NOT NULL, 
	details JSON NOT NULL, 
	PRIMARY KEY (seq), 
	UNIQUE (event_id), 
	FOREIGN KEY(application_id) REFERENCES applications (application_id) ON DELETE RESTRICT
);

CREATE TABLE mandates (
	mandate_id VARCHAR(64) NOT NULL, 
	jti VARCHAR(64) NOT NULL, 
	application_id VARCHAR(64) NOT NULL, 
	agent_actor_id VARCHAR(64) NOT NULL, 
	customer_id VARCHAR(64) NOT NULL, 
	audience VARCHAR(64) NOT NULL, 
	scopes JSON NOT NULL, 
	issued_at DATETIME NOT NULL, 
	expires_at DATETIME NOT NULL, 
	revoked_at DATETIME, 
	PRIMARY KEY (mandate_id), 
	UNIQUE (jti), 
	UNIQUE (application_id), 
	FOREIGN KEY(application_id) REFERENCES applications (application_id) ON DELETE RESTRICT
);

CREATE TABLE handoffs (
	handoff_id VARCHAR(64) NOT NULL, 
	application_id VARCHAR(64) NOT NULL, 
	nonce_hash VARCHAR(64) NOT NULL, 
	action JSON NOT NULL, 
	action_digest VARCHAR(64) NOT NULL, 
	created_at DATETIME NOT NULL, 
	expires_at DATETIME NOT NULL, 
	customer_id VARCHAR(64), 
	consumed_at DATETIME, 
	revoked_at DATETIME, 
	PRIMARY KEY (handoff_id), 
	CONSTRAINT ck_handoffs_bound_when_consumed CHECK ((consumed_at IS NULL) = (customer_id IS NULL)), 
	FOREIGN KEY(application_id) REFERENCES applications (application_id) ON DELETE RESTRICT
);

CREATE TABLE disclosure_receipts (
	receipt_id VARCHAR(64) NOT NULL, 
	application_id VARCHAR(64) NOT NULL, 
	customer_id VARCHAR(64) NOT NULL, 
	document_id VARCHAR(64) NOT NULL, 
	document_version VARCHAR(200) NOT NULL, 
	document_digest VARCHAR(64) NOT NULL, 
	acknowledged_at DATETIME NOT NULL, 
	PRIMARY KEY (receipt_id), 
	FOREIGN KEY(application_id) REFERENCES applications (application_id) ON DELETE RESTRICT
);

CREATE TABLE check_results (
	id INTEGER NOT NULL, 
	application_id VARCHAR(64) NOT NULL, 
	check_type VARCHAR(20) NOT NULL, 
	outcome VARCHAR(20) NOT NULL, 
	reference VARCHAR(200) NOT NULL, 
	mock BOOLEAN NOT NULL, 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_check_results_application_type UNIQUE (application_id, check_type), 
	CONSTRAINT ck_check_results_type CHECK (check_type IN ('COMPLIANCE', 'CONTRACT', 'KYC')), 
	CONSTRAINT ck_check_results_outcome CHECK (outcome IN ('FAIL', 'PASS', 'REVIEW_REQUIRED')), 
	FOREIGN KEY(application_id) REFERENCES applications (application_id) ON DELETE RESTRICT
);

CREATE TABLE depot_submissions (
	application_id VARCHAR(64) NOT NULL, 
	provider_ref VARCHAR(64) NOT NULL, 
	submitted_at DATETIME NOT NULL, 
	confirmation_id VARCHAR(64), 
	confirmed_at DATETIME, 
	PRIMARY KEY (application_id), 
	FOREIGN KEY(application_id) REFERENCES applications (application_id) ON DELETE RESTRICT, 
	UNIQUE (confirmation_id)
);

CREATE TABLE consent_receipts (
	receipt_id VARCHAR(64) NOT NULL, 
	application_id VARCHAR(64) NOT NULL, 
	customer_id VARCHAR(64) NOT NULL, 
	handoff_id VARCHAR(64) NOT NULL, 
	action_digest VARCHAR(64) NOT NULL, 
	confirmed_at DATETIME NOT NULL, 
	PRIMARY KEY (receipt_id), 
	FOREIGN KEY(application_id) REFERENCES applications (application_id) ON DELETE RESTRICT, 
	UNIQUE (handoff_id), 
	FOREIGN KEY(handoff_id) REFERENCES handoffs (handoff_id) ON DELETE RESTRICT
);

CREATE INDEX ix_applications_status ON applications (status);

CREATE INDEX ix_applications_agent_actor_id ON applications (agent_actor_id);

CREATE INDEX ix_audit_events_event_type ON audit_events (event_type);

CREATE INDEX ix_audit_events_application_seq ON audit_events (application_id, seq);

CREATE INDEX ix_audit_events_correlation_id ON audit_events (correlation_id);

CREATE UNIQUE INDEX uq_handoffs_one_active_per_application ON handoffs (application_id) WHERE consumed_at IS NULL AND revoked_at IS NULL;

CREATE INDEX ix_handoffs_application_id ON handoffs (application_id);

CREATE INDEX ix_disclosure_receipts_application_id ON disclosure_receipts (application_id);

CREATE INDEX ix_consent_receipts_application_id ON consent_receipts (application_id);

CREATE TRIGGER trg_applications_insert_only_draft
    BEFORE INSERT ON applications
    WHEN NEW.status != 'DRAFT'
    BEGIN SELECT RAISE(ABORT, 'new applications must start as DRAFT'); END;

CREATE TRIGGER trg_applications_opened_only_from_submitted
    BEFORE UPDATE OF status ON applications
    WHEN NEW.status = 'OPENED' AND OLD.status != 'SUBMITTED_TO_DEPOT_PROVIDER'
    BEGIN SELECT RAISE(ABORT, 'OPENED is only reachable from SUBMITTED_TO_DEPOT_PROVIDER'); END;

CREATE TRIGGER trg_applications_terminal_frozen
    BEFORE UPDATE OF status ON applications
    WHEN OLD.status IN ('CANCELLED', 'FAILED', 'OPENED', 'REJECTED') AND NEW.status != OLD.status
    BEGIN SELECT RAISE(ABORT, 'terminal states cannot change'); END;

CREATE TRIGGER trg_audit_events_no_update
    BEFORE UPDATE ON audit_events
    BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END;

CREATE TRIGGER trg_audit_events_no_delete
    BEFORE DELETE ON audit_events
    BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END;

CREATE TRIGGER trg_mandates_revocation_is_final
    BEFORE UPDATE OF revoked_at ON mandates
    WHEN OLD.revoked_at IS NOT NULL
         AND (NEW.revoked_at IS NULL OR NEW.revoked_at != OLD.revoked_at)
    BEGIN SELECT RAISE(ABORT, 'a revoked mandate stays revoked'); END;

CREATE TRIGGER trg_disclosure_receipts_no_update
            BEFORE UPDATE ON disclosure_receipts
            BEGIN SELECT RAISE(ABORT, 'disclosure_receipts is append-only'); END;

CREATE TRIGGER trg_disclosure_receipts_no_delete
            BEFORE DELETE ON disclosure_receipts
            BEGIN SELECT RAISE(ABORT, 'disclosure_receipts is append-only'); END;

CREATE TRIGGER trg_check_results_no_update
            BEFORE UPDATE ON check_results
            BEGIN SELECT RAISE(ABORT, 'check_results is append-only'); END;

CREATE TRIGGER trg_check_results_no_delete
            BEFORE DELETE ON check_results
            BEGIN SELECT RAISE(ABORT, 'check_results is append-only'); END;

CREATE TRIGGER trg_depot_submissions_confirm_once
    BEFORE UPDATE OF confirmed_at, confirmation_id, provider_ref ON depot_submissions
    WHEN OLD.confirmed_at IS NOT NULL
    BEGIN SELECT RAISE(ABORT, 'a confirmed depot submission cannot change'); END;

CREATE TRIGGER trg_consent_receipts_no_update
            BEFORE UPDATE ON consent_receipts
            BEGIN SELECT RAISE(ABORT, 'consent_receipts is append-only'); END;

CREATE TRIGGER trg_consent_receipts_no_delete
            BEFORE DELETE ON consent_receipts
            BEGIN SELECT RAISE(ABORT, 'consent_receipts is append-only'); END;

PRAGMA user_version = 2;
