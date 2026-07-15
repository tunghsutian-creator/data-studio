from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

import pytest

from backend.ai.gold_set import (
    REVIEW_FIELDS,
    build_gold_candidate_bundle,
    load_gold_candidate_pool,
    validate_gold_review,
    write_gold_candidate_bundle,
)
from backend.database import Database


def _catalog(tmp_path: Path) -> tuple[Path, list[str]]:
    database = Database(tmp_path / "catalog" / "vault.sqlite3")
    database.initialize()
    reference = tmp_path / "reference"
    reference.mkdir()
    fixtures = [
        ("UNKNOWN", 0.20, ".txt", False),
        ("SEM", 0.97, ".tif", False),
        ("FTIR", 0.92, ".csv", False),
        ("TENSILE", 0.75, ".dat", False),
        ("RHEOLOGY", 0.88, ".csv", False),
        ("IMPACT", 0.96, ".txt", False),
        ("TORQUE", 0.58, ".csv", False),
        ("SIMULATION", 0.99, ".inp", False),
    ]
    names: list[str] = []
    for index, (modality, confidence, extension, conflict) in enumerate(fixtures):
        name = f"private-sample-{index}{extension}"
        names.append(name)
        source = reference / name
        payload = f"synthetic fixture {index}".encode()
        source.write_bytes(payload)
        database.upsert_scanned_file(
            source_kind="reference",
            source_root=str(reference),
            group_key=f"group-{index}",
            path=str(source),
            size_bytes=len(payload),
            mtime_ns=index + 1,
            modified_at="2026-07-15T00:00:00+00:00",
            sha256=hashlib.sha256(payload).hexdigest(),
            classification={
                "label": modality,
                "confidence": confidence,
                "method": f"synthetic-rule-{index % 3}",
                "evidence": [],
                "conflict": conflict,
                "metadata": {},
            },
            canonical_name=f"SYNTHETIC_{index}",
            role="IMAGE" if extension == ".tif" else "MEASUREMENT",
            mime_type="image/tiff" if extension == ".tif" else "text/plain",
        )
    return database.path, names


def test_candidate_selection_is_deterministic_stratified_and_blinded(tmp_path: Path) -> None:
    catalog, private_names = _catalog(tmp_path)
    _, pool, excluded = load_gold_candidate_pool(catalog)
    first = build_gold_candidate_bundle(catalog, target=5, seed="stable-seed")
    repeated = build_gold_candidate_bundle(catalog, target=5, seed="stable-seed")

    first_ids = [item["dataset_id"] for item in first["audit"]["candidates"]]
    assert first_ids == [item["dataset_id"] for item in repeated["audit"]["candidates"]]
    assert first["audit"]["selection_manifest_sha256"] == repeated["audit"]["selection_manifest_sha256"]
    assert {item.dataset_id for item in pool if item.mandatory} <= set(first_ids)
    assert excluded == {}
    assert first["audit"]["human_labels_prefilled"] == 0
    assert first["audit"]["sampling_prediction_is_not_gold_truth"] is True
    for row in first["review_rows"]:
        assert tuple(row) == REVIEW_FIELDS
        assert row["decision"] == "PENDING"
        assert row["gold_modality"] == ""

    serialized = json.dumps(first, ensure_ascii=False)
    assert str(tmp_path) not in serialized
    for name in private_names:
        assert name not in serialized


def test_bundle_must_live_outside_repo_and_review_must_match_audit(tmp_path: Path) -> None:
    catalog, _ = _catalog(tmp_path)
    bundle = build_gold_candidate_bundle(catalog, target=5, seed="review-seed")
    repository = tmp_path / "repository"
    repository.mkdir()
    with pytest.raises(ValueError, match="outside the Git repository"):
        write_gold_candidate_bundle(
            repository / "leak",
            bundle,
            repository_root=repository,
        )

    paths = write_gold_candidate_bundle(
        tmp_path / "evaluation" / "gold-v1",
        bundle,
        repository_root=repository,
    )
    review_path = Path(paths["review"])
    audit_path = Path(paths["audit"])
    with review_path.open("r", encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    for row in rows:
        row.update(
            {
                "decision": "INCLUDE",
                "gold_modality": "SEM",
                "reviewer": "human-reviewer",
                "reviewed_at": "2026-07-15T12:00:00+08:00",
            }
        )
    with review_path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=REVIEW_FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    report = validate_gold_review(
        review_path,
        audit_path,
        minimum_included=5,
    )
    assert report["ready"] is True
    assert report["included"] == 5

    tampered = json.loads(audit_path.read_text(encoding="utf-8"))
    tampered["candidates"][0]["sampling_only_prediction"]["modality"] = "FTIR"
    audit_path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(ValueError, match="selection manifest"):
        validate_gold_review(review_path, audit_path, minimum_included=5)


def test_pending_or_underfilled_review_is_not_gold_truth(tmp_path: Path) -> None:
    catalog, _ = _catalog(tmp_path)
    bundle = build_gold_candidate_bundle(catalog, target=5, seed="pending-seed")
    repository = tmp_path / "repository"
    repository.mkdir()
    paths = write_gold_candidate_bundle(
        tmp_path / "evaluation" / "pending",
        bundle,
        repository_root=repository,
    )

    with pytest.raises(ValueError, match="still PENDING"):
        validate_gold_review(paths["review"], paths["audit"], minimum_included=5)
