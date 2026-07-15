from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from backend.database import Database


def _database_with_dataset(tmp_path: Path) -> tuple[Database, str]:
    database = Database(tmp_path / "catalog" / "vault.sqlite3")
    database.initialize()
    reference = tmp_path / "reference"
    reference.mkdir()
    source = reference / "sample.dat"
    source.write_bytes(b"fixture")
    dataset_id = database.upsert_scanned_file(
        source_kind="reference",
        source_root=str(reference),
        group_key="sample",
        path=str(source),
        size_bytes=source.stat().st_size,
        mtime_ns=1,
        modified_at="2026-07-15T00:00:00+00:00",
        sha256="1" * 64,
        classification={
            "label": "UNKNOWN",
            "confidence": 0.0,
            "method": "test",
            "evidence": [],
            "conflict": False,
            "metadata": {},
        },
        canonical_name="SAMPLE",
    )
    return database, dataset_id


def _identity() -> dict[str, str]:
    return {
        "provider": "llama.cpp",
        "profile_id": "qwen3vl-8b-q8-windows-cuda",
        "model_id": "Qwen/Qwen3-VL-8B-Instruct-GGUF",
        "quantization": "Q8_0",
        "device": "CUDA",
        "model_revision": "abc123",
        "runtime_release": "b10015",
        "runtime_commit": "def456",
        "prompt_version": "prompt-v1",
        "taxonomy_version": "taxonomy-v1",
        "output_schema_version": "1.2",
    }


def _register(database: Database) -> dict[str, object]:
    return database.register_model(
        _identity(),
        config={"temperature": 0.0, "seed": 42, "context_tokens": 8192},
    )


def _classification(modality: str = "SEM") -> dict[str, object]:
    unknown = modality == "UNKNOWN"
    return {
        "modality": modality,
        "workstream": "REFERENCE",
        "sample_id": "A1",
        "material": None,
        "test_method": None,
        "conditions": {},
        "proposed_name": None,
        "confidence": 0.0 if unknown else 0.91,
        "evidence": [
            {
                "kind": "abstention" if unknown else "file_header",
                "value": "insufficient evidence" if unknown else "TIFF header",
            }
        ],
        "needs_review": unknown,
        "abstain_reason": "insufficient evidence" if unknown else None,
    }


def test_model_registry_is_deterministic_immutable_and_rejects_credentials(tmp_path: Path) -> None:
    database, _ = _database_with_dataset(tmp_path)

    first = _register(database)
    repeated = _register(database)
    changed_config = database.register_model(_identity(), config={"temperature": 0.1})

    assert first["id"] == repeated["id"]
    assert first["config"]["seed"] == 42
    assert changed_config["id"] != first["id"]
    assert len(database.list_registered_models()) == 2
    with pytest.raises(ValueError, match="credentials"):
        database.register_model(_identity(), config={"api_key": "must-not-persist"})
    missing_revision = _identity()
    missing_revision["model_revision"] = None  # type: ignore[assignment]
    with pytest.raises(ValueError, match="model_revision"):
        database.register_model(missing_revision)
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        with database.transaction() as connection:
            connection.execute(
                "UPDATE model_registry SET model_id='mutated' WHERE id=?",
                (first["id"],),
            )


def test_active_task_deduplication_priority_and_successful_run(tmp_path: Path) -> None:
    database, dataset_id = _database_with_dataset(tmp_path)
    model = _register(database)
    low = database.enqueue_ai_task(
        dataset_id, "a" * 64, reason="LOW_CONFIDENCE", priority=10
    )
    high = database.enqueue_ai_task(
        dataset_id, "b" * 64, reason="RULE_CONFLICT", priority=200
    )
    duplicate = database.enqueue_ai_task(
        dataset_id, "b" * 64, reason="DUPLICATE_REQUEST", priority=999
    )

    assert low["created"] is True
    assert high["created"] is True
    assert duplicate["created"] is False
    assert duplicate["id"] == high["id"]
    assert duplicate["priority"] == 200

    claimed = database.claim_next_ai_task("worker-a")
    assert claimed is not None
    assert claimed["id"] == high["id"]
    assert claimed["status"] == "RUNNING"
    assert claimed["attempt_count"] == 1
    with pytest.raises(RuntimeError, match="not owned"):
        database.heartbeat_ai_task(claimed["id"], "worker-b")

    run = database.start_ai_run(
        claimed["id"],
        str(model["id"]),
        "worker-a",
        request_fingerprint="b" * 64,
    )
    completed = database.complete_ai_run(
        run["id"],
        "worker-a",
        classification=_classification(),
        response_sha256="c" * 64,
        latency_ms=1234,
    )

    assert completed["status"] == "SUCCEEDED"
    assert completed["classification"]["modality"] == "SEM"
    assert database.get_ai_task(claimed["id"])["status"] == "COMPLETED"
    assert database.active_ai_task_count() == 1
    repeated_completion = database.complete_ai_run(
        run["id"],
        "worker-a",
        classification=_classification(),
        response_sha256="c" * 64,
        latency_ms=1234,
    )
    assert repeated_completion["id"] == run["id"]

    automatic_reuse = database.enqueue_ai_task(
        dataset_id,
        "b" * 64,
        reason="AUTO_INBOX:UNCHANGED",
        reuse_completed_model_id=str(model["id"]),
    )
    assert automatic_reuse["created"] is False
    assert automatic_reuse["id"] == high["id"]

    requeued = database.enqueue_ai_task(
        dataset_id, "b" * 64, reason="MODEL_VERSION_CHANGED"
    )
    assert requeued["created"] is True
    assert requeued["id"] != high["id"]


def test_unknown_result_becomes_explicit_abstention(tmp_path: Path) -> None:
    database, dataset_id = _database_with_dataset(tmp_path)
    model = _register(database)
    task = database.enqueue_ai_task(dataset_id, "d" * 64, reason="UNKNOWN_MODALITY")
    claimed = database.claim_next_ai_task("worker-a")
    assert claimed is not None
    run = database.start_ai_run(
        task["id"], str(model["id"]), "worker-a", request_fingerprint="d" * 64
    )

    result = database.complete_ai_run(
        run["id"],
        "worker-a",
        classification=_classification("UNKNOWN"),
        response_sha256="e" * 64,
        latency_ms=50,
    )

    assert result["status"] == "ABSTAINED"
    assert database.get_ai_task(task["id"])["status"] == "ABSTAINED"


def test_automatic_completed_reuse_is_scoped_to_registered_model(tmp_path: Path) -> None:
    database, dataset_id = _database_with_dataset(tmp_path)
    first_model = _register(database)
    task = database.enqueue_ai_task(dataset_id, "f" * 64, reason="AUTO_INBOX:UNKNOWN")
    claimed = database.claim_next_ai_task("worker-a")
    assert claimed is not None
    run = database.start_ai_run(
        task["id"],
        str(first_model["id"]),
        "worker-a",
        request_fingerprint="f" * 64,
    )
    database.complete_ai_run(
        run["id"],
        "worker-a",
        classification=_classification(),
        response_sha256="9" * 64,
        latency_ms=10,
    )

    changed_identity = _identity()
    changed_identity["runtime_commit"] = "new-runtime-commit"
    second_model = database.register_model(changed_identity, config={"temperature": 0.0})
    fresh = database.enqueue_ai_task(
        dataset_id,
        "f" * 64,
        reason="AUTO_INBOX:MODEL_CHANGED",
        reuse_completed_model_id=str(second_model["id"]),
    )

    assert fresh["created"] is True
    assert fresh["id"] != task["id"]


def test_retry_exhaustion_and_error_redaction_are_persistent(tmp_path: Path) -> None:
    database, dataset_id = _database_with_dataset(tmp_path)
    model = _register(database)
    task = database.enqueue_ai_task(
        dataset_id, "f" * 64, reason="PROVIDER_RETRY", max_attempts=2
    )

    first_claim = database.claim_next_ai_task("worker-a")
    assert first_claim is not None
    first_run = database.start_ai_run(
        task["id"], str(model["id"]), "worker-a", request_fingerprint="f" * 64
    )
    failed_once = database.fail_ai_run(
        first_run["id"],
        "worker-a",
        error_code="provider_timeout",
        error_detail=r"failed C:\private\sample.dat api_key=hunter2",
        retryable=True,
        latency_ms=30000,
    )

    assert failed_once["status"] == "FAILED"
    assert "C:\\private" not in failed_once["error_detail"]
    assert "hunter2" not in failed_once["error_detail"]
    retry_wait = database.get_ai_task(task["id"])
    assert retry_wait["status"] == "RETRY_WAIT"
    assert retry_wait["finished_at"] is None

    second_claim = database.claim_next_ai_task("worker-b")
    assert second_claim is not None
    assert second_claim["attempt_count"] == 2
    second_run = database.start_ai_run(
        task["id"], str(model["id"]), "worker-b", request_fingerprint="f" * 64
    )
    database.fail_ai_run(
        second_run["id"],
        "worker-b",
        error_code="provider_timeout",
        error_detail="timeout",
        retryable=True,
    )

    exhausted = database.get_ai_task(task["id"])
    assert exhausted["status"] == "FAILED"
    assert exhausted["attempt_count"] == exhausted["max_attempts"] == 2
    assert exhausted["finished_at"] is not None
    assert [item["attempt_number"] for item in database.list_ai_runs(task["id"])] == [2, 1]


def test_expired_lease_recovers_run_and_allows_a_new_attempt(tmp_path: Path) -> None:
    database, dataset_id = _database_with_dataset(tmp_path)
    model = _register(database)
    task = database.enqueue_ai_task(
        dataset_id, "9" * 64, reason="LEASE_RECOVERY", max_attempts=2
    )
    claimed = database.claim_next_ai_task("dead-worker")
    assert claimed is not None
    run = database.start_ai_run(
        task["id"], str(model["id"]), "dead-worker", request_fingerprint="9" * 64
    )
    with database.transaction() as connection:
        connection.execute(
            "UPDATE ai_tasks SET lease_expires_at='2000-01-01T00:00:00+00:00' WHERE id=?",
            (task["id"],),
        )

    assert database.recover_ai_tasks() == 1
    assert database.recover_ai_tasks() == 0
    recovered_task = database.get_ai_task(task["id"])
    recovered_run = database.get_ai_run(run["id"])
    assert recovered_task["status"] == "RETRY_WAIT"
    assert recovered_task["lease_owner"] is None
    assert recovered_run["status"] == "FAILED"
    assert recovered_run["error_code"] == "WORKER_LEASE_EXPIRED"

    reclaimed = database.claim_next_ai_task("replacement-worker")
    assert reclaimed is not None
    assert reclaimed["id"] == task["id"]
    assert reclaimed["attempt_count"] == 2
    with pytest.raises(RuntimeError):
        database.complete_ai_run(
            run["id"],
            "dead-worker",
            classification=_classification(),
            response_sha256="8" * 64,
            latency_ms=10,
        )


def test_concurrent_claimers_cannot_claim_the_same_task(tmp_path: Path) -> None:
    database, dataset_id = _database_with_dataset(tmp_path)
    database.enqueue_ai_task(dataset_id, "7" * 64, reason="CONCURRENT_CLAIM")

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda worker: database.claim_next_ai_task(worker),
                ("worker-a", "worker-b"),
            )
        )

    claimed = [item for item in results if item is not None]
    assert len(claimed) == 1
    assert claimed[0]["attempt_count"] == 1
