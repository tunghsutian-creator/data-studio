from __future__ import annotations

import sqlite3


VERSION = 1
NAME = "initial catalog"


def apply(connection: sqlite3.Connection, _context) -> None:
    statements = (
        """
        CREATE TABLE IF NOT EXISTS app_metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS datasets (
            id TEXT PRIMARY KEY,
            source_kind TEXT NOT NULL CHECK(source_kind IN ('reference','inbox')),
            group_key TEXT NOT NULL,
            source_root TEXT NOT NULL,
            canonical_name TEXT,
            workstream TEXT NOT NULL DEFAULT 'UNASSIGNED',
            material_state TEXT NOT NULL DEFAULT 'UNKNOWN',
            modality TEXT NOT NULL DEFAULT 'UNKNOWN',
            data_level TEXT NOT NULL DEFAULT 'UNKNOWN',
            sample_code TEXT,
            experiment_date TEXT,
            confidence REAL NOT NULL DEFAULT 0.0,
            classification_method TEXT NOT NULL DEFAULT 'unknown',
            conflict INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'REVIEW',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(source_kind, group_key)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS assets (
            id TEXT PRIMARY KEY,
            dataset_id TEXT NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
            original_path TEXT NOT NULL UNIQUE,
            managed_path TEXT,
            original_name TEXT NOT NULL,
            extension TEXT NOT NULL DEFAULT '',
            size_bytes INTEGER NOT NULL DEFAULT 0,
            modified_at TEXT,
            mtime_ns INTEGER,
            sha256 TEXT,
            role TEXT NOT NULL DEFAULT 'PRIMARY',
            mime_type TEXT,
            hash_state TEXT NOT NULL DEFAULT 'UNVERIFIED',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS classification_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dataset_id TEXT NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
            predicted_label TEXT,
            proposed_metadata_json TEXT NOT NULL DEFAULT '{}',
            confidence REAL NOT NULL DEFAULT 0.0,
            method TEXT NOT NULL DEFAULT 'unknown',
            evidence_json TEXT NOT NULL DEFAULT '[]',
            conflict INTEGER NOT NULL DEFAULT 0,
            resolution TEXT NOT NULL DEFAULT 'PREDICTED',
            note TEXT,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS ingest_jobs (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            source TEXT,
            status TEXT NOT NULL,
            progress_current INTEGER NOT NULL DEFAULT 0,
            progress_total INTEGER NOT NULL DEFAULT 0,
            message TEXT,
            error TEXT,
            created_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS operation_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dataset_id TEXT REFERENCES datasets(id) ON DELETE CASCADE,
            job_id TEXT REFERENCES ingest_jobs(id) ON DELETE SET NULL,
            action TEXT NOT NULL,
            detail_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS rules (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            pattern TEXT NOT NULL,
            label TEXT NOT NULL,
            priority INTEGER NOT NULL DEFAULT 100,
            enabled INTEGER NOT NULL DEFAULT 1,
            version INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_datasets_status ON datasets(status)",
        "CREATE INDEX IF NOT EXISTS idx_datasets_modality ON datasets(modality)",
        "CREATE INDEX IF NOT EXISTS idx_datasets_updated ON datasets(updated_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_assets_dataset ON assets(dataset_id)",
        "CREATE INDEX IF NOT EXISTS idx_assets_extension ON assets(extension)",
        "CREATE INDEX IF NOT EXISTS idx_decisions_dataset ON classification_decisions(dataset_id, id DESC)",
        "CREATE INDEX IF NOT EXISTS idx_operations_dataset ON operation_log(dataset_id, id DESC)",
    )
    for statement in statements:
        connection.execute(statement)


class _Migration:
    version = VERSION
    name = NAME
    apply = staticmethod(apply)


migration = _Migration()
