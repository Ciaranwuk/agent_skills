from __future__ import annotations

import hashlib
import json
import re
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from channel_runtime.youtube_transcript import (
    YouTubeTranscript,
    YouTubeTranscriptError,
    extract_first_youtube_url,
    fetch_youtube_transcript,
    format_codex_prompt,
    transcript_from_mapping,
    transcript_to_dict,
)


ACQUISITION_MODE_DISABLED = "disabled"
ACQUISITION_MODE_INLINE = "inline"
ACQUISITION_MODE_COMMAND = "command"
YOUTUBE_TRANSCRIPT_JOB = "youtube-transcript"
DEFAULT_ALLOWED_JOB_TYPES = (YOUTUBE_TRANSCRIPT_JOB,)
DEFAULT_COMMAND_TIMEOUT_S = 60.0
_DIAGNOSTIC_PREVIEW_LIMIT = 180
_COMMAND_TOKEN_PREVIEW_LIMIT = 3
_SENSITIVE_VALUE_PATTERNS = (
    re.compile(r"(?i)\b([A-Z0-9_]*(?:token|secret|password|passphrase|api[_-]?key)[A-Z0-9_]*)=([^\s,;]+)"),
    re.compile(r"(?i)\b(authorization)\s*:\s*([^\s,;]+)"),
    re.compile(r"(?i)\b(bearer)\s+([A-Za-z0-9._-]+)"),
)


@dataclass(frozen=True)
class AcquisitionArtifact:
    job_type: str
    source_url: str
    artifact_path: str
    payload: dict[str, Any]


class AcquisitionDispatcherError(RuntimeError):
    def __init__(
        self,
        *,
        code: str,
        user_message: str,
        detail: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(detail)
        self.code = str(code).strip() or "acquisition-dispatcher-error"
        self.user_message = str(user_message).strip() or "I couldn't acquire the requested artifact."
        self.detail = str(detail).strip() or self.code
        self.metadata = dict(metadata or {})


class PrivilegedAcquisitionDispatcher:
    def __init__(
        self,
        *,
        mode: str = ACQUISITION_MODE_INLINE,
        allowed_job_types: Sequence[str] = DEFAULT_ALLOWED_JOB_TYPES,
        artifact_root: str = ".channel_runtime/acquisitions",
        command: str = "",
        command_timeout_s: float = DEFAULT_COMMAND_TIMEOUT_S,
        youtube_transcript_fetcher: Callable[[str], YouTubeTranscript] | None = None,
    ) -> None:
        normalized_mode = str(mode).strip().lower()
        if normalized_mode not in {
            ACQUISITION_MODE_DISABLED,
            ACQUISITION_MODE_INLINE,
            ACQUISITION_MODE_COMMAND,
        }:
            raise ValueError("unsupported acquisition mode")
        self._mode = normalized_mode
        self._allowed_job_types = tuple(
            sorted({str(job_type).strip().lower() for job_type in allowed_job_types if str(job_type).strip()})
        )
        self._artifact_root = Path(str(artifact_root).strip() or ".channel_runtime/acquisitions")
        self._command = str(command).strip()
        self._command_timeout_s = float(command_timeout_s)
        self._youtube_transcript_fetcher = youtube_transcript_fetcher or fetch_youtube_transcript

    def prepare_message_text(self, message_text: str) -> str:
        source_url = extract_first_youtube_url(message_text)
        if source_url is None:
            return str(message_text)
        if self._mode == ACQUISITION_MODE_DISABLED or YOUTUBE_TRANSCRIPT_JOB not in self._allowed_job_types:
            return str(message_text)
        artifact = self.acquire_youtube_transcript(message_text=message_text, source_url=source_url)
        transcript = transcript_from_mapping(artifact.payload)
        return format_codex_prompt(message_text=message_text, transcript=transcript)

    def acquire_youtube_transcript(self, *, message_text: str, source_url: str) -> AcquisitionArtifact:
        if self._mode == ACQUISITION_MODE_INLINE:
            transcript = self._youtube_transcript_fetcher(source_url)
            return self._write_artifact(
                job_type=YOUTUBE_TRANSCRIPT_JOB,
                source_url=source_url,
                payload=transcript_to_dict(transcript),
            )
        if self._mode == ACQUISITION_MODE_COMMAND:
            return self._run_command_job(
                job_type=YOUTUBE_TRANSCRIPT_JOB,
                request_payload={
                    "job_type": YOUTUBE_TRANSCRIPT_JOB,
                    "source_url": source_url,
                    "message_text": str(message_text),
                },
            )
        raise AcquisitionDispatcherError(
            code="acquisition-disabled",
            user_message="I found the YouTube link, but privileged acquisition is disabled on this host.",
            detail="youtube transcript acquisition requested while acquisition mode disabled",
        )

    def _run_command_job(self, *, job_type: str, request_payload: dict[str, Any]) -> AcquisitionArtifact:
        if not self._command:
            raise AcquisitionDispatcherError(
                code="acquisition-command-missing",
                user_message="I found the YouTube link, but transcript acquisition is not configured on this host.",
                detail=f"command-mode acquisition requested for {job_type} without CHANNEL_ACQUISITION_COMMAND",
                metadata={
                    "job_type": job_type,
                    "mode": ACQUISITION_MODE_COMMAND,
                },
            )
        try:
            command_argv = shlex.split(self._command)
        except ValueError as exc:
            command_display = _format_command_for_diagnostics(())
            raise AcquisitionDispatcherError(
                code="acquisition-command-invalid-config",
                user_message=(
                    "I found the YouTube link, but transcript acquisition is misconfigured on this host. "
                    f"Command: {command_display}."
                ),
                detail=(
                    f"command-mode acquisition command invalid for {job_type}: "
                    f"{command_display}; parse_error={_sanitize_text(str(exc))}"
                ),
                metadata={
                    "job_type": job_type,
                    "mode": ACQUISITION_MODE_COMMAND,
                    "command": command_display,
                    "parse_error": _sanitize_text(str(exc)),
                },
            ) from exc
        command_display = _format_command_for_diagnostics(command_argv)
        try:
            completed = subprocess.run(
                command_argv,
                input=json.dumps(request_payload, sort_keys=True),
                capture_output=True,
                text=True,
                timeout=self._command_timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise AcquisitionDispatcherError(
                code="acquisition-command-timeout",
                user_message=(
                    "I found the YouTube link, but transcript acquisition timed out on this host. "
                    f"Command: {command_display}."
                ),
                detail=f"command-mode acquisition timed out for {job_type}",
                metadata={
                    "job_type": job_type,
                    "mode": ACQUISITION_MODE_COMMAND,
                    "command": command_display,
                    "timeout_s": self._command_timeout_s,
                },
            ) from exc
        except OSError as exc:
            error_summary = f"{type(exc).__name__}: {_sanitize_text(str(exc))}"
            raise AcquisitionDispatcherError(
                code="acquisition-command-exec-failed",
                user_message=(
                    "I found the YouTube link, but transcript acquisition could not start on this host. "
                    f"Command: {command_display}. Error: {error_summary}"
                ),
                detail=(
                    f"command-mode acquisition failed to start for {job_type}: "
                    f"{command_display}; os_error={error_summary}"
                ),
                metadata={
                    "job_type": job_type,
                    "mode": ACQUISITION_MODE_COMMAND,
                    "command": command_display,
                    "exception_type": type(exc).__name__,
                },
            ) from exc
        if completed.returncode != 0:
            helper_error = _parse_helper_error_payload(completed.stderr)
            stderr_summary = _format_stream_summary(label="stderr", value=completed.stderr)
            stdout_summary = _format_stream_summary(label="stdout", value=completed.stdout)
            user_message = "I found the YouTube link, but transcript acquisition failed on this host."
            if helper_error is not None:
                helper_code = helper_error.get("code") or "unknown-helper-error"
                helper_detail = helper_error.get("detail") or "no helper detail provided"
                user_message = (
                    f"{user_message} Helper code: {helper_code}. "
                    f"Detail: {_truncate_text(str(helper_detail), limit=220)}"
                )
            elif stderr_summary:
                user_message = f"{user_message} Detail: {stderr_summary}"
            else:
                user_message = (
                    f"{user_message} Command: {command_display}. Return code: {completed.returncode}."
                )
            raise AcquisitionDispatcherError(
                code="acquisition-command-failed",
                user_message=user_message,
                detail=(
                    f"command-mode acquisition failed for {job_type}: "
                    f"{command_display}; exit_code={completed.returncode}; {stderr_summary}"
                ),
                metadata={
                    "job_type": job_type,
                    "mode": ACQUISITION_MODE_COMMAND,
                    "command": command_display,
                    "returncode": completed.returncode,
                    "stderr": stderr_summary,
                    "stdout": stdout_summary,
                    "helper_error": helper_error,
                },
            )
        try:
            payload = json.loads((completed.stdout or "").strip() or "{}")
        except json.JSONDecodeError as exc:
            raise AcquisitionDispatcherError(
                code="acquisition-command-invalid-json",
                user_message=(
                    "I found the YouTube link, but transcript acquisition returned invalid output. "
                    f"Command: {command_display}."
                ),
                detail=(
                    f"command-mode acquisition returned invalid json for {job_type}: "
                    f"{command_display}; { _format_stream_summary(label='stdout', value=completed.stdout) }; "
                    f"{ _format_stream_summary(label='stderr', value=completed.stderr) }; "
                    f"parse_error={_sanitize_text(str(exc))}"
                ),
                metadata={
                    "job_type": job_type,
                    "mode": ACQUISITION_MODE_COMMAND,
                    "command": command_display,
                    "stdout": _format_stream_summary(label="stdout", value=completed.stdout),
                    "stderr": _format_stream_summary(label="stderr", value=completed.stderr),
                },
            ) from exc
        if not isinstance(payload, Mapping):
            raise AcquisitionDispatcherError(
                code="acquisition-command-invalid-payload",
                user_message="I found the YouTube link, but transcript acquisition returned an invalid payload.",
                detail=(
                    f"command-mode acquisition returned non-mapping payload for {job_type}: "
                    f"{command_display}; stdout_shape={type(payload).__name__}"
                ),
                metadata={
                    "job_type": job_type,
                    "mode": ACQUISITION_MODE_COMMAND,
                    "command": command_display,
                    "stdout": _format_stream_summary(label="stdout", value=completed.stdout),
                },
            )
        artifact_payload = payload.get("artifact")
        if str(payload.get("job_type", "")).strip().lower() != job_type or not isinstance(artifact_payload, Mapping):
            raise AcquisitionDispatcherError(
                code="acquisition-command-invalid-payload",
                user_message="I found the YouTube link, but transcript acquisition returned an invalid payload.",
                detail=(
                    f"command-mode acquisition payload schema invalid for {job_type}: "
                    f"{command_display}; payload_keys={_format_payload_keys(payload)}; "
                    f"artifact_shape={type(artifact_payload).__name__}"
                ),
                metadata={
                    "job_type": job_type,
                    "mode": ACQUISITION_MODE_COMMAND,
                    "command": command_display,
                    "stdout": _format_stream_summary(label="stdout", value=completed.stdout),
                },
            )
        source_url = str(artifact_payload.get("source_url", request_payload.get("source_url", ""))).strip()
        return self._write_artifact(
            job_type=job_type,
            source_url=source_url,
            payload=dict(artifact_payload),
        )

    def _write_artifact(self, *, job_type: str, source_url: str, payload: dict[str, Any]) -> AcquisitionArtifact:
        job_dir = self._artifact_root / job_type
        job_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(source_url.encode("utf-8")).hexdigest()[:16]
        artifact_path = job_dir / f"{int(time.time())}-{digest}.json"
        artifact_payload = {
            "job_type": job_type,
            "source_url": source_url,
            "artifact_created_at": int(time.time()),
            "artifact_version": 1,
            **payload,
        }
        artifact_path.write_text(json.dumps(artifact_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return AcquisitionArtifact(
            job_type=job_type,
            source_url=source_url,
            artifact_path=str(artifact_path),
            payload=artifact_payload,
        )


def _format_command_for_diagnostics(command_argv: Sequence[str]) -> str:
    if not command_argv:
        return "<empty>"
    preview_tokens = [_sanitize_text(token) for token in command_argv[:_COMMAND_TOKEN_PREVIEW_LIMIT]]
    if len(command_argv) > _COMMAND_TOKEN_PREVIEW_LIMIT:
        preview_tokens.append("...")
    return f"argv0={_sanitize_text(command_argv[0])}; argc={len(command_argv)}; argv_preview={' '.join(preview_tokens)}"


def _truncate_text(value: str, *, limit: int = 400) -> str:
    text = str(value).strip()
    if len(text) <= limit:
        return text
    return f"{text[: max(0, limit - 3)]}..."


def _sanitize_text(value: str, *, limit: int = _DIAGNOSTIC_PREVIEW_LIMIT) -> str:
    text = " ".join(str(value).split())
    for pattern in _SENSITIVE_VALUE_PATTERNS:
        if pattern.pattern.lower().startswith("(?i)\\b(bearer)"):
            text = pattern.sub(r"\1 <redacted>", text)
        else:
            text = pattern.sub(r"\1=<redacted>", text)
    return _truncate_text(text, limit=limit)


def _format_stream_summary(*, label: str, value: str | None) -> str:
    text = str(value or "").strip()
    if not text:
        return f"{label}_shape=empty"
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        hint = _infer_stream_hint(text)
        parts = [f"{label}_shape=text", f"{label}_preview={_sanitize_text(text)}"]
        if hint:
            parts.append(f"{label}_hint={hint}")
        return "; ".join(parts)
    if isinstance(payload, Mapping):
        code = _sanitize_text(str(payload.get('code', '')).strip() or "unknown")
        detail = _sanitize_text(str(payload.get('detail', '')).strip() or "<empty>")
        return f"{label}_shape=json-object; {label}_code={code}; {label}_detail={detail}"
    return f"{label}_shape=json-{type(payload).__name__}; {label}_preview={_sanitize_text(text)}"


def _infer_stream_hint(text: str) -> str:
    normalized = text.lower()
    if "modulenotfounderror" in normalized or "importerror" in normalized:
        return "python-import-error"
    if "permission denied" in normalized:
        return "permission-error"
    if "not found" in normalized or "no such file" in normalized:
        return "missing-command-or-dependency"
    return ""


def _format_payload_keys(payload: Mapping[str, Any]) -> str:
    keys = sorted(str(key) for key in payload.keys())
    preview = keys[:5]
    if len(keys) > 5:
        preview.append("...")
    return ",".join(preview) or "<empty>"


def _parse_helper_error_payload(raw_stderr: str) -> dict[str, str] | None:
    stderr = str(raw_stderr or "").strip()
    if not stderr:
        return None
    try:
        payload = json.loads(stderr)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, Mapping):
        return None
    code = str(payload.get("code", "")).strip()
    detail = str(payload.get("detail", "")).strip()
    if not code and not detail:
        return None
    return {
        "code": code,
        "detail": _sanitize_text(detail, limit=220),
    }
