from __future__ import annotations

import sqlite3


VERSION = 5
NAME = "collections and immutable export selections"


def apply(connection: sqlite3.Connection, context) -> None:
    del context
    connection.execute(
        "INSERT OR IGNORE INTO app_metadata(key,value) VALUES('catalog_revision','1')"
    )
    script = """
        CREATE TABLE collections (
            id TEXT PRIMARY KEY,
            library_id TEXT NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
            name TEXT NOT NULL COLLATE NOCASE
                CHECK(length(trim(name)) BETWEEN 1 AND 200),
            purpose TEXT CHECK(purpose IS NULL OR length(purpose) <= 2000),
            revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(library_id,name)
        );

        CREATE TABLE collection_items (
            collection_id TEXT NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
            asset_id TEXT NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
            position INTEGER NOT NULL CHECK(position >= 0),
            added_at TEXT NOT NULL,
            PRIMARY KEY(collection_id,asset_id),
            UNIQUE(collection_id,position)
        );

        CREATE TABLE selection_snapshots (
            id TEXT PRIMARY KEY,
            library_id TEXT NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
            token_sha256 TEXT NOT NULL UNIQUE
                CHECK(length(token_sha256)=64 AND token_sha256 NOT GLOB '*[^0-9a-f]*'),
            selection_kind TEXT NOT NULL
                CHECK(selection_kind IN ('ASSET_IDS','DATASET_IDS','FILTER')),
            normalized_query_json TEXT NOT NULL CHECK(json_valid(normalized_query_json)),
            catalog_revision INTEGER NOT NULL CHECK(catalog_revision >= 1),
            status TEXT NOT NULL CHECK(status IN ('READY','BLOCKED','CONSUMED','EXPIRED')),
            selection_sha256 TEXT NOT NULL
                CHECK(length(selection_sha256)=64 AND selection_sha256 NOT GLOB '*[^0-9a-f]*'),
            asset_count INTEGER NOT NULL CHECK(asset_count >= 1),
            total_bytes INTEGER NOT NULL CHECK(total_bytes >= 0),
            issues_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(issues_json)),
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            consumed_at TEXT
        );

        CREATE TABLE selection_snapshot_items (
            selection_id TEXT NOT NULL REFERENCES selection_snapshots(id) ON DELETE CASCADE,
            asset_id TEXT NOT NULL,
            dataset_id TEXT NOT NULL,
            position INTEGER NOT NULL CHECK(position >= 0),
            dataset_revision INTEGER NOT NULL CHECK(dataset_revision >= 1),
            original_name TEXT NOT NULL CHECK(length(original_name) BETWEEN 1 AND 1000),
            source_kind TEXT NOT NULL CHECK(source_kind IN ('reference','inbox')),
            selected_root_key TEXT,
            selected_relpath TEXT,
            selected_sha256 TEXT
                CHECK(selected_sha256 IS NULL OR (length(selected_sha256)=64 AND selected_sha256 NOT GLOB '*[^0-9a-f]*')),
            size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
            issue_codes_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(issue_codes_json)),
            duplicate_of TEXT,
            PRIMARY KEY(selection_id,asset_id),
            UNIQUE(selection_id,position),
            FOREIGN KEY(selection_id,duplicate_of)
                REFERENCES selection_snapshot_items(selection_id,asset_id)
                DEFERRABLE INITIALLY DEFERRED
        );

        CREATE TABLE exports (
            id TEXT PRIMARY KEY,
            library_id TEXT NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
            collection_id TEXT REFERENCES collections(id) ON DELETE SET NULL,
            selection_id TEXT NOT NULL UNIQUE REFERENCES selection_snapshots(id) ON DELETE RESTRICT,
            name TEXT NOT NULL CHECK(length(trim(name)) BETWEEN 1 AND 200),
            purpose TEXT CHECK(purpose IS NULL OR length(purpose) <= 2000),
            status TEXT NOT NULL DEFAULT 'QUEUED'
                CHECK(status IN ('QUEUED','RUNNING','COMPLETED','FAILED','CANCELLED')),
            export_mode TEXT NOT NULL
                CHECK(export_mode IN ('FOLDER','ZIP64','MANIFEST_ONLY')),
            duplicate_policy TEXT NOT NULL DEFAULT 'PRESERVE'
                CHECK(duplicate_policy IN ('PRESERVE','DEDUPLICATE')),
            archive_root_key TEXT CHECK(archive_root_key IS NULL OR archive_root_key='exports'),
            archive_relpath TEXT,
            manifest_sha256 TEXT
                CHECK(manifest_sha256 IS NULL OR (length(manifest_sha256)=64 AND manifest_sha256 NOT GLOB '*[^0-9a-f]*')),
            file_count INTEGER NOT NULL DEFAULT 0 CHECK(file_count >= 0),
            total_bytes INTEGER NOT NULL DEFAULT 0 CHECK(total_bytes >= 0),
            created_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            error_code TEXT CHECK(error_code IS NULL OR length(error_code) <= 100),
            error_detail TEXT CHECK(error_detail IS NULL OR length(error_detail) <= 1000),
            CHECK(
                (status IN ('COMPLETED','FAILED','CANCELLED') AND finished_at IS NOT NULL)
                OR (status IN ('QUEUED','RUNNING') AND finished_at IS NULL)
            )
        );

        CREATE TABLE export_items (
            export_id TEXT NOT NULL REFERENCES exports(id) ON DELETE CASCADE,
            asset_id TEXT NOT NULL,
            dataset_id TEXT NOT NULL,
            position INTEGER NOT NULL CHECK(position >= 0),
            original_name TEXT NOT NULL CHECK(length(original_name) BETWEEN 1 AND 1000),
            source_kind TEXT NOT NULL CHECK(source_kind IN ('reference','inbox')),
            source_sha256 TEXT NOT NULL
                CHECK(length(source_sha256)=64 AND source_sha256 NOT GLOB '*[^0-9a-f]*'),
            size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
            exported_relpath TEXT,
            exported_sha256 TEXT
                CHECK(exported_sha256 IS NULL OR (length(exported_sha256)=64 AND exported_sha256 NOT GLOB '*[^0-9a-f]*')),
            duplicate_of TEXT,
            PRIMARY KEY(export_id,asset_id),
            UNIQUE(export_id,position),
            FOREIGN KEY(export_id,duplicate_of)
                REFERENCES export_items(export_id,asset_id)
                DEFERRABLE INITIALLY DEFERRED
        );

        CREATE INDEX idx_collections_library_updated
            ON collections(library_id,updated_at DESC,id);
        CREATE INDEX idx_collection_items_asset
            ON collection_items(asset_id,collection_id);
        CREATE INDEX idx_selection_snapshots_expiry
            ON selection_snapshots(status,expires_at);
        CREATE INDEX idx_selection_items_asset
            ON selection_snapshot_items(asset_id,selection_id);
        CREATE INDEX idx_exports_library_created
            ON exports(library_id,created_at DESC,id);
        CREATE INDEX idx_exports_status_created
            ON exports(status,created_at,id);
    """
    # The migration runner owns the transaction. executescript() would commit
    # it before running, so ordinary DDL is executed statement by statement.
    for statement in script.split(";"):
        if statement.strip():
            connection.execute(statement)

    for table in ("datasets", "assets"):
        for action in ("insert", "update", "delete"):
            connection.execute(
                f"""
                CREATE TRIGGER catalog_revision_{table}_{action}
                AFTER {action.upper()} ON {table}
                BEGIN
                    UPDATE app_metadata
                    SET value=CAST(CAST(value AS INTEGER)+1 AS TEXT)
                    WHERE key='catalog_revision';
                END
                """
            )


class _Migration:
    version = VERSION
    name = NAME
    apply = staticmethod(apply)


migration = _Migration()
