# ORCA_auto workflows

The optional workflow distribution for [ORCA_auto](https://github.com/dhsohn/orca_auto).
It provides CREST-based conformer screening, ORCA refinement, and supervised
workflow workers while the core distribution owns the `orca_auto` command,
shared queue infrastructure and standalone ORCA execution. Python imports remain
`orca_auto.flow`.

Version `6.0.0` supports only `conformer_screening`
(`orca_auto scaffold conformer_search <path>`). The former TS
workflows are removed without compatibility or automatic migration; see the
[cutover warning](https://github.com/dhsohn/orca_auto/blob/v6.0.0/docs/RELEASE.md#removing-ts-workflows-in-60).

Both distributions must have exactly the same version. In a fresh environment,
install the matching pair from PyPI:

```bash
python -m pip install 'orca_auto[workflows]==6.0.0'
```

For an editable development installation from the repository root:

```bash
python -m pip install -e . -e ./extensions/workflows
```

Follow the [installation guide](https://github.com/dhsohn/orca_auto/blob/v6.0.0/docs/INSTALLATION.md)
to configure engines and systemd templates; installing a wheel alone does not
deploy services. The historical
[5.0.0 release](https://github.com/dhsohn/orca_auto/releases/tag/v5.0.0)
predates the TS-workflow removal and does not represent the 6.0 workflow scope.
