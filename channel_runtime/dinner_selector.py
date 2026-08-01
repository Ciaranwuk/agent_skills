from __future__ import annotations

import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from channel_core.contracts import InboundMessage, OutboundMessage

_DINNER_COMMANDS = frozenset({"/dinner", "/dinners"})
_DEFAULT_DINNER_COUNT = 5
_REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MEAL_OPTIONS_PATH = Path("data/meal_options.json")


class DinnerSelectorError(ValueError):
    """Raised for deterministic dinner-selector input/configuration failures."""


@dataclass(frozen=True)
class DinnerSelection:
    meals: tuple[str, ...]
    expanded_repeats: bool = False


def build_dinner_command_response(
    *,
    inbound: InboundMessage,
    session_id: str,
    meal_options_path: str | Path = DEFAULT_MEAL_OPTIONS_PATH,
    orchestrator_mode: str,
    rng: random.Random | None = None,
) -> OutboundMessage | None:
    command = str(inbound.text).strip()
    if not command:
        return None
    try:
        command_name, count = _parse_dinner_command(command)
    except DinnerSelectorError as exc:
        return OutboundMessage(
            chat_id=inbound.chat_id,
            text=str(exc),
            reply_to_message_id=inbound.message_id,
            metadata={"session_id": session_id, "orchestrator_mode": orchestrator_mode},
        )
    if command_name is None:
        return None

    metadata = {"session_id": session_id, "orchestrator_mode": orchestrator_mode}
    try:
        meals = load_meal_options(meal_options_path)
        selection = select_dinners(meals=meals, count=count, rng=rng)
    except DinnerSelectorError as exc:
        return OutboundMessage(
            chat_id=inbound.chat_id,
            text=str(exc),
            reply_to_message_id=inbound.message_id,
            metadata=metadata,
        )

    lines = [f"Dinner plan ({len(selection.meals)} dinners):"]
    if selection.expanded_repeats:
        lines[0] += " repeats expanded because the meal list is smaller than the requested count."
    for index, meal in enumerate(selection.meals, start=1):
        lines.append(f"{index}. {meal}")
    return OutboundMessage(
        chat_id=inbound.chat_id,
        text="\n".join(lines),
        reply_to_message_id=inbound.message_id,
        metadata=metadata,
    )


def load_meal_options(meal_options_path: str | Path) -> tuple[str, ...]:
    path = _resolve_meal_options_path(meal_options_path)
    if not path.exists():
        raise DinnerSelectorError(
            "Dinner selector is not configured yet. "
            f"Add meal names to {path} under the 'meals' array, then run /dinners again."
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise DinnerSelectorError(
            "Dinner selector could not read the meal list. "
            f"Expected JSON like {{\"meals\": [\"Chili\", \"Curry\"]}} at {path}."
        ) from exc
    if not isinstance(payload, dict):
        raise DinnerSelectorError(
            "Dinner selector could not read the meal list. "
            f"Expected a JSON object with a 'meals' array at {path}."
        )
    raw_meals = payload.get("meals")
    if not isinstance(raw_meals, list):
        raise DinnerSelectorError(
            "Dinner selector could not read the meal list. "
            f"Expected a 'meals' array at {path}."
        )
    meals = _normalize_meals(raw_meals)
    if not meals:
        raise DinnerSelectorError(
            "Dinner selector found no usable meals. "
            f"Add at least one non-empty meal name to {path} under the 'meals' array."
        )
    return meals


def select_dinners(
    *,
    meals: Sequence[str],
    count: int,
    rng: random.Random | None = None,
) -> DinnerSelection:
    normalized_meals = _normalize_meals(meals)
    if not normalized_meals:
        raise DinnerSelectorError("Dinner selector needs at least one meal option.")
    if int(count) < 1:
        raise DinnerSelectorError("Dinner selector count must be a positive integer.")

    randomizer = rng or random.Random()
    ordered = list(normalized_meals)
    randomizer.shuffle(ordered)
    if count <= len(ordered):
        return DinnerSelection(meals=tuple(ordered[:count]))

    selected = list(ordered)
    remaining = count - len(selected)
    if remaining == 1:
        selected.append(randomizer.choice(ordered))
        return DinnerSelection(meals=tuple(selected))

    if remaining > 0:
        index = 0
        while remaining > 0:
            selected.append(ordered[index % len(ordered)])
            index += 1
            remaining -= 1
        return DinnerSelection(meals=tuple(selected), expanded_repeats=True)

    return DinnerSelection(meals=tuple(selected))


def _parse_dinner_command(command: str) -> tuple[str | None, int]:
    parts = command.strip().split()
    if not parts:
        return None, _DEFAULT_DINNER_COUNT
    command_name = parts[0].lower()
    if command_name not in _DINNER_COMMANDS:
        return None, _DEFAULT_DINNER_COUNT
    if len(parts) == 1:
        return command_name, _DEFAULT_DINNER_COUNT
    if len(parts) != 2:
        raise DinnerSelectorError("Usage: /dinners or /dinners <count>")
    try:
        count = int(parts[1])
    except ValueError as exc:
        raise DinnerSelectorError("Usage: /dinners or /dinners <count>") from exc
    if count < 1:
        raise DinnerSelectorError("Usage: /dinners or /dinners <count>")
    return command_name, count


def _normalize_meals(meals: Sequence[object]) -> tuple[str, ...]:
    seen: set[str] = set()
    normalized: list[str] = []
    for meal in meals:
        text = str(meal).strip()
        if not text:
            continue
        lowered = text.casefold()
        if lowered in seen:
            continue
        seen.add(lowered)
        normalized.append(text)
    return tuple(normalized)


def _resolve_meal_options_path(meal_options_path: str | Path) -> Path:
    path = Path(meal_options_path)
    if path.is_absolute():
        return path
    return (_REPO_ROOT / path).resolve()
