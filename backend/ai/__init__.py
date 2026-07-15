"""Local-only AI contracts, providers, and feasibility tooling."""

from .contracts import AIClassification, Evidence
from .evidence import (
    EvidenceBuildError,
    EvidenceBuilder,
    EvidenceIntegrityError,
    EvidencePackage,
    EvidencePathError,
)
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
from .worker import AIWorker, AIWorkerService, WorkerOutcome, load_locked_llama_provider
from .triggers import AITriggerDecision, evaluate_inbox_ai_trigger

__all__ = [
    "AIClassification",
    "AnalysisRequest",
    "AIWorker",
    "AIWorkerService",
    "AITriggerDecision",
    "Evidence",
    "EvidenceBuildError",
    "EvidenceBuilder",
    "EvidenceIntegrityError",
    "EvidencePackage",
    "EvidencePathError",
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
    "WorkerOutcome",
    "load_locked_llama_provider",
    "load_model_lock",
    "evaluate_inbox_ai_trigger",
]
