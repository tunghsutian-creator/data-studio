"""Durable local-AI queue soak runner used by Phase 3 acceptance."""

from __future__ import annotations

import hashlib
import json
import math
import statistics
import subprocess
import time
from collections import Counter
from contextlib import nullcontext
from datetime import datetime, timezone
from typing import Any, Callable, Sequence

from ..database import Database
from .benchmark import GpuMemorySampler
from .worker import AIWorker


WorkerFactory = Callable[[], AIWorker]
ProgressCallback = Callable[[dict[str, Any]], None]
_ACTIVE = frozenset({"QUEUED", "RUNNING", "RETRY_WAIT"})
_TERMINAL = frozenset({"COMPLETED", "ABSTAINED", "FAILED", "CANCELLED"})


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
        default=str,
    )


def _protected_state_digest(database: Database, dataset_ids: Sequence[str]) -> str:
    if not dataset_ids:
        raise ValueError("soak dataset_ids must not be empty")
    placeholders = ",".join("?" for _ in dataset_ids)
    payload: dict[str, list[dict[str, Any]]] = {}
    with database.connect() as connection:
        for table in ("datasets", "assets", "classification_decisions", "operation_log"):
            key = "id" if table == "datasets" else "dataset_id"
            rows = connection.execute(
                f"SELECT * FROM {table} WHERE {key} IN ({placeholders}) ORDER BY id",
                tuple(dataset_ids),
            ).fetchall()
            payload[table] = [dict(row) for row in rows]
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _percentile(values: Sequence[int], percentile: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    rank = max(0, min(len(ordered) - 1, math.ceil(percentile * len(ordered)) - 1))
    return int(ordered[rank])


def _gpu_total_mib() -> int:
    try:
        output = subprocess.check_output(
            [
                "nvidia-smi",
                "--query-gpu=memory.total",
                "--format=csv,noheader,nounits",
            ],
            text=True,
            timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        values = [int(line.strip()) for line in output.splitlines() if line.strip()]
        return max(values) if values else 0
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0


def _gpu_memory_trend(samples: Sequence[int], total_mib: int) -> dict[str, Any]:
    """Compare post-warmup and final steady-state GPU-memory windows."""

    values = [int(value) for value in samples if int(value) >= 0]
    allowed_growth_mib = max(256, int(total_mib * 0.03)) if total_mib > 0 else 0
    if len(values) < 15 or total_mib <= 0:
        return {
            "available": False,
            "sample_count": len(values),
            "warmup_samples": 0,
            "window_samples": 0,
            "early_steady_median_mib": None,
            "final_steady_median_mib": None,
            "growth_mib": None,
            "allowed_growth_mib": allowed_growth_mib,
        }

    # At the 250 ms production sampling interval, cap both warmup and comparison
    # windows at one minute. Short task-count smoke runs use proportional windows.
    window = min(240, max(5, len(values) // 5))
    warmup = min(240, max(5, len(values) // 5))
    if warmup + (2 * window) > len(values):
        window = max(5, len(values) // 3)
        warmup = max(0, len(values) - (2 * window))
    early = values[warmup : warmup + window]
    final = values[-window:]
    early_median = int(statistics.median(early))
    final_median = int(statistics.median(final))
    return {
        "available": True,
        "sample_count": len(values),
        "warmup_samples": warmup,
        "window_samples": window,
        "early_steady_median_mib": early_median,
        "final_steady_median_mib": final_median,
        "growth_mib": final_median - early_median,
        "allowed_growth_mib": allowed_growth_mib,
    }


def _progress(
    *,
    started_at: float,
    created_tasks: int,
    terminal_tasks: int,
    active_tasks: int,
    run_statuses: Counter[str],
    task_statuses: Counter[str],
    restarts: int,
    queue_peak: int,
) -> dict[str, Any]:
    return {
        "kind": "academic-vault-ai-soak-progress",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "elapsed_seconds": round(time.monotonic() - started_at, 3),
        "created_tasks": created_tasks,
        "terminal_tasks": terminal_tasks,
        "active_tasks": active_tasks,
        "run_statuses": dict(sorted(run_statuses.items())),
        "task_statuses": dict(sorted(task_statuses.items())),
        "worker_restarts": restarts,
        "queue_peak": queue_peak,
    }


def run_worker_soak(
    database: Database,
    dataset_ids: Sequence[str],
    worker_factory: WorkerFactory,
    *,
    duration_seconds: float = 7200,
    max_tasks: int | None = None,
    queue_depth: int = 5,
    restart_after_tasks: int | None = 25,
    drain_timeout_seconds: float = 600,
    checkpoint_every: int = 10,
    progress_callback: ProgressCallback | None = None,
    sample_gpu: bool = True,
) -> dict[str, Any]:
    """Exercise durable tasks repeatedly without mutating dataset-owned state."""

    unique_ids = tuple(dict.fromkeys(str(item) for item in dataset_ids if item))
    if not unique_ids:
        raise ValueError("dataset_ids must contain at least one id")
    if duration_seconds <= 0:
        raise ValueError("duration_seconds must be positive")
    if max_tasks is not None and max_tasks < 1:
        raise ValueError("max_tasks must be positive or null")
    if not 1 <= queue_depth <= min(100, len(unique_ids)):
        raise ValueError("queue_depth must be between 1 and the number of datasets (max 100)")
    if restart_after_tasks is not None and restart_after_tasks < 1:
        raise ValueError("restart_after_tasks must be positive or null")
    if drain_timeout_seconds <= 0 or checkpoint_every < 1:
        raise ValueError("drain timeout and checkpoint interval must be positive")
    missing = [dataset_id for dataset_id in unique_ids if database.get_dataset(dataset_id) is None]
    if missing:
        raise KeyError(f"soak dataset is missing: {missing[0]}")
    if database.active_ai_task_count():
        raise RuntimeError("soak catalog must not contain active AI tasks at start")

    before_digest = _protected_state_digest(database, unique_ids)
    started_at = time.monotonic()
    feed_deadline = started_at + duration_seconds
    drain_deadline: float | None = None
    active_datasets: set[str] = set()
    created_tasks = 0
    terminal_tasks = 0
    run_statuses: Counter[str] = Counter()
    task_statuses: Counter[str] = Counter()
    error_codes: Counter[str] = Counter()
    latencies: list[int] = []
    queue_peak = 0
    case_cursor = 0
    restarts = 0
    model_registry_ids: set[str] = set()
    start_health: dict[str, Any] | None = None
    end_health: dict[str, Any] | None = None

    worker = worker_factory()
    sampler_context = GpuMemorySampler() if sample_gpu else nullcontext(None)
    try:
        start_health = worker.health()
        if not start_health.get("available"):
            raise RuntimeError("local model provider is unavailable before soak")
        model_registry_ids.add(str(worker.registered_model["id"]))

        with sampler_context as gpu:
            while True:
                now = time.monotonic()
                feeding = now < feed_deadline and (
                    max_tasks is None or created_tasks < max_tasks
                )
                if not feeding and drain_deadline is None:
                    drain_deadline = now + drain_timeout_seconds

                attempts = 0
                while feeding and len(active_datasets) < queue_depth:
                    if max_tasks is not None and created_tasks >= max_tasks:
                        break
                    dataset_id = unique_ids[case_cursor % len(unique_ids)]
                    case_cursor += 1
                    attempts += 1
                    if dataset_id in active_datasets:
                        if attempts >= len(unique_ids):
                            break
                        continue
                    task = worker.enqueue(
                        dataset_id,
                        reason="SOAK_STABILITY",
                        priority=-100,
                        max_attempts=2,
                    )
                    if task.get("status") in _ACTIVE:
                        active_datasets.add(dataset_id)
                    if task.get("created"):
                        created_tasks += 1
                    feeding = time.monotonic() < feed_deadline and (
                        max_tasks is None or created_tasks < max_tasks
                    )
                queue_peak = max(queue_peak, len(active_datasets))

                if not active_datasets:
                    if not feeding:
                        break
                    time.sleep(0.02)
                    continue

                outcome = worker.process_next()
                if outcome is None:
                    if drain_deadline is not None and time.monotonic() >= drain_deadline:
                        break
                    time.sleep(0.05)
                    continue
                run = database.get_ai_run(outcome.run_id)
                task = database.get_ai_task(outcome.task_id)
                if run is None or task is None:
                    raise RuntimeError("soak worker outcome was not durably persisted")
                run_statuses[str(run["status"])] += 1
                if run.get("error_code"):
                    error_codes[str(run["error_code"])] += 1
                if run.get("latency_ms") is not None:
                    latencies.append(int(run["latency_ms"]))
                task_status = str(task["status"])
                if task_status in _TERMINAL:
                    terminal_tasks += 1
                    task_statuses[task_status] += 1
                    active_datasets.discard(str(task["dataset_id"]))
                    if (
                        progress_callback
                        and terminal_tasks % checkpoint_every == 0
                    ):
                        progress_callback(
                            _progress(
                                started_at=started_at,
                                created_tasks=created_tasks,
                                terminal_tasks=terminal_tasks,
                                active_tasks=len(active_datasets),
                                run_statuses=run_statuses,
                                task_statuses=task_statuses,
                                restarts=restarts,
                                queue_peak=queue_peak,
                            )
                        )

                if (
                    restart_after_tasks is not None
                    and restarts == 0
                    and terminal_tasks >= restart_after_tasks
                ):
                    worker.close()
                    worker = worker_factory()
                    health = worker.health()
                    if not health.get("available"):
                        raise RuntimeError("local model provider is unavailable after worker restart")
                    model_registry_ids.add(str(worker.registered_model["id"]))
                    restarts += 1

                if drain_deadline is not None and time.monotonic() >= drain_deadline:
                    break

            end_health = worker.health()
            peak_gpu_mib = int(getattr(gpu, "peak_mib", 0) if gpu is not None else 0)
            gpu_samples = list(getattr(gpu, "samples", ()) if gpu is not None else ())
    finally:
        worker.close()

    elapsed = time.monotonic() - started_at
    after_digest = _protected_state_digest(database, unique_ids)
    active_remaining = database.active_ai_task_count()
    gpu_total_mib = _gpu_total_mib() if sample_gpu else 0
    gpu_memory_trend = _gpu_memory_trend(gpu_samples, gpu_total_mib) if sample_gpu else {
        "available": False,
        "sample_count": 0,
        "warmup_samples": 0,
        "window_samples": 0,
        "early_steady_median_mib": None,
        "final_steady_median_mib": None,
        "growth_mib": None,
        "allowed_growth_mib": 0,
    }
    latency_p50 = int(statistics.median(latencies)) if latencies else 0
    latency_p95 = _percentile(latencies, 0.95)
    completion_target_met = (
        terminal_tasks >= max_tasks
        if max_tasks is not None
        else elapsed >= duration_seconds * 0.99
    )
    restart_gate = restart_after_tasks is None or restarts == 1
    gates = {
        "duration_or_task_target_met": completion_target_met,
        "all_tasks_accounted_for": created_tasks == terminal_tasks + active_remaining,
        "queue_drained": active_remaining == 0,
        "no_terminal_failures": task_statuses.get("FAILED", 0) == 0,
        "no_failed_runs": run_statuses.get("FAILED", 0) == 0,
        "protected_dataset_state_unchanged": before_digest == after_digest,
        "registered_model_stable_across_restart": len(model_registry_ids) == 1,
        "worker_restart_completed": restart_gate,
        "provider_available_at_end": bool(end_health and end_health.get("available")),
        "latency_p95_within_30_seconds": latency_p95 <= 30000,
        "gpu_metrics_available": (not sample_gpu) or bool(gpu_memory_trend["available"]),
        "gpu_peak_below_90_percent": (not sample_gpu) or (
            gpu_total_mib > 0 and peak_gpu_mib <= int(gpu_total_mib * 0.9)
        ),
        "gpu_steady_state_growth_bounded": (not sample_gpu) or (
            bool(gpu_memory_trend["available"])
            and int(gpu_memory_trend["growth_mib"]) <= int(gpu_memory_trend["allowed_growth_mib"])
        ),
    }
    return {
        "report_schema_version": 1,
        "kind": "academic-vault-durable-ai-soak",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "duration_target_seconds": duration_seconds,
        "elapsed_seconds": round(elapsed, 3),
        "max_tasks": max_tasks,
        "case_count": len(unique_ids),
        "queue_depth": queue_depth,
        "created_tasks": created_tasks,
        "terminal_tasks": terminal_tasks,
        "active_tasks_remaining": active_remaining,
        "worker_restarts": restarts,
        "queue_peak": queue_peak,
        "run_statuses": dict(sorted(run_statuses.items())),
        "task_statuses": dict(sorted(task_statuses.items())),
        "error_codes": dict(sorted(error_codes.items())),
        "latency_p50_ms": latency_p50,
        "latency_p95_ms": latency_p95,
        "peak_gpu_memory_mib": peak_gpu_mib,
        "gpu_total_memory_mib": gpu_total_mib,
        "gpu_memory_trend": gpu_memory_trend,
        "protected_state_before_sha256": before_digest,
        "protected_state_after_sha256": after_digest,
        "model_registry_ids": sorted(model_registry_ids),
        "provider_health_start": start_health,
        "provider_health_end": end_health,
        "gates": gates,
        "passed": all(gates.values()),
    }


__all__ = ["run_worker_soak"]
