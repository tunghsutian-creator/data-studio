from __future__ import annotations

import sqlite3
import uuid
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from .versions import MIGRATIONS


LATEST_SCHEMA_VERSION = max(item.version for item in MIGRATIONS)


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class MigrationContext:
    root_mappings: Mapping[str, str | Path] = field(default_factory=dict)
    backup_root: Path | None = None
    library_name: str = "Academic Vault Windows Library"
    machine_profile: str = "windows-default"
    device_id: str | None = None


@dataclass(frozen=True, slots=True)
class BackupVerification:
    path: Path
    source_integrity: str
    backup_integrity: str
    restore_integrity: str


@dataclass(frozen=True, slots=True)
class MigrationReport:
    previous_version: int
    current_version: int
    applied_versions: tuple[int, ...]
    backup: BackupVerification | None = None


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=30, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=30000")
    connection.execute("PRAGMA synchronous=FULL")
    return connection


def _integrity(connection: sqlite3.Connection) -> str:
    rows = [str(row[0]) for row in connection.execute("PRAGMA integrity_check").fetchall()]
    return "ok" if rows == ["ok"] else "; ".join(rows)


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }


def _current_version(connection: sqlite3.Connection) -> int:
    tables = _tables(connection)
    if not tables:
        return 0
    if "app_metadata" in tables:
        row = connection.execute(
            "SELECT value FROM app_metadata WHERE key='schema_version'"
        ).fetchone()
        if row:
            return int(row[0])
    if {"datasets", "assets"} <= tables:
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(assets)")}
        return 2 if {"source_sha256", "managed_sha256"} <= columns else 1
    raise MigrationError("catalog schema is not recognized")


def create_consistent_backup(catalog_path: str | Path, backup_root: str | Path) -> BackupVerification:
    source_path = Path(catalog_path).expanduser().resolve(strict=True)
    destination_root = Path(backup_root).expanduser().resolve(strict=False)
    destination_root.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = destination_root / f"catalog-{timestamp}-{uuid.uuid4().hex[:8]}.sqlite3"
    if destination.exists():
        raise MigrationError(f"refusing to overwrite backup: {destination}")

    with closing(_connect(source_path)) as source:
        source_integrity = _integrity(source)
        if source_integrity != "ok":
            raise MigrationError(f"source catalog integrity check failed: {source_integrity}")
        with closing(_connect(destination)) as target:
            source.backup(target)
            target.commit()
            backup_integrity = _integrity(target)
    if backup_integrity != "ok":
        destination.unlink(missing_ok=True)
        raise MigrationError(f"backup integrity check failed: {backup_integrity}")

    drill = destination_root / f".restore-check-{uuid.uuid4().hex}.sqlite3"
    try:
        with closing(_connect(destination)) as backup, closing(_connect(drill)) as restored:
            backup.backup(restored)
            restored.commit()
            restore_integrity = _integrity(restored)
        if restore_integrity != "ok":
            raise MigrationError(f"restored backup integrity check failed: {restore_integrity}")
    finally:
        drill.unlink(missing_ok=True)
        drill.with_name(drill.name + "-shm").unlink(missing_ok=True)
        drill.with_name(drill.name + "-wal").unlink(missing_ok=True)

    return BackupVerification(destination, source_integrity, backup_integrity, restore_integrity)


def run_migrations(
    catalog_path: str | Path,
    *,
    root_mappings: Mapping[str, str | Path] | None = None,
    backup_root: str | Path | None = None,
    library_name: str = "Academic Vault Windows Library",
    machine_profile: str = "windows-default",
    device_id: str | None = None,
) -> MigrationReport:
    path = Path(catalog_path).expanduser().resolve(strict=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    existed = path.exists() and path.stat().st_size > 0
    with closing(_connect(path)) as connection:
        previous = _current_version(connection)
        existing_tables = bool(_tables(connection))
    if previous > LATEST_SCHEMA_VERSION:
        raise MigrationError(
            f"catalog schema {previous} is newer than supported schema {LATEST_SCHEMA_VERSION}"
        )
    pending = tuple(item for item in MIGRATIONS if item.version > previous)
    backup = None
    if pending and existed and existing_tables:
        if backup_root is None:
            raise MigrationError("backup_root is required before migrating an existing catalog")
        backup = create_consistent_backup(path, backup_root)

    context = MigrationContext(
        root_mappings=root_mappings or {},
        backup_root=Path(backup_root).resolve(strict=False) if backup_root else None,
        library_name=library_name,
        machine_profile=machine_profile,
        device_id=device_id,
    )
    applied: list[int] = []
    try:
        with closing(_connect(path)) as connection:
            connection.execute("PRAGMA journal_mode=DELETE")
            for migration in pending:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    migration.apply(connection, context)
                    connection.execute(
                        """
                        INSERT INTO app_metadata(key,value) VALUES('schema_version',?)
                        ON CONFLICT(key) DO UPDATE SET value=excluded.value
                        """,
                        (str(migration.version),),
                    )
                    connection.execute(
                        """
                        INSERT INTO app_metadata(key,value) VALUES('last_migration',?)
                        ON CONFLICT(key) DO UPDATE SET value=excluded.value
                        """,
                        (f"{migration.version}:{migration.name}",),
                    )
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
                if _integrity(connection) != "ok":
                    raise MigrationError(f"integrity check failed after migration {migration.version}")
                applied.append(migration.version)
            current = _current_version(connection)
    except Exception as exc:
        raise MigrationError(
            f"migration failed; catalog transaction was rolled back and backup is {backup.path if backup else 'not required'}: {exc}"
        ) from exc
    return MigrationReport(previous, current, tuple(applied), backup)


__all__ = [
    "LATEST_SCHEMA_VERSION",
    "BackupVerification",
    "MigrationContext",
    "MigrationError",
    "MigrationReport",
    "create_consistent_backup",
    "run_migrations",
]
