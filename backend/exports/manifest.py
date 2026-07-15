"""Versioned, deterministic export manifest rendering."""

from __future__ import annotations

import csv
import hashlib
import io
import json
from typing import Any, Mapping, Sequence


MANIFEST_SCHEMA_VERSION = "1.0"
APP_VERSION = "0.1.0"
TAXONOMY_VERSION = "builtin-v1"
_FIELDS = (
    "position",
    "dataset_id",
    "asset_id",
    "original_name",
    "exported_relpath",
    "source_kind",
    "source_sha256",
    "exported_sha256",
    "size_bytes",
    "duplicate_of",
)


def _csv_cell(value: Any) -> Any:
    # RFC 4180 quoting alone does not stop spreadsheet applications from
    # interpreting an untrusted filename as a formula when the CSV is opened.
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def canonical_json_bytes(value: Any) -> bytes:
    """Render the restricted manifest value domain using JCS-compatible JSON.

    Manifest keys are fixed ASCII strings and numeric values are integers, so
    Python's sorted compact UTF-8 representation matches the RFC 8785 rules
    needed by this schema without a third-party serializer.
    """

    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")


def build_manifest(
    export: Mapping[str, Any],
    *,
    catalog_snapshot_revision: int,
    database_schema_version: int,
    items: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    stable_items = [
        {field: item.get(field) for field in _FIELDS}
        for item in sorted(items, key=lambda item: (int(item["position"]), str(item["asset_id"])))
    ]
    return {
        "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
        "export_id": str(export["id"]),
        "created_at_utc": str(export["created_at"]),
        "app_version": APP_VERSION,
        "database_schema_version": int(database_schema_version),
        "taxonomy_version": TAXONOMY_VERSION,
        "library_id": str(export["library_id"]),
        "catalog_snapshot_revision": int(catalog_snapshot_revision),
        "export_mode": str(export["export_mode"]),
        "duplicate_policy": str(export["duplicate_policy"]),
        "items": stable_items,
    }


def render_manifest_csv(items: Sequence[Mapping[str, Any]]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(
        stream,
        fieldnames=list(_FIELDS),
        extrasaction="ignore",
        lineterminator="\n",
    )
    writer.writeheader()
    for item in sorted(items, key=lambda item: (int(item["position"]), str(item["asset_id"]))):
        writer.writerow({field: _csv_cell(item.get(field)) for field in _FIELDS})
    return stream.getvalue().encode("utf-8")


def render_readme(export: Mapping[str, Any], item_count: int, total_bytes: int) -> bytes:
    purpose = str(export.get("purpose") or "Not specified")
    text = (
        "# Academic Vault export\n\n"
        f"- Export ID: `{export['id']}`\n"
        f"- Name: {export['name']}\n"
        f"- Purpose: {purpose}\n"
        f"- Mode: `{export['export_mode']}`\n"
        f"- Duplicate policy: `{export['duplicate_policy']}`\n"
        f"- Selected assets: {item_count}\n"
        f"- Selected bytes: {total_bytes}\n\n"
        "`manifest.json` is authoritative. Verify the relative paths listed in "
        "`checksums.sha256` before using the bundle.\n"
    )
    return text.encode("utf-8")


def render_checksums(entries: Mapping[str, str]) -> bytes:
    lines: list[str] = []
    for relative_path in sorted(entries):
        digest = str(entries[relative_path]).lower()
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ValueError("checksum entry must be a lowercase SHA-256 digest")
        if "\n" in relative_path or "\r" in relative_path or relative_path.startswith(("/", "\\")):
            raise ValueError("checksum path must be a safe relative path")
        lines.append(f"{digest}  {relative_path}")
    return ("\n".join(lines) + "\n").encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


__all__ = [
    "APP_VERSION",
    "MANIFEST_SCHEMA_VERSION",
    "TAXONOMY_VERSION",
    "build_manifest",
    "canonical_json_bytes",
    "render_checksums",
    "render_manifest_csv",
    "render_readme",
    "sha256_bytes",
]
