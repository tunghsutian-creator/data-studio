from __future__ import annotations

import pytest

from backend.ai.triggers import AUTO_INBOX_PRIORITY, evaluate_inbox_ai_trigger
from backend.config import Settings


def _dataset(**changes):
    item = {
        "source_kind": "inbox",
        "status": "REVIEW",
        "modality": "TENSILE",
        "confidence": 0.97,
        "conflict": False,
        "assets": [{"extension": ".csv", "mime_type": "text/csv", "role": "MEASUREMENT"}],
    }
    item.update(changes)
    return item


def test_trigger_is_limited_to_unaccepted_inbox_review_items() -> None:
    assert evaluate_inbox_ai_trigger(_dataset()).eligible is False
    assert evaluate_inbox_ai_trigger(_dataset(source_kind="reference", modality="UNKNOWN")).eligible is False
    assert evaluate_inbox_ai_trigger(_dataset(status="ACCEPTED", modality="UNKNOWN")).eligible is False


def test_trigger_records_all_conservative_reasons_in_stable_order() -> None:
    decision = evaluate_inbox_ai_trigger(
        _dataset(
            modality="UNKNOWN",
            confidence=0.2,
            conflict=True,
            assets=[{"extension": ".tiff", "mime_type": "image/tiff", "role": "IMAGE"}],
        )
    )

    assert decision.eligible is True
    assert decision.reasons == ("CONFLICT", "UNKNOWN", "LOW_CONFIDENCE", "PIXEL_EVIDENCE")
    assert decision.reason == "AUTO_INBOX:CONFLICT+UNKNOWN+LOW_CONFIDENCE+PIXEL_EVIDENCE"
    assert decision.priority == AUTO_INBOX_PRIORITY


def test_trigger_threshold_is_explicit_and_validated() -> None:
    assert evaluate_inbox_ai_trigger(_dataset(confidence=0.79), confidence_threshold=0.8).eligible
    assert not evaluate_inbox_ai_trigger(_dataset(confidence=0.8), confidence_threshold=0.8).eligible
    with pytest.raises(ValueError, match="between 0 and 1"):
        evaluate_inbox_ai_trigger(_dataset(), confidence_threshold=1.1)
    with pytest.raises(ValueError, match="ai_trigger_confidence_threshold"):
        Settings(ai_trigger_confidence_threshold=-0.1)
