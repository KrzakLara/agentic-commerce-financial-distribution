-- Final-integration database schema (schema version 5), captured from commit 20b4a30: the
-- schema that commit's own init_database creates, in creation order.
-- Used by tests/test_schema_migration_v6.py to prove the v5 -> v6 upgrade on a real v5 database.

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

CREATE INDEX ix_applications_agent_actor_id ON applications (agent_actor_id);

CREATE INDEX ix_applications_status ON applications (status);

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

CREATE TRIGGER trg_applications_identity_is_final
    BEFORE UPDATE OF application_id, provider_id, product_id, channel_id, customer_ref,
                     agent_actor_id, created_at ON applications
    BEGIN SELECT RAISE(ABORT, 'provider, product, channel and parties of an application are final');
    END;

CREATE TABLE customer_sessions (
	session_id VARCHAR(64) NOT NULL, 
	secret_hash VARCHAR(64) NOT NULL, 
	customer_id VARCHAR(64) NOT NULL, 
	created_at DATETIME NOT NULL, 
	expires_at DATETIME NOT NULL, 
	revoked_at DATETIME, 
	PRIMARY KEY (session_id), 
	CONSTRAINT ck_customer_sessions_hash CHECK (length(secret_hash) = 64), 
	CONSTRAINT ck_customer_sessions_expiry CHECK (expires_at > created_at), 
	UNIQUE (secret_hash)
);

CREATE INDEX ix_customer_sessions_customer_id ON customer_sessions (customer_id);

CREATE TRIGGER trg_customer_sessions_revocation_is_final
        BEFORE UPDATE OF revoked_at ON customer_sessions
        WHEN OLD.revoked_at IS NOT NULL
             AND (NEW.revoked_at IS NULL OR NEW.revoked_at != OLD.revoked_at)
        BEGIN SELECT RAISE(ABORT, 'a revoked customer session stays revoked'); END;

CREATE TRIGGER trg_customer_sessions_claims_are_immutable
        BEFORE UPDATE OF session_id, secret_hash, customer_id, created_at, expires_at ON customer_sessions
        BEGIN SELECT RAISE(ABORT, 'customer session claims are immutable'); END;

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

CREATE INDEX ix_audit_events_event_type ON audit_events (event_type);

CREATE INDEX ix_audit_events_correlation_id ON audit_events (correlation_id);

CREATE INDEX ix_audit_events_application_seq ON audit_events (application_id, seq);

CREATE TRIGGER trg_audit_events_no_update
    BEFORE UPDATE ON audit_events
    BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END;

CREATE TRIGGER trg_audit_events_no_delete
    BEFORE DELETE ON audit_events
    BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END;

CREATE TABLE mandates (
	mandate_id VARCHAR(64) NOT NULL, 
	jti VARCHAR(64) NOT NULL, 
	kind VARCHAR(20) NOT NULL, 
	application_id VARCHAR(64), 
	agent_actor_id VARCHAR(64) NOT NULL, 
	customer_id VARCHAR(64) NOT NULL, 
	issuer VARCHAR(64) NOT NULL, 
	audience VARCHAR(64) NOT NULL, 
	scopes JSON NOT NULL, 
	issued_at DATETIME NOT NULL, 
	expires_at DATETIME NOT NULL, 
	revoked_at DATETIME, 
	PRIMARY KEY (mandate_id), 
	CONSTRAINT uq_mandates_application_kind UNIQUE (application_id, kind), 
	CONSTRAINT ck_mandates_kind CHECK (kind IN ('bootstrap', 'onboarding', 'status')), 
	CONSTRAINT ck_mandates_bound CHECK (kind = 'bootstrap' OR application_id IS NOT NULL), 
	CONSTRAINT ck_mandates_expiry CHECK (expires_at > issued_at), 
	UNIQUE (jti), 
	FOREIGN KEY(application_id) REFERENCES applications (application_id) ON DELETE RESTRICT
);

CREATE TRIGGER trg_mandates_revocation_is_final
    BEFORE UPDATE OF revoked_at ON mandates
    WHEN OLD.revoked_at IS NOT NULL
         AND (NEW.revoked_at IS NULL OR NEW.revoked_at != OLD.revoked_at)
    BEGIN SELECT RAISE(ABORT, 'a revoked mandate stays revoked'); END;

CREATE TRIGGER trg_mandates_claims_are_immutable
    BEFORE UPDATE OF mandate_id, jti, kind, agent_actor_id, customer_id, issuer, audience,
                     scopes, issued_at, expires_at ON mandates
    BEGIN SELECT RAISE(ABORT, 'mandate claims are immutable'); END;

CREATE TRIGGER trg_mandates_binding_is_final
    BEFORE UPDATE OF application_id ON mandates
    WHEN OLD.application_id IS NOT NULL OR OLD.kind != 'bootstrap' OR OLD.revoked_at IS NOT NULL
         OR NEW.application_id IS NULL
    BEGIN SELECT RAISE(ABORT, 'only an unused bootstrap mandate can be bound, once'); END;

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

CREATE INDEX ix_handoffs_application_id ON handoffs (application_id);

CREATE UNIQUE INDEX uq_handoffs_one_active_per_application ON handoffs (application_id) WHERE consumed_at IS NULL AND revoked_at IS NULL;

CREATE TABLE disclosure_receipts (
	receipt_id VARCHAR(64) NOT NULL, 
	application_id VARCHAR(64) NOT NULL, 
	customer_id VARCHAR(64) NOT NULL, 
	document_id VARCHAR(64) NOT NULL, 
	document_version VARCHAR(200) NOT NULL, 
	document_digest VARCHAR(64) NOT NULL, 
	acknowledged_at DATETIME NOT NULL, 
	provider_id VARCHAR(64), 
	product_id VARCHAR(64), 
	product_version VARCHAR(200), 
	terms_digest VARCHAR(64), 
	action_digest VARCHAR(64), 
	PRIMARY KEY (receipt_id), 
	FOREIGN KEY(application_id) REFERENCES applications (application_id) ON DELETE RESTRICT
);

CREATE INDEX ix_disclosure_receipts_application_id ON disclosure_receipts (application_id);

CREATE TRIGGER trg_disclosure_receipts_no_update
            BEFORE UPDATE ON disclosure_receipts
            BEGIN SELECT RAISE(ABORT, 'disclosure_receipts is append-only'); END;

CREATE TRIGGER trg_disclosure_receipts_no_delete
            BEFORE DELETE ON disclosure_receipts
            BEGIN SELECT RAISE(ABORT, 'disclosure_receipts is append-only'); END;

CREATE TABLE check_results (
	id INTEGER NOT NULL, 
	application_id VARCHAR(64) NOT NULL, 
	check_type VARCHAR(20) NOT NULL, 
	outcome VARCHAR(20) NOT NULL, 
	reference VARCHAR(200) NOT NULL, 
	mock BOOLEAN NOT NULL, 
	created_at DATETIME NOT NULL, 
	attempt INTEGER NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_check_results_attempt UNIQUE (application_id, check_type, attempt), 
	CONSTRAINT ck_check_results_type CHECK (check_type IN ('COMPLIANCE', 'CONTRACT', 'KYC')), 
	CONSTRAINT ck_check_results_outcome CHECK (outcome IN ('FAIL', 'PASS', 'REVIEW_REQUIRED')), 
	CONSTRAINT ck_check_results_attempt CHECK (attempt >= 1), 
	FOREIGN KEY(application_id) REFERENCES applications (application_id) ON DELETE RESTRICT
);

CREATE TRIGGER trg_check_results_no_update
            BEFORE UPDATE ON check_results
            BEGIN SELECT RAISE(ABORT, 'check_results is append-only'); END;

CREATE TRIGGER trg_check_results_no_delete
            BEFORE DELETE ON check_results
            BEGIN SELECT RAISE(ABORT, 'check_results is append-only'); END;

CREATE TABLE review_cases (
	case_id VARCHAR(64) NOT NULL, 
	application_id VARCHAR(64) NOT NULL, 
	reason VARCHAR(40) NOT NULL, 
	status VARCHAR(30) NOT NULL, 
	check_type VARCHAR(20), 
	check_attempt INTEGER, 
	request_category VARCHAR(40), 
	interrupted_step VARCHAR(40), 
	opened_by VARCHAR(20) NOT NULL, 
	requested_information VARCHAR(40), 
	evidence JSON NOT NULL, 
	resolution VARCHAR(20), 
	resolved_by VARCHAR(64), 
	resolved_at DATETIME, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	version INTEGER NOT NULL, 
	PRIMARY KEY (case_id), 
	CONSTRAINT ck_review_cases_status CHECK (status IN ('AWAITING_INFORMATION', 'CLOSED', 'IN_REVIEW', 'OPEN')), 
	CONSTRAINT ck_review_cases_reason CHECK (reason IN ('CHECK_FAILED', 'COMPLIANCE_REVIEW_REQUIRED', 'CUSTOMER_REQUESTED_HUMAN', 'KYC_REVIEW_REQUIRED')), 
	CONSTRAINT ck_review_cases_resolution CHECK ((status = 'CLOSED') = (resolution IS NOT NULL)), 
	CONSTRAINT ck_review_cases_version CHECK (version >= 1), 
	FOREIGN KEY(application_id) REFERENCES applications (application_id) ON DELETE RESTRICT
);

CREATE INDEX ix_review_cases_application_id ON review_cases (application_id);

CREATE UNIQUE INDEX uq_review_cases_one_active_per_application ON review_cases (application_id) WHERE status != 'CLOSED';

CREATE TRIGGER trg_review_cases_closed_is_final
    BEFORE UPDATE ON review_cases
    WHEN OLD.status = 'CLOSED'
    BEGIN SELECT RAISE(ABORT, 'a closed review case never changes'); END;

CREATE TRIGGER trg_review_cases_facts_are_immutable
    BEFORE UPDATE OF case_id, application_id, reason, check_type, check_attempt,
                     request_category, interrupted_step, opened_by, created_at ON review_cases
    BEGIN SELECT RAISE(ABORT, 'why and where a review case was opened is immutable'); END;

CREATE TRIGGER trg_review_cases_no_delete
    BEFORE DELETE ON review_cases
    BEGIN SELECT RAISE(ABORT, 'review cases are never deleted'); END;

CREATE TABLE tax_information (
	application_id VARCHAR(64) NOT NULL, 
	customer_id VARCHAR(64) NOT NULL, 
	source VARCHAR(40) NOT NULL, 
	tax_residency_country VARCHAR(2) NOT NULL, 
	us_person_declared BOOLEAN NOT NULL, 
	exemption_order_amount_eur INTEGER, 
	church_tax_objection BOOLEAN, 
	field_statuses JSON NOT NULL, 
	external_verification VARCHAR(20) NOT NULL, 
	recorded_at DATETIME NOT NULL, 
	PRIMARY KEY (application_id), 
	FOREIGN KEY(application_id) REFERENCES applications (application_id) ON DELETE RESTRICT
);

CREATE TABLE appropriateness_assessments (
	application_id VARCHAR(64) NOT NULL, 
	customer_id VARCHAR(64) NOT NULL, 
	product_level VARCHAR(20) NOT NULL, 
	outcome VARCHAR(40) NOT NULL, 
	reasons JSON NOT NULL, 
	knows_capital_loss BOOLEAN, 
	knows_no_guarantee BOOLEAN, 
	experience VARCHAR(20), 
	rule_set_version VARCHAR(40) NOT NULL, 
	acknowledged_at DATETIME, 
	recorded_at DATETIME NOT NULL, 
	PRIMARY KEY (application_id), 
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

CREATE TRIGGER trg_depot_submissions_confirm_once
    BEFORE UPDATE OF confirmed_at, confirmation_id, provider_ref ON depot_submissions
    WHEN OLD.confirmed_at IS NOT NULL
    BEGIN SELECT RAISE(ABORT, 'a confirmed depot submission cannot change'); END;

CREATE TABLE agent_access_tokens (
	jti VARCHAR(64) NOT NULL, 
	token_hash VARCHAR(64) NOT NULL, 
	mandate_id VARCHAR(64) NOT NULL, 
	issuer VARCHAR(64) NOT NULL, 
	audience VARCHAR(64) NOT NULL, 
	scopes JSON NOT NULL, 
	issued_at DATETIME NOT NULL, 
	expires_at DATETIME NOT NULL, 
	revoked_at DATETIME, 
	PRIMARY KEY (jti), 
	CONSTRAINT ck_agent_access_tokens_hash CHECK (length(token_hash) = 64), 
	CONSTRAINT ck_agent_access_tokens_expiry CHECK (expires_at > issued_at), 
	UNIQUE (token_hash), 
	FOREIGN KEY(mandate_id) REFERENCES mandates (mandate_id) ON DELETE RESTRICT
);

CREATE INDEX ix_agent_access_tokens_mandate_id ON agent_access_tokens (mandate_id);

CREATE TRIGGER trg_agent_access_tokens_revocation_is_final
        BEFORE UPDATE OF revoked_at ON agent_access_tokens
        WHEN OLD.revoked_at IS NOT NULL
             AND (NEW.revoked_at IS NULL OR NEW.revoked_at != OLD.revoked_at)
        BEGIN SELECT RAISE(ABORT, 'a revoked access token stays revoked'); END;

CREATE TRIGGER trg_agent_access_tokens_claims_are_immutable
        BEFORE UPDATE OF jti, token_hash, mandate_id, issuer, audience, scopes, issued_at, expires_at ON agent_access_tokens
        BEGIN SELECT RAISE(ABORT, 'access token claims are immutable'); END;

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

CREATE INDEX ix_consent_receipts_application_id ON consent_receipts (application_id);

CREATE TRIGGER trg_consent_receipts_no_update
            BEFORE UPDATE ON consent_receipts
            BEGIN SELECT RAISE(ABORT, 'consent_receipts is append-only'); END;

CREATE TRIGGER trg_consent_receipts_no_delete
            BEFORE DELETE ON consent_receipts
            BEGIN SELECT RAISE(ABORT, 'consent_receipts is append-only'); END;

CREATE TABLE channel_attributions (
	application_id VARCHAR(64) NOT NULL, 
	provider_id VARCHAR(64) NOT NULL, 
	channel_id VARCHAR(64) NOT NULL, 
	channel_kind VARCHAR(20) NOT NULL, 
	partner_id VARCHAR(64), 
	registry_version VARCHAR(64) NOT NULL, 
	consent_receipt_id VARCHAR(64) NOT NULL, 
	recorded_at DATETIME NOT NULL, 
	PRIMARY KEY (application_id), 
	CONSTRAINT ck_attributions_kind CHECK (channel_kind IN ('DIRECT', 'PARTNER_BANK')), 
	CONSTRAINT ck_attributions_partner CHECK ((channel_kind = 'PARTNER_BANK') = (partner_id IS NOT NULL)), 
	FOREIGN KEY(application_id) REFERENCES applications (application_id) ON DELETE RESTRICT, 
	UNIQUE (consent_receipt_id), 
	FOREIGN KEY(consent_receipt_id) REFERENCES consent_receipts (receipt_id) ON DELETE RESTRICT
);

CREATE TRIGGER trg_channel_attributions_no_update
            BEFORE UPDATE ON channel_attributions
            BEGIN SELECT RAISE(ABORT, 'channel_attributions is append-only'); END;

CREATE TRIGGER trg_channel_attributions_no_delete
            BEFORE DELETE ON channel_attributions
            BEGIN SELECT RAISE(ABORT, 'channel_attributions is append-only'); END;

PRAGMA user_version = 5;
