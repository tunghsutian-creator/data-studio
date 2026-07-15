from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..taxonomy import Modality


AI_OUTPUT_SCHEMA_VERSION = "1.0"
PROMPT_VERSION = "phase1-feasibility-v1"


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal[
        "parsed_metadata",
        "file_header",
        "image_text",
        "visual_pattern",
        "instrument_metadata",
        "abstention",
    ]
    value: str = Field(min_length=1, max_length=600)


class AIClassification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    modality: Modality
    workstream: str = Field(min_length=1, max_length=120)
    sample_id: str | None = Field(default=None, max_length=120)
    material: str | None = Field(default=None, max_length=120)
    test_method: str | None = Field(default=None, max_length=120)
    conditions: dict[str, str | int | float | bool | None] = Field(default_factory=dict)
    proposed_name: str | None = Field(default=None, max_length=240)
    confidence: float = Field(ge=0, le=1)
    evidence: list[Evidence] = Field(default_factory=list, max_length=20)
    needs_review: bool
    abstain_reason: str | None = Field(default=None, max_length=600)

    @model_validator(mode="after")
    def validate_evidence_and_abstention(self) -> "AIClassification":
        if self.modality == Modality.UNKNOWN:
            if not self.needs_review or not self.abstain_reason:
                raise ValueError("UNKNOWN output must require review and include abstain_reason")
        elif not self.evidence:
            raise ValueError("non-UNKNOWN output must cite at least one evidence item")
        return self


_CODE_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def parse_classification_response(content: str) -> AIClassification:
    text = str(content).strip()
    if text.startswith("```"):
        text = _CODE_FENCE.sub("", text).strip()
    payload: Any = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("model response must be one JSON object")
    return AIClassification.model_validate(payload)


def output_json_schema() -> dict[str, Any]:
    schema = AIClassification.model_json_schema()
    schema["$id"] = f"academic-vault://ai-output/{AI_OUTPUT_SCHEMA_VERSION}"
    return schema


__all__ = [
    "AI_OUTPUT_SCHEMA_VERSION",
    "PROMPT_VERSION",
    "AIClassification",
    "Evidence",
    "output_json_schema",
    "parse_classification_response",
]
