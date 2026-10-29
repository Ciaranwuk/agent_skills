from __future__ import annotations

import os
from pathlib import Path

ENV_FILE_OVERRIDE_VAR = "AGENT_SKILLS_ENV_FILE"
DEFAULT_ENV_FILE_NAME = ".env.local"


def _default_env_files() -> list[Path]:
    repo_root = Path(__file__).resolve().parents[1]
    return [repo_root / DEFAULT_ENV_FILE_NAME]


def candidate_env_files() -> list[Path]:
    override = str(os.environ.get(ENV_FILE_OVERRIDE_VAR, "")).strip()
    if override:
        return [Path(override).expanduser()]
    return _default_env_files()


def load_env_file_values() -> dict[str, str]:
    values: dict[str, str] = {}
    for path in candidate_env_files():
        if not path.is_file():
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for raw_line in lines:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export ") :].strip()
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if not key or key in values:
                continue
            normalized = value.strip()
            if len(normalized) >= 2 and normalized[:1] == normalized[-1:] and normalized[:1] in {"'", '"'}:
                normalized = normalized[1:-1]
            values[key] = normalized
    return values


def first_env_value(*names: str) -> str:
    for name in names:
        value = str(os.environ.get(name, "")).strip()
        if value:
            return value
    file_values = load_env_file_values()
    for name in names:
        value = str(file_values.get(name, "")).strip()
        if value:
            return value
    return ""
