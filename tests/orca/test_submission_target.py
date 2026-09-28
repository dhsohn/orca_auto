"""Submission target resolution: the job directory under the runs root and its newest input."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from orca_auto.orca.submission import resolve_submission_target, select_latest_inp
from tests.conftest import make_app_cfg, write_config_file, write_fake_orca


def test_select_latest_inp_prefers_base_input(tmp_path: Path) -> None:
    base = tmp_path / "rxn.inp"
    retry = tmp_path / "rxn.scfgrad.inp"
    base.write_text("! Opt\n", encoding="utf-8")
    retry.write_text("! Opt\n", encoding="utf-8")
    os.utime(base, ns=(1_000_000_000, 1_000_000_000))
    os.utime(retry, ns=(2_000_000_000, 2_000_000_000))
    assert select_latest_inp(tmp_path).name == "rxn.inp"


def test_select_latest_inp_warns_when_multiple_base_inputs_exist(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    older = tmp_path / "b.inp"
    newer = tmp_path / "a.inp"
    older.write_text("! Opt\n", encoding="utf-8")
    newer.write_text("! Opt\n", encoding="utf-8")
    os.utime(older, ns=(1_000_000_000, 1_000_000_000))
    os.utime(newer, ns=(2_000_000_000, 2_000_000_000))

    with caplog.at_level(logging.WARNING, logger="orca_auto.orca.submission"):
        selected = select_latest_inp(tmp_path)

    # `a.inp` is the newer file here: stamped against the name order so the
    # assertion cannot pass on the alphabetical tie-break alone.
    assert selected.name == "a.inp"
    assert "Multiple ORCA .inp candidates" in caplog.text


def _target_config(tmp_path: Path) -> tuple[Path, str]:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    config = write_config_file(
        tmp_path / "orca_auto.yaml",
        make_app_cfg(
            allowed, orca_executable=write_fake_orca(tmp_path / "fake_orca", "#!/bin/sh\n")
        ),
    )
    return allowed, str(config)


def test_submission_target_is_the_newest_input_under_the_allowed_root(tmp_path: Path) -> None:
    allowed, config = _target_config(tmp_path)
    reaction = allowed / "r1"
    reaction.mkdir()
    (reaction / "r1.inp").write_text("! SP\n", encoding="utf-8")

    target = resolve_submission_target(SimpleNamespace(config=config, path=str(reaction)))

    assert target is not None
    assert target.reaction_dir == reaction.resolve()
    assert target.selected_inp == (reaction / "r1.inp").resolve()
    assert target.allowed_root == allowed.resolve()


@pytest.mark.parametrize(
    ("where", "message"),
    [
        ("outside", "Job directory must be under allowed root"),
        ("missing", "Job directory not found"),
        ("no_input", "No .inp file found in"),
    ],
)
def test_submission_target_refusal_is_logged_and_returns_none(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, where: str, message: str
) -> None:
    allowed, config = _target_config(tmp_path)
    path = {
        "outside": tmp_path / "outside",
        "missing": allowed / "missing",
        "no_input": allowed / "empty",
    }[where]
    if where != "missing":
        path.mkdir()

    with caplog.at_level(logging.ERROR, logger="orca_auto.orca.submission"):
        assert resolve_submission_target(SimpleNamespace(config=config, path=str(path))) is None

    assert message in caplog.text
