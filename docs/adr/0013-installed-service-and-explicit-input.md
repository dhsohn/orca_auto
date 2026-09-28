# ADR 0013: Installed services and explicit input selection

- Status: Accepted
- Date: 2026-09-28

## Problem

A wheel lacked service templates, so normal background installation depended on
a separate checkout. Directory submission could choose only the latest input,
making automation depend on modification time.

## Decision

Package the three canonical systemd templates under orca_auto/systemd_templates.
Prepared runtime assembly copies these same resources. systemd install without
--repo selects the current isolated virtual environment and invokes its Python
with -I; an explicit checkout or prepared runtime keeps its existing behavior.
The installation plan carries the selected interpreter through validation and
application. System-level permissions and configuration validation remain unchanged.

run-dir --input NAME.inp selects a confined regular file in the requested directory.
Paths and links are refused. Omission keeps the existing newest-input behavior;
binding and immutable generation snapshots remain owned by submission.

## Verification and limits

CLI tests verify explicit selection, independent hashes, snapshot immutability,
invalid selections and no writes on rejection. Service tests exercise rendering
and application in a temporary directory, never the operational unit directory.
The distribution gate checks the installed wheel from outside the checkout.

This adds interfaces without removing existing defaults. Unmanaged wheel installs
still lack the immutable build identity of a prepared production runtime.
