from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from .index import MemoryIndex
from .qmd_adapter import QmdLifecycleAdapter, detect_qmd_runtime

SEARCH_WARNING = "Memory search is unavailable due to local index unavailability."
SEARCH_ACTION = "Rebuild local memory index and retry memory_search."
QMD_SEARCH_ACTION = "Install QMD prerequisites or retry memory_search with backend='builtin'."


@dataclass(frozen=True)
class BackendRuntimeStatus:
    provider: str
    available: bool
    reason: str | None
    details: tuple[str, ...] = ()


def build_unavailable_search_payload(
    reason: str,
    *,
    provider: str,
    mode: str,
    action: str,
    details: tuple[str, ...] = (),
) -> dict[str, Any]:
    payload = {
        "results": [],
        "provider": provider,
        "model": "",
        "citations": "off",
        "mode": mode,
        "disabled": True,
        "unavailable": True,
        "error": f"memory index unavailable: {reason}",
        "warning": SEARCH_WARNING,
        "action": action,
    }
    if details:
        payload["details"] = list(details)
    return payload


@runtime_checkable
class SearchBackend(Protocol):
    def search(self, query: str, *, max_results: int, min_score: float) -> dict[str, Any]:
        ...


class BuiltinSearchBackend:
    def __init__(
        self,
        *,
        workspace: str | Path,
        db_path: str | Path | None = None,
        source_roots: list[str | Path] | None = None,
    ) -> None:
        self._index = MemoryIndex(workspace=workspace, db_path=db_path, source_roots=source_roots)

    def search(self, query: str, *, max_results: int, min_score: float) -> dict[str, Any]:
        available, reason = self._index.is_available()
        if not available:
            return build_unavailable_search_payload(
                reason,
                provider="builtin",
                mode="fts-only",
                action=SEARCH_ACTION,
            )

        try:
            rows = self._index.search(query, max_results=max_results, min_score=min_score)
        except Exception as exc:  # deterministic unavailable contract on runtime failures
            return build_unavailable_search_payload(
                str(exc),
                provider="builtin",
                mode="fts-only",
                action=SEARCH_ACTION,
            )

        return {
            "results": rows,
            "provider": "builtin",
            "model": "",
            "citations": "off",
            "mode": "fts-only",
        }


class QmdSearchBackend:
    def __init__(
        self,
        *,
        workspace: str | Path,
        db_path: str | Path | None = None,
        source_roots: list[str | Path] | None = None,
        enable_fallback: bool = True,
        adapter: QmdLifecycleAdapter | None = None,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.db_path = Path(db_path).resolve() if db_path is not None else self.workspace / ".memory_index.sqlite"
        self.source_roots = [Path(root).expanduser().resolve() for root in source_roots or []]
        self.enable_fallback = enable_fallback
        self._adapter = adapter or QmdLifecycleAdapter(workspace=self.workspace, source_roots=source_roots)
        self._builtin_backend = BuiltinSearchBackend(
            workspace=self.workspace,
            db_path=self.db_path,
            source_roots=source_roots,
        )

    def status(self) -> BackendRuntimeStatus:
        runtime = detect_qmd_runtime()
        return BackendRuntimeStatus(
            provider="qmd",
            available=runtime.available,
            reason=runtime.reason,
            details=runtime.details,
        )

    def search(self, query: str, *, max_results: int, min_score: float) -> dict[str, Any]:
        payload = self._adapter.search(query, max_results=max_results, min_score=min_score)
        if not payload.get("unavailable"):
            return payload
        if not self.enable_fallback:
            return payload
        fallback = self._builtin_backend.search(query, max_results=max_results, min_score=min_score)
        fallback["fallback"] = {
            "requestedProvider": "qmd",
            "reason": payload.get("error", "qmd backend unavailable"),
        }
        if "details" in payload:
            fallback["fallback"]["details"] = payload["details"]
        return fallback


def resolve_search_backend(
    backend: str | SearchBackend | None,
    *,
    workspace: str | Path,
    db_path: str | Path | None = None,
    source_roots: list[str | Path] | None = None,
) -> SearchBackend:
    if backend is None or backend == "builtin":
        return BuiltinSearchBackend(workspace=workspace, db_path=db_path, source_roots=source_roots)
    if backend == "qmd":
        return QmdSearchBackend(workspace=workspace, db_path=db_path, source_roots=source_roots)
    if isinstance(backend, str):
        raise ValueError(f"unsupported memory search backend: {backend}")
    if isinstance(backend, SearchBackend):
        return backend
    raise TypeError("backend must be None, 'builtin', 'qmd', or implement SearchBackend")
