"""Production-facing local model provider boundary.

Providers receive bounded, in-memory evidence only. They never receive source
paths and expose no file mutation operations.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, Sequence, runtime_checkable

import httpx

from ..taxonomy import Modality
from .contracts import (
    AI_OUTPUT_SCHEMA_VERSION,
    PROMPT_VERSION,
    AIClassification,
    output_json_schema,
    parse_classification_response,
)


TAXONOMY_VERSION = "builtin-v1"
_FINGERPRINT = re.compile(r"^[0-9a-f]{64}$")
_IMAGE_DATA_URL = re.compile(r"^data:image/(?:png|jpeg|webp);base64,[A-Za-z0-9+/=]+$")


@dataclass(frozen=True, slots=True)
class ProviderIdentity:
    provider: str
    profile_id: str
    model_id: str
    quantization: str
    device: str
    prompt_version: str = PROMPT_VERSION
    taxonomy_version: str = TAXONOMY_VERSION
    output_schema_version: str = AI_OUTPUT_SCHEMA_VERSION
    model_revision: str | None = None
    runtime_release: str | None = None
    runtime_commit: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "provider": self.provider,
            "profile_id": self.profile_id,
            "model_id": self.model_id,
            "quantization": self.quantization,
            "device": self.device,
            "prompt_version": self.prompt_version,
            "taxonomy_version": self.taxonomy_version,
            "output_schema_version": self.output_schema_version,
            "model_revision": self.model_revision,
            "runtime_release": self.runtime_release,
            "runtime_commit": self.runtime_commit,
        }


@dataclass(frozen=True, slots=True)
class LocalModelProfile:
    profile_id: str
    host: str
    port: int
    max_output_tokens: int
    temperature: float
    seed: int
    max_text_bytes: int
    max_text_assets: int
    max_images: int
    max_image_edge: int
    max_image_bytes: int
    provider: str = "llama.cpp"
    model_id: str = "unknown"
    quantization: str = "unknown"
    device: str = "unknown"
    context_tokens: int = 8192
    parallel_requests: int = 1
    flash_attention: bool = False

    def __post_init__(self) -> None:
        if self.host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("local model provider must use a loopback host")
        if not 1 <= self.port <= 65535:
            raise ValueError("local model provider port must be between 1 and 65535")
        positive = {
            "context_tokens": self.context_tokens,
            "max_output_tokens": self.max_output_tokens,
            "parallel_requests": self.parallel_requests,
            "max_text_bytes": self.max_text_bytes,
            "max_text_assets": self.max_text_assets,
            "max_image_edge": self.max_image_edge,
            "max_image_bytes": self.max_image_bytes,
        }
        invalid = [name for name, value in positive.items() if value < 1]
        if invalid:
            raise ValueError(f"local model profile values must be positive: {', '.join(invalid)}")
        if self.max_images < 0:
            raise ValueError("max_images may not be negative")
        if self.temperature < 0:
            raise ValueError("temperature may not be negative")

    @classmethod
    def load(cls, path: str | Path) -> "LocalModelProfile":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        server = payload["server"]
        evidence = payload["evidence"]
        safety = payload.get("safety", {})
        if safety.get("loopback_only") is not True:
            raise ValueError("local model profile must declare loopback_only")
        if safety.get("read_only_sources") is not True:
            raise ValueError("local model profile must declare read_only_sources")
        if safety.get("allow_file_actions") is not False:
            raise ValueError("local model profile may not allow model file actions")
        if safety.get("auto_accept") is not False:
            raise ValueError("local model profile may not allow automatic acceptance")
        return cls(
            profile_id=str(payload["profile_id"]),
            provider=str(payload["provider"]),
            model_id=str(payload["model_id"]),
            quantization=str(payload["quantization"]),
            device=str(payload["device"]),
            host=str(server["host"]),
            port=int(server["port"]),
            context_tokens=int(server["context_tokens"]),
            max_output_tokens=int(server["max_output_tokens"]),
            temperature=float(server["temperature"]),
            seed=int(server["seed"]),
            parallel_requests=int(server["parallel_requests"]),
            flash_attention=bool(server["flash_attention"]),
            max_text_bytes=int(evidence["max_text_bytes_per_asset"]),
            max_text_assets=int(evidence["max_text_assets"]),
            max_images=int(evidence["max_images"]),
            max_image_edge=int(evidence["max_image_edge"]),
            max_image_bytes=int(evidence["max_image_bytes"]),
        )

    @property
    def base_url(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"http://{host}:{self.port}"

    @property
    def identity(self) -> ProviderIdentity:
        return ProviderIdentity(
            provider=self.provider,
            profile_id=self.profile_id,
            model_id=self.model_id,
            quantization=self.quantization,
            device=self.device,
        )


@dataclass(frozen=True, slots=True)
class AnalysisRequest:
    input_fingerprint: str
    structured_evidence: str
    image_data_urls: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not _FINGERPRINT.fullmatch(self.input_fingerprint):
            raise ValueError("input_fingerprint must be a lowercase SHA-256 digest")
        if not self.structured_evidence.strip():
            raise ValueError("structured_evidence may not be empty")
        for image in self.image_data_urls:
            if not _IMAGE_DATA_URL.fullmatch(image):
                raise ValueError("images must be in-memory PNG, JPEG, or WebP data URLs")

    @classmethod
    def from_evidence(
        cls,
        structured_evidence: str,
        image_data_urls: Sequence[str] = (),
    ) -> "AnalysisRequest":
        images = tuple(image_data_urls)
        digest = hashlib.sha256()
        digest.update(b"academic-vault-ai-input-v1\x00")
        digest.update(structured_evidence.encode("utf-8"))
        for image in images:
            digest.update(b"\x00image\x00")
            digest.update(hashlib.sha256(image.encode("ascii")).digest())
        return cls(digest.hexdigest(), structured_evidence, images)


@dataclass(frozen=True, slots=True)
class ProviderHealth:
    available: bool
    status: str
    identity: ProviderIdentity
    checked_at_utc: str
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "status": self.status,
            "checked_at_utc": self.checked_at_utc,
            "detail": self.detail,
            "identity": self.identity.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class ProviderAnalysisResult:
    classification: AIClassification
    identity: ProviderIdentity
    latency_ms: int
    response_sha256: str


class ProviderError(RuntimeError):
    code = "provider_error"
    retryable = False

    def __init__(
        self,
        message: str,
        *,
        latency_ms: int = 0,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.latency_ms = max(0, int(latency_ms))
        self.status_code = status_code


class ProviderUnavailable(ProviderError):
    code = "provider_unavailable"
    retryable = True


class ProviderTimeout(ProviderError):
    code = "provider_timeout"
    retryable = True


class ProviderRequestError(ProviderError):
    code = "provider_request_invalid"


class ProviderInvalidResponse(ProviderError):
    code = "provider_response_invalid"


@runtime_checkable
class LocalModelProvider(Protocol):
    @property
    def identity(self) -> ProviderIdentity: ...

    def health(self) -> ProviderHealth: ...

    def analyze(self, request: AnalysisRequest) -> ProviderAnalysisResult: ...

    def close(self) -> None: ...


def _checked_at() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _system_prompt() -> str:
    modalities = ", ".join(item.value for item in Modality)
    return (
        "You classify local scientific research datasets. Deterministic parsers and rules have already run. "
        "Use only the supplied bounded evidence; never infer from missing filenames or directory names. "
        f"modality must be one of: {modalities}. When evidence is insufficient, use UNKNOWN, set needs_review "
        "to true, and provide a non-empty abstain_reason. "
        "Use UNASSIGNED for an unknown workstream. Evidence must contain one to three concise concrete items; "
        "for UNKNOWN use evidence kind abstention. Return only the requested JSON object."
    )


class LlamaCppProvider:
    def __init__(
        self,
        profile: LocalModelProfile,
        *,
        timeout_seconds: float = 120,
        transport: httpx.BaseTransport | None = None,
        identity: ProviderIdentity | None = None,
    ) -> None:
        if profile.provider != "llama.cpp":
            raise ValueError("LlamaCppProvider requires a llama.cpp profile")
        if timeout_seconds <= 0:
            raise ValueError("provider timeout must be positive")
        self.profile = profile
        self._identity = identity or profile.identity
        self._client = httpx.Client(
            base_url=profile.base_url,
            timeout=timeout_seconds,
            transport=transport,
        )

    @property
    def identity(self) -> ProviderIdentity:
        return self._identity

    def close(self) -> None:
        self._client.close()

    def health(self) -> ProviderHealth:
        try:
            response = self._client.get("/health")
            payload = response.json()
            status = str(payload.get("status", "unknown")) if isinstance(payload, dict) else "invalid"
            available = response.status_code == 200 and status == "ok"
            detail = None if available else f"llama.cpp health returned HTTP {response.status_code} ({status})"
            return ProviderHealth(available, status, self.identity, _checked_at(), detail)
        except httpx.TimeoutException:
            return ProviderHealth(False, "timeout", self.identity, _checked_at(), "llama.cpp health timed out")
        except (httpx.RequestError, ValueError, json.JSONDecodeError):
            return ProviderHealth(False, "unavailable", self.identity, _checked_at(), "llama.cpp health unavailable")

    def _validate_request(self, request: AnalysisRequest) -> None:
        maximum_structured_bytes = max(
            4096,
            min(
                self.profile.context_tokens * 2,
                self.profile.max_text_bytes * self.profile.max_text_assets + 8192,
            ),
        )
        if len(request.structured_evidence.encode("utf-8")) > maximum_structured_bytes:
            raise ProviderRequestError("structured evidence exceeds the configured context safety bound")
        if len(request.image_data_urls) > self.profile.max_images:
            raise ProviderRequestError(
                f"request contains {len(request.image_data_urls)} images; profile allows {self.profile.max_images}"
            )
        maximum_encoded = ((self.profile.max_image_bytes + 2) // 3) * 4
        for image in request.image_data_urls:
            encoded = image.partition(",")[2]
            if len(encoded) > maximum_encoded:
                raise ProviderRequestError("request image exceeds the configured in-memory evidence bound")

    def analyze(self, request: AnalysisRequest) -> ProviderAnalysisResult:
        self._validate_request(request)
        content: list[dict[str, Any]] = [
            {"type": "text", "text": "Classify this evidence pack:\n" + request.structured_evidence}
        ]
        content.extend(
            {"type": "image_url", "image_url": {"url": data_url}}
            for data_url in request.image_data_urls
        )
        payload = {
            "messages": [
                {"role": "system", "content": _system_prompt()},
                {"role": "user", "content": content},
            ],
            "temperature": self.profile.temperature,
            "seed": self.profile.seed,
            "max_tokens": self.profile.max_output_tokens,
            "stream": False,
            "cache_prompt": False,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "academic_vault_classification",
                    "strict": True,
                    "schema": output_json_schema(),
                },
            },
        }
        started = time.perf_counter()
        try:
            response = self._client.post("/v1/chat/completions", json=payload)
            response.raise_for_status()
        except httpx.TimeoutException as exc:
            latency = int((time.perf_counter() - started) * 1000)
            raise ProviderTimeout("llama.cpp analysis timed out", latency_ms=latency) from exc
        except httpx.HTTPStatusError as exc:
            latency = int((time.perf_counter() - started) * 1000)
            status = exc.response.status_code
            if status in {408, 429} or status >= 500:
                raise ProviderUnavailable(
                    f"llama.cpp returned HTTP {status}",
                    latency_ms=latency,
                    status_code=status,
                ) from exc
            raise ProviderRequestError(
                f"llama.cpp rejected the request with HTTP {status}",
                latency_ms=latency,
                status_code=status,
            ) from exc
        except httpx.RequestError as exc:
            latency = int((time.perf_counter() - started) * 1000)
            raise ProviderUnavailable("llama.cpp is unavailable", latency_ms=latency) from exc

        latency = int((time.perf_counter() - started) * 1000)
        try:
            response_payload = response.json()
            raw = response_payload["choices"][0]["message"]["content"]
            if isinstance(raw, list):
                raw = "".join(
                    str(item.get("text", "")) if isinstance(item, dict) else str(item)
                    for item in raw
                )
            raw_text = str(raw)
            classification = parse_classification_response(raw_text)
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ProviderInvalidResponse(
                "llama.cpp returned output that did not satisfy the classification contract",
                latency_ms=latency,
            ) from exc
        return ProviderAnalysisResult(
            classification=classification,
            identity=self.identity,
            latency_ms=latency,
            response_sha256=hashlib.sha256(raw_text.encode("utf-8")).hexdigest(),
        )


class FakeLocalModelProvider:
    def __init__(
        self,
        default: AIClassification,
        *,
        outcomes: Sequence[AIClassification | ProviderError] = (),
        available: bool = True,
        identity: ProviderIdentity | None = None,
    ) -> None:
        self._default = default
        self._outcomes = deque(outcomes)
        self._available = available
        self._identity = identity or ProviderIdentity(
            provider="fake",
            profile_id="fake-local-model",
            model_id="fake-classifier",
            quantization="none",
            device="CPU",
        )
        self._requests: list[AnalysisRequest] = []

    @property
    def identity(self) -> ProviderIdentity:
        return self._identity

    @property
    def requests(self) -> tuple[AnalysisRequest, ...]:
        return tuple(self._requests)

    def close(self) -> None:
        return None

    def health(self) -> ProviderHealth:
        return ProviderHealth(
            self._available,
            "ok" if self._available else "unavailable",
            self.identity,
            _checked_at(),
            None if self._available else "fake provider is unavailable",
        )

    def analyze(self, request: AnalysisRequest) -> ProviderAnalysisResult:
        if not self._available:
            raise ProviderUnavailable("fake provider is unavailable")
        self._requests.append(request)
        outcome = self._outcomes.popleft() if self._outcomes else self._default
        if isinstance(outcome, ProviderError):
            raise outcome
        raw = outcome.model_dump_json()
        return ProviderAnalysisResult(
            classification=outcome,
            identity=self.identity,
            latency_ms=0,
            response_sha256=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        )


__all__ = [
    "AnalysisRequest",
    "FakeLocalModelProvider",
    "LlamaCppProvider",
    "LocalModelProfile",
    "LocalModelProvider",
    "ProviderAnalysisResult",
    "ProviderError",
    "ProviderHealth",
    "ProviderIdentity",
    "ProviderInvalidResponse",
    "ProviderRequestError",
    "ProviderTimeout",
    "ProviderUnavailable",
    "TAXONOMY_VERSION",
]
