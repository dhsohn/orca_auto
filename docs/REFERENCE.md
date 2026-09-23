# Command reference

**English** | [한국어](REFERENCE.ko.md)

Use `orca_auto --help` and each command's `--help` for the full argument list.
Stable behavior is defined in [PUBLIC_CONTRACTS](PUBLIC_CONTRACTS.md).

| Command | Options |
| --- | --- |
| `init` | `--config PATH`, `--force` |
| `run-dir PATH` | `--config PATH`, `--force`, `--priority N`, `--json` |
| `queue list` | `--config PATH`, `--engine orca`, `--kind job`, `--status STATUS`, `--limit N`, `--refresh`, `--json` |
| `queue list clear` | `--config PATH`, `--json`; no list filters |
| `queue cancel TARGET` | `--config PATH`, `--json` |
| `index prune` | `--config PATH`, `--apply`, `--json` |
| `queue worker` | `--config PATH`, `--app orca`, `--json` to inspect the plan |
| `systemd install` | `--user USER`, `--repo PATH`, `--config PATH`, `--worker-only` |
| `service status` | `--json` |
| `service restart` | `--force` bypasses idle protection and can interrupt calculations |

`--orca_auto-config` is the shared-config alias. Resource values come from
the ORCA input; edit `%pal`/`%maxcore` instead of passing command-line overrides.
Queue storage uses pending/running/completed/failed/cancelled states; a successful
submission reports queued. A listed `repair_blocked` row has unresolved publication
evidence. `admission_blockers` remains visible under filters and limits.

Configuration discovery: explicit path → `ORCA_AUTO_CONFIG` → checkout
`config/orca_auto.yaml` → `~/orca_auto/config/orca_auto.yaml`. The
[configuration example](../config/orca_auto.yaml.example) lists accepted keys.

Each execution writes a generation containing bound inputs, raw ORCA output,
private `job_state.json`, and terminal `machine.json`. Human HTML/SI reports
depend on calculation type and available evidence. Queue list does not establish
chemical acceptance. For worker logs:

```bash
journalctl -u "orca_auto-queue-worker@$(id -un)" -f
```

See [RUNTIME](RUNTIME.md) for service preparation and [VALIDATION](VALIDATION.md)
for scientific acceptance. Removed workflow commands/config are documented only
in the [7.0 cutover guide](RELEASE.md#upgrading-to-70).
