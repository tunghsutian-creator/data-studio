from __future__ import annotations

from pathlib import Path

import joblib
import pytest
from fastapi.testclient import TestClient

from backend import classifier
from backend.app import create_app
from backend.classifier import MODEL_FEATURE_VERSION
from backend.config import Settings


class DummyModel:
    classes_ = ("SEM",)

    def predict_proba(self, rows):
        return [[1.0] for _ in rows]


@pytest.fixture(autouse=True)
def clear_process_model():
    classifier.configure_model(None)
    yield
    classifier.configure_model(None)


def model_settings(tmp_path: Path, model_path: Path) -> Settings:
    return Settings(
        reference_root=tmp_path / "instrument-data",
        inbox_root=tmp_path / "inbox",
        vault_root=tmp_path / "vault",
        quarantine_root=tmp_path / "quarantine",
        catalog_path=tmp_path / "catalog" / "vault.sqlite3",
        model_path=model_path,
        auto_scan_seconds=0,
    )


def write_artifact(path: Path, *, feature_version: int = MODEL_FEATURE_VERSION) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(
        {"model": DummyModel(), "feature_version": feature_version, "name": "dummy-local-model"},
        path,
    )


def test_public_config_reports_model_only_when_artifact_exists(tmp_path: Path) -> None:
    artifact = tmp_path / "optional-models" / "classifier.joblib"
    settings = model_settings(tmp_path, artifact)
    assert settings.public_dict()["model"] == "rules-only"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"placeholder")
    assert settings.public_dict()["model"] == "local-lightweight-v1"


def test_lifespan_clears_previous_global_model_and_shutdown_clears_loaded_model(tmp_path: Path) -> None:
    missing = tmp_path / "optional-models" / "missing.joblib"
    classifier._MODEL_BUNDLE = object()
    with TestClient(create_app(model_settings(tmp_path, missing))) as client:
        assert classifier._MODEL_BUNDLE is None
        assert client.get("/api/health").json()["model_loaded"] is False

    artifact = tmp_path / "optional-models" / "valid.joblib"
    write_artifact(artifact)
    with TestClient(create_app(model_settings(tmp_path, artifact))) as client:
        assert classifier._MODEL_BUNDLE is not None
        assert client.get("/api/health").json()["model_loaded"] is True
        assert client.get("/api/config").json()["model"] == "local-lightweight-v1"
    assert classifier._MODEL_BUNDLE is None


def test_model_is_reconfigured_when_settings_change(tmp_path: Path) -> None:
    missing = tmp_path / "optional-models" / "missing.joblib"
    artifact = tmp_path / "alternate-models" / "valid.joblib"
    write_artifact(artifact)
    with TestClient(create_app(model_settings(tmp_path, missing))) as client:
        loaded = client.put("/api/config", json={"model_path": str(artifact)})
        assert loaded.status_code == 200
        assert loaded.json()["model"] == "local-lightweight-v1"
        assert classifier._MODEL_BUNDLE is not None

        cleared = client.put("/api/config", json={"model_path": str(missing)})
        assert cleared.status_code == 200
        assert cleared.json()["model"] == "rules-only"
        assert classifier._MODEL_BUNDLE is None


def test_invalid_model_version_fails_startup_and_config_change_without_pollution(tmp_path: Path) -> None:
    invalid = tmp_path / "invalid-models" / "bad.joblib"
    write_artifact(invalid, feature_version=MODEL_FEATURE_VERSION + 1)
    with pytest.raises(ValueError, match="feature version"):
        with TestClient(create_app(model_settings(tmp_path, invalid))):
            pass
    assert classifier._MODEL_BUNDLE is None

    missing = tmp_path / "optional-models" / "missing.joblib"
    application = create_app(model_settings(tmp_path, missing))
    with TestClient(application) as client:
        response = client.put("/api/config", json={"model_path": str(invalid)})
        assert response.status_code == 400
        assert "feature version" in response.json()["detail"]
        assert classifier._MODEL_BUNDLE is None
        assert application.state.settings.model_path == missing

