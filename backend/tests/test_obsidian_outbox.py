from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from backend.database import Database
from backend.integrations.obsidian import claim_event, complete_event, fail_event, outbox_counts


def _database(tmp_path: Path) -> Database:
    database = Database(
        tmp_path / "catalog" / "vault.sqlite3",
        backup_root=tmp_path / "backups",
    )
    database.initialize()
    return database


def _insert_dataset(database: Database, dataset_id: str = "dataset-outbox") -> None:
    now = "2026-07-15T00:00:00.000+00:00"
    with database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO datasets(
                id,source_kind,group_key,source_root,canonical_name,status,
                created_at,updated_at,library_id,revision
            ) VALUES(?, 'reference', ?, 'reference', 'Outbox dataset', 'REVIEW', ?, ?, ?, 1)
            """,
            (dataset_id, dataset_id, now, now, database.library_id(connection)),
        )


def test_dataset_outbox_is_transactional_leased_and_retryable(tmp_path: Path) -> None:
    database = _database(tmp_path)
    _insert_dataset(database)

    first = claim_event(database, "projector-a", lease_seconds=30)
    assert first is not None
    assert first["aggregate_type"] == "DATASET"
    assert first["aggregate_id"] == "dataset-outbox"
    assert first["aggregate_revision"] == 1
    assert first["event_type"] == "UPSERT"
    assert first["payload"] == {}
    assert claim_event(database, "projector-b", lease_seconds=30) is None
    assert complete_event(database, first["seq"], "wrong-worker") is False
    assert complete_event(database, first["seq"], "projector-a") is True

    updated = database.update_dataset("dataset-outbox", {"workstream": "D_PA"})
    assert updated is not None
    assert updated["revision"] == 2
    second = claim_event(database, "projector-a", lease_seconds=30)
    assert second is not None
    assert second["aggregate_revision"] == 2
    assert fail_event(database, second["seq"], "projector-a", "Obsidian is closed", retry_delay_seconds=60)
    assert claim_event(database, "projector-b", lease_seconds=30) is None
    assert outbox_counts(database) == {"total": 2, "processed": 1, "leased": 0, "pending": 0, "delayed": 1}
    with database.transaction() as connection:
        connection.execute(
            "UPDATE integration_outbox SET available_at=? WHERE seq=?",
            ("2000-01-01T00:00:00.000+00:00", second["seq"]),
        )
    retried = claim_event(database, "projector-b", lease_seconds=30)
    assert retried is not None
    assert retried["seq"] == second["seq"]
    assert retried["attempts"] == 1
    assert retried["last_error"] == "Obsidian is closed"
    assert complete_event(database, retried["seq"], "projector-b")

    before = outbox_counts(database)
    with pytest.raises(RuntimeError, match="rollback"):
        with database.transaction() as connection:
            connection.execute(
                "UPDATE datasets SET revision=revision+1,updated_at=? WHERE id=?",
                ("2026-07-15T00:01:00.000+00:00", "dataset-outbox"),
            )
            raise RuntimeError("rollback")
    assert outbox_counts(database) == before

    with database.transaction() as connection:
        connection.execute("DELETE FROM datasets WHERE id=?", ("dataset-outbox",))
    tombstone = claim_event(database, "projector-a", lease_seconds=30)
    assert tombstone is not None
    assert tombstone["event_type"] == "TOMBSTONE"
    assert tombstone["aggregate_revision"] == 3
    assert complete_event(database, tombstone["seq"], "projector-a")
    assert outbox_counts(database) == {"total": 3, "processed": 3, "leased": 0, "pending": 0, "delayed": 0}


def test_outbox_input_bounds_are_enforced(tmp_path: Path) -> None:
    database = _database(tmp_path)
    with pytest.raises(ValueError, match="worker_id"):
        claim_event(database, "")
    with pytest.raises(ValueError, match="lease_seconds"):
        claim_event(database, "worker", lease_seconds=1)
    with pytest.raises(ValueError, match="retry_delay_seconds"):
        fail_event(database, 1, "worker", "error", retry_delay_seconds=-1)
    with pytest.raises(sqlite3.IntegrityError):
        with database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO obsidian_links(
                    library_id,aggregate_type,aggregate_id,vault_id,note_relpath
                ) VALUES(?, 'DATASET', 'dataset-unsafe', 'vault-test', '../outside.md')
                """,
                (database.library_id(connection),),
            )
