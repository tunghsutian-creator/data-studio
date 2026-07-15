"""Persistent single-concurrency worker for local AI suggestions."""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import httpx

from ..database import Database
from .contracts import AI_OUTPUT_SCHEMA_VERSION, PROMPT_VERSION
from .evidence import EvidenceBuildError, EvidenceBuilder
from .model_lock import load_model_lock
from .provider import (
    LlamaCppProvider,
    LocalModelProfile,
    LocalModelProvider,
    ProviderError,
    ProviderIdentity,
    TAXONOMY_VERSION,
)


logger = logging.getLogger(__name__)


class InputChanged(RuntimeError):
    code = "INPUT_CHANGED"
    retryable = False


class ProviderIdentityMismatch(RuntimeError):
    code = "PROVIDER_IDENTITY_MISMATCH"
    retryable = False


@dataclass(frozen=True, slots=True)
class WorkerOutcome:
    task_id: str
    run_id: str
    task_status: str
    run_status: str
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class LockedLlamaProvider:
    profile: LocalModelProfile
    provider: LlamaCppProvider
    registry_config: Mapping[str, Any]


def load_locked_llama_provider(
    profile_path: str | Path,
    model_lock_path: str | Path,
    *,
    timeout_seconds: float = 120,
    transport: httpx.BaseTransport | None = None,
) -> LockedLlamaProvider:
    profile = LocalModelProfile.load(profile_path)
    model_lock = load_model_lock(model_lock_path)
    if model_lock.profile_id != profile.profile_id:
        raise ValueError("model lock profile_id does not match the local model profile")
    expected_contracts = {
        "prompt_version": PROMPT_VERSION,
        "taxonomy_version": TAXONOMY_VERSION,
        "output_schema_version": AI_OUTPUT_SCHEMA_VERSION,
    }
    actual_contracts = model_lock.contracts.model_dump(mode="json")
    if actual_contracts != expected_contracts:
        raise ValueError("model lock contracts do not match the running application")
    identity = ProviderIdentity(
        provider=profile.provider,
        profile_id=profile.profile_id,
        model_id=profile.model_id,
        quantization=profile.quantization,
        device=profile.device,
        model_revision=model_lock.model.revision,
        runtime_release=model_lock.runtime.release or "unreleased",
        runtime_commit=model_lock.runtime.commit,
        prompt_version=model_lock.contracts.prompt_version,
        taxonomy_version=model_lock.contracts.taxonomy_version,
        output_schema_version=model_lock.contracts.output_schema_version,
    )
    registry_config = {
        "context_tokens": profile.context_tokens,
        "evidence": {
            "max_image_bytes": profile.max_image_bytes,
            "max_image_edge": profile.max_image_edge,
            "max_images": profile.max_images,
            "max_text_assets": profile.max_text_assets,
            "max_text_bytes": profile.max_text_bytes,
        },
        "flash_attention": profile.flash_attention,
        "max_output_tokens": profile.max_output_tokens,
        "model_sha256": model_lock.model.sha256,
        "parallel_requests": profile.parallel_requests,
        "seed": profile.seed,
        "temperature": profile.temperature,
        "vision_projector_sha256": model_lock.vision_projector.sha256,
    }
    return LockedLlamaProvider(
        profile=profile,
        provider=LlamaCppProvider(
            profile,
            timeout_seconds=timeout_seconds,
            transport=transport,
            identity=identity,
        ),
        registry_config=registry_config,
    )


class _LeaseHeartbeat:
    def __init__(
        self,
        database: Database,
        task_id: str,
        worker_id: str,
        lease_seconds: int,
    ) -> None:
        self.database = database
        self.task_id = task_id
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.lost = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _run(self) -> None:
        interval = max(1.0, self.lease_seconds / 3)
        while not self._stop.wait(interval):
            try:
                self.database.heartbeat_ai_task(
                    self.task_id,
                    self.worker_id,
                    lease_seconds=self.lease_seconds,
                )
            except Exception:
                self.lost = True
                return

    def __enter__(self) -> "_LeaseHeartbeat":
        self._thread = threading.Thread(
            target=self._run,
            name="academic-vault-ai-lease",
            daemon=True,
        )
        self._thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)


class AIWorker:
    """Run one local model request at a time without mutating dataset state."""

    def __init__(
        self,
        database: Database,
        evidence_builder: EvidenceBuilder,
        provider: LocalModelProvider,
        *,
        registry_config: Mapping[str, Any] | None = None,
        worker_id: str | None = None,
        lease_seconds: int = 180,
        base_retry_delay_seconds: int = 5,
    ) -> None:
        if not 5 <= lease_seconds <= 3600:
            raise ValueError("lease_seconds must be between 5 and 3600")
        if not 0 <= base_retry_delay_seconds <= 3600:
            raise ValueError("base_retry_delay_seconds must be between 0 and 3600")
        self.database = database
        self.evidence_builder = evidence_builder
        self.provider = provider
        self.worker_id = worker_id or f"worker-{uuid.uuid4()}"
        self.lease_seconds = lease_seconds
        self.base_retry_delay_seconds = base_retry_delay_seconds
        self.registered_model = database.register_model(
            provider.identity.to_dict(),
            config=registry_config or {},
        )

    def close(self) -> None:
        self.provider.close()

    def health(self) -> dict[str, Any]:
        return self.provider.health().to_dict()

    def enqueue(
        self,
        dataset_id: str,
        *,
        reason: str,
        priority: int = 100,
        max_attempts: int = 2,
        reuse_completed: bool = False,
    ) -> dict[str, Any]:
        package = self.evidence_builder.build(dataset_id)
        return self.database.enqueue_ai_task(
            dataset_id,
            package.request.input_fingerprint,
            reason=reason,
            priority=priority,
            max_attempts=max_attempts,
            reuse_completed_model_id=(
                str(self.registered_model["id"]) if reuse_completed else None
            ),
        )

    def _retry_delay(self, attempt_count: int) -> int:
        if self.base_retry_delay_seconds == 0:
            return 0
        return min(3600, self.base_retry_delay_seconds * (2 ** max(0, attempt_count - 1)))

    def _record_failure(
        self,
        task: Mapping[str, Any],
        run_id: str,
        error: Exception,
        *,
        code: str,
        retryable: bool,
        latency_ms: int | None = None,
    ) -> WorkerOutcome:
        run = self.database.fail_ai_run(
            run_id,
            self.worker_id,
            error_code=code,
            error_detail=str(error),
            retryable=retryable,
            latency_ms=latency_ms,
            retry_delay_seconds=self._retry_delay(int(task["attempt_count"])),
        )
        current = self.database.get_ai_task(str(task["id"]))
        return WorkerOutcome(
            task_id=str(task["id"]),
            run_id=run_id,
            task_status=str(current["status"] if current else "MISSING"),
            run_status=str(run["status"]),
            error_code=code,
        )

    def process_next(self) -> WorkerOutcome | None:
        task = self.database.claim_next_ai_task(
            self.worker_id,
            lease_seconds=self.lease_seconds,
        )
        if task is None:
            return None
        run = self.database.start_ai_run(
            str(task["id"]),
            str(self.registered_model["id"]),
            self.worker_id,
            request_fingerprint=str(task["input_fingerprint"]),
        )
        heartbeat = _LeaseHeartbeat(
            self.database,
            str(task["id"]),
            self.worker_id,
            self.lease_seconds,
        )
        try:
            with heartbeat:
                package = self.evidence_builder.build(str(task["dataset_id"]))
                if package.request.input_fingerprint != task["input_fingerprint"]:
                    raise InputChanged("dataset evidence changed after the task was queued")
                result = self.provider.analyze(package.request)
            if heartbeat.lost:
                return WorkerOutcome(
                    str(task["id"]),
                    str(run["id"]),
                    "LEASE_LOST",
                    "RUNNING",
                    "WORKER_LEASE_LOST",
                )
            if result.identity != self.provider.identity:
                raise ProviderIdentityMismatch("provider returned an unexpected identity")
            completed = self.database.complete_ai_run(
                str(run["id"]),
                self.worker_id,
                classification=result.classification,
                response_sha256=result.response_sha256,
                latency_ms=result.latency_ms,
            )
            current = self.database.get_ai_task(str(task["id"]))
            return WorkerOutcome(
                task_id=str(task["id"]),
                run_id=str(run["id"]),
                task_status=str(current["status"] if current else "MISSING"),
                run_status=str(completed["status"]),
            )
        except EvidenceBuildError as exc:
            return self._record_failure(
                task,
                str(run["id"]),
                exc,
                code=exc.code,
                retryable=exc.retryable,
            )
        except ProviderError as exc:
            return self._record_failure(
                task,
                str(run["id"]),
                exc,
                code=exc.code,
                retryable=exc.retryable,
                latency_ms=exc.latency_ms,
            )
        except (InputChanged, ProviderIdentityMismatch) as exc:
            return self._record_failure(
                task,
                str(run["id"]),
                exc,
                code=exc.code,
                retryable=exc.retryable,
            )
        except Exception as exc:
            logger.exception("Unexpected local AI worker failure")
            return self._record_failure(
                task,
                str(run["id"]),
                RuntimeError(type(exc).__name__),
                code="WORKER_INTERNAL_ERROR",
                retryable=False,
            )

    def run_until_idle(self, *, max_tasks: int = 100) -> list[WorkerOutcome]:
        if max_tasks < 1:
            raise ValueError("max_tasks must be positive")
        outcomes: list[WorkerOutcome] = []
        for _ in range(max_tasks):
            outcome = self.process_next()
            if outcome is None:
                break
            outcomes.append(outcome)
        return outcomes


class AIWorkerService:
    """Own an AI worker thread and wake it when durable work is enqueued."""

    def __init__(self, worker: AIWorker, *, poll_seconds: float = 1.0) -> None:
        if not 0.1 <= poll_seconds <= 60:
            raise ValueError("poll_seconds must be between 0.1 and 60")
        self.worker = worker
        self.poll_seconds = poll_seconds
        self.last_outcome: WorkerOutcome | None = None
        self.last_error: str | None = None
        self._stop = threading.Event()
        self._wakeup = threading.Event()
        self._thread: threading.Thread | None = None
        self._closed = False

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> None:
        if self._closed:
            raise RuntimeError("AI worker service is closed")
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="academic-vault-ai-worker",
            daemon=True,
        )
        self._thread.start()

    def wake(self) -> None:
        self._wakeup.set()

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    outcome = self.worker.process_next()
                    if outcome is not None:
                        self.last_outcome = outcome
                        self.last_error = None
                        continue
                except Exception as exc:
                    self.last_error = type(exc).__name__
                    logger.exception("Local AI worker service iteration failed")
                self._wakeup.wait(self.poll_seconds)
                self._wakeup.clear()
        finally:
            self.worker.close()
            self._closed = True

    def stop(self, *, timeout_seconds: float = 5.0) -> bool:
        if timeout_seconds < 0:
            raise ValueError("timeout_seconds may not be negative")
        self._stop.set()
        self._wakeup.set()
        thread = self._thread
        if thread is None:
            if not self._closed:
                self.worker.close()
                self._closed = True
            return True
        thread.join(timeout=timeout_seconds)
        return not thread.is_alive()


__all__ = [
    "AIWorker",
    "AIWorkerService",
    "InputChanged",
    "LockedLlamaProvider",
    "ProviderIdentityMismatch",
    "WorkerOutcome",
    "load_locked_llama_provider",
]
