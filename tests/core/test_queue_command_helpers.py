from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from orca_auto.core.commands import queue as queue_cmd


def test_queue_roots_propagates_runtime_root_errors() -> None:
    def broken_runtime_roots(_cfg: object) -> tuple[Any, ...]:
        raise RuntimeError("bad runtime roots")

    with pytest.raises(RuntimeError, match="bad runtime roots"):
        queue_cmd.queue_roots(
            SimpleNamespace(runtime=SimpleNamespace(allowed_root="/tmp/fallback")),
            runtime_roots_for_cfg_fn=broken_runtime_roots,
        )


def test_queue_entry_listing_skips_missing_roots_without_calling_list_queue(tmp_path: Any) -> None:
    existing_root = tmp_path / "existing"
    missing_root = tmp_path / "missing"
    existing_root.mkdir()
    entry = SimpleNamespace(queue_id="queue-1")
    seen_roots: list[Any] = []

    def list_queue(root: Any) -> list[Any]:
        seen_roots.append(root)
        return [entry]

    rows = queue_cmd.queue_entries_with_roots(
        SimpleNamespace(),
        queue_roots_fn=lambda _cfg: (missing_root, existing_root),
        list_queue_fn=list_queue,
    )

    assert rows == [(existing_root, entry)]
    assert seen_roots == [existing_root]
    assert not missing_root.exists()


def test_queue_runtime_listing_applies_entry_acceptance_filter(tmp_path: Any) -> None:
    queue_root = tmp_path / "queue"
    queue_root.mkdir()
    accepted = SimpleNamespace(queue_id="accepted")
    rejected = SimpleNamespace(queue_id="rejected")
    runtime = queue_cmd.QueueRuntime(
        load_config_fn=lambda value: value,
        runtime_roots_for_cfg_fn=lambda _cfg: (queue_root,),
        list_queue_fn=lambda _root: [rejected, accepted],
        dequeue_next_fn=lambda _root: None,
        dequeue_next_across_roots_fn=lambda *_args, **_kwargs: None,
        peek_next_across_roots_fn=lambda *_args, **_kwargs: None,
        accept_entry_fn=lambda entry: entry is accepted,
    )

    assert runtime.queue_entries_with_roots(SimpleNamespace()) == [(queue_root, accepted)]


def test_queue_runtime_peek_previews_the_selected_entry_without_dequeuing(tmp_path: Any) -> None:
    queue_root = tmp_path / "queue"
    queue_root.mkdir()
    entry = SimpleNamespace(queue_id="queue-1")
    seen: list[dict[str, Any]] = []

    def peek_next_across_roots(roots: Any, **kwargs: Any) -> Any:
        seen.append({"roots": roots, **kwargs})
        return (queue_root, entry)

    def dequeue_next(_root: Any) -> Any:
        raise AssertionError("a preview must not dequeue")

    runtime = queue_cmd.QueueRuntime(
        load_config_fn=lambda value: value,
        runtime_roots_for_cfg_fn=lambda _cfg: (tmp_path / "missing", queue_root),
        list_queue_fn=lambda _root: [entry],
        dequeue_next_fn=dequeue_next,
        dequeue_next_across_roots_fn=dequeue_next,
        peek_next_across_roots_fn=peek_next_across_roots,
        dequeue_entry_if_pending_fn=lambda *_args, **_kwargs: None,
        accept_entry_fn=lambda candidate: candidate is entry,
    )

    assert runtime.peek_next_entry(SimpleNamespace()) == (queue_root, entry)
    assert len(seen) == 1
    assert seen[0]["roots"] == (queue_root,)
    assert seen[0]["select_all_rows"] is True
    assert seen[0]["accept_entry_fn"](entry) is True


def test_run_queue_worker_command_uses_existing_pid_reporter(
    capsys: pytest.CaptureFixture[str],
) -> None:
    reports: list[int] = []

    def worker_factory(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("worker should not be constructed when a live pid exists")

    result = queue_cmd.run_queue_worker_command(
        SimpleNamespace(config="orca_auto.yaml"),
        load_config_fn=lambda _config: SimpleNamespace(),
        config_path_fn=lambda _args: "orca_auto.yaml",
        worker_factory=worker_factory,
        existing_pid_fn=lambda _cfg: 12345,
        existing_pid_report_fn=reports.append,
    )

    assert result == 1
    assert reports == [12345]
    assert capsys.readouterr().out == ""


def test_run_pidfile_queue_worker_command_reads_pid_from_allowed_root(tmp_path: Any) -> None:
    allowed_root = tmp_path / "allowed"
    cfg = SimpleNamespace(runtime=SimpleNamespace(allowed_root=str(allowed_root), max_concurrent=2))
    seen: list[Any] = []

    class FakeWorker:
        def __init__(self, cfg_obj: Any, config_path: str, *, max_concurrent: int) -> None:
            seen.append((cfg_obj, config_path, max_concurrent))

        def run(self) -> int:
            seen.append("run")
            return 7

    def read_worker_pid(root: Any) -> None:
        seen.append(root)
        return None

    result = queue_cmd.run_pidfile_queue_worker_command(
        SimpleNamespace(config="config.yaml"),
        load_config_fn=lambda _config: cfg,
        config_path_fn=lambda args: args.config,
        read_worker_pid_fn=read_worker_pid,
        max_concurrent_fn=lambda cfg_obj: cfg_obj.runtime.max_concurrent,
        worker_factory=FakeWorker,
    )

    assert result == 7
    assert seen == [allowed_root.resolve(), (cfg, "config.yaml", 2), "run"]
