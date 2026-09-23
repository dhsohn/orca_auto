# systemd services

**English** | [한국어](README.ko.md)

`orca_auto-queue-worker@USER.service` runs the ORCA worker.
`orca_auto-engine-workers@USER.target` groups that worker and
`orca_auto-runtime@USER.target` selects the runtime. There are three templates.

```bash
orca_auto systemd install --user user --repo /absolute/runtime/root \
  --config /absolute/external/orca_auto.yaml
orca_auto service status --json
orca_auto service restart
```

Installing units does not reload an existing worker. Cut over only while idle
and verify the process build/root against the unit. Prepared runtimes are read-only;
configuration, queues, logs and scratch remain external. Follow [RUNTIME](../docs/RUNTIME.md).

Version 7 does not provide a workflow worker. Finish/cancel old work with its old
runtime, then stop/disable any old instance. Installing new units does not delete
historical templates. Follow the [upgrade guide](../docs/RELEASE.md#upgrading-to-70).
