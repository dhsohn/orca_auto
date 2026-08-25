from __future__ import annotations

import json
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from orca_auto.core.queue.engine.input_snapshot import SNAPSHOT_DIR_NAME
from orca_auto.core.queue.engine.snapshot_intent import (
    INPUT_SNAPSHOT_NAMESPACE_INTENT_KIND,
)
from orca_auto.flow.engines import submission_snapshot
from orca_auto.flow.engines.crest import submission as crest_submission
from orca_auto.flow.engines.xtb import submission as xtb_submission

_ENGINE_SUBMISSIONS = [
    pytest.param(xtb_submission, "xtb-shared-job-id", id="xtb"),
    pytest.param(crest_submission, "crest-shared-job-id", id="crest"),
]


def _unexpected_queue_root(*_args: object, **_kwargs: object) -> Path:
    raise AssertionError("runtime-missing submission must use the resolved job directory")


@pytest.mark.parametrize(("submission_module", "job_id"), _ENGINE_SUBMISSIONS)
def test_engine_submission_uses_one_durable_snapshot_transaction(
    submission_module: ModuleType,
    job_id: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    token_hex = "a" * 32
    snapshot_namespace = f"snapshot-{token_hex}"
    captured: dict[str, str] = {}
    sentinel = object()

    def capture_submission(*_args: object, **kwargs: object) -> object:
        captured["job_id"] = str(kwargs["job_id"])
        captured["snapshot_namespace"] = str(kwargs["snapshot_namespace"])
        return sentinel

    monkeypatch.setattr(submission_snapshot.secrets, "token_hex", lambda _size: token_hex)
    monkeypatch.setattr(submission_module, "new_job_id", lambda: job_id)
    monkeypatch.setattr(submission_module, "index_root_for_path", _unexpected_queue_root)
    monkeypatch.setattr(submission_module, "_build_submission_impl", capture_submission)

    result = submission_module._build_submission(object(), job_dir, {}, object())

    generation = (job_dir / SNAPSHOT_DIR_NAME / snapshot_namespace).resolve()
    marker = job_dir / ".orca_auto_snapshot_intents" / f"{snapshot_namespace}.json"
    payload = json.loads(marker.read_text(encoding="utf-8"))
    assert result is sentinel
    assert captured == {
        "job_id": job_id,
        "snapshot_namespace": snapshot_namespace,
    }
    assert generation.is_dir()
    assert payload["kind"] == INPUT_SNAPSHOT_NAMESPACE_INTENT_KIND
    assert payload["token"] == snapshot_namespace
    assert payload["generation_paths"] == [str(generation)]


@pytest.mark.parametrize(("submission_module", "job_id"), _ENGINE_SUBMISSIONS)
def test_engine_submission_base_exception_removes_snapshot_and_intent(
    submission_module: ModuleType,
    job_id: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    token_hex = "b" * 32
    snapshot_namespace = f"snapshot-{token_hex}"
    generation = job_dir / SNAPSHOT_DIR_NAME / snapshot_namespace
    marker = job_dir / ".orca_auto_snapshot_intents" / f"{snapshot_namespace}.json"
    failure = KeyboardInterrupt("simulated builder interruption")

    def fail_after_reservation(*_args: object, **_kwargs: object) -> object:
        (generation / "partial").write_text("partial", encoding="utf-8")
        raise failure

    monkeypatch.setattr(submission_snapshot.secrets, "token_hex", lambda _size: token_hex)
    monkeypatch.setattr(submission_module, "new_job_id", lambda: job_id)
    monkeypatch.setattr(submission_module, "index_root_for_path", _unexpected_queue_root)
    monkeypatch.setattr(submission_module, "_build_submission_impl", fail_after_reservation)

    with pytest.raises(KeyboardInterrupt) as exc_info:
        submission_module._build_submission(object(), job_dir, {}, object())

    assert exc_info.value is failure
    assert not generation.exists()
    assert not marker.exists()


def test_snapshot_reservation_failure_discards_created_intent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    token_hex = "c" * 32
    snapshot_namespace = f"snapshot-{token_hex}"
    generation = job_dir / SNAPSHOT_DIR_NAME / snapshot_namespace
    marker = job_dir / ".orca_auto_snapshot_intents" / f"{snapshot_namespace}.json"
    failure = OSError("simulated namespace reservation failure")

    def fail_reservation(*_args: object, **_kwargs: object) -> Path:
        raise failure

    def unexpected_cleanup(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("an unreserved namespace must not be cleaned")

    def unexpected_build(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("builder must not run after reservation failure")

    monkeypatch.setattr(submission_snapshot.secrets, "token_hex", lambda _size: token_hex)
    monkeypatch.setattr(submission_snapshot, "reserve_input_snapshot_namespace", fail_reservation)
    monkeypatch.setattr(
        submission_snapshot,
        "cleanup_unowned_input_snapshot_namespace",
        unexpected_cleanup,
    )

    with pytest.raises(OSError) as exc_info:
        submission_snapshot.build_reserved_input_snapshot_submission(
            object(),
            job_dir,
            {},
            object(),
            new_job_id_fn=lambda: "job-id",
            queue_root_for_path_fn=_unexpected_queue_root,
            build_submission_fn=unexpected_build,
        )

    assert exc_info.value is failure
    assert not generation.exists()
    assert not marker.exists()


def test_snapshot_transaction_preserves_side_effect_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    queue_root = tmp_path / "queue"
    queue_root.mkdir()
    token_hex = "d" * 32
    snapshot_namespace = f"snapshot-{token_hex}"
    failure = KeyboardInterrupt("stop after build")
    events: list[str] = []

    def new_job_id() -> str:
        events.append("new_job_id")
        return "job-id"

    def queue_root_for_path(_cfg: Any, resolved_job_dir: Path) -> Path:
        assert resolved_job_dir == job_dir.resolve()
        events.append("queue_root")
        return queue_root

    def create_intent(
        actual_queue_root: Path,
        *,
        token: str,
        kind: str,
        generation_paths: list[Path],
    ) -> None:
        assert actual_queue_root == queue_root
        assert token == snapshot_namespace
        assert kind == INPUT_SNAPSHOT_NAMESPACE_INTENT_KIND
        assert generation_paths == [job_dir.resolve() / SNAPSHOT_DIR_NAME / snapshot_namespace]
        events.append("create_intent")

    def reserve(actual_job_dir: Path, namespace: str) -> Path:
        assert actual_job_dir == job_dir
        assert namespace == snapshot_namespace
        events.append("reserve")
        return job_dir / SNAPSHOT_DIR_NAME / namespace

    def build(*_args: object, **kwargs: object) -> object:
        assert kwargs == {"job_id": "job-id", "snapshot_namespace": snapshot_namespace}
        events.append("build")
        raise failure

    def cleanup(actual_job_dir: Path, namespace: str) -> None:
        assert actual_job_dir == job_dir
        assert namespace == snapshot_namespace
        events.append("cleanup")

    def discard(actual_queue_root: Path, token: str) -> bool:
        assert actual_queue_root == queue_root
        assert token == snapshot_namespace
        events.append("discard_intent")
        return True

    monkeypatch.setattr(submission_snapshot.secrets, "token_hex", lambda _size: token_hex)
    monkeypatch.setattr(submission_snapshot, "create_snapshot_intent", create_intent)
    monkeypatch.setattr(submission_snapshot, "reserve_input_snapshot_namespace", reserve)
    monkeypatch.setattr(submission_snapshot, "cleanup_unowned_input_snapshot_namespace", cleanup)
    monkeypatch.setattr(
        submission_snapshot,
        "discard_snapshot_intent_if_generations_absent",
        discard,
    )

    with pytest.raises(KeyboardInterrupt) as exc_info:
        submission_snapshot.build_reserved_input_snapshot_submission(
            SimpleNamespace(runtime=object()),
            job_dir,
            {},
            object(),
            new_job_id_fn=new_job_id,
            queue_root_for_path_fn=queue_root_for_path,
            build_submission_fn=build,
        )

    assert exc_info.value is failure
    assert events == [
        "new_job_id",
        "queue_root",
        "create_intent",
        "reserve",
        "build",
        "cleanup",
        "discard_intent",
    ]
