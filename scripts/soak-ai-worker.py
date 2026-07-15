"""Run a durable local-AI queue soak against a disposable catalog snapshot."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from backend.ai.benchmark import (  # noqa: E402
    assert_report_path_is_outside_repository,
    select_diagnostic_datasets,
)
from backend.ai.evidence import EvidenceBuildError, EvidenceBuilder  # noqa: E402
from backend.ai.model_lock import load_model_lock  # noqa: E402
from backend.ai.soak import run_worker_soak  # noqa: E402
from backend.ai.worker import AIWorker, load_locked_llama_provider  # noqa: E402
from backend.config import Settings  # noqa: E402
from backend.database import Database  # noqa: E402


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, payload: dict) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _snapshot_catalog(source_path: Path, destination_path: Path) -> str:
    source = source_path.expanduser().resolve(strict=True)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    source_connection = sqlite3.connect(
        f"file:{source.as_posix()}?mode=ro",
        uri=True,
        timeout=30,
    )
    destination_connection = sqlite3.connect(destination_path)
    try:
        source_connection.execute("PRAGMA query_only=ON")
        source_integrity = [row[0] for row in source_connection.execute("PRAGMA integrity_check")]
        if source_integrity != ["ok"]:
            raise ValueError("source catalog integrity_check failed")
        source_connection.backup(destination_connection)
        destination_connection.commit()
        copied_integrity = [row[0] for row in destination_connection.execute("PRAGMA integrity_check")]
        if copied_integrity != ["ok"]:
            raise ValueError("soak catalog snapshot integrity_check failed")
    finally:
        destination_connection.close()
        source_connection.close()
    return _sha256_file(destination_path)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _guard_output(path: Path, data_root: Path) -> Path:
    output = assert_report_path_is_outside_repository(path, REPOSITORY_ROOT)
    protected = (
        data_root / "data ref",
        data_root / "inbox",
        data_root / "vault",
        data_root / "catalog",
        data_root / "models",
        data_root / "runtimes",
        data_root / "backups",
        data_root / "exports",
    )
    for root in protected:
        if _is_within(output, root.resolve(strict=False)):
            raise ValueError("soak output may not be written inside a data/model/runtime root")
    return output


def _verify_model_files(model_dir: Path, model_lock_path: Path) -> dict[str, dict[str, object]]:
    lock = load_model_lock(model_lock_path)
    results: dict[str, dict[str, object]] = {}
    for name, artifact in (
        ("model", lock.model),
        ("vision_projector", lock.vision_projector),
    ):
        path = (model_dir / artifact.filename).resolve(strict=True)
        actual_size = path.stat().st_size
        actual_digest = _sha256_file(path)
        if actual_size != artifact.bytes or actual_digest != artifact.sha256:
            raise ValueError(f"locked {name} file does not match size/SHA-256")
        results[name] = {
            "filename": artifact.filename,
            "bytes": actual_size,
            "sha256": actual_digest,
            "verified": True,
        }
    return results


def _parser() -> argparse.ArgumentParser:
    data_root = Path(r"C:\research data")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    revision = "f982a07559d4a2f6c8744d840bf6fccab30eea96"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=data_root / "catalog" / "academic_vault.sqlite3")
    parser.add_argument("--reference-root", type=Path, default=data_root / "data ref")
    parser.add_argument("--inbox-root", type=Path, default=data_root / "inbox")
    parser.add_argument("--vault-root", type=Path, default=data_root / "vault")
    parser.add_argument(
        "--profile",
        type=Path,
        default=REPOSITORY_ROOT / "profiles" / "windows-rtx5080.json",
    )
    parser.add_argument(
        "--model-lock",
        type=Path,
        default=REPOSITORY_ROOT / "profiles" / "windows-model-lock.json",
    )
    parser.add_argument(
        "--model-dir",
        type=Path,
        default=data_root / "models" / "qwen3-vl-8b-instruct" / revision,
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=data_root / "evaluation" / "ai-soak" / timestamp,
    )
    parser.add_argument("--duration-seconds", type=float, default=7200)
    parser.add_argument("--max-tasks", type=int, default=0, help="0 means duration-controlled")
    parser.add_argument("--cases", type=int, default=30)
    parser.add_argument("--queue-depth", type=int, default=5)
    parser.add_argument("--restart-after", type=int, default=25, help="0 disables worker restart")
    parser.add_argument("--checkpoint-every", type=int, default=10)
    parser.add_argument("--provider-timeout", type=float, default=120)
    parser.add_argument("--lease-seconds", type=int, default=180)
    parser.add_argument("--retry-delay-seconds", type=int, default=5)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.cases < 1 or args.queue_depth < 1 or args.queue_depth > args.cases:
        raise ValueError("cases must be positive and queue-depth may not exceed cases")
    data_root = Path(r"C:\research data").resolve(strict=False)
    output_dir = _guard_output(args.output_dir, data_root)
    output_dir.mkdir(parents=True, exist_ok=False)
    report_path = output_dir / "soak-report.json"
    snapshot_path = output_dir / "working" / "catalog.sqlite3"

    base_report: dict = {
        "kind": "academic-vault-durable-ai-soak",
        "status": "PREPARING",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_catalog_open_mode": "read-only-online-backup",
        "raw_sources_read_only": True,
        "output_inside_git": False,
    }
    _atomic_json(report_path, base_report)
    try:
        locked_files = _verify_model_files(args.model_dir, args.model_lock)
        snapshot_sha256 = _snapshot_catalog(args.catalog, snapshot_path)
        settings = Settings(
            reference_root=args.reference_root.resolve(strict=True),
            inbox_root=args.inbox_root.resolve(strict=False),
            vault_root=args.vault_root.resolve(strict=False),
            quarantine_root=output_dir / "quarantine",
            catalog_path=snapshot_path,
            model_path=output_dir / "unused-lightweight-model.joblib",
            ai_profile_path=args.profile.resolve(strict=True),
            ai_model_lock_path=args.model_lock.resolve(strict=True),
            export_root=output_dir / "exports",
            backup_root=output_dir / "backups",
            auto_scan_seconds=0,
            stable_file_seconds=0,
        )
        settings.ensure_runtime_directories()
        database = Database(
            settings.catalog_path,
            root_mappings=settings.root_mappings(),
            backup_root=settings.backup_root,
            machine_profile="windows-phase3-soak",
            device_id="phase3-soak",
        )
        database.initialize()
        migration = database.last_migration_report

        preflight_builder_bundle = load_locked_llama_provider(
            settings.ai_profile_path,
            settings.ai_model_lock_path,
            timeout_seconds=args.provider_timeout,
        )
        preflight_builder_bundle.provider.close()
        profile = preflight_builder_bundle.profile
        preflight_builder = EvidenceBuilder(database, settings.root_mapper(), profile)
        diagnostics = select_diagnostic_datasets(
            snapshot_path,
            limit=min(500, max(args.cases * 4, args.cases)),
        )
        dataset_ids: list[str] = []
        preflight_errors: Counter[str] = Counter()
        for item in diagnostics:
            if len(dataset_ids) >= args.cases:
                break
            try:
                preflight_builder.build(item.dataset_id)
            except EvidenceBuildError as exc:
                preflight_errors[exc.code] += 1
            else:
                dataset_ids.append(item.dataset_id)
        if len(dataset_ids) < args.cases:
            raise ValueError(
                f"only {len(dataset_ids)} datasets passed bounded evidence preflight; requested {args.cases}"
            )

        worker_number = 0

        def worker_factory() -> AIWorker:
            nonlocal worker_number
            worker_number += 1
            bundle = load_locked_llama_provider(
                settings.ai_profile_path,
                settings.ai_model_lock_path,
                timeout_seconds=args.provider_timeout,
            )
            try:
                return AIWorker(
                    database,
                    EvidenceBuilder(database, settings.root_mapper(), bundle.profile),
                    bundle.provider,
                    registry_config=bundle.registry_config,
                    worker_id=f"phase3-soak-{worker_number}-{uuid.uuid4().hex[:8]}",
                    lease_seconds=args.lease_seconds,
                    base_retry_delay_seconds=args.retry_delay_seconds,
                )
            except Exception:
                bundle.provider.close()
                raise

        provenance = {
            **base_report,
            "status": "RUNNING",
            "snapshot_catalog_sha256_before_migration": snapshot_sha256,
            "schema_migration": {
                "previous_version": migration.previous_version,
                "current_version": migration.current_version,
                "applied_versions": list(migration.applied_versions),
            },
            "locked_files": locked_files,
            "preflight": {
                "selected_cases": len(dataset_ids),
                "excluded_error_codes": dict(sorted(preflight_errors.items())),
                "paths_or_filenames_persisted": False,
            },
        }

        def checkpoint(progress: dict) -> None:
            _atomic_json(report_path, {**provenance, "progress": progress})
            print(
                json.dumps(
                    {
                        "status": "RUNNING",
                        "terminal_tasks": progress["terminal_tasks"],
                        "active_tasks": progress["active_tasks"],
                        "elapsed_seconds": progress["elapsed_seconds"],
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

        result = run_worker_soak(
            database,
            dataset_ids,
            worker_factory,
            duration_seconds=args.duration_seconds,
            max_tasks=args.max_tasks or None,
            queue_depth=args.queue_depth,
            restart_after_tasks=args.restart_after or None,
            checkpoint_every=args.checkpoint_every,
            progress_callback=checkpoint,
            sample_gpu=True,
        )
        final_report = {**provenance, **result, "status": "PASSED" if result["passed"] else "FAILED"}
        _atomic_json(report_path, final_report)
        print(
            json.dumps(
                {
                    "output": str(report_path),
                    "status": final_report["status"],
                    "terminal_tasks": result["terminal_tasks"],
                    "elapsed_seconds": result["elapsed_seconds"],
                },
                ensure_ascii=False,
            )
        )
        return 0 if result["passed"] else 2
    except Exception as exc:
        failure = {
            **base_report,
            "status": "FAILED",
            "failed_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
        _atomic_json(report_path, failure)
        print(json.dumps({"output": str(report_path), "status": "FAILED", "error": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
