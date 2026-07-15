"""Prepare and validate blinded, human-labelled Phase 3 gold sets."""

from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..taxonomy import Modality
from .benchmark import assert_report_path_is_outside_repository


GOLD_SET_SCHEMA_VERSION = 1
REVIEW_FIELDS = (
    "candidate_id",
    "dataset_id",
    "evidence_fingerprint",
    "decision",
    "gold_modality",
    "reviewer",
    "reviewed_at",
    "exclusion_reason",
    "notes",
)
_IMAGE_EXTENSIONS = frozenset({".bmp", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"})
_TERMINAL_DECISIONS = frozenset({"INCLUDE", "EXCLUDE"})


@dataclass(frozen=True, slots=True)
class GoldCandidate:
    dataset_id: str
    dataset_revision: int
    evidence_fingerprint: str
    predicted_modality: str
    rule_confidence: float
    confidence_band: str
    classification_method: str
    conflict: bool
    pixel_evidence: bool
    source_kind: str
    status: str
    asset_count: int

    @property
    def stratum(self) -> tuple[str, str, bool, str]:
        return (
            self.predicted_modality,
            self.confidence_band,
            self.pixel_evidence,
            self.classification_method,
        )

    @property
    def mandatory(self) -> bool:
        return (
            self.predicted_modality == Modality.UNKNOWN.value
            or self.conflict
            or self.rule_confidence < 0.6
        )


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _readonly_connection(path: str | Path) -> tuple[Path, sqlite3.Connection]:
    resolved = Path(path).expanduser().resolve(strict=True)
    wal_path = Path(str(resolved) + "-wal")
    if wal_path.is_file() and wal_path.stat().st_size:
        raise ValueError(
            "catalog has a non-empty WAL; create a consistent snapshot before gold-set sampling"
        )
    connection = sqlite3.connect(
        f"file:{resolved.as_posix()}?mode=ro&immutable=1",
        uri=True,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    integrity = [str(row[0]) for row in connection.execute("PRAGMA integrity_check")]
    if integrity != ["ok"]:
        connection.close()
        raise ValueError("catalog integrity_check failed before gold-set sampling")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        connection.close()
        raise ValueError("catalog foreign_key_check failed before gold-set sampling")
    return resolved, connection


def _confidence_band(value: float) -> str:
    if value < 0.5:
        return "LT_0_5"
    if value < 0.8:
        return "0_5_TO_0_8"
    if value < 0.95:
        return "0_8_TO_0_95"
    return "GE_0_95"


def _stable_rank(seed: str, *values: object) -> str:
    text = "\0".join((seed, *(str(value) for value in values)))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _candidate_id(seed: str, dataset_id: str) -> str:
    return _stable_rank("academic-vault-gold-candidate-v1", seed, dataset_id)[:20]


def load_gold_candidate_pool(
    catalog_path: str | Path,
    *,
    source_kind: str = "reference",
) -> tuple[Path, list[GoldCandidate], dict[str, int]]:
    """Read only path-free sampling metadata from a catalog."""

    catalog, connection = _readonly_connection(catalog_path)
    try:
        dataset_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(datasets)")
        }
        revision_expression = "d.revision" if "revision" in dataset_columns else "0"
        datasets = [
            dict(row)
            for row in connection.execute(
                f"""
                SELECT d.id,{revision_expression} AS revision,d.modality,d.confidence,
                       d.classification_method,d.conflict,d.source_kind,d.status
                FROM datasets d
                WHERE lower(d.source_kind)=lower(?)
                  AND d.status NOT IN ('STALE','PATH_REVIEW')
                ORDER BY d.id
                """,
                (source_kind,),
            ).fetchall()
        ]
        asset_rows = [
            dict(row)
            for row in connection.execute(
                """
                SELECT a.dataset_id,a.extension,a.size_bytes,
                       COALESCE(a.source_sha256,a.sha256) AS digest,
                       a.mime_type,a.role
                FROM assets a JOIN datasets d ON d.id=a.dataset_id
                WHERE lower(d.source_kind)=lower(?)
                ORDER BY a.dataset_id,a.extension,a.size_bytes,digest,a.id
                """,
                (source_kind,),
            ).fetchall()
        ]
    finally:
        connection.close()

    assets_by_dataset: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in asset_rows:
        assets_by_dataset[str(row["dataset_id"])].append(row)

    excluded = Counter()
    candidates: list[GoldCandidate] = []
    for row in datasets:
        dataset_id = str(row["id"])
        assets = assets_by_dataset.get(dataset_id, [])
        if not assets:
            excluded["NO_ASSETS"] += 1
            continue
        manifest: list[dict[str, Any]] = []
        valid = True
        pixel_evidence = False
        for asset in assets:
            digest = str(asset.get("digest") or "").lower()
            if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
                valid = False
                break
            extension = str(asset.get("extension") or "").lower()
            mime_type = str(asset.get("mime_type") or "").lower()
            role = str(asset.get("role") or "").upper()
            pixel_evidence = pixel_evidence or (
                extension in _IMAGE_EXTENSIONS
                or mime_type.startswith("image/")
                or role == "IMAGE"
            )
            manifest.append(
                {
                    "extension": extension,
                    "sha256": digest,
                    "size_bytes": int(asset.get("size_bytes") or 0),
                }
            )
        if not valid:
            excluded["INVALID_ASSET_DIGEST"] += 1
            continue
        confidence = float(row.get("confidence") or 0)
        candidates.append(
            GoldCandidate(
                dataset_id=dataset_id,
                dataset_revision=int(row.get("revision") or 0),
                evidence_fingerprint=hashlib.sha256(
                    _canonical_json(manifest).encode("utf-8")
                ).hexdigest(),
                predicted_modality=str(row.get("modality") or Modality.UNKNOWN.value).upper(),
                rule_confidence=confidence,
                confidence_band=_confidence_band(confidence),
                classification_method=str(row.get("classification_method") or "unknown"),
                conflict=bool(row.get("conflict")),
                pixel_evidence=pixel_evidence,
                source_kind=str(row.get("source_kind") or ""),
                status=str(row.get("status") or ""),
                asset_count=len(assets),
            )
        )
    return catalog, candidates, dict(sorted(excluded.items()))


def select_gold_candidates(
    pool: Sequence[GoldCandidate],
    *,
    target: int,
    seed: str,
) -> list[GoldCandidate]:
    if target < 1:
        raise ValueError("target must be positive")
    if not seed.strip():
        raise ValueError("seed must not be empty")
    if len(pool) < target:
        raise ValueError(f"candidate pool has {len(pool)} datasets; target is {target}")

    mandatory = sorted(
        (item for item in pool if item.mandatory),
        key=lambda item: _stable_rank(seed, "mandatory", item.dataset_id),
    )
    if len(mandatory) > target:
        raise ValueError(
            f"mandatory unknown/conflict/very-low-confidence cases ({len(mandatory)}) exceed target {target}"
        )
    selected = list(mandatory)
    seen = {item.dataset_id for item in selected}

    groups: dict[tuple[str, str, bool, str], deque[GoldCandidate]] = {}
    grouped: dict[tuple[str, str, bool, str], list[GoldCandidate]] = defaultdict(list)
    for item in pool:
        if item.dataset_id not in seen:
            grouped[item.stratum].append(item)
    for key, items in grouped.items():
        items.sort(key=lambda item: _stable_rank(seed, "candidate", item.dataset_id))
        groups[key] = deque(items)
    order = sorted(groups, key=lambda key: _stable_rank(seed, "stratum", *key))

    while len(selected) < target:
        added = False
        for key in order:
            queue = groups[key]
            if not queue:
                continue
            selected.append(queue.popleft())
            added = True
            if len(selected) == target:
                break
        if not added:  # pragma: no cover - pool size check protects this branch
            break
    return selected


def build_gold_candidate_bundle(
    catalog_path: str | Path,
    *,
    target: int = 180,
    seed: str = "phase3-v1",
    source_kind: str = "reference",
) -> dict[str, Any]:
    catalog, pool, excluded = load_gold_candidate_pool(
        catalog_path,
        source_kind=source_kind,
    )
    selected = select_gold_candidates(pool, target=target, seed=seed)
    audit_candidates: list[dict[str, Any]] = []
    review_rows: list[dict[str, str]] = []
    for item in selected:
        candidate_id = _candidate_id(seed, item.dataset_id)
        audit_candidates.append(
            {
                "candidate_id": candidate_id,
                "dataset_id": item.dataset_id,
                "dataset_revision": item.dataset_revision,
                "evidence_fingerprint": item.evidence_fingerprint,
                "sampling_only_prediction": {
                    "modality": item.predicted_modality,
                    "confidence": item.rule_confidence,
                    "confidence_band": item.confidence_band,
                    "classification_method": item.classification_method,
                    "conflict": item.conflict,
                    "pixel_evidence": item.pixel_evidence,
                },
                "source_kind": item.source_kind,
                "status_at_selection": item.status,
                "asset_count": item.asset_count,
                "mandatory_case": item.mandatory,
            }
        )
        review_rows.append(
            {
                "candidate_id": candidate_id,
                "dataset_id": item.dataset_id,
                "evidence_fingerprint": item.evidence_fingerprint,
                "decision": "PENDING",
                "gold_modality": "",
                "reviewer": "",
                "reviewed_at": "",
                "exclusion_reason": "",
                "notes": "",
            }
        )

    selection_digest = hashlib.sha256(
        _canonical_json(audit_candidates).encode("utf-8")
    ).hexdigest()
    distribution = Counter(
        item["sampling_only_prediction"]["modality"] for item in audit_candidates
    )
    audit = {
        "schema_version": GOLD_SET_SCHEMA_VERSION,
        "kind": "academic-vault-gold-candidate-audit",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "catalog_open_mode": "immutable-read-only",
        "catalog_sha256": _sha256_file(catalog),
        "source_kind": source_kind,
        "selection_seed": seed,
        "target_count": target,
        "pool_count": len(pool),
        "excluded_before_sampling": excluded,
        "selection_manifest_sha256": selection_digest,
        "human_labels_prefilled": 0,
        "sampling_prediction_is_not_gold_truth": True,
        "selected_prediction_distribution": dict(sorted(distribution.items())),
        "candidates": audit_candidates,
    }
    return {"audit": audit, "review_rows": review_rows}


def write_gold_candidate_bundle(
    output_directory: str | Path,
    bundle: Mapping[str, Any],
    *,
    repository_root: str | Path,
) -> dict[str, str]:
    output = assert_report_path_is_outside_repository(
        output_directory,
        repository_root,
    )
    output.mkdir(parents=True, exist_ok=False)
    audit_path = output / "candidate-audit.json"
    review_path = output / "blind-review.csv"
    instructions_path = output / "README.txt"
    audit_path.write_text(
        json.dumps(bundle["audit"], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    with review_path.open("x", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=REVIEW_FIELDS)
        writer.writeheader()
        writer.writerows(bundle["review_rows"])
    instructions_path.write_text(
        "Academic Vault Phase 3 blinded gold review\n"
        "\n"
        "1. Do not open candidate-audit.json while labelling; it contains rule predictions used only for sampling.\n"
        "2. Review each dataset locally by dataset_id. Raw paths and filenames are intentionally absent here.\n"
        "3. Set decision to INCLUDE or EXCLUDE. INCLUDE requires gold_modality, reviewer and timezone-aware reviewed_at.\n"
        "4. EXCLUDE requires exclusion_reason. Do not infer labels from directory or canonical names.\n"
        "5. Run the validator before any benchmark.\n",
        encoding="utf-8",
    )
    return {
        "directory": str(output),
        "audit": str(audit_path),
        "review": str(review_path),
        "instructions": str(instructions_path),
    }


def _timezone_aware_timestamp(value: str) -> bool:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def validate_gold_review(
    review_path: str | Path,
    audit_path: str | Path,
    *,
    minimum_included: int = 150,
) -> dict[str, Any]:
    if minimum_included < 1:
        raise ValueError("minimum_included must be positive")
    audit = json.loads(Path(audit_path).read_text(encoding="utf-8"))
    if audit.get("schema_version") != GOLD_SET_SCHEMA_VERSION:
        raise ValueError("unsupported gold candidate audit schema")
    raw_audit_candidates = list(audit.get("candidates") or [])
    expected_manifest = hashlib.sha256(
        _canonical_json(raw_audit_candidates).encode("utf-8")
    ).hexdigest()
    if expected_manifest != audit.get("selection_manifest_sha256"):
        raise ValueError("gold candidate audit selection manifest does not match its contents")
    audit_candidates = {
        str(item["candidate_id"]): item for item in raw_audit_candidates
    }
    if len(audit_candidates) != len(raw_audit_candidates):
        raise ValueError("gold candidate audit contains duplicate candidate_id values")
    if len({str(item.get("dataset_id")) for item in raw_audit_candidates}) != len(raw_audit_candidates):
        raise ValueError("gold candidate audit contains duplicate dataset_id values")
    if int(audit.get("target_count") or 0) != len(raw_audit_candidates):
        raise ValueError("gold candidate audit target count does not match its contents")
    issues: list[str] = []
    seen: set[str] = set()
    decisions = Counter()
    modality_counts = Counter()
    with Path(review_path).open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if tuple(reader.fieldnames or ()) != REVIEW_FIELDS:
            raise ValueError("blind review columns do not match the versioned schema")
        for line_number, row in enumerate(reader, start=2):
            candidate_id = str(row.get("candidate_id") or "")
            if candidate_id in seen:
                issues.append(f"line {line_number}: duplicate candidate_id")
                continue
            seen.add(candidate_id)
            audit_row = audit_candidates.get(candidate_id)
            if audit_row is None:
                issues.append(f"line {line_number}: candidate_id is not in audit")
                continue
            if row.get("dataset_id") != audit_row.get("dataset_id"):
                issues.append(f"line {line_number}: dataset_id does not match audit")
            if row.get("evidence_fingerprint") != audit_row.get("evidence_fingerprint"):
                issues.append(f"line {line_number}: evidence fingerprint does not match audit")
            decision = str(row.get("decision") or "").upper()
            decisions[decision or "EMPTY"] += 1
            if decision == "PENDING":
                continue
            if decision not in _TERMINAL_DECISIONS:
                issues.append(f"line {line_number}: decision must be PENDING, INCLUDE or EXCLUDE")
                continue
            reviewer = str(row.get("reviewer") or "").strip()
            reviewed_at = str(row.get("reviewed_at") or "").strip()
            if not reviewer:
                issues.append(f"line {line_number}: terminal decision requires reviewer")
            if not _timezone_aware_timestamp(reviewed_at):
                issues.append(f"line {line_number}: terminal decision requires timezone-aware reviewed_at")
            if decision == "INCLUDE":
                modality = str(row.get("gold_modality") or "").upper()
                if modality not in {item.value for item in Modality}:
                    issues.append(f"line {line_number}: gold_modality is outside the taxonomy")
                else:
                    modality_counts[modality] += 1
            elif not str(row.get("exclusion_reason") or "").strip():
                issues.append(f"line {line_number}: EXCLUDE requires exclusion_reason")

    missing = sorted(set(audit_candidates) - seen)
    if missing:
        issues.append(f"blind review is missing {len(missing)} audited candidate(s)")
    included = decisions.get("INCLUDE", 0)
    pending = decisions.get("PENDING", 0)
    if pending:
        issues.append(f"{pending} candidate(s) are still PENDING")
    if included < minimum_included:
        issues.append(
            f"included human labels ({included}) are below required minimum ({minimum_included})"
        )
    if issues:
        raise ValueError("gold review validation failed: " + "; ".join(issues[:20]))
    return {
        "schema_version": GOLD_SET_SCHEMA_VERSION,
        "ready": True,
        "audited_candidates": len(audit_candidates),
        "included": included,
        "excluded": decisions.get("EXCLUDE", 0),
        "pending": 0,
        "gold_modality_distribution": dict(sorted(modality_counts.items())),
        "selection_manifest_sha256": audit.get("selection_manifest_sha256"),
    }


__all__ = [
    "GOLD_SET_SCHEMA_VERSION",
    "GoldCandidate",
    "REVIEW_FIELDS",
    "build_gold_candidate_bundle",
    "load_gold_candidate_pool",
    "select_gold_candidates",
    "validate_gold_review",
    "write_gold_candidate_bundle",
]
