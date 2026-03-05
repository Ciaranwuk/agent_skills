from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from channel_core.contracts import ContractValidationError, InboundMessage, OutboundMessage
from channel_runtime.config import DURABLE_CONTEXT_MODE, LEGACY_CONTEXT_EMERGENCY_TOGGLE, LEGACY_CONTEXT_MODE
from channel_runtime.context.assembler import ContextAssembler
from channel_runtime.context.compaction import CompactionPolicy, CompactionService
from channel_runtime.context.contracts import ContextStorePort, ContextTurn
from channel_runtime.context.errors import ContextSubsystemError
from channel_runtime.context.store import ContextStore
from channel_runtime.context.token_estimator import TokenEstimator

ConversationTurn = dict[str, str | None]
_OVERFLOW_ERROR_SIGNATURES = (
    "context length exceeded",
    "maximum context length",
    "context_window_exceeded",
    "input exceeds the context window",
    "prompt is too long",
    "too many tokens",
)
_OPERATOR_INSPECT_COMMANDS = frozenset({"/ctx inspect", "/context inspect"})
_OPERATOR_COMPACT_COMMANDS = frozenset({"/ctx compact", "/context compact"})


@dataclass(frozen=True)
class CodexInvocationRequest:
    """Serializable payload for default Codex CLI invocation."""

    session_id: str
    chat_id: str
    user_id: str
    text: str
    update_id: str
    message_id: str | None
    conversation_history: tuple[ConversationTurn, ...] = ()

    @classmethod
    def from_inbound(
        cls,
        inbound: InboundMessage,
        *,
        session_id: str,
        conversation_history: tuple[ConversationTurn, ...] = (),
    ) -> "CodexInvocationRequest":
        return cls(
            session_id=session_id,
            chat_id=inbound.chat_id,
            user_id=inbound.user_id,
            text=inbound.text,
            update_id=inbound.update_id,
            message_id=inbound.message_id,
            conversation_history=conversation_history,
        )


@dataclass(frozen=True)
class CodexSessionPolicy:
    """Deterministic lifecycle policy for codex session runtimes."""

    max_sessions: int = 128
    idle_ttl_s: float = 900.0
    max_history_turns: int = 20

    def __post_init__(self) -> None:
        if int(self.max_sessions) < 1:
            raise ValueError("max_sessions must be >= 1")
        if float(self.idle_ttl_s) <= 0:
            raise ValueError("idle_ttl_s must be > 0")
        if int(self.max_history_turns) < 1:
            raise ValueError("max_history_turns must be >= 1")


@dataclass
class _CodexSessionRuntime:
    session_id: str
    created_at_s: float
    last_activity_s: float
    invoke_count: int = 0
    timeout_count: int = 0
    failure_count: int = 0
    conversation_history: list[ConversationTurn] | None = None

    def __post_init__(self) -> None:
        if self.conversation_history is None:
            self.conversation_history = []


@dataclass
class _ContextTelemetryState:
    mode: str
    tokens_estimated_total: int = 0
    current_tokens_estimate: int | None = None
    summary_tokens_estimate: int | None = None
    recent_tokens_estimate: int | None = None
    compaction_attempted: int = 0
    compaction_succeeded: int = 0
    compaction_failed: int = 0
    compaction_fallback_used: int = 0
    compaction_reason_threshold: int = 0
    compaction_reason_overflow: int = 0
    compaction_reason_manual: int = 0
    build_failures: int = 0

    def reset(self, *, mode: str) -> None:
        self.mode = str(mode).strip().lower()
        self.tokens_estimated_total = 0
        self.current_tokens_estimate = None
        self.summary_tokens_estimate = None
        self.recent_tokens_estimate = None
        self.compaction_attempted = 0
        self.compaction_succeeded = 0
        self.compaction_failed = 0
        self.compaction_fallback_used = 0
        self.compaction_reason_threshold = 0
        self.compaction_reason_overflow = 0
        self.compaction_reason_manual = 0
        self.build_failures = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "counters": {
                "tokens_estimated_total": self.tokens_estimated_total,
                "compaction_attempted_total": self.compaction_attempted,
                "compaction_succeeded_total": self.compaction_succeeded,
                "compaction_failed_total": self.compaction_failed,
                "compaction_fallback_used_total": self.compaction_fallback_used,
                "compaction_reasons": {
                    "threshold_total": self.compaction_reason_threshold,
                    "overflow_total": self.compaction_reason_overflow,
                    "manual_total": self.compaction_reason_manual,
                },
                "build_failures_total": self.build_failures,
            },
            "gauges": {
                "current_tokens_estimate": self.current_tokens_estimate,
                "summary_tokens_estimate": self.summary_tokens_estimate,
                "recent_tokens_estimate": self.recent_tokens_estimate,
            },
        }


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

    @property
    def max_history_turns(self) -> int:
        return int(self._policy.max_history_turns)

    def conversation_history(self, session_id: str) -> tuple[ConversationTurn, ...]:
        runtime = self._require_session(session_id)
        return tuple(
            {
                "user_text": str(turn.get("user_text", "")),
                "assistant_text": (str(turn["assistant_text"]) if turn.get("assistant_text") is not None else None),
            }
            for turn in runtime.conversation_history or []
        )

    def append_conversation_turn(self, session_id: str, *, user_text: str, assistant_text: str | None) -> None:
        runtime = self._require_session(session_id)
        history = runtime.conversation_history or []
        history.append(
            {
                "user_text": str(user_text),
                "assistant_text": (str(assistant_text) if assistant_text is not None else None),
            }
        )
        max_turns = int(self._policy.max_history_turns)
        if len(history) > max_turns:
            del history[: len(history) - max_turns]
        runtime.conversation_history = history
        runtime.last_activity_s = float(self._clock())

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
        notify_on_error: bool = False,
        invoke_fn: Callable[[CodexInvocationRequest], str | None] | None = None,
        session_manager: CodexSessionManager | None = None,
        context_mode: str = LEGACY_CONTEXT_MODE,
        context_store: ContextStorePort | None = None,
        context_assembler: ContextAssembler | None = None,
        compaction_service: CompactionService | None = None,
        compaction_policy: CompactionPolicy | None = None,
        enable_context_operator_controls: bool = False,
    ) -> None:
        self._timeout_s = float(timeout_s)
        self._notify_on_error = bool(notify_on_error)
        self._invoke_fn = invoke_fn or (lambda req: _default_codex_invoke(req, timeout_s=self._timeout_s))
        self._session_manager = session_manager or CodexSessionManager()
        self._context_mode = str(context_mode).strip().lower()
        if self._context_mode not in {LEGACY_CONTEXT_MODE, DURABLE_CONTEXT_MODE}:
            raise ValueError(
                "context_mode must be 'legacy' or 'durable' "
                f"(set {LEGACY_CONTEXT_EMERGENCY_TOGGLE} for emergency rollback)"
            )
        self._context_store = context_store
        if self._context_mode == DURABLE_CONTEXT_MODE and self._context_store is None:
            self._context_store = ContextStore()
        self._context_assembler = context_assembler or ContextAssembler(
            default_max_turns=self._session_manager.max_history_turns
        )
        self._compaction_service: CompactionService | None = None
        self._compaction_policy: CompactionPolicy | None = None
        if self._context_mode == DURABLE_CONTEXT_MODE and self._context_store is not None:
            self._compaction_service = compaction_service or CompactionService(
                store=self._context_store,
                assembler=self._context_assembler,
            )
            self._compaction_policy = compaction_policy or CompactionPolicy(
                context_window_tokens=128000,
                reserve_tokens=16000,
                keep_recent_tokens=24000,
                min_compaction_gain_tokens=0,
                cooldown_window_s=0.0,
            )
        self._token_estimator = TokenEstimator()
        self._context_telemetry = _ContextTelemetryState(mode=self._context_mode)
        self._diagnostics: list[dict[str, Any]] = []
        self._enable_context_operator_controls = bool(enable_context_operator_controls)

    def handle_message(self, inbound: InboundMessage, *, session_id: str) -> OutboundMessage | None:
        if self._enable_context_operator_controls:
            operator_response = self._handle_operator_context_command(inbound=inbound, session_id=session_id)
            if operator_response is not None:
                return operator_response
        self._session_manager.begin(session_id)
        try:
            history = self._conversation_history_for_request(session_id=session_id)
            request = CodexInvocationRequest.from_inbound(
                inbound,
                session_id=session_id,
                conversation_history=history,
            )
            response_text = self._invoke_with_overflow_recovery(
                request=request,
                inbound=inbound,
                session_id=session_id,
            )
            if response_text is None:
                self._record_conversation_turn(
                    session_id=session_id,
                    user_text=inbound.text,
                    assistant_text=None,
                )
                self._session_manager.record_success(session_id)
                return None
            if not isinstance(response_text, str):
                actual_type = type(response_text).__name__
                raise CodexInvalidResponseError(
                    f"codex response must be a string or None, got {actual_type}"
                )
            text = response_text.strip()
            if not text:
                self._record_conversation_turn(
                    session_id=session_id,
                    user_text=inbound.text,
                    assistant_text=None,
                )
                self._session_manager.record_success(session_id)
                return None
            self._record_conversation_turn(
                session_id=session_id,
                user_text=inbound.text,
                assistant_text=text,
            )
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
            diagnostic_code = code
            diagnostic_retryable = retryable
            diagnostic: dict[str, Any] = {
                "code": diagnostic_code,
                "update_id": inbound.update_id,
                "session_id": session_id,
                "retryable": diagnostic_retryable,
                "message": _sanitize_exception(exc),
            }
            context_diagnostic = _classify_context_diagnostic(exc)
            if context_diagnostic is not None:
                diagnostic_code = context_diagnostic["code"]
                diagnostic_retryable = context_diagnostic["retryable"]
                diagnostic["code"] = diagnostic_code
                diagnostic["retryable"] = diagnostic_retryable
                diagnostic["layer"] = "context"
                diagnostic["operation"] = context_diagnostic["operation"]
            overflow_recovery = getattr(exc, "_overflow_recovery", None)
            if isinstance(overflow_recovery, dict):
                diagnostic["overflow_recovery"] = dict(overflow_recovery)
                diagnostic["layer"] = "context"
                diagnostic["operation"] = "compact"
            self._diagnostics.append(diagnostic)
            if not self._notify_on_error:
                return None
            return _build_error_fallback_message(inbound=inbound, session_id=session_id, code=code)

    def drain_diagnostics(self) -> list[dict[str, Any]]:
        diagnostics = list(self._diagnostics)
        self._diagnostics.clear()
        return diagnostics

    def drain_context_telemetry(self) -> dict[str, Any]:
        snapshot = self._context_telemetry.as_dict()
        self._context_telemetry.reset(mode=self._context_mode)
        return snapshot

    def _conversation_history_for_request(self, *, session_id: str) -> tuple[ConversationTurn, ...]:
        if self._context_mode != "durable":
            return self._session_manager.conversation_history(session_id)

        store = self._context_store
        if store is None:
            return ()
        transcript_turns = store.load_transcript(session_id=session_id)
        if not transcript_turns:
            transcript_turns = self._initialize_missing_durable_transcript(
                session_id=session_id,
                store=store,
            )
        history = self._context_assembler.assemble_conversation_history(
            session_id=session_id,
            turns=transcript_turns,
            max_turns=self._session_manager.max_history_turns,
        )
        self._record_history_tokens(history)
        return history

    def _initialize_missing_durable_transcript(
        self,
        *,
        session_id: str,
        store: ContextStorePort,
    ) -> tuple[ContextTurn, ...]:
        memory_history = self._session_manager.conversation_history(session_id)
        if not memory_history:
            return ()

        load_metadata = getattr(store, "load_session_metadata", None)
        if callable(load_metadata) and load_metadata(session_id=session_id) is not None:
            return ()

        for turn in memory_history:
            user_text = str(turn.get("user_text", ""))
            assistant_text = turn.get("assistant_text")
            if user_text:
                store.append_turn(
                    session_id=session_id,
                    turn=ContextTurn(role="user", text=user_text),
                )
            if assistant_text is not None:
                assistant_value = str(assistant_text)
                if assistant_value:
                    store.append_turn(
                        session_id=session_id,
                        turn=ContextTurn(role="assistant", text=assistant_value),
                    )

        return store.load_transcript(session_id=session_id)

    def _record_conversation_turn(self, *, session_id: str, user_text: str, assistant_text: str | None) -> None:
        if self._context_mode != "durable":
            self._session_manager.append_conversation_turn(
                session_id,
                user_text=user_text,
                assistant_text=assistant_text,
            )
            return

        store = self._context_store
        if store is None:
            return
        store.append_turn(
            session_id=session_id,
            turn=ContextTurn(role="user", text=str(user_text)),
        )
        if assistant_text is None:
            return
        store.append_turn(
            session_id=session_id,
            turn=ContextTurn(role="assistant", text=str(assistant_text)),
        )

    def _invoke_with_overflow_recovery(
        self,
        *,
        request: CodexInvocationRequest,
        inbound: InboundMessage,
        session_id: str,
    ) -> str | None:
        try:
            return self._invoke_fn(request)
        except Exception as exc:
            if not _is_overflow_like_codex_error(exc):
                raise
            if (
                self._context_mode != "durable"
                or self._context_store is None
                or self._compaction_service is None
                or self._compaction_policy is None
            ):
                raise

            recovery_meta: dict[str, Any] = {"attempted": True, "retry_attempted": False}
            self._record_compaction_attempt(reason="overflow")
            try:
                result = self._compaction_service.evaluate_and_compact(
                    session_id=session_id,
                    policy=self._compaction_policy,
                )
            except Exception as compaction_exc:
                self._record_compaction_failure()
                recovery_meta["compaction_error"] = _sanitize_exception(compaction_exc)
                setattr(compaction_exc, "_overflow_recovery", recovery_meta)
                raise

            recovery_meta["compaction_status"] = result.status
            recovery_meta["compaction_reason"] = result.reason
            self._record_compaction_estimates(result)
            retry_history = self._conversation_history_for_request(session_id=session_id)
            if result.status == "failed":
                self._record_compaction_failure()
                recovery_meta["fallback_applied"] = not self._is_context_strict_mode()
                if recovery_meta["fallback_applied"]:
                    self._record_compaction_fallback_used()
                if self._is_context_strict_mode():
                    compaction_failed_exc = CodexExecError(
                        f"overflow recovery compaction failed: {result.reason}"
                    )
                    setattr(compaction_failed_exc, "_overflow_recovery", recovery_meta)
                    raise compaction_failed_exc from exc
                retry_history = result.conversation_history
                self._diagnostics.append(
                    {
                        "code": "context-compaction-fallback",
                        "update_id": inbound.update_id,
                        "session_id": session_id,
                        "retryable": True,
                        "layer": "context",
                        "operation": "compact",
                        "message": f"compaction failed, fallback context used: {result.reason}",
                        "overflow_recovery": dict(recovery_meta),
                    }
                )
            elif result.status != "compacted":
                self._record_compaction_failure()
                compact_skip_exc = CodexExecError(
                    f"overflow recovery compaction skipped: {result.reason}"
                )
                setattr(compact_skip_exc, "_overflow_recovery", recovery_meta)
                raise compact_skip_exc from exc
            else:
                self._record_compaction_success()

            recovery_meta["retry_attempted"] = True
            retry_request = CodexInvocationRequest.from_inbound(
                inbound,
                session_id=session_id,
                conversation_history=retry_history,
            )
            try:
                return self._invoke_fn(retry_request)
            except Exception as retry_exc:
                recovery_meta["retry_error"] = _sanitize_exception(retry_exc)
                setattr(retry_exc, "_overflow_recovery", recovery_meta)
                raise

    def _record_history_tokens(self, history: tuple[ConversationTurn, ...]) -> None:
        try:
            estimate = int(self._token_estimator.estimate_assembled_window(conversation_history=history))
        except Exception:
            self._context_telemetry.build_failures += 1
            return
        self._context_telemetry.tokens_estimated_total += max(0, estimate)
        self._context_telemetry.current_tokens_estimate = max(0, estimate)

    def _record_compaction_attempt(self, *, reason: str) -> None:
        self._context_telemetry.compaction_attempted += 1
        normalized = str(reason).strip().lower()
        if normalized == "overflow":
            self._context_telemetry.compaction_reason_overflow += 1
        elif normalized == "manual":
            self._context_telemetry.compaction_reason_manual += 1
        else:
            self._context_telemetry.compaction_reason_threshold += 1

    def _record_compaction_success(self) -> None:
        self._context_telemetry.compaction_succeeded += 1

    def _record_compaction_failure(self) -> None:
        self._context_telemetry.compaction_failed += 1

    def _record_compaction_fallback_used(self) -> None:
        self._context_telemetry.compaction_fallback_used += 1

    def _record_compaction_estimates(self, result: Any) -> None:
        estimated_before = max(0, int(getattr(result, "estimated_tokens_before", 0)))
        estimated_after = max(0, int(getattr(result, "estimated_tokens_after", 0)))
        self._context_telemetry.tokens_estimated_total += estimated_before
        self._context_telemetry.current_tokens_estimate = estimated_after

        transcript_turns = getattr(result, "transcript_turns", ())
        if not isinstance(transcript_turns, tuple):
            self._context_telemetry.build_failures += 1
            return
        try:
            summary_turns = tuple(
                turn
                for turn in transcript_turns
                if str(turn.metadata.get("source_type", "")).strip().lower() == "compaction"
                or str(getattr(turn, "role", "")).strip().lower() == "compaction"
            )
            recent_turns = tuple(
                turn
                for turn in transcript_turns
                if str(turn.metadata.get("source_type", "")).strip().lower() != "compaction"
                and str(getattr(turn, "role", "")).strip().lower() in {"user", "assistant"}
            )
            self._context_telemetry.summary_tokens_estimate = (
                self._token_estimator.estimate_window(turns=summary_turns) if summary_turns else 0
            )
            self._context_telemetry.recent_tokens_estimate = (
                self._token_estimator.estimate_window(turns=recent_turns) if recent_turns else 0
            )
        except Exception:
            self._context_telemetry.build_failures += 1

    def _is_context_strict_mode(self) -> bool:
        store = self._context_store
        if store is None:
            return False
        if hasattr(store, "strict_io"):
            try:
                return bool(getattr(store, "strict_io"))
            except Exception:
                return False
        return bool(getattr(store, "_strict_io", False))

    def _handle_operator_context_command(self, *, inbound: InboundMessage, session_id: str) -> OutboundMessage | None:
        command = _parse_operator_context_command(inbound.text)
        if command is None:
            return None
        try:
            if command == "inspect":
                result = self.inspect_session_context(session_id=session_id)
            else:
                result = self.run_manual_compaction(session_id=session_id)
            text = _format_operator_context_report(command=command, result=result)
            return OutboundMessage(
                chat_id=inbound.chat_id,
                text=text,
                reply_to_message_id=inbound.message_id,
                metadata={
                    "session_id": session_id,
                    "orchestrator_mode": "codex",
                    "operator_command": f"context-{command}",
                    "operator_status": str(result.get("status", "")).strip().lower(),
                },
            )
        except Exception as exc:
            self._diagnostics.append(
                {
                    "code": "context-operator-command-error",
                    "update_id": inbound.update_id,
                    "session_id": session_id,
                    "retryable": False,
                    "layer": "context",
                    "operation": command,
                    "message": _sanitize_exception(exc),
                }
            )
            return OutboundMessage(
                chat_id=inbound.chat_id,
                text=f"context {command}: status=failed reason=internal-error",
                reply_to_message_id=inbound.message_id,
                metadata={
                    "session_id": session_id,
                    "orchestrator_mode": "codex",
                    "operator_command": f"context-{command}",
                    "operator_status": "failed",
                },
            )

    def inspect_session_context(self, *, session_id: str) -> dict[str, Any]:
        normalized_session_id = _normalize_non_empty_text(session_id)
        if self._context_mode != "durable" or self._context_store is None:
            existing = self._session_manager.describe(normalized_session_id)
            if existing is None:
                return _operator_result(
                    session_id=normalized_session_id,
                    status="skipped",
                    reason="session-missing",
                )
            history = self._session_manager.conversation_history(normalized_session_id)
            estimated_tokens = 0
            try:
                estimated_tokens = max(
                    0,
                    int(self._token_estimator.estimate_assembled_window(conversation_history=history)),
                )
            except Exception:
                estimated_tokens = 0
            return _operator_result(
                session_id=normalized_session_id,
                status="ok",
                reason="legacy-session",
                estimated_tokens_before=estimated_tokens,
                estimated_tokens_after=estimated_tokens,
                turns_before=len(history),
                turns_after=len(history),
            )

        store = self._context_store
        turns = store.load_transcript(session_id=normalized_session_id)
        if not turns and not _durable_session_exists(store=store, session_id=normalized_session_id):
            return _operator_result(
                session_id=normalized_session_id,
                status="skipped",
                reason="session-missing",
            )
        estimated_tokens = 0
        try:
            estimated_tokens = max(0, int(self._token_estimator.estimate_window(turns=turns)))
        except Exception:
            estimated_tokens = 0
        return _operator_result(
            session_id=normalized_session_id,
            status="ok",
            reason="session-found",
            estimated_tokens_before=estimated_tokens,
            estimated_tokens_after=estimated_tokens,
            turns_before=len(turns),
            turns_after=len(turns),
        )

    def run_manual_compaction(self, *, session_id: str) -> dict[str, Any]:
        normalized_session_id = _normalize_non_empty_text(session_id)
        if (
            self._context_mode != "durable"
            or self._context_store is None
            or self._compaction_service is None
            or self._compaction_policy is None
        ):
            return _operator_result(
                session_id=normalized_session_id,
                status="skipped",
                reason="manual-compaction-unavailable",
            )

        store = self._context_store
        turns_before = store.load_transcript(session_id=normalized_session_id)
        if not turns_before and not _durable_session_exists(store=store, session_id=normalized_session_id):
            return _operator_result(
                session_id=normalized_session_id,
                status="skipped",
                reason="session-missing",
            )

        self._record_compaction_attempt(reason="manual")
        result = self._compaction_service.evaluate_and_compact(
            session_id=normalized_session_id,
            policy=self._compaction_policy,
        )
        self._record_compaction_estimates(result)
        if result.status == "compacted":
            self._record_compaction_success()
        elif result.status == "failed":
            self._record_compaction_failure()
        return _operator_result(
            session_id=normalized_session_id,
            status=result.status,
            reason=str(result.reason),
            estimated_tokens_before=max(0, int(result.estimated_tokens_before)),
            estimated_tokens_after=max(0, int(result.estimated_tokens_after)),
            turns_before=len(turns_before),
            turns_after=len(result.transcript_turns),
        )


def _default_codex_invoke(request: CodexInvocationRequest, *, timeout_s: float) -> str | None:
    codex_cwd = str(Path(__file__).resolve().parents[2])
    payload = json.dumps(
        {
            "session_id": request.session_id,
            "chat_id": request.chat_id,
            "user_id": request.user_id,
            "text": request.text,
            "update_id": request.update_id,
            "message_id": request.message_id,
            "conversation_history": list(request.conversation_history),
        },
        sort_keys=True,
    )
    try:
        completed = subprocess.run(
            [
                "codex",
                "exec",
                "--sandbox",
                "workspace-write",
                "--skip-git-repo-check",
                "--cd",
                codex_cwd,
                payload,
            ],
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


def _is_overflow_like_codex_error(exc: Exception) -> bool:
    if not isinstance(exc, CodexExecError):
        return False
    text = " ".join(str(exc).strip().lower().split())
    if not text:
        return False
    return any(signature in text for signature in _OVERFLOW_ERROR_SIGNATURES)


def _build_error_fallback_message(
    *,
    inbound: InboundMessage,
    session_id: str,
    code: str,
) -> OutboundMessage | None:
    if code not in {
        "codex-timeout",
        "codex-exec-failed",
        "codex-invalid-response",
        "codex-contract-violation",
    }:
        return None

    if code == "codex-timeout":
        text = "Sorry, the request timed out. Please try again."
    else:
        text = "Sorry, something went wrong. Please try again."

    return OutboundMessage(
        chat_id=inbound.chat_id,
        text=text,
        reply_to_message_id=inbound.message_id,
        metadata={
            "session_id": session_id,
            "orchestrator_mode": "codex",
            "fallback": "orchestrator-error",
            "error_code": code,
        },
    )


def _classify_context_diagnostic(exc: Exception) -> dict[str, Any] | None:
    if isinstance(exc, ContextSubsystemError):
        spec = exc.spec
        return {
            "code": spec.code,
            "retryable": bool(spec.retryable),
            "operation": spec.operation,
        }
    return None


def _parse_operator_context_command(text: str) -> str | None:
    normalized = " ".join(str(text or "").strip().lower().split())
    if not normalized:
        return None
    if normalized in _OPERATOR_INSPECT_COMMANDS:
        return "inspect"
    if normalized in _OPERATOR_COMPACT_COMMANDS:
        return "compact"
    return None


def _normalize_non_empty_text(value: str) -> str:
    normalized = str(value).strip()
    if normalized:
        return normalized
    return "unknown-session"


def _durable_session_exists(*, store: ContextStorePort, session_id: str) -> bool:
    load_metadata = getattr(store, "load_session_metadata", None)
    if not callable(load_metadata):
        return False
    try:
        return load_metadata(session_id=session_id) is not None
    except Exception:
        return False


def _operator_result(
    *,
    session_id: str,
    status: str,
    reason: str,
    estimated_tokens_before: int = 0,
    estimated_tokens_after: int = 0,
    turns_before: int = 0,
    turns_after: int = 0,
) -> dict[str, Any]:
    before_tokens = max(0, int(estimated_tokens_before))
    after_tokens = max(0, int(estimated_tokens_after))
    return {
        "session_id": str(session_id),
        "status": str(status).strip().lower(),
        "reason": str(reason).strip().lower(),
        "estimated_tokens_before": before_tokens,
        "estimated_tokens_after": after_tokens,
        "gained_tokens": max(0, before_tokens - after_tokens),
        "turns_before": max(0, int(turns_before)),
        "turns_after": max(0, int(turns_after)),
    }


def _format_operator_context_report(*, command: str, result: dict[str, Any]) -> str:
    return (
        f"context {command}:"
        f" session_id={result['session_id']}"
        f" status={result['status']}"
        f" reason={result['reason']}"
        f" tokens_before={result['estimated_tokens_before']}"
        f" tokens_after={result['estimated_tokens_after']}"
        f" gained_tokens={result['gained_tokens']}"
        f" turns_before={result['turns_before']}"
        f" turns_after={result['turns_after']}"
    )
