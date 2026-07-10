from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from orca_auto.core.queue.child import entrypoint as child_entrypoint
from orca_auto.core.queue.child import execution as child_execution


def test_child_worker_shutdown_controller_tracks_request() -> None:
    controller = child_execution.ChildWorkerShutdownController()

    assert controller.is_requested() is False
    controller.request()
    assert controller.is_requested() is True


def test_find_queue_entry_by_id_returns_matching_entry(tmp_path: Path) -> None:
    wanted = SimpleNamespace(queue_id="q-wanted")
    entries = [SimpleNamespace(queue_id="q-other"), wanted]

    assert (
        child_execution.find_queue_entry_by_id(
            tmp_path,
            "q-wanted",
            list_queue_fn=lambda _root: entries,
        )
        is wanted
    )
    assert (
        child_execution.find_queue_entry_by_id(
            tmp_path,
            "missing",
            list_queue_fn=lambda _root: entries,
        )
        is None
    )


def test_build_queue_entry_lookup_reuses_lister_and_optional_path_coercion(
    tmp_path: Path,
) -> None:
    wanted = SimpleNamespace(queue_id="q-wanted")
    seen_roots: list[Path | str] = []

    def list_queue(root: str | Path) -> list[Any]:
        seen_roots.append(root)
        return [wanted]

    lookup = child_execution.build_queue_entry_lookup(
        list_queue_fn=list_queue,
        coerce_root_to_path=True,
    )

    assert lookup(str(tmp_path), "q-wanted") is wanted
    assert seen_roots == [tmp_path]


def test_load_child_queue_job_resolves_paths_and_entry(tmp_path: Path) -> None:
    cfg = SimpleNamespace(name="cfg")
    entry = SimpleNamespace(queue_id="q-wanted", status="running")
    seen_roots: list[Path] = []

    def find_entry(root: Path, _queue_id: str) -> SimpleNamespace:
        seen_roots.append(root)
        return entry

    job = child_execution.load_child_queue_job(
        config_path="/tmp/orca_auto.yaml",
        queue_root=tmp_path / "queue",
        queue_id="q-wanted",
        load_config_fn=lambda _path: cfg,
        find_queue_entry_fn=find_entry,
        entry_ready_fn=lambda item: item.status == "running",
    )

    assert job == child_execution.ChildQueueJob(
        cfg=cfg,
        queue_root=(tmp_path / "queue").resolve(),
        entry=entry,
    )
    assert seen_roots == [(tmp_path / "queue").resolve()]


def test_load_child_queue_job_releases_admission_when_entry_is_missing(tmp_path: Path) -> None:
    cfg = SimpleNamespace(admission_root=tmp_path / "admission")
    released: list[tuple[Path, str]] = []

    job = child_execution.load_child_queue_job(
        config_path="/tmp/orca_auto.yaml",
        queue_root=tmp_path / "queue",
        queue_id="missing",
        load_config_fn=lambda _path: cfg,
        find_queue_entry_fn=lambda _root, _queue_id: None,
        admission_token="slot-1",
        admission_root_fn=lambda loaded_cfg: loaded_cfg.admission_root,
        release_slot_fn=lambda root, token: released.append((Path(root), token)),
    )

    assert job is None
    assert released == []


def test_child_admission_token_activation_and_release_are_conditional(tmp_path: Path) -> None:
    calls: list[tuple[str, str]] = []

    assert (
        child_execution.activate_child_admission_token(
            tmp_path,
            None,
            work_dir=tmp_path / "work",
            queue_id="q-1",
            source="source",
            activate_reserved_slot_fn=lambda *_args, **_kwargs: calls.append(("activate", "none")),
        )
        is True
    )
    assert calls == []

    assert (
        child_execution.activate_child_admission_token(
            tmp_path,
            "token",
            work_dir=tmp_path / "work",
            queue_id="q-1",
            source="source",
            activate_reserved_slot_fn=lambda *_args, **_kwargs: "slot",
        )
        is True
    )
    assert (
        child_execution.activate_child_admission_token(
            tmp_path,
            "token",
            work_dir=tmp_path / "work",
            queue_id="q-1",
            source="source",
            activate_reserved_slot_fn=lambda *_args, **_kwargs: None,
        )
        is False
    )

    child_execution.release_child_admission_token(
        tmp_path,
        None,
        release_slot_fn=lambda *_args: calls.append(("release", "none")),
    )
    assert calls == []

    child_execution.release_child_admission_token(
        tmp_path,
        "token",
        release_slot_fn=lambda _root, token: calls.append(("release", token)),
    )
    assert calls == [("release", "token")]


def test_child_worker_admission_scope_releases_on_exit(tmp_path: Path) -> None:
    cfg = SimpleNamespace(admission_root=tmp_path / "admission")
    job = child_entrypoint.ChildWorkerEntrypointJob(
        cfg=cfg,
        queue_root=tmp_path / "queue",
        entry=SimpleNamespace(queue_id="q-1"),
        _admission_root_fn=lambda loaded_cfg: loaded_cfg.admission_root,
    )
    released: list[tuple[Path, str]] = []

    with child_entrypoint.child_worker_admission_scope(
        job,
        "slot-1",
        release_slot_fn=lambda root, token: released.append((Path(root), token)),
    ):
        assert released == []

    assert released == []


def test_install_shutdown_request_handlers_wires_controller() -> None:
    installed: list[Callable[[], None]] = []
    controller = child_execution.ChildWorkerShutdownController()

    child_execution.install_shutdown_request_handlers(
        controller,
        install_signal_handlers_fn=lambda callback: installed.append(callback),
    )

    assert controller.is_requested() is False
    installed[0]()
    assert controller.is_requested() is True
