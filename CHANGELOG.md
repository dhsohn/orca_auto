# Changelog

All notable changes to orca_auto are documented in this file.

This project follows a lightweight [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
style. Version numbers are recorded in `pyproject.toml`; release procedure lives
in [docs/RELEASE.md](docs/RELEASE.md).

## [Unreleased]

### Added

- JOSS-style open-source operating materials: contribution guidance, issue and
  pull request templates, release checklist, validation documentation, and a
  fake ORCA smoke example.

## [0.1.0] - Initial public development series

### Added

- Queue-first ORCA runtime with durable queue state, worker execution, retry
  state/report files, and organized output support.
- Internal workflow support for xTB and CREST stages.
- Linux/WSL-first CLI, configuration template, systemd user-service assets, and
  CI coverage for fake-engine integration paths.
