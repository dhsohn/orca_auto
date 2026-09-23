# Installation

**English** | [한국어](INSTALLATION.ko.md)

Use Linux/WSL2, Python 3.11+ and systemd for supervised execution. Install ORCA
separately according to its license. Workflows and the optional extension were
removed in 7.0; read the [upgrade procedure](RELEASE.md#upgrading-to-70) before
changing an existing installation.

```bash
python3 -m venv ~/.local/share/orca_auto/venv-7.0.0
~/.local/share/orca_auto/venv-7.0.0/bin/python -m pip install orca_auto==7.0.0
~/.local/share/orca_auto/venv-7.0.0/bin/python -m pip check
~/.local/share/orca_auto/venv-7.0.0/bin/orca_auto --version
source ~/.local/share/orca_auto/venv-7.0.0/bin/activate
```

Use a fresh environment without `orca_auto_workflows`. Do not modify an
environment used by active calculations. `init --config /absolute/path/orca_auto.yaml`
creates an external configuration; see the [example](../config/orca_auto.yaml.example).
For services, prepare a versioned [wheel runtime](RUNTIME.md) with the matching
release's systemd templates. Package installation alone does not update workers.

For source development, clone the repository, create `.venv`, then run
`.venv/bin/python -m pip install -e '.[dev]'` and `make check` in an isolated
worktree. Continue with [QUICKSTART](QUICKSTART.md).
