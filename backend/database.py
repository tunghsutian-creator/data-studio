from __future__ import annotations

import json
import hashlib
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

from .migrations import MigrationReport, run_migrations
from .paths import RootMapper


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("value must be JSON serializable") from exc


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ERROR_CODE = re.compile(r"^[A-Za-z0-9_.:-]{1,100}$")
_WINDOWS_PATH = re.compile(r"(?i)(?:[a-z]:\\|\\\\)[^\s,;]+")
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|authorization|password|secret|access[_-]?token|refresh[_-]?token)\s*[:=]\s*[^\s,;]+"
)
_MODEL_IDENTITY_FIELDS = (
    "provider",
    "profile_id",
    "model_id",
    "quantization",
    "device",
    "model_revision",
    "runtime_release",
    "runtime_commit",
    "prompt_version",
    "taxonomy_version",
    "output_schema_version",
)
_MODEL_IDENTITY_LIMITS = {
    "provider": 100,
    "profile_id": 200,
    "model_id": 500,
    "quantization": 100,
    "device": 200,
    "model_revision": 200,
    "runtime_release": 200,
    "runtime_commit": 200,
    "prompt_version": 200,
    "taxonomy_version": 200,
    "output_schema_version": 200,
}
_SECRET_CONFIG_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "password",
    "secret",
    "access_token",
    "refresh_token",
    "bearer",
}


def _require_sha256(value: str, name: str) -> str:
    digest = str(value)
    if not _SHA256.fullmatch(digest):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return digest


def _bounded_text(value: Any, name: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be text")
    text = value.strip()
    if not text or len(text) > maximum or any(ord(char) < 32 for char in text):
        raise ValueError(f"{name} must contain 1 to {maximum} printable characters")
    return text


def _sanitize_error_detail(value: str | None) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    text = _WINDOWS_PATH.sub("<local-path>", text)
    text = _SECRET_ASSIGNMENT.sub(lambda match: f"{match.group(1)}=<redacted>", text)
    return text[:1000] or None


def _assert_public_config(value: Any, *, path: str = "config") -> None:
    if isinstance(value, Mapping):
        for raw_key, nested in value.items():
            key = str(raw_key).strip().lower().replace("-", "_")
            if key in _SECRET_CONFIG_KEYS or key.endswith("_password") or key.endswith("_secret"):
                raise ValueError(f"{path} may not contain credentials")
            _assert_public_config(nested, path=f"{path}.{raw_key}")
    elif isinstance(value, (list, tuple)):
        for index, nested in enumerate(value):
            _assert_public_config(nested, path=f"{path}[{index}]")


def _deadline(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec="seconds")


class Database:
    """Small, thread-safe SQLite catalog using one connection per operation."""

    def __init__(
        self,
        path: str | Path,
        *,
        root_mappings: Mapping[str, str | Path] | None = None,
        backup_root: str | Path | None = None,
        library_name: str = "Academic Vault Windows Library",
        machine_profile: str = "windows-default",
        device_id: str | None = None,
    ):
        self.path = Path(path).expanduser().resolve(strict=False)
        self.root_mapper = RootMapper(root_mappings or {})
        self.backup_root = Path(backup_root).expanduser().resolve(strict=False) if backup_root else None
        self.library_name = library_name
        self.machine_profile = machine_profile
        self.device_id = device_id
        self.last_migration_report: MigrationReport | None = None

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        self.last_migration_report = run_migrations(
            self.path,
            root_mappings=self.root_mapper.roots,
            backup_root=self.backup_root,
            library_name=self.library_name,
            machine_profile=self.machine_profile,
            device_id=self.device_id,
        )

    def metadata(self, key: str) -> str | None:
        with self.connect() as connection:
            row = connection.execute("SELECT value FROM app_metadata WHERE key=?", (key,)).fetchone()
        return str(row[0]) if row else None

    def schema_version(self) -> int:
        return int(self.metadata("schema_version") or 0)

    def library_id(self, connection: sqlite3.Connection | None = None) -> str:
        if connection is not None:
            row = connection.execute("SELECT value FROM app_metadata WHERE key='library_id'").fetchone()
            if row:
                return str(row[0])
        value = self.metadata("library_id")
        if not value:
            raise RuntimeError("catalog has no stable library identity")
        return value

    def journal_mode(self) -> str:
        with self.connect() as connection:
            return str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()

    def create_job(self, kind: str, source: str | None = None) -> dict[str, Any]:
        job_id = str(uuid.uuid4())
        now = utc_now()
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO ingest_jobs(id,kind,source,status,created_at) VALUES(?,?,?,?,?)",
                (job_id, kind, source, "QUEUED", now),
            )
        return self.get_job(job_id) or {"id": job_id, "status": "QUEUED"}

    def update_job(
        self,
        job_id: str,
        *,
        status: str | None = None,
        current: int | None = None,
        total: int | None = None,
        message: str | None = None,
        error: str | None = None,
    ) -> None:
        updates: list[str] = []
        values: list[Any] = []
        for column, value in (("status", status), ("progress_current", current), ("progress_total", total), ("message", message), ("error", error)):
            if value is not None:
                updates.append(f"{column}=?")
                values.append(value)
        if status == "RUNNING":
            updates.append("started_at=COALESCE(started_at,?)")
            values.append(utc_now())
        if status in {"COMPLETED", "FAILED", "CANCELLED"}:
            updates.append("finished_at=?")
            values.append(utc_now())
        if not updates:
            return
        values.append(job_id)
        with self.transaction() as connection:
            connection.execute(f"UPDATE ingest_jobs SET {', '.join(updates)} WHERE id=?", values)

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM ingest_jobs WHERE id=?", (job_id,)).fetchone()
        return self._job_dict(row) if row else None

    def list_jobs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM ingest_jobs ORDER BY created_at DESC LIMIT ?", (max(1, min(limit, 200)),)
            ).fetchall()
        return [self._job_dict(row) for row in rows]

    def active_job_count(self) -> int:
        with self.connect() as connection:
            return int(
                connection.execute(
                    "SELECT COUNT(*) FROM ingest_jobs WHERE status IN ('QUEUED','RUNNING')"
                ).fetchone()[0]
            )

    def recover_interrupted_jobs(self) -> int:
        now = utc_now()
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE ingest_jobs
                SET status='FAILED',error='Interrupted by previous service shutdown',finished_at=?
                WHERE status IN ('QUEUED','RUNNING')
                """,
                (now,),
            )
        return int(cursor.rowcount)

    def register_model(
        self,
        identity: Mapping[str, Any],
        *,
        config: Mapping[str, Any] | None = None,
        enabled: bool = True,
    ) -> dict[str, Any]:
        """Register an immutable, reproducible local-model identity.

        The deterministic id includes both versioned identity fields and the
        public inference configuration. Credentials are rejected rather than
        being persisted in the catalog.
        """

        normalized: dict[str, str] = {}
        for field in _MODEL_IDENTITY_FIELDS:
            normalized[field] = _bounded_text(
                identity.get(field), field, _MODEL_IDENTITY_LIMITS[field]
            )
        public_config = dict(config or {})
        _assert_public_config(public_config)
        config_json = _canonical_json(public_config)
        if len(config_json.encode("utf-8")) > 32 * 1024:
            raise ValueError("model config may not exceed 32 KiB")
        registry_id = hashlib.sha256(
            _canonical_json({"identity": normalized, "config": public_config}).encode("utf-8")
        ).hexdigest()
        now = utc_now()
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO model_registry(
                    id,provider,profile_id,model_id,quantization,device,
                    model_revision,runtime_release,runtime_commit,prompt_version,
                    taxonomy_version,output_schema_version,config_json,enabled,
                    created_at,last_seen_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    enabled=excluded.enabled,last_seen_at=excluded.last_seen_at
                """,
                (
                    registry_id,
                    *(normalized[field] for field in _MODEL_IDENTITY_FIELDS),
                    config_json,
                    int(bool(enabled)),
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM model_registry WHERE id=?", (registry_id,)
            ).fetchone()
        if row is None:  # pragma: no cover - protected by the transaction
            raise RuntimeError("model registration did not persist")
        return self._model_registry_dict(row)

    def get_registered_model(self, registry_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM model_registry WHERE id=?", (registry_id,)
            ).fetchone()
        return self._model_registry_dict(row) if row else None

    def list_registered_models(self, *, enabled_only: bool = False) -> list[dict[str, Any]]:
        where = "WHERE enabled=1" if enabled_only else ""
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM model_registry {where} ORDER BY last_seen_at DESC,id"
            ).fetchall()
        return [self._model_registry_dict(row) for row in rows]

    @staticmethod
    def _model_registry_dict(row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
        item = dict(row)
        item["enabled"] = bool(item["enabled"])
        item["config"] = json.loads(item.pop("config_json"))
        return item

    def enqueue_ai_task(
        self,
        dataset_id: str,
        input_fingerprint: str,
        *,
        reason: str,
        priority: int = 100,
        max_attempts: int = 2,
    ) -> dict[str, Any]:
        fingerprint = _require_sha256(input_fingerprint, "input_fingerprint")
        reason_text = _bounded_text(reason, "reason", 200)
        if isinstance(priority, bool) or not isinstance(priority, int) or not -1000 <= priority <= 1000:
            raise ValueError("priority must be an integer between -1000 and 1000")
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or not 1 <= max_attempts <= 10:
            raise ValueError("max_attempts must be an integer between 1 and 10")
        now = utc_now()
        created = False
        with self.transaction() as connection:
            if not connection.execute("SELECT 1 FROM datasets WHERE id=?", (dataset_id,)).fetchone():
                raise KeyError(dataset_id)
            row = connection.execute(
                """
                SELECT * FROM ai_tasks
                WHERE dataset_id=? AND input_fingerprint=?
                  AND status IN ('QUEUED','RUNNING','RETRY_WAIT')
                ORDER BY created_at,id LIMIT 1
                """,
                (dataset_id, fingerprint),
            ).fetchone()
            if row is None:
                task_id = str(uuid.uuid4())
                connection.execute(
                    """
                    INSERT INTO ai_tasks(
                        id,dataset_id,input_fingerprint,reason,status,priority,
                        attempt_count,max_attempts,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        task_id,
                        dataset_id,
                        fingerprint,
                        reason_text,
                        "QUEUED",
                        priority,
                        0,
                        max_attempts,
                        now,
                        now,
                    ),
                )
                row = connection.execute("SELECT * FROM ai_tasks WHERE id=?", (task_id,)).fetchone()
                created = True
        if row is None:  # pragma: no cover - protected by the transaction
            raise RuntimeError("AI task enqueue did not persist")
        item = self._ai_task_dict(row)
        item["created"] = created
        return item

    def get_ai_task(self, task_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM ai_tasks WHERE id=?", (task_id,)).fetchone()
        return self._ai_task_dict(row) if row else None

    def list_ai_tasks(
        self,
        *,
        dataset_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        where: list[str] = []
        values: list[Any] = []
        if dataset_id:
            where.append("dataset_id=?")
            values.append(dataset_id)
        if status:
            normalized_status = status.upper()
            if normalized_status not in {
                "QUEUED", "RUNNING", "RETRY_WAIT", "COMPLETED", "ABSTAINED", "FAILED", "CANCELLED"
            }:
                raise ValueError("unknown AI task status")
            where.append("status=?")
            values.append(normalized_status)
        clause = "WHERE " + " AND ".join(where) if where else ""
        actual_limit = max(1, min(int(limit), 500))
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM ai_tasks {clause} ORDER BY created_at DESC,id LIMIT ?",
                (*values, actual_limit),
            ).fetchall()
        return [self._ai_task_dict(row) for row in rows]

    def active_ai_task_count(self) -> int:
        with self.connect() as connection:
            return int(
                connection.execute(
                    "SELECT COUNT(*) FROM ai_tasks WHERE status IN ('QUEUED','RUNNING','RETRY_WAIT')"
                ).fetchone()[0]
            )

    def ai_task_counts(self) -> dict[str, int]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT status,COUNT(*) AS count FROM ai_tasks GROUP BY status"
            ).fetchall()
        counts = {str(row["status"]): int(row["count"]) for row in rows}
        counts["ACTIVE"] = sum(
            counts.get(status, 0) for status in ("QUEUED", "RUNNING", "RETRY_WAIT")
        )
        counts["TOTAL"] = sum(value for key, value in counts.items() if key not in {"ACTIVE", "TOTAL"})
        return counts

    @staticmethod
    def _ai_task_dict(row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
        return dict(row)

    @staticmethod
    def _recover_expired_ai_tasks(connection: sqlite3.Connection, now: str) -> int:
        rows = connection.execute(
            """
            SELECT * FROM ai_tasks
            WHERE status='RUNNING' AND lease_expires_at<=?
            ORDER BY lease_expires_at,id
            """,
            (now,),
        ).fetchall()
        for row in rows:
            connection.execute(
                """
                UPDATE ai_runs
                SET status='FAILED',error_code='WORKER_LEASE_EXPIRED',
                    error_detail='Worker lease expired before the attempt completed',
                    retryable=1,finished_at=?
                WHERE task_id=? AND status='RUNNING'
                """,
                (now, row["id"]),
            )
            if int(row["attempt_count"]) < int(row["max_attempts"]):
                connection.execute(
                    """
                    UPDATE ai_tasks
                    SET status='RETRY_WAIT',next_attempt_at=?,lease_owner=NULL,
                        lease_expires_at=NULL,last_error_code='WORKER_LEASE_EXPIRED',
                        last_error_detail='Worker lease expired before the attempt completed',
                        updated_at=?
                    WHERE id=? AND status='RUNNING'
                    """,
                    (now, now, row["id"]),
                )
            else:
                connection.execute(
                    """
                    UPDATE ai_tasks
                    SET status='FAILED',next_attempt_at=NULL,lease_owner=NULL,
                        lease_expires_at=NULL,last_error_code='WORKER_LEASE_EXPIRED',
                        last_error_detail='Worker lease expired before the attempt completed',
                        updated_at=?,finished_at=?
                    WHERE id=? AND status='RUNNING'
                    """,
                    (now, now, row["id"]),
                )
        return len(rows)

    def recover_ai_tasks(self) -> int:
        now = utc_now()
        with self.transaction() as connection:
            return self._recover_expired_ai_tasks(connection, now)

    def claim_next_ai_task(self, worker_id: str, *, lease_seconds: int = 120) -> dict[str, Any] | None:
        worker = _bounded_text(worker_id, "worker_id", 200)
        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int) or not 5 <= lease_seconds <= 3600:
            raise ValueError("lease_seconds must be an integer between 5 and 3600")
        now = utc_now()
        lease_expires_at = _deadline(lease_seconds)
        with self.transaction() as connection:
            self._recover_expired_ai_tasks(connection, now)
            candidate = connection.execute(
                """
                SELECT id FROM ai_tasks
                WHERE attempt_count < max_attempts
                  AND (status='QUEUED' OR (status='RETRY_WAIT' AND next_attempt_at<=?))
                ORDER BY priority DESC,created_at,id LIMIT 1
                """,
                (now,),
            ).fetchone()
            if candidate is None:
                return None
            cursor = connection.execute(
                """
                UPDATE ai_tasks
                SET status='RUNNING',attempt_count=attempt_count+1,
                    next_attempt_at=NULL,lease_owner=?,lease_expires_at=?,
                    started_at=COALESCE(started_at,?),updated_at=?
                WHERE id=? AND status IN ('QUEUED','RETRY_WAIT')
                  AND attempt_count < max_attempts
                """,
                (worker, lease_expires_at, now, now, candidate["id"]),
            )
            if cursor.rowcount != 1:  # pragma: no cover - BEGIN IMMEDIATE serializes claimers
                return None
            row = connection.execute("SELECT * FROM ai_tasks WHERE id=?", (candidate["id"],)).fetchone()
        return self._ai_task_dict(row) if row else None

    def heartbeat_ai_task(self, task_id: str, worker_id: str, *, lease_seconds: int = 120) -> dict[str, Any]:
        worker = _bounded_text(worker_id, "worker_id", 200)
        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int) or not 5 <= lease_seconds <= 3600:
            raise ValueError("lease_seconds must be an integer between 5 and 3600")
        now = utc_now()
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE ai_tasks SET lease_expires_at=?,updated_at=?
                WHERE id=? AND status='RUNNING' AND lease_owner=? AND lease_expires_at>?
                """,
                (_deadline(lease_seconds), now, task_id, worker, now),
            )
            if cursor.rowcount != 1:
                if not connection.execute("SELECT 1 FROM ai_tasks WHERE id=?", (task_id,)).fetchone():
                    raise KeyError(task_id)
                raise RuntimeError("AI task lease is not owned by this active worker")
            row = connection.execute("SELECT * FROM ai_tasks WHERE id=?", (task_id,)).fetchone()
        return self._ai_task_dict(row)

    def start_ai_run(
        self,
        task_id: str,
        model_registry_id: str,
        worker_id: str,
        *,
        request_fingerprint: str,
    ) -> dict[str, Any]:
        worker = _bounded_text(worker_id, "worker_id", 200)
        fingerprint = _require_sha256(request_fingerprint, "request_fingerprint")
        now = utc_now()
        with self.transaction() as connection:
            task = connection.execute("SELECT * FROM ai_tasks WHERE id=?", (task_id,)).fetchone()
            if task is None:
                raise KeyError(task_id)
            if task["status"] != "RUNNING" or task["lease_owner"] != worker:
                raise RuntimeError("AI task lease is not owned by this worker")
            if str(task["lease_expires_at"]) <= now:
                raise RuntimeError("AI task lease has expired")
            if task["input_fingerprint"] != fingerprint:
                raise ValueError("request_fingerprint must match the queued input")
            model = connection.execute(
                "SELECT enabled FROM model_registry WHERE id=?", (model_registry_id,)
            ).fetchone()
            if model is None:
                raise KeyError(model_registry_id)
            if not bool(model["enabled"]):
                raise RuntimeError("registered model is disabled")
            existing = connection.execute(
                "SELECT * FROM ai_runs WHERE task_id=? AND attempt_number=?",
                (task_id, task["attempt_count"]),
            ).fetchone()
            if existing is not None:
                if (
                    existing["status"] == "RUNNING"
                    and existing["model_registry_id"] == model_registry_id
                    and existing["request_fingerprint"] == fingerprint
                ):
                    return self._ai_run_dict(existing)
                raise RuntimeError("AI task attempt already has a different run")
            run_id = str(uuid.uuid4())
            connection.execute(
                """
                INSERT INTO ai_runs(
                    id,task_id,model_registry_id,attempt_number,status,
                    request_fingerprint,retryable,started_at
                ) VALUES(?,?,?,?,?,?,0,?)
                """,
                (
                    run_id,
                    task_id,
                    model_registry_id,
                    int(task["attempt_count"]),
                    "RUNNING",
                    fingerprint,
                    now,
                ),
            )
            row = connection.execute("SELECT * FROM ai_runs WHERE id=?", (run_id,)).fetchone()
        return self._ai_run_dict(row)

    def complete_ai_run(
        self,
        run_id: str,
        worker_id: str,
        *,
        classification: Mapping[str, Any] | Any,
        response_sha256: str,
        latency_ms: int,
    ) -> dict[str, Any]:
        worker = _bounded_text(worker_id, "worker_id", 200)
        response_digest = _require_sha256(response_sha256, "response_sha256")
        if hasattr(classification, "model_dump"):
            payload = classification.model_dump(mode="json")
        elif isinstance(classification, Mapping):
            payload = dict(classification)
        else:
            raise ValueError("classification must be a JSON object")
        classification_json = _canonical_json(payload)
        if len(classification_json.encode("utf-8")) > 64 * 1024:
            raise ValueError("classification may not exceed 64 KiB")
        if isinstance(latency_ms, bool) or not isinstance(latency_ms, int) or latency_ms < 0:
            raise ValueError("latency_ms must be a non-negative integer")
        run_status = "ABSTAINED" if str(payload.get("modality", "")).upper() == "UNKNOWN" else "SUCCEEDED"
        task_status = "ABSTAINED" if run_status == "ABSTAINED" else "COMPLETED"
        now = utc_now()
        with self.transaction() as connection:
            row = connection.execute(
                """
                SELECT r.*,t.status AS task_status,t.lease_owner
                FROM ai_runs r JOIN ai_tasks t ON t.id=r.task_id WHERE r.id=?
                """,
                (run_id,),
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            if row["status"] != "RUNNING":
                if (
                    row["status"] == run_status
                    and row["response_sha256"] == response_digest
                    and row["classification_json"] == classification_json
                ):
                    return self._ai_run_dict(row)
                raise RuntimeError("AI run is already terminal")
            if row["task_status"] != "RUNNING" or row["lease_owner"] != worker:
                raise RuntimeError("AI task lease is not owned by this worker")
            connection.execute(
                """
                UPDATE ai_runs
                SET status=?,response_sha256=?,classification_json=?,latency_ms=?,
                    error_code=NULL,error_detail=NULL,retryable=0,finished_at=?
                WHERE id=? AND status='RUNNING'
                """,
                (run_status, response_digest, classification_json, latency_ms, now, run_id),
            )
            connection.execute(
                """
                UPDATE ai_tasks
                SET status=?,next_attempt_at=NULL,lease_owner=NULL,lease_expires_at=NULL,
                    last_error_code=NULL,last_error_detail=NULL,updated_at=?,finished_at=?
                WHERE id=? AND status='RUNNING' AND lease_owner=?
                """,
                (task_status, now, now, row["task_id"], worker),
            )
            completed = connection.execute("SELECT * FROM ai_runs WHERE id=?", (run_id,)).fetchone()
        return self._ai_run_dict(completed)

    def fail_ai_run(
        self,
        run_id: str,
        worker_id: str,
        *,
        error_code: str,
        error_detail: str | None,
        retryable: bool,
        latency_ms: int | None = None,
        retry_delay_seconds: int = 0,
    ) -> dict[str, Any]:
        worker = _bounded_text(worker_id, "worker_id", 200)
        code = str(error_code).strip()
        if not _ERROR_CODE.fullmatch(code):
            raise ValueError("error_code contains unsupported characters")
        detail = _sanitize_error_detail(error_detail)
        if latency_ms is not None and (
            isinstance(latency_ms, bool) or not isinstance(latency_ms, int) or latency_ms < 0
        ):
            raise ValueError("latency_ms must be a non-negative integer or null")
        if (
            isinstance(retry_delay_seconds, bool)
            or not isinstance(retry_delay_seconds, int)
            or not 0 <= retry_delay_seconds <= 86400
        ):
            raise ValueError("retry_delay_seconds must be between 0 and 86400")
        now = utc_now()
        with self.transaction() as connection:
            row = connection.execute(
                """
                SELECT r.*,t.status AS task_status,t.lease_owner,
                       t.attempt_count,t.max_attempts
                FROM ai_runs r JOIN ai_tasks t ON t.id=r.task_id WHERE r.id=?
                """,
                (run_id,),
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            if row["status"] != "RUNNING":
                if row["status"] == "FAILED" and row["error_code"] == code:
                    return self._ai_run_dict(row)
                raise RuntimeError("AI run is already terminal")
            if row["task_status"] != "RUNNING" or row["lease_owner"] != worker:
                raise RuntimeError("AI task lease is not owned by this worker")
            connection.execute(
                """
                UPDATE ai_runs
                SET status='FAILED',latency_ms=?,error_code=?,error_detail=?,
                    retryable=?,finished_at=?
                WHERE id=? AND status='RUNNING'
                """,
                (latency_ms, code, detail, int(bool(retryable)), now, run_id),
            )
            may_retry = bool(retryable) and int(row["attempt_count"]) < int(row["max_attempts"])
            if may_retry:
                next_attempt = (
                    datetime.now(timezone.utc) + timedelta(seconds=retry_delay_seconds)
                ).isoformat(timespec="seconds")
                connection.execute(
                    """
                    UPDATE ai_tasks
                    SET status='RETRY_WAIT',next_attempt_at=?,lease_owner=NULL,
                        lease_expires_at=NULL,last_error_code=?,last_error_detail=?,updated_at=?
                    WHERE id=? AND status='RUNNING' AND lease_owner=?
                    """,
                    (next_attempt, code, detail, now, row["task_id"], worker),
                )
            else:
                connection.execute(
                    """
                    UPDATE ai_tasks
                    SET status='FAILED',next_attempt_at=NULL,lease_owner=NULL,
                        lease_expires_at=NULL,last_error_code=?,last_error_detail=?,
                        updated_at=?,finished_at=?
                    WHERE id=? AND status='RUNNING' AND lease_owner=?
                    """,
                    (code, detail, now, now, row["task_id"], worker),
                )
            failed = connection.execute("SELECT * FROM ai_runs WHERE id=?", (run_id,)).fetchone()
        return self._ai_run_dict(failed)

    def get_ai_run(self, run_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM ai_runs WHERE id=?", (run_id,)).fetchone()
        return self._ai_run_dict(row) if row else None

    def list_ai_runs(self, task_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        actual_limit = max(1, min(int(limit), 500))
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM ai_runs WHERE task_id=?
                ORDER BY attempt_number DESC,id LIMIT ?
                """,
                (task_id, actual_limit),
            ).fetchall()
        return [self._ai_run_dict(row) for row in rows]

    @staticmethod
    def _ai_run_dict(row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
        item = dict(row)
        item["retryable"] = bool(item["retryable"])
        raw_classification = item.pop("classification_json", None)
        item["classification"] = json.loads(raw_classification) if raw_classification else None
        item.pop("task_status", None)
        item.pop("lease_owner", None)
        item.pop("max_attempts", None)
        item.pop("attempt_count", None)
        return item

    @staticmethod
    def _job_dict(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        item = dict(row)
        item.update(
            {
                "startedAt": item.get("started_at") or item.get("created_at"),
                "finishedAt": item.get("finished_at"),
                "detected": item.get("progress_total", 0),
                "verified": item.get("progress_current", 0),
                "committed": item.get("progress_current", 0),
                "note": item.get("message") or item.get("error") or "",
                "statusCode": "complete" if item.get("status") == "COMPLETED" else "review",
            }
        )
        return item

    def upsert_scanned_file(
        self,
        *,
        source_kind: str,
        source_root: str,
        group_key: str,
        path: str,
        size_bytes: int,
        mtime_ns: int,
        modified_at: str,
        sha256: str,
        classification: dict[str, Any],
        canonical_name: str,
        role: str = "PRIMARY",
        mime_type: str | None = None,
    ) -> str:
        now = utc_now()
        metadata = classification.get("metadata") or {}
        modality = str(classification.get("label") or "UNKNOWN").upper()
        material = str(metadata.get("material") or metadata.get("material_state") or "UNKNOWN").upper()
        lifecycle = str(metadata.get("lifecycle") or metadata.get("data_level") or "UNKNOWN").upper()
        sample = metadata.get("sample") or metadata.get("sample_code")
        experiment_date = metadata.get("date") or metadata.get("experiment_date")
        workstream = str(metadata.get("workstream") or "UNASSIGNED").upper()
        confidence = float(classification.get("confidence") or 0.0)
        conflict = int(bool(classification.get("conflict")))
        method = str(classification.get("method") or "unknown")
        evidence = classification.get("evidence") or []
        requires_review = modality == "UNKNOWN" or bool(conflict) or confidence < 0.6
        initial_status = "INDEXED" if source_kind == "reference" and not requires_review else "REVIEW"
        path_obj = Path(path)
        if source_kind in self.root_mapper.roots:
            location = self.root_mapper.relativize(
                path_obj, allowed_keys={source_kind}, must_exist=False
            )
        else:
            location = RootMapper({source_kind: source_root}).relativize(
                path_obj, allowed_keys={source_kind}, must_exist=False
            )

        protected_statuses = {"REVIEWED", "ACCEPTED", "MANAGED", "COMMITTED", "DEFERRED", "STALE", "PATH_REVIEW"}
        with self.transaction() as connection:
            library_id = self.library_id(connection)
            existing_asset = connection.execute(
                """
                SELECT a.*,d.status AS dataset_status
                FROM assets a JOIN datasets d ON d.id=a.dataset_id
                WHERE a.original_path=?
                """,
                (path,),
            ).fetchone()
            keep_existing_group = bool(
                existing_asset
                and (existing_asset["managed_path"] or existing_asset["dataset_status"] in protected_statuses)
            )
            if keep_existing_group:
                row = connection.execute(
                    "SELECT id,status FROM datasets WHERE id=?", (existing_asset["dataset_id"],)
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT id,status FROM datasets WHERE source_kind=? AND group_key=?",
                    (source_kind, group_key),
                ).fetchone()
            if row:
                dataset_id = str(row["id"])
                if row["status"] not in protected_statuses:
                    connection.execute(
                        """
                        UPDATE datasets SET source_root=?, canonical_name=?, workstream=?, material_state=?, modality=?,
                            data_level=?, sample_code=COALESCE(?,sample_code),
                            experiment_date=COALESCE(?,experiment_date), confidence=?,
                            classification_method=?, conflict=?, status=?, revision=revision+1, updated_at=?
                        WHERE id=?
                        """,
                        (
                            source_root, canonical_name, workstream, material, modality, lifecycle, sample,
                            experiment_date, confidence, method, conflict, initial_status, now, dataset_id,
                        ),
                    )
            else:
                dataset_id = str(uuid.uuid4())
                connection.execute(
                    """
                    INSERT INTO datasets(
                        id,source_kind,group_key,source_root,canonical_name,workstream,
                        material_state,modality,data_level,sample_code,experiment_date,
                        confidence,classification_method,conflict,status,created_at,updated_at,
                        library_id,revision
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        dataset_id, source_kind, group_key, source_root, canonical_name,
                        workstream, material, modality, lifecycle, sample, experiment_date,
                        confidence, method, conflict, initial_status, now, now, library_id, 1,
                    ),
                )

            if existing_asset:
                previous_source_hash = existing_asset["source_sha256"] or existing_asset["sha256"]
                source_changed = bool(previous_source_hash and previous_source_hash != sha256)
                has_managed_copy = bool(existing_asset["managed_path"])
                display_hash = (existing_asset["managed_sha256"] or existing_asset["sha256"]) if has_managed_copy else sha256
                if source_changed:
                    hash_state = "STALE_SOURCE" if has_managed_copy else "SOURCE_CHANGED"
                elif has_managed_copy:
                    hash_state = existing_asset["hash_state"]
                else:
                    hash_state = "SOURCE_HASHED"
                old_dataset_id = str(existing_asset["dataset_id"])
                connection.execute(
                    """
                    UPDATE assets SET dataset_id=?,original_name=?,extension=?,size_bytes=?,
                        modified_at=?,mtime_ns=?,sha256=?,source_sha256=?,role=?,mime_type=?,hash_state=?,updated_at=?
                        ,original_root_key=?,original_relpath=?,path_state='VALID'
                    WHERE id=?
                    """,
                    (
                        dataset_id, path_obj.name, path_obj.suffix.lower(), size_bytes,
                        modified_at, mtime_ns, display_hash, sha256, role, mime_type,
                        hash_state, now, location.root_key, location.relative_path, existing_asset["id"],
                    ),
                )
                if source_changed and row and row["status"] in protected_statuses:
                    connection.execute(
                        "UPDATE datasets SET status='STALE',conflict=1,revision=revision+1,updated_at=? WHERE id=?",
                        (now, dataset_id),
                    )
                    connection.execute(
                        "INSERT INTO operation_log(dataset_id,action,detail_json,created_at) VALUES(?,?,?,?)",
                        (
                            dataset_id,
                            "SOURCE_CHANGED",
                            _json({"asset_id": existing_asset["id"], "previous_sha256": previous_source_hash, "source_sha256": sha256}),
                            now,
                        ),
                    )
                if old_dataset_id != dataset_id:
                    connection.execute(
                        """
                        DELETE FROM datasets WHERE id=? AND status IN ('INDEXED','REVIEW')
                        AND NOT EXISTS(SELECT 1 FROM assets WHERE dataset_id=datasets.id)
                        """,
                        (old_dataset_id,),
                    )
            else:
                connection.execute(
                    """
                    INSERT INTO assets(
                        id,dataset_id,original_path,original_name,extension,size_bytes,
                        modified_at,mtime_ns,sha256,source_sha256,role,mime_type,hash_state,created_at,updated_at,
                        original_root_key,original_relpath,path_state
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        str(uuid.uuid4()), dataset_id, path, path_obj.name,
                        path_obj.suffix.lower(), size_bytes, modified_at, mtime_ns,
                        sha256, sha256, role, mime_type, "SOURCE_HASHED", now, now,
                        location.root_key, location.relative_path, "VALID",
                    ),
                )
            connection.execute(
                """
                INSERT INTO classification_decisions(
                    dataset_id,predicted_label,proposed_metadata_json,confidence,method,
                    evidence_json,conflict,resolution,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (dataset_id, modality, _json(metadata), confidence, method, _json(evidence), conflict, "PREDICTED", now),
            )
        return dataset_id

    @staticmethod
    def _dataset_dict(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        item = dict(row)
        item["conflict"] = bool(item.get("conflict"))
        item["name"] = item.get("canonical_name")
        item["category"] = item.get("modality")
        item["type"] = item.get("extensions") or item.get("extension") or ""
        item["extension"] = (str(item.get("extensions") or "").split(",")[0] or item.get("extension") or "")
        item["size"] = item.get("size_bytes", 0)
        item["modified"] = item.get("updated_at")
        item["path"] = item.get("original_path")
        item["asset_count"] = item.get("file_count", 0)
        item["date"] = item.get("experiment_date") or str(item.get("updated_at") or "")[:10]
        return item

    def list_datasets(
        self,
        *,
        search: str | None = None,
        workstream: str | None = None,
        material_state: str | None = None,
        modality: str | None = None,
        status: str | None = None,
        extension: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        sort: str = "updated_at",
        order: str = "desc",
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        where: list[str] = []
        params: list[Any] = []
        if search:
            where.append("(d.canonical_name LIKE ? OR d.sample_code LIKE ? OR d.workstream LIKE ? OR a.original_name LIKE ? OR a.sha256 LIKE ? OR a.source_sha256 LIKE ?)")
            token = f"%{search}%"
            params.extend((token, token, token, token, token, token))
        for column, value in (("d.workstream", workstream), ("d.material_state", material_state), ("d.modality", modality), ("d.status", status)):
            if value:
                where.append(f"{column}=?")
                params.append(value.upper())
        if extension:
            where.append("a.extension=?")
            params.append(extension.lower() if extension.startswith(".") else f".{extension.lower()}")
        if date_from:
            where.append("COALESCE(d.experiment_date,d.updated_at)>=?")
            params.append(date_from)
        if date_to:
            where.append("COALESCE(d.experiment_date,d.updated_at)<=?")
            params.append(date_to)
        clause = "WHERE " + " AND ".join(where) if where else ""
        base = f"FROM datasets d LEFT JOIN assets a ON a.dataset_id=d.id {clause}"
        sort_columns = {
            "updated_at": "d.updated_at",
            "created_at": "d.created_at",
            "name": "d.canonical_name",
            "canonical_name": "d.canonical_name",
            "confidence": "d.confidence",
            "status": "d.status",
            "modality": "d.modality",
            "file_count": "file_count",
            "size_bytes": "size_bytes",
        }
        sort_sql = sort_columns.get(sort, "d.updated_at")
        order_sql = "ASC" if order.lower() == "asc" else "DESC"
        actual_limit = max(1, min(limit, 200))
        actual_offset = max(0, offset)
        with self.connect() as connection:
            total = int(connection.execute(f"SELECT COUNT(DISTINCT d.id) {base}", params).fetchone()[0])
            rows = connection.execute(
                f"""
                SELECT d.*,COUNT(DISTINCT a.id) AS file_count,COALESCE(SUM(a.size_bytes),0) AS size_bytes,
                       MIN(a.original_path) AS original_path,GROUP_CONCAT(DISTINCT a.extension) AS extensions
                {base}
                GROUP BY d.id ORDER BY {sort_sql} {order_sql},d.id ASC LIMIT ? OFFSET ?
                """,
                (*params, actual_limit, actual_offset),
            ).fetchall()
        return {
            "items": [self._dataset_dict(row) for row in rows],
            "total": total,
            "limit": actual_limit,
            "offset": actual_offset,
            "sort": sort,
            "order": order_sql.lower(),
        }

    def get_dataset(self, dataset_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT d.*,COUNT(DISTINCT a.id) AS file_count,COALESCE(SUM(a.size_bytes),0) AS size_bytes,
                       MIN(a.original_path) AS original_path,GROUP_CONCAT(DISTINCT a.extension) AS extensions
                FROM datasets d LEFT JOIN assets a ON a.dataset_id=d.id WHERE d.id=? GROUP BY d.id
                """,
                (dataset_id,),
            ).fetchone()
            if not row:
                return None
            assets = [dict(item) for item in connection.execute(
                "SELECT * FROM assets WHERE dataset_id=? ORDER BY original_name", (dataset_id,)
            ).fetchall()]
            decisions = [dict(item) for item in connection.execute(
                "SELECT * FROM classification_decisions WHERE dataset_id=? ORDER BY id DESC LIMIT 100", (dataset_id,)
            ).fetchall()]
            operations = [dict(item) for item in connection.execute(
                "SELECT * FROM operation_log WHERE dataset_id=? ORDER BY id DESC LIMIT 100", (dataset_id,)
            ).fetchall()]
        for decision in decisions:
            decision["conflict"] = bool(decision["conflict"])
            decision["proposed_metadata"] = json.loads(decision.pop("proposed_metadata_json"))
            decision["evidence"] = json.loads(decision.pop("evidence_json"))
        for operation in operations:
            operation["detail"] = json.loads(operation.pop("detail_json"))
        for asset in assets:
            asset["name"] = asset["original_name"]
            asset["filename"] = asset["original_name"]
            asset["hash_verified"] = asset["hash_state"] == "VERIFIED"
        result = self._dataset_dict(row)
        if assets:
            result["sha256"] = assets[0].get("sha256")
            result["hash_verified"] = all(asset["hash_verified"] for asset in assets)
        if decisions:
            result["evidence"] = decisions[0].get("evidence", [])
        result.update({"assets": assets, "decisions": decisions, "operations": operations})
        return result

    def update_dataset(self, dataset_id: str, changes: dict[str, Any]) -> dict[str, Any] | None:
        allowed = {"canonical_name", "workstream", "material_state", "modality", "data_level", "sample_code", "experiment_date"}
        updates: list[str] = []
        values: list[Any] = []
        for key, value in changes.items():
            if key in allowed and value is not None:
                updates.append(f"{key}=?")
                values.append(value.upper() if key in {"workstream", "material_state", "modality", "data_level"} else value)
        note = changes.get("note")
        if not updates and note is None:
            return self.get_dataset(dataset_id)
        now = utc_now()
        with self.transaction() as connection:
            exists = connection.execute("SELECT id FROM datasets WHERE id=?", (dataset_id,)).fetchone()
            if not exists:
                return None
            if updates:
                updates.extend(("status='REVIEWED'", "revision=revision+1", "updated_at=?"))
                values.extend((now, dataset_id))
                connection.execute(f"UPDATE datasets SET {', '.join(updates)} WHERE id=?", values)
            else:
                connection.execute(
                    "UPDATE datasets SET revision=revision+1,updated_at=? WHERE id=?",
                    (now, dataset_id),
                )
            connection.execute(
                """
                INSERT INTO classification_decisions(dataset_id,proposed_metadata_json,resolution,note,created_at)
                VALUES(?,?,?,?,?)
                """,
                (dataset_id, _json({key: value for key, value in changes.items() if key != "note"}), "MODIFIED", note, now),
            )
            connection.execute(
                "INSERT INTO operation_log(dataset_id,action,detail_json,created_at) VALUES(?,?,?,?)",
                (dataset_id, "MODIFY_METADATA", _json(changes), now),
            )
        return self.get_dataset(dataset_id)

    def mark_resolution(self, dataset_id: str, resolution: str, status: str, note: str | None = None) -> bool:
        now = utc_now()
        with self.transaction() as connection:
            cursor = connection.execute(
                "UPDATE datasets SET status=?,revision=revision+1,updated_at=? WHERE id=?", (status, now, dataset_id)
            )
            if cursor.rowcount == 0:
                return False
            connection.execute(
                "INSERT INTO classification_decisions(dataset_id,resolution,note,created_at) VALUES(?,?,?,?)",
                (dataset_id, resolution, note, now),
            )
            connection.execute(
                "INSERT INTO operation_log(dataset_id,action,detail_json,created_at) VALUES(?,?,?,?)",
                (dataset_id, resolution, _json({"status": status, "note": note}), now),
            )
        return True

    def set_managed_asset(
        self,
        asset_id: str,
        managed_path: str,
        sha256: str,
        managed_root_key: str,
        managed_relpath: str,
        job_id: str | None = None,
    ) -> None:
        now = utc_now()
        with self.transaction() as connection:
            row = connection.execute("SELECT dataset_id,managed_path,managed_sha256 FROM assets WHERE id=?", (asset_id,)).fetchone()
            if not row:
                raise KeyError(asset_id)
            connection.execute(
                """
                UPDATE assets SET managed_path=?,sha256=?,managed_sha256=?,hash_state='VERIFIED',
                    managed_root_key=?,managed_relpath=?,path_state='VALID',updated_at=?
                WHERE id=?
                """,
                (managed_path, sha256, sha256, managed_root_key, managed_relpath, now, asset_id),
            )
            connection.execute(
                "UPDATE datasets SET revision=revision+1,updated_at=? WHERE id=?",
                (now, row["dataset_id"]),
            )
            connection.execute(
                "INSERT INTO operation_log(dataset_id,job_id,action,detail_json,created_at) VALUES(?,?,?,?,?)",
                (
                    row["dataset_id"],
                    job_id,
                    "COPY_VERIFIED",
                    _json(
                        {
                            "asset_id": asset_id,
                            "managed_path": managed_path,
                            "managed_sha256": sha256,
                            "previous_managed_path": row["managed_path"],
                            "previous_managed_sha256": row["managed_sha256"],
                        }
                    ),
                    now,
                ),
            )

    def summary(self) -> dict[str, Any]:
        with self.connect() as connection:
            dataset_count = int(connection.execute("SELECT COUNT(*) FROM datasets").fetchone()[0])
            asset_count, total_bytes = connection.execute("SELECT COUNT(*),COALESCE(SUM(size_bytes),0) FROM assets").fetchone()
            status_rows = connection.execute("SELECT status,COUNT(*) AS n FROM datasets GROUP BY status").fetchall()
            modality_rows = connection.execute("SELECT modality,COUNT(*) AS n FROM datasets GROUP BY modality").fetchall()
            confidence_rows = connection.execute(
                """SELECT
                    SUM(CASE WHEN confidence>=0.9 THEN 1 ELSE 0 END) AS high,
                    SUM(CASE WHEN confidence>=0.6 AND confidence<0.9 THEN 1 ELSE 0 END) AS medium,
                    SUM(CASE WHEN confidence<0.6 THEN 1 ELSE 0 END) AS low
                    FROM datasets"""
            ).fetchone()
            month_prefix = utc_now()[:7]
            ingested_this_month = int(
                connection.execute(
                    "SELECT COUNT(*) FROM datasets WHERE status IN ('ACCEPTED','MANAGED','COMMITTED') AND substr(updated_at,1,7)=?",
                    (month_prefix,),
                ).fetchone()[0]
            )
        statuses = {row["status"]: row["n"] for row in status_rows}
        confidence = {"high": int(confidence_rows[0] or 0), "medium": int(confidence_rows[1] or 0), "low": int(confidence_rows[2] or 0)}
        gib = float(total_bytes) / (1024 ** 3)
        review_count = sum(value for key, value in statuses.items() if key in {"REVIEW", "REVIEWED", "STALE"})
        return {
            "dataset_count": dataset_count,
            "file_count": int(asset_count),
            "total_bytes": int(total_bytes),
            "review_count": review_count,
            "statuses": statuses,
            "modalities": {row["modality"]: row["n"] for row in modality_rows},
            "confidence": confidence,
            "datasets": dataset_count,
            "review": review_count,
            "storage": f"{gib:.2f} GB" if gib >= 0.01 else f"{int(total_bytes) / (1024 ** 2):.2f} MB",
            "high": confidence["high"],
            "medium": confidence["medium"],
            "low": confidence["low"],
            "ingested_this_month": ingested_this_month,
            "ingestedThisMonth": ingested_this_month,
        }

    def filters(self) -> dict[str, list[str]]:
        def values(connection: sqlite3.Connection, column: str, table: str = "datasets") -> list[str]:
            rows = connection.execute(
                f"SELECT DISTINCT {column} FROM {table} WHERE {column} IS NOT NULL AND {column}<>'' ORDER BY {column}"
            ).fetchall()
            return [str(row[0]) for row in rows]

        with self.connect() as connection:
            return {
                "workstreams": values(connection, "workstream"),
                "material_states": values(connection, "material_state"),
                "modalities": values(connection, "modality"),
                "statuses": values(connection, "status"),
                "extensions": values(connection, "extension", "assets"),
            }

    def add_operation(self, dataset_id: str | None, action: str, detail: dict[str, Any], job_id: str | None = None) -> None:
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO operation_log(dataset_id,job_id,action,detail_json,created_at) VALUES(?,?,?,?,?)",
                (dataset_id, job_id, action, _json(detail), utc_now()),
            )

    def list_rules(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM rules ORDER BY priority,id").fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["enabled"] = bool(item["enabled"])
            item["source"] = "user"
            result.append(item)
        return result

    def create_rule(self, payload: dict[str, Any]) -> dict[str, Any]:
        rule_id = str(uuid.uuid4())
        now = utc_now()
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO rules(id,name,pattern,label,priority,enabled,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (rule_id, payload["name"], payload["pattern"], payload["label"].upper(), payload.get("priority", 100), int(payload.get("enabled", True)), now, now),
            )
        return next(item for item in self.list_rules() if item["id"] == rule_id)

    def update_rule(self, rule_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        allowed = {"name", "pattern", "label", "priority", "enabled"}
        updates: list[str] = []
        values: list[Any] = []
        for key, value in payload.items():
            if key in allowed and value is not None:
                updates.append(f"{key}=?")
                if key == "enabled":
                    value = int(value)
                elif key == "label":
                    value = value.upper()
                values.append(value)
        if not updates:
            return next((item for item in self.list_rules() if item["id"] == rule_id), None)
        updates.extend(("version=version+1", "updated_at=?"))
        values.extend((utc_now(), rule_id))
        with self.transaction() as connection:
            cursor = connection.execute(f"UPDATE rules SET {', '.join(updates)} WHERE id=?", values)
            if cursor.rowcount == 0:
                return None
        return next(item for item in self.list_rules() if item["id"] == rule_id)
