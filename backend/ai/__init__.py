"""Local-only AI contracts and feasibility tooling.

The production catalog does not depend on this package. Phase 1 uses it only
for read-only benchmarking until the model passes the documented gate.
"""

from .contracts import AIClassification, Evidence
from .model_lock import WindowsModelLock, load_model_lock

__all__ = ["AIClassification", "Evidence", "WindowsModelLock", "load_model_lock"]
