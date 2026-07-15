from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app import create_app
from backend.config import Settings
from backend.database import Database
from backend.exports import SelectionChanged, preview_selection


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
