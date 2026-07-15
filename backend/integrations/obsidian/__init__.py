"""Durable, local-only Obsidian projection primitives."""

from .outbox import claim_event, complete_event, fail_event, outbox_counts
from .projector import ObsidianProjector, ProjectionConflict, ProjectionSafetyError

__all__ = [
    "ObsidianProjector",
    "ProjectionConflict",
    "ProjectionSafetyError",
    "claim_event",
    "complete_event",
    "fail_event",
    "outbox_counts",
]
