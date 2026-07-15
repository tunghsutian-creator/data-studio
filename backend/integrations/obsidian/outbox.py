from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from ...database import Database


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds")


def _clean_worker_id(value: str) -> str:
    worker_id = str(value or "").strip()
    if not 1 <= len(worker_id) <= 200:
        raise ValueError("worker_id must contain between 1 and 200 characters")
    return worker_id


def _public_event(row: Any) -> dict[str, Any]:
    result = {key: row[key] for key in row.keys() if key != "payload_json"}
    result["payload"] = json.loads(str(row["payload_json"]))
    return result


def claim_event(database: Database, worker_id: str, *, lease_seconds: int = 60) -> dict[str, Any] | None:
    owner = _clean_worker_id(worker_id)
    if not 5 <= int(lease_seconds) <= 3600:
        raise ValueError("lease_seconds must be between 5 and 3600")
    now = _now()
    now_text = _timestamp(now)
    lease_expires_at = _timestamp(now + timedelta(seconds=int(lease_seconds)))
    with database.transaction() as connection:
        row = connection.execute(
            """
            SELECT * FROM integration_outbox
            WHERE processed_at IS NULL AND available_at<=?
              AND (lease_expires_at IS NULL OR lease_expires_at<=?)
            ORDER BY seq
            LIMIT 1
            """,
            (now_text, now_text),
        ).fetchone()
        if row is None:
            return None
        cursor = connection.execute(
            """
            UPDATE integration_outbox SET lease_owner=?,lease_expires_at=?
            WHERE seq=? AND processed_at IS NULL
              AND (lease_expires_at IS NULL OR lease_expires_at<=?)
            """,
            (owner, lease_expires_at, row["seq"], now_text),
        )
        if cursor.rowcount != 1:
            return None
        claimed = connection.execute(
            "SELECT * FROM integration_outbox WHERE seq=?",
            (row["seq"],),
        ).fetchone()
    return _public_event(claimed)


def complete_event(database: Database, seq: int, worker_id: str) -> bool:
    owner = _clean_worker_id(worker_id)
    with database.transaction() as connection:
        cursor = connection.execute(
            """
            UPDATE integration_outbox
            SET processed_at=?,lease_owner=NULL,lease_expires_at=NULL,last_error=NULL
            WHERE seq=? AND processed_at IS NULL AND lease_owner=?
            """,
            (_timestamp(_now()), int(seq), owner),
        )
    return cursor.rowcount == 1


def fail_event(
    database: Database,
    seq: int,
    worker_id: str,
    error: str,
    *,
    retry_delay_seconds: int = 5,
) -> bool:
    owner = _clean_worker_id(worker_id)
    if not 0 <= int(retry_delay_seconds) <= 86400:
        raise ValueError("retry_delay_seconds must be between 0 and 86400")
    detail = str(error or "integration failed").strip()[:1000]
    available_at = _timestamp(_now() + timedelta(seconds=int(retry_delay_seconds)))
    with database.transaction() as connection:
        cursor = connection.execute(
            """
            UPDATE integration_outbox
            SET attempts=attempts+1,last_error=?,available_at=?,
                lease_owner=NULL,lease_expires_at=NULL
            WHERE seq=? AND processed_at IS NULL AND lease_owner=?
            """,
            (detail, available_at, int(seq), owner),
        )
    return cursor.rowcount == 1


def outbox_counts(database: Database) -> dict[str, int]:
    now = _timestamp(_now())
    with database.connect() as connection:
        row = connection.execute(
            """
            SELECT
              COUNT(*) AS total,
              SUM(CASE WHEN processed_at IS NOT NULL THEN 1 ELSE 0 END) AS processed,
              SUM(CASE WHEN processed_at IS NULL AND lease_expires_at>? THEN 1 ELSE 0 END) AS leased,
              SUM(CASE WHEN processed_at IS NULL AND available_at<=? AND (lease_expires_at IS NULL OR lease_expires_at<=?) THEN 1 ELSE 0 END) AS pending,
              SUM(CASE WHEN processed_at IS NULL AND available_at>? AND lease_expires_at IS NULL THEN 1 ELSE 0 END) AS delayed
            FROM integration_outbox
            """,
            (now, now, now, now),
        ).fetchone()
    return {key: int(row[key] or 0) for key in ("total", "processed", "leased", "pending", "delayed")}


__all__ = ["claim_event", "complete_event", "fail_event", "outbox_counts"]
