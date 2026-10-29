"""Phase 1 memory system package."""

from .api import memory_get, memory_rebuild, memory_search
from .backend import BackendRuntimeStatus, BuiltinSearchBackend, QmdSearchBackend, SearchBackend, resolve_search_backend
from .index import MemoryIndex
from .paths import QmdCollection, build_qmd_collections
from .qmd_adapter import QmdLifecycleAdapter, QmdRuntimeStatus

__all__ = [
    "BackendRuntimeStatus",
    "BuiltinSearchBackend",
    "MemoryIndex",
    "QmdCollection",
    "QmdLifecycleAdapter",
    "QmdSearchBackend",
    "QmdRuntimeStatus",
    "SearchBackend",
    "build_qmd_collections",
    "memory_get",
    "memory_rebuild",
    "memory_search",
    "resolve_search_backend",
]
