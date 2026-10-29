from __future__ import annotations

from pathlib import Path
from typing import Any

from .backend import resolve_search_backend
from .paths import DENY_ERROR, check_allowed_path, normalize_source_roots
from .qmd_adapter import QmdLifecycleAdapter
from .index import MemoryIndex


def memory_search(
    query: str,
    maxResults: int | None = None,
    minScore: float | None = None,
    *,
    workspace: str | Path,
    db_path: str | Path | None = None,
    source_roots: list[str | Path] | None = None,
    backend: str | Any | None = None,
) -> dict[str, Any]:
    query_text = (query or "").strip()
    if not query_text:
        raise ValueError("query must be a non-empty string")

    max_results = int(maxResults) if maxResults is not None else 10
    min_score = float(minScore) if minScore is not None else 0.0
    if max_results < 1 or max_results > 50:
        raise ValueError("maxResults must be between 1 and 50")
    if min_score < 0.0 or min_score > 1.0:
        raise ValueError("minScore must be between 0 and 1")
    search_backend = resolve_search_backend(
        backend,
        workspace=workspace,
        db_path=db_path,
        source_roots=source_roots,
    )
    return search_backend.search(query_text, max_results=max_results, min_score=min_score)


def memory_get(
    path: str,
    from_: int | None = None,
    lines: int | None = None,
    *,
    workspace: str | Path,
    source_roots: list[str | Path] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    if from_ is None and "from" in kwargs:
        from_ = kwargs.pop("from")
    if kwargs:
        raise TypeError(f"unexpected keyword arguments: {', '.join(sorted(kwargs.keys()))}")

    workspace_path = Path(workspace).resolve()
    roots = normalize_source_roots(workspace_path, source_roots)
    check = check_allowed_path(roots, path)
    normalized = check.normalized or (path or "").strip().replace("\\", "/")

    if not check.allowed:
        return {
            "path": normalized,
            "text": "",
            "disabled": True,
            "error": DENY_ERROR,
        }

    rel = check.normalized
    target = check.target
    if target is None:
        return {
            "path": rel,
            "text": "",
            "disabled": True,
            "error": DENY_ERROR,
        }
    if not target.exists():
        return {"path": rel, "text": ""}

    try:
        text = target.read_text(encoding="utf-8")
    except Exception as exc:
        return {
            "path": rel,
            "text": "",
            "disabled": True,
            "error": f"read failed: {exc}",
        }

    if from_ is None and lines is None:
        return {"path": rel, "text": text}

    start = 1 if from_ is None else int(from_)
    if start < 1:
        raise ValueError("from must be >= 1")
    if lines is not None:
        line_count = int(lines)
        if line_count < 1:
            raise ValueError("lines must be >= 1")
        if line_count > 2000:
            raise ValueError("lines must be <= 2000")
    else:
        line_count = None

    items = text.splitlines(keepends=True)
    begin = start - 1
    if begin >= len(items):
        return {"path": rel, "text": ""}

    if line_count is None:
        sliced = items[begin:]
    else:
        sliced = items[begin : begin + line_count]

    return {"path": rel, "text": "".join(sliced)}


def memory_rebuild(
    *,
    workspace: str | Path,
    db_path: str | Path | None = None,
    source_roots: list[str | Path] | None = None,
    backend: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    if backend is None or backend == "builtin":
        index = MemoryIndex(workspace=workspace, db_path=db_path, source_roots=source_roots)
        return {"ok": True, "stats": index.sync(force=force)}
    if backend == "qmd":
        adapter = QmdLifecycleAdapter(workspace=workspace, source_roots=source_roots)
        return adapter.rebuild(force=force)
    raise ValueError(f"unsupported memory rebuild backend: {backend}")
