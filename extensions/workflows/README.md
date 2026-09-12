# ORCA_auto workflows

The optional workflow distribution for [ORCA_auto](https://github.com/dhsohn/orca_auto).
It provides CREST→xTB→ORCA pipelines, ORCA-only `scan_ts` workflows, and
supervised workflow workers while
the core distribution owns the `orca_auto` command, shared queue infrastructure
and standalone ORCA execution. Python imports remain `orca_auto.flow`.

The [5.0.0 GitHub release](https://github.com/dhsohn/orca_auto/releases/tag/v5.0.0)
provides core and workflows as separate wheels and source distributions, not
PyPI packages. Both distributions must have exactly the same version. Download
both wheels and install them together into a fresh environment:

```bash
python -m pip install ./orca_auto-5.0.0-py3-none-any.whl ./orca_auto_workflows-5.0.0-py3-none-any.whl
```

For an editable installation from the repository root:

```bash
python -m pip install -e . -e ./extensions/workflows
```

Use the repository installation and operational documentation to configure engines
and systemd templates; installing a wheel alone does not deploy services.
