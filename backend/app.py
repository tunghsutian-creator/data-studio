from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import logging
from pathlib import Path
import re
import threading
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import Settings, load_settings
from .database import Database
from .ingestion import accept_dataset, defer_dataset
from .scanner import directory_signature, scan_source
from .schemas import ConfigUpdate, DatasetUpdate, DecisionRequest, RuleCreate, RuleUpdate, ScanRequest
from .taxonomy import normalize_modality


logger = logging.getLogger(__name__)


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
                        scan_source,
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


def create_app(settings: Settings | None = None) -> FastAPI:
    configured = settings or load_settings()
    database = _database_for(configured)

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        configured.ensure_runtime_directories()
        database.initialize()
        database.recover_interrupted_jobs()
        application.state.model_loaded = _configure_local_model(configured)
        application.state.monitor_loop = asyncio.get_running_loop()
        application.state.monitor_wakeup = asyncio.Event()
        application.state.monitor_cancel = threading.Event()
        monitor_task = asyncio.create_task(_auto_scan_loop(application), name="academic-vault-inbox-monitor")
        application.state.monitor_task = monitor_task
        try:
            yield
        finally:
            application.state.monitor_cancel.set()
            application.state.monitor_wakeup.set()
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
        return {
            "status": "ok",
            "service": "academic-vault",
            "local_only": True,
            "catalog": str(db.path),
            "journal_mode": db.journal_mode(),
            "schema_version": db.schema_version(),
            "library_id": db.library_id(),
            "model_loaded": bool(getattr(request.app.state, "model_loaded", False)),
        }

    @app.get("/api/config")
    def get_config(request: Request) -> dict[str, Any]:
        return _settings(request).public_dict()

    @app.put("/api/config")
    def put_config(payload: ConfigUpdate, request: Request) -> dict[str, Any]:
        with request.app.state.config_lock:
            previous = _settings(request)
            current_database = _database(request)
            if current_database.active_job_count():
                raise HTTPException(status_code=409, detail="Wait for active scan/accept jobs before changing configuration")
            raw = payload.model_dump(exclude_none=True)
            for ui_name, setting_name in {
                "referencePath": "reference_root",
                "inboxPath": "inbox_root",
                "vaultPath": "vault_root",
                "catalogPath": "catalog_path",
                "exportPath": "export_root",
                "backupPath": "backup_root",
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
            from . import classifier

            previous_model_bundle = classifier._MODEL_BUNDLE
            try:
                model_loaded = _configure_local_model(updated)
                updated.save()
            except Exception:
                classifier._MODEL_BUNDLE = previous_model_bundle
                raise
            request.app.state.settings = updated
            request.app.state.database = replacement
            request.app.state.model_loaded = model_loaded
            _wake_auto_scan(request.app)
            return updated.public_dict()

    @app.get("/api/summary")
    def summary(request: Request) -> dict[str, Any]:
        return _database(request).summary()

    @app.get("/api/filters")
    def filters(request: Request) -> dict[str, Any]:
        return _database(request).filters()

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
        background_tasks.add_task(scan_source, settings_snapshot, db, payload.source, job["id"])
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
