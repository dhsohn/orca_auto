from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from orca_auto.cli import main
from orca_auto.core.queue.types import QueueEntry, QueueStatus
from orca_auto.orca.queue.adapter import (
    list_queue,
    queue_entry_force,
    queue_entry_reaction_dir,
)
from orca_auto.orca.state_reading import state_path


@dataclass
class _SubmitEnv:
    allowed: Path
    config: Path
    counter: Path

    def reaction(self, *parts: str) -> Path:
        reaction = self.allowed.joinpath(*parts)
        reaction.mkdir(parents=True)
        (reaction / "rxn.inp").write_text(
            "! Opt\n* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*\n", encoding="utf-8"
        )
        return reaction

    def entries_for(self, reaction: Path) -> list[QueueEntry]:
        return [
            entry
            for entry in list_queue(self.allowed)
            if queue_entry_reaction_dir(entry) == str(reaction.resolve())
        ]


@pytest.fixture
def submit_env(
    tmp_path: Path,
    make_fake_orca: Callable[..., Path],
    config_path: Callable[..., Path],
) -> _SubmitEnv:
    """A runs root, a config, and a fake ORCA that records each launch in ``counter``."""

    counter = tmp_path / "fake_orca_counter.txt"
    fake_orca = make_fake_orca(
        f"#!/bin/sh\necho run >> {counter}\necho '****ORCA TERMINATED NORMALLY****'\nexit 0\n"
    )
    allowed = tmp_path / "orca_runs"
    config = config_path(runs_root=allowed, orca_executable=fake_orca)
    return _SubmitEnv(allowed=allowed, config=config, counter=counter)


def test_run_inp_submit_only_enqueues_without_executing_orca(
    submit_env: _SubmitEnv, capsys: pytest.CaptureFixture[str]
) -> None:
    reaction = submit_env.reaction("project_a", "rxn_queue_demo")

    rc = main(["run-dir", str(reaction), "--config", str(submit_env.config)])

    assert rc == 0
    assert "status: queued" in capsys.readouterr().out
    assert not submit_env.counter.exists()
    queue_entries = submit_env.entries_for(reaction)
    assert len(queue_entries) == 1
    assert queue_entries[0].status == QueueStatus.PENDING
    assert not state_path(reaction).exists()


def test_force_submit_preserves_force_flag_in_queue_entry(
    submit_env: _SubmitEnv, capsys: pytest.CaptureFixture[str]
) -> None:
    reaction = submit_env.reaction("rxn_force_queue")

    rc = main(["run-dir", str(reaction), "--config", str(submit_env.config), "--force"])

    assert rc == 0
    assert "status: queued" in capsys.readouterr().out
    queue_entries = submit_env.entries_for(reaction)
    assert len(queue_entries) == 1
    assert queue_entry_force(queue_entries[0])
    assert not submit_env.counter.exists()


def test_existing_completed_output_is_queued_for_worker_reconciliation(
    submit_env: _SubmitEnv,
) -> None:
    reaction = submit_env.reaction("rxn_completed_skip")
    (reaction / "rxn.out").write_text("****ORCA TERMINATED NORMALLY****\n", encoding="utf-8")

    rc = main(["run-dir", str(reaction), "--config", str(submit_env.config)])

    assert rc == 0
    assert not submit_env.counter.exists()
    assert len(submit_env.entries_for(reaction)) == 1
    assert not state_path(reaction).exists()
