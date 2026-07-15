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
from backend.ai.soak import _gpu_memory_trend, run_worker_soak
from backend.ai.worker import AIWorker
from backend.database import Database


def _profile() -> LocalModelProfile:
    root = Path(__file__).resolve().parents[2]
    return LocalModelProfile.load(root / "profiles" / "windows-rtx5080.json")


def _classification() -> AIClassification:
    return AIClassification.model_validate(
        {
            "modality": "SEM",
            "workstream": "UNASSIGNED",
            "sample_id": None,
            "material": None,
            "test_method": "SEM",
            "conditions": {},
            "proposed_name": None,
            "confidence": 0.9,
            "evidence": [{"kind": "file_header", "value": "synthetic signal"}],
            "needs_review": True,
            "abstain_reason": None,
        }
    )


def _database(tmp_path: Path, count: int = 3) -> tuple[Database, list[str]]:
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
    dataset_ids: list[str] = []
    for index in range(count):
        source = reference / f"private-{index}.dat"
        payload = f"header,value\ncase,{index}\n".encode()
        source.write_bytes(payload)
        dataset_ids.append(
            database.upsert_scanned_file(
                source_kind="reference",
                source_root=str(reference),
                group_key=f"group-{index}",
                path=str(source),
                size_bytes=len(payload),
                mtime_ns=source.stat().st_mtime_ns,
                modified_at="2026-07-15T00:00:00+00:00",
                sha256=hashlib.sha256(payload).hexdigest(),
                classification={
                    "label": "UNKNOWN",
                    "confidence": 0.1,
                    "method": "synthetic",
                    "evidence": [],
                    "conflict": False,
                    "metadata": {},
                },
                canonical_name=f"PRIVATE_{index}",
                mime_type="text/plain",
            )
        )
    return database, dataset_ids


def _factory(
    database: Database,
    providers: list[FakeLocalModelProvider],
    *,
    first_outcomes=(),
):
    calls = 0

    def make_worker() -> AIWorker:
        nonlocal calls
        calls += 1
        provider = FakeLocalModelProvider(
            _classification(),
            outcomes=first_outcomes if calls == 1 else (),
            identity=ProviderIdentity(
                provider="fake",
                profile_id="soak-test",
                model_id="fake-model",
                quantization="Q8_0",
                device="CPU",
                model_revision="a" * 40,
                runtime_release="test-runtime",
                runtime_commit="b" * 40,
            ),
        )
        providers.append(provider)
        return AIWorker(
            database,
            EvidenceBuilder(database, database.root_mapper, _profile()),
            provider,
            registry_config={"temperature": 0, "seed": 42},
            worker_id=f"soak-worker-{calls}",
            lease_seconds=30,
            base_retry_delay_seconds=0,
        )

    return make_worker


def test_soak_restarts_with_persisted_queue_and_never_mutates_datasets(tmp_path: Path) -> None:
    database, dataset_ids = _database(tmp_path)
    providers: list[FakeLocalModelProvider] = []
    progress: list[dict] = []

    report = run_worker_soak(
        database,
        dataset_ids,
        _factory(database, providers),
        duration_seconds=10,
        max_tasks=6,
        queue_depth=2,
        restart_after_tasks=2,
        checkpoint_every=1,
        progress_callback=progress.append,
        sample_gpu=False,
    )

    assert report["passed"] is True
    assert report["created_tasks"] == report["terminal_tasks"] == 6
    assert report["active_tasks_remaining"] == 0
    assert report["worker_restarts"] == 1
    assert report["queue_peak"] == 2
    assert report["run_statuses"] == {"SUCCEEDED": 6}
    assert report["task_statuses"] == {"COMPLETED": 6}
    assert report["protected_state_before_sha256"] == report["protected_state_after_sha256"]
    assert len(report["model_registry_ids"]) == 1
    assert len(providers) == 2
    assert sum(len(provider.requests) for provider in providers) == 6
    assert progress
    for dataset_id in dataset_ids:
        item = database.get_dataset(dataset_id)
        assert item["status"] == "REVIEW"
        assert item["modality"] == "UNKNOWN"


def test_soak_records_retry_failure_even_when_task_eventually_completes(tmp_path: Path) -> None:
    database, dataset_ids = _database(tmp_path, count=1)
    providers: list[FakeLocalModelProvider] = []
    report = run_worker_soak(
        database,
        dataset_ids,
        _factory(
            database,
            providers,
            first_outcomes=(ProviderTimeout("timed out"), _classification()),
        ),
        duration_seconds=10,
        max_tasks=1,
        queue_depth=1,
        restart_after_tasks=None,
        sample_gpu=False,
    )

    assert report["terminal_tasks"] == 1
    assert report["task_statuses"] == {"COMPLETED": 1}
    assert report["run_statuses"] == {"FAILED": 1, "SUCCEEDED": 1}
    assert report["error_codes"] == {"provider_timeout": 1}
    assert report["gates"]["no_failed_runs"] is False
    assert report["passed"] is False


def test_gpu_memory_trend_uses_post_warmup_and_final_windows() -> None:
    stable = _gpu_memory_trend(
        ([10_000] * 240) + ([10_100] * 240) + ([10_200] * 240),
        16_000,
    )
    assert stable["available"] is True
    assert stable["growth_mib"] == 200
    assert stable["allowed_growth_mib"] == 480

    leaking = _gpu_memory_trend(
        ([10_000] * 240) + ([10_100] * 240) + ([11_000] * 240),
        16_000,
    )
    assert leaking["growth_mib"] == 1_000
    assert leaking["growth_mib"] > leaking["allowed_growth_mib"]


def test_gpu_memory_trend_fails_closed_without_enough_samples() -> None:
    trend = _gpu_memory_trend([10_000] * 10, 16_000)
    assert trend["available"] is False
    assert trend["growth_mib"] is None
