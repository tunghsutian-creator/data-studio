from __future__ import annotations

import base64
import io
import json
import math
import sqlite3
import statistics
import subprocess
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, Sequence

import httpx
from PIL import Image, ImageOps

from ..classifier import _decode_text
from ..paths import RootMapper
from ..taxonomy import Modality
from .contracts import (
    AI_OUTPUT_SCHEMA_VERSION,
    PROMPT_VERSION,
    AIClassification,
)
from .provider import (
    AnalysisRequest,
    LlamaCppProvider,
    LocalModelProfile,
    ProviderError,
    ProviderIdentity,
    ProviderUnavailable,
    TAXONOMY_VERSION,
)


TEXT_EXTENSIONS = {".csv", ".txt", ".tsv", ".out", ".err", ".lsp", ".fpo", ".dat"}
IMAGE_EXTENSIONS = {".tif", ".tiff", ".png", ".jpg", ".jpeg", ".bmp", ".webp"}
BenchmarkProfile = LocalModelProfile


@dataclass(frozen=True, slots=True)
class DiagnosticDataset:
    dataset_id: str
    expected_modality: str
    expected_status: str
    rule_confidence: float
    classification_method: str


@dataclass(frozen=True, slots=True)
class EvidencePack:
    dataset: DiagnosticDataset
    structured_text: str
    image_data_urls: tuple[str, ...]
    asset_count: int


@dataclass(frozen=True, slots=True)
class InferenceResult:
    classification: AIClassification | None
    latency_ms: int
    raw_content: str
    error: str | None


class ClassificationClient(Protocol):
    def classify(self, pack: EvidencePack) -> InferenceResult: ...


def _readonly_connection(path: str | Path) -> sqlite3.Connection:
    resolved = Path(path).expanduser().resolve(strict=True)
    connection = sqlite3.connect(
        f"file:{resolved.as_posix()}?mode=ro&immutable=1",
        uri=True,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def select_diagnostic_datasets(catalog_path: str | Path, limit: int = 30) -> list[DiagnosticDataset]:
    if limit < 1:
        raise ValueError("diagnostic limit must be positive")
    with _readonly_connection(catalog_path) as connection:
        rows = [
            dict(row)
            for row in connection.execute(
                """
                SELECT id,modality,status,confidence,classification_method
                FROM datasets ORDER BY confidence ASC,id ASC
                """
            ).fetchall()
        ]
    if not rows:
        return []

    selected: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(row: dict[str, Any]) -> None:
        key = str(row["id"])
        if key not in seen and len(selected) < limit:
            selected.append(row)
            seen.add(key)

    for row in rows:
        if row["modality"] == Modality.UNKNOWN.value or row["status"] == "REVIEW":
            add(row)
    low_confidence_target = min(limit, max(14, len(selected)))
    low_groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if str(row["id"]) not in seen and float(row["confidence"]) < 0.9:
            low_groups.setdefault(str(row["modality"]), []).append(row)
    low_depth = 0
    while len(selected) < low_confidence_target and low_groups:
        added = False
        for modality in sorted(low_groups):
            candidates = low_groups[modality]
            if low_depth < len(candidates):
                add(candidates[low_depth])
                added = True
                if len(selected) >= low_confidence_target:
                    break
        if not added:
            break
        low_depth += 1

    minimum_coverage = {
        Modality.SEM.value: 6,
        Modality.FTIR.value: 3,
        Modality.TENSILE.value: 3,
        Modality.RHEOLOGY.value: 3,
        Modality.TORQUE.value: 2,
        Modality.IMPACT.value: 2,
        Modality.REFERENCE.value: 2,
        Modality.OPTICAL.value: 2,
        Modality.GPC.value: 1,
        Modality.SIMULATION.value: 1,
        Modality.UNKNOWN.value: 2,
    }
    coverage_groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if str(row["id"]) not in seen:
            coverage_groups.setdefault(str(row["modality"]), []).append(row)
    for candidates in coverage_groups.values():
        candidates.sort(key=lambda item: (-float(item["confidence"]), str(item["id"])))
    counts: dict[str, int] = {}
    for item in selected:
        modality = str(item["modality"])
        counts[modality] = counts.get(modality, 0) + 1
    for modality, target in minimum_coverage.items():
        for row in coverage_groups.get(modality, []):
            if len(selected) >= limit or counts.get(modality, 0) >= target:
                break
            add(row)
            counts[modality] = counts.get(modality, 0) + 1

    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if str(row["id"]) not in seen:
            groups.setdefault(str(row["modality"]), []).append(row)
    for candidates in groups.values():
        candidates.sort(key=lambda item: (-float(item["confidence"]), str(item["id"])))
    modality_order = sorted(groups)
    depth = 0
    while len(selected) < limit and modality_order:
        added = False
        for modality in modality_order:
            candidates = groups[modality]
            if depth < len(candidates):
                add(candidates[depth])
                added = True
                if len(selected) >= limit:
                    break
        if not added:
            break
        depth += 1

    return [
        DiagnosticDataset(
            dataset_id=str(row["id"]),
            expected_modality=str(row["modality"]),
            expected_status=str(row["status"]),
            rule_confidence=float(row["confidence"]),
            classification_method=str(row["classification_method"]),
        )
        for row in selected
    ]


def _text_preview(path: Path, limit: int) -> str:
    with path.open("rb") as stream:
        decoded = _decode_text(stream.read(limit))
    compact = "\n".join(line.strip() for line in decoded.splitlines() if line.strip())
    return compact[:8000]


def _image_data_url(path: Path, max_edge: int, max_bytes: int) -> str:
    with Image.open(path) as source:
        source.seek(0)
        image = ImageOps.exif_transpose(source).convert("RGB")
        image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
        while True:
            output = io.BytesIO()
            image.save(output, format="PNG", optimize=True)
            payload = output.getvalue()
            if len(payload) <= max_bytes or min(image.size) <= 256:
                break
            image = image.resize(
                (max(1, int(image.width * 0.8)), max(1, int(image.height * 0.8))),
                Image.Resampling.LANCZOS,
            )
    if len(payload) > max_bytes:
        raise ValueError(f"bounded image preview still exceeds {max_bytes} bytes")
    return "data:image/png;base64," + base64.b64encode(payload).decode("ascii")


def build_evidence_pack(
    catalog_path: str | Path,
    dataset: DiagnosticDataset,
    mapper: RootMapper,
    profile: BenchmarkProfile,
) -> EvidencePack:
    with _readonly_connection(catalog_path) as connection:
        rows = [
            dict(row)
            for row in connection.execute(
                """
                SELECT a.*,d.source_kind
                FROM assets a JOIN datasets d ON d.id=a.dataset_id
                WHERE a.dataset_id=? ORDER BY a.extension,a.id
                """,
                (dataset.dataset_id,),
            ).fetchall()
        ]
    if not rows:
        raise ValueError(f"dataset has no assets: {dataset.dataset_id}")

    descriptions: list[dict[str, Any]] = []
    images: list[str] = []
    text_count = 0
    for index, row in enumerate(rows, start=1):
        if row.get("path_state") not in {None, "VALID"}:
            descriptions.append(
                {"asset": index, "extension": row["extension"], "size_bytes": row["size_bytes"], "state": "PATH_REVIEW"}
            )
            continue
        if row.get("managed_root_key") and row.get("managed_relpath") and row.get("managed_sha256"):
            path = mapper.resolve(row["managed_root_key"], row["managed_relpath"], must_exist=True)
        elif row.get("managed_path") and row.get("managed_sha256"):
            location = mapper.relativize(
                row["managed_path"], allowed_keys={"vault"}, must_exist=True
            )
            path = mapper.resolve(location.root_key, location.relative_path, must_exist=True)
        elif row.get("original_root_key") and row.get("original_relpath"):
            path = mapper.resolve(row["original_root_key"], row["original_relpath"], must_exist=True)
        else:
            allowed_key = str(row.get("source_kind") or "reference").lower()
            location = mapper.relativize(
                row["original_path"], allowed_keys={allowed_key}, must_exist=True
            )
            path = mapper.resolve(location.root_key, location.relative_path, must_exist=True)
        if not path.is_file() or path.stat().st_size != int(row["size_bytes"]):
            raise ValueError(f"asset changed before benchmark: {row['id']}")

        extension = str(row["extension"] or "").lower()
        description: dict[str, Any] = {
            "asset": index,
            "extension": extension,
            "size_bytes": int(row["size_bytes"]),
            "mime_type": row.get("mime_type"),
        }
        if extension in TEXT_EXTENSIONS and text_count < profile.max_text_assets:
            preview = _text_preview(path, profile.max_text_bytes)
            if preview:
                description["content_preview"] = preview
                text_count += 1
        if extension in IMAGE_EXTENSIONS and len(images) < profile.max_images:
            try:
                images.append(_image_data_url(path, profile.max_image_edge, profile.max_image_bytes))
                description["bounded_image_attached"] = True
            except (OSError, ValueError) as exc:
                description["image_preview_error"] = str(exc)
        descriptions.append(description)

    evidence = {
        "contract": "No filenames or parent directories are included.",
        "assets": descriptions,
    }
    return EvidencePack(
        dataset=dataset,
        structured_text=json.dumps(evidence, ensure_ascii=False, separators=(",", ":")),
        image_data_urls=tuple(images),
        asset_count=len(rows),
    )


class LlamaCppClient:
    def __init__(
        self,
        profile: BenchmarkProfile,
        *,
        timeout_seconds: float = 120,
        transport: httpx.BaseTransport | None = None,
        identity: ProviderIdentity | None = None,
    ):
        self.profile = profile
        self.provider = LlamaCppProvider(
            profile,
            timeout_seconds=timeout_seconds,
            transport=transport,
            identity=identity,
        )

    def close(self) -> None:
        self.provider.close()

    def health(self) -> dict[str, Any]:
        health = self.provider.health()
        if not health.available:
            raise ProviderUnavailable(health.detail or "llama.cpp is unavailable")
        return health.to_dict()

    def classify(self, pack: EvidencePack) -> InferenceResult:
        try:
            request = AnalysisRequest.from_evidence(
                pack.structured_text,
                pack.image_data_urls,
            )
            result = self.provider.analyze(request)
            return InferenceResult(result.classification, result.latency_ms, "", None)
        except ProviderError as exc:
            return InferenceResult(None, exc.latency_ms, "", f"{exc.code}: {exc}")


class GpuMemorySampler:
    def __init__(self, interval_seconds: float = 0.25):
        self.interval_seconds = interval_seconds
        self.peak_mib = 0
        self.samples: list[int] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _sample(self) -> None:
        while not self._stop.is_set():
            try:
                output = subprocess.check_output(
                    [
                        "nvidia-smi",
                        "--query-gpu=memory.used",
                        "--format=csv,noheader,nounits",
                    ],
                    text=True,
                    timeout=5,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                values = [int(line.strip()) for line in output.splitlines() if line.strip()]
                if values:
                    current = max(values)
                    self.samples.append(current)
                    self.peak_mib = max(self.peak_mib, current)
            except (OSError, ValueError, subprocess.SubprocessError):
                pass
            self._stop.wait(self.interval_seconds)

    def __enter__(self) -> "GpuMemorySampler":
        self._thread = threading.Thread(target=self._sample, name="benchmark-gpu-sampler", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_args: Any) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)


def _percentile(values: Sequence[int], percentile: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    rank = max(0, min(len(ordered) - 1, math.ceil(percentile * len(ordered)) - 1))
    return int(ordered[rank])


def run_benchmark(
    cases: Sequence[EvidencePack],
    client: ClassificationClient,
    profile: BenchmarkProfile,
    *,
    repeat_probe: int = 3,
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    probe_modalities: list[str | None] = []
    workload: list[tuple[EvidencePack, bool]] = [(case, False) for case in cases]
    if cases and repeat_probe > 0:
        workload.extend((cases[0], True) for _ in range(repeat_probe))

    with GpuMemorySampler() as gpu:
        for pack, is_probe in workload:
            inference = client.classify(pack)
            predicted = inference.classification.modality.value if inference.classification else None
            if is_probe:
                probe_modalities.append(predicted)
            results.append(
                {
                    "dataset_id": pack.dataset.dataset_id,
                    "expected_modality": pack.dataset.expected_modality,
                    "expected_status": pack.dataset.expected_status,
                    "rule_confidence": pack.dataset.rule_confidence,
                    "classification_method": pack.dataset.classification_method,
                    "asset_count": pack.asset_count,
                    "image_count": len(pack.image_data_urls),
                    "is_stability_probe": is_probe,
                    "valid": inference.classification is not None,
                    "predicted_modality": predicted,
                    "match": predicted == pack.dataset.expected_modality,
                    "latency_ms": inference.latency_ms,
                    "error": inference.error,
                    "error_output_excerpt": inference.raw_content[:2000] if inference.error else None,
                    "output": inference.classification.model_dump(mode="json") if inference.classification else None,
                }
            )

    primary = [item for item in results if not item["is_stability_probe"]]
    valid = [item for item in primary if item["valid"]]
    known = [item for item in valid if item["expected_modality"] != Modality.UNKNOWN.value]
    latencies = [int(item["latency_ms"]) for item in primary]
    return {
        "report_schema_version": 2,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "profile_id": profile.profile_id,
        "contracts": {
            "prompt_version": PROMPT_VERSION,
            "taxonomy_version": TAXONOMY_VERSION,
            "output_schema_version": AI_OUTPUT_SCHEMA_VERSION,
        },
        "diagnostic_labels_are_rule_derived": True,
        "metrics": {
            "case_count": len(primary),
            "valid_count": len(valid),
            "valid_rate": len(valid) / len(primary) if primary else 0,
            "known_accuracy": sum(1 for item in known if item["match"]) / len(known) if known else 0,
            "latency_p50_ms": int(statistics.median(latencies)) if latencies else 0,
            "latency_p95_ms": _percentile(latencies, 0.95),
            "peak_gpu_memory_mib": gpu.peak_mib,
            "stability_probe_modalities": probe_modalities,
            "stability_probe_consistent": bool(probe_modalities)
            and None not in probe_modalities
            and len(set(probe_modalities)) == 1,
        },
        "results": results,
    }


def assert_report_path_is_outside_repository(path: str | Path, repository_root: str | Path) -> Path:
    destination = Path(path).expanduser().resolve(strict=False)
    root = Path(repository_root).expanduser().resolve(strict=True)
    try:
        destination.relative_to(root)
    except ValueError:
        return destination
    raise ValueError("benchmark reports derived from real data must be written outside the Git repository")


__all__ = [
    "BenchmarkProfile",
    "DiagnosticDataset",
    "EvidencePack",
    "GpuMemorySampler",
    "InferenceResult",
    "LlamaCppClient",
    "assert_report_path_is_outside_repository",
    "build_evidence_pack",
    "run_benchmark",
    "select_diagnostic_datasets",
]
