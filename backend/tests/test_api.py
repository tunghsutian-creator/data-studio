from __future__ import annotations

import json
import time
from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient

from backend.app import create_app
from backend.ai.contracts import AIClassification
from backend.ai.evidence import EvidenceBuilder
from backend.ai.provider import FakeLocalModelProvider, LocalModelProfile, ProviderIdentity
from backend.ai.worker import AIWorker
from backend.config import Settings
from backend.database import utc_now


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


def fake_ai_worker_factory(providers: list[FakeLocalModelProvider]):
    classification = AIClassification.model_validate(
        {
            "modality": "SEM",
            "workstream": "UNASSIGNED",
            "sample_id": None,
            "material": None,
            "test_method": "SEM",
            "conditions": {},
            "proposed_name": None,
            "confidence": 0.86,
            "evidence": [{"kind": "visual_pattern", "value": "microscopy-like image"}],
            "needs_review": True,
            "abstain_reason": None,
        }
    )

    def factory(settings: Settings, database):
        profile = LocalModelProfile.load(settings.ai_profile_path)
        provider = FakeLocalModelProvider(
            classification,
            identity=ProviderIdentity(
                provider="fake",
                profile_id="api-test-q8",
                model_id="api-test-model",
                quantization="Q8_0",
                device="CPU",
                model_revision="a" * 40,
                runtime_release="test-runtime",
                runtime_commit="b" * 40,
            ),
        )
        providers.append(provider)
        return AIWorker(
            database,
            EvidenceBuilder(database, settings.root_mapper(), profile),
            provider,
            registry_config={"temperature": 0, "seed": 42},
            worker_id="api-test-worker",
            lease_seconds=30,
            base_retry_delay_seconds=0,
        )

    return factory


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

        rejected = client.put(
            "/api/config",
            json={
                **config,
                "confidenceThreshold": 0.85,
                "model": "rules-only",
                "retainSource": True,
                "verifySha256": True,
            },
        )
        assert rejected.status_code == 400
        assert "automatic acceptance" in rejected.json()["detail"].lower()
        assert config["auto_accept_enabled"] is False
        assert config["reviewPolicy"] == "manual"


def test_api_paginates_and_searches_more_than_five_hundred_datasets(tmp_path: Path) -> None:
    application = create_app(api_settings(tmp_path))
    with TestClient(application) as client:
        database = application.state.database
        library_id = database.library_id()
        now = utc_now()
        with database.transaction() as connection:
            connection.executemany(
                """
                INSERT INTO datasets(
                    id,source_kind,group_key,source_root,canonical_name,workstream,
                    material_state,modality,status,created_at,updated_at,library_id,revision
                ) VALUES(?,?,?,?,?,'D_PA','VIRGIN','SEM','INDEXED',?,?,?,1)
                """,
                [
                    (
                        f"dataset-{index:04d}",
                        "reference",
                        f"group-{index:04d}",
                        str(tmp_path / "reference"),
                        f"DATASET_{index:04d}",
                        now,
                        now,
                        library_id,
                    )
                    for index in range(620)
                ],
            )

        collected: list[str] = []
        for offset in range(0, 620, 50):
            response = client.get(
                "/api/datasets",
                params={"limit": 50, "offset": offset, "sort": "canonical_name", "order": "asc"},
            )
            assert response.status_code == 200
            payload = response.json()
            assert payload["total"] == 620
            collected.extend(item["id"] for item in payload["items"])
        assert len(collected) == 620
        assert len(set(collected)) == 620
        assert collected[0] == "dataset-0000"
        assert collected[-1] == "dataset-0619"

        searched = client.get("/api/datasets", params={"query": "DATASET_0601", "limit": 20}).json()
        assert searched["total"] == 1
        assert searched["items"][0]["id"] == "dataset-0601"


def test_ai_api_runs_persistent_suggestion_without_changing_dataset(tmp_path: Path) -> None:
    settings = replace(
        api_settings(tmp_path),
        ai_enabled=True,
        ai_worker_poll_seconds=0.1,
    )
    settings.reference_root.mkdir(parents=True)
    source = settings.reference_root / "sample.dat"
    payload = b"header,value\nalpha,1\n"
    source.write_bytes(payload)
    providers: list[FakeLocalModelProvider] = []
    application = create_app(
        settings,
        ai_worker_factory=fake_ai_worker_factory(providers),
    )

    with TestClient(application) as client:
        scan = client.post("/api/scan", json={"source": "reference"})
        assert scan.status_code == 202
        dataset_id = client.get("/api/datasets").json()["items"][0]["id"]
        before = client.get(f"/api/datasets/{dataset_id}").json()
        health = client.get("/api/ai/health").json()
        assert health["enabled"] is True
        assert health["available"] is True
        assert health["worker_running"] is True
        assert health["model"]["runtime_release"] == "test-runtime"

        queued = client.post(
            f"/api/datasets/{dataset_id}/ai/analyze",
            json={"reason": "MANUAL_REQUEST", "max_attempts": 2},
        )
        assert queued.status_code == 202
        assert "lease_owner" not in queued.json()
        assert "lease_expires_at" not in queued.json()
        task_id = queued.json()["id"]
        detail = None
        for _ in range(100):
            detail = client.get(f"/api/ai/tasks/{task_id}").json()
            if detail["status"] in {"COMPLETED", "ABSTAINED", "FAILED"}:
                break
            time.sleep(0.01)

        assert detail is not None
        assert detail["status"] == "COMPLETED"
        assert "lease_owner" not in detail
        assert "lease_expires_at" not in detail
        assert detail["runs"][0]["classification"]["modality"] == "SEM"
        assert detail["runs"][0]["model"]["model_revision"] == "a" * 40
        assert len(client.get("/api/ai/tasks").json()["items"]) == 1
        dataset_ai = client.get(f"/api/datasets/{dataset_id}/ai").json()
        assert dataset_ai["items"][0]["runs"][0]["status"] == "SUCCEEDED"
        after = client.get(f"/api/datasets/{dataset_id}").json()
        assert before["revision"] == after["revision"]
        assert before["status"] == after["status"]
        assert before["modality"] == after["modality"]
        assert providers[0].requests[0].input_fingerprint == queued.json()["input_fingerprint"]
        invalid_status = client.get("/api/ai/tasks", params={"status": "not-a-state"})
        assert invalid_status.status_code == 400


def test_ai_api_is_explicitly_disabled_in_rules_only_mode(tmp_path: Path) -> None:
    with TestClient(create_app(api_settings(tmp_path))) as client:
        health = client.get("/api/ai/health")
        assert health.status_code == 200
        assert health.json()["status"] == "disabled"
        assert health.json()["worker_running"] is False
        rejected = client.post(
            "/api/datasets/missing/ai/analyze",
            json={"reason": "MANUAL_REQUEST"},
        )
        assert rejected.status_code == 409
        assert "disabled" in rejected.json()["detail"].lower()


def test_ai_service_can_be_enabled_and_disabled_through_config(tmp_path: Path) -> None:
    settings = replace(api_settings(tmp_path), config_file=tmp_path / "config.json")
    providers: list[FakeLocalModelProvider] = []
    application = create_app(
        settings,
        ai_worker_factory=fake_ai_worker_factory(providers),
    )

    with TestClient(application) as client:
        assert client.get("/api/ai/health").json()["status"] == "disabled"

        enabled = client.put("/api/config", json={"aiEnabled": True})
        assert enabled.status_code == 200
        assert enabled.json()["ai_enabled"] is True
        health = client.get("/api/ai/health").json()
        assert health["enabled"] is True
        assert health["worker_running"] is True
        assert health["model"]["model_id"] == "api-test-model"

        disabled = client.put("/api/config", json={"aiEnabled": False})
        assert disabled.status_code == 200
        assert disabled.json()["ai_enabled"] is False
        assert client.get("/api/ai/health").json()["status"] == "disabled"

    saved = json.loads(settings.config_file.read_text(encoding="utf-8"))
    assert saved["ai_enabled"] is False
    assert len(providers) == 1
