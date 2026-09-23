# Quickstart

**English** | [한국어](QUICKSTART.ko.md)

Follow [installation](INSTALLATION.md), prepare an external config and install
ORCA separately. For services use the verified runtime path from [RUNTIME](RUNTIME.md).

```bash
orca_auto init --config /home/user/orca_auto-config.yaml
orca_auto systemd install --user user --repo /absolute/runtime/root \
  --config /home/user/orca_auto-config.yaml
orca_auto service status --json
orca_auto run-dir /home/user/orca_runs/water --config /home/user/orca_auto-config.yaml --json
orca_auto queue list --config /home/user/orca_auto-config.yaml --json
```

Place the input directory below the configured `runs_root`, with an ORCA
`.inp` and its referenced files. Set resources through `%pal`/`%maxcore`.
Successful submission means durable acceptance, not completed chemistry.
Cancel with `orca_auto queue cancel TARGET --config /home/user/orca_auto-config.yaml`; inspect logs with
`journalctl -u orca_auto-queue-worker@user -f`. Validate terminal `machine.json`
and the scientific evidence in raw output. Do not update an active environment.
