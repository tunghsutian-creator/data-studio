from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import logging
from pathlib import Path
import re
import threading
from typing import Any, Callable

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import Settings, load_settings
from .database import Database
from .ingestion import accept_dataset, defer_dataset
from .scanner import directory_signature, scan_source
from .schemas import (
    AIAnalyzeRequest,
    CollectionCreate,
    CollectionItemsRequest,
    CollectionUpdate,
    ConfigUpdate,
    DatasetUpdate,
    DecisionRequest,
    ExportCreateRequest,
    ExportPreviewRequest,
    RuleCreate,
    RuleUpdate,
    ScanRequest,
)
from .taxonomy import normalize_modality


logger = logging.getLogger(__name__)


AIWorkerFactory = Callable[[Settings, Database], Any]


def _database_for(settings: Settings) -> Database:
    return Database(
        settings.catalog_path,
        root_mappings=settings.root_mappings(),
        backup_root=settings.backup_root,
        machine_profile=settings.machine_profile,
        device_id=settings.device_id,
    )


def _database(request: Request) -> Database:
    return request.app.state.database


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _default_ai_worker(settings: Settings, database: Database):
    from .ai.evidence import EvidenceBuilder
    from .ai.worker import AIWorker, load_locked_llama_provider

    bundle = load_locked_llama_provider(
        settings.ai_profile_path,
        settings.ai_model_lock_path,
        timeout_seconds=settings.ai_provider_timeout_seconds,
    )
    try:
        return AIWorker(
            database,
            EvidenceBuilder(database, settings.root_mapper(), bundle.profile),
            bundle.provider,
            registry_config=bundle.registry_config,
            lease_seconds=settings.ai_lease_seconds,
            base_retry_delay_seconds=settings.ai_base_retry_delay_seconds,
        )
    except Exception:
        bundle.provider.close()
        raise


def _ai_service_for(
    settings: Settings,
    database: Database,
    worker_factory: AIWorkerFactory | None,
):
    if not settings.ai_enabled:
        return None
    from .ai.worker import AIWorkerService

    worker = (worker_factory or _default_ai_worker)(settings, database)
    return AIWorkerService(worker, poll_seconds=settings.ai_worker_poll_seconds)


def _export_service_for(database: Database):
    from .exports import ExportWorker, ExportWorkerService

    return ExportWorkerService(ExportWorker(database))


def _public_registered_model(item: dict[str, Any] | None) -> dict[str, Any] | None:
    if item is None:
        return None
    fields = {
        "id",
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
        "enabled",
    }
    return {key: value for key, value in item.items() if key in fields}


def _public_ai_task(item: dict[str, Any]) -> dict[str, Any]:
    fields = {
        "id",
        "dataset_id",
        "input_fingerprint",
        "reason",
        "status",
        "priority",
        "attempt_count",
        "max_attempts",
        "next_attempt_at",
        "last_error_code",
        "last_error_detail",
        "created_at",
        "updated_at",
        "started_at",
        "finished_at",
        "created",
    }
    return {key: value for key, value in item.items() if key in fields}


def _public_ai_run(item: dict[str, Any]) -> dict[str, Any]:
    fields = {
        "id",
        "task_id",
        "model_registry_id",
        "attempt_number",
        "status",
        "request_fingerprint",
        "response_sha256",
        "classification",
        "latency_ms",
        "error_code",
        "error_detail",
        "retryable",
        "started_at",
        "finished_at",
    }
    return {key: value for key, value in item.items() if key in fields}


def _ai_task_detail(database: Database, task: dict[str, Any]) -> dict[str, Any]:
    item = _public_ai_task(task)
    runs = [_public_ai_run(run) for run in database.list_ai_runs(str(task["id"]))]
    models: dict[str, dict[str, Any] | None] = {}
    for run in runs:
        registry_id = str(run["model_registry_id"])
        if registry_id not in models:
            models[registry_id] = _public_registered_model(
                database.get_registered_model(registry_id)
            )
        run["model"] = models[registry_id]
    item["runs"] = runs
    return item


def _configure_local_model(settings: Settings) -> bool:
    """Replace process-global classifier state from explicit app settings."""

    from . import classifier

    classifier.configure_model(None)
    artifact = Path(settings.model_path)
    if not artifact.is_file():
        return False
    # The classifier validates artifact shape and feature version and raises
    # rather than silently running an incompatible model.
    classifier.configure_model(artifact)
    return True


def _monitor_identity(settings: Settings, database: Database) -> tuple[str, str]:
    return (
        str(Path(settings.inbox_root).resolve(strict=False)),
        str(database.path.resolve(strict=False)),
    )


async def _wait_for_monitor(app: FastAPI, seconds: float) -> None:
    wakeup: asyncio.Event = app.state.monitor_wakeup
    try:
        await asyncio.wait_for(wakeup.wait(), timeout=max(0.02, seconds))
    except TimeoutError:
        pass
    finally:
        wakeup.clear()


def _wake_auto_scan(app: FastAPI) -> None:
    loop = getattr(app.state, "monitor_loop", None)
    wakeup = getattr(app.state, "monitor_wakeup", None)
    if loop and wakeup and not loop.is_closed():
        loop.call_soon_threadsafe(wakeup.set)


def _enqueue_auto_ai_candidates(
    app: FastAPI,
    settings_snapshot: Settings,
    database_snapshot: Database,
    source: str,
    scan_result: dict[str, Any],
) -> dict[str, Any]:
    summary = {"eligible": 0, "created": 0, "reused": 0, "failed": 0}
    if source != "inbox" or not settings_snapshot.ai_auto_inbox_enabled:
        return summary
    if scan_result.get("cancelled") or scan_result.get("errors") or scan_result.get("skipped"):
        return summary
    dataset_ids = sorted({str(value) for value in scan_result.get("dataset_ids") or () if value})
    if not dataset_ids:
        return summary

    from .ai.evidence import EvidenceBuildError
    from .ai.triggers import evaluate_inbox_ai_trigger

    with app.state.config_lock:
        if (
            app.state.settings is not settings_snapshot
            or app.state.database is not database_snapshot
        ):
            return summary
        service = getattr(app.state, "ai_service", None)
        if service is None or not service.running:
            return summary
        wake_needed = False
        for dataset_id in dataset_ids:
            dataset = database_snapshot.get_dataset(dataset_id)
            if dataset is None:
                continue
            decision = evaluate_inbox_ai_trigger(
                dataset,
                confidence_threshold=settings_snapshot.ai_trigger_confidence_threshold,
            )
            if not decision.eligible or decision.reason is None:
                continue
            summary["eligible"] += 1
            try:
                task = service.worker.enqueue(
                    dataset_id,
                    reason=decision.reason,
                    priority=decision.priority,
                    max_attempts=2,
                    reuse_completed=True,
                )
            except EvidenceBuildError as exc:
                summary["failed"] += 1
                logger.warning(
                    "Automatic local AI trigger skipped dataset %s (%s)",
                    dataset_id,
                    exc.code,
                )
            except Exception:
                summary["failed"] += 1
                logger.exception(
                    "Automatic local AI trigger failed for dataset %s",
                    dataset_id,
                )
            else:
                key = "created" if task.get("created") else "reused"
                summary[key] += 1
                if task.get("created") or task.get("status") in {"QUEUED", "RUNNING", "RETRY_WAIT"}:
                    wake_needed = True
        if wake_needed:
            service.wake()
    return summary


def _scan_source_with_ai(
    app: FastAPI,
    settings_snapshot: Settings,
    database_snapshot: Database,
    source: str,
    job_id: str | None = None,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    result = scan_source(
        settings_snapshot,
        database_snapshot,
        source,
        job_id,
        cancel_event,
    )
    result["ai_trigger"] = _enqueue_auto_ai_candidates(
        app,
        settings_snapshot,
        database_snapshot,
        source,
        result,
    )
    return result


async def _auto_scan_loop(app: FastAPI) -> None:
    last_identity: tuple[str, str] | None = None
    last_signature: str | None = None
    cancel_event: threading.Event = app.state.monitor_cancel

    while not cancel_event.is_set():
        with app.state.config_lock:
            settings_snapshot: Settings = app.state.settings
            database_snapshot: Database = app.state.database
        interval = float(settings_snapshot.auto_scan_seconds)
        if interval <= 0:
            last_identity = None
            last_signature = None
            await _wait_for_monitor(app, 3600.0)
            continue

        identity = _monitor_identity(settings_snapshot, database_snapshot)
        if identity != last_identity:
            last_identity = identity
            last_signature = None
        try:
            signature, file_count = await asyncio.to_thread(directory_signature, settings_snapshot, "inbox")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Unable to inspect inbox for automatic scan")
            await _wait_for_monitor(app, interval)
            continue
        if cancel_event.is_set():
            break

        if file_count == 0:
            # Empty inboxes do not create jobs, and unchanged empty inboxes are
            # free on subsequent polling cycles.
            last_signature = signature
        elif signature != last_signature:
            job: dict[str, Any] | None = None
            with app.state.config_lock:
                current_settings: Settings = app.state.settings
                current_database: Database = app.state.database
                if (
                    float(current_settings.auto_scan_seconds) > 0
                    and _monitor_identity(current_settings, current_database) == identity
                    and current_database.active_job_count() == 0
                ):
                    job = current_database.create_job("AUTO_SCAN", "inbox")
            if job:
                try:
                    result = await asyncio.to_thread(
                        _scan_source_with_ai,
                        app,
                        settings_snapshot,
                        database_snapshot,
                        "inbox",
                        job["id"],
                        cancel_event,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # scan_source records FAILED. Keeping last_signature stale
                    # intentionally retries this directory state next cycle.
                    logger.exception("Automatic inbox scan failed")
                else:
                    if not result.get("cancelled") and not result.get("skipped") and not result.get("errors"):
                        last_signature = signature
        await _wait_for_monitor(app, interval)


def create_app(
    settings: Settings | None = None,
    *,
    ai_worker_factory: AIWorkerFactory | None = None,
) -> FastAPI:
    configured = settings or load_settings()
    database = _database_for(configured)

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        configured.ensure_runtime_directories()
        database.initialize()
        database.recover_interrupted_jobs()
        database.recover_ai_tasks()
        from .exports import recover_interrupted_exports

        recover_interrupted_exports(database)
        application.state.monitor_loop = asyncio.get_running_loop()
        application.state.monitor_wakeup = asyncio.Event()
        application.state.monitor_cancel = threading.Event()
        monitor_task: asyncio.Task | None = None
        try:
            application.state.model_loaded = _configure_local_model(configured)
            ai_service = _ai_service_for(configured, database, ai_worker_factory)
            application.state.ai_service = ai_service
            if ai_service:
                ai_service.start()
            export_service = _export_service_for(database)
            application.state.export_service = export_service
            export_service.start()
            monitor_task = asyncio.create_task(
                _auto_scan_loop(application),
                name="academic-vault-inbox-monitor",
            )
            application.state.monitor_task = monitor_task
            yield
        finally:
            ai_service = getattr(application.state, "ai_service", None)
            if ai_service and not ai_service.stop(timeout_seconds=5.0):
                logger.warning("Local AI worker is still finishing an in-flight request during shutdown")
            export_service = getattr(application.state, "export_service", None)
            if export_service and not export_service.stop(timeout_seconds=5.0):
                logger.warning("Export worker is still finishing an in-flight job during shutdown")
            application.state.monitor_cancel.set()
            application.state.monitor_wakeup.set()
            if monitor_task:
                try:
                    await asyncio.wait_for(monitor_task, timeout=5.0)
                except TimeoutError:
                    monitor_task.cancel()
                    try:
                        await monitor_task
                    except asyncio.CancelledError:
                        pass
                except asyncio.CancelledError:
                    pass
            from .classifier import configure_model

            configure_model(None)
            application.state.model_loaded = False

    app = FastAPI(title="Academic Vault", version="0.1.0", lifespan=lifespan)
    app.state.settings = configured
    app.state.database = database
    app.state.ai_service = None
    app.state.export_service = None
    app.state.config_lock = threading.RLock()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://127.0.0.1:5173",
            "http://localhost:5173",
            "http://127.0.0.1:8765",
            "http://localhost:8765",
        ],
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "OPTIONS"],
        allow_headers=["*"],
    )

    @app.exception_handler(ValueError)
    async def invalid_value(_: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.get("/api/health")
    def health(request: Request) -> dict[str, Any]:
        db = _database(request)
        ai_service = getattr(request.app.state, "ai_service", None)
        export_service = getattr(request.app.state, "export_service", None)
        from .exports import export_counts

        return {
            "status": "ok",
            "service": "academic-vault",
            "local_only": True,
            "catalog": str(db.path),
            "journal_mode": db.journal_mode(),
            "schema_version": db.schema_version(),
            "library_id": db.library_id(),
            "model_loaded": bool(getattr(request.app.state, "model_loaded", False)),
            "ai_enabled": ai_service is not None,
            "ai_worker_running": bool(ai_service and ai_service.running),
            "export_worker_running": bool(export_service and export_service.running),
            "export_queue": export_counts(db),
        }

    @app.get("/api/config")
    def get_config(request: Request) -> dict[str, Any]:
        return _settings(request).public_dict()

    @app.put("/api/config")
    def put_config(payload: ConfigUpdate, request: Request) -> dict[str, Any]:
        with request.app.state.config_lock:
            previous = _settings(request)
            current_database = _database(request)
            from .exports import active_export_count, recover_interrupted_exports

            if (
                current_database.active_job_count()
                or current_database.active_ai_task_count()
                or active_export_count(current_database)
            ):
                raise HTTPException(
                    status_code=409,
                    detail="Wait for active scan/accept/AI/export jobs before changing configuration",
                )
            raw = payload.model_dump(exclude_none=True)
            for ui_name, setting_name in {
                "referencePath": "reference_root",
                "inboxPath": "inbox_root",
                "vaultPath": "vault_root",
                "catalogPath": "catalog_path",
                "exportPath": "export_root",
                "backupPath": "backup_root",
                "aiEnabled": "ai_enabled",
                "aiProfilePath": "ai_profile_path",
                "aiModelLockPath": "ai_model_lock_path",
                "aiAutoInboxEnabled": "ai_auto_inbox_enabled",
                "aiTriggerConfidenceThreshold": "ai_trigger_confidence_threshold",
            }.items():
                if ui_name in raw:
                    raw[setting_name] = raw[ui_name]
            if raw.get("auto_accept_enabled") or raw.get("autoAcceptEnabled"):
                raise HTTPException(
                    status_code=400,
                    detail="Automatic acceptance is disabled; every Inbox dataset requires human review",
                )
            if "auto_accept_threshold" in raw or "confidenceThreshold" in raw:
                raise HTTPException(
                    status_code=400,
                    detail="Confidence-based automatic acceptance has been removed; use the manual review policy",
                )
            if "scanInterval" in raw:
                interval_value = raw["scanInterval"]
                if isinstance(interval_value, (int, float)):
                    raw["auto_scan_seconds"] = float(interval_value)
                else:
                    match = re.search(r"\d+", str(interval_value))
                    if match:
                        raw["auto_scan_seconds"] = int(match.group()) * 60
            if raw.get("autoScan") is False:
                raw["auto_scan_seconds"] = 0
            elif raw.get("autoScan") is True and previous.auto_scan_seconds == 0 and "auto_scan_seconds" not in raw:
                raw["auto_scan_seconds"] = 900
            updated = previous.updated(raw)
            updated.ensure_runtime_directories()
            replacement = _database_for(updated)
            replacement.initialize()
            replacement.recover_interrupted_jobs()
            replacement.recover_ai_tasks()
            recover_interrupted_exports(replacement)
            from . import classifier

            previous_model_bundle = classifier._MODEL_BUNDLE
            replacement_service = None
            replacement_export_service = None
            try:
                replacement_service = _ai_service_for(updated, replacement, ai_worker_factory)
                replacement_export_service = _export_service_for(replacement)
                model_loaded = _configure_local_model(updated)
                updated.save()
            except Exception:
                if replacement_service:
                    replacement_service.stop(timeout_seconds=0)
                if replacement_export_service:
                    replacement_export_service.stop(timeout_seconds=0)
                classifier._MODEL_BUNDLE = previous_model_bundle
                raise
            previous_export_service = getattr(request.app.state, "export_service", None)
            if previous_export_service and not previous_export_service.stop(timeout_seconds=5.0):
                if replacement_service:
                    replacement_service.stop(timeout_seconds=0)
                classifier._MODEL_BUNDLE = previous_model_bundle
                previous.save()
                raise HTTPException(
                    status_code=409,
                    detail="Export worker is still stopping; configuration was not changed",
                )
            previous_service = getattr(request.app.state, "ai_service", None)
            if previous_service and not previous_service.stop(timeout_seconds=5.0):
                if replacement_service:
                    replacement_service.stop(timeout_seconds=0)
                if previous_export_service:
                    previous_export_service.start()
                classifier._MODEL_BUNDLE = previous_model_bundle
                previous.save()
                raise HTTPException(
                    status_code=409,
                    detail="Local AI worker is still stopping; configuration was not changed",
                )
            request.app.state.settings = updated
            request.app.state.database = replacement
            request.app.state.ai_service = replacement_service
            request.app.state.export_service = replacement_export_service
            request.app.state.model_loaded = model_loaded
            if replacement_service:
                replacement_service.start()
            if replacement_export_service:
                replacement_export_service.start()
            _wake_auto_scan(request.app)
            return updated.public_dict()

    @app.get("/api/summary")
    def summary(request: Request) -> dict[str, Any]:
        return _database(request).summary()

    @app.get("/api/filters")
    def filters(request: Request) -> dict[str, Any]:
        return _database(request).filters()

    @app.post("/api/collections", status_code=201)
    def create_named_collection(payload: CollectionCreate, request: Request) -> dict[str, Any]:
        from .exports import create_collection

        return create_collection(_database(request), payload.name, payload.purpose)

    @app.get("/api/collections")
    def named_collections(request: Request) -> dict[str, Any]:
        from .exports import list_collections

        items = list_collections(_database(request))
        return {"items": items, "total": len(items)}

    @app.get("/api/collections/{collection_id}")
    def named_collection_detail(collection_id: str, request: Request) -> dict[str, Any]:
        from .exports import get_collection

        item = get_collection(_database(request), collection_id)
        if item is None:
            raise HTTPException(status_code=404, detail="Collection not found")
        return item

    @app.patch("/api/collections/{collection_id}")
    def patch_named_collection(
        collection_id: str,
        payload: CollectionUpdate,
        request: Request,
    ) -> dict[str, Any]:
        from .exports import update_collection

        item = update_collection(
            _database(request),
            collection_id,
            payload.model_dump(exclude_unset=True),
        )
        if item is None:
            raise HTTPException(status_code=404, detail="Collection not found")
        return item

    @app.post("/api/collections/{collection_id}/items")
    def add_named_collection_items(
        collection_id: str,
        payload: CollectionItemsRequest,
        request: Request,
    ) -> dict[str, Any]:
        from .exports import add_collection_items

        item = add_collection_items(_database(request), collection_id, payload.asset_ids)
        if item is None:
            raise HTTPException(status_code=404, detail="Collection not found")
        return item

    @app.delete("/api/collections/{collection_id}/items/{asset_id}")
    def delete_named_collection_item(
        collection_id: str,
        asset_id: str,
        request: Request,
    ) -> dict[str, bool]:
        from .exports import remove_collection_item

        if not remove_collection_item(_database(request), collection_id, asset_id):
            raise HTTPException(status_code=404, detail="Collection item not found")
        return {"removed": True}

    @app.post("/api/exports/preview")
    def export_preview(payload: ExportPreviewRequest, request: Request) -> dict[str, Any]:
        from .exports import SelectionChanged, preview_selection

        try:
            return preview_selection(
                _database(request),
                payload.model_dump(mode="json"),
            )
        except SelectionChanged as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/exports", status_code=202)
    def start_export(payload: ExportCreateRequest, request: Request) -> dict[str, Any]:
        from .exports import ExportFailure, create_export

        try:
            item = create_export(
                _database(request),
                payload.model_dump(mode="json"),
            )
        except ExportFailure as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": exc.code, "message": str(exc)},
            ) from exc
        service = getattr(request.app.state, "export_service", None)
        if service:
            service.wake()
        return item

    @app.get("/api/exports")
    def exports(request: Request, limit: int = Query(100, ge=1, le=500)) -> dict[str, Any]:
        from .exports import list_exports

        items = list_exports(_database(request), limit=limit)
        return {"items": items, "total": len(items)}

    @app.get("/api/exports/{export_id}")
    def export_detail(export_id: str, request: Request) -> dict[str, Any]:
        from .exports import get_export

        item = get_export(_database(request), export_id)
        if item is None:
            raise HTTPException(status_code=404, detail="Export not found")
        return item

    @app.get("/api/exports/{export_id}/manifest")
    def export_manifest(export_id: str, request: Request) -> dict[str, Any]:
        from .exports import ExportFailure, load_export_manifest

        try:
            return load_export_manifest(_database(request), export_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="Export not found") from None
        except ExportFailure as exc:
            raise HTTPException(
                status_code=409,
                detail={"code": exc.code, "message": str(exc)},
            ) from exc

    @app.get("/api/ai/health")
    def ai_health(request: Request) -> dict[str, Any]:
        db = _database(request)
        service = getattr(request.app.state, "ai_service", None)
        if service is None:
            return {
                "enabled": False,
                "available": False,
                "status": "disabled",
                "local_only": True,
                "worker_running": False,
                "queue": db.ai_task_counts(),
                "model": None,
            }
        health_payload = service.worker.health()
        health_payload.update(
            {
                "enabled": True,
                "local_only": True,
                "worker_running": service.running,
                "queue": db.ai_task_counts(),
                "model": _public_registered_model(service.worker.registered_model),
                "worker_error": service.last_error,
            }
        )
        return health_payload

    @app.get("/api/ai/tasks")
    def ai_tasks(
        request: Request,
        dataset_id: str | None = None,
        status: str | None = None,
        limit: int = Query(100, ge=1, le=500),
    ) -> dict[str, Any]:
        try:
            items = _database(request).list_ai_tasks(
                dataset_id=dataset_id,
                status=status,
                limit=limit,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        items = [_public_ai_task(item) for item in items]
        return {"items": items, "total": len(items)}

    @app.get("/api/ai/tasks/{task_id}")
    def ai_task_detail(task_id: str, request: Request) -> dict[str, Any]:
        db = _database(request)
        task = db.get_ai_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="AI task not found")
        return _ai_task_detail(db, task)

    @app.get("/api/datasets")
    def datasets(
        request: Request,
        search: str | None = None,
        query: str | None = None,
        workstream: str | None = None,
        material_state: str | None = None,
        modality: str | None = None,
        status: str | None = None,
        extension: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        sort: str = "updated_at",
        order: str = "desc",
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0),
    ) -> dict[str, Any]:
        return _database(request).list_datasets(
            search=query or search,
            workstream=workstream,
            material_state=material_state,
            modality=modality,
            status=status,
            extension=extension,
            date_from=date_from,
            date_to=date_to,
            sort=sort,
            order=order,
            limit=limit,
            offset=offset,
        )

    @app.get("/api/datasets/{dataset_id}")
    def dataset_detail(dataset_id: str, request: Request) -> dict[str, Any]:
        item = _database(request).get_dataset(dataset_id)
        if not item:
            raise HTTPException(status_code=404, detail="Dataset not found")
        return item

    @app.get("/api/datasets/{dataset_id}/ai")
    def dataset_ai(dataset_id: str, request: Request) -> dict[str, Any]:
        db = _database(request)
        if db.get_dataset(dataset_id) is None:
            raise HTTPException(status_code=404, detail="Dataset not found")
        tasks = [
            _ai_task_detail(db, task)
            for task in db.list_ai_tasks(dataset_id=dataset_id, limit=20)
        ]
        return {"items": tasks, "total": len(tasks)}

    @app.post("/api/datasets/{dataset_id}/ai/analyze", status_code=202)
    def analyze_dataset(
        dataset_id: str,
        payload: AIAnalyzeRequest,
        request: Request,
    ) -> dict[str, Any]:
        from .ai.evidence import EvidenceBuildError

        with request.app.state.config_lock:
            service = getattr(request.app.state, "ai_service", None)
            if service is None:
                raise HTTPException(status_code=409, detail="Local AI is disabled")
            try:
                task = service.worker.enqueue(
                    dataset_id,
                    reason=payload.reason,
                    priority=payload.priority,
                    max_attempts=payload.max_attempts,
                )
            except KeyError:
                raise HTTPException(status_code=404, detail="Dataset not found") from None
            except EvidenceBuildError as exc:
                raise HTTPException(
                    status_code=409,
                    detail={"code": exc.code, "message": str(exc)},
                ) from exc
            service.wake()
            return _public_ai_task(task)

    @app.patch("/api/datasets/{dataset_id}")
    @app.put("/api/datasets/{dataset_id}")
    def modify_dataset(dataset_id: str, payload: DatasetUpdate, request: Request) -> dict[str, Any]:
        item = _database(request).update_dataset(dataset_id, payload.model_dump(exclude_none=True))
        if not item:
            raise HTTPException(status_code=404, detail="Dataset not found")
        return item

    @app.post("/api/scan", status_code=202)
    def scan(payload: ScanRequest, background_tasks: BackgroundTasks, request: Request) -> dict[str, Any]:
        with request.app.state.config_lock:
            db = _database(request)
            settings_snapshot = _settings(request)
            job = db.create_job("SCAN", payload.source)
        background_tasks.add_task(
            _scan_source_with_ai,
            request.app,
            settings_snapshot,
            db,
            payload.source,
            job["id"],
        )
        return job

    @app.get("/api/jobs")
    def jobs(request: Request, limit: int = Query(50, ge=1, le=200)) -> dict[str, Any]:
        items = _database(request).list_jobs(limit)
        return {"items": items, "total": len(items)}

    @app.get("/api/jobs/{job_id}")
    def job_detail(job_id: str, request: Request) -> dict[str, Any]:
        item = _database(request).get_job(job_id)
        if not item:
            raise HTTPException(status_code=404, detail="Job not found")
        return item

    @app.post("/api/datasets/{dataset_id}/accept")
    def accept(dataset_id: str, request: Request, payload: DecisionRequest | None = None) -> dict[str, Any]:
        with request.app.state.config_lock:
            db = _database(request)
            settings_snapshot = _settings(request)
            job = db.create_job("ACCEPT", None)
        try:
            item = accept_dataset(settings_snapshot, db, dataset_id, note=payload.note if payload else None, job_id=job["id"])
        except KeyError:
            db.update_job(job["id"], status="FAILED", error="Dataset not found")
            raise HTTPException(status_code=404, detail="Dataset not found") from None
        except (OSError, RuntimeError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        item["job_id"] = job["id"]
        return item

    @app.post("/api/datasets/{dataset_id}/defer")
    def defer(dataset_id: str, request: Request, payload: DecisionRequest | None = None) -> dict[str, Any]:
        try:
            return defer_dataset(_database(request), dataset_id, payload.note if payload else None)
        except KeyError:
            raise HTTPException(status_code=404, detail="Dataset not found") from None

    @app.get("/api/rules")
    def rules(request: Request) -> dict[str, Any]:
        from .classifier import RULES

        builtins = []
        for index, rule in enumerate(RULES):
            item = rule.to_dict()
            item.update({"name": item["id"], "scope": item["label"], "priority": index + 1, "version": "builtin-v1", "enabled": True})
            builtins.append(item)
        user_rules = []
        for item in _database(request).list_rules():
            item.setdefault("description", f"User rule pattern: {item.get('pattern', '')}")
            item.setdefault("scope", item.get("label", "UNKNOWN"))
            user_rules.append(item)
        items = [*builtins, *user_rules]
        return {"items": items, "total": len(items)}

    @app.post("/api/rules", status_code=201)
    def create_rule(payload: RuleCreate, request: Request) -> dict[str, Any]:
        try:
            re.compile(payload.pattern)
            normalized = normalize_modality(payload.label).value
        except (re.error, ValueError) as exc:
            raise HTTPException(status_code=400, detail=f"Invalid rule: {exc}") from exc
        values = payload.model_dump()
        values["label"] = normalized
        return _database(request).create_rule(values)

    @app.patch("/api/rules/{rule_id}")
    def patch_rule(rule_id: str, payload: RuleUpdate, request: Request) -> dict[str, Any]:
        values = payload.model_dump(exclude_none=True)
        try:
            if "pattern" in values:
                re.compile(values["pattern"])
            if "label" in values:
                values["label"] = normalize_modality(values["label"]).value
        except (re.error, ValueError) as exc:
            raise HTTPException(status_code=400, detail=f"Invalid rule: {exc}") from exc
        item = _database(request).update_rule(rule_id, values)
        if not item:
            raise HTTPException(status_code=404, detail="Rule not found")
        return item

    frontend_dist = Path(__file__).resolve().parent.parent / "frontend" / "dist"
    if frontend_dist.is_dir():
        app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="frontend")

    return app


app = create_app()


__all__ = ["app", "create_app"]
