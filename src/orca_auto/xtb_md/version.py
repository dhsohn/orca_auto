from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

SUPPORTED_XTB_MD_VERSIONS = frozenset({"6.7.1"})
XTB_STABLE_RELEASE_TAG = "v6.7.1"
XTB_STABLE_LINUX_ARCHIVE_SHA256 = "62a8d18778286e815292ee53d76ce447daf460a4dea3782c0f25cbac7019b5df"
_VERSION_RE = re.compile(r"\bxtb\s+version\s+(\d+\.\d+\.\d+)\b", re.IGNORECASE)


def probe_xtb_version(
    executable: str | Path,
    *,
    timeout_seconds: float = 10.0,
    run_fn: Callable[..., Any] = subprocess.run,
    env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    resolved = Path(executable).expanduser().resolve()
    completed = run_fn(
        [str(resolved), "--version"],
        check=False,
        capture_output=True,
        text=True,
        timeout=max(0.1, float(timeout_seconds)),
        env=dict(env) if env is not None else None,
    )
    stdout = str(getattr(completed, "stdout", "") or "")
    stderr = str(getattr(completed, "stderr", "") or "")
    output = f"{stdout}\n{stderr}"[:64_000]
    if int(getattr(completed, "returncode", 1)) != 0:
        raise ValueError("Configured xTB executable failed its bounded version probe")
    match = _VERSION_RE.search(output)
    if match is None:
        raise ValueError("Configured xTB executable did not report a parseable version")
    version = match.group(1)
    if version not in SUPPORTED_XTB_MD_VERSIONS:
        supported = ", ".join(sorted(SUPPORTED_XTB_MD_VERSIONS))
        raise ValueError(
            f"xTB-MD supports stable xTB version {supported}; configured executable reports {version}"
        )
    return {
        "version": version,
        "release_tag": XTB_STABLE_RELEASE_TAG,
        "archive_sha256": XTB_STABLE_LINUX_ARCHIVE_SHA256,
    }


__all__ = [
    "SUPPORTED_XTB_MD_VERSIONS",
    "XTB_STABLE_LINUX_ARCHIVE_SHA256",
    "XTB_STABLE_RELEASE_TAG",
    "probe_xtb_version",
]
