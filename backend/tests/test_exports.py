from __future__ import annotations

import csv
import hashlib
import io
import json
import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.config import Settings
from backend.database import Database
from backend.exports import (
    ExportFailure,
    ExportWorker,
    create_export,
    load_export_manifest,
    preview_selection,
    recover_interrupted_exports,
    SelectionChanged,
)
from backend.exports.manifest import render_manifest_csv


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        reference_root=tmp_path / "reference",
        inbox_root=tmp_path / "inbox",
        vault_root=tmp_path / "vault",
        quarantine_root=tmp_path / "quarantine",
        catalog_path=tmp_path / "catalog" / "vault.sqlite3",
        model_path=tmp_path / "models" / "classifier.joblib",
        export_root=tmp_path / "exports",
        backup_root=tmp_path / "backups",
        auto_scan_seconds=0,
        stable_file_seconds=0,
    )


def _database(settings: Settings) -> Database:
    settings.ensure_runtime_directories()
    settings.reference_root.mkdir(parents=True, exist_ok=True)
    database = Database(
        settings.catalog_path,
        root_mappings=settings.root_mappings(),
        backup_root=settings.backup_root,
    )
    database.initialize()
    return database


def _add_asset(
    database: Database,
    settings: Settings,
    relative_path: str,
    content: bytes,
    *,
    group_key: str,
    modality: str = "SEM",
) -> tuple[str, str]:
    path = settings.reference_root / Path(relative_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    dataset_id = database.upsert_scanned_file(
        source_kind="reference",
        source_root=str(settings.reference_root),
        group_key=group_key,
        path=str(path),
        size_bytes=len(content),
        mtime_ns=path.stat().st_mtime_ns,
        modified_at="2026-07-15T00:00:00+00:00",
        sha256=digest,
        classification={
            "label": modality,
            "confidence": 0.99,
            "method": "test",
            "evidence": [],
            "conflict": False,
            "metadata": {},
        },
        canonical_name=group_key,
    )
    asset_id = str(database.get_dataset(dataset_id)["assets"][0]["id"])
    return dataset_id, asset_id


def test_collection_api_is_library_scoped_ordered_and_path_free(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    database = _database(settings)
    _, first = _add_asset(database, settings, "a/first.dat", b"first", group_key="first")
    _, second = _add_asset(database, settings, "b/second.dat", b"second", group_key="second")

    with TestClient(create_app(settings)) as client:
        created = client.post(
            "/api/collections",
            json={"name": "Paper A / Figure 3", "purpose": "Exact figure inputs"},
        )
        assert created.status_code == 201
        collection_id = created.json()["id"]
        added = client.post(
            f"/api/collections/{collection_id}/items",
            json={"asset_ids": [second, first]},
        )
        assert added.status_code == 200
        assert [item["asset_id"] for item in added.json()["items"]] == [second, first]
        assert [item["position"] for item in added.json()["items"]] == [0, 1]

        repeated = client.post(
            f"/api/collections/{collection_id}/items",
            json={"asset_ids": [second, first]},
        )
        assert repeated.status_code == 200
        assert repeated.json()["asset_count"] == 2

        patched = client.patch(
            f"/api/collections/{collection_id}",
            json={"name": "Paper A / Figure 3 final"},
        )
        assert patched.status_code == 200
        assert patched.json()["revision"] == 3

        removed = client.delete(f"/api/collections/{collection_id}/items/{second}")
        assert removed.status_code == 200
        detail = client.get(f"/api/collections/{collection_id}").json()
        assert detail["asset_count"] == 1
        assert detail["items"][0]["position"] == 0
        serialized = json.dumps(detail)
        assert str(tmp_path) not in serialized
        assert "original_path" not in serialized
        assert "managed_path" not in serialized


def test_preview_preserves_duplicates_and_collisions_without_persisting_token(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    database = _database(settings)
    dataset_a, first = _add_asset(
        database,
        settings,
        "a/shared.dat",
        b"identical",
        group_key="group-a",
    )
    _, second = _add_asset(
        database,
        settings,
        "b/shared.dat",
        b"identical",
        group_key="group-b",
    )

    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/api/exports/preview",
            json={"asset_ids": [first, second]},
        )
        assert response.status_code == 200
        preview = response.json()
        assert preview["ready"] is True
        assert preview["asset_count"] == 2
        assert preview["issues"]["counts"] == {
            "DUPLICATE_SHA256": 2,
            "NAME_COLLISION": 2,
        }
        assert preview["items"][0]["duplicate_of"] is None
        assert preview["items"][1]["duplicate_of"] == first
        serialized = json.dumps(preview)
        assert str(tmp_path) not in serialized
        assert "selected_relpath" not in serialized

        token = preview["selection_token"]
        with database.connect() as connection:
            snapshot = connection.execute(
                "SELECT * FROM selection_snapshots"
            ).fetchone()
            item_count = connection.execute(
                "SELECT COUNT(*) FROM selection_snapshot_items"
            ).fetchone()[0]
        assert token != snapshot["token_sha256"]
        assert hashlib.sha256(token.encode("ascii")).hexdigest() == snapshot["token_sha256"]
        assert item_count == 2

        filtered = client.post(
            "/api/exports/preview",
            json={
                "filter": {"modality": "SEM"},
                "excluded_asset_ids": [second],
            },
        )
        assert filtered.status_code == 200
        assert filtered.json()["asset_count"] == 1
        assert filtered.json()["items"][0]["dataset_id"] == dataset_a

        by_dataset = client.post(
            "/api/exports/preview",
            json={"dataset_ids": [dataset_a]},
        )
        assert by_dataset.status_code == 200
        assert by_dataset.json()["selection_kind"] == "DATASET_IDS"
        assert [item["asset_id"] for item in by_dataset.json()["items"]] == [first]

        mixed = client.post(
            "/api/exports/preview",
            json={"dataset_ids": [dataset_a], "asset_ids": [first, second]},
        )
        assert mixed.status_code == 200
        assert mixed.json()["selection_kind"] == "ASSET_IDS"
        assert [item["asset_id"] for item in mixed.json()["items"]] == [first, second]

        invalid_mixed_filter = client.post(
            "/api/exports/preview",
            json={"asset_ids": [first], "filter": {"modality": "SEM"}},
        )
        assert invalid_mixed_filter.status_code == 422


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ("delete", "MISSING"),
        ("same-size-change", "HASH_MISMATCH"),
        ("size-change", "SIZE_MISMATCH"),
    ],
)
def test_preview_blocks_missing_or_changed_sources(
    tmp_path: Path,
    mutation: str,
    expected_code: str,
) -> None:
    settings = _settings(tmp_path)
    database = _database(settings)
    _, asset_id = _add_asset(
        database,
        settings,
        "source.dat",
        b"original",
        group_key="source",
    )
    path = settings.reference_root / "source.dat"
    if mutation == "delete":
        path.unlink()
    elif mutation == "same-size-change":
        path.write_bytes(b"ORIGINAL")
    else:
        path.write_bytes(b"different-size")

    preview = preview_selection(database, {"asset_ids": [asset_id]})

    assert preview["ready"] is False
    assert expected_code in preview["items"][0]["issue_codes"]


def test_preview_rejects_catalog_change_during_hashing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    database = _database(settings)
    dataset_id, asset_id = _add_asset(
        database,
        settings,
        "race.dat",
        b"stable",
        group_key="race",
    )
    before = database.catalog_revision()
    original_sha256_file = __import__(
        "backend.exports.selection",
        fromlist=["_sha256_file"],
    )._sha256_file
    changed = False

    def mutate_then_hash(path: Path) -> str:
        nonlocal changed
        if not changed:
            changed = True
            database.update_dataset(dataset_id, {"workstream": "RACE_CHANGED"})
        return original_sha256_file(path)

    monkeypatch.setattr("backend.exports.selection._sha256_file", mutate_then_hash)

    with pytest.raises(SelectionChanged, match="catalog changed"):
        preview_selection(database, {"asset_ids": [asset_id]})
    assert database.catalog_revision() > before
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM selection_snapshots").fetchone()[0] == 0


def test_preview_blocks_unreadable_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    database = _database(settings)
    _, asset_id = _add_asset(
        database,
        settings,
        "unreadable.dat",
        b"content",
        group_key="unreadable",
    )

    def denied(_path: Path) -> str:
        raise PermissionError("denied")

    monkeypatch.setattr("backend.exports.selection._sha256_file", denied)
    preview = preview_selection(database, {"asset_ids": [asset_id]})

    assert preview["ready"] is False
    assert preview["items"][0]["issue_codes"] == ["UNREADABLE"]


def test_folder_export_deduplicates_bytes_but_preserves_every_manifest_item(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    database = _database(settings)
    source_a = b"same scientific bytes"
    _, first = _add_asset(database, settings, "a/shared.dat", source_a, group_key="a")
    _, second = _add_asset(database, settings, "b/shared.dat", source_a, group_key="b")
    preview = preview_selection(database, {"asset_ids": [first, second]})
    created = create_export(
        database,
        {
            "selection_token": preview["selection_token"],
            "name": "Paper A / Figure 3",
            "purpose": "Verified inputs",
            "export_mode": "FOLDER",
            "duplicate_policy": "DEDUPLICATE",
        },
    )

    completed = ExportWorker(database).process_next()

    assert created["status"] == "QUEUED"
    assert completed["status"] == "COMPLETED"
    archive = Path(completed["archive_path"])
    assert archive.is_dir()
    physical_files = list((archive / "files").iterdir())
    assert len(physical_files) == 1
    assert physical_files[0].read_bytes() == source_a
    manifest = load_export_manifest(database, completed["id"])
    assert len(manifest["items"]) == 2
    assert manifest["items"][0]["duplicate_of"] is None
    assert manifest["items"][1]["duplicate_of"] == first
    assert manifest["items"][0]["exported_relpath"] == manifest["items"][1]["exported_relpath"]
    assert hashlib.sha256((archive / "manifest.json").read_bytes()).hexdigest() == completed["manifest_sha256"]
    checksums = (archive / "checksums.sha256").read_text(encoding="utf-8")
    assert checksums.count("files/") == 1
    assert (settings.reference_root / "a/shared.dat").read_bytes() == source_a
    with pytest.raises(ExportFailure, match="already been consumed"):
        create_export(
            database,
            {
                "selection_token": preview["selection_token"],
                "name": "reused",
                "export_mode": "FOLDER",
                "duplicate_policy": "PRESERVE",
            },
        )


def test_zip64_and_manifest_only_modes_are_verified(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    database = _database(settings)
    _, asset_id = _add_asset(
        database,
        settings,
        "zip/input.dat",
        b"zip payload",
        group_key="zip",
    )

    zip_preview = preview_selection(database, {"asset_ids": [asset_id]})
    zip_job = create_export(
        database,
        {
            "selection_token": zip_preview["selection_token"],
            "name": "Portable package",
            "export_mode": "ZIP64",
            "duplicate_policy": "PRESERVE",
        },
    )
    zip_result = ExportWorker(database).process_next()
    assert zip_result["id"] == zip_job["id"]
    assert zip_result["status"] == "COMPLETED"
    archive = Path(zip_result["archive_path"])
    assert archive.suffix == ".zip"
    with zipfile.ZipFile(archive, "r", allowZip64=True) as bundle:
        names = bundle.namelist()
        assert "manifest.json" in names
        assert "checksums.sha256" in names
        assert len([name for name in names if name.startswith("files/")]) == 1
    assert load_export_manifest(database, zip_job["id"])["export_mode"] == "ZIP64"

    manifest_preview = preview_selection(database, {"asset_ids": [asset_id]})
    manifest_job = create_export(
        database,
        {
            "selection_token": manifest_preview["selection_token"],
            "name": "Manifest only",
            "export_mode": "MANIFEST_ONLY",
            "duplicate_policy": "PRESERVE",
        },
    )
    manifest_result = ExportWorker(database).process_next()
    assert manifest_result["id"] == manifest_job["id"]
    assert manifest_result["status"] == "COMPLETED"
    manifest_root = Path(manifest_result["archive_path"])
    assert not (manifest_root / "files").exists()
    manifest = load_export_manifest(database, manifest_job["id"])
    assert manifest["items"][0]["exported_relpath"] is None
    assert manifest["items"][0]["exported_sha256"] == manifest["items"][0]["source_sha256"]


def test_export_fails_closed_when_source_changes_after_preview(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    database = _database(settings)
    _, asset_id = _add_asset(
        database,
        settings,
        "changed.dat",
        b"before",
        group_key="changed",
    )
    preview = preview_selection(database, {"asset_ids": [asset_id]})
    job = create_export(
        database,
        {
            "selection_token": preview["selection_token"],
            "name": "Must fail",
            "export_mode": "FOLDER",
            "duplicate_policy": "PRESERVE",
        },
    )
    (settings.reference_root / "changed.dat").write_bytes(b"AFTER!")

    failed = ExportWorker(database).process_next()

    assert failed["id"] == job["id"]
    assert failed["status"] == "FAILED"
    assert failed["error_code"] == "SOURCE_HASH_MISMATCH"
    assert failed["archive_path"] is None
    assert list(settings.export_root.glob("Must-fail--*")) == []


def test_recovery_reconciles_an_existing_verified_atomic_output(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    database = _database(settings)
    _, asset_id = _add_asset(database, settings, "recover.dat", b"recover", group_key="recover")
    preview = preview_selection(database, {"asset_ids": [asset_id]})
    job = create_export(
        database,
        {
            "selection_token": preview["selection_token"],
            "name": "Recoverable",
            "export_mode": "FOLDER",
            "duplicate_policy": "PRESERVE",
        },
    )
    completed = ExportWorker(database).process_next()
    archive = Path(completed["archive_path"])
    before_manifest = (archive / "manifest.json").read_bytes()
    with database.transaction() as connection:
        connection.execute(
            "UPDATE exports SET status='RUNNING',finished_at=NULL WHERE id=?",
            (job["id"],),
        )

    assert recover_interrupted_exports(database) == 1
    recovered = ExportWorker(database).process_next()

    assert recovered["status"] == "COMPLETED"
    assert (archive / "manifest.json").read_bytes() == before_manifest
    assert len(list(settings.export_root.glob("Recoverable--*"))) == 1


def test_manifest_contains_exactly_eighteen_selected_assets(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    database = _database(settings)
    asset_ids: list[str] = []
    for index in range(18):
        _, asset_id = _add_asset(
            database,
            settings,
            f"eighteen/{index:02d}.dat",
            f"payload-{index}".encode(),
            group_key=f"eighteen-{index}",
        )
        asset_ids.append(asset_id)
    preview = preview_selection(database, {"asset_ids": asset_ids})
    job = create_export(
        database,
        {
            "selection_token": preview["selection_token"],
            "name": "Exact eighteen",
            "export_mode": "MANIFEST_ONLY",
            "duplicate_policy": "PRESERVE",
        },
    )

    completed = ExportWorker(database).process_next()
    manifest = load_export_manifest(database, job["id"])

    assert completed["status"] == "COMPLETED"
    assert len(manifest["items"]) == 18
    assert [item["asset_id"] for item in manifest["items"]] == asset_ids


def test_export_api_wakes_durable_worker_and_returns_verified_manifest(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    database = _database(settings)
    _, asset_id = _add_asset(database, settings, "api.dat", b"api", group_key="api")

    with TestClient(create_app(settings)) as client:
        preview = client.post("/api/exports/preview", json={"asset_ids": [asset_id]}).json()
        response = client.post(
            "/api/exports",
            json={
                "selection_token": preview["selection_token"],
                "name": "API export",
                "export_mode": "FOLDER",
                "duplicate_policy": "PRESERVE",
            },
        )
        assert response.status_code == 202
        export_id = response.json()["id"]
        detail = None
        for _ in range(200):
            detail = client.get(f"/api/exports/{export_id}").json()
            if detail["status"] in {"COMPLETED", "FAILED"}:
                break
            time.sleep(0.01)
        assert detail is not None
        assert detail["status"] == "COMPLETED"
        manifest = client.get(f"/api/exports/{export_id}/manifest")
        assert manifest.status_code == 200
        assert manifest.json()["items"][0]["asset_id"] == asset_id


def test_export_never_overwrites_preexisting_destination(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    database = _database(settings)
    _, asset_id = _add_asset(database, settings, "collision.dat", b"safe", group_key="collision")
    preview = preview_selection(database, {"asset_ids": [asset_id]})
    job = create_export(
        database,
        {
            "selection_token": preview["selection_token"],
            "name": "Collision",
            "export_mode": "FOLDER",
            "duplicate_policy": "PRESERVE",
        },
    )
    destination = settings.export_root / f"Collision--{job['id'].replace('-', '')[:8]}"
    destination.mkdir()
    marker = destination / "user-owned.txt"
    marker.write_bytes(b"do not overwrite")

    failed = ExportWorker(database).process_next()

    assert failed["status"] == "FAILED"
    assert failed["error_code"] == "OUTPUT_EXISTS"
    assert marker.read_bytes() == b"do not overwrite"


def test_human_csv_projection_neutralizes_spreadsheet_formulas() -> None:
    payload = render_manifest_csv(
        [
            {
                "position": 0,
                "dataset_id": "dataset",
                "asset_id": "asset",
                "original_name": "=1+1.dat",
                "exported_relpath": None,
                "source_kind": "reference",
                "source_sha256": "a" * 64,
                "exported_sha256": "a" * 64,
                "size_bytes": 1,
                "duplicate_of": None,
            }
        ]
    )
    row = next(csv.DictReader(io.StringIO(payload.decode("utf-8"))))

    assert row["original_name"] == "'=1+1.dat"


def test_completed_bundle_detects_post_export_tampering(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    database = _database(settings)
    _, asset_id = _add_asset(database, settings, "tamper.dat", b"trusted", group_key="tamper")
    preview = preview_selection(database, {"asset_ids": [asset_id]})
    job = create_export(
        database,
        {
            "selection_token": preview["selection_token"],
            "name": "Tamper check",
            "export_mode": "FOLDER",
            "duplicate_policy": "PRESERVE",
        },
    )
    completed = ExportWorker(database).process_next()
    exported_file = next((Path(completed["archive_path"]) / "files").iterdir())
    exported_file.write_bytes(b"tampered")

    with pytest.raises(ExportFailure, match="SHA-256"):
        load_export_manifest(database, job["id"])
