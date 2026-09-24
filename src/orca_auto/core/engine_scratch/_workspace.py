"""``EngineScratchWorkspace`` and its lifecycle: ``create`` prepares the root,
takes the root lock, recovers the durable generation, sweeps peers for
capacity accounting, stages inputs and writes the manifest; ``publish``,
``cleanup`` and ``discard_unlaunched`` drive the later phases. The launch
sweep and the tombstone-based owned-workspace removal live here because
they are the workspace's own bookkeeping.
"""

from __future__ import annotations

import os
import secrets
import shutil
import stat
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Self

from orca_auto.core.confined_io import require_confined_regular_file
from orca_auto.core.utils import stable_fs
from orca_auto.core.utils.lock import FileLockTimeoutError, file_lock_at

from ._constants import (
    _CLEANUP_TOMBSTONE_NAME_RE,
    _CLEANUP_TOMBSTONE_PREFIX,
    _SCRATCH_ROOT_LOCK_FILE_NAME,
    _SCRATCH_ROOT_LOCK_TIMEOUT_SECONDS,
    SCRATCH_WORKSPACE_PREFIX,
)
from ._errors import (
    EngineScratchCapacityError,
    EngineScratchError,
)
from ._fs import (
    _open_pinned_directory,
    _open_pinned_directory_at,
    _require_directory_path_identity,
)
from ._manifest import (
    _inspect_workspace_entry,
    _write_workspace_manifest,
)
from ._policy import (
    EngineScratchPolicy,
    _filesystem_free_bytes,
    _linux_available_memory_bytes,
    _prepare_scratch_root,
)
from ._publication import (
    ScratchPublication,
    _publish_workspace,
    _recover_incomplete_publication,
)
from ._staging import (
    _capture_input_closure,
    _input_closure_size_bytes,
    _stage_input_closure,
    _StagedInput,
    _verify_staged_sources_unchanged,
)


@dataclass
class EngineScratchWorkspace:
    policy: EngineScratchPolicy
    durable_input: Path
    durable_output_dir: Path
    path: Path
    scratch_input: Path
    staged_inputs: dict[str, _StagedInput] = field(default_factory=dict)
    input_dir_fd: int = -1
    input_dir_identity: tuple[int, int] = (-1, -1)
    durable_dir_fd: int = -1
    durable_dir_identity: tuple[int, int] = (-1, -1)
    scratch_root_identity: tuple[int, int] = (-1, -1)
    workspace_identity: tuple[int, int] = (-1, -1)
    workspace_dir_fd: int = -1
    _published: bool = False
    _closed: bool = False

    @classmethod
    def create(
        cls,
        policy: EngineScratchPolicy,
        durable_input: Path,
        *,
        durable_output_dir: Path | None = None,
        expected_durable_dir_identity: tuple[int, int] | None = None,
    ) -> Self:
        durable = require_confined_regular_file(
            durable_input.parent,
            durable_input,
            label="engine durable scratch input",
        )
        output_dir = Path(durable_output_dir or durable.parent).expanduser().resolve()
        if not durable.is_relative_to(output_dir):
            raise EngineScratchError(
                "engine durable scratch input must stay inside its publication directory"
            )
        root = _prepare_scratch_root(policy)
        root_status = root.stat()
        root_identity = (int(root_status.st_dev), int(root_status.st_ino))
        workspace = root / f"{SCRATCH_WORKSPACE_PREFIX}{os.getpid()}-{secrets.token_hex(8)}"
        input_dir_fd = -1
        durable_dir_fd = -1
        workspace_dir_fd = -1
        workspace_identity = (-1, -1)
        root_fd, _observed_root_identity = _open_pinned_directory(
            root,
            label="engine scratch root",
            expected_identity=root_identity,
        )
        try:
            with ExitStack() as root_lock:
                try:
                    root_lock.enter_context(
                        file_lock_at(
                            root_fd,
                            _SCRATCH_ROOT_LOCK_FILE_NAME,
                            display_path=root / _SCRATCH_ROOT_LOCK_FILE_NAME,
                            timeout_seconds=_SCRATCH_ROOT_LOCK_TIMEOUT_SECONDS,
                        )
                    )
                except FileLockTimeoutError as exc:
                    raise EngineScratchCapacityError(
                        "engine scratch root stayed busy with a peer workspace for "
                        f"{_SCRATCH_ROOT_LOCK_TIMEOUT_SECONDS:.0f} s"
                    ) from exc
                _require_directory_path_identity(
                    root,
                    root_fd,
                    root_identity,
                    label="engine scratch root",
                )
                input_dir_fd, input_dir_identity = _open_pinned_directory(
                    durable.parent,
                    label="engine durable scratch input directory",
                )
                try:
                    durable_dir_fd, durable_dir_identity = _open_pinned_directory(
                        output_dir,
                        label="engine durable generation",
                        expected_identity=expected_durable_dir_identity,
                    )
                    _recover_incomplete_publication(
                        durable_dir_fd,
                        output_dir,
                        durable_dir_identity,
                    )
                    live_task_memory_bytes = _sweep_scratch_root(root, root_fd)
                    captured_inputs = _capture_input_closure(
                        input_dir_fd,
                        durable.parent,
                        durable.name,
                        dependency_names_from_primary=policy.dependency_names_from_primary,
                    )
                    required_bytes = _input_closure_size_bytes(
                        durable.name,
                        captured_inputs,
                        normalize_primary_newline=policy.normalize_primary_newline,
                    )
                    free_bytes = _filesystem_free_bytes(root_fd)
                    if free_bytes < policy.min_free_bytes + required_bytes:
                        raise EngineScratchCapacityError(
                            "engine scratch has insufficient free space: "
                            f"free={free_bytes}, required_inputs={required_bytes}, "
                            f"minimum_free={policy.min_free_bytes}"
                        )
                    # Live workspaces may still grow to their own task-memory
                    # caps after this snapshot, so those caps count in full;
                    # the tmpfs pool is shared and counts once.
                    available_memory_bytes = _linux_available_memory_bytes()
                    required_memory_bytes = (
                        live_task_memory_bytes
                        + policy.max_task_memory_bytes
                        + free_bytes
                        + policy.min_free_bytes
                    )
                    if available_memory_bytes < required_memory_bytes:
                        raise EngineScratchCapacityError(
                            "engine scratch cannot guarantee RAM headroom without swap: "
                            f"available_memory={available_memory_bytes}, "
                            f"task_memory_limit={policy.max_task_memory_bytes}, "
                            f"live_task_memory_limits={live_task_memory_bytes}, "
                            f"scratch_free={free_bytes}, host_reserve={policy.min_free_bytes}"
                        )
                    os.mkdir(workspace.name, mode=0o700, dir_fd=root_fd)
                    workspace_dir_fd, workspace_identity = _open_pinned_directory_at(
                        root_fd,
                        workspace.name,
                        display_path=workspace,
                        label="engine scratch workspace",
                    )
                    _write_workspace_manifest(
                        output_dir,
                        workspace_dir_fd=workspace_dir_fd,
                        max_task_memory_bytes=policy.max_task_memory_bytes,
                    )
                    staged = _stage_input_closure(
                        durable,
                        workspace_dir_fd=workspace_dir_fd,
                        captured_inputs=captured_inputs,
                        normalize_primary_newline=policy.normalize_primary_newline,
                    )
                    if _filesystem_free_bytes(root_fd) < policy.min_free_bytes:
                        raise EngineScratchCapacityError(
                            "engine scratch fell below its minimum free-space reserve while staging"
                        )
                    _require_directory_path_identity(
                        root,
                        root_fd,
                        root_identity,
                        label="engine scratch root",
                    )
                    _require_directory_path_identity(
                        workspace,
                        workspace_dir_fd,
                        workspace_identity,
                        label="engine scratch workspace",
                    )
                    return cls(
                        policy=policy,
                        durable_input=durable,
                        durable_output_dir=output_dir,
                        path=workspace,
                        scratch_input=workspace / durable.name,
                        staged_inputs=staged,
                        input_dir_fd=input_dir_fd,
                        input_dir_identity=input_dir_identity,
                        durable_dir_fd=durable_dir_fd,
                        durable_dir_identity=durable_dir_identity,
                        scratch_root_identity=root_identity,
                        workspace_identity=workspace_identity,
                        workspace_dir_fd=workspace_dir_fd,
                    )
                except BaseException as create_error:
                    if input_dir_fd >= 0:
                        os.close(input_dir_fd)
                    if durable_dir_fd >= 0:
                        os.close(durable_dir_fd)
                    if workspace_dir_fd >= 0:
                        os.close(workspace_dir_fd)
                    if workspace_identity != (-1, -1):
                        try:
                            _remove_owned_workspace(
                                root,
                                root_identity,
                                workspace.name,
                                workspace_identity,
                            )
                        except (EngineScratchError, OSError) as removal_error:
                            if isinstance(create_error, EngineScratchCapacityError):
                                # A capacity refusal promises that nothing was left behind.
                                raise EngineScratchError(
                                    "engine scratch workspace could not be removed after a "
                                    f"capacity refusal: {workspace}"
                                ) from removal_error
                    raise
        finally:
            os.close(root_fd)

    def publish(self) -> ScratchPublication:
        if self._published:
            raise EngineScratchError("engine scratch workspace was already published")
        self._require_open()
        _require_directory_path_identity(
            self.path,
            self.workspace_dir_fd,
            self.workspace_identity,
            label="engine scratch workspace",
        )
        _require_directory_path_identity(
            self.durable_input.parent,
            self.input_dir_fd,
            self.input_dir_identity,
            label="engine durable scratch input directory",
        )
        _require_directory_path_identity(
            self.durable_output_dir,
            self.durable_dir_fd,
            self.durable_dir_identity,
            label="engine durable generation",
        )
        _verify_staged_sources_unchanged(
            self.input_dir_fd,
            self.durable_input.parent,
            self.staged_inputs,
        )
        publication = _publish_workspace(
            self.path,
            self.workspace_dir_fd,
            self.workspace_identity,
            self.durable_output_dir,
            self.durable_dir_fd,
            self.durable_dir_identity,
            self.scratch_input.stem,
            self.staged_inputs,
        )
        self._published = True
        return publication

    def cleanup(self) -> None:
        if not self._published:
            raise EngineScratchError("Refusing to remove unpublished engine scratch workspace")
        self._remove_workspace()

    def discard_unlaunched(self) -> None:
        """Remove a workspace whose engine was never started.

        It holds only staged copies of durable inputs, so there is nothing to
        publish. The caller owns the guarantee that no engine ran in it.
        """
        if self._published:
            raise EngineScratchError("engine scratch workspace was already published")
        self._remove_workspace()

    def _remove_workspace(self) -> None:
        self._require_open()
        root_fd, _observed_identity = _open_pinned_directory(
            self.policy.root,
            label="engine scratch root",
            expected_identity=self.scratch_root_identity,
        )
        try:
            with file_lock_at(
                root_fd,
                _SCRATCH_ROOT_LOCK_FILE_NAME,
                display_path=self.policy.root / _SCRATCH_ROOT_LOCK_FILE_NAME,
                timeout_seconds=_SCRATCH_ROOT_LOCK_TIMEOUT_SECONDS,
            ):
                _remove_owned_workspace_at(
                    root_fd,
                    self.path.name,
                    self.workspace_identity,
                )
        finally:
            os.close(root_fd)
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        if self.input_dir_fd >= 0:
            os.close(self.input_dir_fd)
        if self.durable_dir_fd >= 0:
            os.close(self.durable_dir_fd)
        if self.workspace_dir_fd >= 0:
            os.close(self.workspace_dir_fd)
        self.input_dir_fd = -1
        self.durable_dir_fd = -1
        self.workspace_dir_fd = -1
        self._closed = True

    def _require_open(self) -> None:
        if (
            self._closed
            or self.input_dir_fd < 0
            or self.durable_dir_fd < 0
            or self.workspace_dir_fd < 0
        ):
            raise EngineScratchError("engine scratch workspace is already closed")


def _sweep_scratch_root(root: Path, root_fd: int) -> int:
    """Finish interrupted cleanups and return the summed task-memory caps of live workspaces.

    Any workspace whose ownership cannot be resolved, or whose owner is gone,
    is preserved for inspection and blocks the new launch.
    """
    live_task_memory_bytes = 0
    for name in os.listdir(root_fd):
        if _CLEANUP_TOMBSTONE_NAME_RE.fullmatch(name):
            # A tombstone is a workspace renamed for deletion whose rmtree was
            # interrupted. Completing the removal is the recorded intent;
            # leaving it would pin tmpfs RAM invisibly. The caller holds the
            # scratch-root lock, so no live cleanup can be mid-removal here.
            info = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
            if not stat.S_ISDIR(info.st_mode):
                raise EngineScratchError(
                    f"engine scratch root contains an unsafe entry: {root / name}"
                )
            shutil.rmtree(name, dir_fd=root_fd)
            continue
        if not name.startswith(SCRATCH_WORKSPACE_PREFIX):
            continue
        report, _identity = _inspect_workspace_entry(root, root_fd, name, measure_size=False)
        if report.blocks_launch:
            raise EngineScratchError(report.detail)
        live_task_memory_bytes += report.max_task_memory_bytes or 0
    return live_task_memory_bytes


def _remove_owned_workspace(
    root: Path,
    root_identity: tuple[int, int],
    workspace_name: str,
    workspace_identity: tuple[int, int],
) -> None:
    root_fd, _observed_root_identity = _open_pinned_directory(
        root,
        label="engine scratch root",
        expected_identity=root_identity,
    )
    try:
        _remove_owned_workspace_at(root_fd, workspace_name, workspace_identity)
    finally:
        os.close(root_fd)


def _remove_owned_workspace_at(
    root_fd: int,
    workspace_name: str,
    workspace_identity: tuple[int, int],
) -> None:
    tombstone_name = f"{_CLEANUP_TOMBSTONE_PREFIX}{secrets.token_hex(16)}"
    before = os.stat(workspace_name, dir_fd=root_fd, follow_symlinks=False)
    if not stat.S_ISDIR(before.st_mode) or stable_fs.inode_identity(before) != workspace_identity:
        raise EngineScratchError("engine scratch workspace identity changed before cleanup")
    os.rename(
        workspace_name,
        tombstone_name,
        src_dir_fd=root_fd,
        dst_dir_fd=root_fd,
    )
    after = os.stat(tombstone_name, dir_fd=root_fd, follow_symlinks=False)
    if not stat.S_ISDIR(after.st_mode) or stable_fs.inode_identity(after) != workspace_identity:
        try:
            os.rename(
                tombstone_name,
                workspace_name,
                src_dir_fd=root_fd,
                dst_dir_fd=root_fd,
            )
        except OSError:
            pass
        raise EngineScratchError("engine scratch workspace identity changed during cleanup")
    shutil.rmtree(tombstone_name, dir_fd=root_fd)
    os.fsync(root_fd)
