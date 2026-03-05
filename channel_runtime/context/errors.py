from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


ErrorCategory = Literal["error", "drop"]


@dataclass(frozen=True)
class ContextErrorSpec:
    """Maps context failures to runtime error_details semantics."""

    code: str
    layer: str
    operation: str
    category: ErrorCategory
    retryable: bool


_CONTEXT_ERROR_SPECS: dict[str, ContextErrorSpec] = {
    "context-store-load-error": ContextErrorSpec(
        code="context-store-load-error",
        layer="context",
        operation="store_load",
        category="error",
        retryable=True,
    ),
    "context-store-save-error": ContextErrorSpec(
        code="context-store-save-error",
        layer="context",
        operation="store_save",
        category="error",
        retryable=True,
    ),
    "context-assembler-error": ContextErrorSpec(
        code="context-assembler-error",
        layer="context",
        operation="assemble",
        category="error",
        retryable=False,
    ),
    "context-estimator-error": ContextErrorSpec(
        code="context-estimator-error",
        layer="context",
        operation="estimate",
        category="error",
        retryable=False,
    ),
    "context-compaction-error": ContextErrorSpec(
        code="context-compaction-error",
        layer="context",
        operation="compact",
        category="error",
        retryable=True,
    ),
}


@dataclass(frozen=True)
class ContextErrorDetail:
    """Minimal context payload needed to materialize runtime error_details entries."""

    code: str
    message: str
    retryable: bool
    category: ErrorCategory
    layer: str
    operation: str
    source: str = "context"
    update_id: str = ""
    chat_id: str = ""
    session_id: str = ""

    def to_runtime_error_detail(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "context": {
                "update_id": self.update_id,
                "chat_id": self.chat_id,
                "session_id": self.session_id,
                "layer": self.layer,
                "operation": self.operation,
            },
            "source": self.source,
            "category": self.category,
        }


class ContextSubsystemError(RuntimeError):
    """Base exception carrying runtime classification metadata."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        operation: str | None = None,
        retryable: bool | None = None,
    ) -> None:
        super().__init__(message)
        self._spec = resolve_context_error_spec(
            code,
            operation_override=operation,
            retryable_override=retryable,
        )

    @property
    def spec(self) -> ContextErrorSpec:
        return self._spec


class ContextStoreError(ContextSubsystemError):
    def __init__(
        self,
        message: str,
        *,
        code: Literal["context-store-load-error", "context-store-save-error"],
        operation: str,
        retryable: bool = True,
    ) -> None:
        super().__init__(message, code=code, operation=operation, retryable=retryable)


class ContextAssemblerError(ContextSubsystemError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="context-assembler-error")


class ContextEstimatorError(ContextSubsystemError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="context-estimator-error")


class ContextCompactionError(ContextSubsystemError):
    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message, code="context-compaction-error", retryable=retryable)


def resolve_context_error_spec(
    code: str,
    *,
    operation_override: str | None = None,
    retryable_override: bool | None = None,
) -> ContextErrorSpec:
    normalized = str(code or "").strip()
    base = _CONTEXT_ERROR_SPECS.get(
        normalized,
        ContextErrorSpec(
            code=normalized or "context-unknown-error",
            layer="context",
            operation="unknown",
            category="error",
            retryable=False,
        ),
    )
    return ContextErrorSpec(
        code=base.code,
        layer=base.layer,
        operation=operation_override or base.operation,
        category=base.category,
        retryable=base.retryable if retryable_override is None else bool(retryable_override),
    )


def build_context_error_detail(
    *,
    code: str,
    message: str,
    source: str = "context",
    update_id: str = "",
    chat_id: str = "",
    session_id: str = "",
    operation_override: str | None = None,
    retryable_override: bool | None = None,
) -> ContextErrorDetail:
    spec = resolve_context_error_spec(
        code,
        operation_override=operation_override,
        retryable_override=retryable_override,
    )
    return ContextErrorDetail(
        code=spec.code,
        message=str(message or "").strip(),
        retryable=spec.retryable,
        category=spec.category,
        layer=spec.layer,
        operation=spec.operation,
        source=str(source or "context").strip(),
        update_id=str(update_id or "").strip(),
        chat_id=str(chat_id or "").strip(),
        session_id=str(session_id or "").strip(),
    )
