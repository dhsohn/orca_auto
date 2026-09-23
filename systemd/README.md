# systemd Service Management

**English** | [한국어](README.ko.md)

ORCA_auto uses system-level templated `systemd` units instantiated per user (`@USER`) on Linux and WSL to supervise background worker processes. Units are installed to `/etc/systemd/system/` and managed with standard `systemctl` / `sudo` or through `orca_auto service` commands (not `systemctl --user`).

---

## 1. Unit Architecture

ORCA_auto 7.0 provides three templated systemd units configured per user (`@USER`):

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
The installer renders template units into `/etc/systemd/system/`. It requires `--user` and `--repo` (pointing to a repository checkout containing `.venv` or a prepared runtime root):
```bash
# Render and install systemd templates for the current user
orca_auto systemd install --user "$(id -un)" --repo /path/to/orca_auto --config ~/orca_auto.yaml
```

### Check Service Status
Verifies that running worker processes match the installed systemd unit build:
```bash
orca_auto service status
```

### Safe Service Restart
To protect running calculations from accidental interruption, restarts are only permitted during an idle maintenance window (zero active simulations):
```bash
orca_auto service restart

# Force restart (aborts active calculations; use with caution)
orca_auto service restart --force
```

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
