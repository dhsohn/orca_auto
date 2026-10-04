from __future__ import annotations

import sys
from pathlib import Path

import pytest

from orca_auto.machine_contracts import ContractError, validate_machine_path


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
