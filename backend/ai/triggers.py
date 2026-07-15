"""Conservative policy for scheduling local AI after an Inbox scan."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence


PIXEL_EXTENSIONS = frozenset({".bmp", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"})
AUTO_INBOX_PRIORITY = 25


@dataclass(frozen=True, slots=True)
class AITriggerDecision:
    eligible: bool
    reasons: tuple[str, ...] = ()
    priority: int = AUTO_INBOX_PRIORITY

    @property
    def reason(self) -> str | None:
        if not self.eligible:
            return None
        return "AUTO_INBOX:" + "+".join(self.reasons)


def evaluate_inbox_ai_trigger(
    dataset: Mapping[str, Any],
    *,
    confidence_threshold: float = 0.8,
) -> AITriggerDecision:
    """Return a scheduling decision without changing catalog state.

    Automatic inference is limited to unaccepted Inbox review items. A model
    suggestion never changes the dataset's review status or metadata.
    """

    if isinstance(confidence_threshold, bool) or not 0 <= confidence_threshold <= 1:
        raise ValueError("confidence_threshold must be between 0 and 1")
    if str(dataset.get("source_kind") or "").lower() != "inbox":
        return AITriggerDecision(False)
    if str(dataset.get("status") or "").upper() != "REVIEW":
        return AITriggerDecision(False)

    reasons: list[str] = []
    if bool(dataset.get("conflict")):
        reasons.append("CONFLICT")
    if str(dataset.get("modality") or "UNKNOWN").upper() == "UNKNOWN":
        reasons.append("UNKNOWN")
    if float(dataset.get("confidence") or 0) < confidence_threshold:
        reasons.append("LOW_CONFIDENCE")

    assets: Sequence[Mapping[str, Any]] = dataset.get("assets") or ()
    has_pixel_evidence = any(
        str(asset.get("extension") or "").lower() in PIXEL_EXTENSIONS
        or str(asset.get("mime_type") or "").lower().startswith("image/")
        or str(asset.get("role") or "").upper() == "IMAGE"
        for asset in assets
    )
    if has_pixel_evidence:
        reasons.append("PIXEL_EVIDENCE")

    return AITriggerDecision(bool(reasons), tuple(reasons))


__all__ = ["AITriggerDecision", "AUTO_INBOX_PRIORITY", "evaluate_inbox_ai_trigger"]
