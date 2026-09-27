"""Fixtures for the one-attempt tests: run ``run_attempt`` with a scripted ORCA run."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from orca_auto.orca.attempt.run import run_attempt
from orca_auto.orca.config import AppConfig
from orca_auto.orca.orca_runner import RunResult
from orca_auto.orca.state import new_state
from orca_auto.orca.types import RunState
from tests.conftest import make_orca_runner, make_run_context


@pytest.fixture
def attempt(
    tmp_path: Path, app_cfg: Callable[..., AppConfig], monkeypatch: pytest.MonkeyPatch
) -> Callable[..., int]:
    """``attempt(selected_inp, run, *, resumed=False, state=None)`` runs ``run_attempt``.

    The job directory is ``tmp_path``. The runner is a real ``OrcaRunner`` whose
    ``run`` is ``run``; the state defaults to a new one for ``selected_inp``.
    """

    cfg = app_cfg()

    def run_one(
        selected_inp: Path,
        run: Callable[[Path], RunResult],
        *,
        resumed: bool = False,
        state: RunState | None = None,
    ) -> int:
        runner = make_orca_runner(cfg.paths.orca_executable, tmp_path)
        monkeypatch.setattr(runner, "run", run)
        return run_attempt(
            make_run_context(cfg, tmp_path, selected_inp),
            state if state is not None else new_state(tmp_path, selected_inp),
            runner,
            resumed=resumed,
        )

    return run_one
