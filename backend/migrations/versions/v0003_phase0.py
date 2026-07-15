from __future__ import annotations

import hashlib
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ...paths import PathLocationError, RootMapper


VERSION = 3
NAME = "windows library identity and portable paths"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _column_names(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def _metadata(connection: sqlite3.Connection, key: str) -> str | None:
    row = connection.execute("SELECT value FROM app_metadata WHERE key=?", (key,)).fetchone()
    return str(row[0]) if row else None


def _set_metadata(connection: sqlite3.Connection, key: str, value: str) -> None:
    connection.execute(
        "INSERT INTO app_metadata(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_existing(path: Path, *, size_bytes: int, expected_hash: str | None) -> None:
    if not path.exists():
        return
    if not path.is_file():
        raise RuntimeError(f"migration path is not a regular file: {path}")
    if path.stat().st_size != int(size_bytes):
        raise RuntimeError(f"migration size mismatch: {path}")
    if expected_hash and _sha256(path) != expected_hash.lower():
        raise RuntimeError(f"migration SHA-256 mismatch: {path}")


def apply(connection: sqlite3.Connection, context) -> None:
    now = _utc_now()
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS libraries (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            machine_profile TEXT NOT NULL,
            device_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    library_id = _metadata(connection, "library_id") or str(uuid.uuid4())
    device_id = _metadata(connection, "device_id") or context.device_id or str(uuid.uuid4())
    _set_metadata(connection, "library_id", library_id)
    _set_metadata(connection, "device_id", device_id)
    _set_metadata(connection, "catalog_revision", _metadata(connection, "catalog_revision") or "1")
    connection.execute(
        """
        INSERT INTO libraries(id,name,machine_profile,device_id,created_at,updated_at)
        VALUES(?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET
            name=excluded.name,
            machine_profile=excluded.machine_profile,
            device_id=excluded.device_id,
            updated_at=excluded.updated_at
        """,
        (library_id, context.library_name, context.machine_profile, device_id, now, now),
    )

    dataset_columns = _column_names(connection, "datasets")
    if "library_id" not in dataset_columns:
        connection.execute("ALTER TABLE datasets ADD COLUMN library_id TEXT")
    if "revision" not in dataset_columns:
        connection.execute("ALTER TABLE datasets ADD COLUMN revision INTEGER NOT NULL DEFAULT 1")
    connection.execute(
        "UPDATE datasets SET library_id=?,revision=MAX(COALESCE(revision,1),1)",
        (library_id,),
    )

    asset_columns = _column_names(connection, "assets")
    for name, sql_type in (
        ("original_root_key", "TEXT"),
        ("original_relpath", "TEXT"),
        ("managed_root_key", "TEXT"),
        ("managed_relpath", "TEXT"),
        ("path_state", "TEXT NOT NULL DEFAULT 'PENDING'"),
    ):
        if name not in asset_columns:
            connection.execute(f"ALTER TABLE assets ADD COLUMN {name} {sql_type}")

    mapper = RootMapper(context.root_mappings)
    unresolved_datasets: set[str] = set()
    rows = connection.execute(
        """
        SELECT a.*,d.source_kind
        FROM assets a JOIN datasets d ON d.id=a.dataset_id
        ORDER BY a.id
        """
    ).fetchall()
    for row in rows:
        original_key = original_relpath = managed_key = managed_relpath = None
        path_state = "VALID"
        try:
            original = mapper.relativize(
                row["original_path"],
                allowed_keys={str(row["source_kind"]).lower()},
                must_exist=False,
            )
            original_key, original_relpath = original.root_key, original.relative_path
            original_path = mapper.resolve(original_key, original_relpath, must_exist=False)
            _verify_existing(
                original_path,
                size_bytes=int(row["size_bytes"]),
                expected_hash=row["source_sha256"] or (row["sha256"] if not row["managed_path"] else None),
            )
        except PathLocationError:
            path_state = "PATH_REVIEW"

        if row["managed_path"]:
            try:
                managed = mapper.relativize(
                    row["managed_path"], allowed_keys={"vault"}, must_exist=False
                )
                managed_key, managed_relpath = managed.root_key, managed.relative_path
                managed_path = mapper.resolve(managed_key, managed_relpath, must_exist=False)
                if managed_path.exists() and row["managed_sha256"] and _sha256(managed_path) != str(row["managed_sha256"]).lower():
                    raise RuntimeError(f"migration managed SHA-256 mismatch: {managed_path}")
            except PathLocationError:
                path_state = "PATH_REVIEW"

        if path_state != "VALID":
            unresolved_datasets.add(str(row["dataset_id"]))
        connection.execute(
            """
            UPDATE assets SET
                original_root_key=?,original_relpath=?,managed_root_key=?,managed_relpath=?,path_state=?
            WHERE id=?
            """,
            (original_key, original_relpath, managed_key, managed_relpath, path_state, row["id"]),
        )

    if unresolved_datasets:
        placeholders = ",".join("?" for _ in unresolved_datasets)
        connection.execute(
            f"UPDATE datasets SET status='PATH_REVIEW',conflict=1,revision=revision+1,updated_at=? WHERE id IN ({placeholders})",
            (now, *sorted(unresolved_datasets)),
        )

    connection.execute("CREATE INDEX IF NOT EXISTS idx_datasets_library_updated ON datasets(library_id,updated_at DESC)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_assets_original_location ON assets(original_root_key,original_relpath)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_assets_managed_location ON assets(managed_root_key,managed_relpath)")
    connection.execute(
        """
        CREATE TRIGGER IF NOT EXISTS datasets_library_required_insert
        BEFORE INSERT ON datasets
        WHEN NEW.library_id IS NULL OR NEW.library_id=''
        BEGIN SELECT RAISE(ABORT,'datasets.library_id is required'); END
        """
    )
    connection.execute(
        """
        CREATE TRIGGER IF NOT EXISTS datasets_library_required_update
        BEFORE UPDATE OF library_id ON datasets
        WHEN NEW.library_id IS NULL OR NEW.library_id=''
        BEGIN SELECT RAISE(ABORT,'datasets.library_id is required'); END
        """
    )


class _Migration:
    version = VERSION
    name = NAME
    apply = staticmethod(apply)


migration = _Migration()
