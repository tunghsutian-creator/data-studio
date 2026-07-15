from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from backend.app import create_app
from backend.config import Settings


def api_settings(tmp_path: Path) -> Settings:
    return Settings(
        reference_root=tmp_path / "reference",
        inbox_root=tmp_path / "inbox",
        vault_root=tmp_path / "vault",
        quarantine_root=tmp_path / "quarantine",
        catalog_path=tmp_path / "catalog" / "vault.sqlite3",
        model_path=tmp_path / "models" / "model.joblib",
        auto_scan_seconds=0,
        stable_file_seconds=0,
    )


def test_api_scan_detail_and_bodyless_accept(tmp_path: Path) -> None:
    settings = api_settings(tmp_path)
    settings.inbox_root.mkdir(parents=True)
    source = settings.inbox_root / "A7.id_tens"
    source.write_text("Sample,Strain,Stress\nA7,0.1,12\n", encoding="utf-8")

    with TestClient(create_app(settings)) as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["journal_mode"] == "delete"

        scan = client.post("/api/scan", json={"source": "inbox"})
        assert scan.status_code == 202
        assert client.get(f"/api/jobs/{scan.json()['id']}").json()["status"] == "COMPLETED"

        rows = client.get("/api/datasets").json()
        assert rows["total"] == 1
        dataset_id = rows["items"][0]["id"]
        accepted = client.post(f"/api/datasets/{dataset_id}/accept")
        assert accepted.status_code == 200
        assert accepted.json()["status"] == "COMMITTED"
        assert source.exists()


def test_api_contract_aliases_and_rule_shape(tmp_path: Path) -> None:
    with TestClient(create_app(api_settings(tmp_path))) as client:
        summary = client.get("/api/summary").json()
        assert {"datasets", "review", "storage", "high", "medium", "low"} <= summary.keys()
        config = client.get("/api/config").json()
        assert config["inboxPath"] == config["inbox_root"]
        rules = client.get("/api/rules").json()["items"]
        assert rules and {"name", "description", "scope", "enabled"} <= rules[0].keys()

        saved = client.put(
            "/api/config",
            json={
                **config,
                "confidenceThreshold": 0.85,
                "model": "rules-only",
                "retainSource": True,
                "verifySha256": True,
            },
        )
        assert saved.status_code == 200
        assert saved.json()["auto_accept_threshold"] == 0.85
