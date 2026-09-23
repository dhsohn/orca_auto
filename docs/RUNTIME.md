# Prepared production runtimes

Prepare each production version in its own directory, using wheels rather than
an editable checkout. Development and verification remain in an isolated source
worktree. Preparation does not install units, restart workers, or access queues.

## Prepare an offline installation

From the matching, validated source worktree and its development environment,
supply the core wheel and every dependency wheel explicitly. The current core
runtime dependency is PyYAML. Choose a PyYAML wheel compatible with the host
Python and platform. Obtain or build these artifacts before preparation; the
preparer never contacts a package index.

```bash
.venv/bin/python -m scripts.prepare_runtime \
  --wheel "$CORE_WHEEL" \
  --wheel "$PYYAML_WHEEL" \
  --releases-root "$HOME/.local/share/orca_auto/releases"
```

Version 7 accepts the ORCA package and its dependencies; the retired workflow
extension cannot be included. Follow [the 7.0 cutover](RELEASE.md#upgrading-to-70)
when replacing an older installation.
`--templates` optionally selects the matching `systemd/` template directory.

The command prints JSON containing `runtime_root` and the full SHA-256 `build_id`.
Set `RUNTIME_ROOT` to that exact returned absolute path for the commands below.
The path is `<releases-root>/<version>-<build-id-prefix>` and contains:

- the original wheels and their matching unit templates;
- `.venv`, installed offline with dependency consistency and import checks;
- `orca_auto_runtime.json`, containing build inputs and an inventory of installed
  bytes, permissions, directories, and internal symbolic links.

Build identity includes wheel and template digests, Python version, host platform,
and the base interpreter path/digest. Ready files and directories have all write
bits removed. The installer, worker startup, and status checks reject changed
bytes, writable paths, external symlinks, moved installations, and incomplete
preparations. Identical preparation inputs reuse a verified existing directory.

Preparation never replaces an existing directory. An interrupted attempt remains
marked incomplete and cannot be used or automatically retried in place. Inspect
the failure and prepare under another releases root. Remove an abandoned partial
directory only after confirming its ownership and that no service uses it.

## Switch during an idle maintenance window

Keep configuration, `runs_root`, admission state, logs, and scratch outside the
prepared runtime. Preserve the currently deployed configuration and state during
the switch. Configure the chemical engine executables separately.

1. Check the current installation's `queue list --json` and wait for
   `active_simulations: 0`. Keep the existing source, environment, and configuration
   intact until the maintenance window.
2. Install units pinned to the returned runtime path, without starting services:

   ```bash
   "$RUNTIME_ROOT/.venv/bin/python" -I -m orca_auto.cli systemd install \
     --user "$(id -un)" --repo "$RUNTIME_ROOT" \
     --config "$EXISTING_CONFIG" --no-start
   ```

   Preserve `--worker-only` when that is the intended service mode. A managed
   runtime requires explicit configuration outside its directory. The unit uses
   isolated Python imports, pins the build identity in its environment, and mounts
   the runtime read-only through `ReadOnlyPaths`.
3. Restart through the existing admission guard:

   ```bash
   "$RUNTIME_ROOT/.venv/bin/python" -I -m orca_auto.cli service restart
   "$RUNTIME_ROOT/.venv/bin/python" -I -m orca_auto.cli service status --json
   ```

   The restart holds the shared admission lock and refuses active or unresolved
   engine reservations. Initial `queue list` output is an observation; this guard
   checks again at the actual restart. Installing unit files or restarting only a
   target does not replace an already running member worker.

For a running managed worker, status reports `runtime_build_id`, `runtime_version`,
and `source_root`, and compares them with the installed unit's
`expected_runtime_build_id` and `expected_runtime_root`. A different process build
or root is `stale`, including the first switch from editable code. Unreadable or
inconsistent evidence is `undetermined`. Both exit nonzero even if unit health
alone is `ok: true`. A switch is complete only after status verifies the active
worker against the desired installation.

## Roll back and retain evidence

Keep the old runtime while any process uses it. To roll back, select its original
verified path and repeat the same idle install/restart/status sequence. Do not
redirect a symlink beneath running workers, move directories, edit installed
files, or run pip inside a prepared runtime. Rebuild into a new directory instead.

Read-only installation is protection against accidental in-place deployment;
the owning account can deliberately change permissions. The receipt is not a
publisher signature. The virtual environment also depends on the host Python
standard library and operating-system shared libraries: it is not a self-contained
container or a guarantee against host updates. Configuration and calculation data
are intentionally mutable and are not part of the build receipt.

`make check-packages` verifies real wheel preparation, idempotent reuse, a fake
ORCA worker using the installed interpreter, and rendering the pinned service
plan. The unit tests cover byte/permission rejection, idle/busy restart, and
old-process/new-unit mismatch. These checks do not deploy production services.
