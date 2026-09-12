from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from orca_auto.core.utils import normalize_text


@dataclass(frozen=True)
class InternalEngineCommandResult:
    status: str
    command_argv: list[str]
    returncode: int
    reason: str = ""
    stdout: str = ""
    stderr: str = ""
    parsed_stdout: dict[str, str] = field(default_factory=dict)
    job_id: str = ""
    queue_id: str = ""
    job_dir: str = ""
    extra_fields: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": self.status,
            "reason": self.reason,
            "returncode": self.returncode,
            "command_argv": list(self.command_argv),
            "stdout": self.stdout,
            "stderr": self.stderr,
            "parsed_stdout": dict(self.parsed_stdout),
            "job_id": self.job_id,
            "queue_id": self.queue_id,
        }
        if self.job_dir:
            payload["job_dir"] = self.job_dir
        payload.update(self.extra_fields)
        return payload


def internal_call_argv(
    *,
    api_name: str,
    config_path: str,
    kwargs: dict[str, Any],
) -> list[str]:
    return [
        api_name,
        f"config={config_path}",
        *[f"{key}={value}" for key, value in kwargs.items()],
    ]


def _stderr_with_exception(stderr: str, exc: Exception) -> str:
    pieces = [stderr] if stderr else []
    if pieces and not pieces[-1].endswith("\n"):
        pieces[-1] += "\n"
    pieces.append(f"{exc.__class__.__name__}: {exc}\n")
    return "".join(pieces)


def _key_value_stdout(fields: dict[str, str]) -> str:
    return "\n".join(f"{key}: {value}" for key, value in fields.items() if value)


def _text_fields(fields: dict[str, Any]) -> dict[str, str]:
    return {key: text for key, value in fields.items() if (text := normalize_text(value))}
