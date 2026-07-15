from __future__ import annotations

import hashlib
from pathlib import Path

from backend.ai.contracts import AIClassification
from backend.ai.evidence import EvidenceBuilder
from backend.ai.provider import (
    FakeLocalModelProvider,
    LocalModelProfile,
    ProviderIdentity,
    ProviderTimeout,
)
from backend.ai.worker import AIWorker, load_locked_llama_provider
from backend.database import Database


def _profile() -> LocalModelProfile:
    root = Path(__file__).resolve().parents[2]
    return LocalModelProfile.load(root / "profiles" / "windows-rtx5080.json")


def _identity() -> ProviderIdentity:
    return ProviderIdentity(
        provider="fake",
        profile_id="fake-q8",
        model_id="fake-model",
        quantization="Q8_0",
        device="CPU",
        model_revision="a" * 40,
        runtime_release="test-runtime",
        runtime_commit="b" * 40,
    )


def _classification(modality: str = "SEM") -> AIClassification:
    return AIClassification.model_validate(
        {
            "modality": modality,
            "workstream": "UNASSIGNED",
            "sample_id": None,
            "material": None,
            "test_method": modality,
            "conditions": {},
            "proposed_name": None,
            "confidence": 0.88,
            "evidence": [
                {"kind": "visual_pattern", "value": "microscopy-like grayscale image"}
            ],
            "needs_review": True,
            "abstain_reason": None,
        }
    )


def _database_and_dataset(tmp_path: Path) -> tuple[Database, Path, str]:
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
    source = reference / "private-name.dat"
    source.write_bytes(b"header,value\nalpha,1\n")
    dataset_id = database.upsert_scanned_file(
        source_kind="reference",
        source_root=str(reference),
        group_key="group-1",
        path=str(source),
        size_bytes=source.stat().st_size,
        mtime_ns=source.stat().st_mtime_ns,
        modified_at="2026-07-15T00:00:00+00:00",
        sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        classification={
            "label": "UNKNOWN",
            "confidence": 0.1,
            "method": "test",
            "evidence": [],
            "conflict": False,
            "metadata": {},
        },
        canonical_name="PRIVATE_NAME",
        mime_type="text/plain",
    )
    return database, source, dataset_id


def _worker(
    database: Database,
    provider: FakeLocalModelProvider,
    *,
    retry_delay: int = 0,
) -> AIWorker:
    return AIWorker(
        database,
        EvidenceBuilder(database, database.root_mapper, _profile()),
        provider,
        registry_config={"temperature": 0, "seed": 42},
        worker_id="test-worker",
        lease_seconds=30,
        base_retry_delay_seconds=retry_delay,
    )


def test_worker_persists_suggestion_without_mutating_dataset(tmp_path: Path) -> None:
    database, _, dataset_id = _database_and_dataset(tmp_path)
    provider = FakeLocalModelProvider(_classification(), identity=_identity())
    worker = _worker(database, provider)
    before = database.get_dataset(dataset_id)
    task = worker.enqueue(dataset_id, reason="UNKNOWN_MODALITY")

    outcome = worker.process_next()
    after = database.get_dataset(dataset_id)
    worker.close()

    assert outcome is not None
    assert outcome.task_status == "COMPLETED"
    assert outcome.run_status == "SUCCEEDED"
    assert task["status"] == "QUEUED"
    assert database.get_ai_task(task["id"])["status"] == "COMPLETED"
    assert database.list_ai_runs(task["id"])[0]["classification"]["modality"] == "SEM"
    assert len(provider.requests) == 1
    assert "private-name" not in provider.requests[0].structured_evidence
    assert before["revision"] == after["revision"]
    assert before["status"] == after["status"] == "REVIEW"
    assert before["modality"] == after["modality"] == "UNKNOWN"


def test_worker_retries_typed_provider_failure_then_completes(tmp_path: Path) -> None:
    database, _, dataset_id = _database_and_dataset(tmp_path)
    provider = FakeLocalModelProvider(
        _classification(),
        outcomes=(ProviderTimeout("timed out", latency_ms=5000), _classification()),
        identity=_identity(),
    )
    worker = _worker(database, provider)
    task = worker.enqueue(dataset_id, reason="PROVIDER_RETRY", max_attempts=2)

    outcomes = worker.run_until_idle()
    worker.close()

    assert [item.task_status for item in outcomes] == ["RETRY_WAIT", "COMPLETED"]
    assert outcomes[0].error_code == "provider_timeout"
    assert database.get_ai_task(task["id"])["attempt_count"] == 2
    assert [item["status"] for item in database.list_ai_runs(task["id"])] == [
        "SUCCEEDED",
        "FAILED",
    ]


def test_worker_rejects_changed_source_without_calling_model(tmp_path: Path) -> None:
    database, source, dataset_id = _database_and_dataset(tmp_path)
    provider = FakeLocalModelProvider(_classification(), identity=_identity())
    worker = _worker(database, provider)
    task = worker.enqueue(dataset_id, reason="SOURCE_CHANGE", max_attempts=2)
    original = source.read_bytes()
    source.write_bytes(b"X" * len(original))

    outcome = worker.process_next()
    worker.close()

    assert outcome is not None
    assert outcome.task_status == "FAILED"
    assert outcome.error_code == "EVIDENCE_INTEGRITY"
    assert database.get_ai_task(task["id"])["attempt_count"] == 1
    assert provider.requests == ()


def test_worker_invalidates_task_when_catalog_assessment_changes(tmp_path: Path) -> None:
    database, _, dataset_id = _database_and_dataset(tmp_path)
    provider = FakeLocalModelProvider(_classification(), identity=_identity())
    worker = _worker(database, provider)
    task = worker.enqueue(dataset_id, reason="CATALOG_CHANGE")
    database.update_dataset(dataset_id, {"modality": "FTIR"})

    outcome = worker.process_next()
    worker.close()

    assert outcome is not None
    assert outcome.error_code == "INPUT_CHANGED"
    assert database.get_ai_task(task["id"])["status"] == "FAILED"
    assert provider.requests == ()


def test_locked_provider_uses_repository_model_and_contract_identity() -> None:
    root = Path(__file__).resolve().parents[2]
    bundle = load_locked_llama_provider(
        root / "profiles" / "windows-rtx5080.json",
        root / "profiles" / "windows-model-lock.json",
    )
    try:
        identity = bundle.provider.identity
        assert identity.model_revision == "f982a07559d4a2f6c8744d840bf6fccab30eea96"
        assert identity.runtime_release == "b10015"
        assert identity.runtime_commit == "12127defda4f41b7679cb2477a4b0d65ee6a0c8f"
        assert bundle.registry_config["model_sha256"] == (
            "0d264b3941185d00a74f75c4245521dae088ff1efc90ab8d1754e83f5844adb0"
        )
    finally:
        bundle.provider.close()
