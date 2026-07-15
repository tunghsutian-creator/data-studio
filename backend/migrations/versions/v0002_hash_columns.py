from __future__ import annotations

import sqlite3


VERSION = 2
NAME = "separate source and managed hashes"


def apply(connection: sqlite3.Connection, _context) -> None:
    columns = {row[1] for row in connection.execute("PRAGMA table_info(assets)")}
    if "source_sha256" not in columns:
        connection.execute("ALTER TABLE assets ADD COLUMN source_sha256 TEXT")
    if "managed_sha256" not in columns:
        connection.execute("ALTER TABLE assets ADD COLUMN managed_sha256 TEXT")
    connection.execute("UPDATE assets SET source_sha256=COALESCE(source_sha256,sha256)")
    connection.execute(
        """
        UPDATE assets
        SET managed_sha256=COALESCE(managed_sha256,sha256)
        WHERE managed_path IS NOT NULL AND hash_state='VERIFIED'
        """
    )


class _Migration:
    version = VERSION
    name = NAME
    apply = staticmethod(apply)


migration = _Migration()
