from __future__ import annotations

import sqlite3


VERSION = 4
NAME = "durable local AI task state"


def apply(connection: sqlite3.Connection, context) -> None:
    del context
    script = """
        CREATE TABLE model_registry (
            id TEXT PRIMARY KEY
                CHECK(length(id)=64 AND id NOT GLOB '*[^0-9a-f]*'),
            provider TEXT NOT NULL CHECK(length(trim(provider)) BETWEEN 1 AND 100),
            profile_id TEXT NOT NULL CHECK(length(trim(profile_id)) BETWEEN 1 AND 200),
            model_id TEXT NOT NULL CHECK(length(trim(model_id)) BETWEEN 1 AND 500),
            quantization TEXT NOT NULL CHECK(length(trim(quantization)) BETWEEN 1 AND 100),
            device TEXT NOT NULL CHECK(length(trim(device)) BETWEEN 1 AND 200),
            model_revision TEXT NOT NULL CHECK(length(trim(model_revision)) BETWEEN 1 AND 200),
            runtime_release TEXT NOT NULL CHECK(length(trim(runtime_release)) BETWEEN 1 AND 200),
            runtime_commit TEXT NOT NULL CHECK(length(trim(runtime_commit)) BETWEEN 1 AND 200),
            prompt_version TEXT NOT NULL CHECK(length(trim(prompt_version)) BETWEEN 1 AND 200),
            taxonomy_version TEXT NOT NULL CHECK(length(trim(taxonomy_version)) BETWEEN 1 AND 200),
            output_schema_version TEXT NOT NULL CHECK(length(trim(output_schema_version)) BETWEEN 1 AND 200),
            config_json TEXT NOT NULL CHECK(json_valid(config_json)),
            enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
            created_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL
        );

        CREATE TABLE ai_tasks (
            id TEXT PRIMARY KEY,
            dataset_id TEXT NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
            input_fingerprint TEXT NOT NULL
                CHECK(length(input_fingerprint)=64 AND input_fingerprint NOT GLOB '*[^0-9a-f]*'),
            reason TEXT NOT NULL CHECK(length(trim(reason)) BETWEEN 1 AND 200),
            status TEXT NOT NULL DEFAULT 'QUEUED'
                CHECK(status IN ('QUEUED','RUNNING','RETRY_WAIT','COMPLETED','ABSTAINED','FAILED','CANCELLED')),
            priority INTEGER NOT NULL DEFAULT 100 CHECK(priority BETWEEN -1000 AND 1000),
            attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
            max_attempts INTEGER NOT NULL DEFAULT 2 CHECK(max_attempts BETWEEN 1 AND 10),
            next_attempt_at TEXT,
            lease_owner TEXT CHECK(lease_owner IS NULL OR length(trim(lease_owner)) BETWEEN 1 AND 200),
            lease_expires_at TEXT,
            last_error_code TEXT CHECK(last_error_code IS NULL OR length(last_error_code) <= 100),
            last_error_detail TEXT CHECK(last_error_detail IS NULL OR length(last_error_detail) <= 1000),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            CHECK(attempt_count <= max_attempts),
            CHECK(
                (status='RUNNING' AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)
                OR
                (status<>'RUNNING' AND lease_owner IS NULL AND lease_expires_at IS NULL)
            ),
            CHECK(
                (status='RETRY_WAIT' AND next_attempt_at IS NOT NULL)
                OR
                (status<>'RETRY_WAIT' AND next_attempt_at IS NULL)
            ),
            CHECK(
                (status IN ('COMPLETED','ABSTAINED','FAILED','CANCELLED') AND finished_at IS NOT NULL)
                OR
                (status NOT IN ('COMPLETED','ABSTAINED','FAILED','CANCELLED') AND finished_at IS NULL)
            )
        );

        CREATE TABLE ai_runs (
            id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL REFERENCES ai_tasks(id) ON DELETE CASCADE,
            model_registry_id TEXT NOT NULL REFERENCES model_registry(id) ON DELETE RESTRICT,
            attempt_number INTEGER NOT NULL CHECK(attempt_number >= 1),
            status TEXT NOT NULL DEFAULT 'RUNNING'
                CHECK(status IN ('RUNNING','SUCCEEDED','ABSTAINED','FAILED')),
            request_fingerprint TEXT NOT NULL
                CHECK(length(request_fingerprint)=64 AND request_fingerprint NOT GLOB '*[^0-9a-f]*'),
            response_sha256 TEXT
                CHECK(response_sha256 IS NULL OR (length(response_sha256)=64 AND response_sha256 NOT GLOB '*[^0-9a-f]*')),
            classification_json TEXT CHECK(classification_json IS NULL OR json_valid(classification_json)),
            latency_ms INTEGER CHECK(latency_ms IS NULL OR latency_ms >= 0),
            error_code TEXT CHECK(error_code IS NULL OR length(error_code) <= 100),
            error_detail TEXT CHECK(error_detail IS NULL OR length(error_detail) <= 1000),
            retryable INTEGER NOT NULL DEFAULT 0 CHECK(retryable IN (0,1)),
            started_at TEXT NOT NULL,
            finished_at TEXT,
            UNIQUE(task_id,attempt_number),
            CHECK(
                (status='RUNNING' AND finished_at IS NULL)
                OR
                (status<>'RUNNING' AND finished_at IS NOT NULL)
            ),
            CHECK(
                status NOT IN ('SUCCEEDED','ABSTAINED')
                OR (classification_json IS NOT NULL AND response_sha256 IS NOT NULL)
            )
        );

        CREATE UNIQUE INDEX idx_ai_tasks_one_active_input
            ON ai_tasks(dataset_id,input_fingerprint)
            WHERE status IN ('QUEUED','RUNNING','RETRY_WAIT');
        CREATE INDEX idx_ai_tasks_claim
            ON ai_tasks(status,next_attempt_at,priority DESC,created_at,id);
        CREATE INDEX idx_ai_tasks_lease
            ON ai_tasks(status,lease_expires_at)
            WHERE status='RUNNING';
        CREATE INDEX idx_ai_tasks_dataset_created
            ON ai_tasks(dataset_id,created_at DESC);
        CREATE INDEX idx_ai_runs_task_started
            ON ai_runs(task_id,started_at DESC);
        CREATE INDEX idx_ai_runs_model_started
            ON ai_runs(model_registry_id,started_at DESC);
        """
    # sqlite3.executescript() commits an open transaction before running. The
    # migration runner deliberately owns the transaction, so execute each DDL
    # statement through the existing transaction instead.
    for statement in script.split(";"):
        if statement.strip():
            connection.execute(statement)
    connection.execute(
        """
        CREATE TRIGGER model_registry_identity_immutable
        BEFORE UPDATE OF
            provider,profile_id,model_id,quantization,device,model_revision,
            runtime_release,runtime_commit,prompt_version,taxonomy_version,
            output_schema_version,config_json
        ON model_registry
        BEGIN
            SELECT RAISE(ABORT,'model registry identity is immutable');
        END
        """
    )


class _Migration:
    version = VERSION
    name = NAME
    apply = staticmethod(apply)


migration = _Migration()
