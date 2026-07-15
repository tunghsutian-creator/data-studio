from __future__ import annotations

from pathlib import Path

import pytest

from backend.database import Database
from backend.integrations.obsidian import (
    ObsidianProjector,
    ProjectionConflict,
    ProjectionSafetyError,
    claim_event,
    complete_event,
)


def _database(tmp_path: Path) -> tuple[Database, Path]:
    raw_root = tmp_path / "synthetic-raw"
    raw_root.mkdir()
    database = Database(
        tmp_path / "catalog" / "vault.sqlite3",
        root_mappings={"reference": raw_root},
        backup_root=tmp_path / "backups",
    )
    database.initialize()
    now = "2026-07-15T00:00:00.000+00:00"
    private_source = raw_root / "private" / "spectrum.csv"
    with database.transaction() as connection:
        library_id = database.library_id(connection)
        connection.execute(
            """
            INSERT INTO datasets(
                id,source_kind,group_key,source_root,canonical_name,workstream,
                material_state,modality,sample_code,confidence,status,
                created_at,updated_at,library_id,revision
            ) VALUES(
                'dataset-projector','reference','projector-group',?,'Polymer FTIR',
                'D_PA','VIRGIN','FTIR','S-01',0.91,'REVIEW',?,?,?,1
            )
            """,
            (str(raw_root), now, now, library_id),
        )
        connection.execute(
            """
            INSERT INTO assets(
                id,dataset_id,original_path,original_name,extension,size_bytes,
                sha256,source_sha256,hash_state,original_root_key,original_relpath,
                path_state,created_at,updated_at
            ) VALUES(
                'asset-projector','dataset-projector',?,'spectrum.csv','.csv',128,
                ?,?,'SOURCE_HASHED','reference','private/spectrum.csv','VALID',?,?
            )
            """,
            (str(private_source), "a" * 64, "a" * 64, now, now),
        )
    return database, raw_root


def _project_first(database: Database, notes_root: Path) -> tuple[ObsidianProjector, Path]:
    event = claim_event(database, "projector-test", lease_seconds=30)
    assert event is not None
    projector = ObsidianProjector(database, notes_root, vault_id="test-vault")
    result = projector.project_event(event)
    assert result["status"] == "SYNCED"
    assert complete_event(database, event["seq"], "projector-test")
    note = notes_root.joinpath(*result["note_relpath"].split("/"))
    return projector, note


def test_projection_omits_paths_preserves_user_notes_and_tombstones(tmp_path: Path) -> None:
    database, raw_root = _database(tmp_path)
    notes_root = tmp_path / "synthetic-notes"
    projector, note = _project_first(database, notes_root)

    initial = note.read_text(encoding="utf-8")
    assert "Polymer FTIR" in initial
    assert "spectrum.csv" in initial
    assert "a" * 64 in initial
    assert str(raw_root) not in initial
    assert "private/spectrum.csv" not in initial

    user_text = "Keep this interpretation exactly: peak 1715 cm-1.\n"
    note.write_text(initial + user_text, encoding="utf-8")
    updated = database.update_dataset(
        "dataset-projector",
        {"canonical_name": "Polymer FTIR reviewed", "status": "REVIEWED"},
    )
    assert updated is not None and updated["revision"] == 2
    event = claim_event(database, "projector-test", lease_seconds=30)
    assert event is not None
    result = projector.project_event(event)
    assert result["status"] == "SYNCED"
    assert complete_event(database, event["seq"], "projector-test")
    refreshed = note.read_text(encoding="utf-8")
    assert "Polymer FTIR reviewed" in refreshed
    assert refreshed.endswith(user_text)

    with database.transaction() as connection:
        connection.execute("DELETE FROM datasets WHERE id='dataset-projector'")
    tombstone = claim_event(database, "projector-test", lease_seconds=30)
    assert tombstone is not None and tombstone["event_type"] == "TOMBSTONE"
    result = projector.project_event(tombstone)
    assert result["status"] == "TOMBSTONED"
    assert complete_event(database, tombstone["seq"], "projector-test")
    archived = note.read_text(encoding="utf-8")
    assert "av_tombstoned: true" in archived
    assert "archived: true" in archived
    assert archived.endswith(user_text)
    with database.connect() as connection:
        link = connection.execute(
            "SELECT * FROM obsidian_links WHERE aggregate_id='dataset-projector'"
        ).fetchone()
    assert link["sync_state"] == "TOMBSTONED"
    assert link["last_aggregate_revision"] == 3


def test_projection_refuses_managed_edits_and_root_overlap(tmp_path: Path) -> None:
    database, raw_root = _database(tmp_path)
    notes_root = tmp_path / "synthetic-notes"
    projector, note = _project_first(database, notes_root)
    edited = note.read_text(encoding="utf-8").replace("file_count: 1", "file_count: 999")
    note.write_text(edited, encoding="utf-8")
    updated = database.update_dataset("dataset-projector", {"workstream": "REFERENCE"})
    assert updated is not None and updated["revision"] == 2
    event = claim_event(database, "projector-test", lease_seconds=30)
    assert event is not None
    before = note.read_bytes()
    with pytest.raises(ProjectionConflict, match="database-owned"):
        projector.project_event(event)
    assert note.read_bytes() == before
    with database.connect() as connection:
        state = connection.execute(
            "SELECT sync_state,last_aggregate_revision FROM obsidian_links WHERE aggregate_id='dataset-projector'"
        ).fetchone()
    assert tuple(state) == ("CONFLICT", 1)

    with pytest.raises(ProjectionSafetyError, match="overlap"):
        ObsidianProjector(database, raw_root / "notes", vault_id="unsafe-vault")
    assert not (raw_root / "notes").exists()


def test_projection_refuses_duplicate_dataset_identity(tmp_path: Path) -> None:
    database, _ = _database(tmp_path)
    notes_root = tmp_path / "synthetic-notes"
    projector, note = _project_first(database, notes_root)
    duplicate = notes_root / "manually-copied-note.md"
    duplicate.write_bytes(note.read_bytes())
    updated = database.update_dataset("dataset-projector", {"sample_code": "S-02"})
    assert updated is not None and updated["revision"] == 2
    event = claim_event(database, "projector-test", lease_seconds=30)
    assert event is not None
    original_before = note.read_bytes()
    duplicate_before = duplicate.read_bytes()
    with pytest.raises(ProjectionConflict, match="duplicate av_id"):
        projector.project_event(event)
    assert note.read_bytes() == original_before
    assert duplicate.read_bytes() == duplicate_before
