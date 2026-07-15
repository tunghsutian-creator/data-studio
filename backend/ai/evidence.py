"""Build bounded, read-only evidence for local AI classification.

The model never receives an absolute path, relative path, filename, or a file
mutation capability. Every catalog asset is resolved through a configured root
and hash-verified before any bounded preview is constructed.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import warnings
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from PIL import Image, ImageOps

from ..classifier import _decode_text
from ..database import Database
from ..paths import PathLocationError, RootMapper
from .provider import AnalysisRequest, LocalModelProfile


TEXT_EXTENSIONS = frozenset(
    {".csv", ".txt", ".tsv", ".out", ".err", ".lsp", ".fpo", ".dat"}
)
IMAGE_EXTENSIONS = frozenset(
    {".tif", ".tiff", ".png", ".jpg", ".jpeg", ".bmp", ".webp"}
)
MAX_ASSET_DESCRIPTIONS = 16
MAX_SOURCE_IMAGE_PIXELS = 100_000_000
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class EvidenceBuildError(RuntimeError):
    code = "EVIDENCE_BUILD_ERROR"
    retryable = False


class EvidenceUnavailable(EvidenceBuildError):
    code = "EVIDENCE_UNAVAILABLE"
    retryable = True


class EvidenceIntegrityError(EvidenceBuildError):
    code = "EVIDENCE_INTEGRITY"


class EvidencePathError(EvidenceBuildError):
    code = "EVIDENCE_PATH_INVALID"


class EvidenceTooLarge(EvidenceBuildError):
    code = "EVIDENCE_TOO_LARGE"


@dataclass(frozen=True, slots=True)
class EvidencePackage:
    dataset_id: str
    request: AnalysisRequest
    asset_count: int
    total_bytes: int
    manifest_sha256: str
    image_count: int


@dataclass(frozen=True, slots=True)
class _VerifiedAsset:
    row: Mapping[str, Any]
    path: Path
    sha256: str
    extension: str
    size_bytes: int


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise EvidenceUnavailable("asset bytes are temporarily unavailable") from exc
    return digest.hexdigest()


def _has_symlink_component(root: Path, relative_path: str) -> bool:
    current = root
    for part in PurePosixPath(relative_path).parts:
        current = current / part
        try:
            if current.is_symlink():
                return True
        except OSError:
            return True
    return False


def _resolve_asset(row: Mapping[str, Any], mapper: RootMapper) -> tuple[Path, str]:
    if row.get("path_state") not in {None, "VALID"}:
        raise EvidencePathError("asset requires path review before AI analysis")

    if row.get("managed_sha256"):
        root_key = row.get("managed_root_key")
        relative_path = row.get("managed_relpath")
        expected_hash = row.get("managed_sha256")
    else:
        root_key = row.get("original_root_key")
        relative_path = row.get("original_relpath")
        expected_hash = row.get("source_sha256") or row.get("sha256")
    if not root_key or not relative_path or not expected_hash:
        raise EvidencePathError("asset has no complete portable verified location")
    expected = str(expected_hash).lower()
    if len(expected) != 64 or any(char not in "0123456789abcdef" for char in expected):
        raise EvidenceIntegrityError("asset has no valid catalog digest")

    roots = mapper.roots
    normalized_key = str(root_key).strip().lower()
    root = roots.get(normalized_key)
    if root is None:
        raise EvidencePathError("asset refers to an unconfigured root")
    try:
        path = mapper.resolve(normalized_key, str(relative_path), must_exist=True)
    except (OSError, PathLocationError) as exc:
        raise EvidencePathError("asset location cannot be resolved inside its configured root") from exc
    if _has_symlink_component(root, str(relative_path)):
        raise EvidencePathError("symbolic-link assets are not eligible for AI analysis")
    if not path.is_file():
        raise EvidencePathError("asset location is not a regular file")
    return path, expected


def _verify_asset(row: Mapping[str, Any], mapper: RootMapper) -> _VerifiedAsset:
    path, expected_hash = _resolve_asset(row, mapper)
    expected_size = int(row.get("size_bytes") or 0)
    try:
        actual_size = path.stat().st_size
    except OSError as exc:
        raise EvidenceUnavailable("asset metadata is temporarily unavailable") from exc
    if expected_size < 0 or actual_size != expected_size:
        raise EvidenceIntegrityError("asset size changed after catalog indexing")
    actual_hash = _sha256_file(path)
    if actual_hash != expected_hash:
        raise EvidenceIntegrityError("asset digest changed after catalog indexing")
    return _VerifiedAsset(
        row=row,
        path=path,
        sha256=actual_hash,
        extension=str(row.get("extension") or "").lower(),
        size_bytes=actual_size,
    )


def _confirm_preview_source_unchanged(asset: _VerifiedAsset) -> None:
    try:
        if asset.path.stat().st_size != asset.size_bytes:
            raise EvidenceIntegrityError("asset changed while its preview was being built")
    except OSError as exc:
        raise EvidenceUnavailable("preview source became unavailable") from exc
    if _sha256_file(asset.path) != asset.sha256:
        raise EvidenceIntegrityError("asset changed while its preview was being built")


def _text_preview(path: Path, limit: int) -> str:
    try:
        with path.open("rb") as stream:
            decoded = _decode_text(stream.read(limit))
    except OSError as exc:
        raise EvidenceUnavailable("text preview is temporarily unavailable") from exc
    lines: list[str] = []
    for raw_line in decoded.splitlines():
        line = " ".join(_CONTROL_CHARACTERS.sub(" ", raw_line).split())
        if line:
            lines.append(line)
    return "\n".join(lines)[: max(1, limit)]


def _image_data_url(path: Path, max_edge: int, max_bytes: int) -> str:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as source:
                source.seek(0)
                width, height = source.size
                if width < 1 or height < 1 or width * height > MAX_SOURCE_IMAGE_PIXELS:
                    raise ValueError("image dimensions exceed the local preview safety bound")
                image = ImageOps.exif_transpose(source).convert("RGB")
                image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
                while True:
                    output = io.BytesIO()
                    image.save(output, format="PNG", optimize=True)
                    payload = output.getvalue()
                    if len(payload) <= max_bytes or min(image.size) <= 128:
                        break
                    image = image.resize(
                        (
                            max(1, int(image.width * 0.8)),
                            max(1, int(image.height * 0.8)),
                        ),
                        Image.Resampling.LANCZOS,
                    )
    except (OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise EvidenceBuildError("image preview could not be decoded safely") from exc
    if len(payload) > max_bytes:
        raise EvidenceTooLarge("bounded image preview exceeds its configured limit")
    return "data:image/png;base64," + base64.b64encode(payload).decode("ascii")


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )


def _fit_structured_payload(payload: dict[str, Any], maximum_bytes: int) -> str:
    def encoded() -> str:
        return _canonical_json(payload)

    text = encoded()
    while len(text.encode("utf-8")) > maximum_bytes:
        previews = [
            item
            for item in payload["assets"]
            if len(str(item.get("content_preview") or "")) > 256
        ]
        if previews:
            longest = max(previews, key=lambda item: len(str(item["content_preview"])))
            preview = str(longest["content_preview"])
            longest["content_preview"] = preview[: max(256, len(preview) // 2)]
            text = encoded()
            continue
        removable = next(
            (
                index
                for index in range(len(payload["assets"]) - 1, -1, -1)
                if "content_preview" not in payload["assets"][index]
                and not payload["assets"][index].get("bounded_image_attached")
            ),
            None,
        )
        if removable is not None:
            payload["assets"].pop(removable)
            payload["manifest"]["descriptions_omitted"] += 1
            text = encoded()
            continue
        raise EvidenceTooLarge("structured evidence exceeds its configured context limit")
    return text


class EvidenceBuilder:
    def __init__(
        self,
        database: Database,
        mapper: RootMapper,
        profile: LocalModelProfile,
    ) -> None:
        self.database = database
        self.mapper = mapper
        self.profile = profile

    def build(self, dataset_id: str) -> EvidencePackage:
        detail = self.database.get_dataset(dataset_id)
        if detail is None:
            raise EvidencePathError("dataset does not exist")
        raw_assets = list(detail.get("assets") or [])
        if not raw_assets:
            raise EvidencePathError("dataset has no assets")

        verified = [_verify_asset(row, self.mapper) for row in raw_assets]
        verified.sort(key=lambda item: (item.extension, item.sha256, item.size_bytes))
        manifest_records = [
            {
                "extension": item.extension,
                "mime_type": item.row.get("mime_type"),
                "sha256": item.sha256,
                "size_bytes": item.size_bytes,
            }
            for item in verified
        ]
        manifest_sha256 = hashlib.sha256(
            _canonical_json(manifest_records).encode("utf-8")
        ).hexdigest()

        ranked = sorted(
            verified,
            key=lambda item: (
                0
                if item.extension in TEXT_EXTENSIONS
                else 1
                if item.extension in IMAGE_EXTENSIONS
                else 2,
                item.extension,
                item.sha256,
            ),
        )
        descriptions: list[dict[str, Any]] = []
        image_data_urls: list[str] = []
        text_count = 0
        for asset in ranked[:MAX_ASSET_DESCRIPTIONS]:
            preview_attached = False
            description: dict[str, Any] = {
                "asset": len(descriptions) + 1,
                "extension": asset.extension,
                "mime_type": asset.row.get("mime_type"),
                "size_bytes": asset.size_bytes,
                "verified_sha256": asset.sha256,
            }
            if asset.extension in TEXT_EXTENSIONS and text_count < self.profile.max_text_assets:
                preview = _text_preview(asset.path, self.profile.max_text_bytes)
                if preview:
                    description["content_preview"] = preview
                    description["content_is_untrusted_data"] = True
                    text_count += 1
                    preview_attached = True
            if asset.extension in IMAGE_EXTENSIONS and len(image_data_urls) < self.profile.max_images:
                try:
                    image_data_urls.append(
                        _image_data_url(
                            asset.path,
                            self.profile.max_image_edge,
                            self.profile.max_image_bytes,
                        )
                    )
                    description["bounded_image_attached"] = True
                    description["image_is_untrusted_data"] = True
                    preview_attached = True
                except EvidenceBuildError:
                    description["image_preview_state"] = "UNAVAILABLE"
            if preview_attached:
                _confirm_preview_source_unchanged(asset)
            descriptions.append(description)

        extension_counts = Counter(item.extension or "(none)" for item in verified)
        payload = {
            "contract": {
                "content_is_untrusted_data": True,
                "filenames_omitted": True,
                "paths_omitted": True,
            },
            "catalog_assessment": {
                "classification_method": detail.get("classification_method"),
                "conflict": bool(detail.get("conflict")),
                "confidence": round(float(detail.get("confidence") or 0.0), 6),
                "modality": detail.get("modality"),
            },
            "manifest": {
                "asset_count": len(verified),
                "descriptions_omitted": max(0, len(verified) - len(descriptions)),
                "extension_counts": dict(sorted(extension_counts.items())),
                "manifest_sha256": manifest_sha256,
                "total_bytes": sum(item.size_bytes for item in verified),
            },
            "assets": descriptions,
        }
        structured = _fit_structured_payload(
            payload,
            self.profile.max_structured_evidence_bytes,
        )
        request = AnalysisRequest.from_evidence(structured, tuple(image_data_urls))
        return EvidencePackage(
            dataset_id=dataset_id,
            request=request,
            asset_count=len(verified),
            total_bytes=sum(item.size_bytes for item in verified),
            manifest_sha256=manifest_sha256,
            image_count=len(image_data_urls),
        )


__all__ = [
    "EvidenceBuildError",
    "EvidenceBuilder",
    "EvidenceIntegrityError",
    "EvidencePackage",
    "EvidencePathError",
    "EvidenceTooLarge",
    "EvidenceUnavailable",
    "IMAGE_EXTENSIONS",
    "TEXT_EXTENSIONS",
]
