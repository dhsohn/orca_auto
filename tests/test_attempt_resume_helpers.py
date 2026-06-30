from __future__ import annotations

from pathlib import Path
from typing import cast
from unittest.mock import patch

from orca_auto.orca import attempt_resume
from orca_auto.orca.state import new_state, state_path
from orca_auto.orca.statuses import AnalyzerStatus, RunStatus
from orca_auto.orca.types import AttemptRecord, RunFinishedNotification, RunState


def test_attempt_resume_text_and_patch_action_helpers_cover_existing_and_missing_values() -> None:
    attempt: AttemptRecord = {"patch_actions": ["existing"]}
    assert attempt_resume._ensure_patch_actions_list(attempt) == ["existing"]

    attempt = {}
    actions = attempt_resume._ensure_patch_actions_list(attempt)
    assert actions == []
    assert attempt["patch_actions"] == []

    assert attempt_resume._as_non_empty_text(" hello ") == "hello"
    assert attempt_resume._as_non_empty_text("   ") is None
    assert attempt_resume._as_non_empty_text(123) is None


def test_recover_missing_retry_input_covers_missing_attempt_shapes_and_sources(
    tmp_path: Path,
) -> None:
    reaction_dir = tmp_path / "rxn"
    reaction_dir.mkdir()
    selected_inp = reaction_dir / "calc.inp"
    current_inp = reaction_dir / "calc.retry01.inp"
    selected_inp.write_text("! Opt\n", encoding="utf-8")

    assert attempt_resume.recover_missing_retry_input(
        state={},
        reaction_dir=reaction_dir,
        selected_inp=selected_inp,
        current_inp=current_inp,
        retries_used=1,
        retry_recipe_step=lambda retry_number: retry_number,
        to_resolved_local=lambda raw: Path(raw),
        save_state=lambda _reaction_dir, _state: state_path(reaction_dir),
    ) == (False, "resume_attempts_missing")
    assert attempt_resume.recover_missing_retry_input(
        state=cast(RunState, {"attempts": ["bad"]}),
        reaction_dir=reaction_dir,
        selected_inp=selected_inp,
        current_inp=current_inp,
        retries_used=1,
        retry_recipe_step=lambda retry_number: retry_number,
        to_resolved_local=lambda raw: Path(raw),
        save_state=lambda _reaction_dir, _state: state_path(reaction_dir),
    ) == (False, "resume_last_attempt_invalid")
    assert attempt_resume.recover_missing_retry_input(
        state={"attempts": [{}]},
        reaction_dir=reaction_dir,
        selected_inp=selected_inp,
        current_inp=current_inp,
        retries_used=1,
        retry_recipe_step=lambda retry_number: retry_number,
        to_resolved_local=lambda raw: Path(raw),
        save_state=lambda _reaction_dir, _state: state_path(reaction_dir),
    ) == (False, "resume_source_input_missing")

    missing_selected = reaction_dir / "missing_selected.inp"
    assert attempt_resume.recover_missing_retry_input(
        state={"attempts": [{"inp_path": str(current_inp)}]},
        reaction_dir=reaction_dir,
        selected_inp=missing_selected,
        current_inp=current_inp,
        retries_used=1,
        retry_recipe_step=lambda retry_number: retry_number,
        to_resolved_local=lambda raw: Path(raw),
        save_state=lambda _reaction_dir, _state: state_path(reaction_dir),
    ) == (False, "resume_fallback_source_missing")

    assert attempt_resume.recover_missing_retry_input(
        state={"attempts": [{"inp_path": str(reaction_dir / "missing_source.inp")}]},
        reaction_dir=reaction_dir,
        selected_inp=selected_inp,
        current_inp=current_inp,
        retries_used=1,
        retry_recipe_step=lambda retry_number: retry_number,
        to_resolved_local=lambda raw: Path(raw),
        save_state=lambda _reaction_dir, _state: state_path(reaction_dir),
    ) == (False, "resume_source_input_not_found")


def test_recover_missing_retry_input_success_creates_patch_actions_and_saves_state(
    tmp_path: Path,
) -> None:
    reaction_dir = tmp_path / "rxn"
    reaction_dir.mkdir()
    selected_inp = reaction_dir / "calc.inp"
    current_inp = reaction_dir / "calc.retry01.inp"
    selected_inp.write_text("! Opt\n", encoding="utf-8")
    source_inp = reaction_dir / "calc.prev.inp"
    source_inp.write_text("! Retry\n", encoding="utf-8")
    state = cast(RunState, {"attempts": [{"inp_path": str(source_inp), "patch_actions": "bad"}]})

    with patch(
        "orca_auto.orca.attempt_resume.rewrite_for_retry", return_value=["patch_one"]
    ) as rewrite_mock:
        saved_paths: list[Path] = []

        def _save_state(reaction_dir_arg: Path, _state: RunState) -> Path:
            saved_paths.append(reaction_dir_arg)
            return state_path(reaction_dir)

        recovered, reason = attempt_resume.recover_missing_retry_input(
            reaction_dir=reaction_dir,
            state=state,
            selected_inp=selected_inp,
            current_inp=current_inp,
            retries_used=1,
            retry_recipe_step=lambda retry_number: retry_number + 1,
            to_resolved_local=lambda raw: Path(raw).resolve(),
            save_state=_save_state,
        )

    assert recovered
    assert reason == "resume_recovered"
    rewrite_mock.assert_called_once_with(
        source_inp=source_inp.resolve(),
        target_inp=current_inp,
        reaction_dir=reaction_dir,
        step=2,
        max_memory_gb=None,
        allow_no_effective_change=True,
    )
    assert state["attempts"][-1]["patch_actions"] == [
        "resume_recreated_missing_input:calc.retry01.inp",
        "resume_patch_one",
    ]
    assert saved_paths == [reaction_dir]


def test_resolve_execution_input_covers_existing_retry_recovery_exception_and_success(
    tmp_path: Path,
) -> None:
    reaction_dir = tmp_path / "rxn"
    reaction_dir.mkdir()
    selected_inp = reaction_dir / "calc.inp"
    selected_inp.write_text("! Opt\n", encoding="utf-8")
    retry_path = reaction_dir / "calc.retry01.inp"
    retry_path.write_text("! Retry\n", encoding="utf-8")

    empty_state: RunState = {"attempts": []}
    current_inp, reason = attempt_resume.resolve_execution_input(
        reaction_dir=reaction_dir,
        selected_inp=selected_inp,
        state=empty_state,
        execution_index=2,
        retries_used=1,
        retry_inp_path=lambda inp, retry_number: inp.with_name(
            f"{inp.stem}.retry{retry_number:02d}.inp"
        ),
        retry_recipe_step=lambda retry_number: retry_number,
        to_resolved_local=lambda raw: Path(raw),
        save_state=lambda _reaction_dir, _state: state_path(reaction_dir),
    )
    assert current_inp == retry_path
    assert reason is None

    retry_path.unlink()
    with patch(
        "orca_auto.orca.attempt_resume.recover_missing_retry_input",
        side_effect=RuntimeError("boom"),
    ):
        current_inp, reason = attempt_resume.resolve_execution_input(
            reaction_dir=reaction_dir,
            selected_inp=selected_inp,
            state=empty_state,
            execution_index=2,
            retries_used=1,
            retry_inp_path=lambda inp, retry_number: inp.with_name(
                f"{inp.stem}.retry{retry_number:02d}.inp"
            ),
            retry_recipe_step=lambda retry_number: retry_number,
            to_resolved_local=lambda raw: Path(raw),
            save_state=lambda _reaction_dir, _state: state_path(reaction_dir),
        )
    assert current_inp is None
    assert reason == "missing_input_for_attempt_2:resume_recovery_exception"

    def _recover_and_create(**_kwargs: object) -> tuple[bool, str]:
        retry_path.write_text("! Retry\n", encoding="utf-8")
        return True, "resume_recovered"

    with patch(
        "orca_auto.orca.attempt_resume.recover_missing_retry_input", side_effect=_recover_and_create
    ):
        current_inp, reason = attempt_resume.resolve_execution_input(
            reaction_dir=reaction_dir,
            selected_inp=selected_inp,
            state=empty_state,
            execution_index=2,
            retries_used=1,
            retry_inp_path=lambda inp, retry_number: inp.with_name(
                f"{inp.stem}.retry{retry_number:02d}.inp"
            ),
            retry_recipe_step=lambda retry_number: retry_number,
            to_resolved_local=lambda raw: Path(raw),
            save_state=lambda _reaction_dir, _state: state_path(reaction_dir),
        )
    assert current_inp == retry_path
    assert reason is None


def test_resume_terminal_decision_covers_non_resumed_malformed_and_defaulted_terminal_paths(
    tmp_path: Path,
) -> None:
    reaction_dir = tmp_path / "rxn"
    reaction_dir.mkdir()
    selected_inp = reaction_dir / "calc.inp"
    selected_inp.write_text("! Opt\n", encoding="utf-8")

    state = new_state(reaction_dir, selected_inp, max_retries=2)
    assert (
        attempt_resume.resume_terminal_decision(
            reaction_dir=reaction_dir,
            selected_inp=selected_inp,
            state=state,
            resumed=False,
            max_retries=2,
            last_out_path_from_state=lambda current_state: current_state.get("selected_inp"),
            exit_with_result=lambda *args, **kwargs: 0,
            emit=lambda _payload: None,
        )
        is None
    )
    assert (
        attempt_resume.resume_terminal_decision(
            reaction_dir=reaction_dir,
            selected_inp=selected_inp,
            state=cast(RunState, {"attempts": "bad"}),
            resumed=True,
            max_retries=2,
            last_out_path_from_state=lambda current_state: None,
            exit_with_result=lambda *args, **kwargs: 0,
            emit=lambda _payload: None,
        )
        is None
    )
    assert (
        attempt_resume.resume_terminal_decision(
            reaction_dir=reaction_dir,
            selected_inp=selected_inp,
            state=cast(RunState, {"attempts": ["bad"]}),
            resumed=True,
            max_retries=2,
            last_out_path_from_state=lambda current_state: None,
            exit_with_result=lambda *args, **kwargs: 0,
            emit=lambda _payload: None,
        )
        is None
    )

    non_terminal_state: RunState = {
        "attempts": [
            {
                "analyzer_status": AnalyzerStatus.INCOMPLETE.value,
                "analyzer_reason": "still_running",
            }
        ]
    }
    assert (
        attempt_resume.resume_terminal_decision(
            reaction_dir=reaction_dir,
            selected_inp=selected_inp,
            state=non_terminal_state,
            resumed=True,
            max_retries=3,
            last_out_path_from_state=lambda current_state: None,
            exit_with_result=lambda *args, **kwargs: 0,
            emit=lambda _payload: None,
        )
        is None
    )

    terminal_state: RunState = {
        "attempts": [
            {"analyzer_status": "completed", "analyzer_reason": "normal_termination"},
            {"analyzer_status": " ", "analyzer_reason": " ", "out_path": " "},
        ]
    }
    exit_calls: list[dict[str, object]] = []

    def notify_finished(payload: RunFinishedNotification) -> None:
        del payload

    def _exit_with_result(*args: object, **kwargs: object) -> int:
        del args
        exit_calls.append(dict(kwargs))
        return 7

    result = attempt_resume.resume_terminal_decision(
        reaction_dir=reaction_dir,
        selected_inp=selected_inp,
        state=terminal_state,
        resumed=True,
        max_retries=1,
        last_out_path_from_state=lambda current_state: "state.out",
        exit_with_result=_exit_with_result,
        emit=lambda _payload: None,
        notify_finished=notify_finished,
    )

    assert result == 7
    assert len(exit_calls) == 1
    assert exit_calls[0]["status"] == RunStatus.FAILED
    assert exit_calls[0]["analyzer_status"] == AnalyzerStatus.INCOMPLETE.value
    assert exit_calls[0]["reason"] == "retry_limit_reached"
    assert exit_calls[0]["last_out_path"] == "state.out"
    assert exit_calls[0]["notify_finished"] is notify_finished
