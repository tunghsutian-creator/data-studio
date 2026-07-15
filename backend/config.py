from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, Iterable

from .paths import RootMapper


def _default_base() -> Path:
    return Path(os.environ.get("ACADEMIC_VAULT_HOME", Path.cwd() / ".academic-vault"))


def _bundled_profile(name: str) -> Path:
    return Path(__file__).resolve().parent.parent / "profiles" / name


def _is_within(child: str | Path, parent: str | Path) -> bool:
    child_text = os.path.normcase(str(Path(child).expanduser().resolve(strict=False)))
    parent_text = os.path.normcase(str(Path(parent).expanduser().resolve(strict=False)))
    try:
        return os.path.commonpath((child_text, parent_text)) == parent_text
    except ValueError:
        return False


@dataclass(frozen=True, slots=True)
class Settings:
    host: str = "127.0.0.1"
    port: int = 8765
    reference_root: Path = _default_base() / "reference"
    inbox_root: Path = _default_base() / "inbox"
    vault_root: Path = _default_base() / "vault"
    quarantine_root: Path = _default_base() / "quarantine"
    catalog_path: Path = _default_base() / "catalog" / "academic_vault.sqlite3"
    model_path: Path = _default_base() / "models" / "modality-classifier.joblib"
    ai_enabled: bool = False
    ai_profile_path: Path = _bundled_profile("windows-rtx5080.json")
    ai_model_lock_path: Path = _bundled_profile("windows-model-lock.json")
    ai_worker_poll_seconds: float = 1.0
    ai_provider_timeout_seconds: float = 120.0
    ai_lease_seconds: int = 180
    ai_base_retry_delay_seconds: int = 5
    ai_auto_inbox_enabled: bool = True
    ai_trigger_confidence_threshold: float = 0.8
    export_root: Path | None = None
    backup_root: Path | None = None
    auto_scan_seconds: float = 30
    stable_file_seconds: int = 5
    auto_accept_enabled: bool = False
    copy_on_accept: bool = True
    machine_profile: str = "windows-default"
    device_id: str | None = None
    config_file: Path | None = None

    def __post_init__(self) -> None:
        data_root = Path(self.catalog_path).expanduser().resolve(strict=False).parent.parent
        if self.export_root is None:
            object.__setattr__(self, "export_root", data_root / "exports")
        if self.backup_root is None:
            object.__setattr__(self, "backup_root", data_root / "backups")
        if self.host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("Academic Vault may only bind to a loopback address")
        if not (1 <= int(self.port) <= 65535):
            raise ValueError("port must be between 1 and 65535")
        if self.stable_file_seconds < 0 or self.auto_scan_seconds < 0:
            raise ValueError("scan intervals cannot be negative")
        if not 0.1 <= self.ai_worker_poll_seconds <= 60:
            raise ValueError("ai_worker_poll_seconds must be between 0.1 and 60")
        if not 1 <= self.ai_provider_timeout_seconds <= 600:
            raise ValueError("ai_provider_timeout_seconds must be between 1 and 600")
        if not 5 <= self.ai_lease_seconds <= 3600:
            raise ValueError("ai_lease_seconds must be between 5 and 3600")
        if not 0 <= self.ai_base_retry_delay_seconds <= 3600:
            raise ValueError("ai_base_retry_delay_seconds must be between 0 and 3600")
        if isinstance(self.ai_trigger_confidence_threshold, bool) or not 0 <= self.ai_trigger_confidence_threshold <= 1:
            raise ValueError("ai_trigger_confidence_threshold must be between 0 and 1")
        if Path(self.ai_profile_path).resolve(strict=False) == Path(self.ai_model_lock_path).resolve(strict=False):
            raise ValueError("ai_profile_path and ai_model_lock_path must be different files")
        if self.ai_enabled:
            if not Path(self.ai_profile_path).is_file():
                raise ValueError("enabled local AI profile does not exist")
            if not Path(self.ai_model_lock_path).is_file():
                raise ValueError("enabled local AI model lock does not exist")
        if self.auto_accept_enabled:
            raise ValueError("automatic acceptance is disabled; every Inbox dataset requires human review")
        roots = {
            "reference_root": self.reference_root,
            "inbox_root": self.inbox_root,
            "vault_root": self.vault_root,
            "quarantine_root": self.quarantine_root,
            "export_root": self.export_root,
            "backup_root": self.backup_root,
        }
        names = list(roots)
        for index, left_name in enumerate(names):
            for right_name in names[index + 1 :]:
                left, right = roots[left_name], roots[right_name]
                if _is_within(left, right) or _is_within(right, left):
                    raise ValueError(f"configured roots must not overlap: {left_name} and {right_name}")
        if Path(self.catalog_path).resolve(strict=False) == Path(self.model_path).resolve(strict=False):
            raise ValueError("catalog_path and model_path must be different files")
        for root_name, root in roots.items():
            if _is_within(self.catalog_path, root) or _is_within(self.model_path, root):
                raise ValueError(f"catalog/model files cannot live inside {root_name}")

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Settings":
        raw_path = path or os.environ.get("ACADEMIC_VAULT_CONFIG")
        if not raw_path:
            return cls()
        config_path = Path(raw_path).expanduser().resolve(strict=False)
        payload = json.loads(config_path.read_text(encoding="utf-8"))
        payload["config_file"] = config_path
        return cls.from_mapping(payload, base=config_path.parent)

    @classmethod
    def from_mapping(cls, payload: dict[str, Any], *, base: Path | None = None) -> "Settings":
        known = {item.name for item in fields(cls)}
        path_names = {
            "reference_root",
            "inbox_root",
            "vault_root",
            "quarantine_root",
            "catalog_path",
            "model_path",
            "ai_profile_path",
            "ai_model_lock_path",
            "export_root",
            "backup_root",
            "config_file",
        }
        data: dict[str, Any] = {}
        for key, value in payload.items():
            if key not in known or value is None:
                continue
            if key in path_names:
                candidate = Path(value).expanduser()
                if base and not candidate.is_absolute():
                    candidate = base / candidate
                value = candidate.resolve(strict=False)
            data[key] = value
        return cls(**data)

    def updated(self, payload: dict[str, Any]) -> "Settings":
        merged = asdict(self)
        merged.update({key: value for key, value in payload.items() if value is not None})
        return self.from_mapping(merged, base=self.config_file.parent if self.config_file else None)

    def ensure_runtime_directories(self) -> None:
        for path in (
            self.inbox_root,
            self.vault_root,
            self.quarantine_root,
            self.export_root,
            self.backup_root,
        ):
            Path(path).mkdir(parents=True, exist_ok=True)
        Path(self.catalog_path).parent.mkdir(parents=True, exist_ok=True)
        Path(self.model_path).parent.mkdir(parents=True, exist_ok=True)

    def public_dict(self) -> dict[str, Any]:
        data = asdict(self)
        public = {key: str(value) if isinstance(value, Path) else value for key, value in data.items() if key != "config_file"}
        # UI-friendly aliases keep the JSON contract pleasant while the
        # snake_case keys remain available to scripts.
        public.update(
            {
                "referencePath": str(self.reference_root),
                "inboxPath": str(self.inbox_root),
                "vaultPath": str(self.vault_root),
                "catalogPath": str(self.catalog_path),
                "exportPath": str(self.export_root),
                "backupPath": str(self.backup_root),
                "retainSource": True,
                "verifySha256": True,
                "autoScan": self.auto_scan_seconds > 0,
                "scanInterval": self.auto_scan_seconds,
                "model": "local-lightweight-v1" if Path(self.model_path).is_file() else "rules-only",
                "aiEnabled": self.ai_enabled,
                "aiProfilePath": str(self.ai_profile_path),
                "aiModelLockPath": str(self.ai_model_lock_path),
                "aiAutoInboxEnabled": self.ai_auto_inbox_enabled,
                "aiTriggerConfidenceThreshold": self.ai_trigger_confidence_threshold,
                "autoAcceptEnabled": False,
                "reviewPolicy": "manual",
            }
        )
        return public

    def save(self) -> None:
        if not self.config_file:
            return
        destination = self.config_file.resolve(strict=False)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.tmp")
        temporary.write_text(json.dumps(self.public_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, destination)

    def source_root(self, source: str) -> Path:
        if source == "reference":
            return Path(self.reference_root)
        if source == "inbox":
            return Path(self.inbox_root)
        raise ValueError("source must be 'reference' or 'inbox'")

    def root_mappings(self) -> dict[str, Path]:
        return {
            "reference": Path(self.reference_root),
            "inbox": Path(self.inbox_root),
            "vault": Path(self.vault_root),
            "quarantine": Path(self.quarantine_root),
            "exports": Path(self.export_root),
        }

    def root_mapper(self) -> RootMapper:
        return RootMapper(self.root_mappings())

    def assert_within(self, path: str | Path, roots: Iterable[str | Path], *, must_exist: bool = False) -> Path:
        candidate = Path(path).expanduser().resolve(strict=must_exist)
        for root in roots:
            resolved_root = Path(root).expanduser().resolve(strict=False)
            try:
                if os.path.commonpath([os.path.normcase(str(candidate)), os.path.normcase(str(resolved_root))]) == os.path.normcase(str(resolved_root)):
                    return candidate
            except ValueError:
                continue
        raise ValueError(f"path is outside configured roots: {candidate}")

    def assert_source_path(self, path: str | Path, source: str, *, must_exist: bool = True) -> Path:
        return self.assert_within(path, (self.source_root(source),), must_exist=must_exist)

    def assert_vault_path(self, path: str | Path, *, must_exist: bool = False) -> Path:
        return self.assert_within(path, (self.vault_root,), must_exist=must_exist)


def load_settings(path: str | Path | None = None) -> Settings:
    return Settings.load(path)
