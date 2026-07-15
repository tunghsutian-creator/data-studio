"""Durable, local-only Obsidian projection primitives."""

from .outbox import claim_event, complete_event, fail_event, outbox_counts
from .projector import ObsidianProjector, ProjectionConflict, ProjectionSafetyError
from .worker import ProjectionWorker

__all__ = [
    "ObsidianProjector",
    "ProjectionConflict",
    "ProjectionSafetyError",
    "ProjectionWorker",
    "claim_event",
    "complete_event",
    "fail_event",
    "outbox_counts",
]
