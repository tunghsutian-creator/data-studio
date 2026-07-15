from .v0001_initial import migration as v0001
from .v0002_hash_columns import migration as v0002
from .v0003_phase0 import migration as v0003
from .v0004_ai_tasks import migration as v0004

MIGRATIONS = (v0001, v0002, v0003, v0004)

__all__ = ["MIGRATIONS"]
