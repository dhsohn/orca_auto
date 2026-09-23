"""Read-only ownership boundary for retired workflow directories."""

from __future__ import annotations

from pathlib import Path

_RETIRED_WORKFLOW_MARKERS = ("flow.yaml", "workflow.json")


def path_is_retired_workflow_owned(path: str | Path, root: str | Path) -> bool:
    """Exclude retired workspaces without loading or changing their state.

    Resolving first also handles fd-pinned submission paths. Marker symlinks and
    unreadable ownership evidence fail closed; an unrelated path outside root
    is left to the caller's ordinary root-confinement check.
    """
    try:
        resolved_root = Path(root).expanduser().resolve()
        current = Path(path).expanduser().resolve()
        if not current.is_relative_to(resolved_root):
            return False
        if current.is_file():
            current = current.parent
        if not current.is_relative_to(resolved_root):
            return True
        while True:
            for marker in _RETIRED_WORKFLOW_MARKERS:
                try:
                    (current / marker).lstat()
                except FileNotFoundError:
                    continue
                return True
            if current == resolved_root:
                return False
            current = current.parent
    except (OSError, RuntimeError, ValueError):
        return True
