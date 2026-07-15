from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


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
    confidenceThreshold: float | None = None


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
