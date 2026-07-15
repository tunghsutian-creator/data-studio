from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from backend.ai.contracts import AIClassification
from backend.ai.provider import (
    AnalysisRequest,
    FakeLocalModelProvider,
    LlamaCppProvider,
    LocalModelProfile,
    LocalModelProvider,
    ProviderInvalidResponse,
    ProviderRequestError,
    ProviderTimeout,
    ProviderUnavailable,
)


def classification(modality: str = "SEM") -> AIClassification:
    payload = {
        "modality": modality,
        "workstream": "UNASSIGNED",
        "sample_id": None,
        "material": None,
        "test_method": modality,
        "conditions": {},
        "proposed_name": None,
        "confidence": 0.8,
        "evidence": [{"kind": "visual_pattern", "value": "bounded grayscale micrograph"}],
        "needs_review": True,
        "abstain_reason": None,
    }
    return AIClassification.model_validate(payload)


def profile() -> LocalModelProfile:
    root = Path(__file__).resolve().parents[2]
    return LocalModelProfile.load(root / "profiles" / "windows-rtx5080.json")


def test_analysis_request_has_deterministic_content_fingerprint_and_no_path_api() -> None:
    first = AnalysisRequest.from_evidence('{"assets":[]}', ("data:image/png;base64,AA==",))
    second = AnalysisRequest.from_evidence('{"assets":[]}', ("data:image/png;base64,AA==",))
    changed = AnalysisRequest.from_evidence('{"assets":[{}]}', ("data:image/png;base64,AA==",))

    assert first.input_fingerprint == second.input_fingerprint
    assert first.input_fingerprint != changed.input_fingerprint
    assert not hasattr(first, "path")
    with pytest.raises(ValueError, match="in-memory"):
        AnalysisRequest(first.input_fingerprint, first.structured_evidence, ("file:///raw/data.tif",))


def test_fake_provider_is_deterministic_scriptable_and_protocol_compatible() -> None:
    expected = classification()
    timeout = ProviderTimeout("synthetic timeout", latency_ms=25)
    provider = FakeLocalModelProvider(expected, outcomes=(timeout, expected))
    request = AnalysisRequest.from_evidence('{"assets":[]}')

    assert isinstance(provider, LocalModelProvider)
    assert provider.health().available is True
    with pytest.raises(ProviderTimeout) as raised:
        provider.analyze(request)
    assert raised.value.retryable is True
    result = provider.analyze(request)
    assert result.classification == expected
    assert result.identity.provider == "fake"
    assert provider.requests == (request, request)

    offline = FakeLocalModelProvider(expected, available=False)
    assert offline.health().available is False
    with pytest.raises(ProviderUnavailable):
        offline.analyze(request)


def test_llama_cpp_provider_uses_loopback_strict_schema_and_validates_output() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"}, request=request)
        captured.update(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": classification().model_dump_json()}}]},
            request=request,
        )

    provider = LlamaCppProvider(profile(), transport=httpx.MockTransport(handler))
    try:
        health = provider.health()
        result = provider.analyze(AnalysisRequest.from_evidence('{"assets":[]}'))
    finally:
        provider.close()

    assert health.available is True
    assert health.identity.model_id == "Qwen3-VL-8B-Instruct"
    assert result.classification.modality.value == "SEM"
    assert len(result.response_sha256) == 64
    assert captured["stream"] is False
    assert captured["cache_prompt"] is False
    assert "input_fingerprint" not in captured
    schema = captured["response_format"]["json_schema"]["schema"]
    assert schema["properties"]["evidence"]["maxItems"] == 3


def test_llama_cpp_provider_maps_timeout_http_and_invalid_output_without_raw_echo() -> None:
    request = AnalysisRequest.from_evidence('{"assets":[]}')

    def timeout_handler(http_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("secret raw evidence", request=http_request)

    timeout_provider = LlamaCppProvider(profile(), transport=httpx.MockTransport(timeout_handler))
    try:
        with pytest.raises(ProviderTimeout) as timeout:
            timeout_provider.analyze(request)
    finally:
        timeout_provider.close()
    assert timeout.value.retryable is True
    assert "secret" not in str(timeout.value)

    def rejected_handler(http_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="secret raw evidence", request=http_request)

    rejected_provider = LlamaCppProvider(profile(), transport=httpx.MockTransport(rejected_handler))
    try:
        with pytest.raises(ProviderRequestError) as rejected:
            rejected_provider.analyze(request)
    finally:
        rejected_provider.close()
    assert rejected.value.status_code == 400
    assert rejected.value.retryable is False
    assert "secret" not in str(rejected.value)

    def invalid_handler(http_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "secret invalid output"}}]},
            request=http_request,
        )

    invalid_provider = LlamaCppProvider(profile(), transport=httpx.MockTransport(invalid_handler))
    try:
        with pytest.raises(ProviderInvalidResponse) as invalid:
            invalid_provider.analyze(request)
    finally:
        invalid_provider.close()
    assert invalid.value.retryable is False
    assert "secret" not in str(invalid.value)


def test_llama_cpp_provider_rejects_excess_images_before_network() -> None:
    configured = profile()
    images = tuple("data:image/png;base64,AA==" for _ in range(configured.max_images + 1))
    provider = LlamaCppProvider(
        configured,
        transport=httpx.MockTransport(lambda request: pytest.fail(f"unexpected request: {request.url}")),
    )
    try:
        with pytest.raises(ProviderRequestError, match="profile allows"):
            provider.analyze(AnalysisRequest.from_evidence('{"assets":[]}', images))
    finally:
        provider.close()


def test_llama_cpp_provider_rejects_unbounded_structured_evidence_before_network() -> None:
    provider = LlamaCppProvider(
        profile(),
        transport=httpx.MockTransport(lambda request: pytest.fail(f"unexpected request: {request.url}")),
    )
    try:
        with pytest.raises(ProviderRequestError, match="context safety bound"):
            provider.analyze(AnalysisRequest.from_evidence("x" * 20_000))
    finally:
        provider.close()
