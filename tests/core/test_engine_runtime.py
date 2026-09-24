from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from orca_auto.core.config.schema import OrcaRuntimeConfig
from orca_auto.core.engines import (
    entry_matches_engine_identity,
    own_engine_accept_entry,
)
from orca_auto.core.queue.engine.runtime import EngineQueueRuntime
from orca_auto.core.queue.publication import (
    QUEUE_RECORD_SYNC_COMPLETE,
    QUEUE_RECORD_SYNC_KEY,
)
from orca_auto.core.queue.types import QueueEntry


def _cfg() -> SimpleNamespace:
    return SimpleNamespace(runtime=OrcaRuntimeConfig(allowed_root="/unused"))


def _runtime(
    tmp_path: Path,
    *,
    entries: dict[Path, list[Any]] | None = None,
    dequeued: dict[Path, Any | None] | None = None,
) -> EngineQueueRuntime:
    return EngineQueueRuntime(
        runtime_roots_for_cfg=lambda _cfg: (tmp_path / "a", tmp_path / "b"),
        list_queue=lambda root: dict(entries or {}).get(Path(root), []),
        dequeue_next=lambda root: dict(dequeued or {}).get(root),
        worker_pid_file_name="engine_worker.pid",
    )


def _internal_entry(engine: str, queue_id: str) -> QueueEntry:
    task_kind = {
        "orca": "orca_run_inp",
        "crest": "crest_conformer_search",
        "xtb": "xtb_opt",
    }.get(engine, f"{engine}_run")
    return QueueEntry(
        queue_id=queue_id,
        app_name=f"orca_auto_{engine}",
        task_id=f"{engine}-task-{queue_id}",
        task_kind=task_kind,
        engine=engine,
        metadata={
            **({"job_type": "opt"} if engine == "xtb" else {}),
            QUEUE_RECORD_SYNC_KEY: QUEUE_RECORD_SYNC_COMPLETE,
        },
    )


def test_engine_identity_rejects_conflicting_present_labels() -> None:
    accept_orca = own_engine_accept_entry("orca")

    own_entry = SimpleNamespace(
        queue_id="q-orca",
        app_name="orca_auto_orca",
        task_id="orca-1",
        task_kind="orca_run_inp",
        engine="orca",
        metadata={"job_type": "opt"},
    )
    assert accept_orca(own_entry)

    for overrides in (
        {"app_name": "orca_auto_crest"},
        {"engine": "crest"},
        {"queue_id": ""},
        {"task_id": ""},
        {"task_kind": ""},
        {"task_kind": "crest_conformer_search"},
        {"app_name": "", "engine": ""},
    ):
        assert not accept_orca(SimpleNamespace(**{**vars(own_entry), **overrides}))

    assert not entry_matches_engine_identity(
        SimpleNamespace(
            queue_id="q-crest-invalid",
            app_name="orca_auto_crest",
            task_id="crest-1",
            task_kind="conformer_search",
            engine="crest",
        ),
        "crest",
    )

    canonical_orca_mapping = {
        "queue_id": "q-orca",
        "app_name": "orca_auto_orca",
        "task_id": "orca-1",
        "task_kind": "orca_run_inp",
        "engine": "orca",
        "metadata": {},
    }
    assert entry_matches_engine_identity(canonical_orca_mapping, "orca")
    for missing_field in ("app_name", "task_id", "task_kind", "engine"):
        partial = dict(canonical_orca_mapping)
        partial.pop(missing_field)
        assert not entry_matches_engine_identity(partial, "orca")


def test_engine_queue_runtime_combines_roots_entries_and_next_entry(
    tmp_path: Path,
) -> None:
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()
    entry_a = SimpleNamespace(queue_id="a", status=SimpleNamespace(value="pending"))
    entry_b = SimpleNamespace(
        queue_id="b",
        status=SimpleNamespace(value="pending"),
        priority=1,
        enqueued_at="2026-01-01T00:00:00Z",
        cancel_requested=False,
        metadata={QUEUE_RECORD_SYNC_KEY: QUEUE_RECORD_SYNC_COMPLETE},
    )
    runtime = _runtime(
        tmp_path,
        entries={root_a: [entry_a], root_b: [entry_b]},
        dequeued={root_b: entry_b},
    )

    assert runtime.queue_roots(SimpleNamespace()) == (root_a, root_b)
    assert runtime.queue_entries_with_roots(SimpleNamespace()) == [
        (root_a, entry_a),
        (root_b, entry_b),
    ]
    assert runtime.dequeue_next_entry(SimpleNamespace()) == (root_b, entry_b)


@pytest.mark.parametrize("override_listing", [False, True])
def test_engine_queue_runtime_listing_filters_missing_roots_and_foreign_entries(
    tmp_path: Path, override_listing: bool
) -> None:
    queue_root = tmp_path / "queue"
    queue_root.mkdir()
    own_entry = _internal_entry("orca", "own")
    foreign_entry = _internal_entry("crest", "foreign")
    seen: list[tuple[str, Path | str]] = []

    def list_queue(root: Path | str) -> list[Any]:
        seen.append(("default", root))
        return [foreign_entry, own_entry]

    def override(root: Path | str) -> list[Any]:
        seen.append(("override", root))
        return [own_entry, foreign_entry]

    runtime: EngineQueueRuntime[SimpleNamespace] = EngineQueueRuntime(
        runtime_roots_for_cfg=lambda _cfg: (tmp_path / "missing", queue_root),
        list_queue=list_queue,
        dequeue_next=lambda _root: None,
        worker_pid_file_name="worker.pid",
        accept_entry_fn=own_engine_accept_entry("orca"),
    )

    assert runtime.queue_entries_with_roots(
        _cfg(), list_queue_fn=override if override_listing else None
    ) == [(queue_root, own_entry)]
    assert seen == [("override" if override_listing else "default", queue_root)]
    assert not (tmp_path / "missing").exists()


@pytest.mark.parametrize("dequeue_by_id", [False, True])
def test_engine_queue_runtime_peek_preserves_selection_without_dequeuing(
    tmp_path: Path, dequeue_by_id: bool
) -> None:
    root_a, root_b = tmp_path / "a", tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()
    foreign_entry = _internal_entry("crest", "foreign")
    own_entry = _internal_entry("orca", "own")
    own_entry = replace(own_entry, priority=-1)
    fallback_entry = _internal_entry("orca", "fallback")
    fallback_entry = replace(fallback_entry, priority=5)
    seen: list[Path | str] = []

    def list_queue(root: Path | str) -> list[Any]:
        seen.append(root)
        return {root_a: [foreign_entry, own_entry], root_b: [fallback_entry]}[Path(root)]

    def unexpected_dequeue(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("preview must not dequeue a row")

    runtime: EngineQueueRuntime[SimpleNamespace] = EngineQueueRuntime(
        runtime_roots_for_cfg=lambda _cfg: (tmp_path / "missing", root_a, root_b),
        list_queue=list_queue,
        dequeue_next=unexpected_dequeue,
        dequeue_entry_if_pending=unexpected_dequeue if dequeue_by_id else None,
        worker_pid_file_name="worker.pid",
        accept_entry_fn=own_engine_accept_entry("orca"),
    )

    expected = (root_a, own_entry) if dequeue_by_id else (root_b, fallback_entry)
    assert runtime.peek_next_entry(_cfg()) == expected
    assert seen == [root_a, root_b]
    assert own_entry.status.value == fallback_entry.status.value == "pending"


def test_skip_predicate_steers_both_the_preview_and_the_by_id_claim(tmp_path: Path) -> None:
    root = tmp_path / "queue"
    root.mkdir()
    tracked = _internal_entry("orca", "queue-tracked")
    behind = _internal_entry("orca", "queue-behind")
    foreign = _internal_entry("crest", "queue-foreign")
    claimed: list[tuple[str, Any]] = []

    def dequeue_by_id(_root: Path, queue_id: str, *, expected_entry: Any) -> Any:
        claimed.append((queue_id, expected_entry))
        return expected_entry

    runtime: EngineQueueRuntime[SimpleNamespace] = EngineQueueRuntime(
        runtime_roots_for_cfg=lambda _cfg: (root,),
        list_queue=lambda _root: [foreign, tracked, behind],
        dequeue_next=lambda _root: pytest.fail("a skipped head row must not be claimed"),
        dequeue_entry_if_pending=dequeue_by_id,
        worker_pid_file_name="worker.pid",
        accept_entry_fn=own_engine_accept_entry("orca"),
    )

    def skip(entry: Any) -> bool:
        # The engine filter runs first, so a foreign row never reaches it.
        assert entry is not foreign
        return entry is tracked

    assert runtime.peek_next_entry(_cfg(), skip_entry_fn=skip) == (root, behind)
    assert runtime.dequeue_next_entry(_cfg(), skip_entry_fn=skip) == (root, behind)
    assert claimed == [("queue-behind", behind)]
    assert runtime.peek_next_entry(_cfg(), skip_entry_fn=lambda _entry: True) is None
    assert runtime.peek_next_entry(_cfg()) == (root, tracked)


def test_skip_predicate_is_ignored_by_a_runtime_that_claims_its_head_row(tmp_path: Path) -> None:
    # Such a runtime cannot claim around a row, so filtering only the preview
    # would disagree with what the dequeue then claims.
    root = tmp_path / "queue"
    root.mkdir()
    head = _internal_entry("orca", "queue-head")
    runtime: EngineQueueRuntime[SimpleNamespace] = EngineQueueRuntime(
        runtime_roots_for_cfg=lambda _cfg: (root,),
        list_queue=lambda _root: [head],
        dequeue_next=lambda _root: head,
        worker_pid_file_name="worker.pid",
        accept_entry_fn=own_engine_accept_entry("orca"),
    )

    assert runtime.peek_next_entry(_cfg(), skip_entry_fn=lambda _entry: True) == (root, head)
    assert runtime.dequeue_next_entry(_cfg(), skip_entry_fn=lambda _entry: True) == (root, head)


def test_engine_queue_runtime_common_accessors(tmp_path: Path) -> None:
    entry = _internal_entry("orca", "queue-1")
    runtime = _runtime(tmp_path, entries={tmp_path / "a": [entry]})
    cfg = SimpleNamespace(
        runtime=SimpleNamespace(
            allowed_root="/tmp/allowed",
            admission_root="/tmp/admission",
            admission_limit=1,
            max_concurrent=1,
            resolved_admission_root="/tmp/admission",
            resolved_admission_limit=1,
        )
    )

    assert runtime.queue_entry_by_id(tmp_path / "a", "queue-1") is entry
    assert runtime.admission_root(cfg) == "/tmp/admission"

    (tmp_path / "engine_worker.pid").write_text("123\n", encoding="utf-8")
    assert runtime.read_worker_pid(tmp_path) is None


def test_engine_queue_runtime_filters_by_id_lookups_through_the_accept_predicate(
    tmp_path: Path,
) -> None:
    queue_root = tmp_path / "queue"
    queue_root.mkdir()
    own_entry = _internal_entry("orca", "queue-own")
    foreign_entry = _internal_entry("crest", "queue-foreign")
    looked_up: list[tuple[Path | str, str]] = []

    def queue_entry_by_id(root: Path | str, queue_id: str) -> Any | None:
        looked_up.append((root, queue_id))
        return own_entry if queue_id == own_entry.queue_id else foreign_entry

    runtime: EngineQueueRuntime[SimpleNamespace] = EngineQueueRuntime(
        runtime_roots_for_cfg=lambda _cfg: (queue_root,),
        list_queue=lambda _root: [foreign_entry, own_entry],
        dequeue_next=lambda _root: own_entry,
        dequeue_entry_if_pending=lambda _root, _queue_id, **_kwargs: own_entry,
        queue_entry_by_id_fn=queue_entry_by_id,
        worker_pid_file_name="queue-contract.pid",
        accept_entry_fn=own_engine_accept_entry("orca"),
    )

    assert runtime.worker_pid_file_name == "queue-contract.pid"
    assert runtime.queue_entries_with_roots(_cfg()) == [(queue_root, own_entry)]
    assert runtime.dequeue_next_entry(_cfg()) == (queue_root, own_entry)
    assert runtime.queue_entry_by_id(queue_root, own_entry.queue_id) is own_entry
    assert runtime.queue_entry_by_id(queue_root, foreign_entry.queue_id) is None
    assert looked_up == [
        (queue_root, own_entry.queue_id),
        (queue_root, foreign_entry.queue_id),
    ]


def test_queue_roots_propagates_runtime_root_errors(tmp_path: Path) -> None:
    from dataclasses import replace

    def broken_roots(_cfg: Any) -> tuple[Path, ...]:
        raise RuntimeError("bad runtime roots")

    runtime = replace(_runtime(tmp_path), runtime_roots_for_cfg=broken_roots)
    with pytest.raises(RuntimeError, match="bad runtime roots"):
        runtime.queue_roots(_cfg())


def test_listing_skips_missing_roots_without_creating_them(tmp_path: Path) -> None:
    from dataclasses import replace

    existing_root = tmp_path / "existing"
    missing_root = tmp_path / "missing"
    existing_root.mkdir()
    entry = _internal_entry("orca", "queue-1")
    calls: list[Path] = []

    def list_queue(root: str | Path) -> list[QueueEntry]:
        calls.append(Path(root))
        return [entry]

    runtime = replace(
        _runtime(tmp_path),
        runtime_roots_for_cfg=lambda _cfg: (missing_root, existing_root),
        list_queue=list_queue,
    )
    assert runtime.queue_entries_with_roots(_cfg()) == [(existing_root, entry)]
    assert calls == [existing_root]
    assert not missing_root.exists()
