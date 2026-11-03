from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from orca_auto.machine_contracts import ContractError, validate_machine_path
from orca_auto.orca import machine_observation as machine


def validate_common_machine(path: Path) -> None:
    """Validate ``path`` with the source-owned machine-observation v1 validator.

    ``orca_auto.machine_contracts`` checks the envelope, the ORCA route and every
    available artifact receipt; no external clone, git or network is used. A
    rejection or a missing ``jsonschema`` fails the test; the check is never
    skipped.
    """
    try:
        validate_machine_path(path)
    except ImportError as exc:
        pytest.fail(
            f"{sys.executable} cannot import jsonschema, which the machine-contracts validator "
            "needs. Run scripts/check.sh without ORCA_AUTO_CHECK_SKIP_INSTALL=1, or install "
            f"the dev extras: python -m pip install -c constraints-dev.txt -e '.[dev]' ({exc})"
        )
    except ContractError as exc:
        pytest.fail(f"machine-observation v1 rejects {path}:\n{exc}")
    except (OSError, ValueError) as exc:
        pytest.fail(f"machine-observation v1 cannot read {path}: {type(exc).__name__}: {exc}")


def build_test_observation(
    tmp_path: Path, inp: bytes, out: bytes, status: str = "completed"
) -> dict[str, Any]:
    selected = tmp_path / "job.inp"
    output = tmp_path / "job.out"
    selected.write_bytes(inp)
    output.write_bytes(out)
    return machine.build_machine_observation(
        tmp_path,
        {
            "job": {"id": "test-science"},
            "input": {"primary_path": str(selected)},
            "status": {"state": status},
            "engine_payload": {
                "final_result": {"last_out_path": str(output), "reason": "normal_termination"},
            },
        },
    )
