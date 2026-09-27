"""Static ownership guards for ``queue.json`` rows.

* The file is written only by ``core/queue/store.py`` (``mutate_entries``).
* A row's status moves to PENDING or a terminal status only through
  ``transitions.requeued_entry`` or ``transitions.terminal_entry``.
* A row is created only by ``adapter.enqueue`` and loaded only by
  ``persistence.entry_from_dict``; no rewrite sets ``enqueued_at``, which is
  part of every writer fence's generation identity.
* An admission slot is released and its engine process completed only by the
  listed owners: the worker parent's release, the child's one slot rule
  (``execution._child_admission_slot``) and the engine-process registrar and
  dead-owner recovery.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

from orca_auto.core.queue import transitions
from orca_auto.core.queue.types import QueueStatus

_PACKAGE_ROOT = Path(transitions.__file__).resolve().parents[2]
_STORE_OWNERS = {"core/queue/store.py", "core/queue/persistence.py"}


def _nodes() -> Iterator[tuple[str, str, ast.AST]]:
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

    for source_path in sorted(_PACKAGE_ROOT.rglob("*.py")):
        relative = source_path.relative_to(_PACKAGE_ROOT).as_posix()
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
        yield from walk(tree, (), relative)


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


_SLOT_MUTATION_OWNERS = {
    "release_slot": {
        ("orca/queue/worker.py", "OrcaQueueWorker._release_admission_slot"),
        ("orca/execution.py", "_child_admission_slot"),
    },
    "complete_slot_engine_process": {
        ("core/admission/engine_process.py", "register_slot_engine_process"),
        ("core/admission/engine_process.py", "_clear_dead_owner_pending_launch"),
        ("orca/execution.py", "_child_admission_slot"),
    },
}


def test_admission_slots_are_released_and_completed_only_by_their_owners() -> None:
    offenders = [
        f"{path}:{node.lineno} ({scope}) uses {name}"
        for path, scope, node in _nodes()
        for name, owners in _SLOT_MUTATION_OWNERS.items()
        if (
            (isinstance(node, ast.Name) and node.id == name)
            or (isinstance(node, ast.Attribute) and node.attr == name)
        )
        and (path, scope) not in owners
    ]

    assert offenders == [], offenders
