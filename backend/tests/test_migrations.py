from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from backend.database import Database
from backend.migrations import MigrationError
from backend.migrations.runner import MigrationContext
from backend.migrations.versions import MIGRATIONS


def create_v2_catalog(path: Path, source: Path, *, stored_hash: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    context = MigrationContext()
    try:
        for migration in MIGRATIONS[:2]:
            connection.execute("BEGIN IMMEDIATE")
            migration.apply(connection, context)
            connection.execute(
                "INSERT OR REPLACE INTO app_metadata(key,value) VALUES('schema_version',?)",
                (str(migration.version),),
            )
            connection.commit()
        digest = stored_hash or hashlib.sha256(source.read_bytes()).hexdigest()
        connection.execute(
            """
            INSERT INTO datasets(
                id,source_kind,group_key,source_root,canonical_name,status,created_at,updated_at
            ) VALUES('dataset-1','reference','group-1',?,'LEGACY','INDEXED','2026-01-01','2026-01-01')
            """,
            (str(source.parent),),
        )
        connection.execute(
            """
            INSERT INTO assets(
                id,dataset_id,original_path,original_name,extension,size_bytes,sha256,
                source_sha256,hash_state,created_at,updated_at
            ) VALUES('asset-1','dataset-1',?,?,?,?,?,?,'SOURCE_HASHED','2026-01-01','2026-01-01')
            """,
            (str(source), source.name, source.suffix, source.stat().st_size, digest, digest),
        )
        connection.commit()
    finally:
        connection.close()


def test_v2_migration_backs_up_restores_and_backfills_portable_paths(tmp_path: Path) -> None:
    reference = tmp_path / "reference"
    reference.mkdir()
    source = reference / "sample.dat"
    source.write_bytes(b"migration fixture")
    catalog = tmp_path / "catalog" / "vault.sqlite3"
    create_v2_catalog(catalog, source)
    database = Database(
        catalog,
        root_mappings={
            "reference": reference,
            "inbox": tmp_path / "inbox",
            "vault": tmp_path / "vault",
            "quarantine": tmp_path / "quarantine",
            "exports": tmp_path / "exports",
        },
        backup_root=tmp_path / "backups",
        machine_profile="windows-test",
    )

    database.initialize()

    report = database.last_migration_report
    assert report is not None
    assert report.previous_version == 2
    assert report.current_version == 6
    assert report.applied_versions == (3, 4, 5, 6)
    assert report.backup is not None
    assert report.backup.source_integrity == "ok"
    assert report.backup.backup_integrity == "ok"
    assert report.backup.restore_integrity == "ok"
    assert report.backup.path.is_file()
    with sqlite3.connect(report.backup.path) as backup:
        assert backup.execute("SELECT value FROM app_metadata WHERE key='schema_version'").fetchone()[0] == "2"
        assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    detail = database.get_dataset("dataset-1")
    assert detail is not None
    assert detail["library_id"] == database.library_id()
    assert detail["revision"] == 1
    assert detail["assets"][0]["original_root_key"] == "reference"
    assert detail["assets"][0]["original_relpath"] == "sample.dat"
    assert detail["assets"][0]["path_state"] == "VALID"
    with database.connect() as connection:
        seeded = connection.execute(
            "SELECT aggregate_type,aggregate_id,aggregate_revision,event_type FROM integration_outbox"
        ).fetchone()
    assert tuple(seeded) == ("DATASET", "dataset-1", 1, "UPSERT")

    stable_library_id = database.library_id()
    database.initialize()
    assert database.last_migration_report.applied_versions == ()
    assert database.last_migration_report.backup is None
    assert database.library_id() == stable_library_id


def test_failed_hash_verification_rolls_back_migration_and_keeps_backup(tmp_path: Path) -> None:
    reference = tmp_path / "reference"
    reference.mkdir()
    source = reference / "tampered.dat"
    source.write_bytes(b"actual bytes")
    catalog = tmp_path / "catalog" / "vault.sqlite3"
    create_v2_catalog(catalog, source, stored_hash="0" * 64)
    database = Database(
        catalog,
        root_mappings={"reference": reference},
        backup_root=tmp_path / "backups",
    )

    with pytest.raises(MigrationError, match="SHA-256 mismatch"):
        database.initialize()

    with sqlite3.connect(catalog) as connection:
        assert connection.execute("SELECT value FROM app_metadata WHERE key='schema_version'").fetchone()[0] == "2"
        assert "library_id" not in {row[1] for row in connection.execute("PRAGMA table_info(datasets)")}
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    backups = list((tmp_path / "backups").glob("catalog-*.sqlite3"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_new_catalog_reaches_latest_schema_without_redundant_backup(tmp_path: Path) -> None:
    database = Database(tmp_path / "catalog" / "new.sqlite3")
    database.initialize()
    assert database.schema_version() == 6
    assert database.last_migration_report.previous_version == 0
    assert database.last_migration_report.applied_versions == (1, 2, 3, 4, 5, 6)
    assert database.last_migration_report.backup is None
    assert database.library_id()
    with database.connect() as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        indexes = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }
    assert {
        "model_registry",
        "ai_tasks",
        "ai_runs",
        "collections",
        "collection_items",
        "selection_snapshots",
        "selection_snapshot_items",
        "exports",
        "export_items",
        "integration_outbox",
        "knowledge_entities",
        "knowledge_relations",
        "obsidian_links",
    } <= tables
    assert {
        "idx_ai_tasks_one_active_input",
        "idx_ai_tasks_claim",
        "idx_ai_tasks_lease",
        "idx_selection_snapshots_expiry",
        "idx_exports_status_created",
        "idx_integration_outbox_claim",
        "idx_obsidian_links_state",
    } <= indexes


def test_ai_schema_ddl_rolls_back_with_the_runner_transaction(tmp_path: Path) -> None:
    catalog = tmp_path / "catalog" / "rollback.sqlite3"
    catalog.parent.mkdir(parents=True)
    connection = sqlite3.connect(catalog)
    connection.row_factory = sqlite3.Row
    context = MigrationContext()
    try:
        for migration in MIGRATIONS[:3]:
            connection.execute("BEGIN IMMEDIATE")
            migration.apply(connection, context)
            connection.execute(
                "INSERT OR REPLACE INTO app_metadata(key,value) VALUES('schema_version',?)",
                (str(migration.version),),
            )
            connection.commit()

        connection.execute("BEGIN IMMEDIATE")
        MIGRATIONS[3].apply(connection, context)
        connection.rollback()
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {"model_registry", "ai_tasks", "ai_runs"}.isdisjoint(tables)
        assert connection.execute(
            "SELECT value FROM app_metadata WHERE key='schema_version'"
        ).fetchone()[0] == "3"
    finally:
        connection.close()


def test_export_schema_ddl_and_revision_triggers_roll_back_transactionally(tmp_path: Path) -> None:
    catalog = tmp_path / "catalog" / "rollback-exports.sqlite3"
    catalog.parent.mkdir(parents=True)
    connection = sqlite3.connect(catalog)
    connection.row_factory = sqlite3.Row
    context = MigrationContext()
    try:
        for migration in MIGRATIONS[:4]:
            connection.execute("BEGIN IMMEDIATE")
            migration.apply(connection, context)
            connection.execute(
                "INSERT OR REPLACE INTO app_metadata(key,value) VALUES('schema_version',?)",
                (str(migration.version),),
            )
            connection.commit()

        connection.execute("BEGIN IMMEDIATE")
        MIGRATIONS[4].apply(connection, context)
        connection.rollback()
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {"collections", "selection_snapshots", "exports"}.isdisjoint(tables)
        assert connection.execute(
            "SELECT value FROM app_metadata WHERE key='schema_version'"
        ).fetchone()[0] == "4"
    finally:
        connection.close()


def test_obsidian_foundation_ddl_and_triggers_roll_back_transactionally(tmp_path: Path) -> None:
    catalog = tmp_path / "catalog" / "rollback-obsidian.sqlite3"
    catalog.parent.mkdir(parents=True)
    connection = sqlite3.connect(catalog)
    connection.row_factory = sqlite3.Row
    context = MigrationContext()
    try:
        for migration in MIGRATIONS[:5]:
            connection.execute("BEGIN IMMEDIATE")
            migration.apply(connection, context)
            connection.execute(
                "INSERT OR REPLACE INTO app_metadata(key,value) VALUES('schema_version',?)",
                (str(migration.version),),
            )
            connection.commit()

        connection.execute("BEGIN IMMEDIATE")
        MIGRATIONS[5].apply(connection, context)
        connection.rollback()
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        triggers = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            )
        }
        assert {"integration_outbox", "knowledge_entities", "knowledge_relations", "obsidian_links"}.isdisjoint(tables)
        assert not any(name.startswith("obsidian_outbox_") for name in triggers)
        assert connection.execute(
            "SELECT value FROM app_metadata WHERE key='schema_version'"
        ).fetchone()[0] == "5"
    finally:
        connection.close()
