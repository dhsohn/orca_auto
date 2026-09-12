# ORCA_auto workflows

The optional workflow distribution for [ORCA_auto](https://github.com/dhsohn/orca_auto).
It provides CREST-based conformer screening, ORCA refinement, and supervised
workflow workers while
the core distribution owns the `orca_auto` command, shared queue infrastructure
and standalone ORCA execution. Python imports remain `orca_auto.flow`.

The current source is unreleased `6.0.0.dev0`. Only `conformer_screening`
(`orca_auto scaffold conformer_search <path>`) is supported. The former TS
workflows are removed without compatibility or automatic migration; see the
[cutover warning](../../docs/RELEASE.md#removing-ts-workflows-in-60).

Both distributions must have exactly the same version. For an editable
installation into a fresh environment from the repository root:

```bash
python -m pip install -e . -e ./extensions/workflows
```

Use the repository installation and operational documentation to configure engines
and systemd templates; installing a wheel alone does not deploy services.
The published [5.0.0 release](https://github.com/dhsohn/orca_auto/releases/tag/v5.0.0)
predates this removal; its downloadable packages do not represent the current
development workflow scope.
