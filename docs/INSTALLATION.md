# Install ORCA_auto

**English** | [한국어](INSTALLATION.ko.md)

## Choose a profile

- **Core** (`orca_auto`): standalone ORCA execution, queues, and reports.
- **Core + Workflows** (`orca_auto[workflows]`): adds `orca_auto_workflows` for CREST-based conformer screening and ORCA refinement.

Both profiles use the unified `orca_auto` CLI. Python 3.11+ and Linux/WSL2 are required.
Note: Chemistry engines (ORCA, and optionally xTB/CREST for workflows) must be installed separately on your system.

## Install release packages

Create a virtual environment and install from PyPI:

```bash
python3 -m venv .venv
source .venv/bin/activate

# Core only
python -m pip install orca_auto==6.0.0

# Core + Workflows
python -m pip install 'orca_auto[workflows]==6.0.0'
```

Alternatively, pre-built wheels and source archives are available on the [GitHub Releases page](https://github.com/dhsohn/orca_auto/releases/tag/v6.0.0).

> **Note**: After installing the package, register the systemd services to run background workers. See the [quickstart](QUICKSTART.md) and [service documentation](../systemd/README.md) for complete setup instructions.

## Install from source

Follow the [quickstart](QUICKSTART.md) for bootstrap and worker setup from source:

```bash
cd <repo_root>

# Core only
bash scripts/bootstrap_wsl.sh

# Core + Workflows
bash scripts/bootstrap_wsl.sh --with-workflows
```

For editable development installations and testing, see the [development guide](DEVELOPMENT.md).

## Upgrading an existing runtime

To upgrade safely without interrupting active simulations, prepare a new virtual environment and switch when calculations are complete.

For breaking changes and migration details between major versions, see [RELEASE.md](RELEASE.md).

[Back to README](../README.md)
