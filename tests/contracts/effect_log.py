"""Test-only log of durable writes across the worker parent and its children.

``install`` wraps the canonical writers of each public on-disk file. Every
successful write appends one JSON line to the file named by
``ORCA_AUTO_TEST_EFFECT_LOG`` with ``O_APPEND``, so the parent (the test
process) and worker children interleave in real write order. Children log
through ``sitecustomize/sitecustomize.py`` when that directory is on
``PYTHONPATH``. Production code has no hook: only module attributes are
replaced, and nothing is logged while the variable is unset.
"""

from __future__ import annotations

import functools
import importlib
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

ENV_VAR = "ORCA_AUTO_TEST_EFFECT_LOG"
WORKER_CHILD_MODULE = "orca_auto.orca.commands.worker_child"
SITECUSTOMIZE_DIR = Path(__file__).resolve().with_name("sitecustomize")

_TERMINAL_REPLAY_METADATA_KEY = "orca_terminal_replay"
_ROOT_STATE_LABEL = "ORCA state"
_STATE_FILE = "job_state.json"

Setattr = Callable[[object, str, object], None]


def _role() -> str:
    return "child" if WORKER_CHILD_MODULE in sys.orig_argv else "parent"


def _append(event: dict[str, Any]) -> None:
    path = os.environ.get(ENV_VAR)
    if not path:
        return
    line = json.dumps({"role": _role(), **event}) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)


def _queue_event(_root: object, entries: Any, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
    rows = []
    for entry in entries:
        row = str(entry.status.value)
        if entry.metadata.get(_TERMINAL_REPLAY_METADATA_KEY):
            row += "+replay"
        rows.append(row)
    return {"file": "queue.json", "rows": rows}


def _admission_event(_root: object, slots: Any, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
    return {
        "file": "admission_slots.json",
        "slots": [f"{slot.state}/{slot.engine_process_state}" for slot in slots],
    }


def _index_event(_root: object, records: Any, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
    return {"file": "job_locations.json", "rows": [record.status for record in records]}


def _confined_event(
    _directory: object, path: Path, payload: bytes, *_args: Any, **kwargs: Any
) -> dict[str, Any]:
    name = Path(path).name
    scope = "root" if kwargs.get("label") == _ROOT_STATE_LABEL else "generation"
    event: dict[str, Any] = {"file": f"{scope}/{name}"}
    if name == _STATE_FILE:
        event["status"] = json.loads(payload)["status"]["state"]
    return event


def _intent_write_event(_path: object, marker: Any, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
    return {"file": "snapshot_intent", "state": marker.get("state"), "keys": list(marker)}


def _intent_unlink_event(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
    return {"file": "snapshot_intent", "state": "removed"}


def _notify_event(*_args: Any, **kwargs: Any) -> dict[str, Any]:
    return {"notify": kwargs.get("kind")}


# (module, attribute, event builder, replace every alias of the original).
# Aliases are replaced only for functions the module defines; a shared low-level
# writer is wrapped only in the one module whose writes it classifies.
_TARGETS: tuple[tuple[str, str, Callable[..., dict[str, Any]], bool], ...] = (
    ("orca_auto.core.queue.persistence", "save_entries", _queue_event, True),
    ("orca_auto.core.admission.persistence", "save_slots", _admission_event, True),
    ("orca_auto.core.indexing.store", "_save_records", _index_event, True),
    ("orca_auto.orca.state", "atomic_write_confined_bytes", _confined_event, False),
    ("orca_auto.orca.report.publication", "atomic_write_confined_bytes", _confined_event, False),
    ("orca_auto.orca.report.si", "atomic_write_confined_bytes", _confined_event, False),
    (
        "orca_auto.core.queue.engine.snapshot_intent",
        "atomic_write_json",
        _intent_write_event,
        False,
    ),
    ("orca_auto.core.queue.engine.snapshot_intent", "_unlink_intent", _intent_unlink_event, True),
    ("orca_auto.orca.notifications", "dispatch_notification", _notify_event, True),
)


def _logged(original: Callable[..., Any], build: Callable[..., dict[str, Any]]) -> Any:
    @functools.wraps(original)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        _append(build(*args, **kwargs))
        return result

    return wrapper


def install(set_attr: Setattr = setattr) -> None:
    """Wrap every target writer; ``set_attr`` may be ``monkeypatch.setattr`` to undo later."""
    for module_name, attribute, build, sweep_aliases in _TARGETS:
        module = importlib.import_module(module_name)
        original = getattr(module, attribute)
        wrapper = _logged(original, build)
        set_attr(module, attribute, wrapper)
        if not sweep_aliases:
            continue
        for name, loaded in list(sys.modules.items()):
            if not name.startswith("orca_auto") or loaded is None or loaded is module:
                continue
            for alias, value in list(vars(loaded).items()):
                if value is original:
                    set_attr(loaded, alias, wrapper)


def install_in_worker_child() -> None:
    """``sitecustomize`` entry: log only from worker children, never the ORCA launch gate."""
    if os.environ.get(ENV_VAR) and _role() == "child":
        install()


def read(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
