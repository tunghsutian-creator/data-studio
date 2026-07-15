from __future__ import annotations

import time
import importlib
from pathlib import Path
from typing import Callable

from fastapi.testclient import TestClient

from backend.app import create_app
from backend.config import Settings


def automatic_settings(tmp_path: Path, *, interval: float, stable: float = 0) -> Settings:
    return Settings(
        reference_root=tmp_path / "instrument-data",
        inbox_root=tmp_path / "inbox",
        vault_root=tmp_path / "vault",
        quarantine_root=tmp_path / "quarantine",
        catalog_path=tmp_path / "catalog" / "vault.sqlite3",
        model_path=tmp_path / "models" / "model.joblib",
        auto_scan_seconds=interval,
        stable_file_seconds=stable,
    )


def wait_until(predicate: Callable[[], bool], timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition was not met before timeout")


def test_changed_inbox_is_automatically_indexed_once_without_accepting(tmp_path: Path) -> None:
    settings = automatic_settings(tmp_path, interval=0.05)
    with TestClient(create_app(settings)) as client:
        source = settings.inbox_root / "A7.id_tens"
        source.write_text("Sample,Strain,Stress\nA7,0.1,12\n", encoding="utf-8")

        wait_until(lambda: client.get("/api/datasets").json()["total"] == 1)
        jobs = client.get("/api/jobs").json()["items"]
        automatic_jobs = [item for item in jobs if item["kind"] == "AUTO_SCAN"]
        assert len(automatic_jobs) == 1
        assert automatic_jobs[0]["status"] == "COMPLETED"
        assert client.get("/api/datasets").json()["items"][0]["status"] == "REVIEW"
        assert source.exists()
        assert not any(settings.vault_root.rglob("*"))

        time.sleep(0.2)
        jobs_after_idle_cycles = client.get("/api/jobs").json()["items"]
        assert len([item for item in jobs_after_idle_cycles if item["kind"] == "AUTO_SCAN"]) == 1


def test_unstable_inbox_file_is_retried_until_it_can_be_indexed(tmp_path: Path) -> None:
    settings = automatic_settings(tmp_path, interval=0.04, stable=0.14)
    with TestClient(create_app(settings)) as client:
        source = settings.inbox_root / "fresh.id_tens"
        source.write_text("Sample,Strain,Stress\nF1,0.1,8\n", encoding="utf-8")

        wait_until(lambda: client.get("/api/datasets").json()["total"] == 1)
        automatic_jobs = [item for item in client.get("/api/jobs").json()["items"] if item["kind"] == "AUTO_SCAN"]
        assert len(automatic_jobs) >= 2
        assert all(item["status"] == "COMPLETED" for item in automatic_jobs)


def test_zero_interval_disables_automatic_scan(tmp_path: Path) -> None:
    settings = automatic_settings(tmp_path, interval=0)
    settings.inbox_root.mkdir(parents=True)
    (settings.inbox_root / "waiting.id_tens").write_bytes(b"Sample,Strain,Stress\nW1,0.1,9\n")

    application = create_app(settings)
    with TestClient(application) as client:
        time.sleep(0.15)
        assert client.get("/api/datasets").json()["total"] == 0
        assert client.get("/api/jobs").json()["total"] == 0
        monitor_task = application.state.monitor_task
    assert monitor_task.done()


def test_configuration_can_enable_monitor_without_restart(tmp_path: Path) -> None:
    settings = automatic_settings(tmp_path, interval=0)
    settings.inbox_root.mkdir(parents=True)
    (settings.inbox_root / "enabled.id_tens").write_bytes(b"Sample,Strain,Stress\nE1,0.1,9\n")

    with TestClient(create_app(settings)) as client:
        assert client.get("/api/datasets").json()["total"] == 0
        response = client.put("/api/config", json={"auto_scan_seconds": 0.05})
        assert response.status_code == 200
        wait_until(lambda: client.get("/api/datasets").json()["total"] == 1)


def test_scan_result_with_error_is_retried_next_cycle(tmp_path: Path, monkeypatch) -> None:
    settings = automatic_settings(tmp_path, interval=0.04)
    app_module = importlib.import_module("backend.app")
    real_scan = app_module.scan_source
    attempts = 0

    def fail_once(settings_arg, database, source, job_id, cancel_event):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            database.update_job(job_id, status="RUNNING", total=1)
            database.update_job(job_id, status="COMPLETED", current=1, total=1, message="Synthetic read error")
            return {"source": source, "scanned": 0, "skipped": 0, "errors": 1}
        return real_scan(settings_arg, database, source, job_id, cancel_event)

    monkeypatch.setattr(app_module, "scan_source", fail_once)
    with TestClient(create_app(settings)) as client:
        (settings.inbox_root / "retry.id_tens").write_bytes(b"Sample,Strain,Stress\nR1,0.1,9\n")
        wait_until(lambda: client.get("/api/datasets").json()["total"] == 1)
        automatic_jobs = [item for item in client.get("/api/jobs").json()["items"] if item["kind"] == "AUTO_SCAN"]
        assert attempts >= 2
        assert len(automatic_jobs) >= 2
