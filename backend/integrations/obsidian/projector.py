from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from ...database import Database


MANAGED_START = "<!-- academic-vault:managed:start -->"
MANAGED_END = "<!-- academic-vault:managed:end -->"
DEFAULT_USER_SECTION = "\n\n## Research notes\n\n"
MAX_NOTE_BYTES = 16 * 1024 * 1024


class ProjectionConflict(RuntimeError):
    """Raised when a projection would overwrite user or unexpected content."""


class ProjectionSafetyError(ValueError):
    """Raised when a Vault or note path crosses a filesystem safety boundary."""


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _managed_hash(text: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return _sha256_bytes(normalized.encode("utf-8"))


def _is_within(child: Path, parent: Path) -> bool:
    try:
        return os.path.commonpath(
            (os.path.normcase(str(child)), os.path.normcase(str(parent)))
        ) == os.path.normcase(str(parent))
    except ValueError:
        return False


def _is_link(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction and is_junction())


def _yaml_string(value: Any) -> str:
    return json.dumps(str(value or ""), ensure_ascii=False)


def _markdown_text(value: Any) -> str:
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value or "")).strip()
    text = text.replace("\\", "\\\\").replace("`", "\\`").replace("|", "\\|")
    for character in "[]()!<>":
        text = text.replace(character, "\\" + character)
    return text


def _slug(value: Any) -> str:
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "-", str(value or "dataset"))
    text = re.sub(r"\s+", "-", text).strip(" .-")
    text = re.sub(r"-+", "-", text)
    return (text or "dataset")[:60].rstrip(" .-") or "dataset"


def _note_relpath(dataset: Mapping[str, Any]) -> str:
    digest = hashlib.sha256(str(dataset["id"]).encode("utf-8")).hexdigest()[:10]
    return PurePosixPath(
        "Academic Vault",
        "Datasets",
        f"{_slug(dataset.get('canonical_name') or dataset.get('name'))}--{digest}.md",
    ).as_posix()


def _extract_managed(text: str) -> tuple[str, str]:
    end = text.find(MANAGED_END)
    if end < 0 or MANAGED_START not in text[:end]:
        raise ProjectionConflict("linked note has no intact Academic Vault managed block")
    end += len(MANAGED_END)
    return text[:end], text[end:]


def _render_dataset_prefix(dataset: Mapping[str, Any]) -> str:
    assets = list(dataset.get("assets") or [])
    title = str(dataset.get("canonical_name") or dataset.get("name") or dataset["id"])
    modality = str(dataset.get("modality") or "UNKNOWN")
    status = str(dataset.get("status") or "REVIEW")
    total_bytes = sum(int(asset.get("size_bytes") or 0) for asset in assets)
    tag_modality = re.sub(r"[^a-z0-9_-]+", "-", modality.lower()).strip("-") or "unknown"
    lines = [
        "---",
        f"av_id: {_yaml_string(dataset['id'])}",
        f"av_library_id: {_yaml_string(dataset['library_id'])}",
        f"av_revision: {int(dataset['revision'])}",
        "av_kind: dataset",
        f"title: {_yaml_string(title)}",
        f"status: {_yaml_string(status.lower())}",
        f"modality: {_yaml_string(modality)}",
        f"workstream: {_yaml_string(dataset.get('workstream') or 'UNKNOWN')}",
        f"sample: {_yaml_string(dataset.get('sample_code') or '')}",
        f"confidence: {float(dataset.get('confidence') or 0):.6f}",
        f"file_count: {len(assets)}",
        f"size_bytes: {total_bytes}",
        "tags:",
        "  - academic-vault/dataset",
        f"  - modality/{tag_modality}",
        "---",
        "",
        f"# {_markdown_text(title)}",
        "",
        MANAGED_START,
        "## Catalog snapshot",
        "",
        f"- Status: `{_markdown_text(status)}`",
        f"- Modality: `{_markdown_text(modality)}`",
        f"- Workstream: `{_markdown_text(dataset.get('workstream') or 'UNKNOWN')}`",
        f"- Sample: `{_markdown_text(dataset.get('sample_code') or '未识别')}`",
        "",
        "## 文件清单",
        "",
    ]
    if assets:
        for asset in assets:
            digest = str(asset.get("sha256") or asset.get("source_sha256") or "unverified")
            lines.append(
                f"- {_markdown_text(asset.get('original_name') or '未命名文件')} · "
                f"{int(asset.get('size_bytes') or 0)} B · SHA-256 `{_markdown_text(digest)}`"
            )
    else:
        lines.append("- 无文件记录")
    lines.extend(("", MANAGED_END))
    return "\n".join(lines)


def _render_tombstone_prefix(prefix: str, revision: int) -> str:
    lines = prefix.splitlines()
    for index, line in enumerate(lines):
        if line.startswith("av_revision:"):
            lines[index] = f"av_revision: {int(revision)}"
            break
    first_close = next((index for index in range(1, len(lines)) if lines[index] == "---"), None)
    if first_close is None:
        raise ProjectionConflict("linked note frontmatter is not intact")
    if "av_tombstoned: true" not in lines[:first_close]:
        lines[first_close:first_close] = ["av_tombstoned: true", "archived: true"]
    end = lines.index(MANAGED_END)
    notice = "> Catalog record tombstoned; user-authored Research notes are preserved."
    if notice not in lines[:end]:
        lines[end:end] = ["", notice, ""]
    return "\n".join(lines)


class ObsidianProjector:
    def __init__(self, database: Database, vault_root: str | Path, *, vault_id: str) -> None:
        requested = Path(vault_root).expanduser()
        if requested.exists() and _is_link(requested):
            raise ProjectionSafetyError("Obsidian Vault root may not be a symlink or junction")
        self.vault_root = requested.resolve(strict=False)
        self.database = database
        self.vault_id = str(vault_id or "").strip()
        if not 1 <= len(self.vault_id) <= 200:
            raise ValueError("vault_id must contain between 1 and 200 characters")
        for root in database.root_mapper.roots.values():
            resolved = Path(root).resolve(strict=False)
            if _is_within(self.vault_root, resolved) or _is_within(resolved, self.vault_root):
                raise ProjectionSafetyError("Obsidian Vault must not overlap a configured data root")

    def _link(self, aggregate_id: str) -> dict[str, Any] | None:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM obsidian_links
                WHERE library_id=? AND aggregate_type='DATASET' AND aggregate_id=?
                """,
                (self.database.library_id(connection), aggregate_id),
            ).fetchone()
        return dict(row) if row else None

    def _target(self, relpath: str) -> Path:
        relative = PurePosixPath(str(relpath).replace("\\", "/"))
        if relative.is_absolute() or not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
            raise ProjectionSafetyError("note path must be a normalized relative path")
        unresolved = self.vault_root.joinpath(*relative.parts)
        current = self.vault_root
        for part in relative.parts[:-1]:
            current = current / part
            if current.exists() and _is_link(current):
                raise ProjectionSafetyError("note parent may not contain a symlink or junction")
        target = unresolved.resolve(strict=False)
        if not _is_within(target, self.vault_root):
            raise ProjectionSafetyError("note path escapes the Obsidian Vault")
        return target

    def _ensure_parent(self, target: Path) -> None:
        self.vault_root.mkdir(parents=True, exist_ok=True)
        relative_parent = target.parent.relative_to(self.vault_root)
        current = self.vault_root
        for part in relative_parent.parts:
            current = current / part
            if current.exists() and _is_link(current):
                raise ProjectionSafetyError("note parent may not contain a symlink or junction")
            current.mkdir(exist_ok=True)

    def _read_note(self, path: Path) -> str:
        if _is_link(path):
            raise ProjectionSafetyError("note may not be a symlink or junction")
        if path.stat().st_size > MAX_NOTE_BYTES:
            raise ProjectionConflict("linked note exceeds the safe projection size")
        return path.read_bytes().decode("utf-8")

    def _find_duplicate(self, aggregate_id: str, target: Path) -> Path | None:
        if not self.vault_root.exists():
            return None
        needle = f"av_id: {_yaml_string(aggregate_id)}"
        for directory, names, files in os.walk(self.vault_root, followlinks=False):
            base = Path(directory)
            names[:] = [name for name in names if not _is_link(base / name)]
            for name in files:
                candidate = base / name
                if candidate == target or candidate.suffix.lower() != ".md" or _is_link(candidate):
                    continue
                if candidate.stat().st_size > MAX_NOTE_BYTES:
                    continue
                with candidate.open("r", encoding="utf-8") as stream:
                    head = stream.read(65536)
                if needle in head:
                    return candidate
        return None

    def _store_link(
        self,
        aggregate_id: str,
        relpath: str,
        *,
        revision: int,
        state: str,
        note_hash: str | None = None,
        managed_hash: str | None = None,
        error: str | None = None,
    ) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO obsidian_links(
                    library_id,aggregate_type,aggregate_id,vault_id,note_relpath,
                    last_aggregate_revision,last_note_hash,last_managed_hash,
                    sync_state,last_synced_at,last_error
                ) VALUES(
                    ?, 'DATASET', ?, ?, ?, ?, ?, ?, ?,
                    CASE WHEN ? IN ('SYNCED','TOMBSTONED') THEN CURRENT_TIMESTAMP ELSE NULL END,
                    ?
                )
                ON CONFLICT(library_id,aggregate_type,aggregate_id) DO UPDATE SET
                    vault_id=excluded.vault_id,
                    note_relpath=excluded.note_relpath,
                    last_aggregate_revision=excluded.last_aggregate_revision,
                    last_note_hash=COALESCE(excluded.last_note_hash,obsidian_links.last_note_hash),
                    last_managed_hash=COALESCE(excluded.last_managed_hash,obsidian_links.last_managed_hash),
                    sync_state=excluded.sync_state,
                    last_synced_at=COALESCE(excluded.last_synced_at,obsidian_links.last_synced_at),
                    last_error=excluded.last_error
                """,
                (
                    self.database.library_id(connection),
                    aggregate_id,
                    self.vault_id,
                    relpath,
                    max(0, int(revision)),
                    note_hash,
                    managed_hash,
                    state,
                    state,
                    str(error or "")[:1000] or None,
                ),
            )

    def _conflict(self, aggregate_id: str, relpath: str, revision: int, message: str) -> None:
        self._store_link(aggregate_id, relpath, revision=max(0, revision - 1), state="CONFLICT", error=message)
        raise ProjectionConflict(message)

    def _atomic_write(self, target: Path, content: str, expected_current_hash: str | None) -> None:
        self._ensure_parent(target)
        payload = content.encode("utf-8")
        if len(payload) > MAX_NOTE_BYTES:
            raise ProjectionSafetyError("generated note exceeds the safe projection size")
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            if expected_current_hash is not None:
                if not target.is_file() or _sha256_bytes(target.read_bytes()) != expected_current_hash:
                    raise ProjectionConflict("note changed while the projection was being prepared")
            elif target.exists():
                raise ProjectionConflict("refusing to overwrite an unexpected note")
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    def _project_upsert(self, event: Mapping[str, Any]) -> dict[str, Any]:
        aggregate_id = str(event["aggregate_id"])
        revision = int(event["aggregate_revision"])
        dataset = self.database.get_dataset(aggregate_id)
        if dataset is None:
            raise ProjectionConflict("dataset no longer exists for an UPSERT projection")
        current_revision = int(dataset["revision"])
        if current_revision > revision:
            return {"status": "SUPERSEDED", "aggregate_id": aggregate_id, "revision": revision}
        if current_revision != revision:
            raise ProjectionConflict("outbox revision is newer than the catalog record")

        desired_prefix = _render_dataset_prefix(dataset)
        desired_managed_hash = _managed_hash(desired_prefix)
        link = self._link(aggregate_id)
        relpath = str(link["note_relpath"]) if link else _note_relpath(dataset)
        target = self._target(relpath)
        duplicate = self._find_duplicate(aggregate_id, target)
        if duplicate is not None:
            self._conflict(aggregate_id, relpath, revision, "duplicate av_id exists in another note")

        suffix = DEFAULT_USER_SECTION
        expected_current_hash = None
        if link:
            if str(link["vault_id"]) != self.vault_id:
                self._conflict(aggregate_id, relpath, revision, "dataset is linked to a different Vault identity")
            if target.exists():
                existing = self._read_note(target)
                existing_prefix, suffix = _extract_managed(existing)
                actual_managed_hash = _managed_hash(existing_prefix)
                expected_current_hash = _sha256_bytes(target.read_bytes())
                stored_managed_hash = link.get("last_managed_hash")
                if stored_managed_hash and actual_managed_hash != stored_managed_hash:
                    self._conflict(aggregate_id, relpath, revision, "database-owned note content was modified")
                if not stored_managed_hash and not (
                    str(link.get("sync_state")) == "PENDING" and actual_managed_hash == desired_managed_hash
                ):
                    self._conflict(aggregate_id, relpath, revision, "linked note has no trusted managed hash")
            elif link.get("last_note_hash"):
                self._conflict(aggregate_id, relpath, revision, "linked note is missing")
        else:
            if target.exists():
                self._conflict(aggregate_id, relpath, revision, "refusing to overwrite an unlinked note")
            self._store_link(aggregate_id, relpath, revision=0, state="PENDING")

        content = desired_prefix + suffix
        self._atomic_write(target, content, expected_current_hash)
        note_hash = _sha256_bytes(content.encode("utf-8"))
        self._store_link(
            aggregate_id,
            relpath,
            revision=revision,
            state="SYNCED",
            note_hash=note_hash,
            managed_hash=desired_managed_hash,
        )
        return {"status": "SYNCED", "aggregate_id": aggregate_id, "revision": revision, "note_relpath": relpath}

    def _project_tombstone(self, event: Mapping[str, Any]) -> dict[str, Any]:
        aggregate_id = str(event["aggregate_id"])
        revision = int(event["aggregate_revision"])
        link = self._link(aggregate_id)
        if link is None:
            return {"status": "TOMBSTONED", "aggregate_id": aggregate_id, "revision": revision, "note_relpath": None}
        relpath = str(link["note_relpath"])
        target = self._target(relpath)
        if not target.is_file():
            self._conflict(aggregate_id, relpath, revision, "linked note is missing during tombstone")
        existing = self._read_note(target)
        prefix, suffix = _extract_managed(existing)
        managed_hash = _managed_hash(prefix)
        if link.get("last_managed_hash") != managed_hash:
            self._conflict(aggregate_id, relpath, revision, "database-owned note content was modified")
        tombstone_prefix = _render_tombstone_prefix(prefix, revision)
        content = tombstone_prefix + suffix
        self._atomic_write(target, content, _sha256_bytes(target.read_bytes()))
        self._store_link(
            aggregate_id,
            relpath,
            revision=revision,
            state="TOMBSTONED",
            note_hash=_sha256_bytes(content.encode("utf-8")),
            managed_hash=_managed_hash(tombstone_prefix),
        )
        return {"status": "TOMBSTONED", "aggregate_id": aggregate_id, "revision": revision, "note_relpath": relpath}

    def project_event(self, event: Mapping[str, Any]) -> dict[str, Any]:
        if str(event.get("library_id")) != self.database.library_id():
            raise ProjectionSafetyError("outbox event belongs to a different library")
        if str(event.get("integration")) != "OBSIDIAN" or str(event.get("aggregate_type")) != "DATASET":
            raise ValueError("unsupported integration aggregate")
        event_type = str(event.get("event_type"))
        if event_type in {"UPSERT", "RECONCILE"}:
            return self._project_upsert(event)
        if event_type == "TOMBSTONE":
            return self._project_tombstone(event)
        raise ValueError("unsupported integration event type")


__all__ = [
    "DEFAULT_USER_SECTION",
    "MANAGED_END",
    "MANAGED_START",
    "ObsidianProjector",
    "ProjectionConflict",
    "ProjectionSafetyError",
]
