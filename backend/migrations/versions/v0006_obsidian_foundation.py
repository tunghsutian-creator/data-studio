from __future__ import annotations

import sqlite3


VERSION = 6
NAME = "obsidian outbox and knowledge graph foundation"


def apply(connection: sqlite3.Connection, context) -> None:
    del context
    script = """
        CREATE TABLE integration_outbox (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            library_id TEXT NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
            integration TEXT NOT NULL DEFAULT 'OBSIDIAN'
                CHECK(integration='OBSIDIAN'),
            aggregate_type TEXT NOT NULL
                CHECK(length(trim(aggregate_type)) BETWEEN 1 AND 80),
            aggregate_id TEXT NOT NULL
                CHECK(length(trim(aggregate_id)) BETWEEN 1 AND 200),
            aggregate_revision INTEGER NOT NULL CHECK(aggregate_revision >= 1),
            event_type TEXT NOT NULL
                CHECK(event_type IN ('UPSERT','TOMBSTONE','RECONCILE')),
            payload_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(payload_json)),
            created_at TEXT NOT NULL,
            available_at TEXT NOT NULL,
            processed_at TEXT,
            attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
            last_error TEXT CHECK(last_error IS NULL OR length(last_error) <= 1000),
            lease_owner TEXT CHECK(lease_owner IS NULL OR length(lease_owner) BETWEEN 1 AND 200),
            lease_expires_at TEXT,
            UNIQUE(integration,aggregate_type,aggregate_id,aggregate_revision,event_type),
            CHECK((lease_owner IS NULL)=(lease_expires_at IS NULL))
        );

        CREATE TABLE knowledge_entities (
            id TEXT PRIMARY KEY,
            library_id TEXT NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
            entity_type TEXT NOT NULL
                CHECK(entity_type IN ('PROJECT','SAMPLE','EXPERIMENT','METHOD','PAPER')),
            canonical_key TEXT NOT NULL COLLATE NOCASE
                CHECK(length(trim(canonical_key)) BETWEEN 1 AND 300),
            display_name TEXT NOT NULL
                CHECK(length(trim(display_name)) BETWEEN 1 AND 300),
            aliases_json TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(aliases_json)),
            revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1),
            tombstoned_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(library_id,entity_type,canonical_key)
        );

        CREATE TABLE knowledge_relations (
            id TEXT PRIMARY KEY,
            library_id TEXT NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
            source_type TEXT NOT NULL CHECK(length(trim(source_type)) BETWEEN 1 AND 80),
            source_id TEXT NOT NULL CHECK(length(trim(source_id)) BETWEEN 1 AND 200),
            relation_type TEXT NOT NULL CHECK(length(trim(relation_type)) BETWEEN 1 AND 80),
            target_type TEXT NOT NULL CHECK(length(trim(target_type)) BETWEEN 1 AND 80),
            target_id TEXT NOT NULL CHECK(length(trim(target_id)) BETWEEN 1 AND 200),
            created_at TEXT NOT NULL,
            UNIQUE(library_id,source_type,source_id,relation_type,target_type,target_id)
        );

        CREATE TABLE obsidian_links (
            library_id TEXT NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
            aggregate_type TEXT NOT NULL CHECK(length(trim(aggregate_type)) BETWEEN 1 AND 80),
            aggregate_id TEXT NOT NULL CHECK(length(trim(aggregate_id)) BETWEEN 1 AND 200),
            vault_id TEXT NOT NULL CHECK(length(trim(vault_id)) BETWEEN 1 AND 200),
            note_relpath TEXT NOT NULL COLLATE NOCASE
                CHECK(
                    length(trim(note_relpath)) BETWEEN 1 AND 1000
                    AND substr(note_relpath,1,1)<>'/'
                    AND instr(note_relpath,char(92))=0
                    AND note_relpath<>'..'
                    AND note_relpath NOT LIKE '../%'
                    AND note_relpath NOT LIKE '%/../%'
                    AND note_relpath NOT LIKE '%/..'
                ),
            last_aggregate_revision INTEGER NOT NULL DEFAULT 0 CHECK(last_aggregate_revision >= 0),
            last_note_hash TEXT
                CHECK(last_note_hash IS NULL OR (length(last_note_hash)=64 AND last_note_hash NOT GLOB '*[^0-9a-f]*')),
            last_managed_hash TEXT
                CHECK(last_managed_hash IS NULL OR (length(last_managed_hash)=64 AND last_managed_hash NOT GLOB '*[^0-9a-f]*')),
            sync_state TEXT NOT NULL DEFAULT 'PENDING'
                CHECK(sync_state IN ('PENDING','SYNCED','CONFLICT','ERROR','TOMBSTONED')),
            last_synced_at TEXT,
            last_error TEXT CHECK(last_error IS NULL OR length(last_error) <= 1000),
            PRIMARY KEY(library_id,aggregate_type,aggregate_id),
            UNIQUE(library_id,vault_id,note_relpath)
        );

        CREATE INDEX idx_integration_outbox_claim
            ON integration_outbox(processed_at,available_at,lease_expires_at,seq);
        CREATE INDEX idx_integration_outbox_aggregate
            ON integration_outbox(library_id,aggregate_type,aggregate_id,aggregate_revision);
        CREATE INDEX idx_knowledge_entities_type_name
            ON knowledge_entities(library_id,entity_type,display_name,id);
        CREATE INDEX idx_knowledge_relations_source
            ON knowledge_relations(library_id,source_type,source_id,relation_type);
        CREATE INDEX idx_knowledge_relations_target
            ON knowledge_relations(library_id,target_type,target_id,relation_type);
        CREATE INDEX idx_obsidian_links_state
            ON obsidian_links(library_id,vault_id,sync_state,aggregate_type,aggregate_id);
    """
    for statement in script.split(";"):
        if statement.strip():
            connection.execute(statement)

    connection.execute(
        """
        INSERT OR IGNORE INTO integration_outbox(
            library_id,integration,aggregate_type,aggregate_id,aggregate_revision,
            event_type,payload_json,created_at,available_at
        )
        SELECT library_id,'OBSIDIAN','DATASET',id,revision,'UPSERT','{}',updated_at,updated_at
        FROM datasets
        """
    )
    connection.execute(
        """
        CREATE TRIGGER obsidian_outbox_dataset_insert
        AFTER INSERT ON datasets
        BEGIN
            INSERT OR IGNORE INTO integration_outbox(
                library_id,integration,aggregate_type,aggregate_id,aggregate_revision,
                event_type,payload_json,created_at,available_at
            ) VALUES(
                NEW.library_id,'OBSIDIAN','DATASET',NEW.id,NEW.revision,
                'UPSERT','{}',
                strftime('%Y-%m-%dT%H:%M:%f+00:00','now'),
                strftime('%Y-%m-%dT%H:%M:%f+00:00','now')
            );
        END
        """
    )
    connection.execute(
        """
        CREATE TRIGGER obsidian_outbox_dataset_update
        AFTER UPDATE ON datasets
        WHEN NEW.revision > OLD.revision
        BEGIN
            INSERT OR IGNORE INTO integration_outbox(
                library_id,integration,aggregate_type,aggregate_id,aggregate_revision,
                event_type,payload_json,created_at,available_at
            ) VALUES(
                NEW.library_id,'OBSIDIAN','DATASET',NEW.id,NEW.revision,
                'UPSERT','{}',
                strftime('%Y-%m-%dT%H:%M:%f+00:00','now'),
                strftime('%Y-%m-%dT%H:%M:%f+00:00','now')
            );
        END
        """
    )
    connection.execute(
        """
        CREATE TRIGGER obsidian_outbox_dataset_delete
        AFTER DELETE ON datasets
        BEGIN
            INSERT OR IGNORE INTO integration_outbox(
                library_id,integration,aggregate_type,aggregate_id,aggregate_revision,
                event_type,payload_json,created_at,available_at
            ) VALUES(
                OLD.library_id,'OBSIDIAN','DATASET',OLD.id,OLD.revision+1,
                'TOMBSTONE','{}',
                strftime('%Y-%m-%dT%H:%M:%f+00:00','now'),
                strftime('%Y-%m-%dT%H:%M:%f+00:00','now')
            );
        END
        """
    )


class _Migration:
    version = VERSION
    name = NAME
    apply = staticmethod(apply)


migration = _Migration()
