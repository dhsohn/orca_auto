"""Static single-writer guards for the durable files.

Each test walks the package's AST and fails when a writer of one durable file
is reached from outside its listed owners. ``docs/ARCHITECTURE.md`` ("Durable
files and writers") names the same owners; change both together.

* ``queue.json`` is written only by ``core/queue/store.py`` (``mutate_entries``),
  and its lock is held elsewhere only by two read-only users.
* A row's status moves to PENDING or a terminal status only through
  ``transitions.requeued_entry`` or ``transitions.terminal_entry``.
* A row is created only by ``adapter.enqueue`` and loaded only by
  ``persistence.entry_from_dict``; no rewrite sets ``enqueued_at``, which is
  part of every writer fence's generation identity.
* Each atomic-write primitive is called only by the module that owns the file
  it writes.
* ``job_state.json`` is saved only through ``orca/state.py`` by the listed
  execution and settlement steps, and only the parent's settlement records a
  ``cancelled`` result.
* ``machine.json``, ``execution_provenance.json`` and the reports are built and
  written only by ``orca/report/publication.py``, for the child's normal exit
  and the parent's terminal-state synthesis.
* ``admission_slots.json`` is written only by ``core/admission/store.py``, and
  each slot mutation is reached only from its owner: the worker parent, the
  child's one slot rule (``execution._child_admission_slot``) or the
  engine-process registrar and recovery.
* ``job_locations.json`` is written only by ``core/indexing/store.py``, reached
  through ``queue/job_records.py`` and the two ``index`` commands.
* The worker PID file is written only by the worker, and notifications are
  dispatched only from the three claim/dispatch sites.
"""

from __future__ import annotations

import ast
import functools
from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path

from orca_auto.core.queue import transitions
from orca_auto.core.queue.types import QueueStatus

_PACKAGE_ROOT = Path(transitions.__file__).resolve().parents[2]
_STORE_OWNERS = {"core/queue/store.py", "core/queue/persistence.py"}

# An owner is a whole module (``"path.py"``) or one scope in it
# (``("path.py", "Class.method")``).
_Owner = str | tuple[str, str]


@functools.cache
def _package_nodes() -> tuple[tuple[str, str, ast.AST], ...]:
    """Every AST node in the package with its file and enclosing function chain."""

    def walk(
        node: ast.AST, scope: tuple[str, ...], path: str
    ) -> Iterator[tuple[str, str, ast.AST]]:
        for child in ast.iter_child_nodes(node):
            yield path, ".".join(scope), child
            inner = (
                (*scope, child.name)
                if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
                else scope
            )
            yield from walk(child, inner, path)

    nodes: list[tuple[str, str, ast.AST]] = []
    for source_path in sorted(_PACKAGE_ROOT.rglob("*.py")):
        relative = source_path.relative_to(_PACKAGE_ROOT).as_posix()
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
        nodes.extend(walk(tree, (), relative))
    return tuple(nodes)


def _nodes() -> Iterator[tuple[str, str, ast.AST]]:
    return iter(_package_nodes())


def _callee_name(call: ast.Call) -> str:
    callee = call.func
    if isinstance(callee, ast.Name):
        return callee.id
    if isinstance(callee, ast.Attribute):
        return callee.attr
    return ""


def _queue_status_literal(value: ast.expr) -> str | None:
    if (
        isinstance(value, ast.Attribute)
        and isinstance(value.value, ast.Name)
        and value.value.id == "QueueStatus"
    ):
        return value.attr
    return None


def _referenced_name(node: ast.AST) -> str | None:
    """The name a node reads: a bare name or an attribute."""

    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _renamed_imports(node: ast.AST) -> Iterator[str]:
    """Names imported under another name, which would hide their later uses."""

    if isinstance(node, ast.Import | ast.ImportFrom):
        for alias in node.names:
            if alias.asname and alias.asname != alias.name.rsplit(".", 1)[-1]:
                yield alias.name.rsplit(".", 1)[-1]


def _is_owned(path: str, scope: str, owners: Iterable[_Owner]) -> bool:
    return any(owner == path or owner == (path, scope) for owner in owners)


def _unowned_references(table: Mapping[str, Iterable[_Owner]]) -> list[str]:
    offenders: list[str] = []
    for path, scope, node in _nodes():
        referenced = _referenced_name(node)
        names = [*_renamed_imports(node)] if referenced is None else [referenced]
        for name in names:
            if name in table and not _is_owned(path, scope, table[name]):
                line = getattr(node, "lineno", 0)
                offenders.append(f"{path}:{line} ({scope or '<module>'}) uses {name}")
    return offenders


def test_queue_file_is_written_only_by_the_store() -> None:
    offenders = [
        f"{path}:{node.lineno}"
        for path, _scope, node in _nodes()
        if path not in _STORE_OWNERS
        and (
            (isinstance(node, ast.Name) and node.id == "save_entries")
            or (isinstance(node, ast.Attribute) and node.attr == "save_entries")
        )
    ]

    assert offenders == [], offenders


def test_queue_lock_is_held_outside_the_store_only_by_read_only_users() -> None:
    offenders = _unowned_references(
        {
            "queue_lock": {
                "core/queue/store.py",
                # Holds the lock while it reads the rows and unlinks a root
                # job_state.json that no row protects; it writes no row.
                ("orca/run_cleanup.py", "clear_terminal_run_states"),
                # Holds the lock while it reads the rows and retires abandoned
                # snapshot intents; it writes no row.
                ("core/queue/snapshot_intent.py", "reconcile_orphaned_snapshot_generations"),
            }
        }
    )

    assert offenders == [], offenders


def test_pending_and_terminal_rows_are_built_only_by_the_transition_constructors() -> None:
    terminal_names = {status.name for status in transitions.TERMINAL_QUEUE_STATUSES}
    allowed = {
        # ``terminal_entry`` passes its validated ``status`` variable, so a
        # literal terminal status in a ``replace`` call is always a bypass.
        QueueStatus.PENDING.name: {("core/queue/transitions.py", "requeued_entry")},
        **{name: set() for name in terminal_names},
    }
    offenders: list[str] = []
    for path, scope, node in _nodes():
        if not isinstance(node, ast.Call) or _callee_name(node) != "replace":
            continue
        for keyword in node.keywords:
            status = _queue_status_literal(keyword.value) if keyword.arg == "status" else None
            if status in allowed and (path, scope) not in allowed[status]:
                offenders.append(f"{path}:{node.lineno} ({scope}) status={status}")

    assert offenders == [], offenders


def test_rows_are_created_once_and_no_rewrite_sets_enqueued_at() -> None:
    constructors = {
        ("core/queue/persistence.py", "entry_from_dict"),
        ("orca/queue/adapter.py", "enqueue.append"),
    }
    offenders: list[str] = []
    for path, scope, node in _nodes():
        if not isinstance(node, ast.Call):
            continue
        name = _callee_name(node)
        if name == "QueueEntry" and (path, scope) not in constructors:
            offenders.append(f"{path}:{node.lineno} ({scope}) builds a QueueEntry")
        if name == "replace" and any(keyword.arg == "enqueued_at" for keyword in node.keywords):
            offenders.append(f"{path}:{node.lineno} ({scope}) rewrites enqueued_at")

    assert offenders == [], offenders


# Each generic atomic writer, and the one module per durable file allowed to
# call it. A new durable file adds its owner here and to ARCHITECTURE.md.
_ATOMIC_WRITE_OWNERS: dict[str, set[_Owner]] = {
    "atomic_write_json": {
        "core/queue/persistence.py",  # queue.json
        "core/admission/persistence.py",  # .admission/admission_slots.json
        "core/indexing/store.py",  # job_locations.json
        "core/queue/snapshot_intent.py",  # .orca_auto_snapshot_intents/*
    },
    "atomic_write_text": {
        "core/utils/persistence.py",  # atomic_write_json itself
        "core/queue/worker/pid_file.py",  # queue_worker.pid
        "orca/commands/init.py",  # orca_auto.yaml
    },
    "atomic_write_confined_bytes": {
        "orca/state.py",  # job_state.json, and write_generation_bytes
        "orca/report/publication.py",  # job_report.html
        "orca/report/si.py",  # si_block.md
        "orca/execution_binding/_confinement.py",  # bound generation inputs
        "orca/orca_runner.py",  # trailing newline of the bound input
    },
    "atomic_write_bytes_at": {
        "core/engine_scratch/_fs.py",  # scratch manifest and staged inputs
        "core/engine_scratch/_publication.py",  # scratch copy-back journal
    },
}


def test_atomic_writers_are_called_only_by_the_module_that_owns_the_file() -> None:
    offenders = _unowned_references(_ATOMIC_WRITE_OWNERS)

    assert offenders == [], offenders


_STATE_WRITERS: dict[str, set[_Owner]] = {
    "save_state": {
        ("orca/state.py", "finalize_state"),
        ("orca/execution.py", "execute_locked_run"),
        ("orca/attempt/run.py", "run_attempt"),
        ("orca/attempt/run.py", "_run_and_record_attempt"),
        ("orca/attempt/run.py", "_record_exception_scratch_publication"),
        ("orca/attempt/resume.py", "recover_crashed_state"),
        ("orca/attempt/resume.py", "load_or_create_state"),
        ("orca/output_adoption.py", "existing_completed_exit"),
        # The parent's terminal notification claim (root bookkeeping only).
        ("orca/queue/notifications.py", "claim_and_send_terminal"),
    },
    "finalize_state": {
        ("orca/attempt/reporting.py", "exit_with_result"),
        ("orca/queue/terminal_state.py", "_record_terminal_run_state"),
    },
    # Root and generation bytes go through the state writer; generation
    # reports through the publisher.
    "write_generation_bytes": {"orca/state.py", "orca/report/publication.py"},
    # The per-directory lock that orders both state files; queue list clear
    # takes it to unlink the root state.
    "STATE_MUTATION_LOCK_FILE_NAME": {
        "core/artifacts.py",
        "core/engine_scratch/_constants.py",
        ("orca/state.py", "save_state"),
        ("orca/run_cleanup.py", "clear_terminal_run_states"),
    },
}


def test_job_state_is_written_only_through_the_state_writer_by_its_owners() -> None:
    offenders = _unowned_references(_STATE_WRITERS)

    assert offenders == [], offenders


def test_only_the_parent_settlement_records_a_cancelled_result() -> None:
    allowed = {
        ("orca/queue/terminal_state.py", "record_cancelled_run_state"),
        ("orca/statuses.py", ""),
        # Readers of a recorded status.
        ("orca/notifications.py", "run_finished_message"),
        ("orca/machine_observation.py", "machine_lifecycle"),
    }
    offenders = [
        f"{path}:{node.lineno} ({scope or '<module>'})"
        for path, scope, node in _nodes()
        if isinstance(node, ast.Attribute)
        and node.attr == "CANCELLED"
        and isinstance(node.value, ast.Name)
        and node.value.id == "RunStatus"
        and (path, scope) not in allowed
    ]

    assert offenders == [], offenders


_REPORT_WRITERS: dict[str, set[_Owner]] = {
    "build_machine_observation": {("orca/report/publication.py", "write_report_json")},
    "machine_json_bytes": {("orca/report/publication.py", "write_report_json")},
    "write_report_json": {("orca/report/publication.py", "write_report_files")},
    "write_job_html_report": {("orca/report/publication.py", "write_report_files")},
    "write_si_block": {("orca/report/publication.py", "write_report_files")},
    "write_report_files": {
        ("orca/attempt/reporting.py", "exit_with_result"),
        ("orca/queue/terminal_state.py", "_record_terminal_run_state"),
    },
}


def test_machine_json_and_reports_are_written_only_by_the_publisher() -> None:
    offenders = _unowned_references(_REPORT_WRITERS)

    assert offenders == [], offenders


_ADMISSION_STORE = "core/admission/store.py"
_WORKER = "orca/queue/worker.py"
_ENGINE_PROCESS = "core/admission/engine_process.py"
_SLOT_MUTATION_OWNERS: dict[str, set[_Owner]] = {
    # The file itself and the store's lock/load/mutate/save primitive.
    "save_slots": {_ADMISSION_STORE},
    "mutate_live_slots": {_ADMISSION_STORE},
    "mutate_all_slots": {_ADMISSION_STORE},
    "mutate_slot_by_token": {_ADMISSION_STORE},
    "admission_lock": {
        _ADMISSION_STORE,
        # Holds the lock across its read-only count so no slot is reserved
        # while the service restarts.
        ("cli_systemd_restart_guard.py", "guard_service_restart"),
    },
    # Slot operations, each reached only from its owner.
    "reserve_slot": {(_WORKER, "_try_reserve_admission_slot")},
    "update_slot_metadata": {(_WORKER, "OrcaQueueWorker._on_worker_process_started")},
    "release_slot": {
        (_WORKER, "OrcaQueueWorker._release_admission_slot"),
        ("orca/execution.py", "_child_admission_slot"),
    },
    "activate_reserved_slot": {("orca/execution.py", "_child_admission_slot")},
    "complete_slot_engine_process": {
        (_ENGINE_PROCESS, "register_slot_engine_process"),
        (_ENGINE_PROCESS, "_clear_dead_owner_pending_launch"),
        ("orca/execution.py", "_child_admission_slot"),
    },
    "build_slot_engine_process_preparer": {("orca/execution.py", "execute_locked_run")},
    "build_slot_engine_process_registrar": {("orca/execution.py", "execute_locked_run")},
    "prepare_slot_engine_process": {
        (_ENGINE_PROCESS, "build_slot_engine_process_preparer.prepare")
    },
    "register_slot_engine_process": {(_ENGINE_PROCESS, "build_slot_engine_process_registrar")},
    "set_slot_engine_process": {(_ENGINE_PROCESS, "register_slot_engine_process")},
    "clear_slot_engine_process": {(_ENGINE_PROCESS, "_clear_record")},
    "recover_slot_engine_process": {
        (_WORKER, "OrcaQueueWorker._finalize_completed_job"),
        (_WORKER, "OrcaQueueWorker._stop_child_and_recover_engine"),
        (_ENGINE_PROCESS, "recover_orphaned_engine_slots"),
    },
    "recover_orphaned_engine_slots": {(_WORKER, "OrcaQueueWorker._reconcile_worker_state")},
    "reconcile_stale_slots": {(_WORKER, "OrcaQueueWorker._reconcile_worker_state")},
}


def test_admission_slots_are_written_only_by_the_store_for_their_owners() -> None:
    offenders = _unowned_references(_SLOT_MUTATION_OWNERS)

    assert offenders == [], offenders


def test_only_the_worker_reconcile_lists_slots_with_a_rewrite() -> None:
    """``list_slots`` drops dead owners from the file unless ``normalize_file=False``."""

    rewriting_owners = {
        (_ADMISSION_STORE, "list_slots"),
        (_WORKER, "OrcaQueueWorker._reconcile_worker_state"),
    }
    offenders: list[str] = []
    for path, scope, node in _nodes():
        if not isinstance(node, ast.Call) or _callee_name(node) != "list_slots":
            continue
        read_only = any(
            keyword.arg == "normalize_file"
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value is False
            for keyword in node.keywords
        )
        if not read_only and (path, scope) not in rewriting_owners:
            offenders.append(f"{path}:{node.lineno} ({scope})")

    assert offenders == [], offenders


_LOCATION_INDEX_WRITERS: dict[str, set[_Owner]] = {
    "_save_records": {"core/indexing/store.py"},
    "upsert_job_location": {("orca/job_locations/_records.py", "upsert_job_record")},
    "upsert_job_record": {"orca/queue/job_records.py"},
    "upsert_row_job_record": {
        ("orca/queue/enqueue_publication.py", "_publish_and_complete"),
        ("orca/queue/publication_repair.py", "repair_enqueue_publication_outcome"),
        (_WORKER, "OrcaQueueWorker._on_worker_process_started"),
    },
    "upsert_terminal_job_record": {("orca/queue/settlement.py", "_publish")},
    "merge_job_locations": {("orca/job_locations/_rebuild.py", "rebuild_job_location_records")},
    "rebuild_job_location_records": {("cli_index.py", "cmd_index_rebuild")},
    "prune_job_locations": {("cli_index.py", "cmd_index_prune")},
}


def test_location_index_is_written_only_through_job_records_and_index_commands() -> None:
    offenders = _unowned_references(_LOCATION_INDEX_WRITERS)

    assert offenders == [], offenders


def test_worker_pid_file_is_written_only_by_the_worker() -> None:
    offenders = _unowned_references(
        {
            "write_worker_pid_file": {(_WORKER, "OrcaQueueWorker._write_pid_file")},
            "remove_worker_pid_file": {(_WORKER, "OrcaQueueWorker._remove_pid_file")},
        }
    )

    assert offenders == [], offenders


def test_notifications_are_dispatched_only_from_the_three_claim_sites() -> None:
    offenders = _unowned_references(
        {
            "dispatch_notification": {
                # queued: after the parent's claim on the durable row
                ("orca/queue/notifications.py", "notify_queued_jobs"),
                # started: the child, after it records the attempt
                ("orca/attempt/run.py", "run_attempt"),
                # finished: after the parent's claim in the root job_state.json
                ("orca/queue/notifications.py", "claim_and_send_terminal"),
            }
        }
    )

    assert offenders == [], offenders
