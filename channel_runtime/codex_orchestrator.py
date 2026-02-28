from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Callable

from channel_core.contracts import ContractValidationError, InboundMessage, OutboundMessage


@dataclass(frozen=True)
class CodexInvocationRequest:
    """Serializable payload for default Codex CLI invocation."""

    session_id: str
    chat_id: str
    user_id: str
    text: str
    update_id: str
    message_id: str | None

    @classmethod
    def from_inbound(cls, inbound: InboundMessage, *, session_id: str) -> "CodexInvocationRequest":
        return cls(
            session_id=session_id,
            chat_id=inbound.chat_id,
            user_id=inbound.user_id,
            text=inbound.text,
            update_id=inbound.update_id,
            message_id=inbound.message_id,
        )


@dataclass(frozen=True)
class CodexSessionPolicy:
    """Deterministic lifecycle policy for codex session runtimes."""

    max_sessions: int = 128
    idle_ttl_s: float = 900.0

    def __post_init__(self) -> None:
        if int(self.max_sessions) < 1:
            raise ValueError("max_sessions must be >= 1")
        if float(self.idle_ttl_s) <= 0:
            raise ValueError("idle_ttl_s must be > 0")


@dataclass
class _CodexSessionRuntime:
    session_id: str
    created_at_s: float
    last_activity_s: float
    invoke_count: int = 0
    timeout_count: int = 0
    failure_count: int = 0


class CodexExecError(RuntimeError):
    """Raised when codex execution fails before producing a valid response."""


class CodexInvalidResponseError(ValueError):
    """Raised when codex returns a response that violates orchestrator expectations."""


class CodexSessionManager:
    """Session-keyed runtime state with deterministic idle/capacity eviction."""

    def __init__(
        self,
        *,
        policy: CodexSessionPolicy | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._policy = policy or CodexSessionPolicy()
        self._clock = clock or time.monotonic
        self._sessions: dict[str, _CodexSessionRuntime] = {}

    def begin(self, session_id: str) -> None:
        now = float(self._clock())
        self._evict_idle(now)
        runtime = self._sessions.get(session_id)
        if runtime is None:
            runtime = _CodexSessionRuntime(session_id=session_id, created_at_s=now, last_activity_s=now)
            self._sessions[session_id] = runtime
        else:
            runtime.last_activity_s = now
        self._evict_over_capacity(prefer_keep_session_id=session_id)

    def record_success(self, session_id: str) -> None:
        runtime = self._require_session(session_id)
        runtime.invoke_count += 1
        runtime.last_activity_s = float(self._clock())

    def record_timeout(self, session_id: str) -> None:
        runtime = self._require_session(session_id)
        runtime.timeout_count += 1
        runtime.failure_count += 1
        runtime.last_activity_s = float(self._clock())

    def record_failure(self, session_id: str) -> None:
        runtime = self._require_session(session_id)
        runtime.failure_count += 1
        runtime.last_activity_s = float(self._clock())

    def cleanup(self) -> tuple[str, ...]:
        before = set(self._sessions.keys())
        self._evict_idle(float(self._clock()))
        return tuple(sorted(before - set(self._sessions.keys())))

    def describe(self, session_id: str) -> dict[str, Any] | None:
        runtime = self._sessions.get(session_id)
        if runtime is None:
            return None
        return {
            "session_id": runtime.session_id,
            "created_at_s": runtime.created_at_s,
            "last_activity_s": runtime.last_activity_s,
            "invoke_count": runtime.invoke_count,
            "timeout_count": runtime.timeout_count,
            "failure_count": runtime.failure_count,
        }

    def list_session_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._sessions.keys()))

    def _require_session(self, session_id: str) -> _CodexSessionRuntime:
        runtime = self._sessions.get(session_id)
        if runtime is not None:
            return runtime
        now = float(self._clock())
        created = _CodexSessionRuntime(session_id=session_id, created_at_s=now, last_activity_s=now)
        self._sessions[session_id] = created
        self._evict_over_capacity(prefer_keep_session_id=session_id)
        return created

    def _evict_idle(self, now: float) -> None:
        stale_ids = [
            session_id
            for session_id, runtime in self._sessions.items()
            if (now - runtime.last_activity_s) >= self._policy.idle_ttl_s
        ]
        for session_id in sorted(stale_ids):
            self._sessions.pop(session_id, None)

    def _evict_over_capacity(self, *, prefer_keep_session_id: str | None = None) -> None:
        while len(self._sessions) > self._policy.max_sessions:
            candidates = [
                runtime
                for runtime in self._sessions.values()
                if runtime.session_id != prefer_keep_session_id
            ]
            if not candidates:
                candidates = list(self._sessions.values())
            victim = min(
                candidates,
                key=lambda runtime: (runtime.last_activity_s, runtime.created_at_s, runtime.session_id),
            )
            self._sessions.pop(victim.session_id, None)


class CodexOrchestrator:
    """TG-LIVE-B1 sync seam for Codex-backed message orchestration."""

    def __init__(
        self,
        *,
        timeout_s: float = 20.0,
        invoke_fn: Callable[[CodexInvocationRequest], str | None] | None = None,
        session_manager: CodexSessionManager | None = None,
    ) -> None:
        self._timeout_s = float(timeout_s)
        self._invoke_fn = invoke_fn or (lambda req: _default_codex_invoke(req, timeout_s=self._timeout_s))
        self._session_manager = session_manager or CodexSessionManager()
        self._diagnostics: list[dict[str, Any]] = []

    def handle_message(self, inbound: InboundMessage, *, session_id: str) -> OutboundMessage | None:
        self._session_manager.begin(session_id)
        try:
            request = CodexInvocationRequest.from_inbound(inbound, session_id=session_id)
            response_text = self._invoke_fn(request)
            if response_text is None:
                self._session_manager.record_success(session_id)
                return None
            if not isinstance(response_text, str):
                actual_type = type(response_text).__name__
                raise CodexInvalidResponseError(
                    f"codex response must be a string or None, got {actual_type}"
                )
            text = response_text.strip()
            if not text:
                self._session_manager.record_success(session_id)
                return None
            self._session_manager.record_success(session_id)
            return OutboundMessage(
                chat_id=inbound.chat_id,
                text=text,
                reply_to_message_id=inbound.message_id,
                metadata={"session_id": session_id, "orchestrator_mode": "codex"},
            )
        except Exception as exc:
            code, retryable, track_as_timeout = _classify_codex_exception(exc)
            if track_as_timeout:
                self._session_manager.record_timeout(session_id)
            else:
                self._session_manager.record_failure(session_id)
            self._diagnostics.append(
                {
                    "code": code,
                    "update_id": inbound.update_id,
                    "session_id": session_id,
                    "retryable": retryable,
                    "message": _sanitize_exception(exc),
                }
            )
            return None

    def drain_diagnostics(self) -> list[dict[str, Any]]:
        diagnostics = list(self._diagnostics)
        self._diagnostics.clear()
        return diagnostics


def _default_codex_invoke(request: CodexInvocationRequest, *, timeout_s: float) -> str | None:
    payload = json.dumps(
        {
            "session_id": request.session_id,
            "chat_id": request.chat_id,
            "user_id": request.user_id,
            "text": request.text,
            "update_id": request.update_id,
            "message_id": request.message_id,
        },
        sort_keys=True,
    )
    try:
        completed = subprocess.run(
            ["codex", "exec", payload],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError("codex invocation timed out") from exc
    except OSError as exc:
        raise CodexExecError(str(exc) or "codex invocation failed") from exc
    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip() or "codex invocation failed"
        raise CodexExecError(stderr)
    output = (completed.stdout or "").strip()
    return output or None


def _classify_codex_exception(exc: Exception) -> tuple[str, bool, bool]:
    if isinstance(exc, TimeoutError):
        return ("codex-timeout", True, True)
    if isinstance(exc, ContractValidationError):
        return ("codex-contract-violation", False, False)
    if isinstance(exc, CodexInvalidResponseError):
        return ("codex-invalid-response", False, False)
    if isinstance(exc, (CodexExecError, OSError, subprocess.SubprocessError, RuntimeError)):
        return ("codex-exec-failed", True, False)
    return ("codex-exec-failed", True, False)


def _sanitize_exception(exc: Exception) -> str:
    raw = f"{type(exc).__name__}: {exc}".strip()
    compact = " ".join(raw.split())
    return compact[:500]
