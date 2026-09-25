# systemd Service Management

**English** | [한국어](README.ko.md)

ORCA_auto uses system-level templated `systemd` units instantiated per user (`@USER`) on Linux and WSL to supervise background worker processes. Units are installed to `/etc/systemd/system/` and managed with standard `systemctl` / `sudo` or through `orca_auto service` commands (not `systemctl --user`).

---

## 1. Unit Architecture

ORCA_auto provides three templated systemd units configured per user (`@USER`):

```text
orca_auto-runtime@USER.target          # Top-level runtime target
  └─ orca_auto-engine-workers@USER.target # Engine worker grouping target
       └─ orca_auto-queue-worker@USER.service  # Supervised ORCA queue worker process
```

- **`orca_auto-queue-worker@USER.service`**: The resident worker service polling the queue and launching ORCA jobs.
- **`orca_auto-engine-workers@USER.target`**: Groups all calculation worker services.
- **`orca_auto-runtime@USER.target`**: Controls the overall lifecycle of the ORCA_auto runtime.

> **Note**: Following the retirement of workflows in 7.0, `orca_auto-workflow-worker@.service` is no longer provided.

---

## 2. Service Management Commands

### Install Units
The installer renders template units into `/etc/systemd/system/`. It requires `--user` and `--repo` (pointing to a repository checkout containing `.venv` or a prepared runtime root). `--config` defaults to the target user's `~/orca_auto/config/orca_auto.yaml`; the unit binds that path through `ORCA_AUTO_CONFIG`, its `ExecStart` runs `queue worker` without engine options, and `TimeoutStopSec` is rendered from the configured `scheduler.max_active_simulations` (see below). A config that exists but does not load fails the install before any unit is written:
```bash
# Render and install systemd templates for the current user
orca_auto systemd install --user "$(id -un)" --repo /path/to/orca_auto --config ~/orca_auto.yaml
```

### Check Service Status
Verifies unit health and that running worker processes match the checkout HEAD or the installed runtime build. Returns non-zero when a unit is unhealthy or a worker is stale or undetermined:
```bash
orca_auto service status
```

### Service Restart
To protect running calculations from accidental interruption, restarts are only permitted during an idle maintenance window (zero active simulations):
```bash
orca_auto service restart

# Force restart (aborts active calculations; use with caution)
orca_auto service restart --force
```

### Stop Behaviour
`systemctl stop` sends SIGTERM only to the supervisor (`KillMode=mixed`). The supervisor forwards the stop to the queue worker, which sends SIGTERM to every ORCA child at once, waits up to 10 s for each, SIGKILLs what is still running and requeues its row. `TimeoutStopSec` is rendered at install time as `scheduler.max_active_simulations` × 15 s + 27 s (87 s for the default of 4); only when it expires does systemd SIGKILL the whole control group.

---

## 3. Real-time Worker Logs

Monitor worker scheduling, dequeue events, and simulation transitions via systemd journal:

```bash
journalctl -u "orca_auto-queue-worker@$(id -un)" -f
```

---

## 4. Upgrading to 7.0

If you have legacy `orca_auto-workflow-worker` services from 6.x or earlier, ensure remaining jobs have finished or been cancelled, then stop and disable them:

```bash
sudo systemctl stop "orca_auto-workflow-worker@$(id -un)"
sudo systemctl disable "orca_auto-workflow-worker@$(id -un)"
```

See the [7.0 Upgrade Guide](../docs/RELEASE.md#upgrading-to-70) for complete cutover instructions.
