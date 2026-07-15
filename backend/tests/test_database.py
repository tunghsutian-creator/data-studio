from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from backend.config import Settings
from backend.database import Database
from backend.ingestion import accept_dataset
from backend.scanner import scan_source


def settings_for(tmp_path: Path) -> Settings:
    return Settings(
        reference_root=tmp_path / "instrument-data",
        inbox_root=tmp_path / "inbox",
        vault_root=tmp_path / "vault",
        quarantine_root=tmp_path / "quarantine",
        catalog_path=tmp_path / "catalog" / "vault.sqlite3",
        model_path=tmp_path / "models" / "classifier.joblib",
        stable_file_seconds=0,
    )


def test_database_uses_delete_journal_and_queries_catalog(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    settings.ensure_runtime_directories()
    database = Database(settings.catalog_path)
    database.initialize()

    dataset_id = database.upsert_scanned_file(
        source_kind="reference",
        source_root=str(settings.reference_root),
        group_key="sem/a1",
        path=str(settings.reference_root / "a1.tif"),
        size_bytes=42,
        mtime_ns=1,
        modified_at="2026-07-14T00:00:00+00:00",
        sha256="a" * 64,
        classification={
            "label": "SEM",
            "confidence": 0.99,
            "method": "rule:sem",
            "evidence": ["header"],
            "conflict": False,
            "metadata": {"sample": "A1", "material": "VIRGIN", "lifecycle": "RAW"},
        },
        canonical_name="SEM_A1",
    )

    assert database.journal_mode() == "delete"
    listing = database.list_datasets(modality="SEM")
    assert listing["total"] == 1
    assert listing["items"][0]["file_count"] == 1
    assert database.get_dataset(dataset_id)["assets"][0]["sha256"] == "a" * 64
    assert database.summary()["total_bytes"] == 42
    assert database.filters()["modalities"] == ["SEM"]


def test_reference_scan_only_indexes_and_does_not_mutate_source(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    settings.ensure_runtime_directories()
    settings.reference_root.mkdir(parents=True)
    source = settings.reference_root / "sample.id_tens"
    original = b"Sample,Strain,Stress\nA1,0.01,10\n"
    source.write_bytes(original)
    database = Database(settings.catalog_path)
    database.initialize()

    result = scan_source(settings, database, "reference")

    assert result["scanned"] == 1
    assert source.read_bytes() == original
    assert database.list_datasets()["items"][0]["status"] == "INDEXED"
    assert not any(settings.vault_root.rglob("*"))


@pytest.mark.parametrize(
    ("label", "confidence", "conflict", "expected"),
    [
        ("TENSILE", 0.99, False, "INDEXED"),
        ("UNKNOWN", 0.99, False, "REVIEW"),
        ("TENSILE", 0.59, False, "REVIEW"),
        ("TENSILE", 0.99, True, "REVIEW"),
    ],
)
def test_reference_initial_status_requires_confident_unconflicted_classification(
    tmp_path: Path,
    label: str,
    confidence: float,
    conflict: bool,
    expected: str,
) -> None:
    settings = settings_for(tmp_path)
    settings.ensure_runtime_directories()
    database = Database(settings.catalog_path)
    database.initialize()
    source = settings.reference_root / f"{label}-{confidence}-{conflict}.dat"
    dataset_id = database.upsert_scanned_file(
        source_kind="reference",
        source_root=str(settings.reference_root),
        group_key=source.stem,
        path=str(source),
        size_bytes=1,
        mtime_ns=1,
        modified_at="2026-07-14T00:00:00+00:00",
        sha256="b" * 64,
        classification={
            "label": label,
            "confidence": confidence,
            "method": "test",
            "evidence": [],
            "conflict": conflict,
            "metadata": {},
        },
        canonical_name=source.stem,
    )

    assert database.get_dataset(dataset_id)["status"] == expected


def test_enabled_user_rule_is_applied_on_next_scan(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    settings.ensure_runtime_directories()
    settings.reference_root.mkdir(parents=True)
    source = settings.reference_root / "mystery.special"
    source.write_bytes(b"unrecognized local format")
    database = Database(settings.catalog_path)
    database.initialize()
    rule = database.create_rule(
        {"name": "Local GPC suffix", "pattern": r"mystery\.special$", "label": "GPC", "priority": 1, "enabled": True}
    )

    scan_source(settings, database, "reference")
    item = database.list_datasets()["items"][0]
    detail = database.get_dataset(item["id"])

    assert item["modality"] == "GPC"
    assert detail["classification_method"] == f"user-rule:{rule['id']}"


@pytest.mark.parametrize(
    ("inner_folder", "expected_workstream"),
    [
        ("1 Reference", "REFERENCE"),
        ("2 PA ADR Recycle", "PA_ADR_RECYCLE"),
        ("3 D PA", "D_PA"),
        ("4 UDC", "UDC"),
        (None, "VITRIMER"),
        ("reference", "REFERENCE"),
    ],
)
def test_reference_folder_context_enriches_workstream_after_classification(
    tmp_path: Path,
    inner_folder: str | None,
    expected_workstream: str,
) -> None:
    settings = settings_for(tmp_path)
    settings.ensure_runtime_directories()
    folder = settings.reference_root / "1 Vitrimer"
    if inner_folder:
        folder /= inner_folder
    folder.mkdir(parents=True)
    (folder / "A7.id_tens").write_bytes(b"Sample,Strain,Stress\nA7,0.1,12\n")
    database = Database(settings.catalog_path)
    database.initialize()

    scan_source(settings, database, "reference")
    item = database.list_datasets()["items"][0]
    detail = database.get_dataset(item["id"])

    assert item["workstream"] == expected_workstream
    assert item["canonical_name"].startswith(expected_workstream + "_")
    assert detail["decisions"][0]["proposed_metadata"]["workstream"] == expected_workstream
    assert any("context workstream" in entry for entry in detail["decisions"][0]["evidence"])


def test_rescan_refreshes_legacy_canonical_name_but_preserves_human_decision(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    settings.ensure_runtime_directories()
    folder = settings.reference_root / "1 Vitrimer" / "2 PA ADR Recycle"
    folder.mkdir(parents=True)
    (folder / "A7.id_tens").write_bytes(b"Sample,Strain,Stress\nA7,0.1,12\n")
    database = Database(settings.catalog_path)
    database.initialize()
    scan_source(settings, database, "reference")
    dataset_id = database.list_datasets()["items"][0]["id"]
    with database.transaction() as connection:
        connection.execute(
            "UPDATE datasets SET workstream='UNASSIGNED',canonical_name='LEGACY_NAME',status='INDEXED' WHERE id=?",
            (dataset_id,),
        )

    scan_source(settings, database, "reference")
    refreshed = database.get_dataset(dataset_id)
    assert refreshed["workstream"] == "PA_ADR_RECYCLE"
    assert refreshed["canonical_name"].startswith("PA_ADR_RECYCLE_")

    database.update_dataset(
        dataset_id,
        {"workstream": "HUMAN_PROJECT", "canonical_name": "HUMAN_LOCKED_NAME"},
    )
    scan_source(settings, database, "reference")
    protected = database.get_dataset(dataset_id)
    assert protected["status"] == "REVIEWED"
    assert protected["workstream"] == "HUMAN_PROJECT"
    assert protected["canonical_name"] == "HUMAN_LOCKED_NAME"


def test_inbox_accept_creates_verified_copy_and_retains_source(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    settings.ensure_runtime_directories()
    source = settings.inbox_root / "experiment.is_tens"
    original = b"Sample,Strain,Stress\nA1,0.01,10\n"
    source.write_bytes(original)
    database = Database(settings.catalog_path)
    database.initialize()
    assert scan_source(settings, database, "inbox")["scanned"] == 1
    dataset_id = database.list_datasets()["items"][0]["id"]

    accepted = accept_dataset(settings, database, dataset_id)

    assert accepted["status"] == "COMMITTED"
    assert source.read_bytes() == original
    managed = Path(accepted["assets"][0]["managed_path"])
    assert settings.assert_vault_path(managed, must_exist=True) == managed.resolve()
    assert managed.read_bytes() == original
    assert accepted["assets"][0]["hash_state"] == "VERIFIED"
    assert accepted["assets"][0]["sha256"] == hashlib.sha256(original).hexdigest()

    rescanned = scan_source(settings, database, "inbox")
    after_rescan = database.get_dataset(dataset_id)
    assert rescanned["scanned"] == 1
    assert after_rescan["status"] == "COMMITTED"
    assert after_rescan["assets"][0]["hash_state"] == "VERIFIED"
    assert after_rescan["assets"][0]["managed_sha256"] == hashlib.sha256(original).hexdigest()


def test_rescan_preserves_human_metadata_edits(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    settings.ensure_runtime_directories()
    source = settings.inbox_root / "A7.id_tens"
    source.write_text("Sample,Strain,Stress\nA7,0.1,12\n", encoding="utf-8")
    database = Database(settings.catalog_path)
    database.initialize()
    scan_source(settings, database, "inbox")
    dataset_id = database.list_datasets()["items"][0]["id"]
    database.update_dataset(
        dataset_id,
        {"canonical_name": "HUMAN_APPROVED_NAME", "modality": "FTIR", "workstream": "PROJECT_X"},
    )

    scan_source(settings, database, "inbox")
    rescanned = database.get_dataset(dataset_id)

    assert rescanned["canonical_name"] == "HUMAN_APPROVED_NAME"
    assert rescanned["modality"] == "FTIR"
    assert rescanned["workstream"] == "PROJECT_X"
    assert rescanned["status"] == "REVIEWED"
    assert rescanned["decisions"][0]["resolution"] == "PREDICTED"
    assert rescanned["decisions"][0]["predicted_label"] == "TENSILE"


def test_changed_source_after_commit_is_marked_stale_without_changing_managed_copy(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    settings.ensure_runtime_directories()
    source = settings.inbox_root / "A7.id_tens"
    original = b"Sample,Strain,Stress\nA7,0.1,12\n"
    changed = b"Sample,Strain,Stress\nA7,0.2,24\n"
    source.write_bytes(original)
    database = Database(settings.catalog_path)
    database.initialize()
    scan_source(settings, database, "inbox")
    dataset_id = database.list_datasets()["items"][0]["id"]
    committed = accept_dataset(settings, database, dataset_id)
    managed = Path(committed["assets"][0]["managed_path"])
    managed_hash = committed["assets"][0]["managed_sha256"]

    source.write_bytes(changed)
    scan_source(settings, database, "inbox")
    stale = database.get_dataset(dataset_id)

    assert stale["status"] == "STALE"
    assert stale["conflict"] is True
    assert stale["assets"][0]["hash_state"] == "STALE_SOURCE"
    assert stale["assets"][0]["source_sha256"] == hashlib.sha256(changed).hexdigest()
    assert stale["assets"][0]["managed_sha256"] == managed_hash
    assert stale["assets"][0]["sha256"] == managed_hash
    assert managed.read_bytes() == original
    assert source.read_bytes() == changed
    assert any(item["action"] == "SOURCE_CHANGED" for item in stale["operations"])

    recommitted = accept_dataset(settings, database, dataset_id)
    new_managed = Path(recommitted["assets"][0]["managed_path"])
    assert recommitted["status"] == "COMMITTED"
    assert new_managed != managed
    assert managed.read_bytes() == original
    assert new_managed.read_bytes() == changed


def test_configured_roots_reject_path_escape(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    settings.ensure_runtime_directories()
    outside = tmp_path / "outside.dat"
    outside.write_bytes(b"no")

    with pytest.raises(ValueError, match="outside configured roots"):
        settings.assert_source_path(outside, "inbox")


def test_configured_roots_cannot_overlap(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must not overlap"):
        Settings(
            reference_root=tmp_path / "reference",
            inbox_root=tmp_path / "inbox",
            vault_root=tmp_path / "inbox" / "vault",
            quarantine_root=tmp_path / "quarantine",
            catalog_path=tmp_path / "catalog.sqlite3",
            model_path=tmp_path / "model.joblib",
        )

    with pytest.raises(ValueError, match="catalog/model files cannot live inside inbox_root"):
        Settings(
            reference_root=tmp_path / "reference",
            inbox_root=tmp_path / "inbox",
            vault_root=tmp_path / "vault",
            quarantine_root=tmp_path / "quarantine",
            catalog_path=tmp_path / "inbox" / "catalog.sqlite3",
            model_path=tmp_path / "model.joblib",
        )
