from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class Database:
    """Small, thread-safe SQLite catalog using one connection per operation."""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().resolve(strict=False)

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
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            # DELETE is intentional: it is reliable on older SQLite builds and
            # OneDrive-backed Windows folders where WAL sidecars are fragile.
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS app_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS datasets (
                    id TEXT PRIMARY KEY,
                    source_kind TEXT NOT NULL CHECK(source_kind IN ('reference','inbox')),
                    group_key TEXT NOT NULL,
                    source_root TEXT NOT NULL,
                    canonical_name TEXT,
                    workstream TEXT NOT NULL DEFAULT 'UNASSIGNED',
                    material_state TEXT NOT NULL DEFAULT 'UNKNOWN',
                    modality TEXT NOT NULL DEFAULT 'UNKNOWN',
                    data_level TEXT NOT NULL DEFAULT 'UNKNOWN',
                    sample_code TEXT,
                    experiment_date TEXT,
                    confidence REAL NOT NULL DEFAULT 0.0,
                    classification_method TEXT NOT NULL DEFAULT 'unknown',
                    conflict INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'REVIEW',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(source_kind, group_key)
                );

                CREATE TABLE IF NOT EXISTS assets (
                    id TEXT PRIMARY KEY,
                    dataset_id TEXT NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
                    original_path TEXT NOT NULL UNIQUE,
                    managed_path TEXT,
                    original_name TEXT NOT NULL,
                    extension TEXT NOT NULL DEFAULT '',
                    size_bytes INTEGER NOT NULL DEFAULT 0,
                    modified_at TEXT,
                    mtime_ns INTEGER,
                    sha256 TEXT,
                    source_sha256 TEXT,
                    managed_sha256 TEXT,
                    role TEXT NOT NULL DEFAULT 'PRIMARY',
                    mime_type TEXT,
                    hash_state TEXT NOT NULL DEFAULT 'UNVERIFIED',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS classification_decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    dataset_id TEXT NOT NULL REFERENCES datasets(id) ON DELETE CASCADE,
                    predicted_label TEXT,
                    proposed_metadata_json TEXT NOT NULL DEFAULT '{}',
                    confidence REAL NOT NULL DEFAULT 0.0,
                    method TEXT NOT NULL DEFAULT 'unknown',
                    evidence_json TEXT NOT NULL DEFAULT '[]',
                    conflict INTEGER NOT NULL DEFAULT 0,
                    resolution TEXT NOT NULL DEFAULT 'PREDICTED',
                    note TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS ingest_jobs (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    source TEXT,
                    status TEXT NOT NULL,
                    progress_current INTEGER NOT NULL DEFAULT 0,
                    progress_total INTEGER NOT NULL DEFAULT 0,
                    message TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT
                );

                CREATE TABLE IF NOT EXISTS operation_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    dataset_id TEXT REFERENCES datasets(id) ON DELETE CASCADE,
                    job_id TEXT REFERENCES ingest_jobs(id) ON DELETE SET NULL,
                    action TEXT NOT NULL,
                    detail_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS rules (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    pattern TEXT NOT NULL,
                    label TEXT NOT NULL,
                    priority INTEGER NOT NULL DEFAULT 100,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_datasets_status ON datasets(status);
                CREATE INDEX IF NOT EXISTS idx_datasets_modality ON datasets(modality);
                CREATE INDEX IF NOT EXISTS idx_datasets_updated ON datasets(updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_assets_dataset ON assets(dataset_id);
                CREATE INDEX IF NOT EXISTS idx_assets_extension ON assets(extension);
                CREATE INDEX IF NOT EXISTS idx_decisions_dataset ON classification_decisions(dataset_id, id DESC);
                CREATE INDEX IF NOT EXISTS idx_operations_dataset ON operation_log(dataset_id, id DESC);
                """
            )
            asset_columns = {row[1] for row in connection.execute("PRAGMA table_info(assets)")}
            if "source_sha256" not in asset_columns:
                connection.execute("ALTER TABLE assets ADD COLUMN source_sha256 TEXT")
            if "managed_sha256" not in asset_columns:
                connection.execute("ALTER TABLE assets ADD COLUMN managed_sha256 TEXT")
            connection.execute("UPDATE assets SET source_sha256=COALESCE(source_sha256,sha256)")
            connection.execute(
                "UPDATE assets SET managed_sha256=COALESCE(managed_sha256,sha256) WHERE managed_path IS NOT NULL AND hash_state='VERIFIED'"
            )
            connection.execute(
                "INSERT OR REPLACE INTO app_metadata(key,value) VALUES('schema_version','2')"
            )

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

        protected_statuses = {"REVIEWED", "ACCEPTED", "MANAGED", "COMMITTED", "DEFERRED", "STALE"}
        with self.transaction() as connection:
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
                            classification_method=?, conflict=?, status=?, updated_at=?
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
                        confidence,classification_method,conflict,status,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        dataset_id, source_kind, group_key, source_root, canonical_name,
                        workstream, material, modality, lifecycle, sample, experiment_date,
                        confidence, method, conflict, initial_status, now, now,
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
                    WHERE id=?
                    """,
                    (
                        dataset_id, path_obj.name, path_obj.suffix.lower(), size_bytes,
                        modified_at, mtime_ns, display_hash, sha256, role, mime_type,
                        hash_state, now, existing_asset["id"],
                    ),
                )
                if source_changed and row and row["status"] in protected_statuses:
                    connection.execute(
                        "UPDATE datasets SET status='STALE',conflict=1,updated_at=? WHERE id=?",
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
                        modified_at,mtime_ns,sha256,source_sha256,role,mime_type,hash_state,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        str(uuid.uuid4()), dataset_id, path, path_obj.name,
                        path_obj.suffix.lower(), size_bytes, modified_at, mtime_ns,
                        sha256, sha256, role, mime_type, "SOURCE_HASHED", now, now,
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
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        where: list[str] = []
        params: list[Any] = []
        if search:
            where.append("(d.canonical_name LIKE ? OR d.sample_code LIKE ? OR a.original_name LIKE ?)")
            token = f"%{search}%"
            params.extend((token, token, token))
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
        with self.connect() as connection:
            total = int(connection.execute(f"SELECT COUNT(DISTINCT d.id) {base}", params).fetchone()[0])
            rows = connection.execute(
                f"""
                SELECT d.*,COUNT(DISTINCT a.id) AS file_count,COALESCE(SUM(a.size_bytes),0) AS size_bytes,
                       MIN(a.original_path) AS original_path,GROUP_CONCAT(DISTINCT a.extension) AS extensions
                {base}
                GROUP BY d.id ORDER BY d.updated_at DESC LIMIT ? OFFSET ?
                """,
                (*params, max(1, min(limit, 500)), max(0, offset)),
            ).fetchall()
        return {"items": [self._dataset_dict(row) for row in rows], "total": total, "limit": limit, "offset": offset}

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
                updates.extend(("status='REVIEWED'", "updated_at=?"))
                values.extend((now, dataset_id))
                connection.execute(f"UPDATE datasets SET {', '.join(updates)} WHERE id=?", values)
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
                "UPDATE datasets SET status=?,updated_at=? WHERE id=?", (status, now, dataset_id)
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

    def set_managed_asset(self, asset_id: str, managed_path: str, sha256: str, job_id: str | None = None) -> None:
        now = utc_now()
        with self.transaction() as connection:
            row = connection.execute("SELECT dataset_id,managed_path,managed_sha256 FROM assets WHERE id=?", (asset_id,)).fetchone()
            if not row:
                raise KeyError(asset_id)
            connection.execute(
                "UPDATE assets SET managed_path=?,sha256=?,managed_sha256=?,hash_state='VERIFIED',updated_at=? WHERE id=?",
                (managed_path, sha256, sha256, now, asset_id),
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
