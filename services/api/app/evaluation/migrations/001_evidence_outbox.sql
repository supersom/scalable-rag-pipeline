-- Evidence persistence: immutable safe runs, leased outbox, payload references, upload intents.
-- Applied by services.api.app.evaluation.migrate inside one transaction under an advisory lock.
-- Never edit after application: the runner verifies this file's SHA-256 checksum.

CREATE TABLE evaluation_runs (
    id bigserial PRIMARY KEY,
    export_version text NOT NULL,
    run_schema_version text NOT NULL,
    run_id uuid NOT NULL,
    export_hash char(64) NOT NULL CHECK (export_hash ~ '^[0-9a-f]{64}$'),
    envelope bytea NOT NULL CHECK (octet_length(envelope) > 0),
    redaction_policy_version text NOT NULL,
    completeness text NOT NULL CHECK (completeness IN ('complete', 'partial', 'metadata_only')),
    completed_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    CONSTRAINT evaluation_runs_identity_key UNIQUE (export_version, run_schema_version, run_id),
    CONSTRAINT evaluation_runs_expiry_order CHECK (expires_at > completed_at)
);

CREATE INDEX evaluation_runs_expires_at_idx ON evaluation_runs (expires_at, id);

CREATE TABLE evaluation_outbox (
    id bigserial PRIMARY KEY,
    run_pk bigint NOT NULL REFERENCES evaluation_runs (id) ON DELETE CASCADE,
    state text NOT NULL CHECK (state IN ('pending', 'leased', 'delivered', 'quarantined')),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    next_attempt_at timestamptz NOT NULL,
    lease_token uuid,
    lease_until timestamptz,
    acknowledgement_id text,
    error_class text CHECK (error_class ~ '^[A-Za-z][A-Za-z0-9_.]{0,63}$'),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    delivered_at timestamptz,
    CONSTRAINT evaluation_outbox_run_key UNIQUE (run_pk),
    CONSTRAINT evaluation_outbox_lease_pair CHECK ((lease_token IS NULL) = (lease_until IS NULL)),
    CONSTRAINT evaluation_outbox_leased_has_lease CHECK (state <> 'leased' OR lease_token IS NOT NULL),
    CONSTRAINT evaluation_outbox_delivered_ack CHECK (
        (state = 'delivered') = (acknowledgement_id IS NOT NULL AND delivered_at IS NOT NULL))
);

-- Claim index: only claimable rows, ordered exactly like the claim query.
CREATE INDEX evaluation_outbox_claim_idx ON evaluation_outbox (next_attempt_at, id)
    WHERE state IN ('pending', 'leased');
CREATE INDEX evaluation_outbox_quarantine_idx ON evaluation_outbox (updated_at)
    WHERE state = 'quarantined';

CREATE TABLE evaluation_payload_refs (
    id bigserial PRIMARY KEY,
    run_pk bigint NOT NULL REFERENCES evaluation_runs (id) ON DELETE CASCADE,
    namespace text NOT NULL,
    kind text NOT NULL,
    ordinal integer NOT NULL CHECK (ordinal > 0),
    item_ordinal integer CHECK (item_ordinal > 0),
    bucket text NOT NULL,
    object_key text NOT NULL,
    version_id text NOT NULL,
    tenant_partition text NOT NULL,
    byte_size integer NOT NULL CHECK (byte_size > 0),
    sha256 char(64) NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    policy_version text NOT NULL,
    created_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    CONSTRAINT evaluation_payload_refs_object_key UNIQUE (bucket, object_key, version_id),
    CONSTRAINT evaluation_payload_refs_in_namespace CHECK (starts_with(object_key, namespace)),
    CONSTRAINT evaluation_payload_refs_expiry_order CHECK (expires_at > created_at)
);

CREATE UNIQUE INDEX evaluation_payload_refs_slot_key
    ON evaluation_payload_refs (run_pk, kind, ordinal, COALESCE(item_ordinal, 0));
CREATE INDEX evaluation_payload_refs_namespace_idx ON evaluation_payload_refs (namespace, expires_at);

-- One row per generated object-key namespace. An open row is an upload lease; a cleanup_owned row
-- (with or without a run) is the exclusion marker that forbids later uploads/commits there.
CREATE TABLE evaluation_upload_intents (
    namespace text PRIMARY KEY,
    run_id uuid,
    state text NOT NULL CHECK (state IN ('open', 'closed', 'cleanup_owned')),
    lease_token uuid,
    expires_at timestamptz,
    created_at timestamptz NOT NULL,
    closed_at timestamptz,
    cleanup_token uuid,
    cleanup_lease_until timestamptz,
    cleanup_completed_at timestamptz,
    CONSTRAINT evaluation_upload_intents_upload_lease CHECK (
        state = 'cleanup_owned' OR (run_id IS NOT NULL AND lease_token IS NOT NULL AND expires_at IS NOT NULL)),
    CONSTRAINT evaluation_upload_intents_cleanup_lease CHECK (
        (cleanup_token IS NULL) = (cleanup_lease_until IS NULL)
        AND (state = 'cleanup_owned' OR cleanup_token IS NULL))
);

CREATE INDEX evaluation_upload_intents_expiry_idx ON evaluation_upload_intents (state, expires_at);
