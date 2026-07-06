"""Direct queue submission error handling."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import orca_auto.orca.commands.run_inp_submission as submission_mod


def _deps(context: Any) -> SimpleNamespace:
    return SimpleNamespace(
        submission=SimpleNamespace(
            resolve_submission_context=lambda _args: context,
            queue_adapter=SimpleNamespace(
                get_active_entry_for_reaction_dir=lambda _root, _reaction_dir: None,
            ),
            active_direct_run_error=lambda _reaction_dir: None,
        )
    )


def test_submit_without_selectable_inp_fails_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A reaction dir without any .inp used to leak the ValueError from
    # resource-request resolution as a CLI traceback.
    context = SimpleNamespace(
        cfg=None,
        allowed_root=tmp_path,
        reaction_dir=tmp_path / "job",
        selected_inp=None,
    )

    def raise_value_error(*_args: Any, **_kwargs: Any) -> Any:
        raise ValueError("No .inp file selected for ORCA queue submission.")

    monkeypatch.setattr(submission_mod, "create_queued_submission", raise_value_error)

    result = submission_mod.submit_reaction_dir_to_queue(SimpleNamespace(), deps=_deps(context))

    assert result.status == "failed"
    assert result.reason == "invalid_submission_input"
    assert "No .inp file selected" in result.stderr
