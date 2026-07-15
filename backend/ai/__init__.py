"""Local-only AI contracts, providers, and feasibility tooling."""

from .contracts import AIClassification, Evidence
from .model_lock import WindowsModelLock, load_model_lock
from .provider import (
    AnalysisRequest,
    FakeLocalModelProvider,
    LlamaCppProvider,
    LocalModelProfile,
    LocalModelProvider,
    ProviderAnalysisResult,
    ProviderError,
    ProviderHealth,
    ProviderIdentity,
    ProviderInvalidResponse,
    ProviderRequestError,
    ProviderTimeout,
    ProviderUnavailable,
)

__all__ = [
    "AIClassification",
    "AnalysisRequest",
    "Evidence",
    "FakeLocalModelProvider",
    "LlamaCppProvider",
    "LocalModelProfile",
    "LocalModelProvider",
    "ProviderAnalysisResult",
    "ProviderError",
    "ProviderHealth",
    "ProviderIdentity",
    "ProviderInvalidResponse",
    "ProviderRequestError",
    "ProviderTimeout",
    "ProviderUnavailable",
    "WindowsModelLock",
    "load_model_lock",
]
