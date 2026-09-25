from __future__ import annotations

import importlib.util
import io
import os
import re
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

import pytest

CI_WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"


def machine_contract_pin() -> str:
    """Return the machine-contracts commit that CI checks out for this suite."""
    pins = re.findall(
        r"repository: dhsohn/machine-contracts\n\s+ref: ([0-9a-f]{40})\n",
        CI_WORKFLOW.read_text(encoding="utf-8"),
    )
    if len(pins) != 1:
        pytest.fail(f"{CI_WORKFLOW} must pin exactly one full dhsohn/machine-contracts commit")
    return pins[0]


def validate_common_machine(path: Path) -> None:
    """Validate ``path`` with the machine-contracts validator at the CI pin.

    The validator is read out of the pinned commit of a machine-contracts clone
    (``FACTORY_MACHINE_CONTRACT_REPO``, default ``~/machine_contracts``), never
    from the clone's working tree. A missing clone, pin or validator dependency
    fails the test; the check is never skipped.
    """
    pin = machine_contract_pin()
    repo = Path(
        os.environ.get("FACTORY_MACHINE_CONTRACT_REPO") or Path.home() / "machine_contracts"
    )
    if importlib.util.find_spec("jsonschema") is None:
        pytest.fail(
            f"{sys.executable} cannot import jsonschema, which the machine-contracts validator "
            "needs. Run scripts/check.sh without ORCA_AUTO_CHECK_SKIP_INSTALL=1, or install "
            "the dev extras: python -m pip install -c constraints-dev.txt -e '.[dev]'"
        )
    archive = subprocess.run(
        ["git", "-C", str(repo), "archive", pin], capture_output=True, check=False
    )
    if archive.returncode != 0:
        pytest.fail(
            f"machine-contracts clone {repo} cannot provide CI pin {pin}: "
            f"{archive.stderr.decode('utf-8', 'replace').strip()}\n"
            "Clone dhsohn/machine-contracts there and fetch, or set FACTORY_MACHINE_CONTRACT_REPO."
        )
    with tempfile.TemporaryDirectory() as snapshot:
        with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as contract:
            contract.extractall(snapshot, filter="data")
        validation = subprocess.run(
            [
                sys.executable,
                str(Path(snapshot) / "scripts" / "validate.py"),
                "--machine",
                str(path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    if validation.returncode != 0:
        pytest.fail(
            f"machine-contracts {pin} rejects {path}:\n{validation.stderr}{validation.stdout}"
        )
