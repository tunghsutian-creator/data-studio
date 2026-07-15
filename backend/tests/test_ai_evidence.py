from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from PIL import Image

from backend.ai.evidence import EvidenceBuilder, EvidenceIntegrityError
from backend.ai.provider import LocalModelProfile
from backend.database import Database


def _profile() -> LocalModelProfile:
    root = Path(__file__).resolve().parents[2]
    return LocalModelProfile.load(root / "profiles" / "windows-rtx5080.json")


def _database(tmp_path: Path) -> tuple[Database, Path]:
    reference = tmp_path / "reference"
    reference.mkdir()
    database = Database(
        tmp_path / "catalog" / "vault.sqlite3",
        root_mappings={
            "reference": reference,
            "inbox": tmp_path / "inbox",
            "vault": tmp_path / "vault",
            "quarantine": tmp_path / "quarantine",
            "exports": tmp_path / "exports",
        },
    )
    database.initialize()
    return database, reference


def _add_asset(
    database: Database,
    reference: Path,
    name: str,
    payload: bytes,
    *,
    group: str = "group-1",
    mime_type: str | None = None,
) -> str:
    source = reference / name
    source.write_bytes(payload)
    return database.upsert_scanned_file(
        source_kind="reference",
        source_root=str(reference),
        group_key=group,
        path=str(source),
        size_bytes=len(payload),
        mtime_ns=source.stat().st_mtime_ns,
        modified_at="2026-07-15T00:00:00+00:00",
        sha256=hashlib.sha256(payload).hexdigest(),
        classification={
            "label": "UNKNOWN",
            "confidence": 0.2,
            "method": "test",
            "evidence": [],
            "conflict": False,
            "metadata": {},
        },
        canonical_name="TEST_DATASET",
        mime_type=mime_type,
    )


def test_evidence_is_deterministic_bounded_and_omits_paths_and_filenames(tmp_path: Path) -> None:
    database, reference = _database(tmp_path)
    dataset_id = _add_asset(
        database,
        reference,
        "private-sample-name.csv",
        b"strain,stress\n0.01,12\n",
        mime_type="text/csv",
    )
    for index in range(20):
        _add_asset(
            database,
            reference,
            f"private-binary-{index}.bin",
            f"binary-{index}".encode("ascii"),
        )
    builder = EvidenceBuilder(database, database.root_mapper, _profile())

    first = builder.build(dataset_id)
    second = builder.build(dataset_id)
    structured = first.request.structured_evidence
    parsed = json.loads(structured)

    assert first.request.input_fingerprint == second.request.input_fingerprint
    assert first.asset_count == 21
    assert first.image_count == 0
    assert len(structured.encode("utf-8")) <= _profile().max_structured_evidence_bytes
    assert "private-sample-name" not in structured
    assert "private-binary" not in structured
    assert str(reference) not in structured
    assert parsed["contract"]["paths_omitted"] is True
    assert parsed["manifest"]["asset_count"] == 21
    assert parsed["manifest"]["descriptions_omitted"] == 5
    assert parsed["assets"][0]["content_is_untrusted_data"] is True
    assert "strain,stress" in parsed["assets"][0]["content_preview"]


def test_image_preview_is_bounded_in_memory(tmp_path: Path) -> None:
    database, reference = _database(tmp_path)
    image_path = reference / "confidential-micrograph.tif"
    Image.new("RGB", (128, 64), color=(120, 120, 120)).save(image_path)
    payload = image_path.read_bytes()
    dataset_id = _add_asset(
        database,
        reference,
        image_path.name,
        payload,
        mime_type="image/tiff",
    )
    builder = EvidenceBuilder(database, database.root_mapper, _profile())

    package = builder.build(dataset_id)

    assert package.image_count == 1
    assert package.request.image_data_urls[0].startswith("data:image/png;base64,")
    assert len(package.request.image_data_urls[0]) < (_profile().max_image_bytes * 2)
    assert "confidential-micrograph" not in package.request.structured_evidence


def test_evidence_refuses_asset_bytes_changed_after_indexing(tmp_path: Path) -> None:
    database, reference = _database(tmp_path)
    dataset_id = _add_asset(database, reference, "sample.dat", b"AAAA")
    builder = EvidenceBuilder(database, database.root_mapper, _profile())
    builder.build(dataset_id)
    (reference / "sample.dat").write_bytes(b"BBBB")

    with pytest.raises(EvidenceIntegrityError, match="digest changed"):
        builder.build(dataset_id)
