from __future__ import annotations

import hashlib
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .config import Settings
from .database import Database
from .scanner import sha256_file


_COMMIT_LOCK = threading.Lock()
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_name(value: str, fallback: str = "DATASET") -> str:
    cleaned = _UNSAFE_NAME.sub("_", value.strip()).strip(" ._-")
    return cleaned[:180] or fallback


def _compound_suffix(name: str) -> str:
    suffixes = Path(name).suffixes
    suffix = "".join(suffixes[-2:]) if len(suffixes) > 1 and suffixes[-2].lower() in {".is_tens", ".id_tens"} else (suffixes[-1] if suffixes else "")
    if not suffix:
        return ""
    return "." + _safe_name(suffix.lstrip("."), "bin")


def _commit_no_clobber(temporary: Path, target: Path) -> bool:
    """Atomically publish *temporary* only if *target* does not exist."""

    try:
        # Hard-link creation is an atomic O_EXCL-style commit on the same
        # volume. Removing the temporary name leaves the committed inode.
        os.link(temporary, target)
        temporary.unlink()
        return True
    except FileExistsError:
        return False
    except OSError as exc:
        if os.name != "nt":
            raise RuntimeError("Filesystem cannot provide a no-clobber atomic commit") from exc
        try:
            # Windows rename fails rather than replacing an existing target.
            os.rename(temporary, target)
            return True
        except FileExistsError:
            return False


def _copy_verified(source: Path, target: Path, expected_hash: str | None) -> tuple[str, Path]:
    before = source.stat()
    source_hash = sha256_file(source)
    if expected_hash and expected_hash != source_hash:
        raise RuntimeError(f"Source changed since scan; rescan before accepting: {source.name}")

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.parent / f".{target.name}.partial-{uuid.uuid4().hex}"
    copied_hash = hashlib.sha256()
    try:
        with source.open("rb") as incoming, temporary.open("xb") as outgoing:
            for chunk in iter(lambda: incoming.read(1024 * 1024), b""):
                copied_hash.update(chunk)
                outgoing.write(chunk)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        after = source.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise RuntimeError(f"Source changed while copying: {source.name}")
        if copied_hash.hexdigest() != source_hash or sha256_file(temporary) != source_hash:
            raise RuntimeError(f"SHA-256 verification failed: {source.name}")
        with _COMMIT_LOCK:
            candidates = [
                target,
                target.with_name(f"{target.stem}__{source_hash[:8]}{target.suffix}"),
            ]
            while True:
                candidate = candidates.pop(0) if candidates else target.with_name(
                    f"{target.stem}__{source_hash[:8]}_{uuid.uuid4().hex[:8]}{target.suffix}"
                )
                if candidate.exists():
                    if sha256_file(candidate) == source_hash:
                        temporary.unlink(missing_ok=True)
                        return source_hash, candidate
                    continue
                if _commit_no_clobber(temporary, candidate):
                    return source_hash, candidate
                # Another process won the race. Re-check without overwriting.
                if candidate.exists() and sha256_file(candidate) == source_hash:
                    temporary.unlink(missing_ok=True)
                    return source_hash, candidate
    finally:
        if temporary.exists():
            temporary.unlink(missing_ok=True)


def accept_dataset(
    settings: Settings,
    database: Database,
    dataset_id: str,
    *,
    note: str | None = None,
    job_id: str | None = None,
) -> dict[str, Any]:
    dataset = database.get_dataset(dataset_id)
    if not dataset:
        raise KeyError(dataset_id)
    if job_id:
        database.update_job(job_id, status="RUNNING", total=len(dataset["assets"]), message="Preparing verified managed copy")

    if dataset["source_kind"] == "reference":
        database.mark_resolution(dataset_id, "ACCEPTED", "ACCEPTED", note)
        if job_id:
            database.update_job(job_id, status="COMPLETED", current=0, total=0, message="Reference dataset accepted; source remained untouched")
        return database.get_dataset(dataset_id) or dataset

    if not settings.copy_on_accept:
        database.mark_resolution(dataset_id, "ACCEPTED", "ACCEPTED", note)
        if job_id:
            database.update_job(job_id, status="COMPLETED", current=0, total=0, message="Accepted without managed copy by configuration")
        return database.get_dataset(dataset_id) or dataset

    canonical = _safe_name(dataset.get("canonical_name") or dataset_id)
    destination_dir = settings.assert_vault_path(Path(settings.vault_root) / f"{canonical}__{dataset_id[:8]}")
    destination_dir.mkdir(parents=True, exist_ok=True)
    total = len(dataset["assets"])
    try:
        for index, asset in enumerate(dataset["assets"], start=1):
            source = settings.assert_source_path(asset["original_path"], "inbox", must_exist=True)
            age = time.time_ns() - source.stat().st_mtime_ns
            if age < int(settings.stable_file_seconds * 1_000_000_000):
                raise RuntimeError(f"Source is not stable yet: {source.name}")
            role = _safe_name(str(asset.get("role") or "PRIMARY"))
            suffix = _compound_suffix(str(asset.get("original_name") or source.name))
            target = settings.assert_vault_path(destination_dir / f"{canonical}__{index:02d}_{role}{suffix}")
            digest, managed = _copy_verified(source, target, asset.get("source_sha256") or asset.get("sha256"))
            database.set_managed_asset(asset["id"], str(managed), digest, job_id)
            if job_id:
                database.update_job(job_id, current=index, total=total, message=f"Verified {index}/{total}")
        database.mark_resolution(dataset_id, "ACCEPTED", "COMMITTED", note)
        database.add_operation(dataset_id, "SOURCE_RETAINED", {"source_kind": "inbox", "asset_count": total}, job_id)
        if job_id:
            database.update_job(job_id, status="COMPLETED", current=total, total=total, message="Verified managed copy committed; source retained")
    except Exception as exc:
        database.add_operation(dataset_id, "ACCEPT_FAILED", {"error": str(exc)}, job_id)
        if job_id:
            database.update_job(job_id, status="FAILED", error=str(exc))
        raise
    return database.get_dataset(dataset_id) or dataset


def defer_dataset(database: Database, dataset_id: str, note: str | None = None) -> dict[str, Any]:
    if not database.mark_resolution(dataset_id, "DEFERRED", "DEFERRED", note):
        raise KeyError(dataset_id)
    result = database.get_dataset(dataset_id)
    if result is None:
        raise KeyError(dataset_id)
    return result


__all__ = ["accept_dataset", "defer_dataset"]
