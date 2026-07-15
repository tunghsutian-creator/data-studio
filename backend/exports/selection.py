"""Immutable, path-safe selection previews and named collections."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..database import Database
from ..paths import PathLocationError


_SHA256 = frozenset("0123456789abcdef")
_BLOCKING_CODES = frozenset(
    {
        "HASH_MISMATCH",
        "HASH_UNAVAILABLE",
        "MISSING",
        "PATH_REVIEW",
        "PATH_UNRESOLVED",
        "PATH_UNSAFE",
        "SIZE_MISMATCH",
        "STALE",
        "UNREADABLE",
    }
)
_WARNING_CODES = frozenset({"DUPLICATE_SHA256", "NAME_COLLISION"})
_MAX_SELECTION_ASSETS = 10_000


class SelectionChanged(RuntimeError):
    """Raised when catalog facts change while a preview is being verified."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )


def _clean_text(value: Any, name: str, maximum: int, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} must be text")
    text = value.strip()
    if not text or len(text) > maximum or "\x00" in text:
        raise ValueError(f"{name} must contain 1 to {maximum} characters")
    return text


def _clean_ids(values: Sequence[Any], name: str) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = _clean_text(value, name, 200)
        assert item is not None
        if item in seen:
            raise ValueError(f"{name} contains duplicate ids")
        seen.add(item)
        cleaned.append(item)
    return cleaned


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _valid_sha256(value: Any) -> str | None:
    digest = str(value or "").lower()
    if len(digest) == 64 and set(digest) <= _SHA256:
        return digest
    return None


def _collection_dict(row: Mapping[str, Any], items: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    result = {
        "id": str(row["id"]),
        "library_id": str(row["library_id"]),
        "name": str(row["name"]),
        "purpose": row["purpose"],
        "revision": int(row["revision"]),
        "asset_count": int(row.get("asset_count", 0)),
        "total_bytes": int(row.get("total_bytes", 0)),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
    }
    if items is not None:
        result["items"] = items
    return result


def _collection_items(connection: sqlite3.Connection, collection_id: str) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT ci.asset_id,ci.position,ci.added_at,a.dataset_id,a.original_name,a.size_bytes,
               d.modality,d.status
        FROM collection_items ci
        JOIN assets a ON a.id=ci.asset_id
        JOIN datasets d ON d.id=a.dataset_id
        WHERE ci.collection_id=?
        ORDER BY ci.position,ci.asset_id
        """,
        (collection_id,),
    ).fetchall()
    return [
        {
            "asset_id": str(row["asset_id"]),
            "dataset_id": str(row["dataset_id"]),
            "position": int(row["position"]),
            "original_name": str(row["original_name"]),
            "size_bytes": int(row["size_bytes"]),
            "modality": str(row["modality"]),
            "dataset_status": str(row["status"]),
            "added_at": str(row["added_at"]),
        }
        for row in rows
    ]


def create_collection(database: Database, name: str, purpose: str | None = None) -> dict[str, Any]:
    collection_id = str(uuid.uuid4())
    clean_name = _clean_text(name, "collection name", 200)
    clean_purpose = _clean_text(purpose, "collection purpose", 2000, nullable=True)
    now = _utc_now()
    try:
        with database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO collections(id,library_id,name,purpose,created_at,updated_at)
                VALUES(?,?,?,?,?,?)
                """,
                (collection_id, database.library_id(connection), clean_name, clean_purpose, now, now),
            )
    except sqlite3.IntegrityError as exc:
        if "collections.library_id, collections.name" in str(exc):
            raise ValueError("a collection with this name already exists") from exc
        raise
    item = get_collection(database, collection_id)
    assert item is not None
    return item


def create_collection_from_selection(
    database: Database,
    name: str,
    purpose: str | None,
    selection_token: str,
) -> dict[str, Any]:
    collection_id = str(uuid.uuid4())
    clean_name = _clean_text(name, "collection name", 200)
    clean_purpose = _clean_text(purpose, "collection purpose", 2000, nullable=True)
    token = _clean_text(selection_token, "selection token", 200)
    assert token is not None
    if len(token) < 32:
        raise ValueError("selection token is too short")
    token_sha256 = hashlib.sha256(token.encode("utf-8")).hexdigest()
    now = _utc_now()
    try:
        with database.transaction() as connection:
            library_id = database.library_id(connection)
            snapshot = connection.execute(
                "SELECT * FROM selection_snapshots WHERE token_sha256=? AND library_id=?",
                (token_sha256, library_id),
            ).fetchone()
            if snapshot is None:
                raise ValueError("selection token is invalid")
            if str(snapshot["status"]) != "READY":
                raise ValueError("selection snapshot is not ready to save")
            if str(snapshot["expires_at"]) <= now:
                raise ValueError("selection token has expired")
            items = connection.execute(
                "SELECT asset_id,position FROM selection_snapshot_items WHERE selection_id=? ORDER BY position,asset_id",
                (snapshot["id"],),
            ).fetchall()
            if len(items) != int(snapshot["asset_count"]):
                raise ValueError("selection snapshot item count is inconsistent")
            connection.execute(
                """
                INSERT INTO collections(id,library_id,name,purpose,created_at,updated_at)
                VALUES(?,?,?,?,?,?)
                """,
                (collection_id, library_id, clean_name, clean_purpose, now, now),
            )
            connection.executemany(
                "INSERT INTO collection_items(collection_id,asset_id,position,added_at) VALUES(?,?,?,?)",
                ((collection_id, str(item["asset_id"]), int(item["position"]), now) for item in items),
            )
    except sqlite3.IntegrityError as exc:
        if "collections.library_id, collections.name" in str(exc):
            raise ValueError("a collection with this name already exists") from exc
        if "FOREIGN KEY constraint failed" in str(exc):
            raise ValueError("selection contains assets that no longer exist") from exc
        raise
    item = get_collection(database, collection_id)
    assert item is not None
    return item


def list_collections(database: Database) -> list[dict[str, Any]]:
    with database.connect() as connection:
        rows = connection.execute(
            """
            SELECT c.*,COUNT(ci.asset_id) AS asset_count,COALESCE(SUM(a.size_bytes),0) AS total_bytes
            FROM collections c
            LEFT JOIN collection_items ci ON ci.collection_id=c.id
            LEFT JOIN assets a ON a.id=ci.asset_id
            WHERE c.library_id=?
            GROUP BY c.id
            ORDER BY c.updated_at DESC,c.id
            """,
            (database.library_id(connection),),
        ).fetchall()
    return [_collection_dict(dict(row)) for row in rows]


def get_collection(database: Database, collection_id: str) -> dict[str, Any] | None:
    clean_id = _clean_text(collection_id, "collection id", 200)
    with database.connect() as connection:
        row = connection.execute(
            """
            SELECT c.*,COUNT(ci.asset_id) AS asset_count,COALESCE(SUM(a.size_bytes),0) AS total_bytes
            FROM collections c
            LEFT JOIN collection_items ci ON ci.collection_id=c.id
            LEFT JOIN assets a ON a.id=ci.asset_id
            WHERE c.id=? AND c.library_id=?
            GROUP BY c.id
            """,
            (clean_id, database.library_id(connection)),
        ).fetchone()
        if row is None:
            return None
        items = _collection_items(connection, str(row["id"]))
    return _collection_dict(dict(row), items)


def update_collection(database: Database, collection_id: str, changes: Mapping[str, Any]) -> dict[str, Any] | None:
    clean_id = _clean_text(collection_id, "collection id", 200)
    updates: list[str] = []
    values: list[Any] = []
    if "name" in changes:
        updates.append("name=?")
        values.append(_clean_text(changes["name"], "collection name", 200))
    if "purpose" in changes:
        updates.append("purpose=?")
        values.append(_clean_text(changes["purpose"], "collection purpose", 2000, nullable=True))
    if not updates:
        return get_collection(database, clean_id)
    updates.extend(("revision=revision+1", "updated_at=?"))
    values.extend((_utc_now(), clean_id, database.library_id()))
    try:
        with database.transaction() as connection:
            cursor = connection.execute(
                f"UPDATE collections SET {', '.join(updates)} WHERE id=? AND library_id=?",
                values,
            )
            if cursor.rowcount == 0:
                return None
    except sqlite3.IntegrityError as exc:
        if "collections.library_id, collections.name" in str(exc):
            raise ValueError("a collection with this name already exists") from exc
        raise
    return get_collection(database, clean_id)


def add_collection_items(database: Database, collection_id: str, asset_ids: Sequence[Any]) -> dict[str, Any] | None:
    clean_id = _clean_text(collection_id, "collection id", 200)
    ids = _clean_ids(asset_ids, "asset_ids")
    if not ids:
        raise ValueError("asset_ids must not be empty")
    placeholders = ",".join("?" for _ in ids)
    now = _utc_now()
    with database.transaction() as connection:
        collection = connection.execute(
            "SELECT id FROM collections WHERE id=? AND library_id=?",
            (clean_id, database.library_id(connection)),
        ).fetchone()
        if collection is None:
            return None
        rows = connection.execute(
            f"""
            SELECT a.id FROM assets a JOIN datasets d ON d.id=a.dataset_id
            WHERE a.id IN ({placeholders}) AND d.library_id=?
            """,
            (*ids, database.library_id(connection)),
        ).fetchall()
        found = {str(row["id"]) for row in rows}
        if found != set(ids):
            raise ValueError(f"{len(set(ids) - found)} asset id(s) do not exist in this library")
        existing = {
            str(row["asset_id"])
            for row in connection.execute(
                f"SELECT asset_id FROM collection_items WHERE collection_id=? AND asset_id IN ({placeholders})",
                (clean_id, *ids),
            ).fetchall()
        }
        position = int(
            connection.execute(
                "SELECT COALESCE(MAX(position)+1,0) FROM collection_items WHERE collection_id=?",
                (clean_id,),
            ).fetchone()[0]
        )
        added = 0
        for asset_id in ids:
            if asset_id in existing:
                continue
            connection.execute(
                "INSERT INTO collection_items(collection_id,asset_id,position,added_at) VALUES(?,?,?,?)",
                (clean_id, asset_id, position, now),
            )
            position += 1
            added += 1
        if added:
            connection.execute(
                "UPDATE collections SET revision=revision+1,updated_at=? WHERE id=?",
                (now, clean_id),
            )
    return get_collection(database, clean_id)


def remove_collection_item(database: Database, collection_id: str, asset_id: str) -> bool:
    clean_collection = _clean_text(collection_id, "collection id", 200)
    clean_asset = _clean_text(asset_id, "asset id", 200)
    now = _utc_now()
    with database.transaction() as connection:
        exists = connection.execute(
            "SELECT id FROM collections WHERE id=? AND library_id=?",
            (clean_collection, database.library_id(connection)),
        ).fetchone()
        if exists is None:
            return False
        cursor = connection.execute(
            "DELETE FROM collection_items WHERE collection_id=? AND asset_id=?",
            (clean_collection, clean_asset),
        )
        if cursor.rowcount == 0:
            return False
        remaining = [
            str(row["asset_id"])
            for row in connection.execute(
                "SELECT asset_id FROM collection_items WHERE collection_id=? ORDER BY position,asset_id",
                (clean_collection,),
            ).fetchall()
        ]
        connection.execute(
            "UPDATE collection_items SET position=position+1000000000 WHERE collection_id=?",
            (clean_collection,),
        )
        for position, remaining_id in enumerate(remaining):
            connection.execute(
                "UPDATE collection_items SET position=? WHERE collection_id=? AND asset_id=?",
                (position, clean_collection, remaining_id),
            )
        connection.execute(
            "UPDATE collections SET revision=revision+1,updated_at=? WHERE id=?",
            (now, clean_collection),
        )
    return True


_ASSET_SELECT = """
    SELECT a.id AS asset_id,a.dataset_id,a.original_name,a.size_bytes,a.source_sha256,
           a.managed_sha256,a.sha256,a.hash_state,a.path_state,a.original_root_key,
           a.original_relpath,a.managed_root_key,a.managed_relpath,d.source_kind,
           d.status AS dataset_status,d.revision AS dataset_revision,d.updated_at AS dataset_updated_at
    FROM assets a JOIN datasets d ON d.id=a.dataset_id
"""


def _filter_clause(filters: Mapping[str, Any]) -> tuple[str, list[Any]]:
    where: list[str] = []
    params: list[Any] = []
    search = filters.get("search")
    if search:
        token = f"%{str(search).strip()}%"
        where.append(
            "(d.canonical_name LIKE ? OR d.sample_code LIKE ? OR d.workstream LIKE ? "
            "OR a.original_name LIKE ? OR a.sha256 LIKE ? OR a.source_sha256 LIKE ?)"
        )
        params.extend((token, token, token, token, token, token))
    for column, key in (
        ("d.workstream", "workstream"),
        ("d.material_state", "material_state"),
        ("d.modality", "modality"),
        ("d.status", "status"),
    ):
        if filters.get(key):
            where.append(f"{column}=?")
            params.append(str(filters[key]).upper())
    if filters.get("extension"):
        extension = str(filters["extension"]).lower()
        where.append("a.extension=?")
        params.append(extension if extension.startswith(".") else f".{extension}")
    if filters.get("date_from"):
        where.append("COALESCE(d.experiment_date,d.updated_at)>=?")
        params.append(str(filters["date_from"]))
    if filters.get("date_to"):
        where.append("COALESCE(d.experiment_date,d.updated_at)<=?")
        params.append(str(filters["date_to"]))
    return (" AND ".join(where), params)


def _load_selection_rows(
    database: Database,
    payload: Mapping[str, Any],
) -> tuple[int, str, dict[str, Any], list[dict[str, Any]]]:
    asset_ids = _clean_ids(payload.get("asset_ids") or (), "asset_ids")
    dataset_ids = _clean_ids(payload.get("dataset_ids") or (), "dataset_ids")
    filters = dict(payload.get("filter") or {}) if payload.get("filter") is not None else None
    excluded = _clean_ids(payload.get("excluded_asset_ids") or (), "excluded_asset_ids")
    explicit_ids = bool(asset_ids or dataset_ids)
    if int(explicit_ids) + int(filters is not None) != 1:
        raise ValueError("provide explicit asset/dataset ids or one filter, but not both")
    if excluded and filters is None:
        raise ValueError("excluded_asset_ids require filter selection")

    with database.connect() as connection:
        connection.execute("BEGIN")
        try:
            revision = database.catalog_revision(connection)
            library_id = database.library_id(connection)
            if explicit_ids:
                kind = "DATASET_IDS" if dataset_ids and not asset_ids else "ASSET_IDS"
                rows = []
                selected_asset_ids: set[str] = set()
                normalized = {}

                if dataset_ids:
                    placeholders = ",".join("?" for _ in dataset_ids)
                    existing = {
                        str(row["id"])
                        for row in connection.execute(
                            f"SELECT id FROM datasets WHERE id IN ({placeholders}) AND library_id=?",
                            (*dataset_ids, library_id),
                        ).fetchall()
                    }
                    missing = [dataset_id for dataset_id in dataset_ids if dataset_id not in existing]
                    if missing:
                        raise ValueError(f"{len(missing)} dataset id(s) do not exist in this library")
                    found_rows = connection.execute(
                        _ASSET_SELECT + f" WHERE d.id IN ({placeholders}) AND d.library_id=?",
                        (*dataset_ids, library_id),
                    ).fetchall()
                    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
                    for row in found_rows:
                        grouped[str(row["dataset_id"])].append(dict(row))
                    for dataset_id in dataset_ids:
                        for row in sorted(
                            grouped[dataset_id],
                            key=lambda item: (str(item["original_name"]).casefold(), str(item["asset_id"])),
                        ):
                            rows.append(row)
                            selected_asset_ids.add(str(row["asset_id"]))
                    normalized["dataset_ids"] = dataset_ids

                if asset_ids:
                    placeholders = ",".join("?" for _ in asset_ids)
                    found_rows = connection.execute(
                        _ASSET_SELECT + f" WHERE a.id IN ({placeholders}) AND d.library_id=?",
                        (*asset_ids, library_id),
                    ).fetchall()
                    by_id = {str(row["asset_id"]): dict(row) for row in found_rows}
                    missing = [asset_id for asset_id in asset_ids if asset_id not in by_id]
                    if missing:
                        raise ValueError(f"{len(missing)} asset id(s) do not exist in this library")
                    rows.extend(by_id[asset_id] for asset_id in asset_ids if asset_id not in selected_asset_ids)
                    normalized["asset_ids"] = asset_ids
            else:
                kind = "FILTER"
                assert filters is not None
                clause, params = _filter_clause(filters)
                where = "d.library_id=?"
                if clause:
                    where += " AND " + clause
                found_rows = connection.execute(
                    _ASSET_SELECT
                    + f" WHERE {where} ORDER BY d.updated_at DESC,d.id,a.original_name COLLATE NOCASE,a.id LIMIT ?",
                    (library_id, *params, _MAX_SELECTION_ASSETS + 1),
                ).fetchall()
                excluded_set = set(excluded)
                rows = [dict(row) for row in found_rows if str(row["asset_id"]) not in excluded_set]
                normalized_filter = {
                    key: value for key, value in sorted(filters.items()) if value is not None
                }
                normalized = {
                    "filter": normalized_filter,
                    "excluded_asset_ids": sorted(excluded_set),
                }
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    if not rows:
        raise ValueError("selection resolved to zero assets")
    if len(rows) > _MAX_SELECTION_ASSETS:
        raise ValueError(f"selection exceeds the {_MAX_SELECTION_ASSETS}-asset safety limit")
    return revision, kind, normalized, rows


def _inspect_item(database: Database, row: Mapping[str, Any], position: int) -> dict[str, Any]:
    issues: set[str] = set()
    if str(row["dataset_status"]).upper() == "STALE" or str(row["hash_state"]).upper() in {
        "SOURCE_CHANGED",
        "STALE_SOURCE",
    }:
        issues.add("STALE")
    if str(row["path_state"]).upper() != "VALID":
        issues.add("PATH_REVIEW")

    has_managed = bool(
        row.get("managed_root_key")
        and row.get("managed_relpath")
        and _valid_sha256(row.get("managed_sha256"))
    )
    root_key = row.get("managed_root_key") if has_managed else row.get("original_root_key")
    relative_path = row.get("managed_relpath") if has_managed else row.get("original_relpath")
    expected_hash = _valid_sha256(
        row.get("managed_sha256") if has_managed else row.get("source_sha256") or row.get("sha256")
    )
    if not root_key or not relative_path:
        issues.add("PATH_UNRESOLVED")
    if expected_hash is None:
        issues.add("HASH_UNAVAILABLE")

    if root_key and relative_path:
        try:
            path = database.root_mapper.resolve(str(root_key), str(relative_path), must_exist=False)
        except (OSError, PathLocationError):
            issues.add("PATH_UNSAFE")
        else:
            if not path.is_file():
                issues.add("MISSING")
            else:
                try:
                    actual_size = path.stat().st_size
                    if actual_size != int(row["size_bytes"]):
                        issues.add("SIZE_MISMATCH")
                    actual_hash = _sha256_file(path)
                    if expected_hash is None or actual_hash != expected_hash:
                        issues.add("HASH_MISMATCH")
                except FileNotFoundError:
                    issues.add("MISSING")
                except OSError:
                    issues.add("UNREADABLE")

    return {
        "asset_id": str(row["asset_id"]),
        "dataset_id": str(row["dataset_id"]),
        "position": position,
        "dataset_revision": int(row["dataset_revision"]),
        "original_name": str(row["original_name"]),
        "source_kind": str(row["source_kind"]),
        "selected_root_key": str(root_key) if root_key else None,
        "selected_relpath": str(relative_path) if relative_path else None,
        "selected_sha256": expected_hash,
        "size_bytes": int(row["size_bytes"]),
        "issue_codes": sorted(issues),
        "duplicate_of": None,
    }


def preview_selection(database: Database, payload: Mapping[str, Any]) -> dict[str, Any]:
    catalog_revision, kind, normalized, rows = _load_selection_rows(database, payload)
    items = [_inspect_item(database, row, position) for position, row in enumerate(rows)]

    by_hash: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        if item["selected_sha256"] and not (_BLOCKING_CODES & set(item["issue_codes"])):
            by_hash[str(item["selected_sha256"])].append(item)
        by_name[str(item["original_name"]).casefold()].append(item)
    for group in by_hash.values():
        if len(group) > 1:
            keeper = str(group[0]["asset_id"])
            for duplicate in group[1:]:
                duplicate["duplicate_of"] = keeper
            for item in group:
                item["issue_codes"] = sorted({*item["issue_codes"], "DUPLICATE_SHA256"})
    for group in by_name.values():
        if len(group) > 1:
            for item in group:
                item["issue_codes"] = sorted({*item["issue_codes"], "NAME_COLLISION"})

    counts = Counter(code for item in items for code in item["issue_codes"])
    blocking_item_count = sum(
        1 for item in items if _BLOCKING_CODES & set(item["issue_codes"])
    )
    warning_item_count = sum(
        1 for item in items if _WARNING_CODES & set(item["issue_codes"])
    )
    issues = {
        "counts": dict(sorted(counts.items())),
        "blocking_codes": sorted(code for code in counts if code in _BLOCKING_CODES),
        "warning_codes": sorted(code for code in counts if code in _WARNING_CODES),
        "blocking_item_count": blocking_item_count,
        "warning_item_count": warning_item_count,
    }
    selection_records = [
        {
            key: item[key]
            for key in (
                "asset_id",
                "dataset_id",
                "position",
                "dataset_revision",
                "original_name",
                "source_kind",
                "selected_root_key",
                "selected_relpath",
                "selected_sha256",
                "size_bytes",
                "issue_codes",
                "duplicate_of",
            )
        }
        for item in items
    ]
    selection_sha256 = hashlib.sha256(
        _canonical_json(selection_records).encode("utf-8")
    ).hexdigest()
    token = secrets.token_urlsafe(32)
    token_sha256 = hashlib.sha256(token.encode("ascii")).hexdigest()
    selection_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    created_at = now.isoformat(timespec="seconds")
    expires_at = (now + timedelta(minutes=15)).isoformat(timespec="seconds")
    status = "READY" if blocking_item_count == 0 else "BLOCKED"
    total_bytes = sum(int(item["size_bytes"]) for item in items)

    with database.transaction() as connection:
        if database.catalog_revision(connection) != catalog_revision:
            raise SelectionChanged("catalog changed while the selection preview was being verified; retry preview")
        connection.execute(
            """
            UPDATE selection_snapshots SET status='EXPIRED'
            WHERE status IN ('READY','BLOCKED') AND expires_at<=?
            """,
            (created_at,),
        )
        connection.execute(
            """
            INSERT INTO selection_snapshots(
                id,library_id,token_sha256,selection_kind,normalized_query_json,
                catalog_revision,status,selection_sha256,asset_count,total_bytes,
                issues_json,created_at,expires_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                selection_id,
                database.library_id(connection),
                token_sha256,
                kind,
                _canonical_json(normalized),
                catalog_revision,
                status,
                selection_sha256,
                len(items),
                total_bytes,
                _canonical_json(issues),
                created_at,
                expires_at,
            ),
        )
        for item in items:
            connection.execute(
                """
                INSERT INTO selection_snapshot_items(
                    selection_id,asset_id,dataset_id,position,dataset_revision,
                    original_name,source_kind,selected_root_key,selected_relpath,
                    selected_sha256,size_bytes,issue_codes_json,duplicate_of
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    selection_id,
                    item["asset_id"],
                    item["dataset_id"],
                    item["position"],
                    item["dataset_revision"],
                    item["original_name"],
                    item["source_kind"],
                    item["selected_root_key"],
                    item["selected_relpath"],
                    item["selected_sha256"],
                    item["size_bytes"],
                    _canonical_json(item["issue_codes"]),
                    item["duplicate_of"],
                ),
            )

    public_items = [
        {
            "asset_id": item["asset_id"],
            "dataset_id": item["dataset_id"],
            "position": item["position"],
            "original_name": item["original_name"],
            "source_kind": item["source_kind"],
            "source_sha256": item["selected_sha256"],
            "size_bytes": item["size_bytes"],
            "issue_codes": item["issue_codes"],
            "duplicate_of": item["duplicate_of"],
        }
        for item in items
    ]
    return {
        "selection_token": token,
        "expires_at": expires_at,
        "catalog_revision": catalog_revision,
        "selection_sha256": selection_sha256,
        "selection_kind": kind,
        "ready": status == "READY",
        "asset_count": len(items),
        "total_bytes": total_bytes,
        "issues": issues,
        "items": public_items,
    }


__all__ = [
    "SelectionChanged",
    "add_collection_items",
    "create_collection",
    "create_collection_from_selection",
    "get_collection",
    "list_collections",
    "preview_selection",
    "remove_collection_item",
    "update_collection",
]
