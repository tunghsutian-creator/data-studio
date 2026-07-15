from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from typing import Any, Iterable

from .config import Settings
from .database import Database


_SAFE_TOKEN = re.compile(r"[^A-Za-z0-9._-]+")
_SEM_DERIVATIVES = ("_white_backplate", "_white_halo", "_backplate", "_halo")
_TENSILE_SUFFIXES = (".is_tens.pdf", ".is_tens", ".id_tens")
_INNER_WORKSTREAMS = {
    "reference": "REFERENCE",
    "pa adr recycle": "PA_ADR_RECYCLE",
    "d pa": "D_PA",
    "udc": "UDC",
}


class ScanCancelled(RuntimeError):
    """Raised internally when application shutdown cancels an automatic scan."""


def sha256_file(path: Path, chunk_size: int = 1024 * 1024, cancel_event: Event | None = None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(chunk_size), b""):
            if cancel_event and cancel_event.is_set():
                raise ScanCancelled("scan cancelled during shutdown")
            digest.update(chunk)
    return digest.hexdigest()


def _safe_token(value: Any, fallback: str = "UNKNOWN") -> str:
    cleaned = _SAFE_TOKEN.sub("_", str(value or "").strip()).strip("._-")
    return cleaned[:80] or fallback


def _classification(path: Path, siblings: list[str]) -> dict[str, Any]:
    # Import lazily so database-only maintenance still works if optional model
    # dependencies are not installed.
    from .classifier import classify_file

    return classify_file(path, sibling_names=siblings).to_dict()


def _apply_user_rules(path: Path, result: dict[str, Any], rules: list[dict[str, Any]]) -> dict[str, Any]:
    matches: list[dict[str, Any]] = []
    searchable = path.as_posix()
    for rule in rules:
        if not rule.get("enabled"):
            continue
        try:
            if re.search(str(rule["pattern"]), searchable, flags=re.IGNORECASE):
                matches.append(rule)
        except re.error:
            continue
    if not matches:
        return result

    updated = dict(result)
    evidence = list(updated.get("evidence") or [])
    labels = {str(item["label"]).upper() for item in matches}
    chosen = matches[0]
    chosen_label = str(chosen["label"]).upper()
    existing_label = str(updated.get("label") or "UNKNOWN").upper()
    evidence.append(f"user rule {chosen['id']} matched configured path pattern")
    if len(labels) > 1 or (existing_label not in {"UNKNOWN", chosen_label} and float(updated.get("confidence") or 0) >= 0.9):
        updated["conflict"] = True
        updated["confidence"] = min(float(updated.get("confidence") or 0), 0.79)
        evidence.append(f"conflict: user rule proposed {chosen_label} while classifier proposed {existing_label}")
    else:
        updated["label"] = chosen_label
        updated["confidence"] = max(float(updated.get("confidence") or 0), 0.96)
        updated["method"] = f"user-rule:{chosen['id']}"
    updated["evidence"] = evidence
    return updated


def _context_folder_key(value: str) -> str:
    without_order = re.sub(r"^\s*\d+\s*[-_.]?\s*", "", value)
    return re.sub(r"[^a-z0-9]+", " ", without_order.lower()).strip()


def enrich_workstream_context(path: Path, root: Path, result: dict[str, Any]) -> dict[str, Any]:
    """Add path-derived project context only after modality classification."""

    try:
        directories = path.relative_to(root).parts[:-1]
    except ValueError:
        return result
    chosen: str | None = None
    matched_folder: str | None = None
    vitrimer_folder: str | None = None
    for directory in reversed(directories):
        key = _context_folder_key(directory)
        if key in _INNER_WORKSTREAMS:
            chosen = _INNER_WORKSTREAMS[key]
            matched_folder = directory
            break
    for directory in directories:
        if _context_folder_key(directory) == "vitrimer":
            vitrimer_folder = directory
    if chosen is None and vitrimer_folder is not None:
        chosen = "VITRIMER"
        matched_folder = vitrimer_folder
    if chosen is None:
        return result

    updated = dict(result)
    metadata = dict(updated.get("metadata") or {})
    context_evidence = f"context workstream {chosen} from relative folder '{matched_folder}'"
    metadata["workstream"] = chosen
    metadata_evidence = list(metadata.get("evidence") or [])
    if context_evidence not in metadata_evidence:
        metadata_evidence.append(context_evidence)
    metadata["evidence"] = metadata_evidence
    evidence = list(updated.get("evidence") or [])
    if context_evidence not in evidence:
        evidence.append(context_evidence)
    updated["metadata"] = metadata
    updated["evidence"] = evidence
    return updated


def _strip_tensile_name(name: str) -> str:
    lowered = name.lower()
    for suffix in _TENSILE_SUFFIXES:
        if lowered.endswith(suffix):
            return name[: -len(suffix)]
    return Path(name).stem


def group_key(path: Path, root: Path, classification: dict[str, Any]) -> str:
    relative_parent = path.parent.relative_to(root).as_posix().lower()
    modality = str(classification.get("label") or "UNKNOWN").upper()
    metadata = classification.get("metadata") or {}
    explicit = metadata.get("group_key")
    if explicit:
        return f"{relative_parent}/{str(explicit).lower()}"

    parent_name = path.parent.name.lower()
    if modality == "TENSILE":
        if parent_name.endswith(".is_tens_exports") or parent_name.endswith("_exports"):
            base = re.sub(r"(?:\.is_tens)?_exports$", "", path.parent.name, flags=re.I)
            return f"{path.parent.parent.relative_to(root).as_posix().lower()}/{base.lower()}"
        return f"{relative_parent}/{_strip_tensile_name(path.name).lower()}"

    stem = path.stem
    if modality == "SEM":
        lowered = stem.lower()
        for suffix in _SEM_DERIVATIVES:
            if lowered.endswith(suffix):
                stem = stem[: -len(suffix)]
                break
        return f"{relative_parent}/{stem.lower()}"

    if modality == "SIMULATION":
        # Simulation exports are multi-file bundles; their directory is the
        # most stable conservative grouping boundary.
        return f"{relative_parent}/__simulation_bundle__"

    return f"{relative_parent}/{stem.lower()}"


def canonical_name(group: str, classification: dict[str, Any]) -> str:
    metadata = classification.get("metadata") or {}
    parts = [
        metadata.get("workstream") or "UNASSIGNED",
        metadata.get("material") or metadata.get("material_state") or "UNKNOWN",
        classification.get("label") or "UNKNOWN",
        metadata.get("sample") or metadata.get("sample_code"),
        metadata.get("date") or metadata.get("experiment_date"),
    ]
    tokens = [_safe_token(part) for part in parts if part]
    digest = hashlib.sha256(group.encode("utf-8")).hexdigest()[:8].upper()
    return "_".join((*tokens, digest))[:240]


def _iter_files(root: Path, settings: Settings, source: str) -> Iterable[Path]:
    if not root.exists():
        return
    for current, directory_names, file_names in os.walk(root, followlinks=False):
        current_path = Path(current)
        safe_directories: list[str] = []
        for name in directory_names:
            candidate = current_path / name
            try:
                settings.assert_source_path(candidate, source, must_exist=True)
            except (ValueError, OSError):
                continue
            if not candidate.is_symlink():
                safe_directories.append(name)
        directory_names[:] = safe_directories
        for name in file_names:
            candidate = current_path / name
            if candidate.is_symlink():
                continue
            try:
                resolved = settings.assert_source_path(candidate, source, must_exist=True)
            except (ValueError, OSError):
                continue
            if resolved.is_file():
                yield resolved


def directory_signature(settings: Settings, source: str = "inbox") -> tuple[str, int]:
    """Return a cheap metadata signature without reading file contents."""

    root = settings.source_root(source).resolve(strict=False)
    digest = hashlib.sha256()
    count = 0
    if not root.exists():
        digest.update(b"missing")
        return digest.hexdigest(), 0
    for path in _iter_files(root, settings, source):
        try:
            stat = path.stat()
            relative = path.relative_to(root).as_posix()
            record = f"{relative}\0{stat.st_size}\0{stat.st_mtime_ns}\n"
        except (OSError, ValueError) as exc:
            record = f"{path}\0ERROR:{type(exc).__name__}\n"
        digest.update(record.encode("utf-8", errors="surrogatepass"))
        count += 1
    return digest.hexdigest(), count


def scan_source(
    settings: Settings,
    database: Database,
    source: str,
    job_id: str | None = None,
    cancel_event: Event | None = None,
) -> dict[str, Any]:
    root = settings.source_root(source).resolve(strict=False)
    if job_id:
        database.update_job(job_id, status="RUNNING", message=f"Scanning {source}")
    if not root.exists():
        result = {"source": source, "root": str(root), "scanned": 0, "skipped": 0, "errors": 0}
        if job_id:
            database.update_job(job_id, status="COMPLETED", total=0, current=0, message="Source root does not exist")
        return result

    files = list(_iter_files(root, settings, source))
    total = len(files)
    scanned = skipped = errors = 0
    stable_ns = int(settings.stable_file_seconds * 1_000_000_000)
    sibling_cache: dict[Path, list[str]] = {}
    user_rules = database.list_rules()

    try:
        for index, path in enumerate(files, start=1):
            try:
                if cancel_event and cancel_event.is_set():
                    raise ScanCancelled("scan cancelled during shutdown")
                stat = path.stat()
                if source == "inbox" and time.time_ns() - stat.st_mtime_ns < stable_ns:
                    skipped += 1
                    continue
                siblings = sibling_cache.setdefault(path.parent, [item.name for item in path.parent.iterdir() if item.is_file()])
                classified = _classification(path, siblings)
                ruled = _apply_user_rules(path, classified, user_rules)
                # Deliberately post-classification: project folders enrich
                # catalog metadata and never become modality-model features.
                result = enrich_workstream_context(path, root, ruled)
                key = group_key(path, root, result)
                database.upsert_scanned_file(
                    source_kind=source,
                    source_root=str(root),
                    group_key=key,
                    path=str(path),
                    size_bytes=stat.st_size,
                    mtime_ns=stat.st_mtime_ns,
                    modified_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(timespec="seconds"),
                    sha256=sha256_file(path, cancel_event=cancel_event),
                    classification=result,
                    canonical_name=canonical_name(key, result),
                    role=str((result.get("metadata") or {}).get("lifecycle") or "PRIMARY").upper(),
                    mime_type=mimetypes.guess_type(path.name)[0],
                )
                scanned += 1
            except (OSError, ValueError, UnicodeError):
                errors += 1
            finally:
                if job_id:
                    database.update_job(job_id, current=index, total=total, message=f"Indexed {scanned} file(s)")
    except ScanCancelled:
        output = {
            "source": source,
            "root": str(root),
            "scanned": scanned,
            "skipped": skipped,
            "errors": errors,
            "cancelled": True,
        }
        if job_id:
            database.update_job(
                job_id,
                status="CANCELLED",
                current=scanned + skipped + errors,
                total=total,
                message="Automatic scan cancelled during shutdown",
            )
        return output
    except Exception as exc:
        if job_id:
            database.update_job(job_id, status="FAILED", error=str(exc), current=scanned + skipped + errors, total=total)
        raise

    output = {"source": source, "root": str(root), "scanned": scanned, "skipped": skipped, "errors": errors}
    if job_id:
        database.update_job(job_id, status="COMPLETED", current=total, total=total, message=f"Indexed {scanned}; skipped {skipped}; errors {errors}")
    return output


__all__ = [
    "canonical_name",
    "directory_signature",
    "enrich_workstream_context",
    "group_key",
    "scan_source",
    "sha256_file",
]
