from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_VALID_CALLS = """
from pathlib import Path
from orca_auto.flow.adapters._orca_contract_context import context_from_runtime
from orca_auto.flow.registry.journal import append_workflow_journal_event
from orca_auto.flow.runtime.models import WorkflowJournalWriter
from orca_auto.orca.job_locations._models import JobRuntimeContext

writer: WorkflowJournalWriter = append_workflow_journal_event
context_from_runtime(JobRuntimeContext())
writer(Path('/unused'), event_type='stage_changed', metadata={'source': 'test'})
writer('/unused', event_type='cancelled', event_id='stable-id', occurred_at='fixed-time')
"""

_INVALID_CALLS = """
context_from_runtime((None, None, None, {}, {}, None))
writer('/unused')
writer('/unused', event_type=1)
writer('/unused', event_type='changed', unsupported='value')
def incompatible_writer(root: int, *, event_type: str) -> None:
    pass
invalid_writer: WorkflowJournalWriter = incompatible_writer
"""


@pytest.mark.parametrize("invalid", [False, True])
def test_workflow_boundaries_reject_invalid_typed_callers(tmp_path: Path, invalid: bool) -> None:
    # These snippets are type-checked, never executed: no journal or queue writes.
    code = _VALID_CALLS + (_INVALID_CALLS if invalid else "")
    caller = tmp_path / "caller.py"
    caller.write_text(code, encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "mypy",
            "--strict",
            "--python-version",
            "3.11",
            "--follow-imports=silent",
            "--cache-dir",
            str(tmp_path / "mypy-cache"),
            "--show-error-codes",
            "--no-pretty",
            "--no-color-output",
            str(caller),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert not result.stderr
    if invalid:
        assert result.returncode == 1, result.stdout
        errors = [line for line in result.stdout.splitlines() if ": error:" in line]
        assert len(errors) == 5, result.stdout
        assert sum("[arg-type]" in line for line in errors) == 2
        assert sum("[call-arg]" in line for line in errors) == 2
        assert sum("[assignment]" in line for line in errors) == 1
    else:
        assert result.returncode == 0, result.stdout
