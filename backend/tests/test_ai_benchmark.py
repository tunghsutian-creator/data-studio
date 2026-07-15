from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.ai.benchmark import (
    BenchmarkProfile,
    DiagnosticDataset,
    EvidencePack,
    InferenceResult,
    assert_report_path_is_outside_repository,
    run_benchmark,
)
from backend.ai.contracts import AIClassification, output_json_schema, parse_classification_response
from backend.ai.model_lock import load_model_lock


def profile(tmp_path: Path) -> BenchmarkProfile:
    payload = {
        "profile_schema_version": 1,
        "profile_id": "test-q8",
        "provider": "llama.cpp",
        "model_id": "test-model",
        "quantization": "Q8_0",
        "device": "CPU",
        "server": {
            "host": "127.0.0.1",
            "port": 8877,
            "context_tokens": 8192,
            "max_output_tokens": 512,
            "temperature": 0,
            "seed": 42,
            "parallel_requests": 1,
            "flash_attention": False,
        },
        "evidence": {
            "max_text_bytes_per_asset": 1024,
            "max_text_assets": 1,
            "max_images": 1,
            "max_image_edge": 512,
            "max_image_bytes": 1024 * 1024,
        },
        "safety": {
            "loopback_only": True,
            "read_only_sources": True,
            "allow_file_actions": False,
            "auto_accept": False,
        },
    }
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return BenchmarkProfile.load(path)


def valid_payload(modality: str = "SEM") -> dict:
    return {
        "modality": modality,
        "workstream": "UNASSIGNED",
        "sample_id": None,
        "material": None,
        "test_method": modality,
        "conditions": {},
        "proposed_name": None,
        "confidence": 0.8,
        "evidence": [{"kind": "visual_pattern", "value": "microscopy-like grayscale image"}],
        "needs_review": True,
        "abstain_reason": None,
    }


def test_strict_response_contract_rejects_extra_fields_and_unsupported_unknown() -> None:
    parsed = parse_classification_response("```json\n" + json.dumps(valid_payload()) + "\n```")
    assert parsed.modality.value == "SEM"

    with pytest.raises(ValueError):
        AIClassification.model_validate({**valid_payload(), "unexpected": "field"})
    unknown = valid_payload("UNKNOWN")
    unknown["evidence"] = [{"kind": "abstention", "value": "bounded evidence is insufficient"}]
    with pytest.raises(ValueError, match="abstain_reason"):
        AIClassification.model_validate(unknown)

    schema = output_json_schema()
    assert "evidence" in schema["required"]
    assert schema["properties"]["evidence"]["minItems"] == 1
    assert schema["properties"]["evidence"]["maxItems"] == 3
    unknown_then = schema["allOf"][0]["then"]
    assert unknown_then["properties"]["needs_review"]["const"] is True
    assert unknown_then["properties"]["abstain_reason"]["minLength"] == 1


def test_profile_requires_loopback_and_read_only_sources(tmp_path: Path) -> None:
    configured = profile(tmp_path)
    assert configured.base_url == "http://127.0.0.1:8877"
    payload = json.loads((tmp_path / "profile.json").read_text(encoding="utf-8"))
    payload["server"]["host"] = "0.0.0.0"
    (tmp_path / "profile.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="loopback"):
        BenchmarkProfile.load(tmp_path / "profile.json")


def test_real_data_reports_are_forbidden_inside_repository(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    with pytest.raises(ValueError, match="outside"):
        assert_report_path_is_outside_repository(repo / "report.json", repo)
    allowed = assert_report_path_is_outside_repository(tmp_path / "reports" / "report.json", repo)
    assert allowed == (tmp_path / "reports" / "report.json").resolve()


def test_repository_model_lock_is_strict_and_matches_profile() -> None:
    root = Path(__file__).resolve().parents[2]
    lock = load_model_lock(root / "profiles" / "windows-model-lock.json")
    configured = BenchmarkProfile.load(root / "profiles" / "windows-rtx5080.json")
    assert lock.profile_id == configured.profile_id
    assert lock.model.bytes == 8_709_519_456
    assert lock.vision_projector.bytes == 752_289_728
    assert lock.runtime.repository == "https://github.com/ggml-org/llama.cpp"
    assert all(item.url.startswith("https://github.com/ggml-org/llama.cpp/") for item in lock.runtime.artifacts)
    assert lock.runtime.launch_arguments[lock.runtime.launch_arguments.index("--host") + 1] == "127.0.0.1"
    assert lock.runtime.launch_arguments[lock.runtime.launch_arguments.index("--cors-origins") + 1] == "localhost"
    assert "--no-cors-credentials" in lock.runtime.launch_arguments


class _FakeClient:
    def __init__(self) -> None:
        self.calls = 0

    def classify(self, _pack: EvidencePack) -> InferenceResult:
        self.calls += 1
        value = AIClassification.model_validate(valid_payload())
        return InferenceResult(value, 20 + self.calls, json.dumps(valid_payload()), None)


def test_benchmark_reports_metrics_and_repeat_probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class _NoGpuSampler:
        peak_mib = 512

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    monkeypatch.setattr("backend.ai.benchmark.GpuMemorySampler", _NoGpuSampler)
    dataset = DiagnosticDataset("dataset-1", "SEM", "REVIEW", 0.7, "unknown")
    pack = EvidencePack(dataset, '{"assets":[]}', (), 1)
    report = run_benchmark([pack], _FakeClient(), profile(tmp_path), repeat_probe=3)

    assert report["metrics"]["case_count"] == 1
    assert report["metrics"]["valid_rate"] == 1
    assert report["metrics"]["known_accuracy"] == 1
    assert report["metrics"]["peak_gpu_memory_mib"] == 512
    assert report["metrics"]["stability_probe_consistent"] is True
    assert len(report["results"]) == 4
