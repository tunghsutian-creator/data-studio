"""Durable, single-concurrency export jobs with atomic local commits."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import unicodedata
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

from ..database import Database
from ..paths import PathLocationError
from .manifest import (
    build_manifest,
    canonical_json_bytes,
    render_checksums,
    render_manifest_csv,
    render_readme,
    sha256_bytes,
)


_INVALID_WINDOWS = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')
_RESERVED_WINDOWS = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}
_ACTIVE_EXPORTS = frozenset({"QUEUED", "RUNNING"})
_TERMINAL_EXPORTS = frozenset({"COMPLETED", "FAILED", "CANCELLED"})


class ExportFailure(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code[:100]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clean_text(value: Any, name: str, maximum: int, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} must be text")
    text = value.strip()
    if not text or len(text) > maximum or "\x00" in text:
        raise ValueError(f"{name} must contain 1 to {maximum} characters")
    return text


def _safe_component(value: str, maximum: int = 90) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    normalized = _INVALID_WINDOWS.sub("-", normalized)
    normalized = re.sub(r"\s+", "-", normalized).strip(" .-")
    if not normalized:
        normalized = "export"
    stem = normalized.split(".", 1)[0].upper()
    if stem in _RESERVED_WINDOWS:
        normalized = "_" + normalized
    if len(normalized) > maximum:
        suffix = Path(normalized).suffix[:20]
        normalized = normalized[: max(1, maximum - len(suffix))].rstrip(" .-") + suffix
    return normalized.rstrip(" .") or "export"


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_new(path: Path, payload: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    return sha256_bytes(payload)


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o100644 << 16
    return info


def _hash_token(token: str) -> str:
    value = _clean_text(token, "selection_token", 200)
    assert value is not None
    if len(value) < 32:
        raise ValueError("selection_token is too short")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _public_export(database: Database, row: Mapping[str, Any], *, items: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
    archive_path = None
    if row.get("archive_root_key") and row.get("archive_relpath"):
        try:
            archive_path = str(
                database.root_mapper.resolve(
                    str(row["archive_root_key"]),
                    str(row["archive_relpath"]),
                    must_exist=False,
                )
            )
        except PathLocationError:
            archive_path = None
    result = {
        key: row.get(key)
        for key in (
            "id",
            "library_id",
            "collection_id",
            "name",
            "purpose",
            "status",
            "export_mode",
            "duplicate_policy",
            "manifest_sha256",
            "file_count",
            "total_bytes",
            "created_at",
            "started_at",
            "finished_at",
            "error_code",
            "error_detail",
        )
    }
    result["archive_path"] = archive_path
    if items:
        result["items"] = [
            {
                key: item.get(key)
                for key in (
                    "asset_id",
                    "dataset_id",
                    "position",
                    "original_name",
                    "source_kind",
                    "source_sha256",
                    "size_bytes",
                    "exported_relpath",
                    "exported_sha256",
                    "duplicate_of",
                )
            }
            for item in items
        ]
    return result


def create_export(database: Database, payload: Mapping[str, Any]) -> dict[str, Any]:
    token_sha256 = _hash_token(str(payload.get("selection_token") or ""))
    name = _clean_text(payload.get("name"), "export name", 200)
    purpose = _clean_text(payload.get("purpose"), "export purpose", 2000, nullable=True)
    mode = str(payload.get("export_mode") or "FOLDER").upper()
    duplicate_policy = str(payload.get("duplicate_policy") or "PRESERVE").upper()
    if mode not in {"FOLDER", "ZIP64", "MANIFEST_ONLY"}:
        raise ValueError("unsupported export_mode")
    if duplicate_policy not in {"PRESERVE", "DEDUPLICATE"}:
        raise ValueError("unsupported duplicate_policy")
    collection_id = payload.get("collection_id")
    if collection_id is not None:
        collection_id = _clean_text(collection_id, "collection id", 200)
    export_id = str(uuid.uuid4())
    now = _utc_now()

    with database.transaction() as connection:
        library_id = database.library_id(connection)
        snapshot = connection.execute(
            "SELECT * FROM selection_snapshots WHERE token_sha256=? AND library_id=?",
            (token_sha256, library_id),
        ).fetchone()
        if snapshot is None:
            raise ExportFailure("SELECTION_TOKEN_INVALID", "selection token is invalid")
        if str(snapshot["status"]) == "CONSUMED":
            raise ExportFailure("SELECTION_TOKEN_CONSUMED", "selection token has already been consumed")
        if str(snapshot["status"]) == "BLOCKED":
            raise ExportFailure("SELECTION_BLOCKED", "selection preview contains blocking integrity issues")
        if str(snapshot["status"]) != "READY":
            raise ExportFailure("SELECTION_TOKEN_INVALID", "selection token is not ready")
        if str(snapshot["expires_at"]) <= now:
            raise ExportFailure("SELECTION_TOKEN_EXPIRED", "selection token has expired")
        if collection_id is not None:
            exists = connection.execute(
                "SELECT 1 FROM collections WHERE id=? AND library_id=?",
                (collection_id, library_id),
            ).fetchone()
            if exists is None:
                raise ValueError("collection does not exist in this library")
        selection_items = connection.execute(
            "SELECT * FROM selection_snapshot_items WHERE selection_id=? ORDER BY position,asset_id",
            (snapshot["id"],),
        ).fetchall()
        if len(selection_items) != int(snapshot["asset_count"]):
            raise ExportFailure("SELECTION_CORRUPT", "selection snapshot item count is inconsistent")
        connection.execute(
            """
            INSERT INTO exports(
                id,library_id,collection_id,selection_id,name,purpose,status,
                export_mode,duplicate_policy,file_count,total_bytes,created_at
            ) VALUES(?,?,?,?,?,?,'QUEUED',?,?,?,?,?)
            """,
            (
                export_id,
                library_id,
                collection_id,
                snapshot["id"],
                name,
                purpose,
                mode,
                duplicate_policy,
                len(selection_items),
                int(snapshot["total_bytes"]),
                now,
            ),
        )
        for item in selection_items:
            digest = str(item["selected_sha256"] or "")
            if len(digest) != 64:
                raise ExportFailure("SELECTION_CORRUPT", "selection snapshot lacks a verified SHA-256")
            connection.execute(
                """
                INSERT INTO export_items(
                    export_id,asset_id,dataset_id,position,original_name,source_kind,
                    source_sha256,size_bytes,duplicate_of
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    export_id,
                    item["asset_id"],
                    item["dataset_id"],
                    item["position"],
                    item["original_name"],
                    item["source_kind"],
                    digest,
                    item["size_bytes"],
                    item["duplicate_of"] if duplicate_policy == "DEDUPLICATE" else None,
                ),
            )
        cursor = connection.execute(
            """
            UPDATE selection_snapshots SET status='CONSUMED',consumed_at=?
            WHERE id=? AND status='READY'
            """,
            (now, snapshot["id"]),
        )
        if cursor.rowcount != 1:
            raise ExportFailure("SELECTION_TOKEN_CONSUMED", "selection token was consumed concurrently")
    item = get_export(database, export_id)
    assert item is not None
    return item


def list_exports(database: Database, limit: int = 100) -> list[dict[str, Any]]:
    with database.connect() as connection:
        rows = connection.execute(
            """
            SELECT * FROM exports WHERE library_id=?
            ORDER BY created_at DESC,id LIMIT ?
            """,
            (database.library_id(connection), max(1, min(int(limit), 500))),
        ).fetchall()
    return [_public_export(database, dict(row)) for row in rows]


def get_export(database: Database, export_id: str) -> dict[str, Any] | None:
    clean_id = _clean_text(export_id, "export id", 200)
    with database.connect() as connection:
        row = connection.execute(
            "SELECT * FROM exports WHERE id=? AND library_id=?",
            (clean_id, database.library_id(connection)),
        ).fetchone()
        if row is None:
            return None
        items = connection.execute(
            "SELECT * FROM export_items WHERE export_id=? ORDER BY position,asset_id",
            (clean_id,),
        ).fetchall()
    return _public_export(database, dict(row), items=[dict(item) for item in items])


def export_counts(database: Database) -> dict[str, int]:
    with database.connect() as connection:
        rows = connection.execute(
            "SELECT status,COUNT(*) AS count FROM exports GROUP BY status"
        ).fetchall()
    counts = {status: 0 for status in (*sorted(_ACTIVE_EXPORTS), *sorted(_TERMINAL_EXPORTS))}
    counts.update({str(row["status"]): int(row["count"]) for row in rows})
    return counts


def active_export_count(database: Database) -> int:
    with database.connect() as connection:
        return int(
            connection.execute(
                "SELECT COUNT(*) FROM exports WHERE status IN ('QUEUED','RUNNING')"
            ).fetchone()[0]
        )


def recover_interrupted_exports(database: Database) -> int:
    with database.transaction() as connection:
        cursor = connection.execute(
            """
            UPDATE exports SET status='QUEUED',started_at=NULL,error_code=NULL,error_detail=NULL
            WHERE status='RUNNING'
            """
        )
    return int(cursor.rowcount)


def _claim_next(database: Database) -> dict[str, Any] | None:
    now = _utc_now()
    with database.transaction() as connection:
        row = connection.execute(
            "SELECT id FROM exports WHERE status='QUEUED' ORDER BY created_at,id LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        cursor = connection.execute(
            """
            UPDATE exports SET status='RUNNING',started_at=COALESCE(started_at,?),
                error_code=NULL,error_detail=NULL
            WHERE id=? AND status='QUEUED'
            """,
            (now, row["id"]),
        )
        if cursor.rowcount != 1:
            return None
        claimed = connection.execute("SELECT * FROM exports WHERE id=?", (row["id"],)).fetchone()
    return dict(claimed) if claimed else None


def _live_items(database: Database, export: Mapping[str, Any]) -> list[dict[str, Any]]:
    with database.connect() as connection:
        rows = connection.execute(
            """
            SELECT ei.*,ssi.dataset_revision,ssi.selected_root_key,ssi.selected_relpath,
                   a.original_name AS live_original_name,a.size_bytes AS live_size_bytes,
                   a.source_sha256 AS live_source_sha256,a.managed_sha256 AS live_managed_sha256,
                   a.sha256 AS live_sha256,a.hash_state AS live_hash_state,
                   a.path_state AS live_path_state,a.original_root_key AS live_original_root_key,
                   a.original_relpath AS live_original_relpath,a.managed_root_key AS live_managed_root_key,
                   a.managed_relpath AS live_managed_relpath,d.source_kind AS live_source_kind,
                   d.status AS live_dataset_status,d.revision AS live_dataset_revision
            FROM export_items ei
            JOIN exports e ON e.id=ei.export_id
            JOIN selection_snapshot_items ssi
              ON ssi.selection_id=e.selection_id AND ssi.asset_id=ei.asset_id
            LEFT JOIN assets a ON a.id=ei.asset_id
            LEFT JOIN datasets d ON d.id=a.dataset_id
            WHERE ei.export_id=?
            ORDER BY ei.position,ei.asset_id
            """,
            (export["id"],),
        ).fetchall()
    if len(rows) != int(export["file_count"]):
        raise ExportFailure("SELECTION_CHANGED", "selected asset records are missing")

    result: list[dict[str, Any]] = []
    for raw in rows:
        row = dict(raw)
        if row.get("live_dataset_revision") is None:
            raise ExportFailure("SELECTION_CHANGED", "a selected asset no longer exists")
        if int(row["live_dataset_revision"]) != int(row["dataset_revision"]):
            raise ExportFailure("SELECTION_CHANGED", "a selected dataset changed after preview")
        if str(row["live_dataset_status"]).upper() == "STALE":
            raise ExportFailure("SELECTION_STALE", "a selected dataset is stale")
        if str(row["live_path_state"]).upper() != "VALID":
            raise ExportFailure("SELECTION_PATH_INVALID", "a selected asset path requires review")
        has_managed = bool(
            row.get("live_managed_root_key")
            and row.get("live_managed_relpath")
            and row.get("live_managed_sha256")
        )
        root_key = row.get("live_managed_root_key") if has_managed else row.get("live_original_root_key")
        relpath = row.get("live_managed_relpath") if has_managed else row.get("live_original_relpath")
        digest = str(
            row.get("live_managed_sha256")
            if has_managed
            else row.get("live_source_sha256") or row.get("live_sha256") or ""
        ).lower()
        expected_facts = (
            str(row["original_name"]),
            str(row["source_kind"]),
            str(row["source_sha256"]),
            int(row["size_bytes"]),
            str(row["selected_root_key"]),
            str(row["selected_relpath"]),
        )
        live_facts = (
            str(row["live_original_name"]),
            str(row["live_source_kind"]),
            digest,
            int(row["live_size_bytes"]),
            str(root_key),
            str(relpath),
        )
        if expected_facts != live_facts:
            raise ExportFailure("SELECTION_CHANGED", "selected asset facts changed after preview")
        try:
            source_path = database.root_mapper.resolve(str(root_key), str(relpath), must_exist=True)
        except (OSError, PathLocationError) as exc:
            raise ExportFailure("SOURCE_MISSING", "a selected source is missing or outside configured roots") from exc
        if not source_path.is_file():
            raise ExportFailure("SOURCE_MISSING", "a selected source is not a regular file")
        row["source_path"] = source_path
        result.append(row)
    return result


def _verify_source(item: Mapping[str, Any]) -> None:
    path = Path(item["source_path"])
    try:
        if path.stat().st_size != int(item["size_bytes"]):
            raise ExportFailure("SOURCE_SIZE_MISMATCH", "a selected source size changed")
        if _sha256_path(path) != str(item["source_sha256"]):
            raise ExportFailure("SOURCE_HASH_MISMATCH", "a selected source SHA-256 changed")
    except FileNotFoundError as exc:
        raise ExportFailure("SOURCE_MISSING", "a selected source disappeared") from exc
    except PermissionError as exc:
        raise ExportFailure("SOURCE_UNREADABLE", "a selected source cannot be read") from exc
    except OSError as exc:
        raise ExportFailure("SOURCE_UNREADABLE", "a selected source cannot be read reliably") from exc


def _copy_verified(source: Path, destination: Path, expected: str) -> str:
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    try:
        with source.open("rb") as input_stream, destination.open("xb") as output_stream:
            for chunk in iter(lambda: input_stream.read(4 * 1024 * 1024), b""):
                output_stream.write(chunk)
                digest.update(chunk)
            output_stream.flush()
            os.fsync(output_stream.fileno())
    except FileNotFoundError as exc:
        raise ExportFailure("SOURCE_MISSING", "a selected source disappeared during export") from exc
    except PermissionError as exc:
        raise ExportFailure("EXPORT_WRITE_DENIED", "a source or export destination is not accessible") from exc
    except OSError as exc:
        raise ExportFailure("EXPORT_IO_ERROR", "an I/O error interrupted the verified copy") from exc
    copied = digest.hexdigest()
    if copied != expected or _sha256_path(destination) != expected:
        raise ExportFailure("EXPORTED_HASH_MISMATCH", "an exported file failed SHA-256 verification")
    return copied


def _item_relpath(item: Mapping[str, Any]) -> str:
    name = _safe_component(str(item["original_name"]), maximum=90)
    return f"files/{int(item['position']) + 1:05d}--{str(item['asset_id'])[:8]}--{name}"


def _manifest_payloads(
    database: Database,
    export: Mapping[str, Any],
    selection_revision: int,
    items: Sequence[Mapping[str, Any]],
) -> tuple[bytes, str, bytes, bytes]:
    manifest = build_manifest(
        export,
        catalog_snapshot_revision=selection_revision,
        database_schema_version=database.schema_version(),
        items=items,
    )
    manifest_bytes = canonical_json_bytes(manifest)
    return (
        manifest_bytes,
        sha256_bytes(manifest_bytes),
        render_manifest_csv(items),
        render_readme(
            export,
            item_count=len(items),
            total_bytes=sum(int(item["size_bytes"]) for item in items),
        ),
    )


def _selection_revision(database: Database, export_id: str) -> int:
    with database.connect() as connection:
        row = connection.execute(
            """
            SELECT s.catalog_revision FROM exports e
            JOIN selection_snapshots s ON s.id=e.selection_id WHERE e.id=?
            """,
            (export_id,),
        ).fetchone()
    if row is None:
        raise ExportFailure("EXPORT_CORRUPT", "export selection snapshot is missing")
    return int(row[0])


def _build_folder(
    database: Database,
    export: Mapping[str, Any],
    live_items: list[dict[str, Any]],
    staging: Path,
) -> tuple[str, list[dict[str, Any]]]:
    staging.mkdir(parents=True, exist_ok=False)
    checksums: dict[str, str] = {}
    outputs_by_asset: dict[str, tuple[str, str]] = {}
    rendered: list[dict[str, Any]] = []
    manifest_only = str(export["export_mode"]) == "MANIFEST_ONLY"
    for item in live_items:
        _verify_source(item)
        output = dict(item)
        duplicate_of = item.get("duplicate_of")
        if manifest_only:
            output["exported_relpath"] = None
            output["exported_sha256"] = str(item["source_sha256"])
        elif duplicate_of:
            if str(duplicate_of) not in outputs_by_asset:
                raise ExportFailure("EXPORT_CORRUPT", "deduplicated item points to a missing keeper")
            output["exported_relpath"], output["exported_sha256"] = outputs_by_asset[str(duplicate_of)]
        else:
            relative = _item_relpath(item)
            digest = _copy_verified(Path(item["source_path"]), staging / Path(relative), str(item["source_sha256"]))
            output["exported_relpath"] = relative
            output["exported_sha256"] = digest
            outputs_by_asset[str(item["asset_id"])] = (relative, digest)
            checksums[relative] = digest
        rendered.append(output)

    # Every selected source is checked again after all output writes, closing
    # the window in which an earlier source could change during a long bundle.
    for item in live_items:
        _verify_source(item)
    _live_items(database, export)

    manifest_bytes, manifest_sha, csv_bytes, readme_bytes = _manifest_payloads(
        database,
        export,
        _selection_revision(database, str(export["id"])),
        rendered,
    )
    checksums["manifest.json"] = _write_new(staging / "manifest.json", manifest_bytes)
    checksums["manifest.csv"] = _write_new(staging / "manifest.csv", csv_bytes)
    checksums["README.md"] = _write_new(staging / "README.md", readme_bytes)
    _write_new(staging / "checksums.sha256", render_checksums(checksums))
    return manifest_sha, rendered


def _build_zip(
    database: Database,
    export: Mapping[str, Any],
    live_items: list[dict[str, Any]],
    staging: Path,
) -> tuple[str, list[dict[str, Any]]]:
    checksums: dict[str, str] = {}
    outputs_by_asset: dict[str, tuple[str, str]] = {}
    rendered: list[dict[str, Any]] = []
    staging.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(staging, mode="x", allowZip64=True) as archive:
        for item in live_items:
            _verify_source(item)
            output = dict(item)
            duplicate_of = item.get("duplicate_of")
            if duplicate_of:
                if str(duplicate_of) not in outputs_by_asset:
                    raise ExportFailure("EXPORT_CORRUPT", "deduplicated item points to a missing keeper")
                output["exported_relpath"], output["exported_sha256"] = outputs_by_asset[str(duplicate_of)]
            else:
                relative = _item_relpath(item)
                digest = hashlib.sha256()
                try:
                    with Path(item["source_path"]).open("rb") as source, archive.open(
                        _zip_info(relative), mode="w", force_zip64=True
                    ) as destination:
                        for chunk in iter(lambda: source.read(4 * 1024 * 1024), b""):
                            destination.write(chunk)
                            digest.update(chunk)
                except FileNotFoundError as exc:
                    raise ExportFailure(
                        "SOURCE_MISSING", "a selected source disappeared during ZIP export"
                    ) from exc
                except PermissionError as exc:
                    raise ExportFailure(
                        "EXPORT_WRITE_DENIED", "a source or ZIP destination is not accessible"
                    ) from exc
                except OSError as exc:
                    raise ExportFailure(
                        "EXPORT_IO_ERROR", "an I/O error interrupted the ZIP export"
                    ) from exc
                copied = digest.hexdigest()
                if copied != str(item["source_sha256"]):
                    raise ExportFailure("EXPORTED_HASH_MISMATCH", "a ZIP entry failed SHA-256 verification")
                output["exported_relpath"] = relative
                output["exported_sha256"] = copied
                outputs_by_asset[str(item["asset_id"])] = (relative, copied)
                checksums[relative] = copied
            rendered.append(output)

        for item in live_items:
            _verify_source(item)
        _live_items(database, export)
        manifest_bytes, manifest_sha, csv_bytes, readme_bytes = _manifest_payloads(
            database,
            export,
            _selection_revision(database, str(export["id"])),
            rendered,
        )
        checksums["manifest.json"] = sha256_bytes(manifest_bytes)
        checksums["manifest.csv"] = sha256_bytes(csv_bytes)
        checksums["README.md"] = sha256_bytes(readme_bytes)
        archive.writestr(_zip_info("manifest.json"), manifest_bytes)
        archive.writestr(_zip_info("manifest.csv"), csv_bytes)
        archive.writestr(_zip_info("README.md"), readme_bytes)
        archive.writestr(_zip_info("checksums.sha256"), render_checksums(checksums))
    with staging.open("r+b") as stream:
        os.fsync(stream.fileno())
    return manifest_sha, rendered


def _parse_checksums(payload: bytes) -> dict[str, str]:
    result: dict[str, str] = {}
    try:
        lines = payload.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise ExportFailure("EXPORT_VERIFY_FAILED", "checksums file is not UTF-8") from exc
    for line in lines:
        digest, separator, relative = line.partition("  ")
        pure = PurePosixPath(relative)
        if (
            not separator
            or len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest)
            or pure.is_absolute()
            or not pure.parts
            or any(part in {"", ".", ".."} for part in pure.parts)
            or relative in result
        ):
            raise ExportFailure("EXPORT_VERIFY_FAILED", "checksums file contains an invalid entry")
        result[relative] = digest
    if "manifest.json" not in result or "manifest.csv" not in result:
        raise ExportFailure("EXPORT_VERIFY_FAILED", "checksums file omits a required manifest")
    return result


def _verify_folder(path: Path, expected_manifest_sha: str) -> None:
    try:
        manifest = path / "manifest.json"
        checksums_path = path / "checksums.sha256"
        if not manifest.is_file() or not checksums_path.is_file():
            raise ExportFailure("EXPORT_VERIFY_FAILED", "export bundle is incomplete")
        if _sha256_path(manifest) != expected_manifest_sha:
            raise ExportFailure("EXPORT_VERIFY_FAILED", "manifest SHA-256 does not match")
        checksums = _parse_checksums(checksums_path.read_bytes())
        root = path.resolve(strict=True)
        for relative, expected in checksums.items():
            pure = PurePosixPath(relative)
            candidate = root.joinpath(*pure.parts).resolve(strict=True)
            try:
                candidate.relative_to(root)
            except ValueError as exc:
                raise ExportFailure("EXPORT_VERIFY_FAILED", "checksum path escapes the bundle") from exc
            if not candidate.is_file() or _sha256_path(candidate) != expected:
                raise ExportFailure("EXPORT_VERIFY_FAILED", "bundle file SHA-256 does not match")
    except ExportFailure:
        raise
    except OSError as exc:
        raise ExportFailure("EXPORT_VERIFY_FAILED", "export bundle cannot be read completely") from exc


def _verify_zip(path: Path, expected_manifest_sha: str) -> None:
    try:
        with zipfile.ZipFile(path, mode="r", allowZip64=True) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)):
                raise ExportFailure("EXPORT_VERIFY_FAILED", "ZIP contains duplicate entry names")
            manifest = archive.read("manifest.json")
            if sha256_bytes(manifest) != expected_manifest_sha:
                raise ExportFailure("EXPORT_VERIFY_FAILED", "manifest SHA-256 does not match")
            checksums = _parse_checksums(archive.read("checksums.sha256"))
            for relative, expected in checksums.items():
                digest = hashlib.sha256()
                with archive.open(relative, "r") as stream:
                    for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
                        digest.update(chunk)
                if digest.hexdigest() != expected:
                    raise ExportFailure("EXPORT_VERIFY_FAILED", "ZIP entry SHA-256 does not match")
    except (KeyError, OSError, zipfile.BadZipFile) as exc:
        raise ExportFailure("EXPORT_VERIFY_FAILED", "ZIP64 export is incomplete or invalid") from exc


def _prepared_output(
    database: Database,
    export: Mapping[str, Any],
    archive_relpath: str,
    manifest_sha: str,
    items: Sequence[Mapping[str, Any]],
) -> None:
    with database.transaction() as connection:
        for item in items:
            connection.execute(
                """
                UPDATE export_items SET exported_relpath=?,exported_sha256=?,duplicate_of=?
                WHERE export_id=? AND asset_id=?
                """,
                (
                    item.get("exported_relpath"),
                    item.get("exported_sha256"),
                    item.get("duplicate_of"),
                    export["id"],
                    item["asset_id"],
                ),
            )
        cursor = connection.execute(
            """
            UPDATE exports SET archive_root_key='exports',archive_relpath=?,manifest_sha256=?
            WHERE id=? AND status='RUNNING'
            """,
            (archive_relpath, manifest_sha, export["id"]),
        )
        if cursor.rowcount != 1:
            raise ExportFailure("EXPORT_STATE_LOST", "export job no longer owns the running state")


def _complete(database: Database, export_id: str) -> None:
    with database.transaction() as connection:
        cursor = connection.execute(
            """
            UPDATE exports SET status='COMPLETED',finished_at=?,error_code=NULL,error_detail=NULL
            WHERE id=? AND status='RUNNING'
            """,
            (_utc_now(), export_id),
        )
        if cursor.rowcount != 1:
            raise ExportFailure("EXPORT_STATE_LOST", "export completion state was lost")


def _fail(database: Database, export_id: str, code: str, detail: str) -> None:
    safe_detail = " ".join(detail.split())[:1000]
    with database.transaction() as connection:
        connection.execute(
            """
            UPDATE exports SET status='FAILED',finished_at=?,error_code=?,error_detail=?
            WHERE id=? AND status='RUNNING'
            """,
            (_utc_now(), code[:100], safe_detail, export_id),
        )


def _clear_staging(path: Path, staging_root: Path) -> None:
    resolved_root = staging_root.resolve(strict=False)
    resolved = path.resolve(strict=False)
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise ExportFailure("EXPORT_PATH_INVALID", "temporary export path escaped its reserved root") from exc
    if resolved.is_dir():
        shutil.rmtree(resolved)
    elif resolved.exists():
        resolved.unlink()


class ExportWorker:
    def __init__(self, database: Database) -> None:
        self.database = database

    def process_next(self) -> dict[str, Any] | None:
        export = _claim_next(self.database)
        if export is None:
            return None
        export_id = str(export["id"])
        staging: Path | None = None
        try:
            live_items = _live_items(self.database, export)
            safe_name = _safe_component(str(export["name"]), maximum=80)
            short_id = export_id.replace("-", "")[:8]
            if str(export["export_mode"]) == "ZIP64":
                archive_relpath = f"{safe_name}--{short_id}.zip"
                staging_relpath = f".academic-vault-staging/{export_id}.zip.partial"
            else:
                archive_relpath = f"{safe_name}--{short_id}"
                staging_relpath = f".academic-vault-staging/{export_id}"
            if export.get("archive_relpath") and str(export["archive_relpath"]) != archive_relpath:
                raise ExportFailure("EXPORT_PATH_INVALID", "prepared export path no longer matches its job")
            final_path = self.database.root_mapper.resolve("exports", archive_relpath, must_exist=False)
            staging = self.database.root_mapper.resolve("exports", staging_relpath, must_exist=False)
            staging_root = self.database.root_mapper.resolve(
                "exports", ".academic-vault-staging", must_exist=False
            )
            staging_root.mkdir(parents=True, exist_ok=True)

            expected_manifest = str(export.get("manifest_sha256") or "")
            if final_path.exists():
                if len(expected_manifest) != 64:
                    raise ExportFailure("OUTPUT_EXISTS", "refusing to overwrite an existing export path")
                if str(export["export_mode"]) == "ZIP64":
                    _verify_zip(final_path, expected_manifest)
                else:
                    _verify_folder(final_path, expected_manifest)
                _complete(self.database, export_id)
                return get_export(self.database, export_id)

            _clear_staging(staging, staging_root)
            if str(export["export_mode"]) == "ZIP64":
                manifest_sha, rendered = _build_zip(self.database, export, live_items, staging)
                _verify_zip(staging, manifest_sha)
            else:
                manifest_sha, rendered = _build_folder(self.database, export, live_items, staging)
                _verify_folder(staging, manifest_sha)

            _prepared_output(
                self.database,
                export,
                archive_relpath,
                manifest_sha,
                rendered,
            )
            if final_path.exists():
                raise ExportFailure("OUTPUT_EXISTS", "refusing to overwrite an existing export path")
            staging.replace(final_path)
            staging = None
            if str(export["export_mode"]) == "ZIP64":
                _verify_zip(final_path, manifest_sha)
            else:
                _verify_folder(final_path, manifest_sha)
            _complete(self.database, export_id)
        except ExportFailure as exc:
            if staging is not None:
                try:
                    staging_root = self.database.root_mapper.resolve(
                        "exports", ".academic-vault-staging", must_exist=False
                    )
                    _clear_staging(staging, staging_root)
                except Exception:
                    pass
            _fail(self.database, export_id, exc.code, str(exc))
        except Exception:
            if staging is not None:
                try:
                    staging_root = self.database.root_mapper.resolve(
                        "exports", ".academic-vault-staging", must_exist=False
                    )
                    _clear_staging(staging, staging_root)
                except Exception:
                    pass
            _fail(
                self.database,
                export_id,
                "EXPORT_INTERNAL_ERROR",
                "local export failed unexpectedly; retry after checking the destination",
            )
        return get_export(self.database, export_id)


class ExportWorkerService:
    def __init__(self, worker: ExportWorker, *, poll_seconds: float = 0.5) -> None:
        self.worker = worker
        self.poll_seconds = max(0.05, float(poll_seconds))
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_error: str | None = None

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="academic-vault-export-worker",
            daemon=True,
        )
        self._thread.start()

    def wake(self) -> None:
        self._wake.set()

    def stop(self, timeout_seconds: float = 5.0) -> bool:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=max(0, timeout_seconds))
        return not self.running

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                outcome = self.worker.process_next()
                self.last_error = None
            except Exception as exc:
                self.last_error = type(exc).__name__
                outcome = None
            if outcome is None:
                self._wake.wait(self.poll_seconds)
                self._wake.clear()


def load_export_manifest(database: Database, export_id: str) -> dict[str, Any]:
    clean_id = _clean_text(export_id, "export id", 200)
    with database.connect() as connection:
        row = connection.execute(
            "SELECT * FROM exports WHERE id=? AND library_id=?",
            (clean_id, database.library_id(connection)),
        ).fetchone()
    if row is None:
        raise KeyError(clean_id)
    export = dict(row)
    if export["status"] != "COMPLETED" or not export.get("archive_relpath"):
        raise ExportFailure("EXPORT_NOT_COMPLETE", "export is not complete")
    try:
        path = database.root_mapper.resolve(
            "exports", str(export["archive_relpath"]), must_exist=True
        )
    except (OSError, PathLocationError) as exc:
        raise ExportFailure("EXPORT_OUTPUT_MISSING", "completed export output is missing") from exc
    expected = str(export["manifest_sha256"])
    if export["export_mode"] == "ZIP64":
        _verify_zip(path, expected)
        with zipfile.ZipFile(path, "r", allowZip64=True) as archive:
            payload = archive.read("manifest.json")
    else:
        _verify_folder(path, expected)
        payload = (path / "manifest.json").read_bytes()
    return json.loads(payload.decode("utf-8"))


__all__ = [
    "ExportFailure",
    "ExportWorker",
    "ExportWorkerService",
    "active_export_count",
    "create_export",
    "export_counts",
    "get_export",
    "list_exports",
    "load_export_manifest",
    "recover_interrupted_exports",
]
