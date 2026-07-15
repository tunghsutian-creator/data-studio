"""Canonical classification values shared by the local classifier and API.

This module intentionally uses only the Python standard library.  In particular,
importing the rule classifier must not require scikit-learn or joblib.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class Modality(StrEnum):
    SEM = "SEM"
    TENSILE = "TENSILE"
    FTIR = "FTIR"
    RHEOLOGY = "RHEOLOGY"
    TORQUE = "TORQUE"
    SIMULATION = "SIMULATION"
    IMPACT = "IMPACT"
    GPC = "GPC"
    OPTICAL = "OPTICAL"
    REFERENCE = "REFERENCE"
    UNKNOWN = "UNKNOWN"


class MaterialState(StrEnum):
    VIRGIN = "VIRGIN"
    RECYCLED = "RECYCLED"
    UNKNOWN = "UNKNOWN"


class DataLevel(StrEnum):
    RAW = "RAW"
    NATIVE_EXPORT = "NATIVE_EXPORT"
    PROCESSED = "PROCESSED"
    SUMMARY = "SUMMARY"
    REPORT = "REPORT"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class ParsedMetadata:
    """Conservative metadata parsed from names, nearby folders, and headers."""

    sample: str | None = None
    date: str | None = None
    material: MaterialState = MaterialState.UNKNOWN
    lifecycle: DataLevel = DataLevel.UNKNOWN
    evidence: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "sample": self.sample,
            "date": self.date,
            "material": self.material.value,
            "lifecycle": self.lifecycle.value,
            "evidence": list(self.evidence),
            "conflicts": list(self.conflicts),
        }


@dataclass(frozen=True, slots=True)
class RuleDefinition:
    """Human-readable rule registry entry exposed to the management UI."""

    id: str
    label: Modality
    description: str
    confidence: float
    source: str = "rule"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label.value,
            "description": self.description,
            "confidence": self.confidence,
            "source": self.source,
        }


def normalize_modality(value: str | Modality) -> Modality:
    if isinstance(value, Modality):
        return value
    normalized = value.strip().upper()
    try:
        return Modality(normalized)
    except ValueError as exc:
        supported = ", ".join(item.value for item in Modality)
        raise ValueError(f"Unsupported modality {value!r}; expected one of {supported}") from exc
