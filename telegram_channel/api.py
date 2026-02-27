from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping
from urllib import error, request


@dataclass(frozen=True)
class TelegramApiError(RuntimeError):
    """Deterministic structured error raised by TelegramApiClient."""

    operation: str
    kind: str
    transient: bool
    description: str
    status_code: int | None = None
    error_code: int | None = None

    def __str__(self) -> str:
        parts = [f"operation={self.operation}", f"kind={self.kind}", f"transient={self.transient}"]
        if self.status_code is not None:
            parts.append(f"status_code={self.status_code}")
        if self.error_code is not None:
            parts.append(f"error_code={self.error_code}")
        parts.append(f"description={self.description}")
        return "TelegramApiError(" + ", ".join(parts) + ")"

    def to_dict(self) -> dict[str, object]:
        return {
            "operation": self.operation,
            "kind": self.kind,
            "transient": self.transient,
            "description": self.description,
            "status_code": self.status_code,
            "error_code": self.error_code,
        }


class TelegramApiClient:
    """Minimal Telegram Bot API wrapper with bounded retry/backoff."""

    def __init__(
        self,
        token: str,
        *,
        timeout_s: float = 10.0,
        max_retries: int = 2,
        backoff_seconds: Iterable[float] = (0.25, 0.5),
        opener: Callable[..., Any] | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        token_value = str(token).strip()
        if not token_value:
            raise ValueError("token must be a non-empty string")
        if timeout_s <= 0:
            raise ValueError("timeout_s must be a positive number")
        if max_retries < 0:
            raise ValueError("max_retries must be >= 0")

        normalized_backoff = tuple(float(value) for value in backoff_seconds)
        if any(value < 0 for value in normalized_backoff):
            raise ValueError("backoff_seconds values must be >= 0")

        self._token = token_value
        self._timeout_s = float(timeout_s)
        self._max_retries = int(max_retries)
        self._backoff_seconds = normalized_backoff or (0.0,)
        self._opener = opener or request.urlopen
        self._sleeper = sleeper or time.sleep

    def get_updates(
        self,
        *,
        offset: int | None = None,
        timeout_s: int = 0,
        limit: int = 100,
        allowed_updates: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"timeout": int(timeout_s), "limit": int(limit)}
        if offset is not None:
            payload["offset"] = int(offset)
        if allowed_updates is not None:
            payload["allowed_updates"] = list(allowed_updates)

        result = self._request("getUpdates", payload)
        if not isinstance(result, list):
            raise TelegramApiError(
                operation="getUpdates",
                kind="invalid-result-shape",
                transient=False,
                description="result must be a list",
            )
        return [item for item in result if isinstance(item, dict)]

    def send_message(
        self,
        *,
        chat_id: str | int,
        text: str,
        reply_to_message_id: str | int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if reply_to_message_id is not None:
            payload["reply_to_message_id"] = reply_to_message_id

        result = self._request("sendMessage", payload)
        if not isinstance(result, dict):
            raise TelegramApiError(
                operation="sendMessage",
                kind="invalid-result-shape",
                transient=False,
                description="result must be an object",
            )
        return result

    def _request(self, operation: str, payload: Mapping[str, Any]) -> Any:
        attempts = self._max_retries + 1
        last_error: TelegramApiError | None = None

        for attempt in range(attempts):
            try:
                raw_body = self._post_json(operation, payload)
                parsed = self._decode_json(operation, raw_body)
                return self._extract_result(operation, parsed)
            except TelegramApiError as exc:
                last_error = exc
                should_retry = exc.transient and attempt < self._max_retries
                if not should_retry:
                    raise
                self._sleeper(self._backoff_for_attempt(attempt))

        if last_error is None:
            raise TelegramApiError(
                operation=operation,
                kind="unknown",
                transient=False,
                description="request failed with no captured error",
            )
        raise last_error

    def _post_json(self, operation: str, payload: Mapping[str, Any]) -> bytes:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        req = request.Request(
            url=f"https://api.telegram.org/bot{self._token}/{operation}",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with self._opener(req, timeout=self._timeout_s) as response:
                return response.read()
        except error.HTTPError as exc:
            raw_body = b""
            try:
                raw_body = exc.read()
            except Exception:
                raw_body = b""
            parsed = _try_parse_json(raw_body)
            description = _extract_description(parsed) or str(exc.reason or "HTTP error")
            api_error_code = _extract_error_code(parsed)
            transient = bool(exc.code >= 500 or exc.code == 429)
            raise TelegramApiError(
                operation=operation,
                kind="http-error",
                transient=transient,
                description=description,
                status_code=int(exc.code),
                error_code=api_error_code,
            ) from exc
        except error.URLError as exc:
            raise TelegramApiError(
                operation=operation,
                kind="network-error",
                transient=True,
                description=str(exc.reason),
            ) from exc
        except TimeoutError as exc:
            raise TelegramApiError(
                operation=operation,
                kind="timeout",
                transient=True,
                description=str(exc),
            ) from exc
        except OSError as exc:
            raise TelegramApiError(
                operation=operation,
                kind="network-error",
                transient=True,
                description=str(exc),
            ) from exc

    def _decode_json(self, operation: str, raw_body: bytes) -> Any:
        try:
            return json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TelegramApiError(
                operation=operation,
                kind="invalid-json",
                transient=False,
                description="response body is not valid JSON",
            ) from exc

    def _extract_result(self, operation: str, payload: Any) -> Any:
        if not isinstance(payload, dict):
            raise TelegramApiError(
                operation=operation,
                kind="invalid-response-shape",
                transient=False,
                description="response body must be an object",
            )

        if payload.get("ok") is not True:
            api_error_code = _extract_error_code(payload)
            transient = bool(api_error_code == 429 or (api_error_code is not None and api_error_code >= 500))
            raise TelegramApiError(
                operation=operation,
                kind="api-error",
                transient=transient,
                description=_extract_description(payload) or "telegram api returned ok=false",
                error_code=api_error_code,
            )

        return payload.get("result")

    def _backoff_for_attempt(self, attempt: int) -> float:
        if attempt < len(self._backoff_seconds):
            return self._backoff_seconds[attempt]
        return self._backoff_seconds[-1]


def _try_parse_json(raw_body: bytes) -> Any:
    if not raw_body:
        return None
    try:
        return json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def _extract_description(payload: Any) -> str | None:
    if isinstance(payload, dict):
        description = payload.get("description")
        if description is None:
            return None
        return str(description)
    return None


def _extract_error_code(payload: Any) -> int | None:
    if isinstance(payload, dict) and "error_code" in payload:
        try:
            return int(payload["error_code"])
        except (TypeError, ValueError):
            return None
    return None
