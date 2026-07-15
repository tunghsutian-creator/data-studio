from __future__ import annotations

import uuid
from typing import Any

from ...database import Database
from .outbox import claim_event, complete_event, fail_event
from .projector import ObsidianProjector


class ProjectionWorker:
    """Run one durable projection attempt without owning a background thread."""

    def __init__(
        self,
        database: Database,
        projector: ObsidianProjector,
        *,
        worker_id: str | None = None,
        lease_seconds: int = 120,
        base_retry_delay_seconds: int = 5,
    ) -> None:
        self.database = database
        self.projector = projector
        self.worker_id = str(worker_id or f"obsidian-projector-{uuid.uuid4()}")
        self.lease_seconds = int(lease_seconds)
        self.base_retry_delay_seconds = int(base_retry_delay_seconds)
        if not 5 <= self.lease_seconds <= 3600:
            raise ValueError("lease_seconds must be between 5 and 3600")
        if not 0 <= self.base_retry_delay_seconds <= 3600:
            raise ValueError("base_retry_delay_seconds must be between 0 and 3600")

    def run_once(self) -> dict[str, Any] | None:
        event = claim_event(
            self.database,
            self.worker_id,
            lease_seconds=self.lease_seconds,
        )
        if event is None:
            return None
        try:
            projection = self.projector.project_event(event)
        except Exception as exc:
            attempt = int(event.get("attempts") or 0)
            delay = min(3600, self.base_retry_delay_seconds * (2 ** min(attempt, 10)))
            if not fail_event(
                self.database,
                int(event["seq"]),
                self.worker_id,
                str(exc),
                retry_delay_seconds=delay,
            ):
                raise RuntimeError("projection lease was lost while recording failure") from exc
            return {
                "status": "RETRY_WAIT",
                "seq": int(event["seq"]),
                "aggregate_id": str(event["aggregate_id"]),
                "attempt": attempt + 1,
                "retry_delay_seconds": delay,
                "error_type": type(exc).__name__,
            }
        if not complete_event(self.database, int(event["seq"]), self.worker_id):
            raise RuntimeError("projection lease was lost before completion")
        return {
            "status": "COMPLETED",
            "seq": int(event["seq"]),
            "aggregate_id": str(event["aggregate_id"]),
            "projection": projection,
        }


__all__ = ["ProjectionWorker"]
