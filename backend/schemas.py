from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ScanRequest(BaseModel):
    source: Literal["reference", "inbox"]


class DatasetUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    canonical_name: str | None = Field(default=None, max_length=240)
    workstream: str | None = Field(default=None, max_length=120)
    material_state: str | None = Field(default=None, max_length=120)
    modality: str | None = Field(default=None, max_length=80)
    data_level: str | None = Field(default=None, max_length=80)
    sample_code: str | None = Field(default=None, max_length=120)
    experiment_date: str | None = Field(default=None, max_length=40)
    note: str | None = Field(default=None, max_length=2000)


class DecisionRequest(BaseModel):
    note: str | None = Field(default=None, max_length=2000)


class ConfigUpdate(BaseModel):
    model_config = ConfigDict(extra="ignore")

    host: str | None = None
    port: int | None = None
    reference_root: str | None = None
    inbox_root: str | None = None
    vault_root: str | None = None
    quarantine_root: str | None = None
    catalog_path: str | None = None
    model_path: str | None = None
    ai_enabled: bool | None = None
    ai_profile_path: str | None = None
    ai_model_lock_path: str | None = None
    ai_worker_poll_seconds: float | None = None
    ai_provider_timeout_seconds: float | None = None
    ai_lease_seconds: int | None = None
    ai_base_retry_delay_seconds: int | None = None
    ai_auto_inbox_enabled: bool | None = None
    ai_trigger_confidence_threshold: float | None = None
    export_root: str | None = None
    backup_root: str | None = None
    auto_scan_seconds: float | None = None
    stable_file_seconds: int | None = None
    auto_accept_enabled: bool | None = None
    autoAcceptEnabled: bool | None = None
    # Legacy fields are accepted only so the API can return an explicit error
    # instead of pretending that confidence-based automatic acceptance works.
    auto_accept_threshold: float | None = None
    copy_on_accept: bool | None = None
    referencePath: str | None = None
    inboxPath: str | None = None
    vaultPath: str | None = None
    catalogPath: str | None = None
    exportPath: str | None = None
    backupPath: str | None = None
    retainSource: bool | None = None
    verifySha256: bool | None = None
    autoScan: bool | None = None
    scanInterval: str | float | None = None
    model: str | None = None
    aiEnabled: bool | None = None
    aiProfilePath: str | None = None
    aiModelLockPath: str | None = None
    aiAutoInboxEnabled: bool | None = None
    aiTriggerConfidenceThreshold: float | None = None
    confidenceThreshold: float | None = None


class AIAnalyzeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(default="MANUAL_REQUEST", min_length=1, max_length=200)
    priority: int = Field(default=100, ge=-1000, le=1000)
    max_attempts: int = Field(default=2, ge=1, le=10)


class RuleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    pattern: str = Field(min_length=1, max_length=500)
    label: str = Field(min_length=1, max_length=80)
    priority: int = 100
    enabled: bool = True


class RuleUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    pattern: str | None = Field(default=None, min_length=1, max_length=500)
    label: str | None = Field(default=None, min_length=1, max_length=80)
    priority: int | None = None
    enabled: bool | None = None


class CollectionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    purpose: str | None = Field(default=None, max_length=2000)


class CollectionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    purpose: str | None = Field(default=None, max_length=2000)


class CollectionItemsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_ids: list[str] = Field(min_length=1, max_length=10_000)


class ExportSelectionFilter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    search: str | None = Field(default=None, max_length=500)
    workstream: str | None = Field(default=None, max_length=120)
    material_state: str | None = Field(default=None, max_length=120)
    modality: str | None = Field(default=None, max_length=80)
    status: str | None = Field(default=None, max_length=80)
    extension: str | None = Field(default=None, max_length=40)
    date_from: str | None = Field(default=None, max_length=40)
    date_to: str | None = Field(default=None, max_length=40)


class ExportPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_ids: list[str] = Field(default_factory=list, max_length=10_000)
    dataset_ids: list[str] = Field(default_factory=list, max_length=10_000)
    filter: ExportSelectionFilter | None = None
    excluded_asset_ids: list[str] = Field(default_factory=list, max_length=10_000)

    @model_validator(mode="after")
    def exactly_one_selector(self) -> "ExportPreviewRequest":
        explicit_ids = bool(self.asset_ids or self.dataset_ids)
        if int(explicit_ids) + int(self.filter is not None) != 1:
            raise ValueError("provide explicit asset/dataset ids or one filter, but not both")
        if self.excluded_asset_ids and self.filter is None:
            raise ValueError("excluded_asset_ids may only be used with filter selection")
        return self


class ExportCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selection_token: str = Field(min_length=32, max_length=200)
    name: str = Field(min_length=1, max_length=200)
    purpose: str | None = Field(default=None, max_length=2000)
    collection_id: str | None = Field(default=None, max_length=200)
    export_mode: Literal["FOLDER", "ZIP64", "MANIFEST_ONLY"] = "FOLDER"
    duplicate_policy: Literal["PRESERVE", "DEDUPLICATE"] = "PRESERVE"
