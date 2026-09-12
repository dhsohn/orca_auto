# Install ORCA_auto

**English** | [한국어](INSTALLATION.ko.md)

## Choose a profile

- **Core** (`orca_auto`): standalone ORCA execution, queues, and reports.
- **Core + Workflows**: add `orca_auto_workflows` for CREST-based conformer
  screening and ORCA refinement. The
  extension requires the exact same Core version.

The current source is unreleased `6.0.0.dev0` and removes the two TS workflows
without compatibility support. Read the [cutover warning](RELEASE.md#removing-ts-workflows-in-60)
before upgrading an existing runtime.

Both profiles use the `orca_auto` command. Python 3.11+ and Linux/WSL2 are
required. Install ORCA separately; workflow stages using xTB or CREST also need
those executables. None of these chemistry engines is bundled in the packages.

## Install release packages

The [v5.0.0 GitHub release](https://github.com/dhsohn/orca_auto/releases/tag/v5.0.0)
provides wheels, source distributions, and `SHA256SUMS`, not a PyPI publication.
These published packages predate the TS-workflow removal described above.
Download `orca_auto-5.0.0-py3-none-any.whl`. For workflows, also download
`orca_auto_workflows-5.0.0-py3-none-any.whl` into the same directory.

From that directory, create a fresh environment and install Core:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install ./orca_auto-5.0.0-py3-none-any.whl
```

To add Workflows in that environment:

```bash
python -m pip install ./orca_auto_workflows-5.0.0-py3-none-any.whl
```

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
