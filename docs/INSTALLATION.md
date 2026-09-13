# Install ORCA_auto

**English** | [한국어](INSTALLATION.ko.md)

## Choose a profile

- **Core** (`orca_auto`): standalone ORCA execution, queues, and reports.
- **Core + Workflows**: add `orca_auto_workflows` for CREST-based conformer
  screening and ORCA refinement. The
  extension requires the exact same Core version.

Version `6.0.0` removes the two TS workflows
without compatibility support. Read the [cutover warning](RELEASE.md#removing-ts-workflows-in-60)
before upgrading an existing runtime.

Both profiles use the `orca_auto` command. Python 3.11+ and Linux/WSL2 are
required. Install ORCA separately; workflow stages using xTB or CREST also need
those executables. None of these chemistry engines is bundled in the packages.

## Install release packages

Create a fresh environment and install Core from PyPI:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install orca_auto==6.0.0
```

To add Workflows in that environment:

```bash
python -m pip install 'orca_auto[workflows]==6.0.0'
```

Alternatively, the [v6.0.0 GitHub release](https://github.com/dhsohn/orca_auto/releases/tag/v6.0.0)
provides the same wheels and source distributions, plus `SHA256SUMS`.
Download `orca_auto-6.0.0-py3-none-any.whl`; for Workflows, also download
`orca_auto_workflows-6.0.0-py3-none-any.whl`. Install the local wheel files in a
fresh environment, keeping both distributions at exactly the same version.
The historical [v5.0.0 artifacts](https://github.com/dhsohn/orca_auto/releases/tag/v5.0.0)
predate the TS-workflow removal and were published only on GitHub Releases.

Package installation does **not** configure engines or install/restart systemd
services. The service installer also needs a matching source checkout's
`systemd/` assets. For the complete source-based worker setup, follow the
[quickstart](QUICKSTART.md); see [service documentation](../systemd/README.md)
for operational details.

## Install from source

The [quickstart](QUICKSTART.md) covers bootstrap, configuration, services, and
first submission. A fresh bootstrap installs Core by default;
`bash scripts/bootstrap_wsl.sh --with-workflows` includes the local extension.
Omitting that flag does not remove an extension from a reused `.venv`.

For editable development and verification, follow
[the development guide](DEVELOPMENT.md).

## Upgrade an existing runtime

Keep a running worker's source and environment unchanged. Prepare a fresh
environment and switch only in an idle maintenance window. Do not remove
workflow support from an environment still responsible for workflow state.
Follow the [4.x → 5.x cutover guide](RELEASE.md#moving-from-the-monolithic-4x-installation)
for package, state, and service boundaries, and the
[6.0 TS-workflow removal warning](RELEASE.md#removing-ts-workflows-in-60) for
unsupported old workflow state.

[Back to README](../README.md)
