"""Explainable, local-only scientific-file classification.

Strong, inspectable rules run first.  A separately configured scikit-learn
artifact may be used only as a fallback.  Model features deliberately exclude
directory and filename tokens so a model cannot learn the reference folder
labels used to build a training set.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import math
import os
from pathlib import Path
import re
from typing import Any, Protocol, Sequence

from .taxonomy import DataLevel, MaterialState, Modality, ParsedMetadata, RuleDefinition


MAX_PREVIEW_BYTES = 64 * 1024
MODEL_FEATURE_VERSION = 1


RULES: tuple[RuleDefinition, ...] = (
    RuleDefinition("sem-header", Modality.SEM, "SEM sidecar contains [SemImageFile] or a TM4000 instrument marker.", 0.995),
    RuleDefinition("sem-companion", Modality.SEM, "TIFF has a same-stem SEM text sidecar.", 0.985),
    RuleDefinition("sem-processed-name", Modality.SEM, "Filename identifies a known SEM processed derivative.", 0.96),
    RuleDefinition("tensile-native", Modality.TENSILE, "Instron tensile native/export filename or extension.", 0.995),
    RuleDefinition("tensile-header", Modality.TENSILE, "Tabular header contains tensile stress/strain fields.", 0.99),
    RuleDefinition("rheology-header", Modality.RHEOLOGY, "Header contains rheometer test and modulus/viscosity fields.", 0.99),
    RuleDefinition("torque-header", Modality.TORQUE, "Header contains screw torque, speed, melt, and temperature fields.", 0.995),
    RuleDefinition("ftir-numeric-axis", Modality.FTIR, "CSV resembles a dense two-column FTIR wavenumber trace.", 0.90),
    RuleDefinition("simulation-native", Modality.SIMULATION, "Known simulation-native extension or simulation material header.", 0.96),
    RuleDefinition("impact-name", Modality.IMPACT, "Filename or sibling names identify impact testing.", 0.95),
    RuleDefinition("reference-name", Modality.REFERENCE, "Document filename resembles a manuscript or scholarly reference.", 0.84),
    RuleDefinition("source-folder-hint", Modality.UNKNOWN, "Existing source folder supplies a weak, auditable modality hint.", 0.74, "source-folder"),
)

_RULES_BY_ID = {rule.id: rule for rule in RULES}

_TEXT_SUFFIXES = {
    ".csv", ".txt", ".tsv", ".out", ".err", ".lsp", ".fpo", ".dat",
}
_TIFF_SUFFIXES = {".tif", ".tiff"}
_SIMULATION_SUFFIXES = {
    ".rfn", ".of1", ".of2", ".of3", ".of4", ".sdy", ".mpi", ".die",
    ".fpo", ".lsp", ".stl", ".out", ".err",
}
_TENSILE_SUFFIXES = {".is_tens", ".id_tens"}


@dataclass(frozen=True, slots=True)
class ClassificationResult:
    label: Modality
    confidence: float
    method: str
    evidence: tuple[str, ...]
    conflict: bool
    metadata: ParsedMetadata

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label.value,
            "confidence": round(self.confidence, 6),
            "method": self.method,
            "evidence": list(self.evidence),
            "conflict": self.conflict,
            "metadata": self.metadata.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class _RuleMatch:
    rule_id: str
    label: Modality
    confidence: float
    evidence: str


class _ProbabilisticModel(Protocol):
    classes_: Sequence[str]

    def predict_proba(self, rows: Sequence[dict[str, str | float]]) -> Any: ...


@dataclass(slots=True)
class _ModelBundle:
    model: _ProbabilisticModel
    feature_version: int
    name: str


_MODEL_BUNDLE: _ModelBundle | None = None


def configure_model(path: Path | None) -> None:
    """Load or clear the optional local model without making it a dependency.

    ``joblib`` and scikit-learn are imported only when an artifact is explicitly
    configured.  A mismatched feature version is rejected rather than silently
    producing unreliable predictions.
    """

    global _MODEL_BUNDLE
    if path is None:
        _MODEL_BUNDLE = None
        return
    try:
        import joblib  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - depends on optional install
        raise RuntimeError("Loading a trained model requires joblib and scikit-learn") from exc
    artifact = joblib.load(path)
    if not isinstance(artifact, dict) or "model" not in artifact:
        raise ValueError("Invalid Academic Vault classifier artifact")
    feature_version = int(artifact.get("feature_version", -1))
    if feature_version != MODEL_FEATURE_VERSION:
        raise ValueError(
            f"Model feature version {feature_version} does not match {MODEL_FEATURE_VERSION}"
        )
    _MODEL_BUNDLE = _ModelBundle(
        model=artifact["model"],
        feature_version=feature_version,
        name=str(artifact.get("name", path.name)),
    )


def _read_prefix(path: Path, limit: int = MAX_PREVIEW_BYTES) -> bytes:
    try:
        with path.open("rb") as stream:
            return stream.read(limit)
    except (OSError, PermissionError):
        return b""


def _decode_text(data: bytes) -> str:
    if not data:
        return ""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError:
            return ""
    # Instrument exports are often UTF-16LE.  A binary file may also contain
    # zero bytes, so only attempt UTF-16 when zeros occur in the expected byte
    # lane; otherwise do not turn arbitrary binary data into classifier text.
    prefix = data[:256]
    if b"\x00" in prefix:
        odd_lane = prefix[1::2]
        even_lane = prefix[0::2]
        looks_utf16_le = odd_lane.count(0) >= max(2, len(odd_lane) // 3)
        looks_utf16_be = even_lane.count(0) >= max(2, len(even_lane) // 3)
        if looks_utf16_le or looks_utf16_be:
            encoding = "utf-16-le" if looks_utf16_le else "utf-16-be"
            try:
                return data.decode(encoding)
            except UnicodeDecodeError:
                pass
        return ""
    for encoding in ("utf-8-sig", "gb18030", "utf-16", "cp1252"):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def _preview(path: Path, prefix: bytes | None = None) -> str:
    if path.suffix.casefold() not in _TEXT_SUFFIXES:
        return ""
    return _decode_text(prefix if prefix is not None else _read_prefix(path))


def _normalise_siblings(path: Path, sibling_names: list[str] | None) -> tuple[str, ...]:
    if sibling_names is None:
        try:
            sibling_names = [entry.name for entry in path.parent.iterdir()]
        except (OSError, PermissionError):
            sibling_names = []
    return tuple(Path(name).name.casefold() for name in sibling_names)


def _add(
    matches: list[_RuleMatch], rule_id: str, label: Modality, evidence: str,
    confidence: float | None = None,
) -> None:
    definition = _RULES_BY_ID[rule_id]
    matches.append(_RuleMatch(rule_id, label, confidence or definition.confidence, evidence))


def _contains_all(text: str, *tokens: str) -> bool:
    return all(token in text for token in tokens)


def _numeric_rows(text: str, maximum: int = 80) -> list[list[float]]:
    rows: list[list[float]] = []
    for raw_line in text.splitlines()[:maximum]:
        line = raw_line.strip().strip('"')
        if not line:
            continue
        parts = [item.strip().strip('"') for item in re.split(r"[,;\t]", line)]
        if len(parts) == 1:
            parts = line.split()
        try:
            values = [float(item) for item in parts if item != ""]
        except ValueError:
            continue
        if len(values) == len(parts) and values:
            rows.append(values)
    return rows


def _looks_like_ftir(text: str) -> bool:
    rows = [row for row in _numeric_rows(text) if len(row) == 2]
    if len(rows) < 8:
        return False
    axis = [row[0] for row in rows[:20]]
    monotonic_up = all(right > left for left, right in zip(axis, axis[1:]))
    monotonic_down = all(right < left for left, right in zip(axis, axis[1:]))
    starts_on_spectral_axis = 350.0 <= axis[0] <= 4500.0
    small_regular_step = max(abs(right - left) for left, right in zip(axis, axis[1:])) < 20
    return starts_on_spectral_axis and small_regular_step and (monotonic_up or monotonic_down)


def _source_folder_matches(path: Path) -> list[tuple[Modality, str, float]]:
    aliases: tuple[tuple[Modality, tuple[str, ...], float], ...] = (
        (Modality.SEM, ("sem",), 0.74),
        (Modality.TENSILE, ("tensile",), 0.74),
        (Modality.FTIR, ("ftir", "红外"), 0.74),
        (Modality.RHEOLOGY, ("流变", "rheology"), 0.74),
        (Modality.TORQUE, ("转矩", "torque"), 0.74),
        (Modality.SIMULATION, ("模拟", "simulation"), 0.74),
        (Modality.IMPACT, ("impact resistence", "impact resistance", "impact strength"), 0.82),
        (Modality.GPC, ("gpc",), 0.82),
        (Modality.OPTICAL, ("optical image", "optical"), 0.82),
        (Modality.REFERENCE, ("reference",), 0.88),
    )
    segments = []
    for part in path.parent.parts:
        normalized = re.sub(r"^\d+\s+", "", part.strip().casefold())
        segments.append(normalized)
    found: list[tuple[Modality, str, float]] = []
    for label, tokens, score in aliases:
        for segment in segments:
            if segment in tokens:
                found.append((label, segment, score))
                break
    return found


def _rule_matches(path: Path, text: str, siblings: tuple[str, ...]) -> list[_RuleMatch]:
    matches: list[_RuleMatch] = []
    lower = text.casefold()
    name = path.name.casefold()
    suffix = path.suffix.casefold()

    if "[semimagefile]" in lower or "instructname=tm4000" in lower:
        _add(matches, "sem-header", Modality.SEM, "SEM metadata header/instrument marker found")

    if suffix in _TIFF_SUFFIXES:
        raw_stem = re.sub(r"_(?:white_backplate|white_halo)$", "", path.stem.casefold())
        if f"{raw_stem}.txt" in siblings:
            _add(matches, "sem-companion", Modality.SEM, f"same-stem sidecar {raw_stem}.txt found")
    if "sem_processed" in name or "white_backplate" in name or "white_halo" in name:
        _add(matches, "sem-processed-name", Modality.SEM, "known SEM processed-image suffix found")

    if suffix in _TENSILE_SUFFIXES or ".is_tens" in name:
        _add(matches, "tensile-native", Modality.TENSILE, f"tensile native/export marker {suffix or name} found")
    tensile_header = (
        ("拉伸应力" in text and "拉伸应变" in text)
        or _contains_all(lower, "tensile stress", "tensile strain")
    )
    if tensile_header:
        _add(matches, "tensile-header", Modality.TENSILE, "tensile stress and strain columns found")

    rheology_terms = sum(
        token in lower
        for token in (
            "storage modulus", "loss modulus", "angular frequency", "complex viscosity",
            "frequency sweep", "stress relaxation", "shear strain", "loss factor",
        )
    )
    if rheology_terms >= 2:
        _add(matches, "rheology-header", Modality.RHEOLOGY, f"{rheology_terms} rheometer header markers found")

    torque_terms = sum(
        token in lower
        for token in ("screw torque", "screw speed", "melt viscosity", "pressure transducer", "temp. front")
    )
    if torque_terms >= 3:
        _add(matches, "torque-header", Modality.TORQUE, f"{torque_terms} torque/extrusion header markers found")

    if suffix == ".csv" and _looks_like_ftir(text):
        _add(matches, "ftir-numeric-axis", Modality.FTIR, "dense monotonic two-column spectral axis found")

    if suffix in _SIMULATION_SUFFIXES:
        confidence = 0.90 if suffix in {".stl", ".out", ".err"} else 0.96
        _add(matches, "simulation-native", Modality.SIMULATION, f"simulation-associated extension {suffix} found", confidence)
    if "p=0[mpa]" in lower or ("pvt" in name and suffix == ".txt"):
        _add(matches, "simulation-native", Modality.SIMULATION, "simulation material/PVT header found", 0.94)

    impact_tokens = ("impact strength", "impact resistance", "impact resistence")
    if any(token in name for token in impact_tokens):
        _add(matches, "impact-name", Modality.IMPACT, "impact-test filename marker found")
    elif any(any(token in sibling for token in impact_tokens) for sibling in siblings):
        _add(matches, "impact-name", Modality.IMPACT, "impact-test sibling marker found", 0.90)

    reference_name_tokens = (
        "manuscript", "support information", "supplementary information", "nature-",
        "jacs-", "advanced materials", "sciadv.",
    )
    if suffix in {".pdf", ".docx"} and any(token in name for token in reference_name_tokens):
        _add(matches, "reference-name", Modality.REFERENCE, "scholarly/manuscript filename marker found")

    for label, segment, score in _source_folder_matches(path):
        _add(
            matches,
            "source-folder-hint",
            label,
            f"weak source-folder hint {segment!r}; not a model feature",
            score,
        )
    return matches


_MONTHS = {
    month.casefold(): index
    for index, month in enumerate(
        ("", "January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December")
    )
    if month
}


def _valid_iso_date(year: int, month: int, day: int) -> str | None:
    try:
        return datetime(year, month, day).date().isoformat()
    except ValueError:
        return None


def _extract_date(token_text: str, preview: str) -> tuple[str | None, str | None]:
    for match in re.finditer(r"(?<!\d)(20\d{2})[-_]?([01]\d)[-_]?([0-3]\d)(?!\d)", token_text):
        parsed = _valid_iso_date(*(int(value) for value in match.groups()))
        if parsed:
            return parsed, f"date token {match.group(0)!r}"
    for match in re.finditer(r"(?<!\d)(\d{2})([01]\d)([0-3]\d)(?!\d)", token_text):
        year, month, day = (int(value) for value in match.groups())
        parsed = _valid_iso_date(2000 + year, month, day)
        if parsed:
            return parsed, f"short date token {match.group(0)!r}"
    header_match = re.search(
        r"\b([0-3]?\d)\s+(January|February|March|April|May|June|July|August|September|October|November|December)\s+(20\d{2})\b",
        preview,
        re.IGNORECASE,
    )
    if header_match:
        day = int(header_match.group(1))
        month = _MONTHS[header_match.group(2).casefold()]
        year = int(header_match.group(3))
        parsed = _valid_iso_date(year, month, day)
        if parsed:
            return parsed, f"embedded acquisition date {header_match.group(0)!r}"
    return None, None


def _extract_sample(path: Path) -> tuple[str | None, str | None]:
    # Search the basename first, then nearest parent outward.  These are metadata
    # parsers only; none of these strings enter the model feature vector.
    candidates = [path.stem, *reversed(path.parent.parts)]
    patterns: tuple[tuple[re.Pattern[str], Any], ...] = (
        (re.compile(r"(?<![A-Z0-9])([EV])[-_\s]*PA(?![A-Z0-9])", re.I), lambda m: f"{m.group(1).upper()}-PA"),
        (re.compile(r"(?<![A-Z0-9])(D)[-_\s]*PA(\d*)(?![A-Z0-9])", re.I), lambda m: f"D-PA{m.group(2)}".rstrip()),
        (re.compile(r"(?<![A-Z0-9])(E\d+)(?=$|[^A-Z0-9])", re.I), lambda m: m.group(1).upper()),
        (re.compile(r"(?<![A-Z0-9])(A\d+(?:ZN\d+)?)(?=$|[^A-Z0-9])", re.I), lambda m: m.group(1).upper()),
    )
    for candidate in candidates:
        for pattern, normalizer in patterns:
            match = pattern.search(candidate)
            if match:
                return str(normalizer(match)), f"sample token {match.group(0)!r}"
    return None, None


def _extract_material(path: Path) -> tuple[MaterialState, str | None, tuple[str, ...]]:
    segments = {part.strip().casefold() for part in (*path.parent.parts, path.stem)}
    virgin_tokens = {"新料", "virgin", "virgin material", "new material"}
    recycled_tokens = {"回收料", "recycled", "recycle", "recycled material"}
    has_virgin = bool(segments & virgin_tokens)
    has_recycled = bool(segments & recycled_tokens)
    if has_virgin and has_recycled:
        return MaterialState.UNKNOWN, None, ("both virgin and recycled material tokens found",)
    if has_virgin:
        return MaterialState.VIRGIN, "virgin-material path token", ()
    if has_recycled:
        return MaterialState.RECYCLED, "recycled-material path token", ()
    return MaterialState.UNKNOWN, None, ()


def _extract_lifecycle(path: Path, siblings: tuple[str, ...]) -> tuple[DataLevel, str | None]:
    joined = " ".join((*path.parent.parts[-5:], path.name)).casefold()
    suffix = path.suffix.casefold()
    if any(token in joined for token in ("sem_processed", "white_backplate", "white_halo")):
        return DataLevel.PROCESSED, "processed-image token"
    if any(token in joined for token in ("summary", "data collection", "filtered_workbooks", "compare")):
        return DataLevel.SUMMARY, "summary/collection token"
    if suffix in {".pdf", ".docx"}:
        return DataLevel.REPORT, "report/document extension"
    if suffix in _TENSILE_SUFFIXES or suffix == ".opju" or "_exports" in joined:
        return DataLevel.NATIVE_EXPORT, "instrument-native/export token"
    if suffix in _TIFF_SUFFIXES:
        raw_stem = re.sub(r"_(?:white_backplate|white_halo)$", "", path.stem.casefold())
        if f"{raw_stem}.txt" in siblings:
            return DataLevel.RAW, "raw TIFF with acquisition sidecar"
    return DataLevel.UNKNOWN, None


def parse_metadata(path: Path, preview: str = "", sibling_names: tuple[str, ...] = ()) -> ParsedMetadata:
    token_text = " ".join((*path.parent.parts[-6:], path.stem))
    evidence: list[str] = []
    conflicts: list[str] = []

    sample, sample_evidence = _extract_sample(path)
    if sample_evidence:
        evidence.append(sample_evidence)
    date, date_evidence = _extract_date(token_text, preview)
    if date_evidence:
        evidence.append(date_evidence)
    material, material_evidence, material_conflicts = _extract_material(path)
    if material_evidence:
        evidence.append(material_evidence)
    conflicts.extend(material_conflicts)
    lifecycle, lifecycle_evidence = _extract_lifecycle(path, sibling_names)
    if lifecycle_evidence:
        evidence.append(lifecycle_evidence)
    return ParsedMetadata(sample, date, material, lifecycle, tuple(evidence), tuple(conflicts))


def extract_model_features(path: Path, *, preview: str | None = None) -> dict[str, str | float]:
    """Return content/structure features with no filename or directory labels."""

    prefix = _read_prefix(path)
    text = _preview(path, prefix) if preview is None else preview
    lower = text.casefold()
    lines = [line for line in text.splitlines() if line.strip()]
    first_line = lines[0] if lines else ""
    numeric_rows = _numeric_rows(text, maximum=40)
    size = 0
    try:
        size = path.stat().st_size
    except OSError:
        pass
    magic = "other"
    if prefix.startswith(b"%PDF"):
        magic = "pdf"
    elif prefix.startswith(b"PK\x03\x04"):
        magic = "zip"
    elif prefix.startswith((b"II*\x00", b"MM\x00*")):
        magic = "tiff"
    return {
        "suffix": path.suffix.casefold() or "[none]",
        "magic": magic,
        "size_log2": math.log2(size + 1),
        "line_count_preview": float(len(lines)),
        "first_line_columns": float(max(first_line.count(","), first_line.count("\t")) + 1 if first_line else 0),
        "numeric_row_share": (len(numeric_rows) / max(1, min(40, len(lines)))),
        "has_sem_header": float("[semimagefile]" in lower or "instructname=tm4000" in lower),
        "has_tensile_header": float("拉伸应力" in text or "tensile stress" in lower),
        "has_rheology_header": float("storage modulus" in lower or "frequency sweep" in lower),
        "has_torque_header": float("screw torque" in lower and "screw speed" in lower),
        "looks_ftir_numeric": float(_looks_like_ftir(text)),
        "has_pvt_header": float("p=0[mpa]" in lower),
    }


def _model_prediction(path: Path, preview: str) -> tuple[Modality, float, str] | None:
    if _MODEL_BUNDLE is None:
        return None
    probabilities = _MODEL_BUNDLE.model.predict_proba([extract_model_features(path, preview=preview)])[0]
    classes = list(_MODEL_BUNDLE.model.classes_)
    index = max(range(len(classes)), key=lambda item: float(probabilities[item]))
    try:
        label = Modality(str(classes[index]))
    except ValueError:
        return None
    return label, float(probabilities[index]), _MODEL_BUNDLE.name


def classify_file(path: Path, *, sibling_names: list[str] | None = None) -> ClassificationResult:
    """Classify one file without writing, moving, renaming, or uploading it."""

    path = Path(path)
    prefix = _read_prefix(path)
    text = _preview(path, prefix)
    siblings = _normalise_siblings(path, sibling_names)
    metadata = parse_metadata(path, text, siblings)
    matches = _rule_matches(path, text, siblings)

    grouped: dict[Modality, list[_RuleMatch]] = {}
    for match in matches:
        grouped.setdefault(match.label, []).append(match)
    ranked: list[tuple[Modality, float, list[_RuleMatch]]] = []
    for label, label_matches in grouped.items():
        strongest = max(item.confidence for item in label_matches)
        corroboration = min(0.015, 0.005 * (len(label_matches) - 1))
        ranked.append((label, min(0.995, strongest + corroboration), label_matches))
    ranked.sort(key=lambda item: item[1], reverse=True)

    if ranked:
        top_label, top_score, top_matches = ranked[0]
        conflict = bool(metadata.conflicts)
        evidence = [item.evidence for item in top_matches]
        if len(ranked) > 1 and ranked[1][1] >= 0.72:
            conflict = True
            competitor, competitor_score, competitor_matches = ranked[1]
            evidence.append(
                f"conflict: {competitor.value} scored {competitor_score:.3f} via "
                f"{', '.join(item.rule_id for item in competitor_matches)}"
            )
        method = "rule:" + "+".join(sorted({item.rule_id for item in top_matches}))

        # A model can supersede only weak, non-conflicting hints.  It never
        # overrides a strong instrument rule or hides a disagreement.
        model_result = _model_prediction(path, text) if top_score < 0.90 and not conflict else None
        if model_result is not None:
            model_label, model_score, model_name = model_result
            if model_score > top_score:
                if model_label != top_label and top_score >= 0.72:
                    conflict = True
                    evidence.append(f"conflict: local model proposed {model_label.value}")
                else:
                    top_label = model_label
                    top_score = model_score
                    method = f"model:{model_name}"
                    evidence = ["local lightweight model fallback; score is not certainty"]
        if conflict:
            top_score = min(top_score, 0.79)
        return ClassificationResult(
            top_label,
            max(0.0, min(1.0, top_score)),
            method,
            tuple(evidence),
            conflict,
            metadata,
        )

    model_result = _model_prediction(path, text)
    if model_result is not None:
        label, confidence, model_name = model_result
        return ClassificationResult(
            label,
            max(0.0, min(1.0, confidence)),
            f"model:{model_name}",
            ("local lightweight model fallback; score is not certainty",),
            bool(metadata.conflicts),
            metadata,
        )
    missing = not path.exists()
    evidence = ("file is unavailable or unreadable",) if missing else ("no supported rule matched",)
    return ClassificationResult(Modality.UNKNOWN, 0.0, "unknown", evidence, bool(metadata.conflicts), metadata)


_env_model = os.environ.get("ACADEMIC_VAULT_MODEL_PATH")
if _env_model:  # pragma: no cover - opt-in deployment behavior
    configure_model(Path(_env_model))


__all__ = [
    "ClassificationResult",
    "MODEL_FEATURE_VERSION",
    "RULES",
    "classify_file",
    "configure_model",
    "extract_model_features",
    "parse_metadata",
]
