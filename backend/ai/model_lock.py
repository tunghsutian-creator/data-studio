from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class ArtifactLock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository: str = Field(min_length=1)
    revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    license: str = Field(min_length=1)
    filename: str = Field(min_length=1)
    bytes: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class RuntimeArtifactLock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filename: str = Field(min_length=1)
    url: str = Field(pattern=r"^https://")
    bytes: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class RuntimeLock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository: str = Field(pattern=r"^https://")
    commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    release: str | None
    build: str = Field(min_length=1)
    artifacts: list[RuntimeArtifactLock] = Field(min_length=1)
    launch_arguments: list[str] = Field(min_length=1)


class ContractLock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt_version: str = Field(min_length=1)
    taxonomy_version: str = Field(min_length=1)
    output_schema_version: str = Field(min_length=1)


class WindowsModelLock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lock_schema_version: int = Field(ge=1)
    profile_id: str = Field(min_length=1)
    model: ArtifactLock
    vision_projector: ArtifactLock
    runtime: RuntimeLock
    contracts: ContractLock

    def public_summary(self) -> dict[str, str | int | None]:
        return {
            "profile_id": self.profile_id,
            "model_repository": self.model.repository,
            "model_revision": self.model.revision,
            "model_sha256": self.model.sha256,
            "vision_projector_sha256": self.vision_projector.sha256,
            "runtime_release": self.runtime.release,
            "runtime_commit": self.runtime.commit,
        }


def load_model_lock(path: str | Path) -> WindowsModelLock:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return WindowsModelLock.model_validate(payload)


__all__ = ["WindowsModelLock", "load_model_lock"]
